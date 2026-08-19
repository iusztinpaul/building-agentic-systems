"""
Offline end-to-end orchestration — the batch mirror of :mod:`tree.online`.

Sits at the top level because it spans BOTH pipelines: the data step
(``data_etl_coordinator``) and the memory step
(``memory_extract_etl_coordinator``, which owns the trailing index run).
Neither pipeline package may import the other; this module imports both.

:func:`offline_pipeline` runs the whole chain in ONE flow run: the data coordinator
as an inline subflow (it owns source resolution — same ``source_files`` /
inline ``sources`` / backfill+listen default as always — and the per-platform
worker fan-out), then one extraction coordinator per target user (explicit
``user_id``, or every active user when ``None`` — the nightly-cron semantics).
Worker fan-outs inside each coordinator still run as separate deployment runs;
only the two coordinators execute inline here, so the end-to-end run costs the
same single admission slot a lone coordinator already does.

Either phase can be turned OFF (``run_data`` / ``run_extraction``, mirroring
``tree.online.online_pipeline``'s ``run_extraction`` idiom), and an extraction run
can be narrowed to an explicit ``document_ids`` set. That is what lets the
single-step entry points funnel through this ONE flow instead of forking into
their own chains; with every default left alone the behavior is unchanged.

Callers funnel through :func:`dispatch_offline_pipeline` — the offline twin of
``tree.online.dispatch_online_pipeline``: it fires the ``offline-pipeline``
core deployment fire-and-forget. Dispatch therefore REQUIRES a reachable
Prefect API with that deployment registered; there is no in-process fallback,
so submission failures propagate to the caller.

The standalone ``data-etl-coordinator`` / ``memory-extract-etl-coordinator``
deployments remain the manual single-step entry points; this flow composes
them, it does not replace them.
"""

import logging
from dataclasses import asdict
from typing import Any

from beanie import PydanticObjectId
from prefect import flow, tags
from prefect.deployments import run_deployment

from tree.data.offline_pipeline import data_etl_coordinator, resolve_target_user_ids
from tree.flow_runs import flow_run_status
from tree.memory.extraction.pipeline import memory_extract_etl_coordinator
from tree.config.constants import (
    TAGS_DATA_OFFLINE,
    TAGS_EXTRACTION,
    TAG_DATA_PIPELINE,
    TAG_MEMORY_PIPELINE,
    TAG_OFFLINE,
)
from tree.observability import (
    configure_opik,
    flush_opik,
    span,
)

logger = logging.getLogger(__name__)

# Spans/deployment tags for the end-to-end run: it IS both pipelines, offline.
TAGS_OFFLINE_PIPELINE = [TAG_DATA_PIPELINE, TAG_MEMORY_PIPELINE, TAG_OFFLINE]


def _validate_document_ids_scope(
    document_ids: list[str] | None, user_id: PydanticObjectId | None
) -> None:
    """Reject an explicit doc-id list that has no tenant to belong to.

    Document ids are single-tenant: fanned across ALL active users (the
    ``user_id=None`` nightly-cron semantics) they would either extract another
    tenant's documents or fail deep inside a worker. Checked at BOTH edges —
    the flow and the fire-and-forget dispatcher — because a dispatcher-side run
    surfaces errors only as a remote flow-run failure (same rationale as
    ``tree.online.validate_online_source``).

    Raises:
        ValueError: ``document_ids`` passed without a ``user_id``.
    """

    if document_ids and user_id is None:
        raise ValueError("document_ids is single-tenant — pass user_id too.")


