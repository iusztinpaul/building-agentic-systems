import asyncio
import logging

from beanie import PydanticObjectId
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


def arxiv_window_entries(
    entry: HuggingFaceDatasetSource,
) -> list[HuggingFaceDatasetSource]:
    """Fan ONE HuggingFace dataset entry into its disjoint offset-**Window**s.

    Pure decision logic (no DB, no Prefect) shared by the data orchestrator (#072,
    ADR-002 §3 amendment #070–#074): the orchestrator calls this per configured
    ``HuggingFaceDatasetSource`` and dispatches one ``data-etl-worker`` run per
    returned window-entry. Each returned entry is a COPY of ``entry`` (the configured
    entry is NEVER mutated — ``offset`` is a dispatch-time runtime coordinate, #070)
    stamped with that window's ``offset`` + ``max_samples`` via ``model_copy``.

    Window math (``n = entry.num_workers``, ``m = entry.max_samples``):

    * ``window_size = m // n``; window ``i`` ⇒ ``offset = i * window_size`` and
      ``max_samples = window_size``, EXCEPT the LAST window which takes the remainder
      ``m - offset`` so the windows tile ``[0, m)`` exactly (no gap, no overlap, no
      dropped rows when ``m`` isn't divisible by ``n``).
    * ``num_workers == 1`` ⇒ a single window with ``offset`` left UNSET (``None``) and
      ``max_samples`` unchanged — byte-identical to the pre-feature single HF run.
    * ``max_samples == 0`` ⇒ NO windows (empty list — a clean no-op for that entry).
    * ``num_workers > max_samples`` (with ``m >= 1``) ⇒ CLAMP the effective worker
      count to ``m`` so no window has ``max_samples <= 0``: emit ``m`` windows of
      size 1 tiling ``[0, m)``.

    Returns an order-stable list (window 0 first) so callers/tests can assert exact
    shard contents.
    """

    max_samples = entry.max_samples
    if max_samples <= 0:
        return []

    # A single worker reproduces today's run exactly: no offset, full max_samples.
    if entry.num_workers <= 1:
        return [entry.model_copy()]

    # Clamp so no window collapses to <= 0 rows: at most one window per row.
    effective_workers = min(entry.num_workers, max_samples)
    window_size = max_samples // effective_workers

    windows: list[HuggingFaceDatasetSource] = []
    for i in range(effective_workers):
        offset = i * window_size
        is_last = i == effective_workers - 1
        window_max_samples = max_samples - offset if is_last else window_size
        windows.append(
            entry.model_copy(
                update={"offset": offset, "max_samples": window_max_samples}
            )
        )
    return windows


@task(name="extract-arxiv-document")
def extract_document(raw_entry: dict, user_id: PydanticObjectId) -> Document | None:
    return _extract_document(raw_entry, user_id)


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
    user_id: PydanticObjectId,
    max_samples: int | None = None,
    fetch_content: bool | None = None,
    offset: int | None = None,
) -> list[Document]:
    """Ingest the arxiv HF dataset for ``user_id``.

    ``offset`` (#071) selects a disjoint window of the stream: the ingest skips the
    first ``offset`` rows and then streams ``max_samples`` rows — i.e. this run
    persists rows ``[offset, offset + max_samples)``. ``offset=None`` (the default,
    and what a non-windowed entry forwards) applies NO skip and reproduces today's
    single-run ingest exactly.
    """

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

    for batch in _fetch_dataset_batches(max_samples, batch_size, offset=offset):
        documents = [extract_document(entry, user_id) for entry in batch]

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
