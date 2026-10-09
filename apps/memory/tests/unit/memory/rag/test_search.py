"""Unit tests for the hybrid seed search (ADR-006 decision 2).

Two claims are pinned here:

* the EXACT filter shapes both stages send to Mongo — a ``node_filter`` that
  reaches only one of the two stages would let fusion resurrect the rows the
  caller excluded — and the text leg's ``user_id`` / ``kind`` pins are ANDed
  with a ``node_filter``, never replaced by it (``TestTenantPins``);
* the "a **Parent chunk** is never a seed" invariant, asserted behaviourally
  (a parent row whose content matches the query is absent from the results);
* the **Search mode** each leg-failure combination reports (``TestSearchMode``,
  ADR-008 §3) — a dead leg used to be swallowed into ``[]``, so "Mongo is
  down" and "nothing matches" were the same answer;
* the ``query.min_vector_score`` bar on the VECTOR leg (``TestMinVectorScore``,
  ADR-008 §3) — the only absolute score in the pipeline;
* the **Minimum match ratio** rule on the TEXT leg (``TestMinimumMatchRatio``,
  ADR-015 §2–§5) — the exact ``$search`` pipeline, K of M query terms, the
  all-stop-word and unsupported-filter edges, the absent-index probe — plus the
  pure ``query_terms`` (capped at 64 distinct terms) / ``min_should_match`` it
  is built from. The module pins the ratio to ``0.0`` (any one term)
  everywhere else.

``TestRRFFuse`` moved here verbatim with ``_rrf_fuse`` (was
``tests/unit/memory/graph/test_retrieval.py``).
"""

from __future__ import annotations

import asyncio
import logging
import time
from unittest.mock import AsyncMock

import pytest
from beanie import PydanticObjectId

from tree.memory.rag.search import (
    _MAX_QUERY_TERMS,
    STOP_WORDS,
    SearchUnavailableError,
    _rrf_fuse,
    _search_index_is_queryable,
    _text_search,
    hybrid_search,
    min_should_match,
    query_terms,
)
from tree.models.base import EmbeddingRole
from tree.models.exceptions import ExtractionError
from tree.models.fake_model import FakeEmbeddingModel

_USER = PydanticObjectId("507f1f77bcf86cd799439011")
_OTHER_USER = PydanticObjectId("507f1f77bcf86cd799439022")
_CHILD_FILTER = {"type": "chunk", "subtype": "child"}

# A query no row's text contains, so the text leg answers ``[]`` and the
# vector leg's gate is the only thing deciding what reaches fusion.
_OFF_TOPIC = "zxqv plorb wumbus"

# The bar these tests pin the gate to — a fixed number, NOT the shipped default
# (0.70 today): that one is provisional and evals own it (ADR-008 §4), so it is
# asserted once, in tests/unit/config/test_app_config.py, and patched here.
_MIN_VECTOR_SCORE = 0.65

# The first-stage key that identifies each leg's pipeline.
_VECTOR_STAGE = "$vectorSearch"
_TEXT_STAGE = "$search"

# Where the vector leg reads its bound on embedding the query.
_EMBED_TIMEOUT = "tree.memory.rag.search.app_config.query.embedding_timeout_seconds"

# Where the text leg reads the **Minimum match ratio**.
_RATIO = "tree.memory.rag.search.app_config.query.text_min_match_ratio"

# Lucene's 33 English stop words, VERBATIM — what ``lucene.english`` drops at
# index time (task 190's spike: such a clause never matches). Spelled out here
# rather than imported so the pin cannot drift with the code it checks.
_LUCENE_ENGLISH_STOP_WORDS = frozenset(
    "a an and are as at be but by for if in into is it no not of on or such "
    "that the their then there these they this to was will with".split()
)


@pytest.fixture
def embedding_model() -> FakeEmbeddingModel:
    return FakeEmbeddingModel(dimensions=4)


@pytest.fixture(autouse=True)
def any_one_term(mocker) -> None:
    """Pin the **Minimum match ratio** to ``0.0`` (K = 1) for every test that
    is not about it.

    The filter, mode and vector-gate tests use rows that share ONE word with a
    multi-word query; a re-pinned default (``0.5`` today, provisional per
    ADR-015) would otherwise silently drop their text hits.
    ``TestMinimumMatchRatio`` patches it again on top.
    """

    mocker.patch(_RATIO, 0.0)


def _vector_candidate(make_child_row, node_id: str, score: float) -> dict:
    """A child row the ANN stage returns with ``_search_score=score``.

    Its content is deliberately unrelated to ``_OFF_TOPIC`` so the text leg
    cannot resurrect a row the vector gate dropped.
    """

    row = make_child_row(_USER, node_id, content="parent-document retrieval")
    row["_search_score"] = score
    return row


def _break_leg(collection, stage: str) -> None:
    """Make ONE leg's aggregate raise, leaving the other leg working.

    The real trigger is mongot being unreachable; a dropped index does NOT
    raise (it answers ``[]`` — see the probe tests).
    """

    working_aggregate = collection.aggregate

    async def flaky_aggregate(pipeline):
        if stage in pipeline[0]:
            raise RuntimeError(f"{stage} is unavailable")
        return await working_aggregate(pipeline)

    collection.aggregate = flaky_aggregate


class _DownEmbeddingModel(FakeEmbeddingModel):
    """The embedding provider is down: every ``embed`` raises like Voyage does."""

    async def embed(
        self, texts: list[str], input_type: EmbeddingRole | None = None
    ) -> list[list[float]]:
        raise ExtractionError("Voyage AI embedding failed: HTTP 503")


class _SlowEmbeddingModel(FakeEmbeddingModel):
    """The provider answers, but only after ``delay_s`` (backoff, slot wait)."""

    def __init__(self, delay_s: float) -> None:
        super().__init__(dimensions=4)
        self._delay_s = delay_s

    async def embed(
        self, texts: list[str], input_type: EmbeddingRole | None = None
    ) -> list[list[float]]:
        await asyncio.sleep(self._delay_s)
        return await super().embed(texts, input_type)


class TestChildOnlyFilter:
    async def test_vector_filter_is_user_kind_type_and_subtype(
        self, make_collection, embedding_model
    ) -> None:
        collection = make_collection()

        await hybrid_search(
            collection,
            "parent chunk",
            embedding_model,
            _USER,
            limit=8,
            node_filter=_CHILD_FILTER,
        )

        stage = collection.vector_pipeline[0]["$vectorSearch"]
        assert stage["filter"] == {
            "user_id": _USER,
            "kind": "node",
            "type": "chunk",
            "subtype": "child",
        }
        assert stage["limit"] == 8

    async def test_text_filter_carries_the_same_filter_keys(
        self, make_collection, embedding_model
    ) -> None:
        collection = make_collection()

        await hybrid_search(
            collection,
            "parent chunk",
            embedding_model,
            _USER,
            limit=8,
            node_filter=_CHILD_FILTER,
        )

        compound = collection.text_pipeline[0]["$search"]["compound"]
        assert compound["filter"] == [
            {"equals": {"path": "user_id", "value": _USER}},
            {"equals": {"path": "kind", "value": "node"}},
            {"equals": {"path": "type", "value": "chunk"}},
            {"equals": {"path": "subtype", "value": "child"}},
        ]


