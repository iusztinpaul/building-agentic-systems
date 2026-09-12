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
extraction run can be narrowed to an explicit ``document_ids`` set or to the
``source_uris`` an **Ingest receipt** carries (resolved to ids at flow entry —
the retry path for a failed online extraction, ADR-008 §1). That is what
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
from prefect import flow, get_run_logger, tags
from prefect.deployments import run_deployment

from tree.config.settings import settings
from tree.data.offline_pipeline import data_etl_coordinator, resolve_target_user_ids
from tree.db import init_mongodb
from tree.entities.documents import Document
from tree.flow_runs import flow_run_status
from tree.memory.pipeline import (
    ClusteringStats,
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


def _get_run_logger() -> logging.Logger:
    """The PARENT run's logger inside a flow run; the module logger otherwise.

    Load-bearing for the phase lines below: ``tree.cli.wait_for_flow_run``
    streams the logs Prefect stores for THIS flow run, and only the run logger
    writes there — a plain module logger reaches the worker's stdout alone, and
    a subflow's own logger reaches only that subflow's run. Outside a flow run
    (unit tests calling the body directly) it degrades to the module logger so
    the lines stay visible to ``caplog``. Same helper as
    ``tree.memory.pipeline`` / ``tree.memory.graph.sharding``; each binds its
    OWN module logger as the fallback, which is why it is not shared.
    """

    try:
        return get_run_logger()  # type: ignore[return-value]
    except Exception:  # noqa: BLE001 — Prefect raises a typed context error
        return logger


def _validate_single_tenant_scope(
    document_ids: list[str] | None,
    source_uris: list[str] | None,
    user_id: PydanticObjectId | None,
) -> None:
    """Reject a document selector that has no tenant to belong to.

    BOTH selectors are single-tenant: fanned across ALL active users (the
    ``user_id=None`` nightly-cron semantics) an id set would extract another
    tenant's documents or fail deep inside a worker, and a URI set would resolve
    against another tenant's ``documents`` rows (``(user_id, source_type,
    source_uri)`` is unique per TENANT, so the same URI legitimately exists for
    several users). Checked at BOTH edges — the flow and the fire-and-forget
    dispatcher — because a dispatcher-side run surfaces errors only as a remote
    flow-run failure (same rationale as ``tree.online.validate_online_source``).

    The message names the offending selector, since an operator passing
    ``SOURCE_URIS=`` must not be told to fix ``DOC_IDS=``.

    Raises:
        ValueError: ``document_ids`` or ``source_uris`` passed without a
            ``user_id``.
    """

    if user_id is not None:
        return
    if document_ids:
        raise ValueError("document_ids is single-tenant — pass user_id too.")
    if source_uris:
        raise ValueError("source_uris is single-tenant — pass user_id too.")


async def _resolve_source_uris(
    user_id: PydanticObjectId, source_uris: list[str]
) -> list[str]:
    """Resolve **Ingest receipt** URIs to this tenant's document ids.

    ADR-008 Decision 1: a receipt carries ``source_uri`` — the **Document**'s
    natural key — not its random ``_id``, so ``SOURCE_URIS=`` is what an operator
    retries a failed extraction with. ONE indexed
    ``{"user_id", "source_uri": {"$in": …}}`` read over the
    ``user_source_uri_unique`` index covers the whole set; the connection is
    opened here because this is the flow's only own I/O (same "connect only when
    needed" shape as :func:`resolve_target_user_ids`).

    A retry selector over ALREADY-INGESTED documents, NOT an ingest selector: it
    runs before the data phase, so a URI this run would ingest does not resolve.
    Use ``make memory-run-pipeline MODE=online SOURCE=<uri>`` to ingest.

    Order follows the URIs the caller passed, not the cursor (Mongo guarantees
    none), and a URI matching several rows (the same URI under two
    ``source_type``s) yields all of them.

    Raises:
        ValueError: a URI with no document for this user — a typo must fail loud,
            because a silently empty doc set makes a broken retry read green.
    """

    await init_mongodb(
        settings.mongo.mongo_uri.get_secret_value(),
        settings.mongo.mongo_initdb_database,
    )
    documents = await Document.find(
        {"user_id": user_id, "source_uri": {"$in": source_uris}}
    ).to_list()
    ids_by_uri: dict[str, list[str]] = {}
    for document in documents:
        ids_by_uri.setdefault(document.source_uri, []).append(str(document.id))
    resolved: list[str] = []
    for uri in source_uris:
        if uri not in ids_by_uri:
            raise ValueError(f"No document for source_uri {uri} (user {user_id})")
        resolved.extend(ids_by_uri[uri])
    return resolved


def _log_clustering_outcome(
    log: logging.Logger, user_id: PydanticObjectId, stats: ClusteringStats
) -> None:
    """Report ONE clustering phase at the parent level: summary, or the skip.

    A corpus below ``min_cluster_size`` is a NON-failure exit — the run
    completes having written nothing — so without this line the terminal reads
    identically whether 37 clusters were written or the corpus was skipped
    (#117 Story 3). WARNING for the skip, because it names an action.
    """

    if stats.skipped_reason:
        log.warning(
            "clustering SKIPPED: user_id=%s reason=%s", user_id, stats.skipped_reason
        )
        return
    log.info(
        "clustering: user_id=%s run_id=%s clusters=%d clustered=%d noise=%d "
        "fallbacks=%d",
        user_id,
        stats.run_id,
        stats.clusters,
        stats.clustered,
        stats.noise,
        stats.summaries_failed,
    )


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
    source_uris: list[str] | None = None,
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
    resolved PENDING documents. ``source_uris`` narrows it the same way but by
    **Ingest receipt** key: the URIs are resolved to this user's document ids at
    flow entry (:func:`_resolve_source_uris`) and UNIONED with ``document_ids``
    (order preserved, no duplicates), so ``DOC_IDS=`` and ``SOURCE_URIS=`` can be
    mixed in one run. Both selectors require ``user_id`` (see
    :func:`_validate_single_tenant_scope`).

    All phases disabled is a LOGGED no-op, not an error: the run completes and
    returns the empty result, so a misconfigured caller gets a Completed flow
    run it can read rather than a crash.

    Every per-user phase logs ONE line at THIS flow's level once it returns
    (``extraction: user_id=… shards=…`` / ``indexing: user_id=… embedded=N`` /
    ``clustering: user_id=… clusters=k …``, or ``clustering SKIPPED:
    user_id=… reason=…``). ``tree.cli.wait_for_flow_run`` streams the PARENT
    run's logs only, so a subflow's own summary never reaches the terminal the
    operator dispatched from — these lines are what tells them a skipped
    clustering run from a 37-cluster one.

    Observability: owns one span the phases' spans nest under (same-process
    contextvars), so the end-to-end run renders as ONE trace.

    Returns ``{"data": <DataFanOutStats | None>, "extraction": {user_id:
    <FanOutStats | {"error": ...}>}, "indexing": {user_id: {"embedded": <int>} |
    {"error": ...}}, "clustering": {user_id: <ClusteringStats | {"error":
    ...}>}}`` as plain dicts (JSON-safe for the flow-run result).

    Raises:
        ValueError: ``document_ids`` or ``source_uris`` passed without a
            ``user_id``, or a ``source_uris`` entry with no document for this
            user (raised BEFORE any phase runs).
    """

    _validate_single_tenant_scope(document_ids, source_uris, user_id)
    log = _get_run_logger()
    if not run_data and not run_extraction and not run_indexing and not run_clustering:
        log.info(
            "offline-pipeline: all phases disabled (run_data=False, "
            "run_extraction=False, run_indexing=False, run_clustering=False) "
            "— nothing to do"
        )
        return {"data": None, "extraction": {}, "indexing": {}, "clustering": {}}

    # Resolve the receipt keys BEFORE any phase: an unknown URI is an operator
    # typo, and failing at entry costs nothing, while failing later leaves a
    # half-run to reason about. ``user_id`` is non-None here (guarded above).
    extraction_document_ids = document_ids
    if source_uris:
        resolved = await _resolve_source_uris(user_id, source_uris)
        extraction_document_ids = list(
            dict.fromkeys([*(document_ids or []), *resolved])
        )
        log.info(
            "offline-pipeline: resolved %d source_uris to %d document_ids",
            len(source_uris),
            len(resolved),
        )

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
                                uid,
                                document_ids=extraction_document_ids,
                                num_shards=num_shards,
                            )
                        extraction[str(uid)] = asdict(stats)
                        log.info(
                            "extraction: user_id=%s shards=%d succeeded=%d failed=%d",
                            uid,
                            stats.shards_total,
                            stats.succeeded,
                            stats.failed,
                        )
                    except Exception as exc:  # noqa: BLE001 — isolate per user.
                        log.exception(
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
                        log.info("indexing: user_id=%s embedded=%d", uid, embedded)
                    except Exception as exc:  # noqa: BLE001 — isolate per user.
                        log.exception(
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
                        _log_clustering_outcome(log, uid, stats)
                    except Exception as exc:  # noqa: BLE001 — isolate per user.
                        log.exception(
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
    source_uris: list[str] | None = None,
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

    The phase flags, ``document_ids`` and ``source_uris`` are forwarded unchanged
    to the deployment — see :func:`offline_pipeline` for their semantics. URI
    resolution happens THERE, not here: the flow is the contract, and the
    dispatcher does no I/O.

    Callers that want to BLOCK on the run (the CLI) poll the returned
    ``flow_run_id`` themselves — waiting is a caller concern, not the
    dispatcher's.

    Raises:
        ValueError: ``document_ids`` or ``source_uris`` passed without a
            ``user_id`` — validated at the edge, BEFORE any flow run is created,
            since dispatch is fire-and-forget and this is the only synchronous
            failure a caller would otherwise never see. An unresolvable URI is
            NOT checked here (it needs a DB read); it fails the flow run.
    """

    _validate_single_tenant_scope(document_ids, source_uris, user_id)

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
        "source_uris": source_uris,
    }
    flow_run = await run_deployment(
        "offline-pipeline/offline-pipeline", parameters=parameters, timeout=0
    )
    return {"status": flow_run_status(flow_run), "flow_run_id": str(flow_run.id)}
