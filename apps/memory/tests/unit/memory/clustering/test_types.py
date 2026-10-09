"""The clustering layer's transit shapes (ADR-007 §1, §3).

Transit shapes only — the tests below pin the fields the surfaces read, so a
rename fails here rather than in a browser.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tree.memory.clustering.types import (
    ClusteringResult,
    EmbeddingMap,
    MapPoint,
    MemoryClusterInfo,
)


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


class TestMemoryClusterInfo:
    """The in-memory twin of a ``memory_clusters`` row."""

    def test_carries_the_fields_the_legend_and_writer_need(self) -> None:
        info = MemoryClusterInfo(
            cluster_id=3,
            label="Cluster 1",
            size=42,
            centroid_x=1.5,
            centroid_y=-2.5,
        )

        assert (info.cluster_id, info.label, info.size) == (3, "Cluster 1", 42)
        assert (info.centroid_x, info.centroid_y) == (1.5, -2.5)


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
            clustered=0,
            noise=0,
            plotted_documents=0,
            total_documents=2,
        )

        assert embedding_map.run_id == "run-1"
        assert (embedding_map.total_children, embedding_map.unclustered) == (10, 10)
        assert embedding_map.clusters == []
        assert embedding_map.points == []

    @pytest.mark.parametrize(
        ("counts", "message"),
        [
            # The plot is a subset of the run: 2 points, run says 1 clustered.
            ({"clustered": 1}, "<= clustered <= total_children"),
            # The run is a subset of the corpus.
            ({"clustered": 11}, "<= clustered <= total_children"),
            ({"noise": 6}, "noise must be between 0 and clustered"),
            ({"plotted_documents": 4}, "plotted_documents must be between"),
        ],
    )
    def test_rejects_counts_that_do_not_nest(
        self, counts: dict[str, int], message: str
    ) -> None:
        fields = {
            "run_id": "run-1",
            "clusters": [],
            "points": [
                MapPoint(
                    chunk_id=f"c{index}",
                    x=0.0,
                    y=0.0,
                    cluster_id=0,
                    title=None,
                    heading_path=[],
                    snippet="",
                )
                for index in range(2)
            ],
            "total_children": 10,
            "unclustered": 5,
            "clustered": 5,
            "noise": 0,
            "plotted_documents": 1,
            "total_documents": 3,
        }

        with pytest.raises(ValidationError, match=message):
            EmbeddingMap(**{**fields, **counts})


class TestEmbeddingMapWithoutARun:
    """``run_id=None``: nothing has 2D coordinates, so nothing is drawn."""

    def test_accepts_every_child_unclustered_and_no_points(self) -> None:
        embedding_map = EmbeddingMap(
            run_id=None,
            clusters=[],
            points=[],
            total_children=5,
            unclustered=5,
            clustered=0,
            noise=0,
            plotted_documents=1,
            total_documents=1,
        )

        assert embedding_map.unclustered == embedding_map.total_children

    def test_rejects_a_point_without_a_run(self) -> None:
        # Surfaces compute no coordinates: a point needs a run that stored it.
        with pytest.raises(ValidationError, match="no Clustering run"):
            EmbeddingMap(
                run_id=None,
                clusters=[],
                points=[
                    MapPoint(
                        chunk_id="c1",
                        x=0.0,
                        y=0.0,
                        cluster_id=-1,
                        title=None,
                        heading_path=[],
                        snippet="",
                    )
                ],
                total_children=1,
                unclustered=1,
                clustered=0,
                noise=0,
                plotted_documents=1,
                total_documents=1,
            )
