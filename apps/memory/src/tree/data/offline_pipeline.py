"""Data pipeline: orchestrator + worker deployments (#068, ADR-002 §3 amended #066).

Two Prefect flows live here, mirroring the memory split (#067) minus the trailing
index — the data pipeline only produces ``documents``; there is NO index step:

* ``data_etl_worker`` (deployment ``data-etl-worker``) — ingests a SUBSET (shard) of
  the configured ``sources:`` list. It groups the shard's sources by PLATFORM and
  dispatches each group to one unified per-platform pipeline:

  - Substack (``SubstackRssSource`` + ``SubstackArticleSource``) → one
    ``ingest_substack_batch``: flatten feeds + single articles into one item list,
    then one shared load.
  - YouTube (``YouTubeRssSource`` + ``YouTubeVideoSource``) → one
    ``ingest_youtube_batch``: flatten feeds + single videos, then ONE shared
    ``fetch_many`` transcript fetch.
  - Web (``WebSource``) → ``ingest_web_batch`` (adapter over ``ingest_web_url_batch``),
    the last/catch-all platform.
  - ``HuggingFaceDatasetSource`` entries are dispatched per-entry through
    ``_HUGGINGFACE_DATASET_HANDLERS``, keyed on the dataset id (``uri``). Unknown
    dataset ids raise ``ValueError``.

  A platform absent from the shard is skipped (with a scoped "skipped: no entries"
  log line). NO partitioning, NO ``run_deployment``, NO orchestration — the worker is
  the orchestrator's internal dispatch target (but may be triggered directly for a
  bare shard ingestion). Registered as deployment ``data-etl-worker``.

* ``data_etl_orchestrator`` (deployment ``data-etl-orchestrator``) — resolves its
  source set (``source_files`` ++ inline ``sources``, else the backfill+listen
  default) and partitions it by PLATFORM (#072, ADR-002 §3
  amendment #070–#074): one homogeneous ``data-etl-worker`` run per non-HuggingFace
  platform bucket present (``substack`` / ``youtube`` / ``custom``), plus
  ``num_workers`` runs per ``HuggingFaceDatasetSource`` (one per disjoint offset-window
  of the dataset). It dispatches one ``data-etl-worker`` run per shard via
  ``run_deployment`` under ``asyncio.gather(return_exceptions=True)``. NO trailing step.
  Parallelism is declared per-source (platform bucketing + HF ``num_workers``), NOT via
  a global ``num_shards``. Empty sources ⇒ clean no-op (``shards_total=0``). Registered
  as deployment ``data-etl-orchestrator``.

Source-shard serialization: ``SourceEntry`` is a Pydantic discriminated union.
Prefect serializes flow-run parameters as JSON, so the orchestrator dumps each shard's
entries to dicts (``model_dump()``) and the worker re-parses them to ``SourceEntry``
via a ``TypeAdapter`` (the ``type`` discriminator round-trips through JSON cleanly).

Usage:
    Served as Prefect deployments via the orchestrator. Operators trigger the
    ORCHESTRATOR via the unified ``run-data-pipeline`` Make target.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from beanie import PydanticObjectId
from prefect import flow, get_run_logger, tags
from prefect.deployments import run_deployment
from pydantic import TypeAdapter

from tree.config.app_config import (
    HuggingFaceDatasetSource,
    SourceEntry,
    SubstackArticleSource,
    SubstackRssSource,
    WebSource,
    YouTubeRssSource,
    YouTubeVideoSource,
)
from tree.config.settings import settings
from tree.config.sources import default_configured_sources, load_sources
from tree.data.huggingface.arxiv_dataset_pipeline import (
    arxiv_window_entries,
    ingest_arxiv_dataset,
)

# The per-platform unified batch flows, referenced directly in ``_PLATFORM_PIPELINES``.
from tree.data.substack.substack_pipeline_batch import ingest_substack_batch
from tree.data.web.web_pipeline import ingest_web_batch
from tree.data.youtube.youtube_pipeline_batch import ingest_youtube_batch
from tree.db import init_mongodb
from tree.entities.documents import Document
from tree.entities.users import select_active_user_ids
from tree.memory.indexing.core import assert_settings_match_live_vector_index
from tree.observability import (
    TAGS_DATA_OFFLINE,
    configure_opik,
    flush_opik,
    get_distributed_trace_headers,
    pipeline_metadata,
    span,
    tracked_span,
)

# Pipeline-identity tags, shared 1:1 with this pipeline's Prefect deployment /
# flow-run tags (``prefect.tags(*_DATA_TAGS)`` below).
_DATA_TAGS = TAGS_DATA_OFFLINE
_DATA_METADATA = pipeline_metadata("data")

logger = logging.getLogger(__name__)

# Round-trips a serialized shard (``list[dict]``) back to the typed discriminated
# union. The ``type`` discriminator survives the JSON round-trip Prefect performs on
# flow-run parameters, so the worker reconstructs the exact ``SourceEntry`` objects
# the orchestrator partitioned.
_SOURCES_ADAPTER: TypeAdapter[list[SourceEntry]] = TypeAdapter(list[SourceEntry])


async def _ingest_arxiv_dataset_entry(
    entry: HuggingFaceDatasetSource,
    user_id: PydanticObjectId,
) -> list[Document]:
    return await ingest_arxiv_dataset(
        user_id=user_id,
        max_samples=entry.max_samples,
        fetch_content=entry.fetch_content,
        offset=entry.offset,
    )


# Registry: HuggingFace dataset id → ETL handler.
_HUGGINGFACE_DATASET_HANDLERS: dict[
    str,
    Callable[[HuggingFaceDatasetSource, PydanticObjectId], Awaitable[list[Document]]],
] = {
    "librarian-bots/arxiv-metadata-snapshot": _ingest_arxiv_dataset_entry,
}


_BatchFn = Callable[[list[SourceEntry], PydanticObjectId], Awaitable[list[Document]]]


@dataclass(frozen=True)
class PlatformPipeline:
    """One platform's unified pipeline, fed the shard's entries for that platform.

    ``source_types`` selects the entries via ``isinstance`` (a tuple, since a platform
    spans both its RSS and single-source kinds — e.g. Substack RSS + article).
    ``batch_fn`` is the unified per-platform flow; ``label`` names it in log lines.
    """

    source_types: tuple[type[SourceEntry], ...]
    batch_fn: _BatchFn
    label: str


# One unified pipeline per platform. Order is load-bearing — it fixes the ingestion
# order Substack → YouTube → Web (last/catch-all) and the order their log lines are
# emitted. Each pipeline flattens its platform's single + RSS sources into one batch.
_PLATFORM_PIPELINES: list[PlatformPipeline] = [
    PlatformPipeline(
        (SubstackRssSource, SubstackArticleSource),
        ingest_substack_batch,
        "Substack",
    ),
    PlatformPipeline(
        (YouTubeRssSource, YouTubeVideoSource),
        ingest_youtube_batch,
        "YouTube",
    ),
    PlatformPipeline(
        (WebSource,),
        ingest_web_batch,
        "Web",
    ),
]


def _coerce_sources(sources: list[Any]) -> list[SourceEntry]:
    """Coerce a worker ``sources`` argument to typed ``SourceEntry`` objects.

    The worker is dispatched by the orchestrator with the shard serialized as a
    ``list[dict]`` (Prefect JSON-serializes flow-run parameters). Already-typed
    ``SourceEntry`` instances (e.g. a direct in-process call in a test) pass through
    unchanged; dicts are re-parsed via the discriminated-union ``TypeAdapter``.
    """

    if all(not isinstance(s, dict) for s in sources):
        return list(sources)
    return _SOURCES_ADAPTER.validate_python(sources)


@tracked_span("offline_ingest_batch", tags=_DATA_TAGS)
async def offline_ingest_batch(
    sources: list[SourceEntry],
    user_id: PydanticObjectId,
    opik_trace_headers: dict[str, str] | None = None,
) -> list[Document]:
    """Ingest a list of typed source entries by grouping them by platform.

    Hands each platform's entries (its RSS + single sources together) to one unified
    per-platform pipeline, scoped to the entries handed in (a shard, or the full
    configured list). A platform absent from ``sources`` is skipped with a scoped
    "skipped: no entries" log line.

    ``opik_trace_headers`` attaches this task's span to the worker flow's trace.
    """

    all_ingested: list[Document] = []

    # --- Per-platform unified pipelines (Substack → YouTube → Web) ---
    # Each platform flattens its single + RSS sources into a single batch; order is
    # load-bearing (Web last/catch-all).
    for platform in _PLATFORM_PIPELINES:
        entries = [s for s in sources if isinstance(s, platform.source_types)]
        if entries:
            logger.info(
                "Starting %s pipeline with %d source(s)", platform.label, len(entries)
            )
            # Tag the leaf flow run — the in-process sub-flows don't inherit the
            # worker deployment's tags, so we apply them at the call site.
            with tags(*_DATA_TAGS):
                docs = await platform.batch_fn(entries, user_id)
            all_ingested.extend(docs)
            logger.info("%s pipeline ingested %d documents", platform.label, len(docs))
        else:
            logger.info("%s pipeline skipped: no entries configured", platform.label)

    # --- HuggingFace datasets (one call per entry, dispatched by dataset id) ---
    hf_entries = [s for s in sources if isinstance(s, HuggingFaceDatasetSource)]
    if hf_entries:
        for entry in hf_entries:
            handler = _HUGGINGFACE_DATASET_HANDLERS.get(entry.uri)
            if handler is None:
                raise ValueError(
                    f"No ETL registered for HuggingFace dataset id {entry.uri!r}. "
                    f"Register a handler in {__name__}._HUGGINGFACE_DATASET_HANDLERS."
                )
            logger.info("Starting HuggingFace dataset pipeline for %s", entry.uri)
            with tags(*_DATA_TAGS):
                hf_docs = await handler(entry, user_id)
            all_ingested.extend(hf_docs)
            logger.info(
                "HuggingFace dataset pipeline for %s ingested %d documents",
                entry.uri,
                len(hf_docs),
            )
    else:
        logger.info(
            "HuggingFace dataset pipeline skipped: no huggingface_dataset entries configured"
        )

    logger.info("Source ingestion complete. Total ingested: %d", len(all_ingested))

    return all_ingested


# ---------------------------------------------------------------------------
# Worker flow — data-etl-worker (#068)
# ---------------------------------------------------------------------------


@flow(name="data-etl-worker", log_prints=True)
async def data_etl_worker(
    user_id: PydanticObjectId,
    sources: list[Any],
    opik_trace_headers: dict[str, str] | None = None,
) -> list[Document]:
    """Ingest a SUBSET (shard) of the configured sources under ``user_id``.

    Reuses the existing per-source-type batch logic: groups ``sources`` by
    discriminated-union variant and runs the existing batch sub-flow for each variant
    present in the shard (Substack RSS/article, YouTube RSS/video, HuggingFace dataset
    with unknown-id ``ValueError``, web via ``ingest_web_url_batch`` (last)). A variant
    absent from the shard is skipped. This is PURE ingestion: NO partitioning, NO
    ``run_deployment``,
    NO orchestration, NO trailing index.

    ``sources`` arrives serialized (``list[dict]``) when dispatched by the
    orchestrator (Prefect JSON-serializes flow-run parameters); already-typed
    ``SourceEntry`` objects pass through unchanged. The shard is re-parsed to the
    typed discriminated union before grouping.

    Observability: configures Opik at entry (subprocess-safe) and owns ONE trace.
    ``opik_trace_headers`` is forwarded by the data orchestrator so the worker
    nests under the orchestrator's trace; ``None`` (standalone trigger) starts a
    fresh trace.
    """

    configure_opik()
    try:
        with span(
            "data-etl-worker",
            tags=_DATA_TAGS,
            trace_headers=opik_trace_headers,
            metadata=_DATA_METADATA,
        ):
            client = await init_mongodb(
                settings.mongo.mongo_uri.get_secret_value(),
                settings.mongo.mongo_initdb_database,
            )

            # Boot-time gate: refuse to run if
            # ``app_config.models.search_embedding.dimensions`` disagrees with
            # the live Atlas Vector Search index.
            try:
                await assert_settings_match_live_vector_index(
                    client, settings.mongo.mongo_initdb_database
                )
            except RuntimeError as exc:
                if "vector_index not found" in str(exc):
                    logger.info(
                        "vector_index not yet provisioned; skipping dim-check at "
                        "data_etl_worker boot. The indexing pipeline will "
                        "bootstrap it."
                    )
                else:
                    raise

            headers = get_distributed_trace_headers()
            typed_sources = _coerce_sources(sources)
            return await offline_ingest_batch(
                typed_sources, user_id, opik_trace_headers=headers
            )
    finally:
        # Flush batched Opik telemetry (fail-open; no-op without OPIK_API_KEY).
        flush_opik()


# ---------------------------------------------------------------------------
# Source-shard fan-out (orchestrator path)
# ---------------------------------------------------------------------------


# Platform buckets for the data orchestrator's GROUP-BY-PLATFORM partition
_NON_HF_PLATFORMS: list[tuple[str, tuple[type[SourceEntry], ...]]] = [
    ("substack", (SubstackRssSource, SubstackArticleSource)),
    ("youtube", (YouTubeRssSource, YouTubeVideoSource)),
    ("custom", (WebSource,)),
]


def _partition_sources_by_platform(
    sources: list[SourceEntry],
) -> list[list[SourceEntry]]:
    """Partition the configured sources into the shards the orchestrator dispatches.

    The data orchestrator's shard map (#072, ADR-002 §3 amendment #070–#074), replacing
    the old count-based ``_partition_into_shards``. Pure decision logic (no DB, no
    Prefect, order-stable) so it is unit-testable directly. Returns the FULL list of
    shards — each a ``list[SourceEntry]`` — that the orchestrator ``model_dump()``s and
    hands to the unchanged :func:`_fan_out_data` (one ``data-etl-worker`` run per shard):

    * **Non-HuggingFace → one HOMOGENEOUS shard per Platform bucket present.** Variants
      sharing a Platform are grouped (``substack_rss`` + ``substack_article`` → one
      ``substack`` shard; ``youtube_rss`` + ``youtube_video`` → one ``youtube`` shard;
      ``web`` → one ``custom`` shard), preserving each Platform's internal configured
      order. The worker's existing ``offline_ingest_batch`` ``isinstance`` routing then
      batches per VARIANT inside the homogeneous shard, so the worker is unchanged. A
      Platform absent from config emits no shard.
    * **HuggingFace → ``num_workers`` single-entry offset-Window shards per entry.** Each
      ``HuggingFaceDatasetSource`` is expanded via :func:`arxiv_window_entries` into its
      disjoint windows (the configured entry is never mutated); each window is its OWN
      single-entry shard ``[windowed_entry]``. Multiple HF entries fan out independently.
      ``max_samples == 0`` emits no shard for that entry.

    Shard order: the non-HF Platform buckets first (in ``_NON_HF_PLATFORMS`` order), then
    the HF window shards in configured-entry order. Empty sources ⇒ ``[]`` (a no-op).
    """

    shards: list[list[SourceEntry]] = []

    # --- Non-HuggingFace: one homogeneous shard per Platform bucket present ---
    for _platform, variant_types in _NON_HF_PLATFORMS:
        bucket = [s for s in sources if isinstance(s, variant_types)]
        if bucket:
            shards.append(bucket)

    # --- HuggingFace: num_workers disjoint offset-window shards per entry ---
    for entry in sources:
        if isinstance(entry, HuggingFaceDatasetSource):
            shards.extend([window] for window in arxiv_window_entries(entry))

    return shards


def _get_run_logger() -> logging.Logger:
    """Prefect run logger inside a flow/task; the module logger otherwise.

    Lets the pure helpers log through ``caplog`` when invoked outside a flow
    run (unit tests call them directly).
    """

    try:
        return get_run_logger()  # type: ignore[return-value]
    except Exception:  # noqa: BLE001 — Prefect raises a typed context error
        return logger


@dataclass
class DataFanOutStats:
    """Per-run accounting for the source-shard fan-out (orchestrator path).

    Mirrors the memory ``FanOutStats`` shape (``shards_total`` / ``succeeded`` /
    ``failed`` / ``failures``) so the two splits report identically. The data
    orchestrator does NOT collect per-shard ``Document`` lists back — the worker
    persists documents directly, so the orchestrator only needs the fan-out
    accounting. ``failures`` maps the failing shard index (string) to the
    exception message so one shard's blow-up is logged and isolated, never
    aborting the others.
    """

    shards_total: int = 0
    succeeded: int = 0
    failed: int = 0
    failures: dict[str, str] = field(default_factory=dict)


async def _fan_out_data(
    *,
    user_id: PydanticObjectId,
    shards: list[list[dict[str, Any]]],
    run_deployment: Any,
    opik_trace_headers: dict[str, str] | None = None,
) -> DataFanOutStats:
    """Fan one worker dispatch out per shard, isolate failures, NO trailing step.

    Pure orchestration core (no DB, no partitioning) so the gather /
    failure-isolation contract is unit-testable directly. ``run_deployment`` is
    injected (the Prefect entrypoint in the flow; a fake in tests).

    * One ``data-etl-worker`` run per shard under
      ``asyncio.gather(return_exceptions=True)``, each carrying
      ``{user_id, sources}`` — the orchestrator dispatches a DISTINCT worker
      deployment, so there is NO recursion and NO ``num_shards`` child key (the
      worker has no such param). A single shard's exception is caught, logged,
      recorded in ``stats.failures``, and the gather still completes for the
      others (ADR-002 §3).
    * NO trailing/index run — the data pipeline only produces ``documents``;
      there is no index. This function fires EXACTLY ``len(shards)`` worker runs
      and nothing else.

    ``opik_trace_headers`` (the orchestrator's distributed-trace headers) is
    forwarded to every worker as a flow parameter so the orchestrated data run
    renders as ONE Opik trace across ``run_deployment``'s process hop. ``None``
    is simply not forwarded — each worker then starts its own trace.
    """

    log = _get_run_logger()
    stats = DataFanOutStats(shards_total=len(shards))

    if not shards:
        log.info("data fan-out: 0 shards — nothing to do (no-op)")
        return stats

    worker_params: dict[str, Any] = {}
    if opik_trace_headers is not None:
        worker_params["opik_trace_headers"] = opik_trace_headers

    results = await asyncio.gather(
        *[
            run_deployment(
                "data-etl-worker/data-etl-worker",
                parameters={
                    "user_id": str(user_id),
                    "sources": shard,
                    **worker_params,
                },
            )
            for shard in shards
        ],
        return_exceptions=True,
    )

    for idx, result in enumerate(results):
        if isinstance(result, Exception):
            stats.failed += 1
            stats.failures[str(idx)] = str(result)
            log.error(
                "data fan-out: shard %d FAILED (isolated): %s",
                idx,
                result,
                exc_info=result,
            )
            continue
        stats.succeeded += 1

    log.info(
        "data fan-out: shards_total=%d succeeded=%d failed=%d (NO trailing index)",
        stats.shards_total,
        stats.succeeded,
        stats.failed,
    )

    return stats


# ---------------------------------------------------------------------------
# Orchestrator flow — data-etl-orchestrator (#068)
# ---------------------------------------------------------------------------


async def _resolve_target_user_ids(
    user_id: PydanticObjectId | None,
) -> list[PydanticObjectId]:
    """Resolve which tenants a data run targets.

    * An explicit ``user_id`` → just that tenant (the manual
      ``make run-data-pipeline`` path).
    * ``None`` → every ACTIVE user (the nightly scheduled run fans the SAME
      global sources out per tenant, mirroring dream consolidation). This branch
      reads the DB to enumerate active users, so we connect only when needed.
    """

    if user_id is not None:
        return [user_id]

    client = await init_mongodb(
        settings.mongo.mongo_uri.get_secret_value(),
        settings.mongo.mongo_initdb_database,
    )
    database = client[settings.mongo.mongo_initdb_database]
    return await select_active_user_ids(database=database)


def _resolve_source_set(
    source_files: list[str] | None,
    sources: list[dict[str, Any]] | None,
) -> list[SourceEntry]:
    """Resolve the orchestrator's source set: file(s) ++ inline, else the default.

    CONCATENATES (NOT either/or): :func:`load_sources` over ``source_files`` (when
    given) is followed by the coerced inline ``sources`` (when given), in that
    order. When BOTH are absent, the set is :func:`default_configured_sources`
    (backfill + listen). Inline dicts are coerced through the discriminated-union
    ``_SOURCES_ADAPTER`` — the same ``TypeAdapter`` the worker re-parses shards with.

    ``default_configured_sources`` returns a CACHED list; this never mutates it (it
    is partitioned into fresh per-platform shards downstream).
    """

    if source_files is None and sources is None:
        return default_configured_sources()

    resolved: list[SourceEntry] = []
    if source_files is not None:
        resolved.extend(load_sources(list(source_files)))
    if sources is not None:
        resolved.extend(_SOURCES_ADAPTER.validate_python(sources))
    return resolved


@flow(name="data-etl-orchestrator", log_prints=True)
async def data_etl_orchestrator(
    user_id: PydanticObjectId | None = None,
    source_files: list[str] | None = None,
    sources: list[dict[str, Any]] | None = None,
) -> DataFanOutStats:
    """Resolve sources → group by Platform → dispatch ``data-etl-worker`` runs.

    The operator entrypoint for data ingestion (ADR-002 §3, amended #070–#074;
    source selection per ADR-003). Resolves its source set via
    :func:`_resolve_source_set` — ``load_sources(source_files)`` ++ the coerced
    inline ``sources``, or :func:`default_configured_sources` (backfill+listen) when
    BOTH are absent — then partitions it via :func:`_partition_sources_by_platform`
    into:

    * ONE homogeneous ``data-etl-worker`` run per non-HuggingFace **Platform** bucket
      present (``substack`` / ``youtube`` / ``custom``), AND
    * ``num_workers`` worker runs per ``HuggingFaceDatasetSource``, one per disjoint
      offset-**Window** of the dataset.

    Run modes share this one deployment:

    * **Manual** (``user_id`` set) — ``make run-data-pipeline-offline``: ingest the
      resolved source set for that one tenant (default = backfill+listen, or the
      operator's ``--source-file`` / ``--uri`` selection).
    * **Scheduled** (``user_id=None``, ``source_files=["sources/listen.yaml"]``) —
      the deployment's nightly cron: ingest the polled listen feeds, fanned out
      across ALL active users.

    Each shard is dispatched via ``run_deployment`` under
    ``asyncio.gather(return_exceptions=True)`` carrying ``{user_id, sources}`` (the
    shard's serialized source entries). There is NO recursion (a DISTINCT worker
    deployment; the worker never calls ``run_deployment``) and NO trailing step — the
    data pipeline only produces ``documents``; there is no index.

    An empty resolved set ⇒ clean no-op: zero worker dispatch,
    ``DataFanOutStats(shards_total=0)``. One shard's failure is isolated and recorded
    in :class:`DataFanOutStats.failures` (keyed ``user_id:shard_index``) while the
    others proceed.
    """

    # Configure Opik in this flow-run process and own ONE trace whose
    # distributed headers are forwarded to every worker run, so the orchestrated
    # data run renders as a single trace across ``run_deployment``'s process hop.
    configure_opik()
    try:
        with span("data-etl-orchestrator", tags=_DATA_TAGS, metadata=_DATA_METADATA):
            resolved_sources = _resolve_source_set(source_files, sources)
            if not resolved_sources:
                logger.info(
                    "data fan-out: empty resolved source set "
                    "(source_files=%s, inline sources=%s) — nothing to do "
                    "(no child runs, no index run)",
                    source_files,
                    "set" if sources else "unset",
                )
                return DataFanOutStats(shards_total=0)

            typed_shards = _partition_sources_by_platform(resolved_sources)
            shards = [[e.model_dump() for e in shard] for shard in typed_shards]

            user_ids = await _resolve_target_user_ids(user_id)
            if not user_ids:
                logger.info(
                    "data fan-out: no target users (scheduled all-users run found "
                    "no active users) — nothing to do"
                )
                return DataFanOutStats(shards_total=0)

            logger.info(
                "data fan-out: grouped %d source(s) into %d Platform/Window shard(s) "
                "for %d tenant(s)",
                len(resolved_sources),
                len(shards),
                len(user_ids),
            )

            headers = get_distributed_trace_headers()
            # Fan out per tenant SEQUENTIALLY — each user's shards already gather
            # in parallel inside _fan_out_data, so a per-user loop bounds the load
            # at one user's worth of worker runs at a time.
            # ponytail: sequential across users; parallelize if the nightly window
            # gets tight with many tenants.
            aggregate = DataFanOutStats()
            for uid in user_ids:
                stats = await _fan_out_data(
                    user_id=uid,
                    shards=shards,
                    run_deployment=run_deployment,
                    opik_trace_headers=headers,
                )
                aggregate.shards_total += stats.shards_total
                aggregate.succeeded += stats.succeeded
                aggregate.failed += stats.failed
                for shard_idx, message in stats.failures.items():
                    aggregate.failures[f"{uid}:{shard_idx}"] = message
            return aggregate
    finally:
        flush_opik()
