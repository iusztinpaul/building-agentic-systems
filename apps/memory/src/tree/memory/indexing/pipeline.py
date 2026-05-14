"""
Prefect tasks and flows for knowledge graph indexing.

Post-extraction pipeline: embeddings, search indexes.
"""

import logging

from prefect import flow, task
from prefect.cache_policies import NO_CACHE

from tree.config.settings import settings
from tree.db import init_mongodb
from tree.memory.indexing.core import (
    embed_nodes,
    ensure_indexes,
)
from tree.models.get_model import get_embedding_model

logger = logging.getLogger(__name__)


@task(name="embed-kg-nodes", retries=1, retry_delay_seconds=10, cache_policy=NO_CACHE)
async def embed_nodes_task(client, database: str) -> int:
    embedding_model = get_embedding_model()
    return await embed_nodes(client, database, embedding_model)


@task(name="ensure-kg-indexes", retries=1, retry_delay_seconds=5, cache_policy=NO_CACHE)
async def ensure_indexes_task(client, database: str) -> None:
    embedding_model = get_embedding_model()
    await ensure_indexes(client, database, embedding_model=embedding_model)


@flow(name="memory-indexing-etl", log_prints=True)
async def memory_indexing() -> None:
    """Embed nodes and ensure search indexes."""

    client = await init_mongodb(
        settings.mongo.mongo_uri.get_secret_value(),
        settings.mongo.mongo_initdb_database,
    )
    database = settings.mongo.mongo_initdb_database

    count = await embed_nodes_task(client, database)
    await ensure_indexes_task(client, database)

    logger.info("Indexing pipeline finished. Embedded %d nodes.", count)
