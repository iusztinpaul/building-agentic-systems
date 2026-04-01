"""
Indexing pipeline for the knowledge graph.

Post-extraction steps that prepare the graph for querying:
1. Compute embeddings for nodes that lack them.
2. Ensure text and vector search indexes exist.
"""

import asyncio
import logging
from typing import Any

from pymongo import AsyncMongoClient, UpdateOne

from twin.config.app_config import app_config
from twin.models.base import BaseEmbeddingModel

logger = logging.getLogger(__name__)

_KG_COLLECTION = "knowledge_graph"

# Index names (shared with query module).
_TEXT_INDEX_NAME = "text_index"
_VECTOR_INDEX_NAME = "vector_index"


# ---------------------------------------------------------------------------
# 1. Embed nodes
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
