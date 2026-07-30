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
import os

from beanie import PydanticObjectId
from prefect import flow, task
from prefect.client.orchestration import get_client

from tree.config.sources import WebSource
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


# Tier B — billable: each attempt is one Bright Data Web Unlocker request.
# Capped at 2 (ADR-002 amendment #096).
@flow(name="ingest-web-url-etl", log_prints=True, retries=2, retry_delay_seconds=5)
async def ingest_web_url(url: str, user_id: PydanticObjectId) -> Document | None:
    """Thin MCP-only @flow: ingest ONE URL via the core.

    The MCP ``ingest_url`` router (``tree.data.online_pipeline._ingest_web_url``, the generic-web
    fallback) calls this so single-URL ingest still gets its own Prefect flow run + Opik
    trace. The BATCH path does NOT call this — it runs the batch tasks directly.

    Retries live on the FLOW, not on per-row ``@task``s (ADR-002 #078–#082 keeps task
    grain at Batch): the core is one scrape + one load, so the flow IS the unit of work.
    Tier B — capped at ``retries=2`` because each attempt is a billable Bright Data Web
    Unlocker request. Safe because ``load_web_document`` dedups on
    ``(user_id, source_uri)`` — a re-run upserts, never double-inserts.
    """

    return await _ingest_web_url_one(url, user_id)


# Tier B — billable: scrapes via Bright Data Web Unlocker, so ONE batch replay
# re-bills ALL N urls. Capped at 2 (ADR-002 amendment #096); do NOT raise to 3.
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


@task(name="load-web-batch", retries=3, retry_delay_seconds=5)
async def load_batch(docs: list[Document]) -> list[Document]:
    """Dedup + persist each scraped Document via a SINGLE isolated gather.

    Awaits the pure ``web.load_web_document`` per element. Returns the successful,
    non-``None`` subset (duplicates drop as ``None``); a per-element failure is logged +
    skipped, NOT propagated. Retried whole-batch on a batch-WIDE infra failure, safe via
    the ``(user_id, source_uri)`` dedup (LATENT upgrade + ``DuplicateKeyError`` race
    handling). Tier F (idempotent Mongo write) → ``retries=3`` / 5 s = 15 s (ADR-002 #096).
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


async def ingest_web_batch(
    entries: list[WebSource], user_id: PydanticObjectId
) -> list[Document]:
    """Offline-dispatch adapter: ``[WebSource] -> ingest_web_url_batch([uri, ...])``.

    The unified offline dispatch (``offline_pipeline._PLATFORM_PIPELINES``) hands every
    platform pipeline its TYPED ``entries``; web has a single source kind, so this thin
    adapter just unwraps the URIs and runs the existing ``ingest_web_url_batch`` flow
    (left unchanged, since ``search_web`` triggers that same deployment with raw URLs).
    """

    return await ingest_web_url_batch([e.uri for e in entries], user_id)


# --- Fire-and-forget deployment trigger -------------------------------------
#
# Companion helper for the ``search_web`` MCP tool's optional ingestion path.
# Mirrors the trigger pattern in ``apps/memory/scripts/run_url_data_pipeline.py``
# but WITHOUT the polling/log-streaming loop — search_web is a sub-5s tool and
# must not block on a multi-minute batch ingest. The deployment must already be
# served (``make memory-serve-workflows``) for the trigger to succeed.

DEPLOYMENT_NAME = "ingest-web-url-batch-etl/ingest-web-url-batch-etl"


def _build_tracking_url(api_url: str, flow_run_id: str) -> str | None:
    """Construct a human-readable Prefect UI URL for a flow run.

    Strategy:
        1. If ``PREFECT_UI_URL`` is set, use it as-is (Prefect Cloud honors this).
        2. Otherwise, derive a local UI URL from ``api_url`` by stripping a
           trailing ``/api`` (the local Prefect server convention).
        3. If neither shape applies (e.g. a Cloud API URL we don't recognize),
           return ``None`` — search results stay useful even without a link.
    """

    ui_base = os.environ.get("PREFECT_UI_URL")
    if ui_base:
        return f"{ui_base.rstrip('/')}/runs/flow-run/{flow_run_id}"

    cleaned = api_url.rstrip("/")
    if cleaned.endswith("/api"):
        return f"{cleaned.removesuffix('/api')}/runs/flow-run/{flow_run_id}"

    return None


async def trigger_url_batch_ingest(
    urls: list[str], user_id: PydanticObjectId
) -> dict[str, str | None]:
    """Fire the ``ingest-web-url-batch-etl`` deployment with the given URLs.

    Looks up the deployment by name, creates a flow run with
    ``parameters={"urls": urls}``, and returns immediately. Does NOT wait for
    the run to finish.

    Args:
        urls: Non-empty list of URLs to ingest. The deployment validates the
            URL strings itself; this helper does not.

    Returns:
        A dict with two keys:
            - ``flow_run_id`` (str) — the Prefect flow-run UUID.
            - ``tracking_url`` (str | None) — a human-readable URL the caller
              can open to follow the run in the Prefect UI. ``None`` if we
              can't derive one (e.g. unfamiliar API URL shape and no
              ``PREFECT_UI_URL`` env var set).

    Raises:
        ValueError: If ``urls`` is empty.
        Exception: Any error raised by the Prefect client (deployment not
            found, connection refused, etc.) propagates to the caller. The
            ``search_web`` tool catches these and degrades gracefully.
    """

    if not urls:
        raise ValueError("urls must not be empty")

    async with get_client() as client:
        deployment = await client.read_deployment_by_name(DEPLOYMENT_NAME)

        flow_run = await client.create_flow_run_from_deployment(
            deployment_id=deployment.id,
            parameters={"urls": urls, "user_id": str(user_id)},
        )
        flow_run_id = str(flow_run.id)
        tracking_url = _build_tracking_url(str(client.api_url), flow_run_id)

        logger.info(
            "Triggered ingest-web-url-batch-etl flow run %s for %d URL(s)",
            flow_run_id,
            len(urls),
        )

    return {"flow_run_id": flow_run_id, "tracking_url": tracking_url}
