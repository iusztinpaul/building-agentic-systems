"""Generic web leaf pipeline — batch-grain ETL tasks + thin MCP flow (#081).

The web path SCRAPES each URL via Bright Data Web Unlocker, so Extract+Transform FUSE —
one scrape yields the Document. The batch flow runs two ETL-phase tasks over the WHOLE
handed-in URL list:

* ``extract_batch`` (network Extract+Transform fused, ``retries=2``) —
  ``list[str] -> list[Document]`` via the pure ``web.fetch_and_extract_web`` under
  ``tree.data.batch.gather_isolated`` (per-URL scrape failures logged + skipped).
* ``load_batch`` (DB Load, ``retries=1``) — dedups + persists each Document via the pure
  ``web.load_web_document``, again isolated per element.

The per-item sub-flow's body is demoted to the plain async core ``_ingest_web_url_one``;
``ingest_web_url`` remains a THIN 1-line @flow wrapper used ONLY by the MCP URL router
(``tree.data.online_pipeline``, the generic-web fallback). The batch path calls the batch tasks
directly — NEVER the thin wrapper (no per-item sub-flow runs).

Result persistence is OFF by default in Prefect 3.6, so these side-effecting tasks do NOT
persist results — no flag is added.
"""

import logging

from beanie import PydanticObjectId
from prefect import flow, task

from tree.config.settings import settings
from tree.data.batch import gather_isolated
from tree.data.web.web import fetch_and_extract_web, load_web_document
from tree.db import init_mongodb
from tree.entities.documents import Document

logger = logging.getLogger(__name__)


async def _ingest_web_url_one(url: str, user_id: PydanticObjectId) -> Document | None:
    """Scrape + persist a single URL (plain async core, NO decorators).

    Fetches and extracts the URL via the pure ``fetch_and_extract_web``, then loads it via
    the pure ``load_web_document``. Returns the persisted Document, or ``None`` if it was a
    duplicate. Shared by the thin MCP flow (one URL) — the batch path uses the batch tasks
    instead.
    """

    doc = await fetch_and_extract_web(url, user_id)
    result = await load_web_document(doc)

    if result:
        logger.info("Ingested web URL: %s", url)
    else:
        logger.info("Skipped duplicate web URL: %s", url)

    return result


@flow(name="ingest-web-url-etl", log_prints=True)
async def ingest_web_url(url: str, user_id: PydanticObjectId) -> Document | None:
    """Thin MCP-only @flow: ingest ONE URL via the core.

    The MCP ``ingest_url`` router (``tree.data.online_pipeline._ingest_web_url``, the generic-web
    fallback) calls this so single-URL ingest still gets its own Prefect flow run + Opik
    trace. The BATCH path does NOT call this — it runs the batch tasks directly.
    """

    return await _ingest_web_url_one(url, user_id)


@task(name="extract-web-batch", retries=2, retry_delay_seconds=5)
async def extract_batch(urls: list[str], user_id: PydanticObjectId) -> list[Document]:
    """Scrape each URL into a Document via a SINGLE isolated gather.

    Extract+Transform are FUSED (one scrape yields the Document). Runs the pure
    ``web.fetch_and_extract_web`` per URL; a per-URL scrape failure is logged + skipped,
    NOT propagated. Network → ``retries=2`` (whole-batch retry on a batch-WIDE failure).
    """

    async def _extract(url: str) -> Document:
        return await fetch_and_extract_web(url, user_id)

    extracted, failures = await gather_isolated(urls, _extract)
    if failures:
        logger.warning(
            "extract_batch: %d/%d URLs failed to scrape", failures, len(urls)
        )
    return extracted


@task(name="load-web-batch", retries=1, retry_delay_seconds=2)
async def load_batch(docs: list[Document]) -> list[Document]:
    """Dedup + persist each scraped Document via a SINGLE isolated gather.

    Awaits the pure ``web.load_web_document`` per element. Returns the successful,
    non-``None`` subset (duplicates drop as ``None``); a per-element failure is logged +
    skipped, NOT propagated. Retried whole-batch on a batch-WIDE infra failure
    (``retries=1``), safe via the ``(user_id, source_uri)`` dedup (LATENT upgrade +
    ``DuplicateKeyError`` race handling).
    """

    ingested, failures = await gather_isolated(docs, load_web_document)
    if failures:
        logger.warning("load_batch: %d/%d web documents failed", failures, len(docs))
    return ingested


@flow(name="ingest-web-url-batch-etl", log_prints=True)
async def ingest_web_url_batch(
    urls: list[str], user_id: PydanticObjectId
) -> list[Document]:
    """Batch-ingest URLs via the batch tasks (NOT the per-item sub-flow).

    Initialises MongoDB once, then runs ``extract_batch`` (scrape each URL) followed by
    ``load_batch`` (persist each) — each ONCE over the whole URL list. The thin
    ``ingest_web_url`` flow is NEVER invoked here, so the batch produces no per-item
    sub-flow runs.
    """

    await init_mongodb(
        settings.mongo.mongo_uri.get_secret_value(),
        settings.mongo.mongo_initdb_database,
    )

    extracted = await extract_batch(urls, user_id)
    ingested = await load_batch(extracted)

    logger.info("Ingested %d web URLs out of %d", len(ingested), len(urls))

    return ingested
