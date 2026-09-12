"""Unit tests for the hybrid seed search (ADR-006 decision 2).

Two claims are pinned here:

* the EXACT filter shapes both stages send to Mongo — a ``node_filter`` that
  reaches only one of the two stages would let fusion resurrect the rows the
  caller excluded;
* the "a **Parent chunk** is never a seed" invariant, asserted behaviourally
  (a parent row whose content matches the query is absent from the results);
* the **Search mode** each leg-failure combination reports (``TestSearchMode``,
  ADR-008 §3) — a dead leg used to be swallowed into ``[]``, so "Mongo is
  down" and "nothing matches" were the same answer.

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

# The first-stage key that identifies each leg's pipeline.
_VECTOR_STAGE = "$vectorSearch"
_TEXT_STAGE = "$match"


@pytest.fixture
def embedding_model() -> FakeEmbeddingModel:
    return FakeEmbeddingModel(dimensions=4)


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
