"""
Query module for the unified memory.

Two-step retrieval:
1. search_nodes  — find entry-point nodes via text + vector search (RRF fusion).
2. expand_graph  — walk edges up to N hops from the seed nodes.
3. query_memory  — end-to-end orchestrator combining both steps.
"""

import logging
from typing import Any

from pymongo import AsyncMongoClient

from twin.config.app_config import app_config
from twin.memory.types import QueryResult
from twin.models.base import BaseEmbeddingModel

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
    *,
    top_k: int | None = None,
) -> list[dict[str, Any]]:
    """Find entry-point nodes via combined text and vector search.

    Uses reciprocal rank fusion (RRF) to merge results from both methods.
    """

    top_k = top_k if top_k is not None else app_config.query.top_k
    rrf_k = app_config.query.rrf_k
    db = client[database]
    collection = db[_KG_COLLECTION]

    # --- Vector search ---
    vector_results = await _vector_search(
        collection, query, embedding_model, limit=top_k
    )

    # --- Text search ---
    text_results = await _text_search(collection, query, limit=top_k)

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
                "filter": {"kind": "node"},
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
    limit: int,
) -> list[dict[str, Any]]:
    """Run $text query on the knowledge_graph collection.

    Uses a standard MongoDB text index (not Atlas Search).
    """

    pipeline = [
        {"$match": {"kind": "node", "$text": {"$search": query}}},
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
    *,
    max_hops: int | None = None,
) -> QueryResult:
    """Starting from seed node _ids, traverse edges up to max_hops.

    Strategy:
    - Query all edges where source_node_id or target_node_id is in the
      current frontier, then expand the frontier with newly discovered nodes.
    - Repeat for max_hops iterations.
    - Finally hydrate all discovered node documents.
    """

    max_hops = max_hops if max_hops is not None else app_config.query.max_hops
    db = client[database]
    collection = db[_KG_COLLECTION]

    visited_node_ids: set[Any] = set(node_ids)
    frontier: set[Any] = set(node_ids)
    all_edges: list[dict[str, Any]] = []
    seen_edge_ids: set[Any] = set()

    for _hop in range(max_hops):
        if not frontier:
            break

        frontier_list = list(frontier)

        # Find edges connected to the frontier (either direction).
        edge_filter = {
            "kind": "edge",
            "$or": [
                {"source_node_id": {"$in": frontier_list}},
                {"target_node_id": {"$in": frontier_list}},
            ],
        }

        new_frontier: set[Any] = set()
        async for edge in collection.find(edge_filter):
            raw_id = edge["_id"]
            edge_id = (
                tuple(sorted(raw_id.items()))
                if isinstance(raw_id, dict)
                else raw_id
            )
            if edge_id in seen_edge_ids:
                continue
            seen_edge_ids.add(edge_id)
            all_edges.append(edge)

            # Discover new node ids.
            src = edge["source_node_id"]
            tgt = edge["target_node_id"]
            if src not in visited_node_ids:
                new_frontier.add(src)
                visited_node_ids.add(src)
            if tgt not in visited_node_ids:
                new_frontier.add(tgt)
                visited_node_ids.add(tgt)

        frontier = new_frontier

    # Hydrate all discovered nodes.
    all_nodes: list[dict[str, Any]] = []
    if visited_node_ids:
        async for node in collection.find(
            {"kind": "node", "_id": {"$in": list(visited_node_ids)}}
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
    *,
    top_k: int | None = None,
    max_hops: int | None = None,
) -> QueryResult:
    """Search for relevant nodes, then expand the graph around them."""

    seed_nodes = await search_nodes(
        client, database, query, embedding_model, top_k=top_k
    )

    if not seed_nodes:
        logger.info("No seed nodes found for query: %s", query[:100])
        return QueryResult()

    seed_ids = [node["_id"] for node in seed_nodes]

    return await expand_graph(client, database, seed_ids, max_hops=max_hops)
