"""
Offline end-to-end orchestration — the batch mirror of :mod:`tree.online`.

The MOTHER pipeline: it spans BOTH pipelines — the data step
(``data_etl_coordinator``) and the memory steps (``memory_extract_etl_coordinator``
and ``memory_indexing``). Neither pipeline package may import the other; this
module imports both.

:func:`offline_pipeline` runs the whole chain in ONE flow run as FOUR explicit,
independently switchable **Offline phase**s (ADR-007 Decision 5), executed as
SEQUENTIAL BLOCKS:

1. ``run_data`` — the data coordinator as an inline subflow (it owns source
   resolution — same ``source_files`` / inline ``sources`` / backfill+listen
   default as always — and the per-platform worker fan-out).
2. ``run_extraction`` — one extraction coordinator per target user (explicit
   ``user_id``, or every active user when ``None`` — the nightly-cron semantics).
3. ``run_indexing`` — one ``memory_indexing`` subflow per target user, AFTER
   every user's extraction. Indexing is a PHASE here, not something the
   extraction Coordinator does on the side (this superseded ADR-002 §3's
   trailing-index rule).
4. ``run_clustering`` — one ``memory_clustering`` subflow per target user: the
   **Embedding map**'s data (UMAP + HDBSCAN + one LLM summary per **Memory
   cluster**). OFF by default — it is a maintenance phase whose cold ``import
   umap`` costs ~40 s on a fresh container, so the nightly cron never pays for
   it and nobody who does not ask for it imports the stack.

Worker fan-outs inside the coordinators still run as separate deployment runs;
only the coordinators and the indexing flow execute inline here, so the
end-to-end run costs the same single admission slot a lone coordinator already
does.

Any phase can be turned OFF (``run_data`` / ``run_extraction`` / ``run_indexing``
/ ``run_clustering``, mirroring ``tree.online.online_pipeline``'s ``run_extraction`` idiom), and an
extraction run can be narrowed to an explicit ``document_ids`` set. That is what
lets the single-step entry points funnel through this ONE flow instead of forking
into their own chains; with every default left alone the behavior is unchanged.

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
from tree.memory.pipeline import (
    memory_clustering,
    memory_extract_etl_coordinator,
    memory_indexing,
)
from tree.config.constants import (
    TAGS_CLUSTERING,
    TAGS_DATA_OFFLINE,
    TAGS_EXTRACTION,
    TAGS_INDEXING,
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
    run_indexing: bool = True,
    run_clustering: bool = False,
    document_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Run the offline pipeline: data, extraction, indexing, then clustering.

    Phase 1 — data: :func:`data_etl_coordinator` as an inline subflow, passing
    the source selectors through untouched (it owns resolution, so the source
    semantics are identical to a standalone data run). Skipped entirely when
    ``run_data`` is false; the result then carries ``"data": None``.

    Phase 2 — extraction: one :func:`memory_extract_etl_coordinator` inline
    subflow per target user (each resolves that user's PENDING documents and
    fans out extraction workers). Per-user failures are isolated: one user's
    blown extraction is recorded and the others proceed — mirroring the
    shard-failure-isolation convention (#095). Skipped entirely when
    ``run_extraction`` is false; the result then carries ``"extraction": {}``.

    Phase 3 — indexing: one :func:`memory_indexing` inline subflow per target
    user (embedding backfill + search-index reconcile), with the SAME per-user
    failure isolation. Skipped entirely when ``run_indexing`` is false; the
    result then carries ``"indexing": {}``.

    Phase 4 — clustering: one :func:`memory_clustering` inline subflow per
    target user (the **Embedding map**'s data), with the SAME per-user failure
    isolation. OFF by default, so the nightly cron and every other script leave
    ``memory_clusters`` alone and never import the UMAP stack; the result then
    carries ``"clustering": {}``.

    The phases run as sequential BLOCKS — every user is extracted, THEN every
    user is indexed — so the Prefect UI shows extraction and indexing as sibling
    subflows instead of indexing hiding inside the Coordinator. Target users are
    resolved ONCE for all three per-user phases (skipped when none runs, so a
    data-only run never touches the users collection).

    ``document_ids`` narrows extraction to that exact set for that ONE user
    (forwarded verbatim to every coordinator call) instead of the user's
    resolved PENDING documents; it requires ``user_id`` (see
    :func:`_validate_document_ids_scope`).

    All phases disabled is a LOGGED no-op, not an error: the run completes and
    returns the empty result, so a misconfigured caller gets a Completed flow
    run it can read rather than a crash.

    Observability: owns one span the phases' spans nest under (same-process
    contextvars), so the end-to-end run renders as ONE trace.

    Returns ``{"data": <DataFanOutStats | None>, "extraction": {user_id:
    <FanOutStats | {"error": ...}>}, "indexing": {user_id: {"embedded": <int>} |
    {"error": ...}}, "clustering": {user_id: <ClusteringStats | {"error":
    ...}>}}`` as plain dicts (JSON-safe for the flow-run result).

    Raises:
        ValueError: ``document_ids`` passed without a ``user_id``.
    """

    _validate_document_ids_scope(document_ids, user_id)
    if not run_data and not run_extraction and not run_indexing and not run_clustering:
        logger.info(
            "offline-pipeline: all phases disabled (run_data=False, "
            "run_extraction=False, run_indexing=False, run_clustering=False) "
            "— nothing to do"
        )
        return {"data": None, "extraction": {}, "indexing": {}, "clustering": {}}

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

            # Both per-user phases fan across the SAME target users, so resolve
            # them once — and only when a per-user phase actually runs.
            target_user_ids = (
                await resolve_target_user_ids(user_id)
                if (run_extraction or run_indexing or run_clustering)
                else []
            )

            extraction: dict[str, Any] = {}
            if run_extraction:
                for uid in target_user_ids:
                    try:
                        with tags(*TAGS_EXTRACTION):
                            stats = await memory_extract_etl_coordinator(
                                uid, document_ids=document_ids, num_shards=num_shards
                            )
                        extraction[str(uid)] = asdict(stats)
                    except Exception as exc:  # noqa: BLE001 — isolate per user.
                        logger.exception(
                            "offline-pipeline: extraction failed for user %s", uid
                        )
                        extraction[str(uid)] = {"error": str(exc)}

            # Phase 3 runs as its own block AFTER every user's extraction:
            # indexing is a global backfill over the user's unembedded rows, so
            # it is one run per user, never one per shard.
            indexing: dict[str, Any] = {}
            if run_indexing:
                for uid in target_user_ids:
                    try:
                        with tags(*TAGS_INDEXING):
                            embedded = await memory_indexing(user_id=uid)
                        indexing[str(uid)] = {"embedded": embedded}
                    except Exception as exc:  # noqa: BLE001 — isolate per user.
                        logger.exception(
                            "offline-pipeline: indexing failed for user %s", uid
                        )
                        indexing[str(uid)] = {"error": str(exc)}

            # Phase 4 runs LAST and only on request: clustering reads the
            # embeddings phase 3 just backfilled, so a run that does both gets a
            # map of the whole corpus rather than of yesterday's part of it.
            clustering: dict[str, Any] = {}
            if run_clustering:
                for uid in target_user_ids:
                    try:
                        with tags(*TAGS_CLUSTERING):
                            stats = await memory_clustering(user_id=uid)
                        clustering[str(uid)] = stats.model_dump()
                    except Exception as exc:  # noqa: BLE001 — isolate per user.
                        logger.exception(
                            "offline-pipeline: clustering failed for user %s", uid
                        )
                        clustering[str(uid)] = {"error": str(exc)}

            return {
                "data": asdict(data_stats) if data_stats is not None else None,
                "extraction": extraction,
                "indexing": indexing,
                "clustering": clustering,
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
    run_indexing: bool = True,
    run_clustering: bool = False,
    document_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Submit the offline run; the ONE entry point for callers.

    The offline twin of ``tree.online.dispatch_online_pipeline``: fires the
    ``offline-pipeline`` core deployment fire-and-forget (``timeout=0``) — a
    Prefect worker runs the whole data → extraction → index (→ cluster) chain —
    and returns
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
        "run_indexing": run_indexing,
        "run_clustering": run_clustering,
        "document_ids": document_ids,
    }
    flow_run = await run_deployment(
        "offline-pipeline/offline-pipeline", parameters=parameters, timeout=0
    )
    return {"status": flow_run_status(flow_run), "flow_run_id": str(flow_run.id)}
