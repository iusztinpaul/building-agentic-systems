"""The pure clustering recipe: UMAP -> HDBSCAN -> a SEPARATE 2-D UMAP (ADR-007 §1).

Almost every test here runs with FAKE reducers/clusterers, for two reasons.
First speed: one real ``import umap`` compiles numba kernels (~40 s cold, ~3 s
warm), which a unit suite cannot pay per test. Second, and more important, the
claims worth pinning are OURS, not the libraries': that the 2-D fit sees the RAW
embeddings (not the 5-d intermediate), that ``n_neighbors`` is clamped before
umap-learn truncates it with a warning, that ``-2``/``-3`` (inf/NaN rows) come
back as plain noise, and that a corpus below ``min_cluster_size`` is refused
BEFORE anything heavy is imported. Fakes make those visible; the real libraries
would hide them behind their own behaviour.

One ``@pytest.mark.slow`` test does run the real stack end to end, on three
Gaussian blobs, to prove the recipe finds what it should and is reproducible.
"""

from __future__ import annotations

import sys
import time
from types import ModuleType
from typing import Any

import numpy as np
import pytest

from tree.config.app_config import (
    ClusteringConfig,
    HdbscanConfig,
    UmapConfig,
)
from tree.memory.clustering import core
from tree.memory.clustering.core import (
    cluster_centroids_2d,
    cluster_sizes,
    noise_count,
    reduce_and_cluster,
    sample_cluster_members,
)


def _config(
    *,
    n_neighbors: int = 15,
    n_components: int = 5,
    min_cluster_size: int = 15,
    min_samples: int | None = None,
) -> ClusteringConfig:
    return ClusteringConfig(
        umap=UmapConfig(
            n_neighbors=n_neighbors,
            min_dist=0.0,
            metric="cosine",
            n_components=n_components,
            random_state=42,
        ),
        hdbscan=HdbscanConfig(
            min_cluster_size=min_cluster_size, min_samples=min_samples
        ),
    )


class _FakeReducer:
    """Stands in for ``_umap_reduce``: keeps the first ``n_components`` columns.

    Trivially deterministic and shape-correct, so a test can assert exactly WHAT
    each fit was handed — which is the property ADR-007 §1 cares about.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[np.ndarray, int]] = []

    def __call__(
        self, embeddings: np.ndarray, *, n_components: int, config: ClusteringConfig
    ) -> np.ndarray:
        self.calls.append((np.array(embeddings), n_components))
        return np.asarray(embeddings, dtype=float)[:, :n_components]


class _FakeClusterer:
    """Stands in for ``_hdbscan_labels``: returns a fixed label per row."""

    def __init__(self, labels: np.ndarray | None = None) -> None:
        self.labels = labels
        self.calls: list[np.ndarray] = []

    def __call__(self, reduced: np.ndarray, config: ClusteringConfig) -> np.ndarray:
        self.calls.append(np.array(reduced))
        if self.labels is not None:
            return self.labels
        return np.arange(len(reduced)) // 10


class _RecordingUmap:
    """A fake ``umap.UMAP`` class that records the kwargs it was constructed with."""

    def __init__(self) -> None:
        self.kwargs: dict[str, Any] = {}
        self.fitted: np.ndarray | None = None

    def __call__(self, **kwargs: Any) -> "_RecordingUmap":
        self.kwargs = kwargs
        return self

    def fit_transform(self, embeddings: np.ndarray) -> np.ndarray:
        self.fitted = np.array(embeddings)
        return np.zeros((len(embeddings), self.kwargs["n_components"]))


@pytest.fixture
def fake_umap_module(monkeypatch: pytest.MonkeyPatch) -> _RecordingUmap:
    """Inject a fake ``umap`` module so ``_umap_reduce``'s lazy import finds it.

    The lazy import is the point: the real module never enters this process, so
    the test costs milliseconds instead of seconds.
    """

    recorder = _RecordingUmap()
    module = ModuleType("umap")
    module.UMAP = recorder  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "umap", module)
    return recorder


@pytest.fixture
def fake_sklearn_cluster(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Inject a fake ``sklearn.cluster`` exposing a recording ``HDBSCAN``."""

    recorded: dict[str, Any] = {}

    class FakeHDBSCAN:
        def __init__(self, **kwargs: Any) -> None:
            recorded["kwargs"] = kwargs

        def fit(self, data: np.ndarray) -> "FakeHDBSCAN":
            recorded["fitted"] = np.array(data)
            self.labels_ = np.zeros(len(data), dtype=int)
            return self

    cluster_module = ModuleType("sklearn.cluster")
    cluster_module.HDBSCAN = FakeHDBSCAN  # type: ignore[attr-defined]
    sklearn_module = ModuleType("sklearn")
    sklearn_module.cluster = cluster_module  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "sklearn", sklearn_module)
    monkeypatch.setitem(sys.modules, "sklearn.cluster", cluster_module)
    return recorded


