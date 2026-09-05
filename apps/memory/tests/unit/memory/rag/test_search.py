"""Unit tests for the hybrid seed search (ADR-006 decision 2).

Two claims are pinned here:

* the EXACT filter shapes both stages send to Mongo — a ``node_filter`` that
  reaches only one of the two stages would let fusion resurrect the rows the
  caller excluded;
* the "a **Parent chunk** is never a seed" invariant, asserted behaviourally
  (a parent row whose content matches the query is absent from the results).

``TestRRFFuse`` moved here verbatim with ``_rrf_fuse`` (was
``tests/unit/memory/query/test_core.py``).
"""

from __future__ import annotations

import pytest
from beanie import PydanticObjectId

from tree.memory.rag.search import _rrf_fuse, hybrid_search
from tree.models.fake_model import FakeEmbeddingModel

_USER = PydanticObjectId("507f1f77bcf86cd799439011")
_CHILD_FILTER = {"type": "chunk", "subtype": "child"}


@pytest.fixture
def embedding_model() -> FakeEmbeddingModel:
    return FakeEmbeddingModel(dimensions=4)


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

        hits = await hybrid_search(
            collection,
            "parent chunk",
            embedding_model,
            _USER,
            limit=10,
            node_filter={},
        )

        assert [hit.doc["_id"] for hit in hits] == ["c0"]

    async def test_entity_and_child_rows_are_both_seeds(
        self, make_collection, embedding_model, make_child_row, make_entity_row
    ) -> None:
        child = make_child_row(_USER, "c0", content="alice ships memory")
        entity = make_entity_row(_USER, "e1", name="alice")
        collection = make_collection([child, entity])

        hits = await hybrid_search(
            collection, "alice", embedding_model, _USER, limit=10, node_filter={}
        )

        assert {hit.doc["_id"] for hit in hits} == {"c0", "e1"}


class TestFusedOutput:
    async def test_returns_scored_hits_ranked_best_first(
        self, make_collection, embedding_model, make_child_row
    ) -> None:
        rows = [
            make_child_row(_USER, "c0", content="alpha term"),
            make_child_row(_USER, "c1", content="alpha"),
        ]
        collection = make_collection(rows)

        hits = await hybrid_search(
            collection, "alpha", embedding_model, _USER, limit=10, node_filter={}
        )

        assert [hit.doc["_id"] for hit in hits] == ["c0", "c1"]
        assert hits[0].score > hits[1].score

    async def test_truncates_to_limit(
        self, make_collection, embedding_model, make_child_row
    ) -> None:
        rows = [make_child_row(_USER, f"c{i}", content="alpha") for i in range(5)]
        collection = make_collection(rows)

        hits = await hybrid_search(
            collection, "alpha", embedding_model, _USER, limit=2, node_filter={}
        )

        assert len(hits) == 2

    async def test_a_text_stage_failure_degrades_to_vector_only(
        self, make_collection, embedding_model, make_child_row
    ) -> None:
        # Arrange — no text index (the state of a fresh collection before the
        # indexing pipeline runs): the $text aggregate raises.
        collection = make_collection([make_child_row(_USER, "c0", content="alpha")])
        working_aggregate = collection.aggregate

        async def flaky_aggregate(pipeline):
            if "$match" in pipeline[0]:
                raise RuntimeError("text index missing")
            return await working_aggregate(pipeline)

        collection.aggregate = flaky_aggregate

        hits = await hybrid_search(
            collection, "alpha", embedding_model, _USER, limit=10, node_filter={}
        )

        assert [hit.doc["_id"] for hit in hits] == ["c0"]


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
