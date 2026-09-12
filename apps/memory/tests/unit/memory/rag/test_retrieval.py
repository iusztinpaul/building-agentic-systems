"""Unit tests for **Parent-document retrieval** (ADR-006 decision 2).

The hybrid search itself is covered in ``test_search.py``; here it is stubbed
with hand-scored hits so the grouping, the parent/document fetches and the
ranking are asserted on their own. The one end-to-end case (empty collection)
runs the real search against the fake collection.
"""

from __future__ import annotations

import logging

import pytest
from beanie import PydanticObjectId

from tree.entities.memory import MEMORY_COLLECTION
from tree.memory.rag.search import SearchUnavailableError
from tree.memory.rag.retrieval import (
    _CHILD_HITS_PER_PARENT,
    group_children_by_parent,
    retrieve_parents,
)
from tree.memory.rag.types import HybridSearchResult, ScoredHit
from tree.models.fake_model import FakeEmbeddingModel

_USER = PydanticObjectId("507f1f77bcf86cd799439011")
_DATABASE = "test_db"


@pytest.fixture
def embedding_model() -> FakeEmbeddingModel:
    return FakeEmbeddingModel(dimensions=4)


@pytest.fixture
def hits(make_child_row) -> list[ScoredHit]:
    """Two parents' worth of fused child hits: p1 <- c0/c1, p2 <- c2."""

    return [
        ScoredHit(
            doc=make_child_row(_USER, "c0", parent_id="p1", content="first"), score=0.9
        ),
        ScoredHit(
            doc=make_child_row(
                _USER, "c1", parent_id="p1", content="second", chunk_index=1
            ),
            score=0.8,
        ),
        ScoredHit(
            doc=make_child_row(_USER, "c2", parent_id="p2", content="third"), score=0.7
        ),
    ]


def _client(collection) -> dict:
    """The ``client[database][MEMORY_COLLECTION]`` chain, as plain dicts."""

    return {_DATABASE: {MEMORY_COLLECTION: collection}}


class TestGroupChildrenByParent:
    def test_groups_by_parent_id_best_score_first(self, hits) -> None:
        grouped = group_children_by_parent(hits)

        assert list(grouped) == ["p1", "p2"]
        assert [child.child_id for child in grouped["p1"]] == ["c0", "c1"]
        assert [child.score for child in grouped["p1"]] == [0.9, 0.8]

    def test_children_are_sorted_score_desc_within_a_parent(self, hits) -> None:
        # Arrange — feed the weaker child first; grouping must still rank.
        grouped = group_children_by_parent([hits[1], hits[0]])

        assert [child.child_id for child in grouped["p1"]] == ["c0", "c1"]

    def test_tied_children_keep_a_deterministic_order_in_either_input_order(
        self, make_child_row
    ) -> None:
        # #112 Issue 7: two children of ONE parent with byte-identical fused
        # scores used to come back in input order, so the CLI/MCP output
        # flipped between identical runs. ``child_id`` is the tiebreak.
        tied = [
            ScoredHit(doc=make_child_row(_USER, cid, parent_id="p1"), score=0.5)
            for cid in ("c1", "c0")
        ]

        forward = group_children_by_parent(tied)
        reversed_ = group_children_by_parent(list(reversed(tied)))

        assert [child.child_id for child in forward["p1"]] == ["c0", "c1"]
        assert [child.child_id for child in reversed_["p1"]] == ["c0", "c1"]

    def test_tied_parents_keep_a_deterministic_order_in_either_input_order(
        self, make_child_row
    ) -> None:
        tied = [
            ScoredHit(doc=make_child_row(_USER, f"c-{pid}", parent_id=pid), score=0.5)
            for pid in ("p2", "p1")
        ]

        forward = group_children_by_parent(tied)
        reversed_ = group_children_by_parent(list(reversed(tied)))

        assert list(forward) == ["p1", "p2"]
        assert list(reversed_) == ["p1", "p2"]

    def test_hit_without_parent_id_is_dropped_with_a_warning(
        self, make_child_row, caplog
    ) -> None:
        orphan = make_child_row(_USER, "c9")
        orphan["parent_id"] = None

        with caplog.at_level(logging.WARNING):
            grouped = group_children_by_parent([ScoredHit(doc=orphan, score=0.5)])

        assert grouped == {}
        assert "c9" in caplog.text


