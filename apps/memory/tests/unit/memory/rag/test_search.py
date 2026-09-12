"""Unit tests for the hybrid seed search (ADR-006 decision 2).

Two claims are pinned here:

* the EXACT filter shapes both stages send to Mongo — a ``node_filter`` that
  reaches only one of the two stages would let fusion resurrect the rows the
  caller excluded;
* the "a **Parent chunk** is never a seed" invariant, asserted behaviourally
  (a parent row whose content matches the query is absent from the results);
* the **Search mode** each leg-failure combination reports (``TestSearchMode``,
  ADR-008 §3) — a dead leg used to be swallowed into ``[]``, so "Mongo is
  down" and "nothing matches" were the same answer;
* the ``query.min_vector_score`` bar on the VECTOR leg (``TestMinVectorScore``,
  ADR-008 §3) — the only absolute score in the pipeline, so it is the only
  place "nothing relevant" can be decided.

``TestRRFFuse`` moved here verbatim with ``_rrf_fuse`` (was
``tests/unit/memory/graph/test_retrieval.py``).
"""

from __future__ import annotations

import logging

import pytest
from beanie import PydanticObjectId

from tree.memory.rag.search import SearchUnavailableError, _rrf_fuse, hybrid_search
from tree.models.fake_model import FakeEmbeddingModel

_USER = PydanticObjectId("507f1f77bcf86cd799439011")
_CHILD_FILTER = {"type": "chunk", "subtype": "child"}

# A query no row's text contains, so the ``$text`` leg answers ``[]`` and the
# vector leg's gate is the only thing deciding what reaches fusion.
_OFF_TOPIC = "zxqv plorb wumbus"

# The bar these tests pin the gate to — a fixed number, NOT the shipped default
# (0.75 today): that one is provisional and evals own it (ADR-008 §4), so it is
# asserted once, in tests/unit/config/test_app_config.py, and patched here.
_MIN_VECTOR_SCORE = 0.65

# The first-stage key that identifies each leg's pipeline.
_VECTOR_STAGE = "$vectorSearch"
_TEXT_STAGE = "$match"


@pytest.fixture
def embedding_model() -> FakeEmbeddingModel:
    return FakeEmbeddingModel(dimensions=4)


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

    The real triggers are a dropped ``vector_index`` (mid-rebuild) and a missing
    text index on a fresh collection; both surface as the aggregate raising.
    """

    working_aggregate = collection.aggregate

    async def flaky_aggregate(pipeline):
        if stage in pipeline[0]:
            raise RuntimeError(f"{stage} is unavailable")
        return await working_aggregate(pipeline)

    collection.aggregate = flaky_aggregate


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

    async def test_text_match_carries_the_same_filter_keys(
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

        match = collection.text_pipeline[0]["$match"]
        assert match["user_id"] == _USER
        assert match["kind"] == "node"
        assert match["type"] == "chunk"
        assert match["subtype"] == "child"
        assert match["$text"] == {"$search": "parent chunk"}


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

    async def test_text_match_excludes_parent_rows(
        self, make_collection, embedding_model
    ) -> None:
        collection = make_collection()

        await hybrid_search(
            collection, "alice", embedding_model, _USER, limit=10, node_filter={}
        )

        assert collection.text_pipeline[0]["$match"]["$nor"] == [
            {"type": "chunk", "subtype": "parent"}
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
        # Arrange — ``vector_index`` was dropped for a dimension change, so
        # the $vectorSearch aggregate raises while $text still answers.
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
        # Arrange — no text index (the state of a fresh collection before the
        # indexing pipeline runs): the $text aggregate raises.
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

    async def test_text_hits_are_not_gated(
        self, make_collection, embedding_model, make_child_row
    ) -> None:
        # Arrange — a rare identifier the embedding barely recognises (0.1) but
        # ``$text`` matches exactly. The row IS in the vector index, so BOTH
        # legs return it: the gate must drop the vector copy and leave the text
        # copy alone (``textScore`` is not normalisable — a lexical match is a
        # match). A gate applied after fusion would lose this hit.
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
        assert collection.search_index_probes == []

    async def test_operator_override_raises_the_bar(
        self, make_collection, embedding_model, make_child_row, mocker, caplog
    ) -> None:
        # An operator raises the bar on a noisy corpus: what passed at the
        # pinned 0.65 must vanish, and the log must name the new bar. 0.85 rather
        # than the User Story's 0.75 — the live pin in tasks/125 moved the
        # SHIPPED default to 0.75, so that value no longer reads as an override.
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
