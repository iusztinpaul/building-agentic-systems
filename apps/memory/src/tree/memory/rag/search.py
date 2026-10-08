"""Hybrid seed search over the ``memory`` collection (ADR-006 decision 2).

ONE function — :func:`hybrid_search` — serves BOTH memory modes: ``rag`` calls it
with ``node_filter={"type": "chunk", "subtype": "child"}`` (parent-document
retrieval searches children only), ``graphrag`` calls it with ``node_filter={}``
(children + entities + documents are all valid graph seeds). Vector and text
results are fused client-side with reciprocal rank fusion, exactly as the
pre-ADR-006 ``search_nodes`` did.

**A Parent chunk is NEVER a seed, in either mode.** Parents carry no vector
(no ``embedding`` field), so the vector stage cannot return one; the text stage
excludes them explicitly with a ``mustNot`` clause because the **Text search
index** indexes a parent's content like any other row's. That invariant lives
HERE, unconditionally, rather than in each caller's ``node_filter`` — a caller
that forgets it would silently start seeding graph expansion from rows the
retrieval layer promises never to return.

The text stage is Atlas ``$search`` on ``text_search_index`` (ADR-015): a row is
a text candidate iff it contains at least K of the query's M distinct content
terms — the **Minimum match ratio**, ``K = max(1, ceil(ratio × M))`` — sent as
``compound.should`` (one ``text`` clause per term) + ``minimumShouldMatch``.
There is no score bar on that leg: RRF reads rank only.

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
import math
import re
from typing import Any

from beanie import PydanticObjectId
from bson import ObjectId

from tree.config.app_config import app_config
from tree.memory.rag.indexing import (
    _TEXT_INDEX_FILTER_PATHS,
    _TEXT_INDEX_TEXT_PATHS,
    TEXT_SEARCH_INDEX_NAME,
    VECTOR_INDEX_NAME,
    index_entry_is_queryable,
)
from tree.memory.rag.types import HybridSearchResult, ScoredHit, SearchMode
from tree.models.base import BaseEmbeddingModel
from tree.observability import track

logger = logging.getLogger(__name__)

# The one row shape that must never be a search seed (ADR-006 decision 2).
_PARENT_ROW: dict[str, Any] = {"type": "chunk", "subtype": "parent"}

# Lucene's 33 English stop words, verbatim: what ``lucene.english`` (the
# **Text search index** analyzer) drops at index time, so a clause on one of
# them can never match (task 190's spike).
_LUCENE_ENGLISH_STOP_WORDS = frozenset(
    "a an and are as at be but by for if in into is it no not of on or such "
    "that the their then there these they this to was will with".split()
)

STOP_WORDS: frozenset[str] = _LUCENE_ENGLISH_STOP_WORDS | frozenset(
    # Question / pronoun / auxiliary / function words — roughly NLTK's English
    # list, split the way ``\w+`` splits contractions ("don't" → "don", "t").
    "i me my myself we our ours ourselves you your yours yourself yourselves "
    "he him his himself she her hers herself its itself them theirs themselves "
    "what which who whom whose those am were been being have has had having "
    "do does did doing because until while about against between through "
    "during before after above below from up down out off over under again "
    "further once here when where why how all any both each few more most "
    "other some nor only own same so than too very s t can just don should "
    "now d ll m o re ve y ain aren couldn didn doesn hadn hasn haven isn ma "
    "mightn mustn needn shan shouldn wasn weren won wouldn could would might "
    "must shall".split()
)
"""Query words that never count as content terms of the **Minimum match ratio**.

A SUPERSET of Lucene's 33 English stop words: a word ``lucene.english`` drops
can never match, so counting it toward M would silently raise K. The rest are
words a question carries without naming its topic. Vendored — no NLTK
dependency (ADR-015 §3).

Good: ``"What is the ReAct agent?"`` → ``["react", "agent"]`` (the question
words go, the topic stays). Bad: listing a DOMAIN word because it is common —
dropping ``"vector"`` would make "vector database" match every "database" row.
"""


# Distinct content terms one text leg sends. mongot caps a query at 1024
# Lucene clauses (``maxClauseCount``) and each term is 4 (one per text path),
# so 256 terms kill the leg; 64 keeps a 4x margin over any real question.
_MAX_QUERY_TERMS = 64


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

    ``node_filter`` narrows BOTH stages to one row family — merged into the
    ``$vectorSearch.filter``, ANDed after the pins in the ``$search`` filter — a
    filter applied to only one stage would let the other stage smuggle the
    excluded rows back in through fusion.

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
    :func:`_search_index_is_queryable` before it counts as "no matches".

    No parent exclusion here: parents are written without an ``embedding`` and so
    are absent from the vector index — adding a filter clause for them would
    cost a pre-filter on every query to exclude rows that cannot be returned.

    Candidates that ran clear :func:`_gate_vector_candidates` before fusion.
    """

    # ``query`` is the user's question — the QUERY side of retrieval, searched
    # against a corpus of ``document`` vectors (ADR-009 §5).
    query_vector = (await embedding_model.embed([query], input_type="query"))[0]

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
        queryable = await _search_index_is_queryable(
            collection, VECTOR_INDEX_NAME, fallback_mode="text_only"
        )
        return [] if queryable else None

    return _gate_vector_candidates(results)