class TestGraphSeedFilter:
    async def test_vector_filter_is_user_and_kind_only(
        self, make_collection, embedding_model
    ) -> None:
        collection = make_collection()

        await hybrid_search(
            collection, "alice", embedding_model, _USER, limit=10, node_filter={}
        )

        stage = collection.vector_pipeline[0]["$vectorSearch"]
        assert stage["filter"] == {"user_id": _USER, "kind": "node"}

    async def test_text_filter_is_user_and_kind_and_excludes_parent_rows(
        self, make_collection, embedding_model
    ) -> None:
        collection = make_collection()

        await hybrid_search(
            collection, "alice", embedding_model, _USER, limit=10, node_filter={}
        )

        compound = collection.text_pipeline[0]["$search"]["compound"]
        # No ``subtype`` clause: entity rows and children both qualify ...
        assert compound["filter"] == [
            {"equals": {"path": "user_id", "value": _USER}},
            {"equals": {"path": "kind", "value": "node"}},
        ]
        # ... and parents never do, unconditionally (ADR-006 §2).
        assert compound["mustNot"] == [
            {
                "compound": {
                    "must": [
                        {"equals": {"path": "type", "value": "chunk"}},
                        {"equals": {"path": "subtype", "value": "parent"}},
                    ]
                }
            }
        ]

    async def test_a_matching_parent_row_is_not_returned(
        self, make_collection, embedding_model, make_parent_row, make_child_row
    ) -> None:
        # Arrange — both rows carry the query term; only the child has a vector.
        parent = make_parent_row(_USER, "p1", content="parent chunk retrieval")
        child = make_child_row(_USER, "c0", content="parent chunk retrieval")
        collection = make_collection([parent, child])

        result = await hybrid_search(
            collection,
            "parent chunk",
            embedding_model,
            _USER,
            limit=10,
            node_filter={},
        )

        assert [hit.doc["_id"] for hit in result.hits] == ["c0"]

    async def test_entity_and_child_rows_are_both_seeds(
        self, make_collection, embedding_model, make_child_row, make_entity_row
    ) -> None:
        child = make_child_row(_USER, "c0", content="alice ships memory")
        entity = make_entity_row(_USER, "e1", name="alice")
        collection = make_collection([child, entity])

        result = await hybrid_search(
            collection, "alice", embedding_model, _USER, limit=10, node_filter={}
        )

        assert {hit.doc["_id"] for hit in result.hits} == {"c0", "e1"}


class TestTenantPins:
    """The text leg's ``user_id`` / ``kind`` pins are ANDed with ``node_filter``.

    A colliding key narrows the leg (to nothing), never swaps in another tenant
    or row kind — the leg is called directly because the vector leg still
    merges its filter dict (a separate follow-up).
    """

    @pytest.mark.parametrize(
        ("node_filter", "row_user", "row_kind"),
        [
            pytest.param({"user_id": _OTHER_USER}, _OTHER_USER, "node", id="user-id"),
            pytest.param({"kind": "edge"}, _USER, "edge", id="kind"),
            pytest.param(
                {"user_id": _OTHER_USER, "kind": "edge"},
                _OTHER_USER,
                "edge",
                id="user-id-and-kind",
            ),
        ],
    )
    async def test_colliding_key_keeps_the_pins_and_returns_no_foreign_row(
        self, make_collection, make_child_row, node_filter, row_user, row_kind
    ) -> None:
        foreign = make_child_row(row_user, "foreign", content="tenant secret agent")
        foreign["kind"] = row_kind
        collection = make_collection([foreign])

        results = await _text_search(
            collection, "secret agent", user_id=_USER, limit=10, node_filter=node_filter
        )

        compound = collection.text_pipeline[0]["$search"]["compound"]
        assert compound["filter"] == [
            {"equals": {"path": "user_id", "value": _USER}},
            {"equals": {"path": "kind", "value": "node"}},
            *(
                {"equals": {"path": path, "value": value}}
                for path, value in node_filter.items()
            ),
        ]
        assert results == []


class TestFusedOutput:
    async def test_returns_scored_hits_ranked_best_first(
        self, make_collection, embedding_model, make_child_row
    ) -> None:
        rows = [
            make_child_row(_USER, "c0", content="alpha term"),
            make_child_row(_USER, "c1", content="alpha"),
        ]
        collection = make_collection(rows)

        result = await hybrid_search(
            collection, "alpha", embedding_model, _USER, limit=10, node_filter={}
        )

        assert [hit.doc["_id"] for hit in result.hits] == ["c0", "c1"]
        assert result.hits[0].score > result.hits[1].score

    async def test_truncates_to_limit(
        self, make_collection, embedding_model, make_child_row
    ) -> None:
        rows = [make_child_row(_USER, f"c{i}", content="alpha") for i in range(5)]
        collection = make_collection(rows)

        result = await hybrid_search(
            collection, "alpha", embedding_model, _USER, limit=2, node_filter={}
        )

        assert len(result.hits) == 2


