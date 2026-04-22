import asyncio
import logging

from prefect import flow, task

from tree.config.settings import settings
from tree.data.substack.substack_article import (
    fetch_and_extract,
    load_article_document,
)
from tree.db import init_mongodb
from tree.entities.documents import Document

logger = logging.getLogger(__name__)


@task(name="fetch-and-extract-substack-article", retries=2, retry_delay_seconds=5)
async def fetch_and_extract_task(article_url: str) -> tuple[Document, str]:
    return await fetch_and_extract(article_url)


@task(name="load-substack-article-document", retries=1, retry_delay_seconds=2)
async def load_article_document_task(doc: Document, body_html: str) -> Document | None:
    return await load_article_document(doc, body_html)


@flow(name="ingest-substack-article-etl", log_prints=True)
async def ingest_substack_article(article_url: str) -> Document | None:
    doc, body_html = await fetch_and_extract_task(article_url)
    result = await load_article_document_task(doc, body_html)

    if result:
        logger.info("Ingested article: %s", article_url)
    else:
        logger.info("Skipped duplicate article: %s", article_url)

    return result


@flow(name="ingest-substack-article-batch-etl", log_prints=True)
async def ingest_substack_article_batch(article_urls: list[str]) -> list[Document]:
    await init_mongodb(
        settings.mongo.mongo_uri.get_secret_value(),
        settings.mongo.mongo_initdb_database,
    )

    results = await asyncio.gather(
        *[ingest_substack_article(url) for url in article_urls]
    )

    ingested = [doc for doc in results if doc is not None]
    logger.info("Ingested %d articles out of %d URLs", len(ingested), len(article_urls))

    return ingested
