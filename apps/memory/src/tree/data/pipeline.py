"""
Unified data pipeline orchestrator.

Walks ``app_config.sources.sources`` (a flat list of typed ``SourceEntry``
instances) and dispatches each entry to the appropriate sub-flow based on
its discriminated-union variant:

- ``SubstackRssSource`` entries are batched into a single call to
  ``ingest_substack_rss_feed_batch``.
- ``SubstackArticleSource`` entries are batched into a single call to
  ``ingest_substack_article_batch``.
- ``HuggingFaceArxivSource`` entries are dispatched one-by-one to
  ``ingest_arxiv_dataset`` (typically only one such entry).
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

from prefect import flow

from tree.config.app_config import (
    HuggingFaceArxivSource,
    SubstackArticleSource,
    SubstackRssSource,
    WebSource,
    app_config,
)
from tree.config.settings import settings
from tree.data.core.ingest import ingest_url
from tree.data.huggingface.arxiv_dataset_pipeline import ingest_arxiv_dataset
from tree.data.substack.substack_article_pipeline import ingest_substack_article_batch
from tree.data.substack.substack_rss_pipeline import ingest_substack_rss_feed_batch
from tree.db import init_mongodb
from tree.entities.documents import Document

logger = logging.getLogger(__name__)


@flow(name="data-pipeline-etl", log_prints=True)
async def data_pipeline() -> list[Document]:
    """Walk the flat ``app_config.sources.sources`` list and dispatch each
    entry to the right sub-flow.
    """

    await init_mongodb(
        settings.mongo.mongo_uri.get_secret_value(),
        settings.mongo.mongo_initdb_database,
    )

    all_ingested: list[Document] = []

    sources = app_config.sources.sources

    # --- Substack RSS (batched into one call) ---
    rss_entries = [s for s in sources if isinstance(s, SubstackRssSource)]
    if rss_entries:
        feed_urls = [s.uri for s in rss_entries]
        logger.info("Starting substack RSS pipeline with %d feeds", len(feed_urls))
        rss_docs = await ingest_substack_rss_feed_batch(feed_urls)
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
        article_docs = await ingest_substack_article_batch(article_urls)
        all_ingested.extend(article_docs)
        logger.info(
            "Substack article pipeline ingested %d documents", len(article_docs)
        )
    else:
        logger.info(
            "Substack article pipeline skipped: no substack_article entries configured"
        )

    # --- HuggingFace arxiv (one call per entry; typically only one) ---
    arxiv_entries = [s for s in sources if isinstance(s, HuggingFaceArxivSource)]
    if arxiv_entries:
        for entry in arxiv_entries:
            logger.info(
                "Starting arxiv dataset pipeline (max_samples=%d, fetch_content=%s)",
                entry.max_samples,
                entry.fetch_content,
            )
            arxiv_docs = await ingest_arxiv_dataset(
                max_samples=entry.max_samples,
                fetch_content=entry.fetch_content,
            )
            all_ingested.extend(arxiv_docs)
            logger.info("Arxiv pipeline ingested %d documents", len(arxiv_docs))
    else:
        logger.info("Arxiv pipeline skipped: no huggingface_arxiv entries configured")

    # --- Generic web URLs (parallel dispatch via the URL router) ---
    web_entries = [s for s in sources if isinstance(s, WebSource)]
    if web_entries:
        logger.info("Starting URL pipeline (dispatcher) with %d URLs", len(web_entries))
        url_results = await asyncio.gather(*[ingest_url(s.uri) for s in web_entries])
        url_docs = [d for d in url_results if d is not None]
        all_ingested.extend(url_docs)
        logger.info("URL pipeline ingested %d documents", len(url_docs))
    else:
        logger.info("URL pipeline skipped: no web entries configured")

    logger.info("All data pipelines complete. Total ingested: %d", len(all_ingested))

    return all_ingested
