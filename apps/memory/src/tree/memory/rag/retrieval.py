"""**Parent-document retrieval** — search children, return parents.

ADR-006 decision 2, ported from the MongoDB/LangChain parent-document-retrieval
recipe WITHOUT LangChain: children carry the vector and the ``parent_id``,
parents carry the text a caller actually wants to read. The algorithm is four
steps and two round-trips:

1. :func:`~tree.memory.rag.search.hybrid_search` over CHILD rows only, asking
   for ``top_k * _CHILD_HITS_PER_PARENT`` hits — several children of one parent
   collapse into one result, so the fan-out is what keeps ``top_k`` parents
   reachable.
2. :func:`group_children_by_parent` — group the fused hits by ``parent_id`` in
   Python, keeping the best score and every matched child.
3. ONE ``find({"_id": {"$in": ...}})`` for the parents, ONE more for their
   documents (via the parents' own ``parent_id``).
4. Rank parents by best-child score, cut to ``top_k``.

Grouping is client-side ON PURPOSE (ADR-006): RRF fusion already happens in
Python, so a server-side ``$group``/``$lookup`` would need the fused score back
inside Mongo — a second ranking mechanism for no gain at these result sizes.

This module is the ``rag``-mode answer surface AND the seed resolver ``graphrag``
reuses: :mod:`tree.memory.graph.retrieval` imports :func:`group_children_by_parent`
to turn child seeds into parent ids before expanding the graph.
"""

from __future__ import annotations

import logging
from typing import Any

from beanie import PydanticObjectId
from pymongo import AsyncMongoClient

from tree.config.app_config import app_config
from tree.entities.memory import MEMORY_COLLECTION
from tree.memory.rag.search import hybrid_search
from tree.memory.rag.types import (
    DocumentMeta,
    MatchedChild,
    RetrievalOutcome,
    RetrievalResult,
    RetrievedParent,
    ScoredHit,
)
from tree.models.base import BaseEmbeddingModel
from tree.observability import track

logger = logging.getLogger(__name__)

# How many child hits to fetch per requested parent. A constant, not a knob
# (ADR-006 "bias to least"): with 256-token children under 4096-token parents a
# parent holds ~16 children, so a query that hits one section repeatedly would
# otherwise return 2-3 distinct parents for top_k=10. A MEASURED recall gap is
# what would justify promoting this to config.
_CHILD_HITS_PER_PARENT = 4

# The rows hybrid search is allowed to seed from in rag mode. Both keys are
# declared filter paths on the vector index, so this narrows the ANN stage
# server-side rather than post-filtering.
_CHILD_NODE_FILTER: dict[str, Any] = {"type": "chunk", "subtype": "child"}


@track(name="retrieve_parents")
async def retrieve_parents(
    client: AsyncMongoClient,
    database: str,
    query: str,
    embedding_model: BaseEmbeddingModel,
    user_id: PydanticObjectId,
    *,
    top_k: int | None = None,
) -> RetrievalResult:
    """Hybrid-search children and return their distinct parents, best first.

    Every read is pinned to ``user_id`` — the child search server-side, the two
    ``$in`` fetches in their filters — so a parent or document belonging to
    another tenant can never be attached to a hit.

    ``top_k <= 0`` asks for no results and gets an empty one, without a query:
    passing ``limit=0`` down made BOTH search stages raise and log
    "Vector/Text search leg unavailable ...", which reads as an Atlas outage to
    whoever is on call (and now raises :class:`SearchUnavailableError`).

    Every returned result carries the seed search's **Search mode** and its
    **Retrieval outcome** (``nothing_found`` iff the gated search kept no hits);
    only the ``top_k <= 0`` early return keeps both defaults, because it never
    searched. :class:`~tree.memory.rag.search.SearchUnavailableError` (both legs
    dead) propagates — a caller must not read that as "nothing found".
    """

    top_k = top_k if top_k is not None else app_config.query.top_k
    if top_k <= 0:
        logger.info(
            "Skipping retrieval: top_k=%d asks for no parents (must be >= 1)", top_k
        )
        return RetrievalResult()

    collection = client[database][MEMORY_COLLECTION]

    result = await hybrid_search(
        collection,
        query,
        embedding_model,
        user_id,
        limit=top_k * _CHILD_HITS_PER_PARENT,
        node_filter=_CHILD_NODE_FILTER,
    )
    hits = result.hits
    # The **Retrieval outcome** is read off the HITS, not off the parents: a hit
    # whose parent row is missing is a data problem (logged and dropped below),
    # not "nothing in memory matched".
    outcome: RetrievalOutcome = "found" if hits else "nothing_found"
    grouped = group_children_by_parent(hits)
    if not grouped:
        logger.info(
            "No child hits for query (outcome=%s, search_mode=%s): %s",
            outcome,
            result.search_mode,
            query[:100],
        )
        return RetrievalResult(outcome=outcome, search_mode=result.search_mode)

    parent_rows = await _fetch_nodes(collection, user_id, list(grouped))
    document_ids = [
        row["parent_id"] for row in parent_rows.values() if row.get("parent_id")
    ]
    document_rows = await _fetch_nodes(collection, user_id, document_ids)

    parents: list[RetrievedParent] = []
    # ``grouped`` is already ordered by best-child score, so the parents come out
    # ranked; dropping orphans keeps that order.
    for parent_id, children in grouped.items():
        row = parent_rows.get(parent_id)
        if row is None:
            logger.warning(
                "Dropping %d child hit(s) %s: parent row %s is missing",
                len(children),
                [child.child_id for child in children],
                parent_id,
            )
            continue
        properties = row.get("properties") or {}
        parents.append(
            RetrievedParent(
                parent_id=str(parent_id),
                chunk_index=row.get("chunk_index"),
                heading_path=properties.get("heading_path") or [],
                content=properties.get("content") or "",
                score=children[0].score,
                document=_document_meta(row, document_rows),
                matched_children=children,
            )
        )

    logger.info(
        "Parent-document retrieval: %d child hit(s) -> %d parent(s), returning %d",
        len(hits),
        len(parents),
        min(len(parents), top_k),
    )
    return RetrievalResult(
        parents=parents[:top_k], outcome=outcome, search_mode=result.search_mode
    )


