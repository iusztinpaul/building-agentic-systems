"""Substack article leaf pipeline — batch-grain ETL tasks + thin MCP flow (#079).

The article path SCRAPES each URL, so Extract+Transform FUSE — one scrape yields the
Document. The batch flow runs two ETL-phase tasks over the WHOLE handed-in URL list:

* ``extract_batch`` (network Extract+Transform fused, ``retries=2``) —
  ``list[str] -> list[tuple[Document, str]]`` via the pure
  ``substack_article.fetch_and_extract`` under ``tree.data.batch.gather_isolated``
  (per-URL scrape failures logged + skipped).
* ``load_batch`` (DB Load, ``retries=1``) — dedups + persists each
  ``(doc, body_html)`` via the SHARED ``substack_article.load_article_document`` (which
  delegates to ``substack_rss.load_document``), again isolated per element.

The per-item sub-flow's body is demoted to the plain async core
``_ingest_substack_article_one``; ``ingest_substack_article`` remains a THIN 1-line
@flow wrapper used ONLY by the MCP URL router (``tree.data.online_pipeline``). The batch path
calls the batch tasks directly — NEVER the thin wrapper (no per-item sub-flow runs).

Result persistence is OFF by default in Prefect 3.6, so these side-effecting tasks do
NOT persist results — no flag is added.
"""

import logging

from beanie import PydanticObjectId
from prefect import flow, task

from tree.config.settings import settings
from tree.data.batch import gather_isolated
from tree.data.substack.substack_article import (
    fetch_and_extract,
    load_article_document,
)
from tree.db import init_mongodb
from tree.entities.documents import Document

logger = logging.getLogger(__name__)


async def _ingest_substack_article_one(
    article_url: str, user_id: PydanticObjectId
) -> Document | None:
    """Scrape + persist a single Substack article (plain async core, NO decorators).

    Fetches and extracts the article via the pure ``fetch_and_extract``, then loads it
    via the SHARED ``load_article_document``. Returns the persisted Document, or
    ``None`` if it was a duplicate. Shared by the thin MCP flow (one URL) — the batch
    path uses the batch tasks instead.
    """

    doc, body_html = await fetch_and_extract(article_url, user_id)
    result = await load_article_document(doc, body_html)

    if result:
        logger.info("Ingested article: %s", article_url)
    else:
        logger.info("Skipped duplicate article: %s", article_url)

    return result


@flow(name="ingest-substack-article-etl", log_prints=True)
async def ingest_substack_article(
    article_url: str, user_id: PydanticObjectId
) -> Document | None:
    """Thin MCP-only @flow: ingest ONE article via the core.

    The MCP ``ingest_url`` router (``tree.data.online_pipeline._ingest_substack_article``) calls
    this so single-URL ingest still gets its own Prefect flow run + Opik trace. The
    BATCH path does NOT call this — it runs the batch tasks directly.
    """

    return await _ingest_substack_article_one(article_url, user_id)


@task(name="extract-substack-article-batch", retries=2, retry_delay_seconds=5)
async def extract_batch(
    article_urls: list[str], user_id: PydanticObjectId
) -> list[tuple[Document, str]]:
    """Scrape each URL into ``(Document, body_html)`` via a SINGLE isolated gather.

    Extract+Transform are FUSED (one scrape yields the Document). Runs the pure
    ``substack_article.fetch_and_extract`` per URL; a per-URL scrape failure is logged +
    skipped, NOT propagated. Network → ``retries=2`` (whole-batch retry on a batch-WIDE
    failure).
    """

    async def _extract(url: str) -> tuple[Document, str]:
        return await fetch_and_extract(url, user_id)

    extracted, failures = await gather_isolated(article_urls, _extract)
    if failures:
        logger.warning(
            "extract_batch: %d/%d URLs failed to scrape", failures, len(article_urls)
        )
    return extracted


@task(name="load-substack-article-batch", retries=1, retry_delay_seconds=2)
async def load_batch(extracted: list[tuple[Document, str]]) -> list[Document]:
    """Dedup + persist each scraped article via a SINGLE isolated gather.

    Awaits the SHARED ``substack_article.load_article_document(doc, body_html)`` (which
    delegates to ``substack_rss.load_document`` — reference resolution identical to the
    RSS path) per element. Returns the successful, non-``None`` subset (duplicates drop
    as ``None``); a per-element failure is logged + skipped, NOT propagated. Retried
    whole-batch on a batch-WIDE infra failure (``retries=1``), safe via the
    ``(user_id, source_uri)`` dedup.
    """

    async def _load(pair: tuple[Document, str]) -> Document | None:
        doc, body_html = pair
        return await load_article_document(doc, body_html)

    ingested, failures = await gather_isolated(extracted, _load)
    if failures:
        logger.warning("load_batch: %d/%d articles failed", failures, len(extracted))
    return ingested


@flow(name="ingest-substack-article-batch-etl", log_prints=True)
async def ingest_substack_article_batch(
    article_urls: list[str], user_id: PydanticObjectId
) -> list[Document]:
    """Batch-ingest article URLs via the batch tasks (NOT the per-item sub-flow).

    Initialises MongoDB once, then runs ``extract_batch`` (scrape each URL) followed by
    ``load_batch`` (persist each) — each ONCE over the whole URL list. The thin
    ``ingest_substack_article`` flow is NEVER invoked here, so the batch produces no
    per-item sub-flow runs.
    """

    await init_mongodb(
        settings.mongo.mongo_uri.get_secret_value(),
        settings.mongo.mongo_initdb_database,
    )

    extracted = await extract_batch(article_urls, user_id)
    ingested = await load_batch(extracted)

    logger.info("Ingested %d articles out of %d URLs", len(ingested), len(article_urls))

    return ingested
