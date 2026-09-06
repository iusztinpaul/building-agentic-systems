"""In-memory shapes the clustering layer passes around (ADR-007 §1, §3, §4).

Three families live here:

* The recipe's output — :class:`ClusteringResult`, one label and one 2-D point
  per input row, in input order.
* The LLM contract — :class:`ClusterSummary`, what one ``generate_json`` call
  per **Memory cluster** is validated against. Its bounds are enforced here,
  once, so a chatty model produces a retry rather than a legend entry that
  wraps over the map.
* The read side of the **Embedding map** — :class:`MemoryClusterInfo`,
  :class:`MapPoint` and :class:`EmbeddingMap`, built from stored rows by
  ``clustering.store`` (#117) and rendered by ``visualize.embeddings`` (#118).
  The persisted shapes are :class:`tree.entities.clusters.MemoryCluster` and
  the ``cluster_id`` / ``viz`` fields of
  :class:`tree.entities.memory.MemoryEntry`; these are the transit twins, so a
  raw Mongo dict never reaches a renderer.
"""

from __future__ import annotations

from pydantic import BaseModel, Field, field_validator, model_validator

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


class ClusterSummary(BaseModel):
    """What ONE LLM call must return for a cluster (ADR-007 §4).

    The bounds are display constraints, not taste: ``label`` is a legend row
    beside a coloured dot, ``summary`` is a tooltip paragraph, and ``keywords``
    is the handful of terms a reader scans. A model that ignores them fails
    validation and the call is retried; if it keeps failing the cluster is
    stored fail-open as ``Cluster {id}`` with an empty summary.
    """

    label: str = Field(
        description="Cluster name, at most 6 whitespace-separated words, non-empty."
    )
    summary: str = Field(
        description=(
            "What the cluster's chunks are about, at most 100 words. May be "
            "empty — the fail-open fallback stores no summary."
        ),
    )
    keywords: list[str] = Field(
        description=(
            "3-5 terms, stripped and deduped case-insensitively (first spelling wins)."
        ),
    )

    @field_validator("label", mode="after")
    @classmethod
    def _check_label(cls, value: str) -> str:
        words = value.split()
        if not words:
            raise ValueError(
                "label must not be empty: an unnamed cluster has no legend entry "
                "(the fail-open fallback is 'Cluster {cluster_id}')"
            )
        if len(words) > _MAX_LABEL_WORDS:
            raise ValueError(
                f"label must be at most {_MAX_LABEL_WORDS} words; got "
                f"{len(words)}: {value!r}"
            )
        return " ".join(words)

    @field_validator("summary", mode="after")
    @classmethod
    def _check_summary(cls, value: str) -> str:
        words = value.split()
        if len(words) > _MAX_SUMMARY_WORDS:
            raise ValueError(
                f"summary must be at most {_MAX_SUMMARY_WORDS} words; got {len(words)}"
            )
        return value.strip()

    @field_validator("keywords", mode="after")
    @classmethod
    def _normalise_keywords(cls, value: list[str]) -> list[str]:
        """Strip, drop blanks, dedupe case-insensitively, THEN count.

        Counting first would accept ``["AI", "ai", "Ai"]`` as three keywords and
        render one term three times in the legend.
        """

        deduped: list[str] = []
        seen: set[str] = set()
        for keyword in value:
            stripped = keyword.strip()
            if not stripped or stripped.casefold() in seen:
                continue
            seen.add(stripped.casefold())
            deduped.append(stripped)

        if not _MIN_KEYWORDS <= len(deduped) <= _MAX_KEYWORDS:
            raise ValueError(
                f"keywords must be {_MIN_KEYWORDS}-{_MAX_KEYWORDS} distinct "
                f"non-empty terms after normalisation; got {len(deduped)}: "
                f"{deduped}"
            )
        return deduped


class MemoryClusterInfo(BaseModel):
    """One **Memory cluster** as the surfaces read it (a ``memory_clusters`` row).

    Flat where the row is nested (``centroid_x`` / ``centroid_y`` for the row's
    ``centroid {x, y}``): this is what a legend entry needs, not what Mongo
    stores.
    """

    cluster_id: int = Field(description="HDBSCAN label, >= 0 (noise has no row).")
    label: str = Field(description="LLM-written cluster name shown in the legend.")
    summary: str = Field(description="LLM-written description; empty on a failure.")
    keywords: list[str] = Field(description="3-5 LLM-written keywords.")
    size: int = Field(description="Child chunks assigned to this cluster in the run.")
    sample_chunk_ids: list[str] = Field(
        description="The <= 20 chunk _ids the summariser actually saw."
    )
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


class EmbeddingMap(BaseModel):
    """The whole picture one surface renders — read from storage, never computed.

    ``total_children`` and ``unclustered`` drive the warning contract
    (ADR-007 §8): the output starts with "N of M chunks have no cluster
    assignment (or a stale one)" whenever ``unclustered`` is non-zero.
    """

    run_id: str = Field(description="The Clustering run these points come from.")
    clusters: list[MemoryClusterInfo] = Field(
        description="One entry per non-noise cluster of that run (the legend)."
    )
    points: list[MapPoint] = Field(
        description="The drawn chunks: those carrying this run's coordinates."
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
