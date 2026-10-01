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

from tree.config.app_config import app_config
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

    async def test_an_entity_whose_provenance_is_a_hex_string_still_belongs(
        self, make_collection, make_document_row, make_entity_row
    ) -> None:
        """Regression: ``add_entity`` writes ``sources`` as the hex STRING of the
        document id while document / chunk / edge rows hold the ObjectId, and
        Mongo's ``$in`` never matches one against the other."""

        new_src, old_src = PydanticObjectId(), PydanticObjectId()
        collection = make_collection(
            [
                make_document_row(_USER, "d1", date="2026-09-01", sources=[new_src]),
                make_document_row(_USER, "d2", date="2026-08-01", sources=[old_src]),
                _with_sources(
                    make_entity_row(_USER, "e-shared"), str(old_src), str(new_src)
                ),
            ]
        )

        result = await fetch_full_graph(_client(collection), _DATABASE, _USER)

        assert _rank_of(result.nodes)["e-shared"] == 1

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
                _with_sources(make_entity_row(_USER, "e-b"), str(b_src)),
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
                _with_sources(make_entity_row(_USER, "e1"), str(new_src)),
                _with_sources(make_entity_row(_USER, "e2"), str(old_src)),
                _with_sources(make_entity_row(_USER, "e3"), str(beyond_src)),
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
        # The provenance lists are exactly the kept ids, in both forms (an
        # entity's `sources` holds the hex string, see the regression above).
        expected = {src for src in srcs} | {str(src) for src in srcs}
        assert set(filters[1]["sources"]["$in"]) == expected
        # Edges: the kept provenance, OR no provenance at all (hand-made rows).
        provenance_leg, no_provenance_leg = filters[2]["$or"]
        assert set(provenance_leg["sources"]["$in"]) == expected
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
