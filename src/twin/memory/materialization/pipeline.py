"""
Prefect tasks and flows for knowledge graph materialization.
"""

import logging

from prefect import flow, task
from prefect.cache_policies import NO_CACHE

from twin.config.settings import settings
from twin.db import init_mongodb
from twin.memory.materialization.core import embed_nodes, ensure_indexes, materialize
from twin.models.get_model import get_embedding_model

logger = logging.getLogger(__name__)


@task(name="materialize-kg", retries=1, retry_delay_seconds=5, cache_policy=NO_CACHE)
async def materialize_task(client, database: str) -> None:
    await materialize(client, database)


@task(name="embed-kg-nodes", retries=1, retry_delay_seconds=10, cache_policy=NO_CACHE)
async def embed_nodes_task(client, database: str) -> int:
    embedding_model = get_embedding_model()
    return await embed_nodes(client, database, embedding_model)


@task(name="ensure-kg-indexes", retries=1, retry_delay_seconds=5, cache_policy=NO_CACHE)
async def ensure_indexes_task(client, database: str) -> None:
    await ensure_indexes(client, database)


@flow(name="memory-materialization-etl", log_prints=True)
async def memory_materialization() -> None:
    """Rebuild the materialized knowledge_graph from logs and embed nodes."""

    client = await init_mongodb(
        settings.mongo.mongo_uri.get_secret_value(),
        settings.mongo.mongo_initdb_database,
    )
    database = settings.mongo.mongo_initdb_database

    await materialize_task(client, database)
    count = await embed_nodes_task(client, database)
    await ensure_indexes_task(client, database)

    logger.info("Materialization pipeline finished. Embedded %d nodes.", count)