class TestSearchMode:
    """One leg down is a **Search mode**; both down is an error (ADR-008 §3)."""

    async def test_vector_leg_failure_reports_text_only(
        self, make_collection, embedding_model, make_child_row
    ) -> None:
        # Arrange — mongot rejects the $vectorSearch aggregate while the
        # $search one still answers.
        collection = make_collection([make_child_row(_USER, "c0", content="alpha")])
        _break_leg(collection, _VECTOR_STAGE)

        result = await hybrid_search(
            collection, "alpha", embedding_model, _USER, limit=10, node_filter={}
        )

        assert result.search_mode == "text_only"
        assert [hit.doc["_id"] for hit in result.hits] == ["c0"]

    async def test_text_leg_failure_reports_vector_only(
        self, make_collection, embedding_model, make_child_row
    ) -> None:
        # Arrange — the $search aggregate raises (mongot rejected it).
        collection = make_collection([make_child_row(_USER, "c0", content="alpha")])
        _break_leg(collection, _TEXT_STAGE)

        result = await hybrid_search(
            collection, "alpha", embedding_model, _USER, limit=10, node_filter={}
        )

        assert result.search_mode == "vector_only"
        assert [hit.doc["_id"] for hit in result.hits] == ["c0"]

    async def test_both_legs_failing_raises(
        self, make_collection, embedding_model, make_child_row
    ) -> None:
        # Arrange — Mongo is unreachable: every aggregate raises. An empty hit
        # list here would read as "nothing matches" to the caller.
        collection = make_collection([make_child_row(_USER, "c0", content="alpha")])
        _break_leg(collection, _VECTOR_STAGE)
        _break_leg(collection, _TEXT_STAGE)

        with pytest.raises(SearchUnavailableError, match="both unavailable"):
            await hybrid_search(
                collection, "alpha", embedding_model, _USER, limit=10, node_filter={}
            )

    async def test_empty_leg_is_hybrid_not_degraded(
        self, make_collection, embedding_model
    ) -> None:
        # A leg that RAN and matched nothing is not a failure: the collection is
        # empty, so both legs answer ``[]`` and the mode stays ``hybrid``.
        result = await hybrid_search(
            make_collection(), "alpha", embedding_model, _USER, limit=10, node_filter={}
        )

        assert result.search_mode == "hybrid"
        assert result.hits == []

    async def test_missing_vector_index_reports_text_only(
        self, make_collection, embedding_model, make_document_row
    ) -> None:
        # Arrange — the indexing pipeline never ran (or ``vector_index`` was
        # dropped): $vectorSearch does NOT raise, it answers zero rows. The row
        # carries no embedding, so the vector leg is empty either way and the
        # text leg still matches it.
        collection = make_collection(
            [make_document_row(_USER, "doc1", content="alpha")], search_indexes=[]
        )

        result = await hybrid_search(
            collection, "alpha", embedding_model, _USER, limit=10, node_filter={}
        )

        assert result.search_mode == "text_only"
        assert [hit.doc["_id"] for hit in result.hits] == ["doc1"]

    async def test_building_vector_index_reports_text_only(
        self, make_collection, embedding_model, make_document_row, caplog
    ) -> None:
        # Arrange — the index exists but mongot is still building it, so it
        # serves no queries yet (ADR-008 §5: "not-yet-ready" is text_only, not
        # an empty memory).
        collection = make_collection(
            [make_document_row(_USER, "doc1", content="alpha")],
            search_indexes=[
                {"name": "vector_index", "status": "BUILDING", "queryable": False}
            ],
        )

        with caplog.at_level(logging.WARNING):
            result = await hybrid_search(
                collection, "alpha", embedding_model, _USER, limit=10, node_filter={}
            )

        assert result.search_mode == "text_only"
        assert "status=BUILDING" in caplog.text
        assert "queryable=False" in caplog.text

    async def test_empty_but_queryable_index_stays_hybrid(
        self, make_collection, embedding_model, make_document_row
    ) -> None:
        # A queryable index that matched nothing IS a result: the leg ran.
        collection = make_collection(
            [make_document_row(_USER, "doc1", content="alpha")],
            search_indexes=[
                {"name": "vector_index", "status": "READY", "queryable": True}
            ],
        )

        result = await hybrid_search(
            collection, "alpha", embedding_model, _USER, limit=10, node_filter={}
        )

        assert result.search_mode == "hybrid"
        assert [hit.doc["_id"] for hit in result.hits] == ["doc1"]

    async def test_index_entry_without_status_or_queryable_stays_hybrid(
        self, make_collection, embedding_model, make_document_row
    ) -> None:
        # The LOCAL mongot reports neither field (verified 2026-09-12: the entry
        # is ``{id, name, type, latestDefinition}``), so "present but silent"
        # must read as healthy — otherwise every empty vector leg on a working
        # local stack would claim to be degraded.
        collection = make_collection(
            [make_document_row(_USER, "doc1", content="alpha")],
            search_indexes=[{"name": "vector_index", "type": "vectorSearch"}],
        )

        result = await hybrid_search(
            collection, "alpha", embedding_model, _USER, limit=10, node_filter={}
        )

        assert result.search_mode == "hybrid"

    async def test_index_probe_only_runs_on_empty_leg(
        self, make_collection, embedding_model, make_child_row
    ) -> None:
        # The probe is an extra command per query; a leg that returned hits is
        # self-evidently queryable, so the hot path must not pay for it.
        collection = make_collection([make_child_row(_USER, "c0", content="alpha")])

        result = await hybrid_search(
            collection, "alpha", embedding_model, _USER, limit=10, node_filter={}
        )

        assert result.search_mode == "hybrid"
        assert collection.search_index_probes == []

    async def test_leg_failure_logs_traceback(
        self, make_collection, embedding_model, make_child_row, caplog
    ) -> None:
        # The WARNING is the only trace of a degraded leg an operator gets, so it
        # must carry the Mongo error — the pre-ADR-008 bare message did not.
        collection = make_collection([make_child_row(_USER, "c0", content="alpha")])
        _break_leg(collection, _VECTOR_STAGE)

        with caplog.at_level(logging.WARNING):
            await hybrid_search(
                collection, "alpha", embedding_model, _USER, limit=10, node_filter={}
            )

        warnings = [
            record
            for record in caplog.records
            if record.levelno == logging.WARNING and "Vector search leg" in record.msg
        ]
        assert len(warnings) == 1
        assert warnings[0].exc_info is not None

    async def test_query_embedding_failure_reports_text_only(
        self, make_collection, make_child_row
    ) -> None:
        # Arrange — Voyage / Modal is down while Mongo is fine: the question
        # cannot be embedded, but the text leg can still answer it.
        collection = make_collection([make_child_row(_USER, "c0", content="alpha")])

        result = await hybrid_search(
            collection, "alpha", _DownEmbeddingModel(), _USER, limit=10, node_filter={}
        )

        assert result.search_mode == "text_only"
        assert [hit.doc["_id"] for hit in result.hits] == ["c0"]
        assert not [p for p in collection.pipelines if _VECTOR_STAGE in p[0]]

    async def test_query_embedding_timeout_reports_text_only_within_bound(
        self, make_collection, make_child_row, mocker
    ) -> None:
        # Arrange — the provider WOULD answer, after a wait far past the bound
        # (a 429 backoff, a held ``voyage-embeddings`` slot, a cold start).
        mocker.patch(_EMBED_TIMEOUT, 0.01)
        collection = make_collection([make_child_row(_USER, "c0", content="alpha")])

        started = time.monotonic()
        result = await hybrid_search(
            collection,
            "alpha",
            _SlowEmbeddingModel(delay_s=5.0),
            _USER,
            limit=10,
            node_filter={},
        )
        elapsed = time.monotonic() - started

        assert result.search_mode == "text_only"
        assert [hit.doc["_id"] for hit in result.hits] == ["c0"]
        assert elapsed < 1.0

    async def test_query_embedding_failure_and_broken_text_leg_raises(
        self, make_collection, make_child_row
    ) -> None:
        # No embedding AND no text leg: there is no hit list to report.
        collection = make_collection([make_child_row(_USER, "c0", content="alpha")])
        _break_leg(collection, _TEXT_STAGE)

        with pytest.raises(SearchUnavailableError, match="both unavailable"):
            await hybrid_search(
                collection,
                "alpha",
                _DownEmbeddingModel(),
                _USER,
                limit=10,
                node_filter={},
            )

    async def test_query_embedding_failure_logs_its_own_warning(
        self, make_collection, make_child_row, caplog
    ) -> None:
        # The model being down and mongot being down are different pages for
        # an operator, so they must not share one WARNING line.
        collection = make_collection([make_child_row(_USER, "c0", content="alpha")])

        with caplog.at_level(logging.WARNING):
            await hybrid_search(
                collection,
                "alpha",
                _DownEmbeddingModel(),
                _USER,
                limit=10,
                node_filter={},
            )

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert [r.getMessage() for r in warnings] == [
            "Query embedding unavailable; the query runs text-only"
        ]
        assert warnings[0].exc_info is not None
        assert "HTTP 503" in caplog.text


