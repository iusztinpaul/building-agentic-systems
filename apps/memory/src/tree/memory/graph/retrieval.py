"""GraphRAG retrieval: hybrid seeds -> parent resolution -> graph expansion.

The graph half of the pre-ADR-006 ``tree.memory.graph.core``. Two steps, every
one scoped to the run's ``user_id``:

1. :func:`~tree.memory.rag.search.hybrid_search` with ``node_filter={}`` — in
   ``graphrag`` a seed may be a **Child chunk**, an entity or a ``document``
   row; parents are excluded by the search layer itself.
2. :func:`expand_graph` — walk edges up to N hops from the seed ids.

:func:`query_memory` composes them, and the composition is where ADR-006
decision 2 shows up in the graph: child seeds are resolved to their **Parent
chunk**s (via :func:`~tree.memory.rag.retrieval.group_children_by_parent`, the
same helper **Parent-document retrieval** uses) before expansion runs, so
``QueryResult.nodes`` carries parents — never the 256-token children that
matched. Expansion therefore starts from ``distinct parent ids ∪ non-chunk seed
ids``, and the ``part_of`` / ``next`` edges around a parent are what the caller
gets to read.
"""

import logging
from typing import Any

from beanie import PydanticObjectId
from pymongo import AsyncMongoClient

from tree.config.app_config import app_config
from tree.entities.memory import MEMORY_COLLECTION
from tree.memory.rag.retrieval import group_children_by_parent
from tree.memory.rag.search import hybrid_search
from tree.memory.rag.types import ScoredHit
from tree.memory.types import QueryResult
from tree.models.base import BaseEmbeddingModel
from tree.observability import track

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 1. Expand graph (multi-hop traversal)
# ---------------------------------------------------------------------------


@track(name="expand_graph")
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
    collection = db[MEMORY_COLLECTION]

    if not node_ids or max_hops == 0:
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
                "from": MEMORY_COLLECTION,
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
                "from": MEMORY_COLLECTION,
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
# 2. End-to-end query
# ---------------------------------------------------------------------------


@track(name="query_memory")
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
    """Search for seed nodes, resolve chunk seeds to parents, expand the graph.

    Every step is scoped to ``user_id``. A single query never returns rows from
    another tenant.
    """

    top_k = top_k if top_k is not None else app_config.query.top_k
    collection = client[database][MEMORY_COLLECTION]

    hits = await hybrid_search(
        collection, query, embedding_model, user_id, limit=top_k, node_filter={}
    )
    if not hits:
        logger.info("No seed nodes found for query: %s", query[:100])
        return QueryResult()

    seed_ids = _seed_ids(hits)

    return await expand_graph(client, database, seed_ids, user_id, max_hops=max_hops)


def _seed_ids(hits: list[ScoredHit]) -> list[Any]:
    """``distinct parent ids ∪ non-chunk seed ids``, parents first.

    A child seed is replaced by its parent, never added alongside it: the parent
    is the retrieval unit (ADR-006 decision 2) and expanding from both would
    double the ``part_of`` edges around every hit for no extra information.
    """

    children: list[ScoredHit] = []
    other_ids: list[Any] = []
    for hit in hits:
        if hit.doc.get("type") == "chunk" and hit.doc.get("subtype") == "child":
            children.append(hit)
        else:
            other_ids.append(hit.doc["_id"])

    seed_ids = list(group_children_by_parent(children))
    seed_ids.extend(node_id for node_id in other_ids if node_id not in seed_ids)
    return seed_ids


async def fetch_full_graph(
    client: AsyncMongoClient,
    database: str,
    user_id: PydanticObjectId,
) -> QueryResult:
    """Load the ENTIRE materialized knowledge graph for ``user_id``.

    Unlike :func:`query_memory` (semantic + text search expanded around seed
    nodes), this returns every node and edge the user owns — the "show me
    everything" view used when no search query is given. Scoped to ``user_id``,
    so it never returns another tenant's rows. graphrag-only: in ``rag`` mode the
    collection holds no edges, so the CLI refuses this view instead of rendering
    a graph of isolated dots.
    """

    collection = client[database][MEMORY_COLLECTION]
    nodes = [doc async for doc in collection.find({"user_id": user_id, "kind": "node"})]
    edges = [doc async for doc in collection.find({"user_id": user_id, "kind": "edge"})]
    return QueryResult(nodes=nodes, edges=edges)
