"""
Query module for the unified memory.

Two-step retrieval, every step scoped to the run's ``user_id``:

1. search_nodes  — find entry-point nodes via text + vector search (RRF fusion).
2. expand_graph  — walk edges up to N hops from the seed nodes.
3. query_memory  — end-to-end orchestrator combining both steps.

Every public entry point takes ``user_id`` as a required, non-Optional
parameter. The internal helpers thread the value through ``$vectorSearch``
``filter`` clauses and ``$match`` stages so cross-tenant rows never appear
in a single response.
"""

import logging
from typing import Any

from beanie import PydanticObjectId
from pymongo import AsyncMongoClient

from tree.config.app_config import app_config
from tree.memory.types import QueryResult
from tree.models.base import BaseEmbeddingModel

logger = logging.getLogger(__name__)

_KG_COLLECTION = "knowledge_graph"


# ---------------------------------------------------------------------------
# 1. Search nodes (text + vector → RRF)
# ---------------------------------------------------------------------------


async def search_nodes(
    client: AsyncMongoClient,
    database: str,
    query: str,
    embedding_model: BaseEmbeddingModel,
    user_id: PydanticObjectId,
    *,
    top_k: int | None = None,
) -> list[dict[str, Any]]:
    """Find entry-point nodes via combined text and vector search.

    Uses reciprocal rank fusion (RRF) to merge results from both methods.
    ``user_id`` is pinned into both the ``$vectorSearch`` filter and the
    text ``$match`` so cross-tenant rows are pruned server-side.
    """

    top_k = top_k if top_k is not None else app_config.query.top_k
    rrf_k = app_config.query.rrf_k
    db = client[database]
    collection = db[_KG_COLLECTION]

    # --- Vector search ---
    vector_results = await _vector_search(
        collection, query, embedding_model, user_id=user_id, limit=top_k
    )

    # --- Text search ---
    text_results = await _text_search(collection, query, user_id=user_id, limit=top_k)

    # --- RRF fusion ---
    fused = _rrf_fuse(vector_results, text_results, k=rrf_k)

    # Sort by fused score descending, take top_k.
    ranked = sorted(fused.items(), key=lambda x: x[1]["score"], reverse=True)[:top_k]

    return [item["doc"] for _, item in ranked]


async def _vector_search(
    collection: Any,
    query: str,
    embedding_model: BaseEmbeddingModel,
    *,
    user_id: PydanticObjectId,
    limit: int,
) -> list[dict[str, Any]]:
    """Run $vectorSearch on the knowledge_graph collection."""

    query_vector = (await embedding_model.embed([query]))[0]

    pipeline = [
        {
            "$vectorSearch": {
                "index": "vector_index",
                "path": "embedding",
                "queryVector": query_vector,
                "numCandidates": limit * 10,
                "limit": limit,
                "filter": {"user_id": user_id, "kind": "node"},
            }
        },
        {"$addFields": {"_search_score": {"$meta": "vectorSearchScore"}}},
    ]

    try:
        cursor = await collection.aggregate(pipeline)
        results = []
        async for doc in cursor:
            results.append(doc)
        return results
    except Exception:
        logger.warning("Vector search unavailable, falling back to text-only")
        return []


async def _text_search(
    collection: Any,
    query: str,
    *,
    user_id: PydanticObjectId,
    limit: int,
) -> list[dict[str, Any]]:
    """Run $text query on the knowledge_graph collection.

    Uses a standard MongoDB text index (not Atlas Search). ``user_id`` is
    folded into the ``$match`` so cross-tenant hits never leak.
    """

    pipeline = [
        {
            "$match": {
                "user_id": user_id,
                "kind": "node",
                "$text": {"$search": query},
            }
        },
        {"$addFields": {"_search_score": {"$meta": "textScore"}}},
        {"$sort": {"_search_score": -1}},
        {"$limit": limit},
    ]

    try:
        cursor = await collection.aggregate(pipeline)
        results = []
        async for doc in cursor:
            results.append(doc)
        return results
    except Exception:
        logger.warning("Text search unavailable, falling back to vector-only")
        return []


def _rrf_fuse(
    vector_results: list[dict[str, Any]],
    text_results: list[dict[str, Any]],
    *,
    k: int = 60,
) -> dict[Any, dict[str, Any]]:
    """Reciprocal Rank Fusion: score = sum(1 / (k + rank)) across both lists.

    Returns {doc_id: {"doc": document, "score": float}}.
    """

    fused: dict[Any, dict[str, Any]] = {}

    for rank, doc in enumerate(vector_results):
        doc_id = doc["_id"]
        if doc_id not in fused:
            fused[doc_id] = {"doc": doc, "score": 0.0}
        fused[doc_id]["score"] += 1.0 / (k + rank + 1)

    for rank, doc in enumerate(text_results):
        doc_id = doc["_id"]
        if doc_id not in fused:
            fused[doc_id] = {"doc": doc, "score": 0.0}
        fused[doc_id]["score"] += 1.0 / (k + rank + 1)

    return fused


