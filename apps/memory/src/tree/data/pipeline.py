"""Data pipeline: orchestrator + worker deployments (#068, ADR-002 §3 amended #066).

Two Prefect flows live here, mirroring the memory split (#067) minus the trailing
index — the data pipeline only produces ``documents``; there is NO index step:

* ``data_etl_worker`` (deployment ``data-etl-worker``) — ingests a SUBSET (shard) of
  the configured ``sources:`` list, reusing the existing per-source-type batch logic.
  It groups the shard's sources by discriminated-union variant and dispatches each
  entry to the appropriate sub-flow:

  - ``SubstackRssSource`` entries are batched into one ``ingest_substack_rss_feed_batch``.
  - ``SubstackArticleSource`` entries are batched into one ``ingest_substack_article_batch``.
  - ``YouTubeRssSource`` entries are batched into one ``ingest_youtube_rss_feed_batch``.
  - ``YouTubeVideoSource`` entries are batched into one ``ingest_youtube_video_batch``.
  - ``HuggingFaceDatasetSource`` entries are dispatched per-entry through
    ``_HUGGINGFACE_DATASET_HANDLERS``, keyed on the dataset id (``uri``). Unknown
    dataset ids raise ``ValueError``.
  - ``WebSource`` entries are dispatched in parallel via the ``ingest_url`` router.

  A variant absent from the shard is skipped (with a scoped "skipped: no X entries"
  log line). NO partitioning, NO ``run_deployment``, NO orchestration — the worker is
  the orchestrator's internal dispatch target (but may be triggered directly for a
  bare shard ingestion). Registered as deployment ``data-etl-worker``.

* ``data_etl_orchestrator`` (deployment ``data-etl-orchestrator``) — reads the
  configured ``sources:`` list and partitions it by PLATFORM (#072, ADR-002 §3
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
from prefect import flow, get_run_logger
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
    app_config,
)
from tree.config.settings import settings
from tree.data.core.ingest import ingest_url
from tree.data.huggingface.arxiv_dataset_pipeline import (
    arxiv_window_entries,
    ingest_arxiv_dataset,
)

# The batched sub-flows are referenced by NAME in ``_BATCHED_VARIANTS`` and looked
# up at call time via ``globals()[batch_fn_name]`` (see ``_BatchedVariant.batch_fn``).
# They MUST be imported here so those names are module globals: that's what makes
# the lookup resolve in production AND lets ``mocker.patch("...pipeline.<name>")``
# rebind them in tests. Each import carries a per-line F401-suppression because
# ruff can't see the ``globals()`` use; dropping these imports turns every
# batched-variant dispatch into a runtime ``KeyError`` (regression guarded by
# ``test_every_batched_variant_resolves_without_mocks``).
from tree.data.substack.substack_article_pipeline import (
    ingest_substack_article_batch,  # noqa: F401
)
from tree.data.substack.substack_rss_pipeline import (
    ingest_substack_rss_feed_batch,  # noqa: F401
)
from tree.data.youtube.youtube_rss_pipeline import (
    ingest_youtube_rss_feed_batch,  # noqa: F401
)
from tree.data.youtube.youtube_video_pipeline import (
    ingest_youtube_video_batch,  # noqa: F401
)
from tree.db import init_mongodb
from tree.entities.documents import Document
from tree.memory.indexing.core import assert_settings_match_live_vector_index
from tree.observability import (
    TAGS_INGESTION_BATCH,
    configure_opik,
    flush_opik,
    get_distributed_trace_headers,
    pipeline_metadata,
    span,
    tracked_span,
)

# Four-tag family: offline Prefect ingestion = ``["ingestion", "batch"]``. The
# former ``"data-pipeline"`` pipeline-name tag is now span metadata.
_DATA_TAGS = TAGS_INGESTION_BATCH
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
# Add a new dataset by registering its id alongside a handler that maps
# the source entry to the right ingestion flow.
_HUGGINGFACE_DATASET_HANDLERS: dict[
    str,
    Callable[[HuggingFaceDatasetSource, PydanticObjectId], Awaitable[list[Document]]],
] = {
    "librarian-bots/arxiv-metadata-snapshot": _ingest_arxiv_dataset_entry,
}


# A batched sub-flow: takes a list of URIs + the user and returns the ingested docs.
_BatchFn = Callable[[list[str], PydanticObjectId], Awaitable[list[Document]]]


@dataclass(frozen=True)
class _BatchedVariant:
    """One source variant whose entries are batched into a single sub-flow call.

    ``source_type`` selects the entries via ``isinstance``; ``batch_fn_name`` is the
    module-global name of the batched ingestion sub-flow, resolved from
    :func:`globals` at CALL time (NOT a captured reference) so a test that
    ``mocker.patch``-es the module global is honoured — same late binding the old
    per-variant ``await ingest_..._batch(...)`` calls had. ``label`` names the
    pipeline in log lines; ``unit`` is the noun for the entry count (``feeds`` /
    ``URLs``); ``config_key`` is the YAML source key reported in the "skipped: no
    <key> entries" log line.
    """

    source_type: type[SourceEntry]
    batch_fn_name: str
    label: str
    unit: str
    config_key: str

    @property
    def batch_fn(self) -> _BatchFn:
        """The batch sub-flow, looked up by name in the module namespace.

        Resolved on every access so ``mocker.patch("...pipeline.<name>")`` (which
        rebinds the module global) takes effect — a frozen reference captured at
        import time would bypass the patch and hit the network.
        """

        return globals()[self.batch_fn_name]


# Table for the four byte-identical batched variants. Order is load-bearing — it
# fixes the ingestion order Substack RSS → Substack article → YouTube RSS → YouTube
# video, and the order their log lines are emitted.
_BATCHED_VARIANTS: list[_BatchedVariant] = [
    _BatchedVariant(
        SubstackRssSource,
        "ingest_substack_rss_feed_batch",
        "Substack RSS",
        "feeds",
        "substack_rss",
    ),
    _BatchedVariant(
        SubstackArticleSource,
        "ingest_substack_article_batch",
        "Substack article",
        "URLs",
        "substack_article",
    ),
    _BatchedVariant(
        YouTubeRssSource,
        "ingest_youtube_rss_feed_batch",
        "YouTube RSS",
        "feeds",
        "youtube_rss",
    ),
    _BatchedVariant(
        YouTubeVideoSource,
        "ingest_youtube_video_batch",
        "YouTube video",
        "URLs",
        "youtube_video",
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


@tracked_span("_ingest_sources", tags=_DATA_TAGS)
async def _ingest_sources(
    sources: list[SourceEntry],
    user_id: PydanticObjectId,
    opik_trace_headers: dict[str, str] | None = None,
) -> list[Document]:
    """Ingest a list of typed source entries by grouping them by variant.

    Reuses the existing per-source-type batch logic, scoped to the entries handed in
    (a shard, or the full configured list). A variant absent from ``sources`` is
    skipped with a scoped "skipped: no X entries" log line.

    ``opik_trace_headers`` attaches this task's span to the worker flow's trace.
    """

    all_ingested: list[Document] = []

    # --- Batched variants (Substack RSS/article, YouTube RSS/video) ---
    # Ordering is preserved: Substack RSS → Substack article → YouTube RSS → YouTube video.
    for variant in _BATCHED_VARIANTS:
        entries = [s for s in sources if isinstance(s, variant.source_type)]
        if entries:
            uris = [s.uri for s in entries]
            logger.info(
                "Starting %s pipeline with %d %s",
                variant.label,
                len(uris),
                variant.unit,
            )
            docs = await variant.batch_fn(uris, user_id)
            all_ingested.extend(docs)
            logger.info("%s pipeline ingested %d documents", variant.label, len(docs))
        else:
            logger.info(
                "%s pipeline skipped: no %s entries configured",
                variant.label,
                variant.config_key,
            )

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

    # --- Generic web URLs (parallel dispatch via the URL router) ---
    web_entries = [s for s in sources if isinstance(s, WebSource)]
    if web_entries:
        logger.info("Starting URL pipeline (dispatcher) with %d URLs", len(web_entries))
        url_results = await asyncio.gather(
            *[ingest_url(s.uri, user_id) for s in web_entries]
        )
        url_docs = [d for d in url_results if d is not None]
        all_ingested.extend(url_docs)
        logger.info("URL pipeline ingested %d documents", len(url_docs))
    else:
        logger.info("URL pipeline skipped: no web entries configured")

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
    with unknown-id ``ValueError``, web via ``ingest_url``). A variant absent from the
    shard is skipped. This is PURE ingestion: NO partitioning, NO ``run_deployment``,
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
            return await _ingest_sources(
                typed_sources, user_id, opik_trace_headers=headers
            )
    finally:
        # Flush batched Opik telemetry (fail-open; no-op without OPIK_API_KEY).
        flush_opik()


# ---------------------------------------------------------------------------
# Source-shard fan-out (orchestrator path)
# ---------------------------------------------------------------------------


# Platform buckets for the data orchestrator's GROUP-BY-PLATFORM partition
# (ADR-002 §3 amendment #070–#074). Each non-HuggingFace source variant maps to a
# Platform; the variants sharing a Platform land in ONE homogeneous worker shard.
# Order is load-bearing — it fixes the dispatch order of the non-HF Platform shards
# (substack → youtube → custom) and is asserted by the orchestrator tests. HuggingFace
# is handled separately (offset-window sub-fan-out via :func:`arxiv_window_entries`),
# so it is NOT in this map.
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
      order. The worker's existing ``_ingest_sources`` ``isinstance`` routing then
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


@flow(name="data-etl-orchestrator", log_prints=True)
async def data_etl_orchestrator(
    user_id: PydanticObjectId,
) -> DataFanOutStats:
    """Read configured sources → group by Platform → dispatch ``data-etl-worker`` runs.

    The operator entrypoint for data ingestion (ADR-002 §3, amended #070–#074). Reads
    the configured ``app_config.sources.sources`` list and partitions it via
    :func:`_partition_sources_by_platform` into:

    * ONE homogeneous ``data-etl-worker`` run per non-HuggingFace **Platform** bucket
      present (``substack`` / ``youtube`` / ``custom``), AND
    * ``num_workers`` worker runs per ``HuggingFaceDatasetSource``, one per disjoint
      offset-**Window** of the dataset.

    Each shard is dispatched via ``run_deployment`` under
    ``asyncio.gather(return_exceptions=True)`` carrying ``{user_id, sources}`` (the
    shard's serialized source entries). There is NO recursion (a DISTINCT worker
    deployment; the worker never calls ``run_deployment``) and NO trailing step — the
    data pipeline only produces ``documents``; there is no index. Parallelism is
    declared per-source (Platform bucketing + HF ``num_workers``), NOT via a global
    ``num_shards`` count — that knob is gone.

    Empty configured sources ⇒ clean no-op: zero worker dispatch,
    ``DataFanOutStats(shards_total=0)``. One shard's failure is isolated and recorded in
    :class:`DataFanOutStats.failures` while the others proceed.
    """

    # Configure Opik in this flow-run process and own ONE trace whose
    # distributed headers are forwarded to every worker run, so the orchestrated
    # data run renders as a single trace across ``run_deployment``'s process hop.
    configure_opik()
    try:
        with span("data-etl-orchestrator", tags=_DATA_TAGS, metadata=_DATA_METADATA):
            sources = app_config.sources.sources
            if not sources:
                logger.info(
                    "data fan-out: no configured sources for user_id=%s — nothing "
                    "to do (no child runs, no index run)",
                    user_id,
                )
                return DataFanOutStats(shards_total=0)

            typed_shards = _partition_sources_by_platform(sources)
            # Serialize each shard's entries to JSON-safe dicts so they round-trip
            # through the ``run_deployment`` flow-run parameters. The worker re-parses
            # to ``SourceEntry`` (the ``type`` discriminator + HF ``offset``/
            # ``max_samples`` window coordinates round-trip cleanly).
            shards = [[e.model_dump() for e in shard] for shard in typed_shards]
            logger.info(
                "data fan-out: grouped %d source(s) into %d Platform/Window shard(s)",
                len(sources),
                len(shards),
            )

            headers = get_distributed_trace_headers()
            return await _fan_out_data(
                user_id=user_id,
                shards=shards,
                run_deployment=run_deployment,
                opik_trace_headers=headers,
            )
    finally:
        flush_opik()
