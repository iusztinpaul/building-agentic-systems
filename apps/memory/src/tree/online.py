"""
Realtime (online) ingestion orchestration — the one cross-pipeline flow.

Sits at the top level (like :mod:`tree.orchestrator`) because it spans BOTH
pipelines: the data step (``tree.data.online_pipeline.online_ingest``) and the
memory step (``memory_extract_etl_worker``). Neither pipeline package may
import the other; this module imports both.

Async-first: callers (MCP ingest tools + the CLI) funnel through
:func:`dispatch_online_pipeline`, which validates at the edge and fires the
``online-pipeline`` deployment fire-and-forget — ONE flow run on a Prefect
worker ingests the source into ``documents`` AND runs the extraction worker
inline, then submits the trailing indexing run. When no deployment is
registered (e.g. the free-tier deployment cap) the SAME flow runs inline in
the caller's process.
"""

import logging
from typing import Any

from beanie import PydanticObjectId
from prefect import flow
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
from tree.memory.extraction.pipeline import memory_extract_etl_worker
from tree.config.constants import (
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
    :func:`dispatch_online_pipeline`, which fires it as a deployment when one is
    registered and runs it inline otherwise.

    ``run_extraction`` (default true) runs the memory step INSIDE this run:
    the extraction WORKER as an inline subflow — deliberately the worker, not
    the coordinator. The worker is pure work (no ``run_deployment``, no
    waiting on child runs), so N concurrent online ingests can never become N
    waiting parents starving their own children out of the ``serve(limit)``
    pool. A single document needs no shard fan-out, which is all the
    coordinator would add. The trailing ``memory-indexing-etl`` run is then
    SUBMITTED fire-and-forget (indexing is a global backfill — never inline,
    mirroring the coordinator's index-once-after-extraction contract).

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
            # Worker processes start cold — no MCP lifespan/CLI bootstrap ran.
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
                summary = await memory_extract_etl_worker(
                    user_id=user_id, document_ids=[str(document.id)]
                )
                logger.info(
                    "Extracted document %s: %d nodes, %d edges written",
                    document.id,
                    summary.nodes_written,
                    summary.edges_written,
                )
                await _submit_indexing(user_id)
            return str(document.id)
    finally:
        # Fail-open telemetry flush — the worker subprocess exits after the run.
        flush_opik()


async def _submit_indexing(user_id: PydanticObjectId) -> None:
    """Fire the trailing ``memory-indexing-etl`` run, fire-and-forget.

    Fail-open: a failed submission is WARNING-logged, never fails the ingest —
    the document and its graph content are already durable, and any later
    indexing run (it is a global backfill over unembedded nodes) covers the gap.
    """

    try:
        flow_run = await run_deployment(
            "memory-indexing-etl/memory-indexing-etl",
            parameters={"user_id": str(user_id)},
            timeout=0,
        )
        logger.info("Submitted indexing as flow run %s", flow_run.id)
    except Exception as exc:  # noqa: BLE001 — indexing gap, not an ingest failure.
        logger.warning(
            "Failed to submit memory-indexing-etl (%s: %s); nodes stay "
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

    Async-first: validates at the edge, then fires the ``online-pipeline``
    deployment fire-and-forget (``timeout=0``) — ONE worker-side flow run does
    the data step AND the extraction, so the caller returns in the time it
    takes to create a flow run. When the deployment isn't registered (e.g.
    the free-tier deployment cap keeps it off prod) or the Prefect API is
    unreachable, the SAME flow runs inline in this process instead — identical
    behavior, synchronous execution. ``mode`` in the result says which happened:

    * ``{"status": "submitted", "flow_run_id": ..., "mode": "deployment"}``
    * ``{"status": "ingested", "document_id": ..., "mode": "in_process"}``
    * ``{"status": "already_ingested", "mode": "in_process"}``

    Raises:
        ValueError: from :func:`validate_online_source` (bad URL / oversized
            payload) — the only synchronous failures; ingest errors in
            deployment mode live in the flow run.
    """

    validate_online_source(source)
    opik_trace_headers = get_distributed_trace_headers()
    try:
        flow_run = await run_deployment(
            "online-pipeline/online-pipeline",
            parameters={
                "source": source.model_dump(mode="json"),
                "user_id": str(user_id),
                "run_extraction": run_extraction,
                "opik_trace_headers": opik_trace_headers,
            },
            timeout=0,
        )
        return {
            "status": "submitted",
            "flow_run_id": str(flow_run.id),
            "mode": "deployment",
        }
    except Exception as exc:  # noqa: BLE001 — absent deployment / unreachable API.
        logger.warning(
            "online-pipeline deployment unavailable (%s: %s); running the flow "
            "in-process instead",
            type(exc).__name__,
            exc,
        )

    # In case the deployment is unavailable, run the flow in-process instead.
    document_id = await online_pipeline(
        source,
        user_id,
        run_extraction=run_extraction,
        opik_trace_headers=opik_trace_headers,
    )
    if document_id is None:
        return {"status": "already_ingested", "mode": "in_process"}
    return {"status": "ingested", "document_id": document_id, "mode": "in_process"}
