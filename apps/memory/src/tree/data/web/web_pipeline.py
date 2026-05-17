"""Prefect tasks and flows for ingesting web URLs via Bright Data Web Unlocker.

Mirrors ``tree.data.substack.substack_article_pipeline``:

- Two ``@task`` wrappers (``fetch-and-extract-web`` with ``retries=2``,
  ``load-web-document`` with ``retries=1``).
- Single-URL flow ``ingest-web-url-etl`` which assumes MongoDB is initialised
  by the caller.
- Batch flow ``ingest-web-url-batch-etl`` which initialises MongoDB itself
  (top-level entry point) and fans out per-URL ingest via ``asyncio.gather``.
"""

from __future__ import annotations

import asyncio
import logging

from beanie import PydanticObjectId
from prefect import flow, task

from tree.config.settings import settings
from tree.data.web.web import fetch_and_extract_web, load_web_document
from tree.db import init_mongodb
from tree.entities.documents import Document

logger = logging.getLogger(__name__)


@task(name="fetch-and-extract-web", retries=2, retry_delay_seconds=5)
async def fetch_and_extract_web_task(url: str, user_id: PydanticObjectId) -> Document:
    return await fetch_and_extract_web(url, user_id)


@task(name="load-web-document", retries=1, retry_delay_seconds=2)
async def load_web_document_task(doc: Document) -> Document | None:
    return await load_web_document(doc)


@flow(name="ingest-web-url-etl", log_prints=True)
async def ingest_web_url(url: str, user_id: PydanticObjectId) -> Document | None:
    """Ingest a single URL. Assumes MongoDB is initialised by the caller."""

    doc = await fetch_and_extract_web_task(url, user_id)
    result = await load_web_document_task(doc)

    if result:
        logger.info("Ingested web URL: %s", url)
    else:
        logger.info("Skipped duplicate web URL: %s", url)

    return result


@flow(name="ingest-web-url-batch-etl", log_prints=True)
async def ingest_web_url_batch(
    urls: list[str], user_id: PydanticObjectId
) -> list[Document]:
    """Batch-ingest URLs. Initialises MongoDB once at the top, then fans out."""

    await init_mongodb(
        settings.mongo.mongo_uri.get_secret_value(),
        settings.mongo.mongo_initdb_database,
    )

    results = await asyncio.gather(*[ingest_web_url(url, user_id) for url in urls])

    ingested = [doc for doc in results if doc is not None]
    logger.info("Ingested %d web URLs out of %d", len(ingested), len(urls))

    return ingested
