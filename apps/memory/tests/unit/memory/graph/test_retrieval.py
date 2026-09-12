"""Unit tests for graphrag retrieval composition and expansion (ADR-006 §2, §5).

The composition claim is the ADR's: a child seed NEVER reaches ``expand_graph``
— it is replaced by its **Parent chunk** — while entity and document seeds pass
through untouched. ``expand_graph`` itself is unchanged by this task, so its
``$graphLookup`` pipeline is pinned here from the new module path.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from beanie import PydanticObjectId

from tree.entities.memory import MEMORY_COLLECTION
from tree.memory.graph.retrieval import expand_graph, fetch_full_graph, query_memory
from tree.memory.rag.types import HybridSearchResult, ScoredHit
from tree.models.fake_model import FakeEmbeddingModel

_USER = PydanticObjectId("507f1f77bcf86cd799439011")
_DATABASE = "test_db"


@pytest.fixture
def embedding_model() -> FakeEmbeddingModel:
    return FakeEmbeddingModel(dimensions=4)


def _client(collection) -> dict:
    return {_DATABASE: {MEMORY_COLLECTION: collection}}


@pytest.fixture
def seed_hits(make_child_row, make_entity_row) -> list[ScoredHit]:
    """One child seed (child of p1) and one entity seed."""

    return [
        ScoredHit(doc=make_child_row(_USER, "c0", parent_id="p1"), score=0.9),
        ScoredHit(doc=make_entity_row(_USER, "e1"), score=0.8),
    ]


async def test_reads_hits_from_hybrid_search_result(
    mocker, make_collection, seed_hits, embedding_model
) -> None:
    """graphrag consumes ``.hits`` and ignores the **Search mode** (ADR-008 §3).

    ``QueryResult`` is Chapter 8's contract and gains no mode field, so a
    degraded seed search must still expand from the hits it did return.
    """

    mocker.patch(
        "tree.memory.graph.retrieval.hybrid_search",
        return_value=HybridSearchResult(hits=seed_hits, search_mode="text_only"),
        autospec=True,
    )
    expand = mocker.patch(
        "tree.memory.graph.retrieval.expand_graph", new_callable=AsyncMock
    )

    await query_memory(
        _client(make_collection()), _DATABASE, "q", embedding_model, _USER
    )

    assert set(expand.await_args.args[2]) == {"p1", "e1"}


class TestQueryMemoryComposition:
    async def test_child_seeds_are_returned_as_their_parents(
        self,
        mocker,
        make_collection,
        make_parent_row,
        make_child_row,
        make_entity_row,
        seed_hits,
        embedding_model,
    ) -> None:
        collection = make_collection(
            [
                make_parent_row(_USER, "p1"),
                make_child_row(_USER, "c0", parent_id="p1"),
                make_entity_row(_USER, "e1"),
            ]
        )
        mocker.patch(
            "tree.memory.graph.retrieval.hybrid_search",
            return_value=HybridSearchResult(hits=seed_hits),
            autospec=True,
        )

        result = await query_memory(
            _client(collection),
            _DATABASE,
            "q",
            embedding_model,
            _USER,
            max_hops=0,
        )

        node_ids = {node["_id"] for node in result.nodes}
        assert node_ids == {"p1", "e1"}
        assert "c0" not in node_ids

    async def test_expansion_starts_from_parent_ids_union_entity_seeds(
        self, mocker, make_collection, seed_hits, embedding_model
    ) -> None:
        mocker.patch(
            "tree.memory.graph.retrieval.hybrid_search",
            return_value=HybridSearchResult(hits=seed_hits),
            autospec=True,
        )
        expand = mocker.patch(
            "tree.memory.graph.retrieval.expand_graph", new_callable=AsyncMock
        )

        await query_memory(
            _client(make_collection()),
            _DATABASE,
            "q",
            embedding_model,
            _USER,
            max_hops=1,
        )

        seed_ids = expand.await_args.args[2]
        assert set(seed_ids) == {"p1", "e1"}
        assert expand.await_args.kwargs["max_hops"] == 1

    async def test_seeds_are_searched_without_a_node_filter(
        self, mocker, make_collection, embedding_model
    ) -> None:
        search = mocker.patch(
            "tree.memory.graph.retrieval.hybrid_search",
            return_value=HybridSearchResult(),
            autospec=True,
        )

        await query_memory(
            _client(make_collection()), _DATABASE, "q", embedding_model, _USER, top_k=5
        )

        assert search.call_args.kwargs["node_filter"] == {}
        assert search.call_args.kwargs["limit"] == 5

    async def test_no_seeds_returns_an_empty_result_without_expanding(
        self, mocker, make_collection, embedding_model
    ) -> None:
        mocker.patch(
            "tree.memory.graph.retrieval.hybrid_search",
            return_value=HybridSearchResult(),
            autospec=True,
        )
        expand = mocker.patch(
            "tree.memory.graph.retrieval.expand_graph", new_callable=AsyncMock
        )

        result = await query_memory(
            _client(make_collection()), _DATABASE, "q", embedding_model, _USER
        )

        assert result.nodes == []
        assert result.edges == []
        expand.assert_not_awaited()


class TestExpandGraph:
    async def test_zero_hops_hydrates_the_seeds_only(
        self, make_collection, make_parent_row, make_entity_row
    ) -> None:
        collection = make_collection(
            [make_parent_row(_USER, "p1"), make_entity_row(_USER, "e1")]
        )

        result = await expand_graph(
            _client(collection), _DATABASE, ["p1"], _USER, max_hops=0
        )

        assert [node["_id"] for node in result.nodes] == ["p1"]
        assert result.edges == []
        assert collection.pipelines == []

    async def test_no_seeds_short_circuits(self, make_collection) -> None:
        collection = make_collection()

        result = await expand_graph(
            _client(collection), _DATABASE, [], _USER, max_hops=2
        )

        assert result.nodes == []
        assert collection.find_filters == []

    async def test_graph_lookup_pipeline_is_bidirectional_and_tenant_scoped(
        self, mocker, make_collection
    ) -> None:
        collection = make_collection()
        mocker.patch.object(collection, "aggregate", new_callable=AsyncMock)
        collection.aggregate.return_value = _empty_cursor()

        await expand_graph(
            _client(collection), _DATABASE, ["p1", "e1"], _USER, max_hops=1
        )

        pipeline = collection.aggregate.await_args.args[0]
        assert pipeline[0]["$match"] == {
            "user_id": _USER,
            "kind": "node",
            "_id": {"$in": ["p1", "e1"]},
        }
        outgoing = pipeline[1]["$graphLookup"]
        incoming = pipeline[2]["$graphLookup"]
        assert outgoing["from"] == MEMORY_COLLECTION
        assert outgoing["connectFromField"] == "target_node_id"
        assert outgoing["connectToField"] == "source_node_id"
        assert incoming["connectFromField"] == "source_node_id"
        assert incoming["connectToField"] == "target_node_id"
        # maxDepth is 0-indexed: max_hops=1 → direct edges only.
        assert outgoing["maxDepth"] == incoming["maxDepth"] == 0
        assert outgoing["restrictSearchWithMatch"] == {
            "user_id": _USER,
            "kind": "edge",
        }
        assert pipeline[3]["$project"] == {
            "edges": {"$setUnion": ["$outgoing", "$incoming"]}
        }


class TestFetchFullGraph:
    async def test_returns_every_node_and_edge_of_the_user(
        self, make_collection, make_parent_row
    ) -> None:
        edge = {
            "_id": "edge1",
            "user_id": _USER,
            "kind": "edge",
            "source_node_id": "p1",
            "target_node_id": "doc1",
        }
        collection = make_collection([make_parent_row(_USER, "p1"), edge])

        result = await fetch_full_graph(_client(collection), _DATABASE, _USER)

        assert [node["_id"] for node in result.nodes] == ["p1"]
        assert [e["_id"] for e in result.edges] == ["edge1"]
        assert collection.find_filters == [
            {"user_id": _USER, "kind": "node"},
            {"user_id": _USER, "kind": "edge"},
        ]


def _empty_cursor():
    """An async-iterable that yields nothing (the aggregate returns a cursor)."""

    class _Cursor:
        async def __aiter__(self):
            return
            yield  # pragma: no cover - makes this an async generator

    return _Cursor()
