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
from tree.memory.rag.indexing import VECTOR_INDEX_NAME, index_entry_is_queryable
from tree.memory.rag.types import HybridSearchResult, ScoredHit, SearchMode
from tree.models.base import BaseEmbeddingModel
from tree.observability import track

logger = logging.getLogger(__name__)

# The one row shape that must never be a search seed (ADR-006 decision 2).
_PARENT_ROW: dict[str, Any] = {"type": "chunk", "subtype": "parent"}


class SearchUnavailableError(RuntimeError):
    """Both search legs raised, so no hit list exists — degraded, not empty."""


@track(name="hybrid_search")
async def hybrid_search(
    collection: Any,
    query: str,
    embedding_model: BaseEmbeddingModel,
    user_id: PydanticObjectId,
    *,
    limit: int,
    node_filter: dict[str, Any],
) -> HybridSearchResult:
    """Vector + text search over ``collection``, fused with RRF.

    ``node_filter`` narrows BOTH stages to one row family and is merged into the
    ``$vectorSearch.filter`` and the text ``$match`` alike — a filter applied to
    only one stage would let the other stage smuggle the excluded rows back in
    through fusion.

    ``user_id`` is pinned into both stages server-side, so cross-tenant rows are
    pruned before fusion rather than after. Returns at most ``limit`` hits,
    best fused score first.

    Each leg answers ``list[dict] | None``: ``None`` means the leg RAISED,
    ``[]`` means it ran and matched nothing (ADR-008 §3). This function turns
    that pair into a **Search mode** — one dead leg is ``text_only`` /
    ``vector_only``, two live legs are ``hybrid`` even when both are empty —
    so a caller can tell "no matches" from "half the index is gone".

    Raises:
        SearchUnavailableError: both legs raised; there is no hit list to
            report, and an empty one would read as "nothing matches".
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

    if vector_results is None and text_results is None:
        raise SearchUnavailableError("vector and text search are both unavailable")

    search_mode: SearchMode = "hybrid"
    if vector_results is None:
        search_mode = "text_only"
    elif text_results is None:
        search_mode = "vector_only"

    fused = _rrf_fuse(
        vector_results or [], text_results or [], k=app_config.query.rrf_k
    )
    ranked = sorted(fused.values(), key=lambda item: item["score"], reverse=True)

    return HybridSearchResult(
        hits=[
            ScoredHit(doc=item["doc"], score=item["score"]) for item in ranked[:limit]
        ],
        search_mode=search_mode,
    )


@track(name="_vector_search")
async def _vector_search(
    collection: Any,
    query: str,
    embedding_model: BaseEmbeddingModel,
    *,
    user_id: PydanticObjectId,
    limit: int,
    node_filter: dict[str, Any],
) -> list[dict[str, Any]] | None:
    """Approximate-nearest-neighbour stage. ``None`` when the leg is unavailable.

    Unavailable is TWO states, because ``$vectorSearch`` only raises for one of
    them: the aggregate raising (mongot unreachable), and the aggregate quietly
    answering ``[]`` because ``vector_index`` is absent or still building —
    verified 2026-09-12 against the local mongot, which returns an empty result
    set rather than an error for a missing index. Both must read as
    ``text_only``, so the empty answer is confirmed against
    :func:`_vector_index_is_queryable` before it counts as "no matches".

    No parent exclusion here: parents are written with ``embedding: []`` and so
    are absent from the vector index — adding a filter clause for them would
    cost a pre-filter on every query to exclude rows that cannot be returned.

    Candidates that ran clear :func:`_gate_vector_candidates` before fusion.
    """

    query_vector = (await embedding_model.embed([query]))[0]

    pipeline = [
        {
            "$vectorSearch": {
                "index": VECTOR_INDEX_NAME,
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
    except Exception:
        logger.warning(
            "Vector search leg unavailable; the query runs text-only", exc_info=True
        )
        return None

    if not results:
        return [] if await _vector_index_is_queryable(collection) else None

    return _gate_vector_candidates(results)


def _gate_vector_candidates(
    candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Drop ANN candidates scoring below ``query.min_vector_score`` (ADR-008 §3).

    Runs only on a leg that ANSWERED with candidates, and only after
    :func:`_vector_index_is_queryable` has settled availability — a leg gated
    down to nothing returns ``[]`` (a real "no matches", mode stays ``hybrid``),
    never ``None`` (degraded). Degraded and filtered must not be the same answer.

    The bar sits on ``vectorSearchScore`` (Atlas normalises cosine to
    ``(1 + cos) / 2``, an absolute similarity) and NEVER on the fused RRF score,
    which is a rank statistic comparable only within one query. ``$text`` hits
    are ungated in :func:`_text_search`: a lexical match is a match, and
    ``textScore`` is not normalisable.

    ``top`` in the log line is the best CANDIDATE score, before the gate — it is
    what tells an operator whether the knob is set too high.
    """

    threshold = app_config.query.min_vector_score
    kept = [doc for doc in candidates if doc["_search_score"] >= threshold]
    logger.info(
        "vector leg: %d candidate(s), %d kept at min_vector_score=%.2f (top=%.3f)",
        len(candidates),
        len(kept),
        threshold,
        max(doc["_search_score"] for doc in candidates),
    )
    return kept


async def _vector_index_is_queryable(collection: Any) -> bool:
    """Is ``vector_index`` present AND queryable? Probes ONLY an empty leg.

    One extra ``listSearchIndexes`` command, and only on the path where the ANN
    stage matched nothing — rare for a real query (``retrieve_parents`` asks for
    40 candidates over a non-empty collection), so the hot path pays nothing.

    Fail-OPEN twice, because only an ABSENT index is unambiguous:

    * the probe raising — an undeterminable state must not turn every empty
      vector leg into a degraded query (an unreachable mongot already fails the
      aggregate above, which is the loud path);
    * an entry the shared helper cannot judge. ``index_entry_is_queryable``
      (:mod:`tree.memory.rag.indexing`) answers ``None`` when the deployment
      reports neither ``queryable`` nor ``status`` — the local mongot — and
      ``None`` is FALSY, so the verdict below is tested with ``is False`` and
      NEVER for truthiness.

    So: no entry → unavailable; the shared helper says ``False`` → unavailable;
    anything else → available.
    """

    try:
        cursor = await collection.list_search_indexes(VECTOR_INDEX_NAME)
        entries = await cursor.to_list()
    except Exception:
        logger.warning(
            "Could not probe search index '%s'; reading the empty vector leg as "
            "a real empty result",
            VECTOR_INDEX_NAME,
            exc_info=True,
        )
        return True

    if not entries:
        logger.warning(
            "Vector search leg unavailable: search index '%s' absent; the query "
            "runs text-only",
            VECTOR_INDEX_NAME,
        )
        return False

    entry = entries[0]
    if index_entry_is_queryable(entry) is False:
        logger.warning(
            "Vector search leg unavailable: search index '%s' is not queryable "
            "(status=%s, queryable=%s); the query runs text-only",
            VECTOR_INDEX_NAME,
            entry.get("status"),
            entry.get("queryable"),
        )
        return False

    return True


async def _text_search(
    collection: Any,
    query: str,
    *,
    user_id: PydanticObjectId,
    limit: int,
    node_filter: dict[str, Any],
) -> list[dict[str, Any]] | None:
    """Run ``$text`` on the memory collection — ``None`` when the leg raised.

    (A standard text index, not Atlas Search.)

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
        logger.warning(
            "Text search leg unavailable; the query runs vector-only", exc_info=True
        )
        return None


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
