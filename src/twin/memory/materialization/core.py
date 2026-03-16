"""
Materialization pipeline: rebuild the knowledge_graph collection from logs.

1. Aggregate knowledge_graph_log into deduplicated nodes and edges.
2. Write atomically to knowledge_graph via $out.
3. Compute embeddings for nodes that lack them.
4. Ensure text and vector search indexes exist.
"""

import asyncio
import logging
from typing import Any

from pymongo import AsyncMongoClient
from pymongo.errors import BulkWriteError

from twin.config.app_config import app_config
from twin.entities.knowledge_graph import NodeType
from twin.models.base import BaseEmbeddingModel

logger = logging.getLogger(__name__)

# Collection names (match Beanie Settings.name).
_LOG_COLLECTION = "knowledge_graph_log"
_KG_COLLECTION = "knowledge_graph"

# Index names (shared with query module).
_TEXT_INDEX_NAME = "text_index"
_VECTOR_INDEX_NAME = "vector_index"


# ---------------------------------------------------------------------------
# 1. Aggregation pipeline
# ---------------------------------------------------------------------------


def build_materialization_pipeline() -> list[dict[str, Any]]:
    """Return the MongoDB aggregation pipeline that materializes the KG.

    Runs against the knowledge_graph_log collection.
    Produces deduplicated nodes and edges, then writes via $out.
    """

    # --- Nodes: group by (name, type), composite _id = "type:name" ---
    node_branch: list[dict[str, Any]] = [
        {"$match": {"kind": "node"}},
        {
            "$group": {
                "_id": {"name": "$name", "type": "$type"},
                "properties": {"$mergeObjects": "$properties"},
                "sources": {"$addToSet": "$source_document_id"},
                "created_at": {"$min": "$created_at"},
                "updated_at": {"$max": "$created_at"},
            }
        },
        {
            "$project": {
                "_id": {"$concat": ["$_id.type", ":", "$_id.name"]},
                "kind": {"$literal": "node"},
                "name": "$_id.name",
                "type": "$_id.type",
                "properties": 1,
                "embedding": {"$literal": []},
                "sources": 1,
                "created_at": 1,
                "updated_at": 1,
            }
        },
    ]

    # --- Edges: group by (source, target, type) with types in key ---
    edge_branch: list[dict[str, Any]] = [
        {"$match": {"kind": "edge"}},
        {
            "$group": {
                "_id": {
                    "source_node_id": "$source_node_id",
                    "source_type": "$source_type",
                    "target_node_id": "$target_node_id",
                    "target_type": "$target_type",
                    "type": "$type",
                },
                "properties": {"$mergeObjects": "$properties"},
                "sources": {"$addToSet": "$source_document_id"},
                "created_at": {"$min": "$created_at"},
                "updated_at": {"$max": "$created_at"},
            }
        },
        {
            "$project": {
                "_id": {
                    "source_node_id": {
                        "$concat": ["$_id.source_type", ":", "$_id.source_node_id"]
                    },
                    "target_node_id": {
                        "$concat": ["$_id.target_type", ":", "$_id.target_node_id"]
                    },
                    "type": "$_id.type",
                },
                "kind": {"$literal": "edge"},
                "type": "$_id.type",
                "source_node_id": {
                    "$concat": ["$_id.source_type", ":", "$_id.source_node_id"]
                },
                "source_type": "$_id.source_type",
                "target_node_id": {
                    "$concat": ["$_id.target_type", ":", "$_id.target_node_id"]
                },
                "target_type": "$_id.target_type",
                "properties": 1,
                "sources": 1,
                "created_at": 1,
                "updated_at": 1,
            }
        },
    ]

    # TODO: Switch from $out to $merge to preserve indexes at scale.
    # $out drops and recreates the collection (losing all indexes), which
    # forces a full index rebuild after every materialization. With millions
    # of documents this becomes costly. $merge upserts in-place and preserves
    # indexes, but requires a separate cleanup step to remove stale documents.
    pipeline: list[dict[str, Any]] = [
        *node_branch,
        {"$unionWith": {"coll": _LOG_COLLECTION, "pipeline": edge_branch}},
        {"$out": _KG_COLLECTION},
    ]

    return pipeline


# ---------------------------------------------------------------------------
# 2. Run materialization
# ---------------------------------------------------------------------------


async def materialize(client: AsyncMongoClient, database: str) -> None:
    """Execute the materialization aggregation pipeline."""

    db = client[database]
    pipeline = build_materialization_pipeline()

    logger.info(
        "Starting KG materialization from %s → %s", _LOG_COLLECTION, _KG_COLLECTION
    )

    # $out writes directly; we just need to drain the cursor.
    cursor = await db[_LOG_COLLECTION].aggregate(pipeline)
    async for _ in cursor:
        pass

    node_count = await db[_KG_COLLECTION].count_documents({"kind": "node"})
    edge_count = await db[_KG_COLLECTION].count_documents({"kind": "edge"})
    logger.info(
        "Materialization complete: %d nodes, %d edges in %s",
        node_count,
        edge_count,
        _KG_COLLECTION,
    )


# ---------------------------------------------------------------------------
# 3. Embed nodes
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

    from pymongo import UpdateOne

    ops = [
        UpdateOne({"_id": doc["_id"]}, {"$set": {"embedding": vector}})
        for doc, vector in zip(batch, vectors)
    ]
    if ops:
        await collection.bulk_write(ops)

    return len(ops)


# ---------------------------------------------------------------------------
# 4. Reverse edges for bidirectional traversal
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

    For each matching edge, inserts a new edge with swapped source/target
    and direction='reverse'. This enables $graphLookup to chain through
    person↔document connections in both directions.

    Returns the number of reverse edges created.
    """

    db = client[database]
    collection = db[_KG_COLLECTION]

    # Build $or filter for all bidirectional pairs.
    pair_filters = [
        {"source_type": src, "target_type": tgt} for src, tgt in _BIDIRECTIONAL_PAIRS
    ]
    query = {"kind": "edge", "$or": pair_filters}

    reverse_docs: list[dict[str, Any]] = []
    async for edge in collection.find(query):
        reverse_docs.append(
            {
                "_id": {
                    "source_node_id": edge["target_node_id"],
                    "target_node_id": edge["source_node_id"],
                    "type": edge["type"],
                },
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
            }
        )

    inserted = 0
    if reverse_docs:
        try:
            result = await collection.insert_many(reverse_docs, ordered=False)
            inserted = len(result.inserted_ids)
        except BulkWriteError as exc:
            inserted = exc.details.get("nInserted", 0)
            logger.info(
                "Skipped %d duplicate reverse edges",
                len(reverse_docs) - inserted,
            )

    logger.info("Created %d reverse edges for bidirectional traversal", inserted)
    return inserted


# ---------------------------------------------------------------------------
# 5. Ensure search indexes
# ---------------------------------------------------------------------------


async def ensure_indexes(client: AsyncMongoClient, database: str) -> None:
    """Create text and vector search indexes on the knowledge_graph collection.

    Safe to call repeatedly — skips indexes that already exist.
    Must be called after every materialization since $out drops the collection.
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
