"""
Realtime (online) ingestion orchestration — the one cross-pipeline flow.

Sits at the top level (like :mod:`tree.orchestrator`) because it spans BOTH
pipelines: the data step (``tree.data.online_pipeline.online_ingest``) and the
memory step (``memory_extract_etl_worker``). Neither pipeline package may
import the other; this module imports both.

Async-first: callers (MCP ingest tools + the CLI) funnel through
:func:`dispatch_online_pipeline`, which validates at the edge and fires the
``online-pipeline`` core deployment fire-and-forget — ONE flow run on a Prefect
worker ingests the source into ``documents``, runs the extraction worker inline
AND runs the trailing indexing inline. Dispatch REQUIRES a reachable Prefect API
with that deployment registered; there is no in-process fallback, so submission
failures propagate to the caller.
"""

import logging
from typing import Any

from beanie import PydanticObjectId
from prefect import flow, tags
from prefect.deployments import run_deployment
from pydantic import TypeAdapter

from tree.config.settings import settings
from tree.data.online_pipeline import (
    OnlineSource,
    UrlSource,
    online_ingest,
    validate_url,
)
from tree.db import init_mongodb
from tree.flow_runs import flow_run_status
from tree.memory.extraction.pipeline import memory_extract_etl_worker
from tree.memory.indexing.pipeline import memory_indexing
from tree.config.constants import (
    TAGS_INDEXING,
    TAG_DATA_PIPELINE,
    TAG_MEMORY_PIPELINE,
    TAG_ONLINE,
)
from tree.observability import (
    configure_opik,
    flush_opik,
    get_distributed_trace_headers,
    pipeline_metadata,
    span,
)

logger = logging.getLogger(__name__)

_ONLINE_METADATA = pipeline_metadata("data")

# Spans/deployment tags for the end-to-end run: it IS both pipelines, online.
TAGS_ONLINE_PIPELINE = [TAG_DATA_PIPELINE, TAG_MEMORY_PIPELINE, TAG_ONLINE]
_ONLINE_SOURCE_ADAPTER: TypeAdapter[OnlineSource] = TypeAdapter(OnlineSource)

# Prefect caps flow-run parameter payloads (~512KB server-side)
MAX_SOURCE_PAYLOAD_BYTES = 400_000


@flow(name="online-pipeline", log_prints=True)
async def online_pipeline(
    source: Any,
    user_id: PydanticObjectId,
    run_extraction: bool = True,
    opik_trace_headers: dict[str, str] | None = None,
) -> str | None:
    """The realtime ingest FLOW: data step + extraction, ONE run end-to-end.

    The online counterpart of :func:`tree.offline.offline_pipeline`, collapsed for a
    single source. Callers never invoke it directly — they go through
    :func:`dispatch_online_pipeline`, which fires it as a deployment run.

    ``run_extraction`` (default true) runs the memory step INSIDE this run:
    the extraction WORKER as an inline subflow — deliberately the worker, not
    the coordinator. The worker is pure work (no ``run_deployment``, no
    waiting on child runs), so N concurrent online ingests can never become N
    waiting parents starving their own children out of the ``serve(limit)``
    pool. A single document needs no shard fan-out, which is all the
    coordinator would add. ``memory_indexing`` then runs ONCE as an inline
    subflow (:func:`_run_indexing`), mirroring the coordinator's
    index-once-after-extraction contract — since ``free-tier-deployments``
    indexing is a flow, not a deployment.

    ``source`` arrives JSON-serialized (a ``dict``) when dispatched via a
    deployment; already-typed :data:`OnlineSource` objects pass through
    unchanged — the same coercion contract as ``offline_pipeline._coerce_sources``.

    Observability: configures Opik at entry (worker-subprocess-safe) and owns one
    span; ``opik_trace_headers`` forwarded by the dispatcher nests it under the
    caller's trace across the process hop (same pattern as ``data_etl_worker``).

    Returns the ingested document id (a caller polling the run to completion can
    recover it from the flow-run result), or ``None`` for a duplicate. A
    failed extraction fails THIS run (visible, retryable via
    ``make memory-run-memory-pipeline MODE=online DOC_IDS=<id>`` — a
    plain re-run would dedupe on the data step and skip the memory step).
    """

    configure_opik()
    try:
        with span(
            "online-pipeline",
            tags=TAGS_ONLINE_PIPELINE,
            trace_headers=opik_trace_headers,
            metadata=_ONLINE_METADATA,
        ):
            await init_mongodb(
                settings.mongo.mongo_uri.get_secret_value(),
                settings.mongo.mongo_initdb_database,
            )
            typed_source = (
                _ONLINE_SOURCE_ADAPTER.validate_python(source)
                if isinstance(source, dict)
                else source
            )
            document = await online_ingest(typed_source, user_id)
            if document is None:
                logger.info(
                    "Already ingested (duplicate): %s source", typed_source.type
                )
                return None
            if run_extraction:
                # Inline subflow: tagged at the call site, since it inherits
                # neither this run's deployment tags nor the worker deployment's.
                with tags(TAG_MEMORY_PIPELINE, TAG_ONLINE):
                    summary = await memory_extract_etl_worker(
                        user_id=user_id, document_ids=[str(document.id)]
                    )
                logger.info(
                    "Extracted document %s: %d nodes, %d edges written",
                    document.id,
                    summary.nodes_written,
                    summary.edges_written,
                )
                await _run_indexing(user_id)
            return str(document.id)
    finally:
        # Fail-open telemetry flush — the worker subprocess exits after the run.
        flush_opik()