class TestSearchIndexIsQueryable:
    """The ONE availability probe, shared by both legs' indexes (ADR-015 §5)."""

    async def test_absent_text_search_index_is_unavailable(
        self, make_collection, caplog
    ) -> None:
        # Arrange — the catalogue holds a queryable ``vector_index`` only, so
        # the probe must look up the index BY NAME to say ``False``.
        collection = make_collection(
            search_indexes=[
                {"name": "vector_index", "status": "READY", "queryable": True}
            ]
        )

        with caplog.at_level(logging.WARNING):
            queryable = await _search_index_is_queryable(
                collection, "text_search_index", fallback_mode="vector_only"
            )

        assert queryable is False
        assert collection.search_index_probes == ["text_search_index"]
        assert "search index 'text_search_index' absent" in caplog.text
        assert "the query runs vector_only" in caplog.text

    async def test_raising_probe_fails_open(self, make_collection, caplog) -> None:
        collection = make_collection()
        collection.list_search_indexes = AsyncMock(
            side_effect=RuntimeError("mongot unreachable")
        )

        with caplog.at_level(logging.WARNING):
            queryable = await _search_index_is_queryable(
                collection, "text_search_index", fallback_mode="vector_only"
            )

        assert queryable is True
        assert "Could not probe search index 'text_search_index'" in caplog.text


class TestMinVectorScore:
    """The vector leg is gated on ``vectorSearchScore`` BEFORE fusion (ADR-008 §3).

    Atlas normalises cosine to ``(1 + cos) / 2``, so it is an absolute
    similarity; the fused RRF score is a rank statistic and can never carry the
    bar. Rows here spell out their ``_search_score`` — the fake collection
    stamps the ``$meta`` field exactly as ``$addFields`` does in Mongo.

    ``_OFF_TOPIC`` is the query in most cases: it matches no row text, so the
    text leg answers ``[]`` and whatever reaches fusion came from the gate.
    """

    @pytest.fixture(autouse=True)
    def pinned_knob(self, mocker) -> None:
        """Pin the bar to ``_MIN_VECTOR_SCORE`` so retuning the provisional
        default (Chapter 7 evals) cannot silently change what these tests
        assert."""

        mocker.patch(
            "tree.memory.rag.search.app_config.query.min_vector_score",
            _MIN_VECTOR_SCORE,
        )

    async def test_drops_vector_hits_below_threshold(
        self, make_collection, embedding_model, make_child_row
    ) -> None:
        # Arrange — the ANN stage always returns its ``limit`` nearest rows, so
        # a near-miss (0.40) rides along with a real match (0.90).
        collection = make_collection(
            [
                _vector_candidate(make_child_row, "c0", 0.90),
                _vector_candidate(make_child_row, "c1", 0.40),
            ]
        )

        result = await hybrid_search(
            collection,
            _OFF_TOPIC,
            embedding_model,
            _USER,
            limit=10,
            node_filter=_CHILD_FILTER,
        )

        assert [hit.doc["_id"] for hit in result.hits] == ["c0"]

    async def test_keeps_hit_at_threshold(
        self, make_collection, embedding_model, make_child_row
    ) -> None:
        # The bar is inclusive: a row scoring EXACTLY the knob is a match.
        collection = make_collection(
            [_vector_candidate(make_child_row, "c0", _MIN_VECTOR_SCORE)]
        )

        result = await hybrid_search(
            collection,
            _OFF_TOPIC,
            embedding_model,
            _USER,
            limit=10,
            node_filter=_CHILD_FILTER,
        )

        assert [hit.doc["_id"] for hit in result.hits] == ["c0"]

    async def test_text_hits_are_not_gated_by_the_vector_bar(
        self, make_collection, embedding_model, make_child_row
    ) -> None:
        # Arrange — a rare identifier the embedding barely recognises (0.1) but
        # ``$search`` matches exactly. The row IS in the vector index, so BOTH
        # legs return it: the vector gate must drop the vector copy and leave
        # the text copy alone — the text leg has no score bar (ADR-015 §4). A
        # gate applied after fusion would lose this hit.
        row = make_child_row(_USER, "c0", content="memory-extract-etl-worker")
        row["_search_score"] = 0.1
        collection = make_collection([row])

        result = await hybrid_search(
            collection,
            "memory-extract-etl-worker",
            embedding_model,
            _USER,
            limit=10,
            node_filter=_CHILD_FILTER,
        )

        assert [hit.doc["_id"] for hit in result.hits] == ["c0"]
        assert result.search_mode == "hybrid"

    async def test_logs_candidates_kept_and_top_score(
        self, make_collection, embedding_model, make_child_row, caplog
    ) -> None:
        # The e2e pin (tasks/125) reads the knob off this line, so ``top`` is the
        # best CANDIDATE score — before the gate — not the best survivor.
        collection = make_collection(
            [
                _vector_candidate(make_child_row, "c0", 0.923),
                _vector_candidate(make_child_row, "c1", 0.400),
            ]
        )

        with caplog.at_level(logging.INFO, logger="tree.memory.rag.search"):
            await hybrid_search(
                collection,
                _OFF_TOPIC,
                embedding_model,
                _USER,
                limit=10,
                node_filter=_CHILD_FILTER,
            )

        assert (
            "vector leg: 2 candidate(s), 1 kept at min_vector_score=0.65 (top=0.923)"
            in caplog.text
        )

    async def test_fully_gated_leg_stays_hybrid_and_never_probes(
        self, make_collection, embedding_model, make_child_row
    ) -> None:
        # Gated-to-nothing is a RESULT, not a degraded leg: every candidate was
        # too weak. Reading it as ``text_only`` would tell the caller half the
        # index is gone — and the index probe must not even run.
        collection = make_collection([_vector_candidate(make_child_row, "c0", 0.30)])

        result = await hybrid_search(
            collection,
            _OFF_TOPIC,
            embedding_model,
            _USER,
            limit=10,
            node_filter=_CHILD_FILTER,
        )

        assert result.hits == []
        assert result.search_mode == "hybrid"
        # The off-topic TEXT leg is genuinely empty and probes its own index;
        # the vector leg answered candidates, so it never probes.
        assert "vector_index" not in collection.search_index_probes

    async def test_operator_override_raises_the_bar(
        self, make_collection, embedding_model, make_child_row, mocker, caplog
    ) -> None:
        # An operator raises the bar on a noisy corpus: what passed at the
        # pinned 0.65 must vanish, and the log must name the new bar. 0.85
        # rather than the User Story's 0.75 for the same reason the bar above is
        # pinned: the SHIPPED default is provisional (0.75 on voyage-3.5 in
        # tasks/125, 0.70 on voyage-4 in tasks/141), so this test holds a value
        # no re-pin can turn into "the default" and stop reading as an override.
        mocker.patch("tree.memory.rag.search.app_config.query.min_vector_score", 0.85)
        collection = make_collection([_vector_candidate(make_child_row, "c0", 0.80)])

        with caplog.at_level(logging.INFO, logger="tree.memory.rag.search"):
            result = await hybrid_search(
                collection,
                _OFF_TOPIC,
                embedding_model,
                _USER,
                limit=10,
                node_filter=_CHILD_FILTER,
            )

        assert result.hits == []
        assert "0 kept at min_vector_score=0.85" in caplog.text


