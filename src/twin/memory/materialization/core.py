"""
Materialization pipeline: rebuild the knowledge_graph collection from logs.

1. Aggregate knowledge_graph_log into deduplicated nodes and edges.
2. Write atomically to knowledge_graph via $out.
3. Compute embeddings for nodes that lack them.
"""

import logging
from typing import Any

from pymongo import AsyncMongoClient

from twin.config.app_config import app_config
from twin.models.base import BaseEmbeddingModel

logger = logging.getLogger(__name__)

# Collection names (match Beanie Settings.name).
_LOG_COLLECTION = "knowledge_graph_log"
_KG_COLLECTION = "knowledge_graph"


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

    # Start with nodes, union edges, then $out.
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
