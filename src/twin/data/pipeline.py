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

    substack_config = app_config.data_pipeline.substack
    if substack_config.enabled and substack_config.feeds:
        logger.info(
            "Starting substack pipeline with %d feeds", len(substack_config.feeds)
        )
        substack_docs = await ingest_substack_rss_feed_batch(substack_config.feeds)
        all_ingested.extend(substack_docs)
        logger.info("Substack pipeline ingested %d documents", len(substack_docs))
    else:
        logger.info("Substack pipeline is disabled or has no feeds configured")

    arxiv_config = app_config.data_pipeline.huggingface_arxiv_dataset
    if arxiv_config.enabled:
        logger.info(
            "Starting arxiv dataset pipeline (max_samples=%d)", arxiv_config.max_samples
        )
        arxiv_docs = await ingest_arxiv_dataset()
        all_ingested.extend(arxiv_docs)
        logger.info("Arxiv pipeline ingested %d documents", len(arxiv_docs))
    else:
        logger.info("Arxiv dataset pipeline is disabled")

    logger.info("All data pipelines complete. Total ingested: %d", len(all_ingested))

    return all_ingested
