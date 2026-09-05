"""Hybrid seed search over the ``memory`` collection (ADR-006 decision 2).

ONE function — :func:`hybrid_search` — serves BOTH memory modes: ``rag`` calls it
with ``node_filter={"type": "chunk", "subtype": "child"}`` (parent-document
retrieval searches children only), ``graphrag`` calls it with ``node_filter={}``
(children + entities + documents are all valid graph seeds). Vector and text
results are fused client-side with reciprocal rank fusion, exactly as the
pre-ADR-006 ``search_nodes`` did.

**A Parent chunk is NEVER a seed, in either mode.** Parents carry no vector
(``embedding: []``), so the vector stage cannot return one; the text stage
excludes them explicitly with ``$nor`` because a `$text` query has no such
guard. That invariant lives HERE, unconditionally, rather than in each caller's
``node_filter`` — a caller that forgets it would silently start seeding graph
expansion from rows the retrieval layer promises never to return.

Atlas ``$vectorSearch`` pre-filter semantics (verified 2026-09-05 against
https://www.mongodb.com/docs/vector-search/indexes/vector-search-type and
.../query/aggregation-stages/vector-search-stage):

* Only paths declared as ``{"type": "filter"}`` in the vector index are
  filterable — here ``user_id, kind, type, subtype, merged_into``
  (``_VECTOR_INDEX_FILTER_PATHS`` in :mod:`tree.memory.rag.indexing`). So a
  ``node_filter`` may only use those keys.
* Filterable BSON types are boolean, date, objectId, numeric, string and UUID
  (and arrays of those). ``null`` is NOT filterable — which is why the
  ``merged_into`` exclusion stays a post-``$match`` elsewhere, and why
  ``subtype: "child"`` (a string) is safe as a pre-filter.
* MQL equality short form is supported (``{"subtype": "child"}`` ==
  ``{"subtype": {"$eq": "child"}}``), plus ``$and`` / ``$in``.
* Pre-filtering does not change ``vectorSearchScore``.
"""

from __future__ import annotations

import logging
from typing import Any

from beanie import PydanticObjectId

from tree.config.app_config import app_config
from tree.memory.rag.types import ScoredHit
from tree.models.base import BaseEmbeddingModel
from tree.observability import track

logger = logging.getLogger(__name__)

# The one row shape that must never be a search seed (ADR-006 decision 2).
_PARENT_ROW: dict[str, Any] = {"type": "chunk", "subtype": "parent"}


@track(name="hybrid_search")
async def hybrid_search(
    collection: Any,
    query: str,
    embedding_model: BaseEmbeddingModel,
    user_id: PydanticObjectId,
    *,
    limit: int,
    node_filter: dict[str, Any],
) -> list[ScoredHit]:
    """Vector + text search over ``collection``, fused with RRF.

    ``node_filter`` narrows BOTH stages to one row family and is merged into the
    ``$vectorSearch.filter`` and the text ``$match`` alike — a filter applied to
    only one stage would let the other stage smuggle the excluded rows back in
    through fusion.

    ``user_id`` is pinned into both stages server-side, so cross-tenant rows are
    pruned before fusion rather than after. Returns at most ``limit`` hits,
    best fused score first.
    """

    vector_results = await _vector_search(
        collection,
        query,
        embedding_model,
        user_id=user_id,
        limit=limit,
        node_filter=node_filter,
    )
    text_results = await _text_search(
        collection, query, user_id=user_id, limit=limit, node_filter=node_filter
    )

    fused = _rrf_fuse(vector_results, text_results, k=app_config.query.rrf_k)
    ranked = sorted(fused.values(), key=lambda item: item["score"], reverse=True)

    return [ScoredHit(doc=item["doc"], score=item["score"]) for item in ranked[:limit]]


@track(name="_vector_search")
async def _vector_search(
    collection: Any,
    query: str,
    embedding_model: BaseEmbeddingModel,
    *,
    user_id: PydanticObjectId,
    limit: int,
    node_filter: dict[str, Any],
) -> list[dict[str, Any]]:
    """Approximate-nearest-neighbour stage.

    No parent exclusion here: parents are written with ``embedding: []`` and so
    are absent from the vector index — adding a filter clause for them would
    cost a pre-filter on every query to exclude rows that cannot be returned.
    """

    query_vector = (await embedding_model.embed([query]))[0]

    pipeline = [
        {
            "$vectorSearch": {
                "index": "vector_index",
                "path": "embedding",
                "queryVector": query_vector,
                "numCandidates": limit * 10,
                "limit": limit,
                "filter": {"user_id": user_id, "kind": "node", **node_filter},
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
    node_filter: dict[str, Any],
) -> list[dict[str, Any]]:
    """Run ``$text`` on the memory collection (a standard text index, not Atlas Search).

    The ``$nor`` clause is the parent-exclusion invariant: unlike the vector
    stage, ``$text`` happily matches a parent's content, and a parent seed would
    break both **Parent-document retrieval** (a parent has no ``parent_id``
    pointing at a parent) and graph expansion (ADR-006: expansion starts at
    parents ∪ entities, never at a row that IS the answer's container).
    """

    pipeline = [
        {
            "$match": {
                "user_id": user_id,
                "kind": "node",
                **node_filter,
                "$nor": [_PARENT_ROW],
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