def _gate_vector_candidates(
    candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Drop ANN candidates scoring below ``query.min_vector_score`` (ADR-008 §3).

    Runs only on a leg that ANSWERED with candidates, and only after
    :func:`_search_index_is_queryable` has settled availability — a leg gated
    down to nothing returns ``[]`` (a real "no matches", mode stays ``hybrid``),
    never ``None`` (degraded). Degraded and filtered must not be the same answer.

    The bar sits on ``vectorSearchScore`` (Atlas normalises cosine to
    ``(1 + cos) / 2``, an absolute similarity) and NEVER on the fused RRF score,
    which is a rank statistic comparable only within one query. The text leg
    has no score bar: :func:`_text_search` keeps rows sharing K of the query's
    M terms (ADR-015 §2).

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


async def _search_index_is_queryable(
    collection: Any, index_name: str, *, fallback_mode: SearchMode
) -> bool:
    """Is mongot index ``index_name`` present AND queryable? Probes ONLY an empty leg.

    Shared by both legs' indexes (``vector_index``, ``text_search_index`` —
    ADR-015 §5). ``fallback_mode`` is the **Search mode** the query runs in when
    this leg is unavailable; it only names the outcome in the WARNING lines.

    One extra ``listSearchIndexes`` command, and only on the path where the
    leg matched nothing — rare for a real query (``retrieve_parents`` asks for
    40 candidates over a non-empty collection), so the hot path pays nothing.

    Fail-OPEN twice, because only an ABSENT index is unambiguous:

    * the probe raising — an undeterminable state must not turn every empty
      leg into a degraded query (an unreachable mongot already fails the
      aggregate, which is the loud path);
    * an entry the shared helper cannot judge. ``index_entry_is_queryable``
      (:mod:`tree.memory.rag.indexing`) answers ``None`` when the deployment
      reports neither ``queryable`` nor ``status`` — the local mongot — and
      ``None`` is FALSY, so the verdict below is tested with ``is False`` and
      NEVER for truthiness.

    So: no entry → unavailable; the shared helper says ``False`` → unavailable;
    anything else → available.
    """

    try:
        cursor = await collection.list_search_indexes(index_name)
        entries = await cursor.to_list()
    except Exception:
        logger.warning(
            "Could not probe search index '%s'; reading its empty leg as a real "
            "empty result instead of falling back to %s",
            index_name,
            fallback_mode,
            exc_info=True,
        )
        return True

    if not entries:
        logger.warning(
            "Search leg unavailable: search index '%s' absent; the query runs %s",
            index_name,
            fallback_mode,
        )
        return False

    entry = entries[0]
    if index_entry_is_queryable(entry) is False:
        logger.warning(
            "Search leg unavailable: search index '%s' is not queryable "
            "(status=%s, queryable=%s); the query runs %s",
            index_name,
            entry.get("status"),
            entry.get("queryable"),
            fallback_mode,
        )
        return False

    return True


def query_terms(query: str) -> list[str]:
    """The DISTINCT content terms of ``query``, in first-occurrence order.

    Lower-cased ``\\w+`` tokens minus :data:`STOP_WORDS`, deduped. ``M`` of the
    **Minimum match ratio** is ``len(query_terms(query))``. Dedupe is
    pre-stemming (``agent`` / ``agents`` are two terms) — the accepted
    trade-off of ADR-015 §3; the index still stems both sides of a match.

    Capped at the FIRST :data:`_MAX_QUERY_TERMS` (64) distinct terms, so K is
    computed over the capped M: each term is one Lucene clause per text path,
    and mongot rejects a query past 1024 clauses (``maxClauseCount``). Good: a
    pasted 300-word paragraph keeps its first 64 content terms and the leg
    still runs. Bad: raising the cap to 256 — 256 × 4 paths = 1024 clauses
    trips ``maxClauseCount``, the leg raises and the query runs ``vector_only``.
    """

    terms = (t for t in re.findall(r"\w+", query.lower()) if t not in STOP_WORDS)
    return list(dict.fromkeys(terms))[:_MAX_QUERY_TERMS]


def min_should_match(term_count: int, ratio: float) -> int:
    """K: how many of ``term_count`` terms a row must contain (ADR-015 §2).

    ``0`` when there are no terms, else ``max(1, ceil(ratio × M))`` — ``0.0``
    means "any one term", ``1.0`` "every term". Never above ``term_count``:
    Atlas rejects a ``minimumShouldMatch`` larger than the number of ``should``
    clauses. The product is rounded first because binary floats can overshoot
    (``0.28 * 25 == 7.000000000000001`` would otherwise ask for 8 of 25 terms).
    """

    if term_count == 0:
        return 0
    return min(term_count, max(1, math.ceil(round(ratio * term_count, 9))))


async def _text_search(
    collection: Any,
    query: str,
    *,
    user_id: PydanticObjectId,
    limit: int,
    node_filter: dict[str, Any],
) -> list[dict[str, Any]] | None:
    """Atlas ``$search`` on ``text_search_index`` — ``None`` when unavailable.

    A row is a candidate iff it contains at least K of the query's M terms:
    M = :func:`query_terms` (lower-cased words minus :data:`STOP_WORDS`,
    deduped, capped at :data:`_MAX_QUERY_TERMS`), K = :func:`min_should_match` at ``query.text_min_match_ratio``
    (the **Minimum match ratio**). One ``text`` clause per term over ALL four
    text paths, so ``minimumShouldMatch`` counts terms, not paths. M == 0 (an
    all-stop-word query) answers ``[]`` without a query — Atlas rejects an
    empty ``should``, and the leg did run: the mode stays ``hybrid``.

    No gate on ``searchScore``: BM25 grows with repetition, not word overlap,
    and RRF reads rank only. The ratio applies to every caller — graphrag's
    seed search (``node_filter={}``) as well as rag's children.

    The ``mustNot`` clause is the parent-exclusion invariant: the index holds a
    parent's content like any row's, and a parent seed would break both
    **Parent-document retrieval** (a parent has no ``parent_id`` pointing at a
    parent) and graph expansion (ADR-006: expansion starts at parents ∪
    entities, never at a row that IS the answer's container).

    Unavailable is TWO states, as for the vector leg: the aggregate raising,
    and an EMPTY answer from an absent / not-queryable index (``$search`` on a
    missing index answers ``[]`` — task 190's spike), confirmed by
    :func:`_search_index_is_queryable` → ``vector_only``.

    Raises:
        ValueError: a ``node_filter`` key outside the index's filter paths or a
            non-scalar value — a programming error, never a dead leg.
    """

    _check_text_node_filter(node_filter)
    terms = query_terms(query)
    k = min_should_match(len(terms), app_config.query.text_min_match_ratio)
    if not terms:
        _log_text_leg(0, 0, 0)
        return []

    # The tenant / kind pins come FIRST and are ANDed with the caller's keys,
    # never merged: a ``node_filter`` naming ``user_id`` must narrow the leg
    # (to nothing), not swap in another tenant.
    filters = [
        _equals("user_id", user_id),
        _equals("kind", "node"),
        *(_equals(path, value) for path, value in node_filter.items()),
    ]
    pipeline = [
        {
            "$search": {
                "index": TEXT_SEARCH_INDEX_NAME,
                "compound": {
                    "filter": filters,
                    "mustNot": [
                        {
                            "compound": {
                                "must": [
                                    _equals(path, value)
                                    for path, value in _PARENT_ROW.items()
                                ]
                            }
                        }
                    ],
                    "should": [
                        {"text": {"query": term, "path": list(_TEXT_INDEX_TEXT_PATHS)}}
                        for term in terms
                    ],
                    "minimumShouldMatch": k,
                },
            }
        },
        {"$addFields": {"_search_score": {"$meta": "searchScore"}}},
        {"$limit": limit},
    ]

    try:
        cursor = await collection.aggregate(pipeline)
        results = []
        async for doc in cursor:
            results.append(doc)
    except Exception:
        logger.warning(
            "Text search leg unavailable; the query runs vector-only", exc_info=True
        )
        return None

    _log_text_leg(len(results), len(terms), k)
    if not results:
        queryable = await _search_index_is_queryable(
            collection, TEXT_SEARCH_INDEX_NAME, fallback_mode="vector_only"
        )
        return [] if queryable else None
    return results


def _check_text_node_filter(node_filter: dict[str, Any]) -> None:
    """Every ``node_filter`` key must be a text-index filter path, every value a
    scalar ``equals`` can match — else ``ValueError`` (raised OUTSIDE the leg's
    ``try``: a programming error must not read as a dead leg)."""

    for key, value in node_filter.items():
        if key not in _TEXT_INDEX_FILTER_PATHS:
            raise ValueError(
                f"node_filter key {key!r} is not a {TEXT_SEARCH_INDEX_NAME} filter "
                f"path ({', '.join(_TEXT_INDEX_FILTER_PATHS)})"
            )
        if not isinstance(value, str | ObjectId):
            raise ValueError(
                f"node_filter value for {key!r} must be a str or ObjectId, "
                f"got {value!r}"
            )


def _equals(path: str, value: Any) -> dict[str, Any]:
    return {"equals": {"path": path, "value": value}}


def _log_text_leg(candidates: int, term_count: int, k: int) -> None:
    """The per-query line of every text leg that RAN (ADR-015 §4)."""

    logger.info(
        "text leg: %d candidate(s), %d query term(s), min_should_match=%d",
        candidates,
        term_count,
        k,
    )


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