class TestRetrieveParents:
    async def test_returns_distinct_parents_with_their_matched_children(
        self,
        mocker,
        make_collection,
        make_parent_row,
        make_document_row,
        hits,
        embedding_model,
    ) -> None:
        collection = make_collection(
            [
                make_document_row(_USER, "doc1", title="Memory for AI Agents"),
                make_parent_row(_USER, "p1", content="parent one"),
                make_parent_row(_USER, "p2", content="parent two", chunk_index=1),
            ]
        )
        mocker.patch(
            "tree.memory.rag.retrieval.hybrid_search",
            return_value=HybridSearchResult(hits=hits),
            autospec=True,
        )

        result = await retrieve_parents(
            _client(collection), _DATABASE, "q", embedding_model, _USER
        )

        assert [parent.parent_id for parent in result.parents] == ["p1", "p2"]
        assert result.parents[0].score == 0.9
        assert [c.child_id for c in result.parents[0].matched_children] == ["c0", "c1"]
        assert [c.child_id for c in result.parents[1].matched_children] == ["c2"]
        assert result.parents[0].content == "parent one"

    async def test_document_metadata_comes_from_the_document_row(
        self,
        mocker,
        make_collection,
        make_parent_row,
        make_document_row,
        hits,
        embedding_model,
    ) -> None:
        # Arrange — the parent's denormalised title is STALE; the document row
        # is the source of truth for the result's metadata.
        collection = make_collection(
            [
                make_document_row(
                    _USER, "doc1", title="Memory for AI Agents", date="2026-09-05"
                ),
                make_parent_row(_USER, "p1", title="stale title"),
                make_parent_row(_USER, "p2", chunk_index=1),
            ]
        )
        mocker.patch(
            "tree.memory.rag.retrieval.hybrid_search",
            return_value=HybridSearchResult(hits=hits),
            autospec=True,
        )

        result = await retrieve_parents(
            _client(collection), _DATABASE, "q", embedding_model, _USER
        )

        document = result.parents[0].document
        assert document.document_id == "doc1"
        assert document.title == "Memory for AI Agents"
        assert document.source_uri == "file://doc1"
        assert document.date == "2026-09-05"

    async def test_missing_document_row_falls_back_to_the_chunk_metadata(
        self,
        mocker,
        make_collection,
        make_parent_row,
        hits,
        embedding_model,
        caplog,
    ) -> None:
        # Arrange — the ``document`` row is gone (half-deleted document) but the
        # parents survive, each carrying the loader's denormalised copy of the
        # document metadata. A valid hit must degrade to that copy, not drop.
        collection = make_collection(
            [
                make_parent_row(_USER, "p1", title="Memory for AI Agents"),
                make_parent_row(_USER, "p2", chunk_index=1),
            ]
        )
        mocker.patch(
            "tree.memory.rag.retrieval.hybrid_search",
            return_value=HybridSearchResult(hits=hits),
            autospec=True,
        )

        with caplog.at_level(logging.WARNING):
            result = await retrieve_parents(
                _client(collection), _DATABASE, "q", embedding_model, _USER
            )

        # Assert — both parents are still returned, their metadata read off the
        # chunk row, and the fallback is announced with a WARNING naming both
        # the missing document and the parent that fell back.
        assert [parent.parent_id for parent in result.parents] == ["p1", "p2"]
        document = result.parents[0].document
        assert document.document_id == "doc1"
        assert document.title == "Memory for AI Agents"
        assert document.source_uri == "file://doc1"
        assert document.source_type == "file"
        assert "doc1" in caplog.text
        assert "p1" in caplog.text

    async def test_requests_four_child_hits_per_requested_parent(
        self, mocker, make_collection, make_parent_row, embedding_model
    ) -> None:
        search = mocker.patch(
            "tree.memory.rag.retrieval.hybrid_search",
            return_value=HybridSearchResult(),
            autospec=True,
        )

        await retrieve_parents(
            _client(make_collection()), _DATABASE, "q", embedding_model, _USER, top_k=2
        )

        assert search.call_args.kwargs["limit"] == 2 * _CHILD_HITS_PER_PARENT == 8
        assert search.call_args.kwargs["node_filter"] == {
            "type": "chunk",
            "subtype": "child",
        }

    async def test_truncates_to_top_k_parents(
        self,
        mocker,
        make_collection,
        make_parent_row,
        make_document_row,
        make_child_row,
        embedding_model,
    ) -> None:
        # Arrange — eight distinct parents match; top_k=2 must cut to two.
        rows = [make_document_row(_USER, "doc1")]
        many_hits = []
        for i in range(8):
            rows.append(make_parent_row(_USER, f"p{i}", chunk_index=i))
            many_hits.append(
                ScoredHit(
                    doc=make_child_row(_USER, f"c{i}", parent_id=f"p{i}"),
                    score=1.0 - i / 10,
                )
            )
        mocker.patch(
            "tree.memory.rag.retrieval.hybrid_search",
            return_value=HybridSearchResult(hits=many_hits),
            autospec=True,
        )

        result = await retrieve_parents(
            _client(make_collection(rows)),
            _DATABASE,
            "q",
            embedding_model,
            _USER,
            top_k=2,
        )

        assert [parent.parent_id for parent in result.parents] == ["p0", "p1"]

    async def test_child_with_a_missing_parent_row_is_dropped_with_a_warning(
        self,
        mocker,
        make_collection,
        make_parent_row,
        make_document_row,
        hits,
        embedding_model,
        caplog,
    ) -> None:
        # Arrange — p1's row was deleted; p2's is intact.
        collection = make_collection(
            [make_document_row(_USER, "doc1"), make_parent_row(_USER, "p2")]
        )
        mocker.patch(
            "tree.memory.rag.retrieval.hybrid_search",
            return_value=HybridSearchResult(hits=hits),
            autospec=True,
        )

        with caplog.at_level(logging.WARNING):
            result = await retrieve_parents(
                _client(collection), _DATABASE, "q", embedding_model, _USER
            )

        assert [parent.parent_id for parent in result.parents] == ["p2"]
        assert "c0" in caplog.text
        assert "c1" in caplog.text

    async def test_parent_and_document_fetches_are_pinned_to_the_user(
        self,
        mocker,
        make_collection,
        make_parent_row,
        make_document_row,
        hits,
        embedding_model,
    ) -> None:
        collection = make_collection(
            [
                make_document_row(_USER, "doc1"),
                make_parent_row(_USER, "p1"),
                make_parent_row(_USER, "p2", chunk_index=1),
            ]
        )
        mocker.patch(
            "tree.memory.rag.retrieval.hybrid_search",
            return_value=HybridSearchResult(hits=hits),
            autospec=True,
        )

        await retrieve_parents(
            _client(collection), _DATABASE, "q", embedding_model, _USER
        )

        # One $in fetch for the parents, one for their documents — no per-hit
        # round trip.
        assert len(collection.find_filters) == 2
        for query in collection.find_filters:
            assert query["user_id"] == _USER
            assert query["kind"] == "node"
        assert set(collection.find_filters[0]["_id"]["$in"]) == {"p1", "p2"}
        assert collection.find_filters[1]["_id"]["$in"] == ["doc1"]

    async def test_empty_collection_returns_no_parents(
        self, make_collection, embedding_model
    ) -> None:
        result = await retrieve_parents(
            _client(make_collection()), _DATABASE, "q", embedding_model, _USER
        )

        assert result.parents == []


