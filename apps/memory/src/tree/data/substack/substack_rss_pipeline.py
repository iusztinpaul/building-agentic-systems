"""Substack RSS leaf pipeline — batch-grain ETL tasks (#079, #078 pattern).

The RSS path builds Documents from FEED-EMBEDDED content; it NEVER re-scrapes the
articles (``substack_article.fetch_and_extract`` is intentionally not imported here).
Per configured feed the batch flow runs three ETL-phase tasks:

* ``fetch_feed_task`` (Extract, per feed, ``retries=2``) — the existing
  ``substack_rss.fetch_feed``, returning the feed's raw entries.
* ``transform_batch`` (pure map, ``retries=0``) — ``list[dict] -> list[Document]`` via
  the pure ``substack_rss.extract_document``. No network, no DB.
* ``load_batch`` (DB Load, ``retries=1``) — dedups + persists each ``(doc, raw_entry)``
  via the shared ``substack_rss.load_document`` under
  ``tree.data.batch.gather_isolated`` (per-element failures logged + skipped). Reference
  resolution still reads the feed-embedded ``raw_entry`` exactly as before.

Result persistence is OFF by default in Prefect 3.6 (the repo sets no ``persist_result``
/ ``result_storage`` / ``cache_policy``), so these side-effecting tasks already do NOT
persist results — no flag is added.
"""

import asyncio
import logging

from beanie import PydanticObjectId
from prefect import flow, task

from tree.config.settings import settings
from tree.data.batch import gather_isolated
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


@task(name="transform-substack-rss-batch", retries=0)
async def transform_batch(
    entries: list[dict], user_id: PydanticObjectId
) -> list[Document]:
    """Pure map ``list[dict] -> list[Document]`` over one feed's entries.

    Runs the pure ``substack_rss.extract_document`` per feed entry — building from the
    feed-embedded content (NO re-scrape). No network, no DB → ``retries=0``.
    """

    return [extract_document(entry, user_id) for entry in entries]


@task(name="load-substack-rss-batch", retries=1, retry_delay_seconds=2)
async def load_batch(docs: list[Document], entries: list[dict]) -> list[Document]:
    """Dedup + persist one feed's documents via a SINGLE isolated gather.

    Awaits the shared ``substack_rss.load_document(doc, raw_entry)`` per
    ``(doc, entry)`` pair; reference resolution still reads the feed-embedded
    ``raw_entry``. Returns the successful, non-``None`` subset (duplicates drop as
    ``None``); a per-element failure is logged + skipped, NOT propagated. Retried
    whole-batch on a batch-WIDE infra failure (``retries=1``), safe via the
    ``(user_id, source_uri)`` dedup.
    """

    async def _load(pair: tuple[Document, dict]) -> Document | None:
        doc, entry = pair
        return await load_document(doc, entry)

    ingested, failures = await gather_isolated(list(zip(docs, entries)), _load)
    if failures:
        logger.warning("load_batch: %d/%d entries failed", failures, len(docs))
    return ingested


@flow(name="ingest-substack-rss-feed-batch-etl", log_prints=True)
async def ingest_substack_rss_feed_batch(
    feed_urls: list[str], user_id: PydanticObjectId
) -> list[Document]:
    """Ingest each configured RSS feed from its feed-embedded content.

    Initialises MongoDB once, then runs the per-feed batch path
    (``fetch_feed`` → ``transform_batch`` → ``load_batch``) for every feed under
    ``asyncio.gather(return_exceptions=True)`` so one bad feed is logged + skipped and
    never sinks the others. NO per-feed sub-flow runs — the per-feed body is inlined.
    """

    await init_mongodb(
        settings.mongo.mongo_uri.get_secret_value(),
        settings.mongo.mongo_initdb_database,
    )

    async def _ingest_one_feed(feed_url: str) -> list[Document]:
        entries = await fetch_feed_task(feed_url)
        documents = await transform_batch(entries, user_id)
        ingested = await load_batch(documents, entries)
        logger.info("Ingested %d new documents from %s", len(ingested), feed_url)
        return ingested

    results = await asyncio.gather(
        *[_ingest_one_feed(feed_url) for feed_url in feed_urls],
        return_exceptions=True,
    )

    all_ingested: list[Document] = []
    for feed_url, result in zip(feed_urls, results, strict=True):
        if isinstance(result, BaseException):
            logger.warning(
                "Failed to ingest feed %s; skipping", feed_url, exc_info=result
            )
            continue
        all_ingested.extend(result)

    return all_ingested
