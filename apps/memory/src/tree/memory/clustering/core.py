"""The clustering recipe itself: numpy in, labels and 2-D coordinates out.

The shape is BERTopic's, ported not imported (ADR-007 §1):

1. UMAP the raw embeddings down to ``config.umap.n_components`` (5) dimensions.
   HDBSCAN on raw 1024-d cosine space finds almost nothing — distance
   concentration flattens the density contrast a density clusterer needs.
2. HDBSCAN there. Noise is ``-1``.
3. A SEPARATE UMAP to 2-D, fit on the SAME raw embeddings with the same seed
   and knobs, for display only. Projecting the 5-d intermediate again would
   compound distortion and couple the picture to the clustering step; fitting
   both from the same input keeps them independent evidence for one cheap extra
   fit. And we never cluster on the 2-D picture: two dimensions destroy
   neighbourhood structure to make an image, so clusters read off it are
   artefacts of ``min_dist`` and the seed.

This module is PURE: no Prefect, no Mongo, no LLM, and no config *reads* — the
:class:`~tree.config.app_config.ClusteringConfig` is passed in, so a reader can
run the whole recipe from a notebook with a numpy array and a config object.

``umap`` and ``sklearn`` are imported INSIDE the two private helpers that use
them (ADR-007 §6). A module-level import would cost every non-clustering path
(the nightly run, the MCP server, every CLI) ~3 s warm and ~40 s on a fresh
machine, where numba compiles umap's kernels. The rule is AST-enforced in
``tests/unit/memory/test_package_layout.py::TestHeavyImportsAreLazy``.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

import numpy as np

from tree.config.app_config import ClusteringConfig
from tree.memory.clustering.types import ClusteringResult

logger = logging.getLogger(__name__)

NOISE_LABEL = -1
"""The ONE label meaning "not in any cluster" — HDBSCAN's ``-1``."""

_DEGENERATE_LABELS = (-2, -3)
"""scikit-learn's other outlier codes: ``-2`` = infinite values, ``-3`` = NaN.

Both mean the row could not be placed, which is what ``-1`` already means to
everything downstream (the writer, the palette, the legend). Keeping them apart
would add two grey "clusters" nobody can act on.
"""

_DISPLAY_COMPONENTS = 2
"""The Embedding map is a 2-D scatter (ADR-005 stands: no 3-D)."""


def reduce_and_cluster(
    embeddings: np.ndarray, config: ClusteringConfig
) -> ClusteringResult:
    """Run the full recipe on one user's child-chunk embeddings.

    Args:
        embeddings: Shape ``(n, d)``, one row per **Child chunk**, in the order
            the caller loaded them (``_id`` order) — the result is aligned to it.
        config: The ``memory.clustering`` block. Read, never loaded, here.

    Returns:
        One label and one 2-D point per input row.

    Raises:
        ValueError: If fewer than ``config.hdbscan.min_cluster_size`` rows were
            given (nothing could form a cluster), or if the 2-D fit produced
            non-finite coordinates.
    """

    row_count = len(embeddings)
    minimum = config.hdbscan.min_cluster_size
    # Checked BEFORE the helpers, so a tiny corpus never pays the lazy imports:
    # a 12-chunk memory should be told "nothing to cluster" in milliseconds, not
    # after a 40 s numba compile.
    if row_count < minimum:
        raise ValueError(
            f"Nothing to cluster: {row_count} embeddings < min_cluster_size "
            f"{minimum}. Embed more child chunks, or lower "
            "memory.clustering.hdbscan.min_cluster_size "
            "(TREE_MEMORY__CLUSTERING__HDBSCAN__MIN_CLUSTER_SIZE)."
        )

    logger.info(
        "Clustering %d embeddings (umap %d-d -> hdbscan min_cluster_size %d)",
        row_count,
        config.umap.n_components,
        minimum,
    )

    intermediate = _umap_reduce(
        embeddings, n_components=config.umap.n_components, config=config
    )
    raw_labels = _hdbscan_labels(intermediate, config)
    coords = _umap_reduce(embeddings, n_components=_DISPLAY_COMPONENTS, config=config)

    labels = [
        NOISE_LABEL if int(label) in _DEGENERATE_LABELS else int(label)
        for label in raw_labels
    ]
    _reject_non_finite(coords)

    logger.info(
        "Clustered %d embeddings into %d clusters (%d noise)",
        row_count,
        len(cluster_sizes(labels)),
        noise_count(labels),
    )
    return ClusteringResult(
        labels=labels,
        coords=[(float(x), float(y)) for x, y in coords],
    )


