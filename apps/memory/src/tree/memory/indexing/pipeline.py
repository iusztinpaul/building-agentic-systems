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
from tree.observability import (
    TAGS_INGESTION_BATCH,
    configure_opik,
    flush_opik,
    get_distributed_trace_headers,
    pipeline_metadata,
    span,
)

logger = logging.getLogger(__name__)

# Ingestion telemetry tags for the indexing tasks. Each task opens its span via
# :func:`tree.observability.span` attached to the flow's trace through the
# ``opik_trace_headers`` parameter (the orchestrator forwards its trace headers
# so indexing nests under the same trace as the extraction it follows). Nested
# embedding spans (Voyage/Modal) attach to the task span via contextvars.
#
# Four-tag family: offline Prefect ingestion = ``["ingestion", "batch"]``. The
# former ``"memory-indexing"`` pipeline-name tag is now span metadata.
_INDEXING_TAGS = TAGS_INGESTION_BATCH
_INDEXING_METADATA = pipeline_metadata("indexing")


async def _embed_nodes(
    client,
    database: str,
    user_id: PydanticObjectId,
    opik_trace_headers: dict[str, str] | None = None,
) -> int:
    with span(
        "embed_nodes_task", tags=_INDEXING_TAGS, trace_headers=opik_trace_headers
    ):
        embedding_model = get_embedding_model()
        return await embed_nodes(client, database, embedding_model, user_id)


async def _ensure_indexes(
    client,
    database: str,
    user_id: PydanticObjectId,
    opik_trace_headers: dict[str, str] | None = None,
) -> None:
    with span(
        "ensure_indexes_task", tags=_INDEXING_TAGS, trace_headers=opik_trace_headers
    ):
        embedding_model = get_embedding_model()
        await ensure_indexes(
            client, database, embedding_model=embedding_model, user_id=user_id
        )


embed_nodes_task = task(
    _embed_nodes,
    name="embed-kg-nodes",
    retries=1,
    retry_delay_seconds=10,
    cache_policy=NO_CACHE,
)

ensure_indexes_task = task(
    _ensure_indexes,
    name="ensure-kg-indexes",
    retries=1,
    retry_delay_seconds=5,
    cache_policy=NO_CACHE,
)


@flow(name="memory-indexing-etl", log_prints=True)
async def memory_indexing(
    user_id: PydanticObjectId,
    opik_trace_headers: dict[str, str] | None = None,
) -> None:
    """Embed nodes and ensure search indexes for ``user_id``.

    ``user_id`` is required and threaded through both tasks. ``embed_nodes``
    only processes nodes belonging to the run's tenant; ``ensure_indexes``
    re-asserts the global compound indexes whose leading key is
    ``user_id``.

    Observability: configures Opik at entry (subprocess-safe) and owns ONE
    trace. ``opik_trace_headers`` is forwarded by the extraction orchestrator so
    the trailing indexing run nests under the SAME trace as the extraction; when
    triggered standalone it is ``None`` and indexing starts its own trace. Both
    tasks receive the run's distributed-trace headers so their spans nest under
    this trace.
    """

    configure_opik()
    try:
        with span(
            "memory-indexing-etl",
            tags=_INDEXING_TAGS,
            trace_headers=opik_trace_headers,
            metadata=_INDEXING_METADATA,
        ):
            client = await init_mongodb(
                settings.mongo.mongo_uri.get_secret_value(),
                settings.mongo.mongo_initdb_database,
            )
            database = settings.mongo.mongo_initdb_database

            # Headers for THIS run's trace (the indexing root span), passed to
            # each task so its span nests here.
            headers = get_distributed_trace_headers()

            count = await embed_nodes_task(
                client, database, user_id, opik_trace_headers=headers
            )
            await ensure_indexes_task(
                client, database, user_id, opik_trace_headers=headers
            )

            # Boot-time gate: assert the live mongot vector index agrees with
            # ``app_config.models.search_embedding.dimensions``. Runs AFTER
            # ``ensure_indexes`` so a freshly bootstrapped index passes;
            # mismatch → hard-fail before the next pipeline run silently writes
            # vectors of the wrong dimension.
            await assert_settings_match_live_vector_index(client, database)

            logger.info(
                "Indexing pipeline finished for user_id=%s. Embedded %d nodes.",
                user_id,
                count,
            )
    finally:
        # Flush batched Opik telemetry (fail-open; no-op without OPIK_API_KEY).
        flush_opik()
