import asyncio
import logging

from prefect import flow, task

from tree.config.app_config import HuggingFaceDatasetSource, app_config
from tree.config.settings import settings
from tree.data.huggingface.arxiv_dataset import (
    extract_document as _extract_document,
    fetch_dataset_batches as _fetch_dataset_batches,
    fetch_paper_content as _fetch_paper_content,
    load_document as _load_document,
)
from tree.db import init_mongodb
from tree.entities.documents import Document

logger = logging.getLogger(__name__)

ARXIV_DATASET_ID = "librarian-bots/arxiv-metadata-snapshot"


@task(name="extract-arxiv-document")
def extract_document(raw_entry: dict) -> Document | None:
    return _extract_document(raw_entry)


@task(name="fetch-arxiv-paper-content", retries=1, retry_delay_seconds=5)
async def fetch_paper_content(doc: Document) -> Document:
    content = await _fetch_paper_content(doc.source_uri)
    if content:
        doc.content = content
    return doc


@task(name="load-arxiv-document", retries=1, retry_delay_seconds=2)
async def load_document(doc: Document) -> Document | None:
    return await _load_document(doc)


async def _process_document(
    doc: Document,
    do_fetch_content: bool,
    semaphore: asyncio.Semaphore,
) -> Document | None:
    """Process a single document: optionally fetch content, then load to DB."""

    async with semaphore:
        if do_fetch_content:
            doc = await fetch_paper_content(doc)
        return await load_document(doc)


def _get_huggingface_arxiv_defaults() -> tuple[int, bool, int, int]:
    """Return (max_samples, fetch_content, batch_size, concurrency) for HF arxiv.

    Walks the flat ``app_config.sources.sources`` list and picks the first
    ``HuggingFaceDatasetSource`` entry whose ``uri`` matches the arxiv dataset
    id. Falls back to ``HuggingFaceDatasetSource(uri=ARXIV_DATASET_ID)``
    defaults if no such entry exists.
    """

    for entry in app_config.sources.sources:
        if (
            isinstance(entry, HuggingFaceDatasetSource)
            and entry.uri == ARXIV_DATASET_ID
        ):
            return (
                entry.max_samples,
                entry.fetch_content,
                entry.batch_size,
                entry.concurrency,
            )

    fallback = HuggingFaceDatasetSource(uri=ARXIV_DATASET_ID)
    return (
        fallback.max_samples,
        fallback.fetch_content,
        fallback.batch_size,
        fallback.concurrency,
    )


@flow(name="ingest-arxiv-dataset-etl", log_prints=True)
async def ingest_arxiv_dataset(
    max_samples: int | None = None,
    fetch_content: bool | None = None,
) -> list[Document]:
    (
        default_max_samples,
        default_fetch_content,
        batch_size,
        concurrency,
    ) = _get_huggingface_arxiv_defaults()
    if max_samples is None:
        max_samples = default_max_samples
    if fetch_content is None:
        fetch_content = default_fetch_content

    await init_mongodb(
        settings.mongo.mongo_uri.get_secret_value(),
        settings.mongo.mongo_initdb_database,
    )

    semaphore = asyncio.Semaphore(concurrency)
    ingested: list[Document] = []

    for batch in _fetch_dataset_batches(max_samples, batch_size):
        documents = [extract_document(entry) for entry in batch]

        results = await asyncio.gather(
            *[
                _process_document(doc, fetch_content, semaphore)
                for doc in documents
                if doc is not None
            ]
        )
        ingested.extend(r for r in results if r is not None)

        logger.info("Batch processed: %d ingested so far", len(ingested))

    logger.info("Ingested %d new arxiv documents", len(ingested))

    return ingested