def _reject_non_finite(coords: np.ndarray) -> None:
    """Fail loudly on NaN/inf display coordinates.

    A NaN coordinate is not a drawable point, and it is not a JSON value either:
    ``json.dumps`` emits a bare ``NaN`` that the browser's ``JSON.parse``
    rejects, so the failure would surface as a blank map with a console error,
    hours later and far from here. One ``np.isfinite`` pass over ``(n, 2)`` buys
    a message that names the run's own step.
    """

    finite = np.isfinite(coords)
    if not finite.all():
        offending = int(np.count_nonzero(~finite.all(axis=1)))
        raise ValueError(
            f"The 2-D UMAP fit produced non-finite coordinates for {offending} "
            "of the embeddings; they cannot be drawn or stored. Check the input "
            "embeddings for NaN/inf values."
        )


def _umap_reduce(
    embeddings: np.ndarray, *, n_components: int, config: ClusteringConfig
) -> np.ndarray:
    """Project ``embeddings`` to ``n_components`` dimensions with UMAP.

    Used for BOTH fits — the clustering intermediate and the 2-D display map —
    with the same knobs and seed, so the picture and the clustering can never
    disagree about ``metric`` or ``random_state``.

    ``import umap`` is deliberately function-scope (ADR-007 §6).
    """

    import umap  # noqa: PLC0415 — lazy by design: ~3 s warm, ~40 s cold (numba)

    # umap-learn truncates n_neighbors to n-1 itself, with a UserWarning. Doing
    # it here keeps a small corpus a normal, silent run.
    n_neighbors = min(config.umap.n_neighbors, len(embeddings) - 1)
    reducer = umap.UMAP(
        n_neighbors=n_neighbors,
        n_components=n_components,
        min_dist=config.umap.min_dist,
        metric=config.umap.metric,
        random_state=config.umap.random_state,
        # Not a knob: umap-learn overrides n_jobs to 1 (with a warning) as soon
        # as random_state is set, and a reproducible map is the whole point.
        n_jobs=1,
    )
    return np.asarray(reducer.fit_transform(embeddings), dtype=float)


def _hdbscan_labels(reduced: np.ndarray, config: ClusteringConfig) -> np.ndarray:
    """Label the UMAP intermediate with density clustering.

    ``metric="euclidean"`` is fixed rather than taken from the config: the input
    is a UMAP embedding, and that space is euclidean whichever metric the
    projection was fitted with.

    ``copy=True`` is passed explicitly because scikit-learn 1.9 defaults it to
    ``"warn"`` (it becomes ``True`` in 1.10); the suite runs at zero warnings and
    the future default is the one we want anyway.

    ``from sklearn.cluster import HDBSCAN`` is deliberately function-scope
    (ADR-007 §6).
    """

    from sklearn.cluster import HDBSCAN  # noqa: PLC0415 — lazy by design

    clusterer = HDBSCAN(
        min_cluster_size=config.hdbscan.min_cluster_size,
        min_samples=config.hdbscan.min_samples,
        metric="euclidean",
        copy=True,
    )
    return clusterer.fit(reduced).labels_


