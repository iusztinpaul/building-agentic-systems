"""
Indexing pipeline for the knowledge graph.

Post-extraction steps that prepare the graph for querying:
1. Create reverse edges for bidirectional $graphLookup traversal.
2. Compute embeddings for nodes that lack them.
3. Ensure text and vector search indexes exist.
"""

import asyncio
import logging
from typing import Any

from pymongo import AsyncMongoClient, UpdateOne
from pymongo.errors import BulkWriteError

from twin.config.app_config import app_config
from twin.entities.knowledge_graph import NodeType, build_edge_id
from twin.models.base import BaseEmbeddingModel

logger = logging.getLogger(__name__)

_KG_COLLECTION = "knowledge_graph"

# Index names (shared with query module).
_TEXT_INDEX_NAME = "text_index"
_VECTOR_INDEX_NAME = "vector_index"


# ---------------------------------------------------------------------------
# 1. Reverse edges for bidirectional traversal
# ---------------------------------------------------------------------------

# Node type pairs that get reverse edges so $graphLookup can traverse
# in both directions (person ↔ document).
_BIDIRECTIONAL_PAIRS: set[tuple[str, str]] = {
    (NodeType.PERSON, NodeType.DOCUMENT),
    (NodeType.DOCUMENT, NodeType.PERSON),
    (NodeType.PERSON, NodeType.PERSON),
    (NodeType.DOCUMENT, NodeType.DOCUMENT),
}


async def create_reverse_edges(client: AsyncMongoClient, database: str) -> int:
    """Create reverse copies of edges between person and document nodes.

    For each matching edge, upserts a new edge with swapped source/target
    and direction='reverse'. This enables $graphLookup to chain through
    person↔document connections in both directions.

    Returns the number of reverse edges upserted.
    """

    db = client[database]
    collection = db[_KG_COLLECTION]

    # Build $or filter for all bidirectional pairs.
    pair_filters = [
        {"source_type": src, "target_type": tgt} for src, tgt in _BIDIRECTIONAL_PAIRS
    ]
    query = {"kind": "edge", "direction": {"$exists": False}, "$or": pair_filters}

    ops: list[UpdateOne] = []
    async for edge in collection.find(query):
        reverse_id = build_edge_id(
            edge["target_node_id"], edge["type"], edge["source_node_id"]
        )
        ops.append(
            UpdateOne(
                {"_id": reverse_id},
                {
                    "$set": {
                        "kind": "edge",
                        "type": edge["type"],
                        "source_node_id": edge["target_node_id"],
                        "source_type": edge["target_type"],
                        "target_node_id": edge["source_node_id"],
                        "target_type": edge["source_type"],
                        "properties": edge.get("properties", {}),
                        "sources": edge.get("sources", []),
                        "created_at": edge["created_at"],
                        "updated_at": edge["updated_at"],
                        "direction": "reverse",
                    },
                },
                upsert=True,
            )
        )

    upserted = 0
    if ops:
        try:
            result = await collection.bulk_write(ops, ordered=False)
            upserted = result.upserted_count + result.modified_count
        except BulkWriteError as exc:
            upserted = exc.details.get("nUpserted", 0) + exc.details.get("nModified", 0)
            logger.info("Bulk write completed with some errors for reverse edges")

    logger.info("Upserted %d reverse edges for bidirectional traversal", upserted)
    return upserted


# ---------------------------------------------------------------------------
# 2. Embed nodes
# ---------------------------------------------------------------------------


def _node_to_text(node: dict[str, Any]) -> str:
    """Build an embeddable text representation from a node document."""

    parts = [f"{node.get('type', '')}: {node.get('_id', '')}"]
    props = node.get("properties", {})
    for key, value in props.items():
        if value and key != "content":
            parts.append(f"{key}: {value}")
    # Include content last (may be long).
    if props.get("content"):
        parts.append(str(props["content"]))
    return "\n".join(parts)


async def embed_nodes(
    client: AsyncMongoClient,
    database: str,
    embedding_model: BaseEmbeddingModel,
) -> int:
    """Compute embeddings for all nodes that have an empty embedding vector.

    Returns the number of nodes embedded.
    """

    db = client[database]
    collection = db[_KG_COLLECTION]
    batch_size = app_config.query.embedding_batch_size

    # Fetch all nodes without embeddings.
    docs = await collection.find(
        {"kind": "node", "embedding": {"$in": [[], None]}},
    ).to_list()

    embedded_count = 0

    for i in range(0, len(docs), batch_size):
        batch = docs[i : i + batch_size]
        embedded_count += await _embed_batch(collection, batch, embedding_model)

    logger.info("Embedded %d nodes in %s", embedded_count, _KG_COLLECTION)
    return embedded_count


async def _embed_batch(
    collection: Any,
    batch: list[dict[str, Any]],
    embedding_model: BaseEmbeddingModel,
) -> int:
    """Embed a batch of node documents and write vectors back."""

    texts = [_node_to_text(doc) for doc in batch]
    vectors = await embedding_model.embed(texts)

    ops = [
        UpdateOne({"_id": doc["_id"]}, {"$set": {"embedding": vector}})
        for doc, vector in zip(batch, vectors)
    ]
    if ops:
        await collection.bulk_write(ops)

    return len(ops)


# ---------------------------------------------------------------------------
# 3. Ensure search indexes
# ---------------------------------------------------------------------------


async def ensure_indexes(client: AsyncMongoClient, database: str) -> None:
    """Create text and vector search indexes on the knowledge_graph collection.

    Safe to call repeatedly — skips indexes that already exist.
    """

    db = client[database]
    collection = db[_KG_COLLECTION]

    # --- Text index (for $text queries) ---
    await collection.create_index(
        [
            ("name", "text"),
            ("properties.content", "text"),
            ("properties.aliases", "text"),
        ],
        name=_TEXT_INDEX_NAME,
    )
    logger.info("Text index '%s' ensured on %s", _TEXT_INDEX_NAME, _KG_COLLECTION)

    # --- Vector search index (for $vectorSearch) ---
    cursor = await collection.list_search_indexes()
    existing = [idx["name"] async for idx in cursor]
    if _VECTOR_INDEX_NAME in existing:
        logger.info("Vector search index '%s' already exists", _VECTOR_INDEX_NAME)
        return

    dimensions = app_config.models.embedding.dimensions
    await collection.create_search_index(
        model={
            "name": _VECTOR_INDEX_NAME,
            "type": "vectorSearch",
            "definition": {
                "fields": [
                    {
                        "type": "vector",
                        "path": "embedding",
                        "numDimensions": dimensions,
                        "similarity": "cosine",
                    }
                ]
            },
        }
    )

    # Wait for mongot to sync the index.
    logger.info("Waiting for vector search index to be ready...")
    for _ in range(30):
        cursor = await collection.list_search_indexes(_VECTOR_INDEX_NAME)
        results = await cursor.to_list()
        if results:
            await asyncio.sleep(3)
            logger.info("Vector search index '%s' ready", _VECTOR_INDEX_NAME)
            return
        await asyncio.sleep(2)

    logger.warning(
        "Vector search index '%s' did not appear in time", _VECTOR_INDEX_NAME
    )
