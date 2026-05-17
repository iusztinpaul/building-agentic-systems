import asyncio
import logging

from beanie import PydanticObjectId
from prefect import flow, task

from tree.config.settings import settings
from tree.data.substack.substack_rss import (
    extract_document,
    fetch_feed,
    load_document,
)
from tree.db import init_mongodb
from tree.entities.documents import Document

logger = logging.getLogger(__name__)


@task(name="fetch-substack-rss-feed", retries=2, retry_delay_seconds=5)
async def fetch_feed_task(source_uri: str) -> list[dict]:
    return await fetch_feed(source_uri)


@task(name="extract-substack-document")
async def extract_document_task(raw_entry: dict, user_id: PydanticObjectId) -> Document:
    return extract_document(raw_entry, user_id)


@task(name="load-substack-document", retries=1, retry_delay_seconds=2)
async def load_document_task(doc: Document, raw_entry: dict) -> Document | None:
    return await load_document(doc, raw_entry)


@flow(name="ingest-substack-rss-feed-etl", log_prints=True)
async def ingest_substack_rss_feed(
    feed_url: str, user_id: PydanticObjectId
) -> list[Document]:
    entries = await fetch_feed_task(feed_url)
    documents = [await extract_document_task(entry, user_id) for entry in entries]

    ingested: list[Document] = []
    for doc, entry in zip(documents, entries):
        result = await load_document_task(doc, entry)
        if result:
            ingested.append(result)

    logger.info("Ingested %d new documents from %s", len(ingested), feed_url)

    return ingested


@flow(name="ingest-substack-rss-feed-batch-etl", log_prints=True)
async def ingest_substack_rss_feed_batch(
    feed_urls: list[str], user_id: PydanticObjectId
) -> list[Document]:
    await init_mongodb(
        settings.mongo.mongo_uri.get_secret_value(),
        settings.mongo.mongo_initdb_database,
    )

    results = await asyncio.gather(
        *[ingest_substack_rss_feed(feed_url, user_id) for feed_url in feed_urls]
    )

    return [doc for docs in results for doc in docs]