def sample_cluster_members(
    embeddings: np.ndarray,
    labels: Sequence[int] | np.ndarray,
    cluster_id: int,
    *,
    nearest: int,
    random: int,
    seed: int,
) -> list[int]:
    """Pick the member rows one cluster is summarised from (ADR-007 §4).

    ``nearest`` most typical members (highest cosine similarity to the cluster's
    centroid in the ORIGINAL embedding space) plus ``random`` seeded draws from
    the rest: the head shows the LLM what the cluster IS, the tail shows how far
    it spreads. The RNG is seeded with ``seed + cluster_id`` so a re-run on an
    unchanged corpus samples the same chunks — the summary cache stays valid —
    while two clusters do not draw the same positions of their rankings.

    Args:
        embeddings: The SAME ``(n, d)`` array that was clustered.
        labels: One label per row of ``embeddings``.
        cluster_id: The cluster to sample. Noise (``-1``) is not a cluster.
        nearest: How many members nearest the centroid to take.
        random: How many further members to draw from the remainder.
        seed: Base seed, offset by ``cluster_id``.

    Returns:
        Row indices into ``embeddings``: the ``nearest`` head in similarity
        order followed by the random tail. A cluster with at most
        ``nearest + random`` members returns ALL of them, in similarity order.

    Raises:
        ValueError: For ``cluster_id == -1`` or a cluster id nothing carries.
    """

    if cluster_id == NOISE_LABEL:
        raise ValueError(
            f"Noise ({NOISE_LABEL}) is not a cluster: it has no centroid and "
            "never gets a summary or a memory_clusters row."
        )

    label_array = np.asarray(labels)
    members = np.flatnonzero(label_array == cluster_id)
    if members.size == 0:
        raise ValueError(
            f"No embedding carries cluster_id {cluster_id}; the labels hold "
            f"{sorted({int(label) for label in label_array})}."
        )

    vectors = np.asarray(embeddings, dtype=float)[members]
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    # A zero vector has no direction; dividing by 1.0 leaves it at similarity 0
    # instead of producing NaNs that would poison the whole ranking.
    unit = vectors / np.where(norms == 0.0, 1.0, norms)
    centroid = unit.mean(axis=0)
    similarity = unit @ centroid

    # Stable sort on the negated similarity: descending, ties broken by row
    # index, so the head is reproducible down to the tie order.
    ranked = members[np.argsort(-similarity, kind="stable")]
    if len(ranked) <= nearest + random:
        return [int(index) for index in ranked]

    rng = np.random.default_rng(seed + cluster_id)
    tail = rng.choice(ranked[nearest:], size=random, replace=False)
    return [int(index) for index in ranked[:nearest]] + [int(index) for index in tail]


def cluster_centroids_2d(
    coords: Sequence[tuple[float, float]] | np.ndarray,
    labels: Sequence[int] | np.ndarray,
) -> dict[int, tuple[float, float]]:
    """Mean **Embedding map** point per non-noise cluster — where the legend points.

    Noise is excluded: the mean of every unplaceable chunk is a point in the
    middle of the map that means nothing.
    """

    points = np.asarray(coords, dtype=float)
    label_array = np.asarray(labels)
    return {
        label: (
            float(points[label_array == label, 0].mean()),
            float(points[label_array == label, 1].mean()),
        )
        for label in _cluster_labels(label_array)
    }


def cluster_sizes(labels: Sequence[int] | np.ndarray) -> dict[int, int]:
    """Member count per non-noise cluster, by cluster id."""

    label_array = np.asarray(labels)
    return {
        label: int(np.count_nonzero(label_array == label))
        for label in _cluster_labels(label_array)
    }


def noise_count(labels: Sequence[int] | np.ndarray) -> int:
    """How many rows HDBSCAN refused to place (label ``-1``)."""

    return int(np.count_nonzero(np.asarray(labels) == NOISE_LABEL))


def _cluster_labels(label_array: np.ndarray) -> list[int]:
    """The distinct non-noise labels, ascending."""

    return sorted({int(label) for label in label_array} - {NOISE_LABEL})
