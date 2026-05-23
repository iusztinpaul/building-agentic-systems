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
  configured ``sources:`` list, partitions into ``min(num_shards, N)`` balanced
  shards, and dispatches one ``data-etl-worker`` run per shard via ``run_deployment``
  under ``asyncio.gather(return_exceptions=True)``. NO trailing step. Empty sources ⇒
  clean no-op (``shards_total=0``). Registered as deployment ``data-etl-orchestrator``.

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
from typing import Any

from beanie import PydanticObjectId
from prefect import flow
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
from tree.data.huggingface.arxiv_dataset_pipeline import ingest_arxiv_dataset
from tree.data.sharding import (
    DataFanOutStats,
    _fan_out_data,
    _partition_into_shards,
    _resolve_num_shards,
)
from tree.data.substack.substack_article_pipeline import ingest_substack_article_batch
from tree.data.substack.substack_rss_pipeline import ingest_substack_rss_feed_batch
from tree.data.youtube.youtube_rss_pipeline import ingest_youtube_rss_feed_batch
from tree.data.youtube.youtube_video_pipeline import ingest_youtube_video_batch
from tree.db import init_mongodb
from tree.entities.documents import Document
from tree.memory.indexing.core import assert_settings_match_live_vector_index

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


async def _ingest_sources(
    sources: list[SourceEntry], user_id: PydanticObjectId
) -> list[Document]:
    """Ingest a list of typed source entries by grouping them by variant.

    Reuses the existing per-source-type batch logic, scoped to the entries handed in
    (a shard, or the full configured list). A variant absent from ``sources`` is
    skipped with a scoped "skipped: no X entries" log line.
    """

    all_ingested: list[Document] = []

    # --- Substack RSS (batched into one call) ---
    rss_entries = [s for s in sources if isinstance(s, SubstackRssSource)]
    if rss_entries:
        feed_urls = [s.uri for s in rss_entries]
        logger.info("Starting substack RSS pipeline with %d feeds", len(feed_urls))
        rss_docs = await ingest_substack_rss_feed_batch(feed_urls, user_id)
        all_ingested.extend(rss_docs)
        logger.info("Substack RSS pipeline ingested %d documents", len(rss_docs))
    else:
        logger.info("Substack RSS pipeline skipped: no substack_rss entries configured")

    # --- Substack articles (batched into one call) ---
    article_entries = [s for s in sources if isinstance(s, SubstackArticleSource)]
    if article_entries:
        article_urls = [s.uri for s in article_entries]
        logger.info(
            "Starting substack article pipeline with %d URLs", len(article_urls)
        )
        article_docs = await ingest_substack_article_batch(article_urls, user_id)
        all_ingested.extend(article_docs)
        logger.info(
            "Substack article pipeline ingested %d documents", len(article_docs)
        )
    else:
        logger.info(
            "Substack article pipeline skipped: no substack_article entries configured"
        )

    # --- YouTube RSS (batched into one call) ---
    yt_rss_entries = [s for s in sources if isinstance(s, YouTubeRssSource)]
    if yt_rss_entries:
        yt_rss_urls = [s.uri for s in yt_rss_entries]
        logger.info("Starting YouTube RSS pipeline with %d feeds", len(yt_rss_urls))
        yt_rss_docs = await ingest_youtube_rss_feed_batch(yt_rss_urls, user_id)
        all_ingested.extend(yt_rss_docs)
        logger.info("YouTube RSS pipeline ingested %d documents", len(yt_rss_docs))
    else:
        logger.info("YouTube RSS pipeline skipped: no youtube_rss entries configured")

    # --- YouTube videos (batched into one call) ---
    yt_video_entries = [s for s in sources if isinstance(s, YouTubeVideoSource)]
    if yt_video_entries:
        yt_video_urls = [s.uri for s in yt_video_entries]
        logger.info("Starting YouTube video pipeline with %d URLs", len(yt_video_urls))
        yt_video_docs = await ingest_youtube_video_batch(yt_video_urls, user_id)
        all_ingested.extend(yt_video_docs)
        logger.info("YouTube video pipeline ingested %d documents", len(yt_video_docs))
    else:
        logger.info(
            "YouTube video pipeline skipped: no youtube_video entries configured"
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
    """

    client = await init_mongodb(
        settings.mongo.mongo_uri.get_secret_value(),
        settings.mongo.mongo_initdb_database,
    )

    # Boot-time gate: refuse to run if
    # ``app_config.models.search_embedding.dimensions`` disagrees with the
    # live Atlas Vector Search index. The data pipeline itself does not write
    # vectors, but it produces the documents the indexing pipeline will embed —
    # a silent dim drift here corrupts every downstream embedding write.
    # ``vector_index not found`` is non-fatal at this layer (first-ever run,
    # indexing hasn't bootstrapped the index yet) — only a real dim **mismatch**
    # hard-fails.
    try:
        await assert_settings_match_live_vector_index(
            client, settings.mongo.mongo_initdb_database
        )
    except RuntimeError as exc:
        if "vector_index not found" in str(exc):
            logger.info(
                "vector_index not yet provisioned; skipping dim-check at "
                "data_etl_worker boot. The indexing pipeline will bootstrap it."
            )
        else:
            raise

    typed_sources = _coerce_sources(sources)
    return await _ingest_sources(typed_sources, user_id)


# ---------------------------------------------------------------------------
# Orchestrator flow — data-etl-orchestrator (#068)
# ---------------------------------------------------------------------------


@flow(name="data-etl-orchestrator", log_prints=True)
async def data_etl_orchestrator(
    user_id: PydanticObjectId,
    num_shards: int = 1,
) -> DataFanOutStats:
    """Read configured sources → partition → dispatch ``data-etl-worker`` runs.

    The operator entrypoint for data ingestion (ADR-002 §3, amended #066). Reads the
    configured ``app_config.sources.sources`` list, partitions it into
    ``min(num_shards, N)`` balanced shards, and dispatches ONE ``data-etl-worker`` run
    per shard via ``run_deployment`` under ``asyncio.gather(return_exceptions=True)``.
    Each worker dispatch carries ``{user_id, sources}`` (the shard's serialized source
    entries). There is NO recursion (a DISTINCT worker deployment) and NO trailing
    step — the data pipeline only produces ``documents``; there is no index.

    Empty configured sources ⇒ clean no-op: zero worker dispatch,
    ``DataFanOutStats(shards_total=0)``. ``num_shards=1`` (the default) dispatches 1
    worker run with all sources. One shard's failure is isolated and recorded in
    :class:`DataFanOutStats.failures`.
    """

    effective_num_shards = _resolve_num_shards(num_shards)

    sources = app_config.sources.sources
    if not sources:
        logger.info(
            "data fan-out: no configured sources for user_id=%s — nothing to do "
            "(no child runs, no index run)",
            user_id,
        )
        return DataFanOutStats(shards_total=0)

    # Serialize each source entry to a JSON-safe dict so it round-trips through the
    # ``run_deployment`` flow-run parameters. The worker re-parses to ``SourceEntry``.
    serialized = [s.model_dump() for s in sources]
    shards = _partition_into_shards(serialized, effective_num_shards)
    logger.info(
        "data fan-out: partitioned %d source(s) into %d shard(s) (num_shards=%d)",
        len(sources),
        len(shards),
        effective_num_shards,
    )

    return await _fan_out_data(
        user_id=user_id,
        shards=shards,
        run_deployment=run_deployment,
    )