# The README / ADR-015 example question: five content terms at ratio 0.5 → K = 3.
_REACT_QUESTION = "What is the ReAct agent tool calling loop?"
_REACT_TERMS = ["react", "agent", "tool", "calling", "loop"]

# One row per number of ``_REACT_TERMS`` it shares (1, 2, 3), none with a
# vector, so the text leg alone decides which of them fuse.
_ONE_TERM = "the tool registry"
_TWO_TERMS = "the tool calling contract"
_THREE_TERMS = "the agent tool calling contract"


def _text_only_row(make_child_row, node_id: str, content: str) -> dict:
    """A child row ONLY the text leg can return: no ``embedding``."""

    row = make_child_row(_USER, node_id, content=content)
    del row["embedding"]
    return row


def _text_leg_lines(caplog) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith("text leg:")
    ]


class TestQueryTerms:
    """``query_terms``: lower-case ``TOKEN_PATTERN`` tokens, minus ``STOP_WORDS``,
    deduped."""

    @pytest.mark.parametrize(
        ("query", "expected"),
        [
            pytest.param("", [], id="empty"),
            pytest.param("what is it", [], id="all-stop-words"),
            pytest.param(_REACT_QUESTION, _REACT_TERMS, id="readme-question"),
            pytest.param("ReAct react REACT", ["react"], id="dedupe-case-folded"),
            pytest.param(
                "Pydantic pydantic structured outputs",
                ["pydantic", "structured", "outputs"],
                id="dedupe-keeps-first-occurrence-order",
            ),
            pytest.param("vector-like", ["vector", "like"], id="hyphen-splits"),
            pytest.param(
                "How does the ReAct agent decide when to call a tool?",
                ["react", "agent", "decide", "call", "tool"],
                id="story-full-sentence",
            ),
            pytest.param(
                "MongoDB Atlas Vector Search scalar binary quantization int8",
                [
                    "mongodb",
                    "atlas",
                    "vector",
                    "search",
                    "scalar",
                    "binary",
                    "quantization",
                    "int8",
                ],
                id="story-eight-term-quantization",
            ),
            pytest.param(
                "ReAct agent tool calling",
                ["react", "agent", "tool", "calling"],
                id="live-ac-four-terms",
            ),
            # Pre-stemming dedupe is the accepted trade-off (ADR-015 §3).
            pytest.param("agent agents", ["agent", "agents"], id="no-stemming"),
            # The index's standard tokenizer keeps ``4.1`` / ``3.5`` whole, so
            # splitting them would send the phantom terms ``4`` / ``1`` (task 194).
            pytest.param(
                "gpt-4.1 pricing", ["gpt", "4.1", "pricing"], id="decimal-stays-whole"
            ),
            pytest.param("voyage-3.5", ["voyage", "3.5"], id="version-stays-whole"),
            pytest.param(
                "voyage-3.5 embeddings",
                ["voyage", "3.5", "embeddings"],
                id="version-query-three-terms",
            ),
            pytest.param("e.g. 0.70", ["e.g", "0.70"], id="dotted-abbreviation"),
            pytest.param("bread.", ["bread"], id="trailing-dot-dropped"),
        ],
    )
    def test_terms(self, query: str, expected: list[str]) -> None:
        assert query_terms(query) == expected

    @pytest.mark.parametrize(
        ("distinct", "kept"),
        [
            pytest.param(_MAX_QUERY_TERMS - 1, _MAX_QUERY_TERMS - 1, id="under-cap"),
            pytest.param(_MAX_QUERY_TERMS, _MAX_QUERY_TERMS, id="at-cap"),
            pytest.param(_MAX_QUERY_TERMS + 1, _MAX_QUERY_TERMS, id="one-over-cap"),
            pytest.param(300, _MAX_QUERY_TERMS, id="pasted-paragraph"),
        ],
    )
    def test_keeps_the_first_capped_distinct_terms(
        self, distinct: int, kept: int
    ) -> None:
        # 256 terms x 4 paths = mongot's 1024-clause ``maxClauseCount``: the
        # cap keeps a long query's leg alive instead of raising.
        query = " ".join(f"term{i}" for i in range(distinct))

        assert query_terms(query) == [f"term{i}" for i in range(kept)]

    def test_cap_counts_distinct_terms_not_tokens(self) -> None:
        # Duplicates and stop words inside the first 64 positions do not use
        # up the cap: it keeps the first 64 DISTINCT content terms.
        query = "the agent agent " + " ".join(f"term{i}" for i in range(100))

        assert query_terms(query) == [
            "agent",
            *(f"term{i}" for i in range(_MAX_QUERY_TERMS - 1)),
        ]

    def test_stop_words_are_a_superset_of_lucene_english(self) -> None:
        # A word ``lucene.english`` drops can never match, so leaving it out
        # of ``STOP_WORDS`` would count it toward M and silently raise K.
        assert len(_LUCENE_ENGLISH_STOP_WORDS) == 33
        assert _LUCENE_ENGLISH_STOP_WORDS <= STOP_WORDS

    @pytest.mark.parametrize(
        "word", ["what", "how", "does", "when", "which", "you", "can", "would"]
    )
    def test_question_and_auxiliary_words_are_stop_words(self, word: str) -> None:
        assert word in STOP_WORDS

    @pytest.mark.parametrize(
        "word", ["vector", "agent", "tool", "call", "calling", "like", "loop"]
    )
    def test_domain_words_are_never_stop_words(self, word: str) -> None:
        # The bad example from the docstring: a common DOMAIN word is content.
        assert word not in STOP_WORDS


