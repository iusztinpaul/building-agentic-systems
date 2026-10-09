"""The rag **Memory structure**: the document → parent → child tree, as rows.

What ``make memory-visualize-structure`` and the rag ``visualize_memory_structure``
MCP tool hand the **Graph renderer** in ``rag`` mode — a
:class:`~tree.memory.rag.types.MemoryStructure` built from the rows
:mod:`tree.memory.rag.load` wrote. Two views:

* :func:`fetch_rag_structure` — no query: the ``max_docs`` most-recent documents
  and every chunk they own (ADR-011 §7's capped, recency-ranked read).
* :func:`fetch_retrieval_structure` — a query: the parents **Parent-document
  retrieval** returned, their documents and ALL their children, ranked by
  relevance.

Why the edges are SYNTHESISED (:func:`synthesize_part_of_edges`): rag stores no
edge rows (ADR-006 §3), yet the **Graph payload** is ``{nodes, edges}`` and the
renderer's rigid-children drag reads ``part_of`` edges (ADR-011 §4). Each chunk
row's ``parent_id`` already IS that edge, so it is turned into the dict a
graphrag run would have written (same direction, same ``build_edge_id`` id) at
read time and never persisted — the template draws a rag tree unchanged.

Why the module is rag's and not ``visualize/``'s: reading rag rows from Mongo is
rag's job, and the renderer draws what it is handed (it reads no Mongo). It never
imports ``tree.memory.graph`` — the recency helpers below are SHARED the other
way: ``graph/retrieval.py``'s **Full graph** imports them, so both readers rank
documents identically.
"""

import logging
from datetime import UTC, datetime
from typing import Any

from beanie import PydanticObjectId
from pymongo import AsyncMongoClient

from tree.config.app_config import app_config
from tree.entities.memory import MEMORY_COLLECTION, EdgeType, build_edge_id
from tree.memory.rag.types import MemoryStructure, RetrievalResult

logger = logging.getLogger(__name__)

# The two lines both structure surfaces (the CLI and the rag MCP tool) answer
# instead of drawing an empty canvas — one wording, imported by both.
EMPTY_MEMORY_MESSAGE = (
    "Memory is empty for this user — run make memory-run-pipeline first."
)
#: A ``str.format`` template taking ``query``.
NO_RESULTS_MESSAGE = 'No results for "{query}" — nothing to draw.'

# What a document with neither ``properties.date`` nor ``created_at`` ranks by:
# behind every dated one.
UNDATED = datetime.min.replace(tzinfo=UTC)

# Node reads never need the vector: the payload draws no embedding, and a full
# view would otherwise haul every child chunk's vector across the wire.
NO_EMBEDDING = {"embedding": 0}


def as_utc(value: Any) -> datetime | None:
    """An ISO string or a datetime as a tz-aware datetime (naive = UTC), else None."""

    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value)
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


def document_recency(row: dict[str, Any]) -> datetime:
    """When a document row is from: ``properties.date``, else ``created_at``."""

    return (
        as_utc((row.get("properties") or {}).get("date"))
        or as_utc(row.get("created_at"))
        or UNDATED
    )


def rank_by_provenance(
    row: dict[str, Any], provenance_rank: dict[PydanticObjectId, int]
) -> int | None:
    """The best (smallest) document rank among ``row["sources"]``, else None."""

    ranks = [
        provenance_rank[source]
        for source in row.get("sources") or []
        if source in provenance_rank
    ]
    return min(ranks, default=None)


def rank_recent_documents(
    rows: list[dict[str, Any]], max_docs: int
) -> list[dict[str, Any]]:
    """The ``max_docs`` newest document rows, newest first, ties on ``str(_id)``.

    Recency is ``properties.date`` (ISO string), else ``created_at``, else last
    (:func:`document_recency`). Ranked in Python, not in a ``$sort``: the two
    dates are a string and a BSON date. Pure.
    """

    by_id = sorted(rows, key=lambda row: str(row["_id"]))
    return sorted(by_id, key=document_recency, reverse=True)[:max_docs]


def synthesize_part_of_edges(
    nodes: list[dict[str, Any]], user_id: PydanticObjectId
) -> list[dict[str, Any]]:
    """One ``part_of`` edge dict per row whose ``parent_id`` is in ``nodes``.

    Child → parent and parent → document, with the ``build_edge_id`` id a
    graphrag run writes for the same pair. A ``parent_id`` pointing outside
    ``nodes`` yields no edge — the renderer would otherwise draw an ``unknown``
    endpoint. ``sources`` and ``doc_rank`` are copied off the child row so the
    ``Documents`` slider hides the edge with it. Input order; pure, never stored.
    """

    node_ids = {row["_id"] for row in nodes}
    edges: list[dict[str, Any]] = []
    for row in nodes:
        parent_id = row.get("parent_id")
        if parent_id is None or parent_id not in node_ids:
            continue
        edges.append(
            {
                "_id": build_edge_id(row["_id"], EdgeType.PART_OF, parent_id),
                "kind": "edge",
                "type": EdgeType.PART_OF.value,
                "source_node_id": row["_id"],
                "target_node_id": parent_id,
                "user_id": user_id,
                "sources": row.get("sources", []),
                "doc_rank": row.get("doc_rank"),
            }
        )
    return edges