# ---------------------------------------------------------------------------
# 2. Expand graph (multi-hop traversal)
# ---------------------------------------------------------------------------


async def expand_graph(
    client: AsyncMongoClient,
    database: str,
    node_ids: list[Any],
    user_id: PydanticObjectId,
    *,
    max_hops: int | None = None,
) -> QueryResult:
    """Starting from seed node _ids, traverse edges up to max_hops.

    Uses two $graphLookup passes (outgoing + incoming) to do bidirectional
    traversal, then hydrates all discovered node documents. Every match
    stage carries ``user_id`` and the $graphLookup ``restrictSearchWithMatch``
    filters cross-tenant edges out of the traversal.
    """

    max_hops = max_hops if max_hops is not None else app_config.query.max_hops
    db = client[database]
    collection = db[_KG_COLLECTION]

    if not node_ids or max_hops == 0:
        # No traversal — just hydrate seed nodes.
        all_nodes: list[dict[str, Any]] = []
        if node_ids:
            async for node in collection.find(
                {
                    "user_id": user_id,
                    "kind": "node",
                    "_id": {"$in": list(node_ids)},
                }
            ):
                all_nodes.append(node)
        return QueryResult(nodes=all_nodes, edges=[])

    # $graphLookup maxDepth is 0-indexed: 0 = direct edges, 1 = two hops, etc.
    depth = max_hops - 1

    pipeline = [
        {"$match": {"user_id": user_id, "kind": "node", "_id": {"$in": node_ids}}},
        # Outgoing: seed._id → edge.source_node_id, follow edge.target_node_id
        {
            "$graphLookup": {
                "from": _KG_COLLECTION,
                "startWith": "$_id",
                "connectFromField": "target_node_id",
                "connectToField": "source_node_id",
                "as": "outgoing",
                "maxDepth": depth,
                "restrictSearchWithMatch": {"user_id": user_id, "kind": "edge"},
            }
        },
        # Incoming: seed._id → edge.target_node_id, follow edge.source_node_id
        {
            "$graphLookup": {
                "from": _KG_COLLECTION,
                "startWith": "$_id",
                "connectFromField": "source_node_id",
                "connectToField": "target_node_id",
                "as": "incoming",
                "maxDepth": depth,
                "restrictSearchWithMatch": {"user_id": user_id, "kind": "edge"},
            }
        },
        # Merge both directions into a single deduplicated array per seed.
        {"$project": {"edges": {"$setUnion": ["$outgoing", "$incoming"]}}},
    ]

    cursor = await collection.aggregate(pipeline)

    # Collect and deduplicate edges across all seed nodes.
    seen_edge_ids: set = set()
    all_edges: list[dict[str, Any]] = []
    node_id_set: set[Any] = set(node_ids)

    async for doc in cursor:
        for edge in doc.get("edges", []):
            edge_key = edge["_id"]
            if edge_key in seen_edge_ids:
                continue
            seen_edge_ids.add(edge_key)
            all_edges.append(edge)
            node_id_set.add(edge["source_node_id"])
            node_id_set.add(edge["target_node_id"])

    # Hydrate all discovered nodes (still scoped to user_id).
    all_nodes: list[dict[str, Any]] = []
    if node_id_set:
        async for node in collection.find(
            {
                "user_id": user_id,
                "kind": "node",
                "_id": {"$in": list(node_id_set)},
            }
        ):
            all_nodes.append(node)

    logger.info(
        "Graph expansion: %d seed(s) → %d nodes, %d edges (%d hops)",
        len(node_ids),
        len(all_nodes),
        len(all_edges),
        max_hops,
    )

    return QueryResult(nodes=all_nodes, edges=all_edges)


# ---------------------------------------------------------------------------
# 3. End-to-end query
# ---------------------------------------------------------------------------


async def query_memory(
    client: AsyncMongoClient,
    database: str,
    query: str,
    embedding_model: BaseEmbeddingModel,
    user_id: PydanticObjectId,
    *,
    top_k: int | None = None,
    max_hops: int | None = None,
) -> QueryResult:
    """Search for relevant nodes, then expand the graph around them.

    Every step is scoped to ``user_id``. A single query never returns
    rows from another tenant.
    """

    seed_nodes = await search_nodes(
        client, database, query, embedding_model, user_id, top_k=top_k
    )

    if not seed_nodes:
        logger.info("No seed nodes found for query: %s", query[:100])
        return QueryResult()

    seed_ids = [node["_id"] for node in seed_nodes]

    return await expand_graph(client, database, seed_ids, user_id, max_hops=max_hops)