class TestMinShouldMatch:
    @pytest.mark.parametrize(
        ("term_count", "ratio", "expected"),
        [
            pytest.param(0, 0.5, 0, id="no-terms"),
            pytest.param(0, 0.0, 0, id="no-terms-ratio-zero"),
            pytest.param(3, 0.0, 1, id="ratio-zero-is-any-one-term"),
            pytest.param(3, 0.5, 2, id="half-of-three"),
            pytest.param(8, 0.5, 4, id="half-of-eight"),
            pytest.param(3, 1.0, 3, id="ratio-one-is-every-term"),
            pytest.param(1, 0.1, 1, id="at-least-one"),
            pytest.param(5, 0.5, 3, id="readme-question"),
            pytest.param(10, 0.7, 7, id="seven-tenths-of-ten"),
            # ``0.28 * 25`` is 7.000000000000001 and ``0.56 * 25`` is
            # 14.000000000000002 in binary floating point: a bare ``ceil``
            # would ask for one term more than the ratio says.
            pytest.param(25, 0.28, 7, id="float-overshoot-0.28"),
            pytest.param(25, 0.56, 14, id="float-overshoot-0.56"),
        ],
    )
    def test_k(self, term_count: int, ratio: float, expected: int) -> None:
        assert min_should_match(term_count, ratio) == expected

    @pytest.mark.parametrize("term_count", range(1, 11))
    @pytest.mark.parametrize("step", range(11))
    def test_k_is_between_one_and_m_at_every_tenth(
        self, term_count: int, step: int
    ) -> None:
        # Atlas rejects ``minimumShouldMatch`` > the number of should clauses
        # (task 190's spike: OperationFailure code 8), so K must never exceed M.
        k = min_should_match(term_count, step / 10)

        assert 1 <= k <= term_count


