"""Unit tests for the rag **Memory structure** reader (``tree.memory.rag.structure``).

The pure halves (edge synthesis, recency ranking) are asserted on plain dicts;
the two readers run against ``FakeMemoryCollection`` (``tests/unit/memory/
conftest.py``), which records every ``find`` filter and projection, so "two
reads, both tenant-scoped, no vector" is a behavioural claim.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from beanie import PydanticObjectId

from tree.config.app_config import app_config
from tree.entities.memory import MEMORY_COLLECTION, EdgeType, build_edge_id
from tree.memory.rag.structure import (
    NO_EMBEDDING,
    UNDATED,
    as_utc,
    document_recency,
    fetch_rag_structure,
    fetch_retrieval_structure,
    rank_recent_documents,
    synthesize_part_of_edges,
)
from tree.memory.rag.types import (
    DocumentMeta,
    MatchedChild,
    MemoryStructure,
    RetrievalResult,
    RetrievedParent,
)

_USER = PydanticObjectId("507f1f77bcf86cd799439011")
_DATABASE = "test_db"


def _client(collection) -> dict:
    """The ``client[database][MEMORY_COLLECTION]`` chain, as plain dicts."""

    return {_DATABASE: {MEMORY_COLLECTION: collection}}


@pytest.fixture
def tree_rows(make_document_row, make_parent_row, make_child_row):
    """Factory: one document's rows — the document, its parents, their children.

    ``spec`` maps parent ids to child ids. Every chunk row carries the
    document's provenance (``sources``), exactly as ``rag.load`` writes it.
    """

    def _make(
        doc_id: str, spec: dict[str, list[str]], **document: Any
    ) -> list[dict[str, Any]]:
        doc = make_document_row(_USER, doc_id, **document)
        rows = [doc]
        for p_index, (parent_id, child_ids) in enumerate(spec.items()):
            rows.append(
                {
                    **make_parent_row(
                        _USER, parent_id, document_id=doc_id, chunk_index=p_index
                    ),
                    "sources": doc["sources"],
                }
            )
            rows.extend(
                {
                    **make_child_row(
                        _USER, child_id, parent_id=parent_id, chunk_index=c_index
                    ),
                    "sources": doc["sources"],
                }
                for c_index, child_id in enumerate(child_ids)
            )
        return rows

    return _make


class TestSynthesizePartOfEdges:
    def test_child_to_parent_and_parent_to_document(self, tree_rows) -> None:
        rows = tree_rows("doc1", {"p1": ["c1"]})

        edges = synthesize_part_of_edges(rows, _USER)

        assert [(e["source_node_id"], e["target_node_id"]) for e in edges] == [
            ("p1", "doc1"),
            ("c1", "p1"),
        ]
        assert [e["_id"] for e in edges] == [
            build_edge_id("p1", EdgeType.PART_OF, "doc1"),
            build_edge_id("c1", EdgeType.PART_OF, "p1"),
        ]
        assert {(e["kind"], e["type"], e["user_id"]) for e in edges} == {
            ("edge", "part_of", _USER)
        }

    def test_the_id_is_the_one_a_graphrag_run_writes(self) -> None:
        edges = synthesize_part_of_edges(
            [{"_id": "d"}, {"_id": "p", "parent_id": "d"}], _USER
        )

        assert edges[0]["_id"] == "p|part_of|d"

    def test_a_parent_id_outside_the_rows_yields_no_edge(self) -> None:
        # The renderer would otherwise draw an `unknown` endpoint.
        rows = [{"_id": "c1", "parent_id": "p-not-loaded"}]

        assert synthesize_part_of_edges(rows, _USER) == []

    def test_rows_without_a_parent_id_yield_no_edge(self, make_document_row) -> None:
        rows = [make_document_row(_USER, "doc1"), make_document_row(_USER, "doc2")]

        assert synthesize_part_of_edges(rows, _USER) == []

    def test_doc_rank_and_sources_are_copied_off_the_child(self) -> None:
        source = PydanticObjectId()
        rows = [
            {"_id": "d", "doc_rank": 1},
            {"_id": "p", "parent_id": "d", "doc_rank": 2, "sources": [source]},
        ]

        (edge,) = synthesize_part_of_edges(rows, _USER)

        assert (edge["doc_rank"], edge["sources"]) == (2, [source])

    def test_input_order_is_kept_and_a_second_call_is_identical(
        self, tree_rows
    ) -> None:
        rows = list(reversed(tree_rows("doc1", {"p1": ["c1", "c2"], "p2": ["c3"]})))

        first = synthesize_part_of_edges(rows, _USER)
        second = synthesize_part_of_edges(rows, _USER)

        assert [e["source_node_id"] for e in first] == ["c3", "p2", "c2", "c1", "p1"]
        assert first == second


class TestRankRecentDocuments:
    def test_an_iso_date_beats_created_at(self, make_document_row) -> None:
        old_dated = make_document_row(
            _USER, "a", date="2020-01-01", created_at=datetime(2026, 1, 1, tzinfo=UTC)
        )
        new_dated = make_document_row(_USER, "b", date="2025-01-01")

        ranked = rank_recent_documents([old_dated, new_dated], 10)

        assert [row["_id"] for row in ranked] == ["b", "a"]

    def test_created_at_is_the_fallback_and_undated_rows_rank_last(
        self, make_document_row
    ) -> None:
        undated = make_document_row(_USER, "a", date=None)
        created = make_document_row(
            _USER, "b", date=None, created_at=datetime(2024, 1, 1, tzinfo=UTC)
        )

        ranked = rank_recent_documents([undated, created], 10)

        assert [row["_id"] for row in ranked] == ["b", "a"]
        assert document_recency(undated) == UNDATED

    def test_ties_break_on_the_string_id(self, make_document_row) -> None:
        rows = [make_document_row(_USER, name) for name in ("c", "a", "b")]

        assert [row["_id"] for row in rank_recent_documents(rows, 10)] == [
            "a",
            "b",
            "c",
        ]

    def test_it_cuts_to_max_docs(self, make_document_row) -> None:
        rows = [
            make_document_row(_USER, f"d{day}", date=f"2026-01-0{day}")
            for day in range(1, 6)
        ]

        assert [row["_id"] for row in rank_recent_documents(rows, 2)] == [
            "d5",
            "d4",
        ]

    @pytest.mark.parametrize(
        "value",
        ["2026-07-01T00:00:00", datetime(2026, 7, 1)],  # noqa: DTZ001 — the input
        ids=["naive-iso", "naive-datetime"],
    )
    def test_a_naive_value_is_read_as_utc(self, value: Any) -> None:
        assert as_utc(value) == datetime(2026, 7, 1, tzinfo=UTC)

    def test_an_unparseable_date_is_none(self) -> None:
        assert as_utc("not a date") is None


class TestFetchRagStructure:
    async def test_two_reads_both_tenant_scoped_without_the_vector(
        self, make_collection, tree_rows
    ) -> None:
        collection = make_collection(tree_rows("doc1", {"p1": ["c1"]}))

        await fetch_rag_structure(_client(collection), _DATABASE, _USER)

        assert len(collection.find_filters) == 2
        assert all(f["user_id"] == _USER for f in collection.find_filters)
        assert collection.find_projections == [NO_EMBEDDING, NO_EMBEDDING]
        assert NO_EMBEDDING == {"embedding": 0}

    async def test_nodes_are_the_documents_parents_and_children(
        self, make_collection, tree_rows
    ) -> None:
        rows = tree_rows("doc1", {"p1": ["c1", "c2"], "p2": ["c3"]})
        rows += tree_rows("doc2", {"p3": ["c4"]}, date="2026-01-01")

        result = await fetch_rag_structure(
            _client(make_collection(rows)), _DATABASE, _USER
        )

        assert sorted(row["_id"] for row in result.nodes) == sorted(
            row["_id"] for row in rows
        )
        assert all("embedding" not in row for row in result.nodes)

    async def test_one_edge_per_chunk_row(self, make_collection, tree_rows) -> None:
        rows = tree_rows("doc1", {"p1": ["c1", "c2"], "p2": ["c3"]})

        result = await fetch_rag_structure(
            _client(make_collection(rows)), _DATABASE, _USER
        )

        assert len(result.edges) == 5  # 2 parents + 3 children
        assert {e["type"] for e in result.edges} == {"part_of"}

    async def test_doc_rank_is_one_based_newest_first_on_every_row(
        self, make_collection, tree_rows
    ) -> None:
        rows = tree_rows("old", {"p-old": ["c-old"]}, date="2025-01-01")
        rows += tree_rows("new", {"p-new": ["c-new"]}, date="2026-01-01")

        result = await fetch_rag_structure(
            _client(make_collection(rows)), _DATABASE, _USER
        )

        ranks = {row["_id"]: row["doc_rank"] for row in result.nodes}
        assert ranks == {
            "new": 1,
            "p-new": 1,
            "c-new": 1,
            "old": 2,
            "p-old": 2,
            "c-old": 2,
        }
        assert {e["source_node_id"]: e["doc_rank"] for e in result.edges} == {
            "p-new": 1,
            "c-new": 1,
            "p-old": 2,
            "c-old": 2,
        }

    async def test_max_docs_keeps_only_the_newest_documents_trees(
        self, make_collection, tree_rows
    ) -> None:
        rows = tree_rows("old", {"p-old": ["c-old"]}, date="2025-01-01")
        rows += tree_rows("new", {"p-new": ["c-new"]}, date="2026-01-01")

        result = await fetch_rag_structure(
            _client(make_collection(rows)), _DATABASE, _USER, max_docs=1
        )

        assert {row["_id"] for row in result.nodes} == {"new", "p-new", "c-new"}

    async def test_max_docs_defaults_to_the_configured_cap(
        self, mocker, make_collection, tree_rows
    ) -> None:
        mocker.patch.object(app_config.query, "full_graph_max_docs", 1)
        rows = tree_rows("old", {"p-old": []}, date="2025-01-01")
        rows += tree_rows("new", {"p-new": []}, date="2026-01-01")

        result = await fetch_rag_structure(
            _client(make_collection(rows)), _DATABASE, _USER
        )

        assert {row["_id"] for row in result.nodes} == {"new", "p-new"}

    async def test_another_users_rows_are_never_read(
        self, make_collection, tree_rows, make_document_row
    ) -> None:
        stranger = make_document_row(PydanticObjectId(), "theirs")

        result = await fetch_rag_structure(
            _client(make_collection([*tree_rows("doc1", {}), stranger])),
            _DATABASE,
            _USER,
        )

        assert [row["_id"] for row in result.nodes] == ["doc1"]

    async def test_an_empty_collection_is_an_empty_structure(
        self, make_collection
    ) -> None:
        result = await fetch_rag_structure(_client(make_collection()), _DATABASE, _USER)

        assert result == MemoryStructure()


def _retrieved(parent_id: str, document_id: str) -> RetrievedParent:
    return RetrievedParent(
        parent_id=parent_id,
        content="parent",
        score=0.03,
        document=DocumentMeta(document_id=document_id),
        matched_children=[MatchedChild(child_id="x", content="x", score=0.03)],
    )


class TestFetchRetrievalStructure:
    async def test_documents_rank_by_their_first_parents_position(
        self, make_collection, tree_rows
    ) -> None:
        rows = tree_rows("doc1", {"p1": ["c1"]}, date="2026-01-01")
        rows += tree_rows("doc2", {"p2": ["c2"], "p3": ["c3"]}, date="2025-01-01")
        retrieval = RetrievalResult(
            parents=[
                _retrieved("p2", "doc2"),
                _retrieved("p1", "doc1"),
                _retrieved("p3", "doc2"),
            ]
        )

        result = await fetch_retrieval_structure(
            _client(make_collection(rows)), _DATABASE, _USER, retrieval
        )

        ranks = {row["_id"]: row["doc_rank"] for row in result.nodes}
        assert ranks == {
            "doc2": 1,
            "doc1": 2,
            "p2": 1,
            "p1": 2,
            "p3": 1,
            "c1": 2,
            "c2": 1,
            "c3": 1,
        }
        assert [row["_id"] for row in result.nodes][:2] == ["doc2", "doc1"]

    async def test_every_child_of_a_retrieved_parent_is_read(
        self, make_collection, tree_rows
    ) -> None:
        # Only one child matched (`matched_children`), yet the view shows all
        # three — and nothing of the parent that was not retrieved.
        rows = tree_rows("doc1", {"p1": ["c1", "c2", "c3"], "p2": ["c4"]})
        retrieval = RetrievalResult(parents=[_retrieved("p1", "doc1")])

        result = await fetch_retrieval_structure(
            _client(make_collection(rows)), _DATABASE, _USER, retrieval
        )

        assert [row["_id"] for row in result.nodes] == ["doc1", "p1", "c1", "c2", "c3"]
        assert len(result.edges) == 4

    async def test_the_child_read_filters_on_subtype_and_parent_id(
        self, make_collection, tree_rows
    ) -> None:
        collection = make_collection(tree_rows("doc1", {"p1": ["c1"]}))
        retrieval = RetrievalResult(parents=[_retrieved("p1", "doc1")])

        await fetch_retrieval_structure(
            _client(collection), _DATABASE, _USER, retrieval
        )

        assert len(collection.find_filters) == 3
        assert collection.find_filters[-1] == {
            "user_id": _USER,
            "kind": "node",
            "type": "chunk",
            "subtype": "child",
            "parent_id": {"$in": ["p1"]},
        }
        assert all(f["user_id"] == _USER for f in collection.find_filters)
        assert collection.find_projections == [NO_EMBEDDING] * 3

    async def test_no_parent_is_an_empty_structure_and_no_read(
        self, make_collection
    ) -> None:
        collection = make_collection()

        result = await fetch_retrieval_structure(
            _client(collection), _DATABASE, _USER, RetrievalResult()
        )

        assert result == MemoryStructure()
        assert collection.find_filters == []
