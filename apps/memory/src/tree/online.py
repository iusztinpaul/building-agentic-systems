"""
Realtime (online) ingestion orchestration — the one cross-pipeline flow.

Sits at the top level (like :mod:`tree.orchestrator`) because it spans BOTH
pipelines: the data step (``tree.data.online_pipeline.online_ingest``) and the
memory step (``tree.memory.extraction.submit.submit_ingestion``). Neither
pipeline package may import the other; this module imports both.

Async-first: callers (MCP ingest tools + the CLI) funnel through
:func:`dispatch_online_ingest`, which validates at the edge and fires the
``data-etl-online`` deployment fire-and-forget — a Prefect worker runs
:func:`data_etl_online`, which ingests the source into ``documents`` and then
chains the memory-extraction deployment. When no deployment is registered
(e.g. the free-tier deployment cap) the SAME flow runs inline in the caller's
process.
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
from tree.memory.extraction.submit import submit_ingestion
from tree.observability import (
    TAGS_DATA_ONLINE,
    configure_opik,
    flush_opik,
    get_distributed_trace_headers,
    pipeline_metadata,
    span,
)

logger = logging.getLogger(__name__)

_ONLINE_METADATA = pipeline_metadata("data")
_ONLINE_SOURCE_ADAPTER: TypeAdapter[OnlineSource] = TypeAdapter(OnlineSource)

# Prefect caps flow-run parameter payloads (~512KB server-side)
MAX_SOURCE_PAYLOAD_BYTES = 400_000


@flow(name="data-etl-online", log_prints=True)
async def data_etl_online(
    source: Any,
    user_id: PydanticObjectId,
    submit_extraction: bool = True,
    opik_trace_headers: dict[str, str] | None = None,
) -> str | None:
    """The realtime ingest FLOW: data step + chained extraction submission.

    The online counterpart of the offline coordinator/worker pair, collapsed to
    one flow (a single source needs no fan-out). Callers never invoke it
    directly — they go through :func:`dispatch_online_ingest`, which fires it as
    a deployment when one is registered and runs it inline otherwise. Because a
    deployment caller never sees the Document, ``submit_extraction`` (default
    true) chains ``submit_ingestion`` from INSIDE the flow — otherwise
    extraction would silently never happen for deployment-dispatched ingests.

    ``source`` arrives JSON-serialized (a ``dict``) when dispatched via a
    deployment; already-typed :data:`OnlineSource` objects pass through
    unchanged — the same coercion contract as ``offline_pipeline._coerce_sources``.

    Observability: configures Opik at entry (worker-subprocess-safe) and owns one
    span; ``opik_trace_headers`` forwarded by the dispatcher nests it under the
    caller's trace across the process hop (same pattern as ``data_etl_worker``).

    Returns the ingested document id (a caller polling the run to completion can
    recover it from the flow-run result), or ``None`` for a duplicate.
    """

    configure_opik()
    try:
        with span(
            "data-etl-online",
            tags=TAGS_DATA_ONLINE,
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
            if submit_extraction:
                result = await submit_ingestion(document, user_id=user_id)
                logger.info(
                    "Extraction submission for document %s: %s", document.id, result
                )
            return str(document.id)
    finally:
        # Fail-open telemetry flush — the worker subprocess exits after the run.
        flush_opik()


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


async def dispatch_online_ingest(
    source: OnlineSource,
    user_id: PydanticObjectId,
    *,
    submit_extraction: bool = True,
) -> dict[str, Any]:
    """Submit ``source`` to the online pipeline; the ONE entry point for callers.

    Async-first: validates at the edge, then fires the ``data-etl-online``
    deployment fire-and-forget (``timeout=0``) — a Prefect worker runs the data
    step and chains the extraction deployment, so the caller returns in the time
    it takes to create a flow run. When the deployment isn't registered (e.g.
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
            "data-etl-online/data-etl-online",
            parameters={
                "source": source.model_dump(mode="json"),
                "user_id": str(user_id),
                "submit_extraction": submit_extraction,
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
            "data-etl-online deployment unavailable (%s: %s); running the flow "
            "in-process instead",
            type(exc).__name__,
            exc,
        )

    document_id = await data_etl_online(
        source,
        user_id,
        submit_extraction=submit_extraction,
        opik_trace_headers=opik_trace_headers,
    )
    if document_id is None:
        return {"status": "already_ingested", "mode": "in_process"}
    return {"status": "ingested", "document_id": document_id, "mode": "in_process"}
