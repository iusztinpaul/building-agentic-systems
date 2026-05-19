"""
Unified data pipeline orchestrator.

Walks ``app_config.sources.sources`` (a flat list of typed ``SourceEntry``
instances) and dispatches each entry to the appropriate sub-flow based on
its discriminated-union variant:

- ``SubstackRssSource`` entries are batched into a single call to
  ``ingest_substack_rss_feed_batch``.
- ``SubstackArticleSource`` entries are batched into a single call to
  ``ingest_substack_article_batch``.
- ``HuggingFaceDatasetSource`` entries are dispatched per-entry through
  ``_HUGGINGFACE_DATASET_HANDLERS``, keyed on the dataset id (``uri``).
  Unknown dataset ids raise ``ValueError``.
- ``WebSource`` entries are dispatched in parallel via the ``ingest_url``
  router, which handles substack-domain matching and the generic web
  fallback.

If a variant has zero entries the corresponding sub-flow is skipped.

Usage:
    Served as a Prefect deployment via the orchestrator. Triggered via the
    unified ``run-data-pipeline`` Make target (wired in #010).
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable

from beanie import PydanticObjectId
from prefect import flow

from tree.config.app_config import (
    HuggingFaceDatasetSource,
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
from tree.data.substack.substack_article_pipeline import ingest_substack_article_batch
from tree.data.substack.substack_rss_pipeline import ingest_substack_rss_feed_batch
from tree.data.youtube.youtube_rss_pipeline import ingest_youtube_rss_feed_batch
from tree.data.youtube.youtube_video_pipeline import ingest_youtube_video_batch
from tree.db import init_mongodb
from tree.entities.documents import Document
from tree.memory.indexing.core import assert_settings_match_live_vector_index

logger = logging.getLogger(__name__)


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


@flow(name="data-pipeline-etl", log_prints=True)
async def data_pipeline(user_id: PydanticObjectId) -> list[Document]:
    """Walk the flat ``app_config.sources.sources`` list and dispatch each
    entry to the right sub-flow under ``user_id``.
    """

    client = await init_mongodb(
        settings.mongo.mongo_uri.get_secret_value(),
        settings.mongo.mongo_initdb_database,
    )

    # #016+#034 boot-time gate: refuse to run if ``app_config.models.embedding.dimensions``
    # disagrees with the live Atlas Vector Search index. The data
    # pipeline itself does not write vectors, but it produces the
    # documents the indexing pipeline will embed — a silent dim drift
    # here corrupts every downstream embedding write. ``vector_index
    # not found`` is non-fatal at this layer (first-ever run, indexing
    # hasn't bootstrapped the index yet) — only a real dim **mismatch**
    # hard-fails.
    try:
        await assert_settings_match_live_vector_index(
            client, settings.mongo.mongo_initdb_database
        )
    except RuntimeError as exc:
        if "vector_index not found" in str(exc):
            logger.info(
                "vector_index not yet provisioned; skipping dim-check at "
                "data_pipeline boot. The indexing pipeline will bootstrap it."
            )
        else:
            raise

    all_ingested: list[Document] = []

    sources = app_config.sources.sources

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

    logger.info("All data pipelines complete. Total ingested: %d", len(all_ingested))

    return all_ingested
