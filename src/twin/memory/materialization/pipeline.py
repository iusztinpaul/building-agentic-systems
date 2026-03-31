"""
Prefect tasks and flows for knowledge graph indexing.

Post-extraction pipeline: reverse edges, embeddings, search indexes.
"""

import logging

from prefect import flow, task
from prefect.cache_policies import NO_CACHE

from twin.config.settings import settings
from twin.db import init_mongodb
from twin.memory.materialization.core import (
    create_reverse_edges,
    embed_nodes,
    ensure_indexes,
)
from twin.models.get_model import get_embedding_model

logger = logging.getLogger(__name__)


@task(name="embed-kg-nodes", retries=1, retry_delay_seconds=10, cache_policy=NO_CACHE)
async def embed_nodes_task(client, database: str) -> int:
    embedding_model = get_embedding_model()
    return await embed_nodes(client, database, embedding_model)


@task(
    name="create-reverse-edges", retries=1, retry_delay_seconds=5, cache_policy=NO_CACHE
)
async def create_reverse_edges_task(client, database: str) -> int:
    return await create_reverse_edges(client, database)


@task(name="ensure-kg-indexes", retries=1, retry_delay_seconds=5, cache_policy=NO_CACHE)
async def ensure_indexes_task(client, database: str) -> None:
    await ensure_indexes(client, database)


@flow(name="memory-indexing-etl", log_prints=True)
async def memory_indexing() -> None:
    """Create reverse edges, embed nodes, and ensure search indexes."""

    client = await init_mongodb(
        settings.mongo.mongo_uri.get_secret_value(),
        settings.mongo.mongo_initdb_database,
    )
    database = settings.mongo.mongo_initdb_database

    await create_reverse_edges_task(client, database)
    count = await embed_nodes_task(client, database)
    await ensure_indexes_task(client, database)

    logger.info("Indexing pipeline finished. Embedded %d nodes.", count)
