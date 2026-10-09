"""In-memory shapes the clustering layer passes around (ADR-007 §1, §3, §4).

Two families live here:

* The recipe's output — :class:`ClusteringResult`, one label and one 2-D point
  per input row, in input order.
* The read side of the **Embedding map** — :class:`MemoryClusterInfo`,
  :class:`MapPoint` and :class:`EmbeddingMap`, built from stored rows by
  ``clustering.store`` (#117) and rendered by ``visualize.embeddings`` (#118).
  The persisted shapes are :class:`tree.entities.clusters.MemoryCluster` and
  the ``cluster_id`` / ``viz`` fields of
  :class:`tree.entities.memory.MemoryEntry`; these are the transit twins, so a
  raw Mongo dict never reaches a renderer.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, model_validator

_MAX_LABEL_WORDS = 6
_MAX_SUMMARY_WORDS = 100
_MIN_KEYWORDS = 3
_MAX_KEYWORDS = 5


class ClusteringResult(BaseModel):
    """One **Clustering run**'s two per-chunk outputs, aligned by position.

    ``labels[i]`` and ``coords[i]`` describe input row ``i`` — the caller keeps
    the row ids in the SAME order it passed the embeddings in (``_id`` order),
    so nothing here carries an id.
    """

    labels: list[int] = Field(
        description=(
            "HDBSCAN label per input row; -1 is noise (a chunk the algorithm "
            "refused to group). Never -2/-3: those are mapped to -1."
        ),
    )
    coords: list[tuple[float, float]] = Field(
        description=(
            "Embedding map (x, y) per input row, from the SEPARATE 2-D UMAP fit "
            "on the raw embeddings. Noise rows get coordinates too."
        ),
    )

    @model_validator(mode="after")
    def _check_equal_lengths(self) -> "ClusteringResult":
        """Both lists index the same rows; a mismatch mis-assigns coordinates.

        The writer zips ``(chunk_id, label, coord)`` — with unequal lengths that
        zip silently truncates, and chunks would keep a previous run's position.
        """

        if len(self.labels) != len(self.coords):
            raise ValueError(
                "labels and coords must describe the same rows: got "
                f"{len(self.labels)} labels and {len(self.coords)} coords"
            )
        return self


class MemoryClusterInfo(BaseModel):
    """One **Memory cluster** as the surfaces read it (a ``memory_clusters`` row).

    Flat where the row is nested (``centroid_x`` / ``centroid_y`` for the row's
    ``centroid {x, y}``): this is what a legend entry needs, not what Mongo
    stores.
    """

    cluster_id: int = Field(description="HDBSCAN label, >= 0 (noise has no row).")
    label: str = Field(
        description="Plain 'Cluster N' name (N = size rank) for the legend."
    )
    size: int = Field(description="Child chunks assigned to this cluster in the run.")
    centroid_x: float = Field(description="Cluster centre x in Embedding map space.")
    centroid_y: float = Field(description="Cluster centre y in Embedding map space.")


class MapPoint(BaseModel):
    """One drawn **Child chunk** of the **Embedding map**.

    Only chunks WITH coordinates from the latest run become points; unclustered
    or stale chunks are omitted and counted instead (ADR-007 §8).
    """

    chunk_id: str = Field(description="The child chunk row's _id.")
    x: float = Field(description="Stored viz.x — a fixed coordinate, never computed.")
    y: float = Field(description="Stored viz.y — a fixed coordinate, never computed.")
    cluster_id: int = Field(
        description="Its cluster, or -1 for noise (drawn mid-grey, no legend entry)."
    )
    title: str | None = Field(
        description="Source document title for the tooltip; None when unknown."
    )
    heading_path: list[str] = Field(
        description="The parent chunk's heading stack, outermost first."
    )
    snippet: str = Field(
        description="First 160 characters of properties.content, for the tooltip."
    )


class QueryMap(BaseModel):
    """A query's **Embedding map**: the matched chunks over a grey reference cloud.

    Both lists are drawn at the latest **Clustering run**'s stored UMAP
    coordinates — the whole-memory map's 2D space — so a match sits where the
    topic map puts it. Matches the run never placed are counted, not drawn.
    """

    matched: list[MapPoint] = Field(
        description="The child chunks the query's search matched (and that have a vector)."
    )
    reference: list[MapPoint] = Field(
        description=(
            "Every other embedded child of the plotted (most-recent) documents — "
            "drawn grey, as the space the matches sit in."
        )
    )
    matched_without_2d: int = Field(
        description=(
            "Matched chunks with no 2D coordinates from the latest run (ingested "
            "since, never clustered, or no run at all): counted, never drawn."
        )
    )
    plotted_documents: int = Field(
        description="Documents whose chunks form the reference cloud: the most-recent ones."
    )
    total_documents: int = Field(description="All documents of the user.")


class EmbeddingMap(BaseModel):
    """The whole picture one surface renders — read from storage.

    Two scopes, on purpose (ADR-013 §3): ``points`` are the PLOT — only the
    chunks of the ``plotted_documents`` most-recent documents
    (``query.full_graph_max_docs``) — while ``clusters``, ``clustered``,
    ``noise``, ``total_children`` and ``unclustered`` describe the WHOLE run.
    ``total_children`` and ``unclustered`` drive the warning contract (ADR-007
    §8): the output starts with "N/M chunks don’t have a 2D embedding. …"
    whenever ``unclustered`` is non-zero.

    ``run_id is None`` means no Clustering run yet: nothing has 2D
    coordinates, so there are no points and every embedded child is
    ``unclustered`` — a surface answers the missing-2D warning instead.
    """

    run_id: str | None = Field(
        description=(
            "The Clustering run these points come from; None when no run exists "
            "yet (no points, every child unclustered)."
        )
    )
    clusters: list[MemoryClusterInfo] = Field(
        description="One entry per non-noise cluster of that run (the legend)."
    )
    points: list[MapPoint] = Field(
        description=(
            "The drawn chunks: those carrying this run's coordinates whose "
            "document is among the plotted (most-recent) documents."
        )
    )
    total_children: int = Field(
        description="All embedded child chunks of the user (drawn or not)."
    )
    unclustered: int = Field(
        description=(
            "Embedded children with cluster_id None or viz.run_id != run_id: "
            "omitted from points and reported in the warning and the legend."
        ),
    )
    clustered: int = Field(
        description=(
            "Embedded children carrying this run's coordinates, run-wide — "
            "plotted or cut by the document cap (the summary's M)."
        ),
    )
    noise: int = Field(
        description="This run's noise children (cluster_id -1), run-wide."
    )
    plotted_documents: int = Field(
        description="Documents whose chunks are plotted: the most-recent ones."
    )
    total_documents: int = Field(description="All documents of the user.")

    @model_validator(mode="after")
    def _check_counts_fit_the_corpus(self) -> "EmbeddingMap":
        """Every count is a SUBSET count of the one it is compared against.

        The store computes ``unclustered`` as ``total_children - clustered``
        (never ``- len(points)``: the document cap would read as staleness), and
        the plot is a subset of the run, the run a subset of the corpus. A value
        out of order means two queries disagreed about what an embedded child
        chunk is — and the warning line would read "7 of 5 chunks have no
        cluster assignment", which reads to an operator as a broken map rather
        than a broken query.
        """

        if self.run_id is None:
            # No run: nothing is placed, so nothing is drawn or clustered.
            if self.clusters or self.points or self.clustered or self.noise:
                raise ValueError(
                    "a map with no Clustering run (run_id=None) has no clusters, "
                    f"points, clustered or noise: got {len(self.clusters)} "
                    f"clusters, {len(self.points)} points, "
                    f"clustered={self.clustered}, noise={self.noise}"
                )
            if self.unclustered != self.total_children:
                raise ValueError(
                    "with no Clustering run every child is unclustered: got "
                    f"unclustered={self.unclustered}, "
                    f"total_children={self.total_children}"
                )
        elif not len(self.points) <= self.clustered <= self.total_children:
            raise ValueError(
                "expected len(points) <= clustered <= total_children: got "
                f"{len(self.points)} points, clustered={self.clustered}, "
                f"total_children={self.total_children}"
            )
        if not 0 <= self.unclustered <= self.total_children:
            raise ValueError(
                "unclustered must be between 0 and total_children: got "
                f"unclustered={self.unclustered}, "
                f"total_children={self.total_children}"
            )
        if not 0 <= self.noise <= self.clustered:
            raise ValueError(
                "noise must be between 0 and clustered: got "
                f"noise={self.noise}, clustered={self.clustered}"
            )
        if not 0 <= self.plotted_documents <= self.total_documents:
            raise ValueError(
                "plotted_documents must be between 0 and total_documents: got "
                f"plotted_documents={self.plotted_documents}, "
                f"total_documents={self.total_documents}"
            )
        return self
