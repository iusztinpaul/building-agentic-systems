"""The clustering layer's transit shapes and the LLM contract (ADR-007 §1, §4).

:class:`ClusterSummary` is the only one of these that guards anything at run
time: it is what a Gemini call is validated against, so its bounds ("6 words",
"100 words", "3-5 keywords") are the difference between a legend that fits and
a legend that is a paragraph. The other models are transit shapes — the tests
below pin the fields the surfaces read, so a rename in #118 fails here rather
than in a browser.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tree.memory.clustering.types import (
    ClusteringResult,
    ClusterSummary,
    EmbeddingMap,
    MapPoint,
    MemoryClusterInfo,
)


def _summary(**overrides: object) -> ClusterSummary:
    """A valid :class:`ClusterSummary`, with one field swapped out."""

    fields: dict[str, object] = {
        "label": "Retrieval augmented generation",
        "summary": "Chunks about hybrid search and reranking.",
        "keywords": ["rag", "search", "reranking"],
    }
    fields.update(overrides)
    return ClusterSummary(**fields)  # type: ignore[arg-type]


class TestClusteringResult:
    """One label and one 2-D point per input row, in input order."""

    def test_accepts_matching_labels_and_coordinates(self) -> None:
        result = ClusteringResult(
            labels=[0, -1, 1], coords=[(0.0, 1.0), (2.0, 3.0), (4.0, 5.0)]
        )

        assert result.labels == [0, -1, 1]
        assert result.coords[1] == (2.0, 3.0)

    def test_rejects_a_length_mismatch(self) -> None:
        """The two lists are indexed together by every caller (row i -> labels[i],
        coords[i]); a mismatch would silently mis-assign coordinates."""

        with pytest.raises(ValidationError) as error:
            ClusteringResult(labels=[0], coords=[])

        assert "1" in str(error.value)
        assert "0" in str(error.value)

    def test_accepts_an_empty_result(self) -> None:
        assert ClusteringResult(labels=[], coords=[]).labels == []


class TestClusterSummaryLabel:
    """<= 6 whitespace-separated words, non-empty — it is a legend entry."""

    def test_accepts_six_words(self) -> None:
        assert _summary(label="a b c d e f").label == "a b c d e f"

    def test_rejects_seven_words(self) -> None:
        with pytest.raises(ValidationError) as error:
            _summary(label="a b c d e f g")

        assert "6 words" in str(error.value)

    @pytest.mark.parametrize("blank", ["", "   ", "\n\t"])
    def test_rejects_a_blank_label(self, blank: str) -> None:
        """An empty label renders as an unnamed legend row; the fail-open
        fallback is ``Cluster {id}`` (ADR-007 §4), never the empty string."""

        with pytest.raises(ValidationError):
            _summary(label=blank)

    def test_strips_surrounding_whitespace(self) -> None:
        assert _summary(label="  vector search  ").label == "vector search"


class TestClusterSummaryText:
    """<= 100 words — the tooltip/legend body, not an essay."""

    def test_accepts_one_hundred_words(self) -> None:
        assert len(_summary(summary=" ".join(["word"] * 100)).summary.split()) == 100

    def test_rejects_one_hundred_and_one_words(self) -> None:
        with pytest.raises(ValidationError) as error:
            _summary(summary=" ".join(["word"] * 101))

        assert "100 words" in str(error.value)

    def test_accepts_an_empty_summary(self) -> None:
        """The fail-open path stores an empty summary (ADR-007 §4)."""

        assert _summary(summary="").summary == ""


class TestClusterSummaryKeywords:
    """3-5 keywords, stripped and deduped case-insensitively."""

    def test_dedupes_case_insensitively_keeping_the_first_spelling(self) -> None:
        summary = _summary(keywords=["Ai", "ai", "ml", "nlp"])

        assert summary.keywords == ["Ai", "ml", "nlp"]

    def test_strips_and_drops_blank_keywords(self) -> None:
        summary = _summary(keywords=["  rag ", "", "search", "  ", "reranking"])

        assert summary.keywords == ["rag", "search", "reranking"]

    def test_rejects_fewer_than_three_keywords(self) -> None:
        with pytest.raises(ValidationError) as error:
            _summary(keywords=["x", "y"])

        assert "3" in str(error.value)

    def test_rejects_more_than_five_keywords(self) -> None:
        with pytest.raises(ValidationError):
            _summary(keywords=["a", "b", "c", "d", "e", "f"])

    def test_rejects_duplicates_that_leave_fewer_than_three(self) -> None:
        """The count is checked AFTER normalisation: three spellings of one word
        are one keyword, and the LLM is asked again rather than trusted."""

        with pytest.raises(ValidationError):
            _summary(keywords=["AI", "ai", "Ai"])


class TestMemoryClusterInfo:
    """The in-memory twin of a ``memory_clusters`` row."""

    def test_carries_the_fields_the_legend_and_writer_need(self) -> None:
        info = MemoryClusterInfo(
            cluster_id=3,
            label="vector search",
            summary="Chunks about ANN indexes.",
            keywords=["ann", "index", "search"],
            size=42,
            sample_chunk_ids=["u:chunk:1", "u:chunk:2"],
            centroid_x=1.5,
            centroid_y=-2.5,
        )

        assert (info.cluster_id, info.size) == (3, 42)
        assert (info.centroid_x, info.centroid_y) == (1.5, -2.5)
        assert info.sample_chunk_ids == ["u:chunk:1", "u:chunk:2"]


class TestMapPoint:
    """One drawn chunk of the **Embedding map**."""

    def test_accepts_a_point_without_a_document_title(self) -> None:
        """``title`` is optional: a chunk whose document row lost its title still
        gets drawn — the tooltip just omits the line."""

        point = MapPoint(
            chunk_id="u:chunk:1",
            x=0.5,
            y=-0.5,
            cluster_id=-1,
            title=None,
            heading_path=[],
            snippet="",
        )

        assert point.title is None
        assert point.cluster_id == -1

    def test_carries_the_tooltip_fields(self) -> None:
        point = MapPoint(
            chunk_id="u:chunk:2",
            x=1.0,
            y=2.0,
            cluster_id=0,
            title="ADR-007",
            heading_path=["Memory", "Clustering"],
            snippet="a" * 160,
        )

        assert point.heading_path == ["Memory", "Clustering"]
        assert len(point.snippet) == 160


class TestEmbeddingMap:
    """What the MCP tool and the CLI render — read, never computed."""

    def test_carries_the_run_the_counts_and_both_collections(self) -> None:
        embedding_map = EmbeddingMap(
            run_id="run-1",
            clusters=[],
            points=[],
            total_children=10,
            unclustered=10,
        )

        assert embedding_map.run_id == "run-1"
        assert (embedding_map.total_children, embedding_map.unclustered) == (10, 10)
        assert embedding_map.clusters == []
        assert embedding_map.points == []
