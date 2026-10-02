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
gets to read — with every chunk's ``part_of`` chain to its document always
complete (:func:`_attach_part_of_closure`, ADR-011 §8), however the last hop
reached it.
"""

import logging
from collections import Counter
from typing import Any

from beanie import PydanticObjectId
from pymongo import AsyncMongoClient

from tree.config.app_config import app_config
from tree.entities.memory import MEMORY_COLLECTION
from tree.memory.rag.retrieval import group_children_by_parent
from tree.memory.rag.search import hybrid_search
from tree.memory.rag.structure import (
    NO_EMBEDDING,
    rank_by_provenance,
    rank_recent_documents,
)
from tree.memory.rag.types import ScoredHit
from tree.memory.types import QueryResult
from tree.models.base import BaseEmbeddingModel
from tree.observability import track

logger = logging.getLogger(__name__)

# Transient top-level key on every row the ``part_of`` closure appended (like
# ``_search_score``): :func:`ranked_rows` orders by it and strips it, so it
# never reaches the model, a file or the Graph payload.
CLOSURE_ADDED = "_closure_added"


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
        all_nodes, all_edges = await _attach_part_of_closure(
            collection, user_id, all_nodes, []
        )
        return QueryResult(nodes=all_nodes, edges=all_edges)

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

    all_nodes, all_edges = await _attach_part_of_closure(
        collection, user_id, all_nodes, all_edges
    )

    logger.info(
        "Graph expansion: %d seed(s) → %d nodes, %d edges (%d hops)",
        len(node_ids),
        len(all_nodes),
        len(all_edges),
        max_hops,
    )

    return QueryResult(nodes=all_nodes, edges=all_edges)


async def _attach_part_of_closure(
    collection: Any,
    user_id: PydanticObjectId,
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Give every chunk in the result its ``part_of`` chain up to its document.

    ``$graphLookup`` stops at ``max_hops``: a chunk found on the last hop (a
    parent via ``next``, a child via ``next``) arrives without its ``part_of``
    edge. Two batched reads close that (ADR-011 §8):

    1. every ``part_of`` edge UP from a chunk in the result, or from a child's
       parent that is not in it yet (one-level lookahead, so child → parent →
       document comes in one pass), plus every edge DOWN into a parent, read
       only to count its children;
    2. the UP targets not in the result (those parents and their documents).

    Rows are APPENDED — hydrated nodes, then each UP edge whose two endpoints
    are now present — as copies marked ``CLOSURE_ADDED``, never reordered, so
    :func:`ranked_rows` can keep every hop row ahead of them. Absent children
    are never pulled in; a parent with children none of which is drawn gets a
    copy stamped ``child_count``. No chunk in the result → no read.
    """

    chunks = [row for row in nodes if row.get("type") == "chunk"]
    if not chunks:
        return nodes, edges

    node_ids = {row["_id"] for row in nodes}
    lookahead = {
        row["parent_id"]
        for row in chunks
        if row.get("subtype") == "child" and row.get("parent_id") is not None
    } - node_ids
    up_sources = {row["_id"] for row in chunks} | lookahead
    parent_ids = {
        row["_id"] for row in chunks if row.get("subtype") == "parent"
    } | lookahead
    walked = [
        edge
        async for edge in collection.find(
            {
                "user_id": user_id,
                "kind": "edge",
                "type": "part_of",
                "$or": [
                    {"source_node_id": {"$in": list(up_sources)}},
                    {"target_node_id": {"$in": list(parent_ids)}},
                ],
            }
        )
    ]
    up = [edge for edge in walked if edge["source_node_id"] in up_sources]

    missing = {edge["target_node_id"] for edge in up} - node_ids
    hydrated = [
        {**row, CLOSURE_ADDED: True}
        async for row in collection.find(
            {"user_id": user_id, "kind": "node", "_id": {"$in": list(missing)}}
        )
    ]
    node_ids |= {row["_id"] for row in hydrated}
    edge_ids = {edge["_id"] for edge in edges}
    added = [
        {**edge, CLOSURE_ADDED: True}
        for edge in up
        if edge["_id"] not in edge_ids
        and edge["source_node_id"] in node_ids
        and edge["target_node_id"] in node_ids
    ]

    children = Counter(
        edge["target_node_id"]
        for edge in walked
        if edge["target_node_id"] in parent_ids
    )
    all_nodes = [*nodes, *hydrated]
    drawn = {row.get("parent_id") for row in all_nodes if row.get("subtype") == "child"}
    stamped = 0
    for i, row in enumerate(all_nodes):
        count = children[row["_id"]]
        if row.get("subtype") == "parent" and count and row["_id"] not in drawn:
            all_nodes[i] = {**row, "child_count": count}
            stamped += 1

    logger.debug(
        "part_of closure: +%d node(s), +%d edge(s), %d parent(s) with hidden children",
        len(hydrated),
        len(added),
        stamped,
    )
    return all_nodes, [*edges, *added]