class TestNonPositiveTopK:
    """#112 Issue 6: ``top_k <= 0`` is a caller bug, not an outage.

    ``limit=0`` used to reach both search stages, both raised, and both logged
    "Vector search unavailable / Text search unavailable" — an on-call reader
    reasonably concluded Atlas was down.
    """

    @pytest.mark.parametrize("top_k", [0, -1])
    async def test_returns_no_parents_without_searching(
        self, mocker, embedding_model, top_k: int, caplog
    ) -> None:
        search = mocker.patch("tree.memory.rag.retrieval.hybrid_search", autospec=True)

        with caplog.at_level(logging.INFO):
            result = await retrieve_parents(
                _client(object()),
                _DATABASE,
                "q",
                embedding_model,
                _USER,
                top_k=top_k,
            )

        assert result.parents == []
        search.assert_not_called()
        assert "unavailable" not in caplog.text
        assert "top_k" in caplog.text


# ---------------------------------------------------------------------------
# Search mode (ADR-008 §3) — module-level: these assert the FIELD's journey
# from the hybrid search onto the result, not the grouping this file's classes
# cover.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("search_mode", ["hybrid", "text_only", "vector_only"])
async def test_search_mode_copied_onto_result(
    mocker,
    make_collection,
    make_parent_row,
    make_document_row,
    hits,
    embedding_model,
    search_mode: str,
) -> None:
    """A degraded seed search must stay visible on the parents it produced."""

    collection = make_collection(
        [
            make_document_row(_USER, "doc1"),
            make_parent_row(_USER, "p1"),
            make_parent_row(_USER, "p2", chunk_index=1),
        ]
    )
    mocker.patch(
        "tree.memory.rag.retrieval.hybrid_search",
        return_value=HybridSearchResult(hits=hits, search_mode=search_mode),
        autospec=True,
    )

    result = await retrieve_parents(
        _client(collection), _DATABASE, "q", embedding_model, _USER
    )

    assert result.parents
    assert result.search_mode == search_mode


async def test_search_mode_survives_a_degraded_search_with_no_hits(
    mocker, make_collection, embedding_model
) -> None:
    """Degraded AND empty is the case a caller must not read as "no matches"."""

    mocker.patch(
        "tree.memory.rag.retrieval.hybrid_search",
        return_value=HybridSearchResult(search_mode="text_only"),
        autospec=True,
    )

    result = await retrieve_parents(
        _client(make_collection()), _DATABASE, "q", embedding_model, _USER
    )

    assert result.parents == []
    assert result.search_mode == "text_only"


async def test_top_k_zero_keeps_default_mode(mocker, embedding_model) -> None:
    """The early return never searched, so it reports the ``hybrid`` default."""

    search = mocker.patch("tree.memory.rag.retrieval.hybrid_search", autospec=True)

    result = await retrieve_parents(
        _client(object()), _DATABASE, "q", embedding_model, _USER, top_k=0
    )

    assert result.search_mode == "hybrid"
    search.assert_not_called()


async def test_search_unavailable_error_propagates(mocker, embedding_model) -> None:
    """Both legs dead is an error, not an empty answer (the MCP boundary catches it)."""

    mocker.patch(
        "tree.memory.rag.retrieval.hybrid_search",
        side_effect=SearchUnavailableError(
            "vector and text search are both unavailable"
        ),
        autospec=True,
    )

    with pytest.raises(SearchUnavailableError):
        await retrieve_parents(
            _client(object()), _DATABASE, "q", embedding_model, _USER
        )