class TestReduceAndClusterWiring:
    """The three steps, and WHAT each one is fed (ADR-007 §1)."""

    def test_returns_one_label_and_one_2d_point_per_row(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        embeddings = np.arange(30 * 8, dtype=float).reshape(30, 8)
        reducer, clusterer = _FakeReducer(), _FakeClusterer()
        monkeypatch.setattr(core, "_umap_reduce", reducer)
        monkeypatch.setattr(core, "_hdbscan_labels", clusterer)

        result = reduce_and_cluster(embeddings, _config(min_cluster_size=15))

        assert result.labels == [0] * 10 + [1] * 10 + [2] * 10
        assert result.coords == [(row[0], row[1]) for row in embeddings]

    def test_fits_umap_twice_on_the_raw_embeddings(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Step C is a SEPARATE 2-D fit on the RAW input, not a projection of the
        5-d intermediate: a projection of a projection compounds distortion and
        couples the picture to the clustering step (ADR-007 §1)."""

        embeddings = np.arange(30 * 8, dtype=float).reshape(30, 8)
        reducer, clusterer = _FakeReducer(), _FakeClusterer()
        monkeypatch.setattr(core, "_umap_reduce", reducer)
        monkeypatch.setattr(core, "_hdbscan_labels", clusterer)

        reduce_and_cluster(embeddings, _config(n_components=5))

        assert [n_components for _, n_components in reducer.calls] == [5, 2]
        for fitted, _ in reducer.calls:
            assert fitted.shape == (30, 8)
            assert np.array_equal(fitted, embeddings)

    def test_clusters_the_intermediate_space_not_the_picture(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """HDBSCAN sees the ``n_components``-d intermediate, never the 2-D map."""

        embeddings = np.arange(30 * 8, dtype=float).reshape(30, 8)
        reducer, clusterer = _FakeReducer(), _FakeClusterer()
        monkeypatch.setattr(core, "_umap_reduce", reducer)
        monkeypatch.setattr(core, "_hdbscan_labels", clusterer)

        reduce_and_cluster(embeddings, _config(n_components=5))

        assert len(clusterer.calls) == 1
        assert clusterer.calls[0].shape == (30, 5)

    def test_returns_plain_python_scalars(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``np.int64`` / ``np.float32`` survive pydantic but not the BSON writer
        and ``json.dumps`` in the renderer, so they are converted here, once."""

        embeddings = np.arange(30 * 8, dtype=np.float32).reshape(30, 8)
        monkeypatch.setattr(core, "_umap_reduce", _FakeReducer())
        monkeypatch.setattr(core, "_hdbscan_labels", _FakeClusterer())

        result = reduce_and_cluster(embeddings, _config())

        assert type(result.labels[0]) is int
        assert type(result.coords[0][0]) is float


class TestReduceAndClusterRefusesTinyCorpora:
    """Fewer rows than ``min_cluster_size``: refuse, and import nothing."""

    def test_raises_naming_both_numbers(self, monkeypatch: pytest.MonkeyPatch) -> None:
        reducer, clusterer = _FakeReducer(), _FakeClusterer()
        monkeypatch.setattr(core, "_umap_reduce", reducer)
        monkeypatch.setattr(core, "_hdbscan_labels", clusterer)

        with pytest.raises(ValueError) as error:
            reduce_and_cluster(np.zeros((14, 8)), _config(min_cluster_size=15))

        message = str(error.value)
        assert "14" in message
        assert "15" in message

    def test_neither_helper_runs(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The refusal happens BEFORE the lazy imports — a 12-chunk corpus must
        not pay the ~40 s cold numba compile to be told there is nothing to do."""

        reducer, clusterer = _FakeReducer(), _FakeClusterer()
        monkeypatch.setattr(core, "_umap_reduce", reducer)
        monkeypatch.setattr(core, "_hdbscan_labels", clusterer)

        with pytest.raises(ValueError):
            reduce_and_cluster(np.zeros((14, 8)), _config(min_cluster_size=15))

        assert reducer.calls == []
        assert clusterer.calls == []


class TestDegenerateLabels:
    """``-2`` (infinite values) and ``-3`` (NaN) are noise like any other."""

    def test_minus_two_and_minus_three_become_minus_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Downstream only knows two kinds of chunk: in a cluster, or noise. The
        three sklearn noise codes would otherwise leak into ``cluster_id`` and
        the palette as two extra 'clusters'."""

        labels = np.array([-2, -3, 0, 0, -1] * 4)
        monkeypatch.setattr(core, "_umap_reduce", _FakeReducer())
        monkeypatch.setattr(core, "_hdbscan_labels", _FakeClusterer(labels=labels))

        result = reduce_and_cluster(np.zeros((20, 8)), _config())

        assert set(result.labels) == {-1, 0}
        assert result.labels[:5] == [-1, -1, 0, 0, -1]


class TestNonFiniteCoordinates:
    """A NaN coordinate is not a drawable point — fail loudly, not silently.

    ``json.dumps`` writes bare ``NaN``, which is invalid JSON: the map would
    reach the browser and die in the renderer with no hint of where the value
    came from. The check costs one ``np.isfinite`` pass over ``(n, 2)``.
    """

    def test_reduce_and_cluster_rejects_a_non_finite_projection(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        coords = np.zeros((20, 2))
        coords[3, 1] = np.nan
        coords[7, 0] = np.inf

        def reducer(
            embeddings: np.ndarray, *, n_components: int, config: ClusteringConfig
        ) -> np.ndarray:
            return coords if n_components == 2 else np.zeros((len(embeddings), 5))

        monkeypatch.setattr(core, "_umap_reduce", reducer)
        monkeypatch.setattr(core, "_hdbscan_labels", _FakeClusterer())

        with pytest.raises(ValueError) as error:
            reduce_and_cluster(np.zeros((20, 8)), _config())

        assert "2" in str(error.value)
        assert "finite" in str(error.value)


class TestUmapReduce:
    """What umap-learn is actually constructed with (the lazy-import boundary)."""

    def test_clamps_n_neighbors_to_n_minus_one_on_a_small_corpus(
        self, fake_umap_module: _RecordingUmap
    ) -> None:
        """umap-learn truncates to ``n - 1`` itself, with a UserWarning. We clamp
        first so a small corpus is a normal run, not a warning to explain."""

        core._umap_reduce(np.zeros((8, 4)), n_components=5, config=_config())

        assert fake_umap_module.kwargs["n_neighbors"] == 7

    def test_keeps_the_configured_n_neighbors_when_the_corpus_is_large(
        self, fake_umap_module: _RecordingUmap
    ) -> None:
        core._umap_reduce(np.zeros((100, 4)), n_components=5, config=_config())

        assert fake_umap_module.kwargs["n_neighbors"] == 15

    def test_forwards_the_configured_knobs_and_pins_single_threaded(
        self, fake_umap_module: _RecordingUmap
    ) -> None:
        """``n_jobs=1`` is not a knob: umap-learn silently overrides any other
        value once ``random_state`` is set, and reproducibility is the point."""

        core._umap_reduce(np.zeros((100, 4)), n_components=2, config=_config())

        assert fake_umap_module.kwargs == {
            "n_neighbors": 15,
            "n_components": 2,
            "min_dist": 0.0,
            "metric": "cosine",
            "random_state": 42,
            "n_jobs": 1,
        }

    def test_fits_on_the_embeddings_it_was_given(
        self, fake_umap_module: _RecordingUmap
    ) -> None:
        embeddings = np.arange(40 * 4, dtype=float).reshape(40, 4)

        reduced = core._umap_reduce(embeddings, n_components=3, config=_config())

        assert np.array_equal(fake_umap_module.fitted, embeddings)
        assert reduced.shape == (40, 3)


class TestHdbscanLabels:
    """What scikit-learn is actually constructed with."""

    def test_forwards_the_configured_knobs(
        self, fake_sklearn_cluster: dict[str, Any]
    ) -> None:
        """``metric='euclidean'`` is fixed: the input is a UMAP embedding, whose
        space is euclidean whatever metric the projection was fitted with.

        ``copy=True`` is explicit to avoid scikit-learn's ``copy='warn'``
        FutureWarning — the suite runs at zero warnings, and silencing it with a
        filter would hide the real behaviour change in 1.10.
        """

        core._hdbscan_labels(
            np.zeros((20, 5)), _config(min_cluster_size=7, min_samples=3)
        )

        assert fake_sklearn_cluster["kwargs"] == {
            "min_cluster_size": 7,
            "min_samples": 3,
            "metric": "euclidean",
            "copy": True,
        }

    def test_fits_on_the_reduced_space_and_returns_labels(
        self, fake_sklearn_cluster: dict[str, Any]
    ) -> None:
        reduced = np.arange(20 * 5, dtype=float).reshape(20, 5)

        labels = core._hdbscan_labels(reduced, _config())

        assert np.array_equal(fake_sklearn_cluster["fitted"], reduced)
        assert labels.shape == (20,)


def _members_in_similarity_order(
    embeddings: np.ndarray, labels: np.ndarray, cluster_id: int
) -> list[int]:
    """The expected ranking, computed independently of the implementation."""

    members = [i for i, label in enumerate(labels) if label == cluster_id]
    unit = [embeddings[i] / np.linalg.norm(embeddings[i]) for i in members]
    centroid = np.mean(unit, axis=0)
    similarity = {
        i: float(np.dot(vector, centroid)) for i, vector in zip(members, unit)
    }
    return sorted(members, key=lambda index: (-similarity[index], index))


@pytest.fixture
def clustered_embeddings() -> tuple[np.ndarray, np.ndarray]:
    """55 rows: 50 in cluster 0, 5 noise — enough to exercise every sampling path."""

    rng = np.random.default_rng(7)
    embeddings = rng.normal(size=(55, 6))
    labels = np.array([0] * 50 + [-1] * 5)
    return embeddings, labels


class TestSampleClusterMembers:
    """10 nearest the centroid + a seeded random tail (ADR-007 §4)."""

    def test_returns_nearest_plus_random_distinct_members(
        self, clustered_embeddings: tuple[np.ndarray, np.ndarray]
    ) -> None:
        embeddings, labels = clustered_embeddings

        sample = sample_cluster_members(
            embeddings, labels, 0, nearest=10, random=10, seed=42
        )

        assert len(sample) == 20
        assert len(set(sample)) == 20
        assert all(labels[index] == 0 for index in sample)

    def test_the_head_is_the_ten_nearest_the_normalised_centroid(
        self, clustered_embeddings: tuple[np.ndarray, np.ndarray]
    ) -> None:
        embeddings, labels = clustered_embeddings
        expected = _members_in_similarity_order(embeddings, labels, 0)[:10]

        sample = sample_cluster_members(
            embeddings, labels, 0, nearest=10, random=10, seed=42
        )

        assert sample[:10] == expected

    def test_the_same_seed_samples_the_same_chunks(
        self, clustered_embeddings: tuple[np.ndarray, np.ndarray]
    ) -> None:
        """A re-run on an unchanged corpus must show the LLM the same evidence,
        so its cached summary stays valid (ADR-007 §4)."""

        embeddings, labels = clustered_embeddings

        first = sample_cluster_members(
            embeddings, labels, 0, nearest=10, random=10, seed=42
        )
        second = sample_cluster_members(
            embeddings, labels, 0, nearest=10, random=10, seed=42
        )

        assert first == second

    def test_a_different_seed_changes_only_the_random_tail(
        self, clustered_embeddings: tuple[np.ndarray, np.ndarray]
    ) -> None:
        embeddings, labels = clustered_embeddings

        with_42 = sample_cluster_members(
            embeddings, labels, 0, nearest=10, random=10, seed=42
        )
        with_43 = sample_cluster_members(
            embeddings, labels, 0, nearest=10, random=10, seed=43
        )

        assert with_42[:10] == with_43[:10]
        assert with_42[10:] != with_43[10:]

    def test_the_seed_is_offset_by_the_cluster_id(
        self, clustered_embeddings: tuple[np.ndarray, np.ndarray]
    ) -> None:
        """``seed + cluster_id``: one seed for the whole run would draw the same
        positions in every cluster's ranking."""

        embeddings, _ = clustered_embeddings
        labels = np.array([0] * 50 + [-1] * 5)
        other = np.array([1] * 50 + [-1] * 5)

        as_cluster_0 = sample_cluster_members(
            embeddings, labels, 0, nearest=0, random=5, seed=42
        )
        as_cluster_1 = sample_cluster_members(
            embeddings, other, 1, nearest=0, random=5, seed=42
        )

        assert as_cluster_0 != as_cluster_1

    def test_a_small_cluster_returns_every_member_in_similarity_order(self) -> None:
        rng = np.random.default_rng(3)
        embeddings = rng.normal(size=(12, 6))
        labels = np.zeros(12, dtype=int)

        sample = sample_cluster_members(
            embeddings, labels, 0, nearest=10, random=10, seed=42
        )

        assert sample == _members_in_similarity_order(embeddings, labels, 0)

    def test_rejects_the_noise_label(
        self, clustered_embeddings: tuple[np.ndarray, np.ndarray]
    ) -> None:
        """Noise is not a cluster: it has no centroid and never gets a summary."""

        embeddings, labels = clustered_embeddings

        with pytest.raises(ValueError) as error:
            sample_cluster_members(
                embeddings, labels, -1, nearest=10, random=10, seed=42
            )

        assert "-1" in str(error.value)

    def test_rejects_an_absent_cluster_id(
        self, clustered_embeddings: tuple[np.ndarray, np.ndarray]
    ) -> None:
        embeddings, labels = clustered_embeddings

        with pytest.raises(ValueError) as error:
            sample_cluster_members(embeddings, labels, 9, nearest=1, random=1, seed=42)

        assert "9" in str(error.value)


class TestClusterHelpers:
    """Noise never counts as a cluster in any of the three summaries."""

    def test_cluster_centroids_2d_averages_each_cluster_and_skips_noise(self) -> None:
        coords = [(0.0, 0.0), (2.0, 4.0), (10.0, 10.0), (-5.0, 5.0)]
        labels = [0, 0, 1, -1]

        centroids = cluster_centroids_2d(coords, labels)

        assert centroids == {0: (1.0, 2.0), 1: (10.0, 10.0)}

    def test_cluster_sizes_counts_members_and_skips_noise(self) -> None:
        assert cluster_sizes([0, 0, 1, -1, -1, 2]) == {0: 2, 1: 1, 2: 1}

    def test_cluster_sizes_is_empty_when_everything_is_noise(self) -> None:
        assert cluster_sizes([-1, -1]) == {}

    def test_noise_count_counts_only_minus_one(self) -> None:
        assert noise_count([0, 0, 1, -1, -1, 2]) == 2

    def test_noise_count_is_zero_without_noise(self) -> None:
        assert noise_count([0, 1, 2]) == 0


@pytest.mark.slow
class TestTheRealRecipe:
    """One end-to-end run of the real UMAP + HDBSCAN stack (~seconds, warm).

    Everything above is a fake; this is the test that would catch "the recipe
    does not actually find clusters" or "a library upgrade broke determinism".
    """

    @staticmethod
    def _blobs() -> tuple[np.ndarray, list[int]]:
        """3 well-separated Gaussian blobs x 40 points in 32-d, L2-normalised."""

        rng = np.random.default_rng(0)
        centers = rng.normal(size=(3, 32))
        centers /= np.linalg.norm(centers, axis=1, keepdims=True)
        points = np.concatenate(
            [center + 0.05 * rng.normal(size=(40, 32)) for center in centers]
        )
        points /= np.linalg.norm(points, axis=1, keepdims=True)
        return points, [0] * 40 + [1] * 40 + [2] * 40

    def test_finds_one_cluster_per_blob_and_repeats_itself(self) -> None:
        points, blob_of = self._blobs()
        config = _config(min_cluster_size=10)

        first = reduce_and_cluster(points, config)
        started = time.perf_counter()
        second = reduce_and_cluster(points, config)
        elapsed = time.perf_counter() - started

        labels = np.array(first.labels)
        found = {label for label in first.labels if label != -1}
        assert len(found) == 3, f"expected 3 clusters, got {sorted(found)}"

        per_blob = [
            {int(label) for label in labels[np.array(blob_of) == blob] if label != -1}
            for blob in (0, 1, 2)
        ]
        assert all(len(labels_of_blob) == 1 for labels_of_blob in per_blob), per_blob
        assert len({next(iter(labels_of_blob)) for labels_of_blob in per_blob}) == 3

        assert np.array_equal(first.labels, second.labels)
        assert np.array_equal(first.coords, second.coords)
        assert elapsed < 10.0, f"a warm run took {elapsed:.1f}s"

    def test_emits_no_warnings(self, recwarn: pytest.WarningsRecorder) -> None:
        """The clamp and the explicit ``copy=True`` exist so a normal run is
        silent; a new warning here means a knob stopped being forwarded."""

        points, _ = self._blobs()

        reduce_and_cluster(points, _config(min_cluster_size=10))

        assert [str(warning.message) for warning in recwarn.list] == []


def test_module_namespace_is_free_of_the_heavy_stack() -> None:
    """The lazy imports live in the two function bodies, so the module object
    never holds ``umap`` / ``sklearn`` attributes (the AST guard in
    ``test_package_layout.py`` proves the same thing from the source side)."""

    assert not hasattr(core, "umap")
    assert not hasattr(core, "HDBSCAN")
    assert isinstance(core.NOISE_LABEL, int)
    assert core.NOISE_LABEL == -1


def test_helpers_accept_a_clustering_result(monkeypatch: pytest.MonkeyPatch) -> None:
    """The three helpers are fed straight from ``ClusteringResult`` fields, so
    they must take plain lists as happily as numpy arrays."""

    monkeypatch.setattr(core, "_umap_reduce", _FakeReducer())
    monkeypatch.setattr(core, "_hdbscan_labels", _FakeClusterer())
    result = reduce_and_cluster(
        np.arange(30 * 8, dtype=float).reshape(30, 8), _config()
    )

    assert cluster_sizes(result.labels) == {0: 10, 1: 10, 2: 10}
    assert noise_count(result.labels) == 0
    assert set(cluster_centroids_2d(result.coords, result.labels)) == {0, 1, 2}