async def _run_indexing(user_id: PydanticObjectId) -> None:
    """Run the trailing ``memory_indexing`` flow ONCE, inline, fail-open.

    Indexing stopped being a deployment with ``free-tier-deployments``, so this
    is an inline subflow of the current ``online-pipeline`` run instead of a
    submitted flow run. Opik nesting is preserved WITHOUT passing headers: the
    subflow runs in THIS process, inside this flow's span, so its span nests via
    the in-process context — the same reason the inline
    ``memory_extract_etl_worker`` call above forwards none. (The deleted
    deployment call passed no headers either.)

    Fail-open, unchanged: a failure is WARNING-logged and NEVER fails the ingest —
    the document and its graph content are already durable, and any later
    indexing run (it is a global backfill over unembedded nodes) covers the gap.
    """

    try:
        with tags(*TAGS_INDEXING):
            await memory_indexing(user_id=user_id)
        logger.info("Indexed inline for user %s", user_id)
    except Exception as exc:  # noqa: BLE001 — indexing gap, not an ingest failure.
        logger.warning(
            "Inline memory_indexing failed (%s: %s); nodes stay "
            "unembedded until the next indexing run",
            type(exc).__name__,
            exc,
        )


def validate_online_source(source: OnlineSource) -> None:
    """Cheap edge validation run BEFORE submitting a deployment run — no I/O.

    Deployment dispatch is fire-and-forget, so anything not validated here
    surfaces only as a remote flow-run failure. Checks what a caller can get
    wrong synchronously: URL shape (:func:`validate_url`) and the flow-run
    parameter payload cap.

    Raises:
        ValueError: invalid URL, or payload over :data:`MAX_SOURCE_PAYLOAD_BYTES`
            (the caller should split or summarize the content and retry).
    """

    if isinstance(source, UrlSource):
        validate_url(source.uri)
    payload_bytes = len(source.model_dump_json().encode())
    if payload_bytes > MAX_SOURCE_PAYLOAD_BYTES:
        raise ValueError(
            f"Source payload is {payload_bytes} bytes — over the "
            f"{MAX_SOURCE_PAYLOAD_BYTES}-byte flow-run parameter cap. "
            "Split or summarize the content and retry."
        )


async def dispatch_online_pipeline(
    source: OnlineSource,
    user_id: PydanticObjectId,
    *,
    run_extraction: bool = True,
) -> dict[str, Any]:
    """Submit ``source`` to the online pipeline; the ONE entry point for callers.

    Async-first: validates at the edge, then fires the ``online-pipeline`` core
    deployment fire-and-forget (``timeout=0``) — ONE worker-side flow run does
    the data step AND the extraction, so the caller returns in the time it
    takes to create a flow run, with::

        {"status": <flow-run state, lowercased>, "flow_run_id": ...}

    ``status`` is Prefect's own state name for the freshly created run
    (:func:`tree.flow_runs.flow_run_status` — ``scheduled`` normally), NOT an
    ingest outcome: whether the source was new or a duplicate is decided later,
    on the worker, and lives in the flow run's result.

    There is exactly ONE path: dispatch requires a reachable Prefect API with
    the deployment registered. Submission failures (unreachable API, missing
    deployment, parameter validation, auth) PROPAGATE — a caller must see them
    rather than have them silently swapped for a long blocking in-process run.

    Raises:
        ValueError: from :func:`validate_online_source` (bad URL / oversized
            payload) — the only failure raised before a run exists; ingest
            errors live in the flow run.
    """

    validate_online_source(source)

    flow_run = await run_deployment(
        "online-pipeline/online-pipeline",
        parameters={
            "source": source.model_dump(mode="json"),
            "user_id": str(user_id),
            "run_extraction": run_extraction,
            "opik_trace_headers": get_distributed_trace_headers(),
        },
        timeout=0,
    )
    return {"status": flow_run_status(flow_run), "flow_run_id": str(flow_run.id)}