class TestMinimumMatchRatio:
    """The text leg keeps rows matching K of the query's M terms (ADR-015 §2–§5).

    ``$search`` on ``text_search_index``: one ``text`` clause per DISTINCT
    content term over the four text paths, ``minimumShouldMatch = K``; no score
    bar of any kind (RRF reads rank only).
    """

    @pytest.fixture(autouse=True)
    def pinned_knobs(self, mocker) -> None:
        mocker.patch(
            "tree.memory.rag.search.app_config.query.min_vector_score",
            _MIN_VECTOR_SCORE,
        )
        mocker.patch(_RATIO, 0.5)

    async def test_pipeline_is_one_search_compound_k_of_m_terms(
        self, make_collection, embedding_model
    ) -> None:
        collection = make_collection()

        await hybrid_search(
            collection,
            _REACT_QUESTION,
            embedding_model,
            _USER,
            limit=8,
            node_filter=_CHILD_FILTER,
        )

        paths = ["name", "aliases", "properties.content", "properties.aliases"]
        # ``$search`` first (Atlas requires it); no ``$sort`` (``$search``
        # already returns by score), no ``$match``, no ``$text``.
        assert collection.text_pipeline == [
            {
                "$search": {
                    "index": "text_search_index",
                    "compound": {
                        "filter": [
                            {"equals": {"path": "user_id", "value": _USER}},
                            {"equals": {"path": "kind", "value": "node"}},
                            {"equals": {"path": "type", "value": "chunk"}},
                            {"equals": {"path": "subtype", "value": "child"}},
                        ],
                        "mustNot": [
                            {
                                "compound": {
                                    "must": [
                                        {"equals": {"path": "type", "value": "chunk"}},
                                        {
                                            "equals": {
                                                "path": "subtype",
                                                "value": "parent",
                                            }
                                        },
                                    ]
                                }
                            }
                        ],
                        # ONE clause per term carrying ALL four paths: K
                        # counts terms, not paths.
                        "should": [
                            {"text": {"query": term, "path": paths}}
                            for term in _REACT_TERMS
                        ],
                        "minimumShouldMatch": 3,
                    },
                }
            },
            {"$addFields": {"_search_score": {"$meta": "searchScore"}}},
            {"$limit": 8},
        ]

    async def test_stop_words_and_duplicates_never_become_clauses(
        self, make_collection, embedding_model
    ) -> None:
        collection = make_collection()

        await hybrid_search(
            collection,
            "the ReAct react REACT",
            embedding_model,
            _USER,
            limit=8,
            node_filter=_CHILD_FILTER,
        )

        compound = collection.text_pipeline[0]["$search"]["compound"]
        assert [clause["text"]["query"] for clause in compound["should"]] == ["react"]
        assert compound["minimumShouldMatch"] == 1

    async def test_half_ratio_keeps_rows_sharing_k_of_m_terms(
        self, make_collection, embedding_model, make_child_row
    ) -> None:
        # M = 5, K = 3: a row brushing one or two of the question's words is
        # not a text candidate; the row sharing three is.
        collection = make_collection(
            [
                _text_only_row(make_child_row, "t1", _ONE_TERM),
                _text_only_row(make_child_row, "t2", _TWO_TERMS),
                _text_only_row(make_child_row, "t3", _THREE_TERMS),
            ]
        )

        result = await hybrid_search(
            collection,
            _REACT_QUESTION,
            embedding_model,
            _USER,
            limit=10,
            node_filter=_CHILD_FILTER,
        )

        assert [hit.doc["_id"] for hit in result.hits] == ["t3"]

    async def test_ratio_zero_needs_any_one_term(
        self, make_collection, embedding_model, make_child_row, mocker
    ) -> None:
        mocker.patch(_RATIO, 0.0)
        collection = make_collection(
            [
                _text_only_row(make_child_row, "t1", _ONE_TERM),
                _text_only_row(make_child_row, "t2", _TWO_TERMS),
            ]
        )

        result = await hybrid_search(
            collection,
            _REACT_QUESTION,
            embedding_model,
            _USER,
            limit=10,
            node_filter=_CHILD_FILTER,
        )

        compound = collection.text_pipeline[0]["$search"]["compound"]
        assert compound["minimumShouldMatch"] == 1
        assert sorted(hit.doc["_id"] for hit in result.hits) == ["t1", "t2"]

    async def test_ratio_one_needs_every_term(
        self, make_collection, embedding_model, make_child_row, mocker
    ) -> None:
        mocker.patch(_RATIO, 1.0)
        collection = make_collection(
            [
                _text_only_row(make_child_row, "t3", _THREE_TERMS),
                _text_only_row(
                    make_child_row, "all", "the ReAct agent tool calling loop"
                ),
            ]
        )

        result = await hybrid_search(
            collection,
            _REACT_QUESTION,
            embedding_model,
            _USER,
            limit=10,
            node_filter=_CHILD_FILTER,
        )

        compound = collection.text_pipeline[0]["$search"]["compound"]
        assert compound["minimumShouldMatch"] == len(_REACT_TERMS)
        assert [hit.doc["_id"] for hit in result.hits] == ["all"]

    async def test_one_shared_word_of_eight_is_not_a_candidate(
        self, make_collection, embedding_model, make_child_row, caplog
    ) -> None:
        # The off-topic story: the physics paper shares only "vector" with an
        # eight-term question (K = 4), so the text leg no longer carries it.
        physics = _text_only_row(
            make_child_row, "physics", "dark photon vector-like fermions"
        )
        collection = make_collection([physics])

        with caplog.at_level(logging.INFO, logger="tree.memory.rag.search"):
            result = await hybrid_search(
                collection,
                "MongoDB Atlas Vector Search scalar binary quantization int8",
                embedding_model,
                _USER,
                limit=10,
                node_filter=_CHILD_FILTER,
            )

        assert result.hits == []
        assert result.search_mode == "hybrid"
        assert _text_leg_lines(caplog) == [
            "text leg: 0 candidate(s), 8 query term(s), min_should_match=4"
        ]

    async def test_decimal_query_term_never_matches_its_bare_digits(
        self, make_collection, embedding_model, make_child_row, caplog
    ) -> None:
        # M = 3 (gpt, 4.1, pricing), K = 2. Split on ``\w+`` it was M = 4 and
        # the stray digits ``4`` + ``1`` made any row carrying them a candidate.
        collection = make_collection(
            [
                _text_only_row(make_child_row, "digits", "chapter 4 of 1 book"),
                _text_only_row(make_child_row, "on-topic", "gpt-4.1 pricing per token"),
            ]
        )

        with caplog.at_level(logging.INFO, logger="tree.memory.rag.search"):
            result = await hybrid_search(
                collection,
                "gpt-4.1 pricing",
                embedding_model,
                _USER,
                limit=10,
                node_filter=_CHILD_FILTER,
            )

        assert [hit.doc["_id"] for hit in result.hits] == ["on-topic"]
        assert _text_leg_lines(caplog) == [
            "text leg: 1 candidate(s), 3 query term(s), min_should_match=2"
        ]

    async def test_long_query_sends_capped_clauses_and_k_over_the_cap(
        self, make_collection, embedding_model, caplog
    ) -> None:
        # 300 distinct terms would be 1200 Lucene clauses — mongot raises
        # (``maxClauseCount`` 1024) and the leg reads ``vector_only``.
        collection = make_collection()
        query = " ".join(f"term{i}" for i in range(300))

        with caplog.at_level(logging.INFO, logger="tree.memory.rag.search"):
            result = await hybrid_search(
                collection,
                query,
                embedding_model,
                _USER,
                limit=10,
                node_filter=_CHILD_FILTER,
            )

        compound = collection.text_pipeline[0]["$search"]["compound"]
        assert [clause["text"]["query"] for clause in compound["should"]] == [
            f"term{i}" for i in range(_MAX_QUERY_TERMS)
        ]
        assert compound["minimumShouldMatch"] == 32
        assert result.search_mode == "hybrid"
        assert _text_leg_lines(caplog) == [
            "text leg: 0 candidate(s), 64 query term(s), min_should_match=32"
        ]

    async def test_all_stop_word_query_sends_no_search_and_stays_hybrid(
        self, make_collection, embedding_model, make_child_row, caplog
    ) -> None:
        # M = 0: Atlas rejects an empty ``should``, and there is nothing to ask
        # — the leg RAN and found nothing, so the vector leg alone decides.
        collection = make_collection(
            [_vector_candidate(make_child_row, "v0", 0.90)],
            search_indexes=[
                {"name": "vector_index", "status": "READY", "queryable": True}
            ],
        )

        with caplog.at_level(logging.INFO, logger="tree.memory.rag.search"):
            result = await hybrid_search(
                collection,
                "what is it",
                embedding_model,
                _USER,
                limit=10,
                node_filter=_CHILD_FILTER,
            )

        assert not [p for p in collection.pipelines if _TEXT_STAGE in p[0]]
        # No probe either — even though ``text_search_index`` is absent here.
        assert "text_search_index" not in collection.search_index_probes
        assert result.search_mode == "hybrid"
        assert [hit.doc["_id"] for hit in result.hits] == ["v0"]
        assert _text_leg_lines(caplog) == [
            "text leg: 0 candidate(s), 0 query term(s), min_should_match=0"
        ]

    async def test_logs_the_line_on_a_leg_with_candidates(
        self, make_collection, embedding_model, make_child_row, caplog
    ) -> None:
        collection = make_collection(
            [
                _text_only_row(make_child_row, "t3", _THREE_TERMS),
                _text_only_row(make_child_row, "t3b", _THREE_TERMS),
            ]
        )

        with caplog.at_level(logging.INFO, logger="tree.memory.rag.search"):
            await hybrid_search(
                collection,
                _REACT_QUESTION,
                embedding_model,
                _USER,
                limit=10,
                node_filter=_CHILD_FILTER,
            )

        assert _text_leg_lines(caplog) == [
            "text leg: 2 candidate(s), 5 query term(s), min_should_match=3"
        ]

    async def test_no_line_on_a_raised_leg(
        self, make_collection, embedding_model, make_child_row, caplog
    ) -> None:
        # Degraded is not empty: ``vector_only``, the WARN, and no text line.
        collection = make_collection([_vector_candidate(make_child_row, "v0", 0.90)])
        _break_leg(collection, _TEXT_STAGE)

        with caplog.at_level(logging.INFO, logger="tree.memory.rag.search"):
            result = await hybrid_search(
                collection,
                _REACT_QUESTION,
                embedding_model,
                _USER,
                limit=10,
                node_filter=_CHILD_FILTER,
            )

        assert result.search_mode == "vector_only"
        assert [hit.doc["_id"] for hit in result.hits] == ["v0"]
        assert _text_leg_lines(caplog) == []
        assert "Text search leg unavailable" in caplog.text

    @pytest.mark.parametrize(
        ("node_filter", "message"),
        [
            pytest.param(
                {"merged_into": None},
                "node_filter key 'merged_into' is not a text_search_index filter "
                "path (user_id, kind, type, subtype)",
                id="key-outside-the-filter-paths",
            ),
            pytest.param(
                {"type": {"$in": ["chunk", "person"]}},
                "node_filter value for 'type' must be a str or ObjectId",
                id="non-scalar-value",
            ),
        ],
    )
    async def test_unsupported_node_filter_raises_before_any_aggregate(
        self, make_collection, embedding_model, node_filter: dict, message: str
    ) -> None:
        # A programming error must raise — never read as a dead leg
        # (``vector_only``) — and no ``$search`` may have been sent.
        collection = make_collection()

        with pytest.raises(ValueError) as excinfo:
            await hybrid_search(
                collection,
                _REACT_QUESTION,
                embedding_model,
                _USER,
                limit=10,
                node_filter=node_filter,
            )

        assert message in str(excinfo.value)
        assert not [p for p in collection.pipelines if _TEXT_STAGE in p[0]]

    @pytest.mark.parametrize(
        "text_entry",
        [
            pytest.param(None, id="absent"),
            pytest.param(
                {"name": "text_search_index", "status": "BUILDING", "queryable": False},
                id="building",
            ),
        ],
    )
    async def test_empty_leg_on_an_unavailable_index_reads_vector_only(
        self, make_collection, embedding_model, make_child_row, caplog, text_entry
    ) -> None:
        # ``$search`` on an absent index answers ``[]`` without raising (task
        # 190's spike) — so the empty leg is confirmed against the catalogue.
        collection = make_collection(
            [_vector_candidate(make_child_row, "v0", 0.90)],
            search_indexes=[
                {"name": "vector_index", "status": "READY", "queryable": True},
                *([text_entry] if text_entry else []),
            ],
        )

        with caplog.at_level(logging.INFO, logger="tree.memory.rag.search"):
            result = await hybrid_search(
                collection,
                _REACT_QUESTION,
                embedding_model,
                _USER,
                limit=10,
                node_filter=_CHILD_FILTER,
            )

        assert result.search_mode == "vector_only"
        assert [hit.doc["_id"] for hit in result.hits] == ["v0"]
        assert collection.search_index_probes == ["text_search_index"]
        assert "search index 'text_search_index'" in caplog.text
        assert "the query runs vector_only" in caplog.text
        # The leg RAN (it did not raise), so its line is written before the
        # probe decides availability.
        assert _text_leg_lines(caplog) == [
            "text leg: 0 candidate(s), 5 query term(s), min_should_match=3"
        ]

    async def test_empty_leg_on_a_readiness_less_entry_stays_hybrid(
        self, make_collection, embedding_model, make_child_row
    ) -> None:
        # The LOCAL mongot reports neither ``status`` nor ``queryable``.
        collection = make_collection(
            [_vector_candidate(make_child_row, "v0", 0.90)],
            search_indexes=[
                {"name": "vector_index", "type": "vectorSearch"},
                {"name": "text_search_index", "type": "search"},
            ],
        )

        result = await hybrid_search(
            collection,
            _REACT_QUESTION,
            embedding_model,
            _USER,
            limit=10,
            node_filter=_CHILD_FILTER,
        )

        assert result.search_mode == "hybrid"

    async def test_empty_leg_with_a_raising_probe_stays_hybrid(
        self, make_collection, embedding_model, make_child_row
    ) -> None:
        collection = make_collection([_vector_candidate(make_child_row, "v0", 0.90)])
        collection.list_search_indexes = AsyncMock(
            side_effect=RuntimeError("mongot unreachable")
        )

        result = await hybrid_search(
            collection,
            _REACT_QUESTION,
            embedding_model,
            _USER,
            limit=10,
            node_filter=_CHILD_FILTER,
        )

        assert result.search_mode == "hybrid"
        assert [hit.doc["_id"] for hit in result.hits] == ["v0"]

    async def test_probe_never_runs_on_a_leg_with_candidates(
        self, make_collection, embedding_model, make_child_row
    ) -> None:
        # Even with ``text_search_index`` missing from the catalogue: a leg
        # that returned rows is self-evidently queryable.
        collection = make_collection(
            [_text_only_row(make_child_row, "t3", _THREE_TERMS)],
            search_indexes=[
                {"name": "vector_index", "status": "READY", "queryable": True}
            ],
        )

        result = await hybrid_search(
            collection,
            _REACT_QUESTION,
            embedding_model,
            _USER,
            limit=10,
            node_filter=_CHILD_FILTER,
        )

        assert result.search_mode == "hybrid"
        assert "text_search_index" not in collection.search_index_probes

    async def test_graph_seed_search_shares_the_rule(
        self, make_collection, embedding_model, make_entity_row, make_parent_row
    ) -> None:
        # graphrag (``node_filter={}``): an entity naming two of the three
        # terms (K = 2) is a seed; the parent naming all three never is.
        entity = make_entity_row(_USER, "e1", name="ReAct agent")
        del entity["embedding"]
        parent = make_parent_row(_USER, "p1", content="ReAct agent loop")
        collection = make_collection([entity, parent])

        result = await hybrid_search(
            collection,
            "ReAct agent loop",
            embedding_model,
            _USER,
            limit=10,
            node_filter={},
        )

        assert [hit.doc["_id"] for hit in result.hits] == ["e1"]