def group_children_by_parent(hits: list[ScoredHit]) -> dict[Any, list[MatchedChild]]:
    """Collapse child hits onto their parents, best-scoring parent first.

    Returns ``{parent_id: [MatchedChild, ...]}`` with each child list sorted by
    score descending — so ``children[0].score`` IS the parent's rank key — and
    the mapping itself ordered by that key. A hit with no ``parent_id`` is
    dropped with a WARNING naming the child: every child written by
    :mod:`tree.memory.rag.load` has one, so its absence means a hand-written or
    pre-ADR-006 row is still in the collection.

    Ties break on the id (``child_id``, then ``parent_id``) — RRF hands out
    identical fused scores routinely (two children matched by the same single
    stage at the same rank), and without a second key the CLI/MCP answer
    reordered itself between two identical calls.
    """

    grouped: dict[Any, list[MatchedChild]] = {}
    for hit in hits:
        parent_id = hit.doc.get("parent_id")
        if not parent_id:
            logger.warning(
                "Dropping child hit %s: the row carries no parent_id",
                hit.doc.get("_id"),
            )
            continue
        grouped.setdefault(parent_id, []).append(_matched_child(hit))

    for children in grouped.values():
        children.sort(key=lambda child: (-child.score, child.child_id))

    return dict(
        sorted(grouped.items(), key=lambda item: (-item[1][0].score, str(item[0])))
    )


def _matched_child(hit: ScoredHit) -> MatchedChild:
    properties = hit.doc.get("properties") or {}
    return MatchedChild(
        child_id=str(hit.doc["_id"]),
        chunk_index=hit.doc.get("chunk_index"),
        content=properties.get("content") or "",
        score=hit.score,
    )


async def _fetch_nodes(
    collection: Any, user_id: PydanticObjectId, node_ids: list[Any]
) -> dict[Any, dict[str, Any]]:
    """One ``$in`` fetch of node rows, keyed by ``_id``. Empty ids → no query."""

    if not node_ids:
        return {}
    rows: dict[Any, dict[str, Any]] = {}
    async for row in collection.find(
        {"user_id": user_id, "kind": "node", "_id": {"$in": list(set(node_ids))}}
    ):
        rows[row["_id"]] = row
    return rows


def _document_meta(
    parent_row: dict[str, Any], document_rows: dict[Any, dict[str, Any]]
) -> DocumentMeta:
    """Build the parent's :class:`DocumentMeta` from its ``document`` row.

    Falls back to the parent's OWN denormalised ``properties`` (the loader
    copies ``title`` / ``source_type`` / ``source_uri`` / ``date`` onto every
    chunk row) when the document row is gone, so a half-deleted document
    degrades to a slightly stale header instead of dropping a valid hit.
    """

    document_id = parent_row.get("parent_id")
    row = document_rows.get(document_id)
    if row is None:
        logger.warning(
            "Document row %s missing for parent %s; falling back to the "
            "denormalised chunk metadata",
            document_id,
            parent_row.get("_id"),
        )
        row = parent_row
    properties = row.get("properties") or {}
    return DocumentMeta(
        document_id=str(document_id) if document_id else "",
        title=properties.get("title"),
        source_type=properties.get("source_type"),
        source_uri=properties.get("source_uri"),
        date=properties.get("date"),
    )
