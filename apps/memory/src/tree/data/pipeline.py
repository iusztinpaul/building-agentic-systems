"""
Unified data pipeline orchestrator.

Spawns individual data pipeline flows for each enabled source
based on the application configuration.

Usage:
    Served as a Prefect deployment via the orchestrator.
    Triggered via: make run-all-data-pipelines
"""

import asyncio
import logging

from prefect import flow

from tree.config.app_config import app_config
from tree.config.settings import settings
from tree.data.core.ingest import ingest_url
from tree.data.huggingface.arxiv_dataset_pipeline import ingest_arxiv_dataset
from tree.data.substack.substack_article_pipeline import ingest_substack_article_batch
from tree.data.substack.substack_rss_pipeline import ingest_substack_rss_feed_batch
from tree.db import init_mongodb
from tree.entities.documents import Document

logger = logging.getLogger(__name__)


@flow(name="ingest-all-data-etl", log_prints=True)
async def ingest_all_data() -> list[Document]:
    """Run all enabled data pipelines as sub-flows."""

    await init_mongodb(
        settings.mongo.mongo_uri.get_secret_value(),
        settings.mongo.mongo_initdb_database,
    )

    all_ingested: list[Document] = []

    substack_feeds = app_config.sources.substack
    if substack_feeds:
        logger.info("Starting substack RSS pipeline with %d feeds", len(substack_feeds))
        substack_docs = await ingest_substack_rss_feed_batch(substack_feeds)
        all_ingested.extend(substack_docs)
        logger.info("Substack RSS pipeline ingested %d documents", len(substack_docs))
    else:
        logger.info("Substack RSS pipeline skipped: no feeds configured")

    substack_articles = app_config.sources.substack_articles
    if substack_articles:
        logger.info(
            "Starting substack article pipeline with %d URLs", len(substack_articles)
        )
        article_docs = await ingest_substack_article_batch(substack_articles)
        all_ingested.extend(article_docs)
        logger.info(
            "Substack article pipeline ingested %d documents", len(article_docs)
        )
    else:
        logger.info("Substack article pipeline skipped: no articles configured")

    arxiv_config = app_config.sources.huggingface_arxiv_dataset
    logger.info(
        "Starting arxiv dataset pipeline (max_samples=%d)", arxiv_config.max_samples
    )
    arxiv_docs = await ingest_arxiv_dataset()
    all_ingested.extend(arxiv_docs)
    logger.info("Arxiv pipeline ingested %d documents", len(arxiv_docs))

    urls = app_config.sources.urls
    if urls:
        logger.info("Starting URL pipeline (dispatcher) with %d URLs", len(urls))
        url_results = await asyncio.gather(*[ingest_url(u) for u in urls])
        url_docs = [d for d in url_results if d is not None]
        all_ingested.extend(url_docs)
        logger.info("URL pipeline ingested %d documents", len(url_docs))
    else:
        logger.info("URL pipeline skipped: no URLs configured")

    logger.info("All data pipelines complete. Total ingested: %d", len(all_ingested))

    return all_ingested
