import logging

from prefect import flow, task

from twin.config.app_config import app_config
from twin.config.settings import settings
from twin.data.huggingface.arxiv_dataset import (
    extract_document,
    fetch_dataset,
    load_document,
)
from twin.db import init_mongodb
from twin.entities.documents import Document

logger = logging.getLogger(__name__)


@task(name="fetch-arxiv-dataset", retries=2, retry_delay_seconds=10)
def fetch_dataset_task(max_samples: int) -> list[dict]:
    return fetch_dataset(max_samples)


@task(name="extract-arxiv-document")
def extract_document_task(raw_entry: dict) -> Document:
    return extract_document(raw_entry)


@task(name="load-arxiv-document", retries=1, retry_delay_seconds=2)
async def load_document_task(doc: Document) -> Document | None:
    return await load_document(doc)


@flow(name="ingest-arxiv-dataset-etl", log_prints=True)
async def ingest_arxiv_dataset(max_samples: int | None = None) -> list[Document]:
    if max_samples is None:
        max_samples = app_config.sources.huggingface_arxiv_dataset.max_samples

    await init_mongodb(
        settings.mongo.mongo_uri.get_secret_value(),
        settings.mongo.mongo_initdb_database,
    )

    entries = fetch_dataset_task(max_samples)
    documents = [extract_document_task(entry) for entry in entries]

    ingested: list[Document] = []
    for doc in documents:
        result = await load_document_task(doc)
        if result:
            ingested.append(result)

    logger.info("Ingested %d new arxiv documents", len(ingested))

    return ingested
