"""
Unified data pipeline orchestrator.

Spawns individual data pipeline flows for each enabled source
based on the application configuration.

Usage:
    Served as a Prefect deployment via the orchestrator.
    Triggered via: make run-all-data-pipelines
"""

import logging

from prefect import flow

from twin.config.app_config import app_config
from twin.config.settings import settings
from twin.data.huggingface.arxiv_dataset_pipeline import ingest_arxiv_dataset
from twin.data.substack.substack_article_pipeline import ingest_substack_article_batch
from twin.data.substack.substack_rss_pipeline import ingest_substack_rss_feed_batch
from twin.db import init_mongodb
from twin.entities.documents import Document

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

    logger.info("All data pipelines complete. Total ingested: %d", len(all_ingested))

    return all_ingested