async def fetch_rag_structure(
    client: AsyncMongoClient,
    database: str,
    user_id: PydanticObjectId,
    *,
    max_docs: int | None = None,
) -> MemoryStructure:
    """The no-query rag **Memory structure**: the newest documents' trees.

    Two reads, both scoped to ``user_id`` and without the ``embedding``: ① every
    ``document`` row, cut to the ``max_docs`` most recent (default
    ``query.full_graph_max_docs``) and stamped ``doc_rank`` 1-based; ② every node
    row whose ``sources`` names a kept document (its parents and children),
    stamped with that document's rank. The edges are synthesised from
    ``parent_id``. No document → an empty structure.
    """

    max_docs = (
        max_docs if max_docs is not None else app_config.query.full_graph_max_docs
    )
    collection = client[database][MEMORY_COLLECTION]

    documents = [
        row
        async for row in collection.find(
            {"user_id": user_id, "kind": "node", "type": "document"}, NO_EMBEDDING
        )
    ]
    if not documents:
        return MemoryStructure()
    kept = rank_recent_documents(documents, max_docs)

    nodes: dict[Any, dict[str, Any]] = {}
    provenance_rank: dict[PydanticObjectId, int] = {}
    for rank, row in enumerate(kept, start=1):
        nodes[row["_id"]] = {**row, "doc_rank": rank}
        if row.get("sources"):
            provenance_rank.setdefault(row["sources"][0], rank)

    async for row in collection.find(
        {"user_id": user_id, "kind": "node", "sources": {"$in": list(provenance_rank)}},
        NO_EMBEDDING,
    ):
        if row["_id"] not in nodes:
            nodes[row["_id"]] = {
                **row,
                "doc_rank": rank_by_provenance(row, provenance_rank),
            }

    rows = list(nodes.values())
    edges = synthesize_part_of_edges(rows, user_id)
    logger.info(
        "Memory structure (rag): embedded %d of %d documents (most recent first) "
        "→ %d nodes, %d edges",
        len(kept),
        len(documents),
        len(rows),
        len(edges),
    )
    return MemoryStructure(nodes=rows, edges=edges)


async def fetch_retrieval_structure(
    client: AsyncMongoClient,
    database: str,
    user_id: PydanticObjectId,
    retrieval: RetrievalResult,
) -> MemoryStructure:
    """The rag query view: the retrieved parents, their documents, MATCHED children.

    Documents rank by their best parent's position in ``retrieval.parents``
    (already best first; the first occurrence wins), ``doc_rank`` 1-based, and
    every row carries its document's rank. Three reads, all scoped to
    ``user_id``, no ``embedding``: the documents by ``_id``, the parents by
    ``_id``, and only the ``matched_children`` — the passages the search actually
    hit, so the tree shows WHY each parent ranked rather than every child it
    has. The child read is keyed on ``_id`` and still pinned to a retrieved
    ``parent_id``, so a child id can never hang under a parent the view does not
    draw. No parent → an empty structure and no read.
    """

    if not retrieval.parents:
        return MemoryStructure()

    document_rank: dict[str, int] = {}
    for parent in retrieval.parents:
        document_rank.setdefault(parent.document.document_id, len(document_rank) + 1)
    parent_ids = [parent.parent_id for parent in retrieval.parents]
    matched_ids = [
        child.child_id
        for parent in retrieval.parents
        for child in parent.matched_children
    ]
    collection = client[database][MEMORY_COLLECTION]

    documents = [
        row
        async for row in collection.find(
            {
                "user_id": user_id,
                "kind": "node",
                "type": "document",
                "_id": {"$in": list(document_rank)},
            },
            NO_EMBEDDING,
        )
    ]
    documents.sort(key=lambda row: document_rank[row["_id"]])
    provenance_rank: dict[PydanticObjectId, int] = {}
    for row in documents:
        for source in row.get("sources") or []:
            provenance_rank.setdefault(source, document_rank[row["_id"]])

    parents = {
        row["_id"]: row
        async for row in collection.find(
            {"user_id": user_id, "kind": "node", "_id": {"$in": parent_ids}},
            NO_EMBEDDING,
        )
    }
    children = [
        row
        async for row in collection.find(
            {
                "user_id": user_id,
                "kind": "node",
                "type": "chunk",
                "subtype": "child",
                "parent_id": {"$in": parent_ids},
                "_id": {"$in": matched_ids},
            },
            NO_EMBEDDING,
        )
    ]

    rows = [{**row, "doc_rank": document_rank[row["_id"]]} for row in documents]
    rows.extend(
        {**row, "doc_rank": rank_by_provenance(row, provenance_rank)}
        for row in [parents[pid] for pid in parent_ids if pid in parents] + children
    )
    edges = synthesize_part_of_edges(rows, user_id)
    logger.info(
        "Memory structure (rag query): %d parents under %d documents → %d nodes, "
        "%d edges",
        len(parents),
        len(documents),
        len(rows),
        len(edges),
    )
    return MemoryStructure(nodes=rows, edges=edges)
