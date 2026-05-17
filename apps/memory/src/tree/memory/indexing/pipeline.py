"""
Prefect tasks and flows for knowledge graph indexing.

Post-extraction pipeline: embeddings, search indexes. Every flow takes
``user_id`` as a required, non-Optional parameter so tenant scoping is
enforced at the Prefect-deployment boundary (the flow refuses to run
without a value).
"""

import logging

from beanie import PydanticObjectId
from prefect import flow, task
from prefect.cache_policies import NO_CACHE

from tree.config.settings import settings
from tree.db import init_mongodb
from tree.memory.indexing.core import (
    assert_settings_match_live_vector_index,
    embed_nodes,
    ensure_indexes,
)
from tree.models.get_model import get_embedding_model

logger = logging.getLogger(__name__)


@task(name="embed-kg-nodes", retries=1, retry_delay_seconds=10, cache_policy=NO_CACHE)
async def embed_nodes_task(client, database: str, user_id: PydanticObjectId) -> int:
    embedding_model = get_embedding_model()
    return await embed_nodes(client, database, embedding_model, user_id)


@task(name="ensure-kg-indexes", retries=1, retry_delay_seconds=5, cache_policy=NO_CACHE)
async def ensure_indexes_task(client, database: str, user_id: PydanticObjectId) -> None:
    embedding_model = get_embedding_model()
    await ensure_indexes(
        client, database, embedding_model=embedding_model, user_id=user_id
    )


@flow(name="memory-indexing-etl", log_prints=True)
async def memory_indexing(user_id: PydanticObjectId) -> None:
    """Embed nodes and ensure search indexes for ``user_id``.

    ``user_id`` is required and threaded through both tasks. ``embed_nodes``
    only processes nodes belonging to the run's tenant; ``ensure_indexes``
    re-asserts the global compound indexes whose leading key is
    ``user_id``.
    """

    client = await init_mongodb(
        settings.mongo.mongo_uri.get_secret_value(),
        settings.mongo.mongo_initdb_database,
    )
    database = settings.mongo.mongo_initdb_database

    count = await embed_nodes_task(client, database, user_id)
    await ensure_indexes_task(client, database, user_id)

    # #016 boot-time gate: assert the live mongot vector index agrees
    # with ``settings.embedding_dim``. Runs AFTER ``ensure_indexes`` so a
    # freshly bootstrapped index passes; mismatch → hard-fail before the
    # next pipeline run silently writes vectors of the wrong dimension.
    await assert_settings_match_live_vector_index(client, database)

    logger.info(
        "Indexing pipeline finished for user_id=%s. Embedded %d nodes.",
        user_id,
        count,
    )