@flow(name="offline-pipeline", log_prints=True)
async def offline_pipeline(
    user_id: PydanticObjectId | None = None,
    source_files: list[str] | None = None,
    sources: list[dict[str, Any]] | None = None,
    num_shards: int = 1,
    run_data: bool = True,
    run_extraction: bool = True,
    document_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Run the offline pipeline: data ingest, then extraction (+ index).

    Phase 1 — data: :func:`data_etl_coordinator` as an inline subflow, passing
    the source selectors through untouched (it owns resolution, so the source
    semantics are identical to a standalone data run). Skipped entirely when
    ``run_data`` is false; the result then carries ``"data": None``.

    Phase 2 — memory: one :func:`memory_extract_etl_coordinator` inline subflow
    per target user (each resolves that user's PENDING documents, fans out
    extraction workers, and fires the trailing index run). Per-user failures
    are isolated: one user's blown extraction is recorded and the others
    proceed — mirroring the shard-failure-isolation convention (#095). Skipped
    entirely when ``run_extraction`` is false (target-user resolution included);
    the result then carries ``"extraction": {}``.

    ``document_ids`` narrows extraction to that exact set for that ONE user
    (forwarded verbatim to every coordinator call) instead of the user's
    resolved PENDING documents; it requires ``user_id`` (see
    :func:`_validate_document_ids_scope`).

    Both phases disabled is a LOGGED no-op, not an error: the run completes and
    returns the empty result, so a misconfigured caller gets a Completed flow
    run it can read rather than a crash.

    Observability: owns one span the two coordinators' spans nest under
    (same-process contextvars), so the end-to-end run renders as ONE trace.

    Returns ``{"data": <DataFanOutStats | None>, "extraction": {user_id:
    <FanOutStats | {"error": ...}>}}`` as plain dicts (JSON-safe for the
    flow-run result).

    Raises:
        ValueError: ``document_ids`` passed without a ``user_id``.
    """

    _validate_document_ids_scope(document_ids, user_id)
    if not run_data and not run_extraction:
        logger.info(
            "offline-pipeline: both phases disabled (run_data=False, "
            "run_extraction=False) — nothing to do"
        )
        return {"data": None, "extraction": {}}

    configure_opik()
    try:
        with span("offline-pipeline", tags=TAGS_OFFLINE_PIPELINE):
            # Inline subflows don't inherit this run's deployment tags, so each
            # coordinator's own flow run is tagged at the call site.
            with tags(*TAGS_DATA_OFFLINE):
                data_stats = (
                    await data_etl_coordinator(
                        user_id=user_id, source_files=source_files, sources=sources
                    )
                    if run_data
                    else None
                )

            extraction: dict[str, Any] = {}
            target_user_ids = (
                await resolve_target_user_ids(user_id) if run_extraction else []
            )
            for uid in target_user_ids:
                try:
                    with tags(*TAGS_EXTRACTION):
                        stats = await memory_extract_etl_coordinator(
                            uid, document_ids=document_ids, num_shards=num_shards
                        )
                    extraction[str(uid)] = asdict(stats)
                except Exception as exc:  # noqa: BLE001 — isolate per-user failures.
                    logger.exception(
                        "offline-pipeline: extraction failed for user %s", uid
                    )
                    extraction[str(uid)] = {"error": str(exc)}

            return {
                "data": asdict(data_stats) if data_stats is not None else None,
                "extraction": extraction,
            }
    finally:
        # Fail-open telemetry flush — the worker subprocess exits after the run.
        flush_opik()


async def dispatch_offline_pipeline(
    user_id: PydanticObjectId | None = None,
    source_files: list[str] | None = None,
    sources: list[dict[str, Any]] | None = None,
    num_shards: int = 1,
    run_data: bool = True,
    run_extraction: bool = True,
    document_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Submit the offline run; the ONE entry point for callers.

    The offline twin of ``tree.online.dispatch_online_pipeline``: fires the
    ``offline-pipeline`` core deployment fire-and-forget (``timeout=0``) — a
    Prefect worker runs the whole data → extraction → index chain — and returns
    at once with::

        {"status": <flow-run state, lowercased>, "flow_run_id": ...}

    ``status`` is Prefect's own state name for the freshly created run
    (:func:`tree.flow_runs.flow_run_status` — ``scheduled`` normally), NOT a
    completion status: the work happens on the worker afterwards.

    There is exactly ONE path: dispatch requires a reachable Prefect API with
    the deployment registered. Submission failures (unreachable API, missing
    deployment, parameter validation, auth) PROPAGATE — a caller must see them
    rather than have them silently swapped for a long blocking in-process run.

    The phase flags and ``document_ids`` are forwarded unchanged to the
    deployment — see :func:`offline_pipeline` for their semantics.

    Callers that want to BLOCK on the run (the CLI) poll the returned
    ``flow_run_id`` themselves — waiting is a caller concern, not the
    dispatcher's.

    Raises:
        ValueError: ``document_ids`` passed without a ``user_id`` — validated at
            the edge, BEFORE any flow run is created, since dispatch is
            fire-and-forget and this is the only synchronous failure a caller
            would otherwise never see.
    """

    _validate_document_ids_scope(document_ids, user_id)

    parameters: dict[str, Any] = {
        "user_id": str(user_id) if user_id is not None else None,
        "source_files": source_files,
        "sources": sources,
        "num_shards": num_shards,
        "run_data": run_data,
        "run_extraction": run_extraction,
        "document_ids": document_ids,
    }
    flow_run = await run_deployment(
        "offline-pipeline/offline-pipeline", parameters=parameters, timeout=0
    )
    return {"status": flow_run_status(flow_run), "flow_run_id": str(flow_run.id)}