class TestRRFFuse:
    def test_single_list(self):
        vector = [{"_id": "a", "score": 0.9}, {"_id": "b", "score": 0.8}]
        fused = _rrf_fuse(vector, [], k=60)

        assert "a" in fused
        assert "b" in fused
        assert fused["a"]["score"] > fused["b"]["score"]

    def test_both_lists_boost_shared(self):
        vector = [{"_id": "a"}, {"_id": "b"}]
        text = [{"_id": "b"}, {"_id": "c"}]
        fused = _rrf_fuse(vector, text, k=60)

        # "b" appears in both lists → highest score.
        assert fused["b"]["score"] > fused["a"]["score"]
        assert fused["b"]["score"] > fused["c"]["score"]

    def test_empty_lists(self):
        fused = _rrf_fuse([], [])
        assert fused == {}

    def test_scores_are_positive(self):
        results = [{"_id": f"node-{i}"} for i in range(5)]
        fused = _rrf_fuse(results, [], k=60)

        for item in fused.values():
            assert item["score"] > 0

    def test_rrf_formula(self):
        vector = [{"_id": "x"}]
        text = [{"_id": "x"}]
        fused = _rrf_fuse(vector, text, k=10)

        # rank=0 → score = 1/(10+1) + 1/(10+1) = 2/11
        expected = 2.0 / 11.0
        assert abs(fused["x"]["score"] - expected) < 1e-9

    @pytest.mark.parametrize("k", [1, 10, 60, 100])
    def test_different_k_values(self, k):
        vector = [{"_id": "a"}, {"_id": "b"}]
        text = [{"_id": "b"}, {"_id": "a"}]
        fused = _rrf_fuse(vector, text, k=k)

        # Both appear in both lists at different ranks.
        # "a": 1/(k+1) + 1/(k+2), "b": 1/(k+2) + 1/(k+1) → equal.
        assert abs(fused["a"]["score"] - fused["b"]["score"]) < 1e-9


# ---------------------------------------------------------------------------
# Embedding role on the query vector (ADR-009 decision 5)
# ---------------------------------------------------------------------------


class _RoleRecordingEmbeddingModel(FakeEmbeddingModel):
    """A :class:`FakeEmbeddingModel` that records the role of every call."""

    def __init__(self, dimensions: int = 4) -> None:
        super().__init__(dimensions=dimensions)
        self.roles: list[EmbeddingRole | None] = []

    async def embed(
        self, texts: list[str], input_type: EmbeddingRole | None = None
    ) -> list[list[float]]:
        self.roles.append(input_type)
        return await super().embed(texts, input_type)


class TestEmbeddingRole:
    """A user's question is the QUERY side of retrieval, so it embeds as
    ``query`` (ADR-009 §5) — against a corpus of ``document`` vectors. This is
    the one place asymmetry pays off, and the only role the search path may use.
    """

    async def test_query_vector_uses_query_role(self, make_collection) -> None:
        collection = make_collection()
        embedding_model = _RoleRecordingEmbeddingModel(dimensions=4)

        await hybrid_search(
            collection,
            "how does the coordinator shard documents?",
            embedding_model,
            _USER,
            limit=8,
            node_filter=_CHILD_FILTER,
        )

        assert embedding_model.roles == ["query"]