def ranked_rows(result: QueryResult) -> list[dict[str, Any]]:
    """``nodes + edges`` the way the model reads them, ranking first.

    Hop nodes, hop edges, then the ``part_of`` closure's nodes and edges
    (a stable sort on :data:`CLOSURE_ADDED`), with that marker stripped — so
    truncating to ``max_results`` keeps exactly the rows it kept before the
    closure existed (ADR-011 §8).
    """

    rows = sorted(
        [*result.nodes, *result.edges], key=lambda row: bool(row.get(CLOSURE_ADDED))
    )
    return [{k: v for k, v in row.items() if k != CLOSURE_ADDED} for row in rows]


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
    another tenant. The result's node rows carry ``doc_rank`` by search
    relevance (:func:`_rank_by_relevance`) — a viz concern the model-facing
    serializers strip.
    """

    top_k = top_k if top_k is not None else app_config.query.top_k
    collection = client[database][MEMORY_COLLECTION]

    # graphrag reads the hits and IGNORES the **Search mode**: ``QueryResult``
    # is Chapter 8's contract and stays unchanged (ADR-008 §3).
    hits = (
        await hybrid_search(
            collection, query, embedding_model, user_id, limit=top_k, node_filter={}
        )
    ).hits
    if not hits:
        logger.info("No seed nodes found for query: %s", query[:100])
        return QueryResult()

    seed_ids = _seed_ids(hits)

    result = await expand_graph(client, database, seed_ids, user_id, max_hops=max_hops)
    return _rank_by_relevance(result, hits)


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


def _rank_by_relevance(result: QueryResult, hits: list[ScoredHit]) -> QueryResult:
    """Stamp ``doc_rank`` on the query view's nodes by search relevance (task 168).

    The result's ``document`` rows (the stars drawn) are ranked by their best
    hit — a hit maps to a document when their ``sources`` share an element,
    so a child seed, a parent, an entity and a document seed all map the same
    way — best RRF score first, ties on ``str(_id)``; documents no hit maps to
    (pulled in by expansion or the ``part_of`` closure) follow, by ``str(_id)``.
    The hit order is the one relevance signal left: ``expand_graph``
    re-hydrates every row, so no score survives on them (ADR-011 §7).

    Every node row a ranked document owns gets a COPY stamped ``doc_rank`` —
    a document its own rank, any other row the best rank among its
    ``sources`` (:func:`rank_by_provenance`). A row with no ranked provenance
    and every edge stay unstamped (always shown). No hit or no document row →
    the result as is. Pure: no read.
    """

    documents = [row for row in result.nodes if row.get("type") == "document"]
    if not hits or not documents:
        return result

    best: dict[Any, float] = {}
    for row in documents:
        provenance = set(row.get("sources") or [])
        scores = [
            hit.score for hit in hits if provenance & set(hit.doc.get("sources") or [])
        ]
        if scores:
            best[row["_id"]] = max(scores)
    ranked = sorted(
        documents,
        key=lambda row: (
            row["_id"] not in best,
            -best.get(row["_id"], 0.0),
            str(row["_id"]),
        ),
    )

    document_rank: dict[Any, int] = {}
    provenance_rank: dict[PydanticObjectId, int] = {}
    for rank, row in enumerate(ranked, start=1):
        document_rank[row["_id"]] = rank
        for source in row.get("sources") or []:
            provenance_rank.setdefault(source, rank)

    nodes: list[dict[str, Any]] = []
    for row in result.nodes:
        rank = document_rank.get(row["_id"]) or rank_by_provenance(row, provenance_rank)
        nodes.append(row if rank is None else {**row, "doc_rank": rank})
    logger.debug(
        "Relevance rank: %d document(s), %d of %d node rows ranked",
        len(ranked),
        sum("doc_rank" in row for row in nodes),
        len(nodes),
    )
    return QueryResult(nodes=nodes, edges=result.edges)


async def fetch_full_graph(
    client: AsyncMongoClient,
    database: str,
    user_id: PydanticObjectId,
    *,
    max_docs: int | None = None,
) -> QueryResult:
    """Load the **Full graph**: the ``max_docs`` most-recent documents' subgraphs.

    The no-query view (ADR-011 §7). Document rows are ranked by
    ``properties.date`` (ISO string), else ``created_at``, newest first, ties on
    ``_id``; the first ``max_docs`` (default ``query.full_graph_max_docs``) are
    kept, and with them every node and edge whose ``sources`` provenance names
    one of them, plus the endpoints those edges reach that no EXCLUDED document
    owns, plus every no-provenance edge (``sources: []``) between two included
    nodes (ranked with the more recent endpoint). Every returned row is a
    copy stamped ``doc_rank`` — the 1-based rank of its most recent kept
    document — which the **Graph renderer**'s ``Documents`` slider reveals by.
    Only edges whose two endpoints are included are returned.

    Four batched reads whatever ``max_docs`` is (documents → nodes → edges →
    unreached endpoints), every one scoped to ``user_id``, node reads without
    the ``embedding``. Ranking happens here, not in a ``$sort``: the two dates
    are a string and a BSON date, and the document rows are the smallest set in
    the collection (:func:`~tree.memory.rag.structure.rank_recent_documents`,
    the ranking the rag **Memory structure** shares). graphrag's reader: in
    ``rag`` the no-query view is
    :func:`~tree.memory.rag.structure.fetch_rag_structure`.
    """

    max_docs = (
        max_docs if max_docs is not None else app_config.query.full_graph_max_docs
    )
    collection = client[database][MEMORY_COLLECTION]

    # 1. Rank the documents.
    documents = [
        row
        async for row in collection.find(
            {"user_id": user_id, "kind": "node", "type": "document"}, NO_EMBEDDING
        )
    ]
    kept = rank_recent_documents(documents, max_docs)

    nodes: dict[Any, dict[str, Any]] = {}
    provenance_rank: dict[PydanticObjectId, int] = {}
    for rank, row in enumerate(kept, start=1):
        nodes[row["_id"]] = {**row, "doc_rank": rank}
        if not row.get("sources"):
            logger.debug("Document %s has no sources: embedded alone", row["_id"])
            continue
        provenance_rank.setdefault(row["sources"][0], rank)
    provenance = list(provenance_rank)

    # 2. Their subgraph nodes: chunks, and every entity extracted from them.
    async for row in collection.find(
        {"user_id": user_id, "kind": "node", "sources": {"$in": provenance}},
        NO_EMBEDDING,
    ):
        if row["_id"] not in nodes:
            rank = rank_by_provenance(row, provenance_rank)
            nodes[row["_id"]] = {**row, "doc_rank": rank}

    # 3. Their edges, plus every edge with no provenance at all (hand-made or
    #    merge rows such as `same_as`): those are kept below only when both
    #    endpoints are already in, and never reach a new endpoint.
    edges = [
        {**row, "doc_rank": rank_by_provenance(row, provenance_rank)}
        async for row in collection.find(
            {
                "user_id": user_id,
                "kind": "edge",
                "$or": [{"sources": {"$in": provenance}}, {"sources": []}],
            }
        )
    ]

    # 4. Endpoints no kept document owns (a hand-added entity, a `referenced`
    #    target): hydrated with the best rank among the edges reaching them.
    endpoint_rank: dict[Any, int] = {}
    for edge in edges:
        if edge["doc_rank"] is None:
            continue
        for endpoint in (edge.get("source_node_id"), edge.get("target_node_id")):
            if endpoint not in nodes:
                endpoint_rank[endpoint] = min(
                    endpoint_rank.get(endpoint, edge["doc_rank"]), edge["doc_rank"]
                )
    async for row in collection.find(
        {"user_id": user_id, "kind": "node", "_id": {"$in": list(endpoint_rank)}},
        NO_EMBEDDING,
    ):
        # An edge can name a kept AND an excluded document; a row owned only
        # by excluded documents stays out (and with it, that edge).
        if row.get("sources") and rank_by_provenance(row, provenance_rank) is None:
            continue
        nodes[row["_id"]] = {**row, "doc_rank": endpoint_rank[row["_id"]]}

    included: list[dict[str, Any]] = []
    for edge in edges:
        source, target = edge.get("source_node_id"), edge.get("target_node_id")
        if source not in nodes or target not in nodes:
            continue
        if edge["doc_rank"] is None:
            edge["doc_rank"] = min(nodes[source]["doc_rank"], nodes[target]["doc_rank"])
        included.append(edge)
    edges = included
    logger.info(
        "Full graph: embedded %d of %d documents (most recent first) → %d nodes, %d edges",
        len(kept),
        len(documents),
        len(nodes),
        len(edges),
    )
    return QueryResult(nodes=list(nodes.values()), edges=edges)
