"""Unit tests for graphrag retrieval composition and expansion (ADR-006 §2, §5).

The composition claim is the ADR's: a child seed NEVER reaches ``expand_graph``
— it is replaced by its **Parent chunk** — while entity and document seeds pass
through untouched. ``expand_graph`` itself is unchanged by this task, so its
``$graphLookup`` pipeline is pinned here from the new module path.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from beanie import PydanticObjectId

from tests.unit.memory.conftest import chunk_star_rows
from tree.config.app_config import app_config
from tree.entities.memory import MEMORY_COLLECTION
from tree.memory.graph.retrieval import (
    CLOSURE_ADDED,
    expand_graph,
    fetch_full_graph,
    query_memory,
    ranked_rows,
)
from tree.memory.rag.types import HybridSearchResult, ScoredHit
from tree.memory.types import QueryResult
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


# Three documents' provenance (task 167: ``sources`` holds Document ObjectIds).
_SRC_A = PydanticObjectId("6abf6ab76789660069adda0a")
_SRC_B = PydanticObjectId("6abf6ab76789660069adda0b")
_SRC_C = PydanticObjectId("6abf6ab76789660069adda0c")
_SRC_STUB = PydanticObjectId("6abf6ab76789660069adda0f")


@pytest.fixture
def ranked_corpus(
    make_document_row, make_parent_row, make_child_row, make_entity_row
) -> list[dict]:
    """Documents A, B, C; A and B each with a parent and a child chunk.

    ``e-ab`` is extracted from A AND B, ``e-none`` has no provenance, and C is
    reachable only through ``pB -references-> C`` — no chunk of C is a hit.
    """

    return [
        make_document_row(_USER, "A", sources=[_SRC_A]),
        make_document_row(_USER, "B", sources=[_SRC_B]),
        make_document_row(_USER, "C", sources=[_SRC_C]),
        _with_sources(make_parent_row(_USER, "pA", document_id="A"), _SRC_A),
        _with_sources(make_parent_row(_USER, "pB", document_id="B"), _SRC_B),
        _with_sources(make_child_row(_USER, "cA", parent_id="pA"), _SRC_A),
        _with_sources(make_child_row(_USER, "cB", parent_id="pB"), _SRC_B),
        _with_sources(make_entity_row(_USER, "e-ab"), _SRC_A, _SRC_B),
        _with_sources(make_entity_row(_USER, "e-none")),
        _graph_edge("pA>A", "pA", "A", _SRC_A, edge_type="part_of"),
        _graph_edge("pB>B", "pB", "B", _SRC_B, edge_type="part_of"),
        _graph_edge("cA>pA", "cA", "pA", _SRC_A, edge_type="part_of"),
        _graph_edge("cB>pB", "cB", "pB", _SRC_B, edge_type="part_of"),
        _graph_edge("pA>e-ab", "pA", "e-ab", _SRC_A),
        _graph_edge("pA>e-none", "pA", "e-none", _SRC_A),
        _graph_edge("pB>C", "pB", "C", _SRC_B, edge_type="references"),
    ]


def _hit(rows: list[dict], node_id: str, score: float) -> ScoredHit:
    return ScoredHit(
        doc=next(row for row in rows if row["_id"] == node_id), score=score
    )


async def _ranked_query(
    mocker,
    collection,
    hits: list[ScoredHit],
    embedding_model: FakeEmbeddingModel,
    max_hops: int = 1,
) -> QueryResult:
    mocker.patch(
        "tree.memory.graph.retrieval.hybrid_search",
        return_value=HybridSearchResult(hits=hits),
        autospec=True,
    )
    return await query_memory(
        _client(collection), _DATABASE, "q", embedding_model, _USER, max_hops=max_hops
    )


def _doc_ranks(rows: list[dict]) -> dict:
    return {row["_id"]: row["doc_rank"] for row in rows if "doc_rank" in row}


class TestQueryMemoryRelevanceRank:
    """Task 168: a query view's documents ranked by their best seed hit, the
    rank stamped as ``doc_rank`` on copies of the node rows they own."""

    async def test_documents_rank_by_their_best_hit_then_expansion_only_last(
        self, mocker, make_collection, ranked_corpus, embedding_model
    ) -> None:
        hits = [_hit(ranked_corpus, "cB", 0.9), _hit(ranked_corpus, "cA", 0.5)]

        result = await _ranked_query(
            mocker, make_collection(ranked_corpus), hits, embedding_model
        )

        # Assert: B (0.9) before A (0.5); C, reached only by expansion, last.
        # Each chunk ranks with its document, the shared entity with the more
        # relevant of its two.
        assert _doc_ranks(result.nodes) == {
            "B": 1,
            "pB": 1,
            "cB": 1,
            "A": 2,
            "pA": 2,
            "cA": 2,
            "e-ab": 1,
            "C": 3,
        }

    async def test_no_provenance_nodes_and_every_edge_stay_unranked(
        self, mocker, make_collection, ranked_corpus, embedding_model
    ) -> None:
        hits = [_hit(ranked_corpus, "cB", 0.9), _hit(ranked_corpus, "cA", 0.5)]

        result = await _ranked_query(
            mocker, make_collection(ranked_corpus), hits, embedding_model
        )

        nodes = {row["_id"]: row for row in result.nodes}
        assert "e-none" in nodes
        assert "doc_rank" not in nodes["e-none"]
        assert result.edges
        assert all("doc_rank" not in edge for edge in result.edges)

    async def test_equal_scores_rank_by_id(
        self, mocker, make_collection, ranked_corpus, embedding_model
    ) -> None:
        hits = [_hit(ranked_corpus, "cB", 0.5), _hit(ranked_corpus, "cA", 0.5)]

        result = await _ranked_query(
            mocker, make_collection(ranked_corpus), hits, embedding_model
        )

        ranks = _doc_ranks(result.nodes)
        assert (ranks["A"], ranks["B"], ranks["C"]) == (1, 2, 3)

    async def test_a_hit_whose_sources_match_no_drawn_document_ranks_nothing(
        self, mocker, make_collection, ranked_corpus, embedding_model, make_entity_row
    ) -> None:
        # An entity hit owned only by a document that is NOT in the result.
        stray = _with_sources(make_entity_row(_USER, "e-stray"), _SRC_STUB)
        rows = [*ranked_corpus, stray]
        hits = [ScoredHit(doc=stray, score=0.99), _hit(rows, "cA", 0.5)]

        result = await _ranked_query(
            mocker, make_collection(rows), hits, embedding_model
        )

        # Assert: the stray hit lifts no document (A, the only one hit, is
        # first) and the stray entity itself stays unranked.
        nodes = {row["_id"]: row for row in result.nodes}
        assert nodes["A"]["doc_rank"] == 1
        assert "doc_rank" not in nodes["e-stray"]

    async def test_when_no_hit_maps_to_a_drawn_document_all_rank_by_id(
        self, mocker, make_collection, ranked_corpus, embedding_model, make_entity_row
    ) -> None:
        # Arrange: the only hit is owned by a document NOT drawn, yet it
        # reaches documents B and A through its own edges.
        stray = _with_sources(make_entity_row(_USER, "e-stray"), _SRC_STUB)
        rows = [
            *ranked_corpus,
            stray,
            _graph_edge("e-stray>B", "e-stray", "B", _SRC_STUB),
            _graph_edge("e-stray>A", "e-stray", "A", _SRC_STUB),
        ]

        result = await _ranked_query(
            mocker,
            make_collection(rows),
            [ScoredHit(doc=stray, score=0.9)],
            embedding_model,
        )

        # Assert: every drawn document is still ranked (the slider stays
        # usable), in `str(_id)` order; the stray entity stays unranked.
        assert _doc_ranks(result.nodes) == {"A": 1, "B": 2}

    async def test_a_document_with_a_stub_id_beside_the_real_one_still_maps(
        self, mocker, make_collection, ranked_corpus, embedding_model
    ) -> None:
        # B's row carries a latent stub id FIRST (167 found such rows live).
        rows = [
            {**row, "sources": [_SRC_STUB, _SRC_B]} if row["_id"] == "B" else row
            for row in ranked_corpus
        ]
        hits = [_hit(rows, "cB", 0.9), _hit(rows, "cA", 0.5)]

        result = await _ranked_query(
            mocker, make_collection(rows), hits, embedding_model
        )

        ranks = _doc_ranks(result.nodes)
        assert (ranks["B"], ranks["cB"], ranks["A"]) == (1, 1, 2)

    async def test_a_document_seed_maps_through_its_own_sources(
        self, mocker, make_collection, ranked_corpus, embedding_model
    ) -> None:
        hits = [_hit(ranked_corpus, "C", 0.9), _hit(ranked_corpus, "cA", 0.5)]

        result = await _ranked_query(
            mocker, make_collection(ranked_corpus), hits, embedding_model
        )

        ranks = _doc_ranks(result.nodes)
        assert (ranks["C"], ranks["A"]) == (1, 2)

    async def test_the_zero_hop_path_ranks_too(
        self, mocker, make_collection, ranked_corpus, embedding_model
    ) -> None:
        hits = [_hit(ranked_corpus, "cB", 0.9), _hit(ranked_corpus, "cA", 0.5)]

        result = await _ranked_query(
            mocker, make_collection(ranked_corpus), hits, embedding_model, max_hops=0
        )

        # Assert: the seeds' parents + the closure's documents, ranked; C is
        # unreachable without a hop.
        assert _doc_ranks(result.nodes) == {"B": 1, "pB": 1, "A": 2, "pA": 2}

    async def test_a_result_without_document_rows_is_left_unstamped(
        self, mocker, make_collection, make_entity_row, embedding_model
    ) -> None:
        entity = _with_sources(make_entity_row(_USER, "e1"), _SRC_A)

        result = await _ranked_query(
            mocker,
            make_collection([entity]),
            [ScoredHit(doc=entity, score=0.9)],
            embedding_model,
            max_hops=0,
        )

        assert [row["_id"] for row in result.nodes] == ["e1"]
        assert _doc_ranks(result.nodes) == {}

    async def test_stamps_copies_and_keeps_rows_and_order(
        self, mocker, make_collection, ranked_corpus, embedding_model
    ) -> None:
        collection = make_collection(ranked_corpus)
        hits = [_hit(ranked_corpus, "cB", 0.9), _hit(ranked_corpus, "cA", 0.5)]
        unranked = await expand_graph(
            _client(make_collection(ranked_corpus)),
            _DATABASE,
            ["pB", "pA"],
            _USER,
            max_hops=1,
        )

        result = await _ranked_query(mocker, collection, hits, embedding_model)

        # Assert: the same rows in the same order, each equal to the unranked
        # one but for a trailing doc_rank; the collection's rows untouched.
        assert [
            {k: v for k, v in row.items() if k != "doc_rank"} for row in result.nodes
        ] == unranked.nodes
        assert all(
            list(row)[-1] == "doc_rank" for row in result.nodes if "doc_rank" in row
        )
        assert result.edges == unranked.edges
        assert all("doc_rank" not in row for row in collection.rows)

    async def test_ranking_issues_no_extra_read(
        self, mocker, make_collection, ranked_corpus, embedding_model
    ) -> None:
        ranked = make_collection(ranked_corpus)
        plain = make_collection(ranked_corpus)
        hits = [_hit(ranked_corpus, "cB", 0.9), _hit(ranked_corpus, "cA", 0.5)]

        await _ranked_query(mocker, ranked, hits, embedding_model)
        await expand_graph(_client(plain), _DATABASE, ["pB", "pA"], _USER, max_hops=1)

        # Assert: the search leg is patched, so every read is expansion's own.
        assert ranked.find_filters == plain.find_filters
        assert ranked.pipelines == plain.pipelines

    async def test_expansion_still_starts_from_the_same_seed_ids(
        self, mocker, make_collection, ranked_corpus, embedding_model
    ) -> None:
        hits = [_hit(ranked_corpus, "cB", 0.9), _hit(ranked_corpus, "cA", 0.5)]
        expand = mocker.patch(
            "tree.memory.graph.retrieval.expand_graph",
            new_callable=AsyncMock,
            return_value=QueryResult(),
        )

        await _ranked_query(
            mocker, make_collection(ranked_corpus), hits, embedding_model
        )

        assert expand.await_args.args[2] == ["pB", "pA"]

    async def test_logs_the_relevance_rank_at_debug(
        self, mocker, make_collection, ranked_corpus, embedding_model, caplog
    ) -> None:
        hits = [_hit(ranked_corpus, "cB", 0.9), _hit(ranked_corpus, "cA", 0.5)]

        with caplog.at_level(logging.DEBUG, logger="tree.memory.graph.retrieval"):
            result = await _ranked_query(
                mocker, make_collection(ranked_corpus), hits, embedding_model
            )

        assert (
            f"Relevance rank: 3 document(s), 8 of {len(result.nodes)} node rows ranked"
            in caplog.messages
        )


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


def _part_of(source: str, target: str) -> dict:
    return _graph_edge(f"{source}>{target}", source, target, edge_type="part_of")


def _next(source: str, target: str) -> dict:
    return _graph_edge(f"{source}>{target}", source, target, edge_type="next")


@pytest.fixture(name="chunk_star_rows")
def _chunk_star_rows() -> list[dict]:
    """The shared star (``tests/unit/memory/conftest.py::chunk_star_rows``)."""

    return chunk_star_rows(_USER)


# The one-hop walk from p1 and c1b ($graphLookup is direction-consistent per
# pass, so c3a is reached through c1b's OUTGOING `next`, never from p1 alone).
_SEEDS = ["p1", "c1b"]
_HOP_NODE_IDS = {"p1", "p2", "doc1", "c1a", "c1b", "c3a"}
_HOP_EDGE_IDS = {"p1>doc1", "p1>p2", "c1a>p1", "c1b>p1", "c1b>c3a"}
_CLOSURE_EDGE_IDS = {"p2>doc1", "c3a>p3", "p3>doc1"}


async def _expand(collection, seeds: list = _SEEDS, max_hops: int = 1):
    return await expand_graph(
        _client(collection), _DATABASE, seeds, _USER, max_hops=max_hops
    )


def _child_counts(result) -> dict:
    return {
        node["_id"]: node["child_count"]
        for node in result.nodes
        if "child_count" in node
    }


class TestPartOfClosure:
    async def test_every_chunk_hangs_off_its_parent_and_document(
        self, make_collection, chunk_star_rows
    ) -> None:
        result = await _expand(make_collection(chunk_star_rows))

        node_ids = set(_ids(result.nodes))
        assert node_ids == _HOP_NODE_IDS | {"p3"}
        assert set(_ids(result.edges)) == _HOP_EDGE_IDS | _CLOSURE_EDGE_IDS
        up = {
            edge["source_node_id"]
            for edge in result.edges
            if edge["type"] == "part_of" and edge["target_node_id"] in node_ids
        }
        chunks = {node["_id"] for node in result.nodes if node["type"] == "chunk"}
        assert chunks <= up

    async def test_a_pulled_in_childs_parent_and_document_come_in_two_reads(
        self, make_collection, chunk_star_rows
    ) -> None:
        collection = make_collection(chunk_star_rows)

        await _expand(collection)

        # Assert: the hop's hydration, then exactly two closure reads — the
        # lookahead brings c3a's parent p3 AND p3's edge to doc1 in one pass.
        assert len(collection.find_filters) == 3

    async def test_closure_reads_are_batched_and_tenant_scoped_first(
        self, make_collection, chunk_star_rows
    ) -> None:
        collection = make_collection(chunk_star_rows)

        await _expand(collection)

        edge_read, node_read = collection.find_filters[1:]
        assert next(iter(edge_read)) == next(iter(node_read)) == "user_id"
        assert {k: v for k, v in edge_read.items() if k != "$or"} == {
            "user_id": _USER,
            "kind": "edge",
            "type": "part_of",
        }
        up, down = edge_read["$or"]
        assert set(up["source_node_id"]["$in"]) == {
            "p1",
            "p2",
            "c1a",
            "c1b",
            "c3a",
            "p3",
        }
        assert set(down["target_node_id"]["$in"]) == {"p1", "p2", "p3"}
        assert {k: v for k, v in node_read.items() if k != "_id"} == {
            "user_id": _USER,
            "kind": "node",
        }
        assert set(node_read["_id"]["$in"]) == {"p3"}

    async def test_a_result_without_chunks_issues_no_extra_read(
        self, make_collection, make_entity_row
    ) -> None:
        collection = make_collection(
            [
                make_entity_row(_USER, "e1"),
                make_entity_row(_USER, "e2", name="bob"),
                _graph_edge("e1>e2", "e1", "e2"),
            ]
        )

        result = await _expand(collection, ["e1"])

        assert set(_ids(result.nodes)) == {"e1", "e2"}
        assert len(collection.find_filters) == 1

    async def test_the_zero_hop_path_also_closes_the_chain(
        self, make_collection, chunk_star_rows
    ) -> None:
        collection = make_collection(chunk_star_rows)

        result = await _expand(collection, ["p2"], max_hops=0)

        assert _ids(result.nodes) == ["p2", "doc1"]
        assert _ids(result.edges) == ["p2>doc1"]
        assert len(collection.find_filters) == 3

    async def test_existing_rows_keep_their_order_and_closure_rows_follow(
        self, make_collection, chunk_star_rows
    ) -> None:
        collection = make_collection(chunk_star_rows)

        result = await _expand(collection)

        node_ids, edge_ids = _ids(result.nodes), _ids(result.edges)
        assert set(node_ids[:-1]) == _HOP_NODE_IDS
        assert node_ids[-1] == "p3"
        assert set(edge_ids[: len(_HOP_EDGE_IDS)]) == _HOP_EDGE_IDS
        assert set(edge_ids[len(_HOP_EDGE_IDS) :]) == _CLOSURE_EDGE_IDS
        assert len(edge_ids) == len(set(edge_ids))

    async def test_no_parent_with_a_drawn_child_or_no_child_is_stamped(
        self, make_collection, chunk_star_rows, make_child_row
    ) -> None:
        # p1: both children drawn; p2: no children; p3: c3a drawn, c3b not.
        rows = [
            *chunk_star_rows,
            make_child_row(_USER, "c3b", parent_id="p3", chunk_index=1),
            _part_of("c3b", "p3"),
        ]

        result = await _expand(make_collection(rows))

        assert _child_counts(result) == {}

    async def test_a_parent_whose_children_are_all_absent_reports_their_count(
        self, make_collection, chunk_star_rows, make_child_row
    ) -> None:
        rows = [
            *chunk_star_rows,
            make_child_row(_USER, "c2a", parent_id="p2"),
            make_child_row(_USER, "c2b", parent_id="p2", chunk_index=1),
            _part_of("c2a", "p2"),
            _part_of("c2b", "p2"),
        ]

        result = await _expand(make_collection(rows))

        # Assert: counted, never pulled in.
        assert _child_counts(result) == {"p2": 2}
        assert not {"c2a", "c2b"} & set(_ids(result.nodes))
        assert not {"c2a>p2", "c2b>p2"} & set(_ids(result.edges))

    async def test_stamps_a_copy_never_the_row_read(
        self, make_collection, chunk_star_rows, make_child_row
    ) -> None:
        rows = [
            *chunk_star_rows,
            make_child_row(_USER, "c2a", parent_id="p2"),
            _part_of("c2a", "p2"),
        ]
        collection = make_collection(rows)

        await _expand(collection)

        assert all("child_count" not in row for row in collection.rows)

    async def test_only_the_rows_the_closure_appended_are_marked(
        self, make_collection, chunk_star_rows
    ) -> None:
        result = await _expand(make_collection(chunk_star_rows))

        marked = [
            row["_id"] for row in result.nodes + result.edges if row.get(CLOSURE_ADDED)
        ]
        assert set(marked) == {"p3"} | _CLOSURE_EDGE_IDS

    async def test_ranked_rows_put_every_hop_row_before_the_closure_unmarked(
        self, make_collection, chunk_star_rows
    ) -> None:
        result = await _expand(make_collection(chunk_star_rows))

        rows = ranked_rows(result)

        # Assert: hop nodes, hop edges, closure nodes, closure edges — each in
        # its own order — and the transient marker never leaves this module.
        hop_nodes = [r["_id"] for r in result.nodes if not r.get(CLOSURE_ADDED)]
        hop_edges = [r["_id"] for r in result.edges if not r.get(CLOSURE_ADDED)]
        added_edges = [r["_id"] for r in result.edges if r.get(CLOSURE_ADDED)]
        assert _ids(rows) == [*hop_nodes, *hop_edges, "p3", *added_edges]
        assert all(CLOSURE_ADDED not in row for row in rows)

    async def test_logs_what_the_closure_added_at_debug(
        self, make_collection, chunk_star_rows, caplog
    ) -> None:
        caplog.set_level(logging.DEBUG, logger="tree.memory.graph.retrieval")

        await _expand(make_collection(chunk_star_rows))

        assert (
            "part_of closure: +1 node(s), +3 edge(s), 0 parent(s) with hidden children"
            in caplog.messages
        )


def _with_sources(row: dict, *sources: object) -> dict:
    """``row`` with its ``sources`` provenance set (the builders leave it out)."""

    return {**row, "sources": list(sources)}


def _graph_edge(
    edge_id: str,
    source: str,
    target: str,
    *sources: object,
    edge_type: str = "mentions",
) -> dict:
    return {
        "_id": edge_id,
        "user_id": _USER,
        "kind": "edge",
        "type": edge_type,
        "source_node_id": source,
        "target_node_id": target,
        "sources": list(sources),
    }


def _ids(rows: list[dict]) -> list:
    return [row["_id"] for row in rows]


def _rank_of(rows: list[dict]) -> dict:
    return {row["_id"]: row["doc_rank"] for row in rows}


@pytest.fixture
def seven_documents(make_document_row) -> list[dict]:
    """Seven document rows with mixed ISO ``properties.date`` strings.

    Most recent first: d-new (Z suffix), d-tie-a / d-tie-b (same instant,
    written with different offsets), d-naive (no offset = UTC), d-mid,
    d-old, d-oldest.
    """

    return [
        make_document_row(_USER, "d-mid", date="2026-05-01T00:00:00+00:00"),
        make_document_row(_USER, "d-tie-b", date="2026-08-01T12:00:00+00:00"),
        make_document_row(_USER, "d-oldest", date="2024-01-01"),
        make_document_row(_USER, "d-new", date="2026-09-30T08:00:00Z"),
        make_document_row(_USER, "d-tie-a", date="2026-08-01T14:00:00+02:00"),
        make_document_row(_USER, "d-old", date="2025-07-22T07:01:17.640000+00:00"),
        make_document_row(_USER, "d-naive", date="2026-07-01T00:00:00"),
    ]


class TestFakeCollectionProvenance:
    def test_a_row_with_a_hex_string_source_is_rejected(
        self, make_collection, make_entity_row
    ) -> None:
        """Guard: no fixture can build a row with string ``sources`` (task 167)."""

        src = PydanticObjectId()

        with pytest.raises(ValueError, match="non-ObjectId sources"):
            make_collection([_with_sources(make_entity_row(_USER, "e1"), str(src))])


class TestFetchFullGraph:
    """The **Full graph** (ADR-011 §7): the ``max_docs`` most-recent documents'
    subgraphs, every row stamped ``doc_rank``, in four batched reads."""

    async def test_keeps_the_most_recent_documents_ranked_from_one(
        self, make_collection, seven_documents
    ) -> None:
        collection = make_collection(seven_documents)

        result = await fetch_full_graph(
            _client(collection), _DATABASE, _USER, max_docs=3
        )

        # Assert: newest first; the two rows at the same instant tie-break on
        # ``_id`` ascending, so the order is deterministic.
        assert _ids(result.nodes) == ["d-new", "d-tie-a", "d-tie-b"]
        assert [row["doc_rank"] for row in result.nodes] == [1, 2, 3]

    async def test_ranks_all_documents_when_the_cap_exceeds_them(
        self, make_collection, seven_documents
    ) -> None:
        collection = make_collection(seven_documents)

        result = await fetch_full_graph(
            _client(collection), _DATABASE, _USER, max_docs=500
        )

        assert _ids(result.nodes) == [
            "d-new",
            "d-tie-a",
            "d-tie-b",
            "d-naive",
            "d-mid",
            "d-old",
            "d-oldest",
        ]

    async def test_a_missing_date_falls_back_to_created_at_then_ranks_last(
        self, make_collection, make_document_row
    ) -> None:
        collection = make_collection(
            [
                make_document_row(_USER, "d-neither", date=None),
                make_document_row(
                    _USER,
                    "d-naive-created",
                    date=None,
                    # A naive datetime is treated as UTC and must not raise.
                    created_at=datetime(2026, 3, 1, 9, 0),
                ),
                make_document_row(
                    _USER,
                    "d-unparsable",
                    date="last tuesday",
                    created_at=datetime(2026, 1, 1, tzinfo=UTC),
                ),
                make_document_row(_USER, "d-dated", date="2026-02-01T00:00:00+00:00"),
            ]
        )

        result = await fetch_full_graph(_client(collection), _DATABASE, _USER)

        assert _rank_of(result.nodes) == {
            "d-naive-created": 1,
            "d-dated": 2,
            "d-unparsable": 3,
            "d-neither": 4,
        }

    async def test_returns_the_subgraph_of_kept_documents_only(
        self,
        make_collection,
        make_document_row,
        make_parent_row,
        make_child_row,
        make_entity_row,
    ) -> None:
        new_src, mid_src, old_src = (
            PydanticObjectId(),
            PydanticObjectId(),
            PydanticObjectId(),
        )
        collection = make_collection(
            [
                make_document_row(_USER, "d1", date="2026-09-01", sources=[new_src]),
                make_document_row(_USER, "d2", date="2026-08-01", sources=[mid_src]),
                make_document_row(_USER, "d3", date="2026-07-01", sources=[old_src]),
                _with_sources(make_parent_row(_USER, "p1", document_id="d1"), new_src),
                _with_sources(make_child_row(_USER, "c1", parent_id="p1"), new_src),
                _with_sources(make_parent_row(_USER, "p3", document_id="d3"), old_src),
                # Shared across the newest and the oldest document.
                _with_sources(make_entity_row(_USER, "e-shared"), old_src, new_src),
                _with_sources(make_entity_row(_USER, "e-old-only"), old_src),
            ]
        )

        result = await fetch_full_graph(
            _client(collection), _DATABASE, _USER, max_docs=2
        )

        # Assert: d1's parent, child and entities come along; nothing that
        # belongs only to d3 (beyond the cap of 2) does.
        assert _rank_of(result.nodes) == {
            "d1": 1,
            "d2": 2,
            "p1": 1,
            "c1": 1,
            "e-shared": 1,
        }

    async def test_a_shared_entity_ranks_with_its_most_recent_document(
        self, make_collection, make_document_row, make_entity_row
    ) -> None:
        srcs = [PydanticObjectId() for _ in range(3)]
        collection = make_collection(
            [
                make_document_row(
                    _USER, f"d{i + 1}", date=f"2026-0{9 - i}-01", sources=[src]
                )
                for i, src in enumerate(srcs)
            ]
            + [_with_sources(make_entity_row(_USER, "e-shared"), srcs[2], srcs[0])]
        )

        result = await fetch_full_graph(_client(collection), _DATABASE, _USER)

        assert _rank_of(result.nodes)["e-shared"] == 1

    async def test_entities_carry_objectids_and_match_the_plain_objectid_in(
        self, make_collection, make_document_row, make_entity_row
    ) -> None:
        """Every ``memory`` row's ``sources`` holds Document ObjectIds (task 167),
        so one ObjectId ``$in`` returns the documents' entities too."""

        new_src, old_src = PydanticObjectId(), PydanticObjectId()
        collection = make_collection(
            [
                make_document_row(_USER, "d1", date="2026-09-01", sources=[new_src]),
                make_document_row(_USER, "d2", date="2026-08-01", sources=[old_src]),
                _with_sources(make_entity_row(_USER, "e-shared"), old_src, new_src),
                _with_sources(make_entity_row(_USER, "e-old"), old_src),
            ]
        )

        result = await fetch_full_graph(_client(collection), _DATABASE, _USER)

        assert _rank_of(result.nodes) == {"d1": 1, "d2": 2, "e-shared": 1, "e-old": 2}
        assert collection.find_filters[1]["sources"] == {"$in": [new_src, old_src]}

    async def test_edges_carry_a_rank_and_need_both_endpoints(
        self, make_collection, make_document_row, make_parent_row, make_entity_row
    ) -> None:
        new_src, old_src, beyond_src = (
            PydanticObjectId(),
            PydanticObjectId(),
            PydanticObjectId(),
        )
        collection = make_collection(
            [
                make_document_row(_USER, "d1", date="2026-09-01", sources=[new_src]),
                make_document_row(_USER, "d2", date="2026-08-01", sources=[old_src]),
                make_document_row(_USER, "d3", date="2026-01-01", sources=[beyond_src]),
                _with_sources(make_parent_row(_USER, "p1", document_id="d1"), new_src),
                _with_sources(make_parent_row(_USER, "p2", document_id="d2"), old_src),
                # Hand-added: no provenance, but an included edge reaches it.
                _with_sources(make_entity_row(_USER, "e-hand"), *[]),
                # Hand-added and nothing links it to a kept document.
                _with_sources(make_entity_row(_USER, "e-orphan"), *[]),
                # Extracted from d3 only, beyond the cap of 2.
                _with_sources(make_entity_row(_USER, "e-beyond"), beyond_src),
                _graph_edge("part1", "p1", "d1", new_src, edge_type="part_of"),
                _graph_edge("part2", "p2", "d2", old_src, edge_type="part_of"),
                _graph_edge("cross", "p2", "e-hand", old_src, new_src),
                _graph_edge("to-beyond", "p2", "e-beyond", old_src),
                _graph_edge("dangling", "p1", "ghost", new_src),
                _graph_edge("beyond", "e-beyond", "d3", beyond_src),
            ]
        )

        result = await fetch_full_graph(
            _client(collection), _DATABASE, _USER, max_docs=2
        )

        # Assert: edge rank = min over its kept provenance; an endpoint only an
        # edge reaches is hydrated with that edge's rank — unless an excluded
        # document owns it (e-beyond, d3), which drops that edge too; an edge
        # whose endpoint does not exist is dropped, so the payload
        # materialises no `unknown`.
        assert _rank_of(result.edges) == {"part1": 1, "part2": 2, "cross": 1}
        nodes = _rank_of(result.nodes)
        assert nodes["e-hand"] == 1
        assert "e-beyond" not in nodes
        assert "e-orphan" not in nodes
        assert "ghost" not in nodes
        assert "d3" not in nodes

    async def test_an_edge_shared_with_an_excluded_document_never_pulls_it_in(
        self, make_collection, make_document_row, make_parent_row, make_entity_row
    ) -> None:
        """Regression (QA 165): an edge whose ``sources`` name a kept AND an
        excluded document is read in step 3; step 4 must not hydrate the
        excluded document's rows through it."""

        a_src, b_src = PydanticObjectId(), PydanticObjectId()
        collection = make_collection(
            [
                make_document_row(_USER, "doc-a", date="2026-09-01", sources=[a_src]),
                make_document_row(_USER, "doc-b", date="2026-08-01", sources=[b_src]),
                _with_sources(make_parent_row(_USER, "pa", document_id="doc-a"), a_src),
                _with_sources(make_parent_row(_USER, "pb", document_id="doc-b"), b_src),
                _with_sources(make_entity_row(_USER, "e-b"), b_src),
                _with_sources(make_entity_row(_USER, "e-hand"), *[]),
                _graph_edge("pa-a", "pa", "doc-a", a_src, edge_type="part_of"),
                # B's star, but both documents' provenance on the edge rows.
                _graph_edge("pb-b", "pb", "doc-b", a_src, b_src, edge_type="part_of"),
                _graph_edge("pb-e", "pb", "e-b", a_src, b_src),
                _graph_edge("pa-hand", "pa", "e-hand", a_src),
            ]
        )

        result = await fetch_full_graph(
            _client(collection), _DATABASE, _USER, max_docs=1
        )

        assert _rank_of(result.nodes) == {"doc-a": 1, "pa": 1, "e-hand": 1}
        assert _rank_of(result.edges) == {"pa-a": 1, "pa-hand": 1}
        assert len(collection.find_filters) == 4

    async def test_a_no_provenance_edge_joins_included_nodes_only(
        self, make_collection, make_document_row, make_entity_row
    ) -> None:
        new_src, old_src, beyond_src = (
            PydanticObjectId(),
            PydanticObjectId(),
            PydanticObjectId(),
        )
        collection = make_collection(
            [
                make_document_row(_USER, "d1", date="2026-09-01", sources=[new_src]),
                make_document_row(_USER, "d2", date="2026-08-01", sources=[old_src]),
                make_document_row(_USER, "d3", date="2026-01-01", sources=[beyond_src]),
                _with_sources(make_entity_row(_USER, "e1"), new_src),
                _with_sources(make_entity_row(_USER, "e2"), old_src),
                _with_sources(make_entity_row(_USER, "e3"), beyond_src),
                _with_sources(make_entity_row(_USER, "e-orphan"), *[]),
                # Hand-made / merge edges carry no provenance at all.
                _graph_edge("same", "e1", "e2", edge_type="same_as"),
                _graph_edge("to-beyond", "e1", "e3", edge_type="same_as"),
                _graph_edge("to-orphan", "e2", "e-orphan", edge_type="same_as"),
                _graph_edge("dangling", "e1", "ghost", edge_type="same_as"),
            ]
        )

        result = await fetch_full_graph(
            _client(collection), _DATABASE, _USER, max_docs=2
        )

        # Assert: kept when both endpoints are already included, ranked with
        # the more recent one; it never hydrates an endpoint of its own.
        assert _rank_of(result.edges) == {"same": 1}
        nodes = _rank_of(result.nodes)
        assert "e3" not in nodes
        assert "e-orphan" not in nodes
        assert len(collection.find_filters) == 4

    @pytest.mark.parametrize("n_documents", [1, 7])
    async def test_reads_in_four_batched_finds_whatever_the_document_count(
        self, make_collection, make_document_row, make_entity_row, n_documents: int
    ) -> None:
        srcs = [PydanticObjectId() for _ in range(n_documents)]
        collection = make_collection(
            [
                make_document_row(
                    _USER, f"d{i}", date=f"2026-01-{i + 1:02d}", sources=[src]
                )
                for i, src in enumerate(srcs)
            ]
            + [
                _with_sources(make_entity_row(_USER, f"e{i}"), src)
                for i, src in enumerate(srcs)
            ]
            + [
                _graph_edge(f"m{i}", f"e{i}", f"d{i}", src)
                for i, src in enumerate(srcs)
            ]
        )

        await fetch_full_graph(
            _client(collection), _DATABASE, _USER, max_docs=n_documents
        )

        # Assert: documents -> nodes -> edges -> unreached endpoints, never one
        # read per document; every filter leads with the tenant.
        filters = collection.find_filters
        assert len(filters) == 4
        assert all(next(iter(f)) == "user_id" for f in filters)
        assert filters[0] == {"user_id": _USER, "kind": "node", "type": "document"}
        assert filters[1]["kind"] == "node"
        assert filters[2]["kind"] == "edge"
        assert filters[3]["kind"] == "node"
        assert filters[3]["_id"] == {"$in": []}
        # Node reads never haul vectors across the wire.
        projections = collection.find_projections
        assert [projections[i] for i in (0, 1, 3)] == [{"embedding": 0}] * 3
        # The provenance lists are exactly the kept ObjectIds, once each.
        assert sorted(filters[1]["sources"]["$in"]) == sorted(srcs)
        # Edges: the kept provenance, OR no provenance at all (hand-made rows).
        provenance_leg, no_provenance_leg = filters[2]["$or"]
        assert sorted(provenance_leg["sources"]["$in"]) == sorted(srcs)
        assert no_provenance_leg == {"sources": []}

    async def test_no_returned_node_carries_an_embedding(
        self, make_collection, make_document_row, make_child_row
    ) -> None:
        src = PydanticObjectId()
        collection = make_collection(
            [
                make_document_row(_USER, "d1", sources=[src]),
                _with_sources(make_child_row(_USER, "c1"), src),
            ]
        )

        result = await fetch_full_graph(_client(collection), _DATABASE, _USER)

        assert _ids(result.nodes) == ["d1", "c1"]
        assert all("embedding" not in row for row in result.nodes)

    async def test_a_document_without_provenance_is_still_ranked_and_embedded(
        self, make_collection, make_document_row
    ) -> None:
        collection = make_collection(
            [
                make_document_row(_USER, "d-bare", date="2026-09-01", sources=[]),
                make_document_row(_USER, "d1", date="2026-08-01"),
            ]
        )

        result = await fetch_full_graph(_client(collection), _DATABASE, _USER)

        assert _rank_of(result.nodes) == {"d-bare": 1, "d1": 2}

    async def test_max_docs_defaults_to_the_configured_cap(
        self, mocker, make_collection, seven_documents
    ) -> None:
        mocker.patch.object(app_config.query, "full_graph_max_docs", 2)
        collection = make_collection(seven_documents)

        result = await fetch_full_graph(_client(collection), _DATABASE, _USER)

        assert _ids(result.nodes) == ["d-new", "d-tie-a"]

    async def test_rows_are_stamped_on_copies(
        self, make_collection, make_document_row
    ) -> None:
        row = make_document_row(_USER, "d1")
        collection = make_collection([row])

        await fetch_full_graph(_client(collection), _DATABASE, _USER)

        assert "doc_rank" not in row

    async def test_logs_how_many_documents_were_embedded(
        self, make_collection, seven_documents, caplog
    ) -> None:
        collection = make_collection(seven_documents)

        with caplog.at_level(logging.INFO, logger="tree.memory.graph.retrieval"):
            await fetch_full_graph(_client(collection), _DATABASE, _USER, max_docs=3)

        assert (
            "Full graph: embedded 3 of 7 documents (most recent first) → 3 nodes, 0 edges"
            in caplog.messages
        )


def _empty_cursor():
    """An async-iterable that yields nothing (the aggregate returns a cursor)."""

    class _Cursor:
        async def __aiter__(self):
            return
            yield  # pragma: no cover - makes this an async generator

    return _Cursor()
