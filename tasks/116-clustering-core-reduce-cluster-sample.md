---
id: 116-clustering-core-reduce-cluster-sample
feature: embedding-clusters-viz
status: pending
---

# `tree/memory/clustering/core.py` — pure UMAP→HDBSCAN→UMAP-2D reduce/cluster + seeded sampling

Tags: `memory`, `clustering`
Depends on: #115
Blocks: #117, #118
Implements: ADR-007 — Decisions 1 (recipe), 4 (sampling), 6 (lazy imports), 7 (package layout)

## Scope

Create the NEUTRAL package `tree/memory/clustering/` with its first two modules. Pure numpy in,
pydantic/numpy out; NO Prefect, NO Mongo, NO LLM, NO config *reads* (the config object is passed
in). `umap` and `sklearn` are imported LAZILY inside the two private helpers that call them, so
importing the module costs nothing (`import umap` is ~39 s cold on a fresh machine, ~3 s warm).

**1. Types** — `tree/memory/clustering/types.py` (Pydantic, all fields described):
- `ClusteringResult(labels: list[int], coords: list[tuple[float, float]])` — one entry per input
  row, input order; `-1` = noise. Validator: equal lengths.
- `ClusterSummary(label: str, summary: str, keywords: list[str])` — the LLM contract:
  `label` ≤ 6 whitespace-separated words and non-empty, `summary` ≤ 100 words, `keywords` 3–5
  non-empty strings after stripping + case-insensitive dedupe (validators raise otherwise).
- `MemoryClusterInfo(cluster_id: int, label: str, summary: str, keywords: list[str], size: int,
  sample_chunk_ids: list[str], centroid_x: float, centroid_y: float)`.
- `MapPoint(chunk_id: str, x: float, y: float, cluster_id: int, title: str | None,
  heading_path: list[str], snippet: str)` — `snippet` = first 160 chars of `properties.content`.
- `EmbeddingMap(run_id: str, clusters: list[MemoryClusterInfo], points: list[MapPoint],
  total_children: int, unclustered: int)` — what the surfaces render; `unclustered` = child rows
  with a non-empty embedding whose `cluster_id is None` or `viz.run_id != run_id`.

**2. Core** — `tree/memory/clustering/core.py`:
- `reduce_and_cluster(embeddings: np.ndarray, config: ClusteringConfig) -> ClusteringResult`
  (`embeddings` shape `(n, d)`, `n >= config.hdbscan.min_cluster_size`, else `ValueError` naming
  both numbers — callers decide to skip BEFORE importing anything heavy):
  - Step A: `_umap_reduce(embeddings, n_components=config.umap.n_components, config)` → `(n, 5)`.
  - Step B: `_hdbscan_labels(intermediate, config)` → `sklearn.cluster.HDBSCAN(min_cluster_size,
    min_samples, metric="euclidean", copy=True).fit(...).labels_` (labels `-2`/`-3` — inf/NaN
    inputs — are mapped to `-1`).
  - Step C: `_umap_reduce(embeddings, n_components=2, config)` — a SEPARATE 2D fit **on the raw
    embeddings** with the same seed/knobs (decision recorded in ADR-007: a projection of the
    5-d projection would compound distortion and couple the picture to the clustering step).
  - `_umap_reduce` passes `n_neighbors=min(config.umap.n_neighbors, n - 1)` (explicit clamp —
    umap-learn otherwise truncates with a warning), `min_dist`, `metric`, `random_state`,
    `n_jobs=1`, and imports `umap` inside the function. `_hdbscan_labels` imports
    `sklearn.cluster` inside the function.
- `sample_cluster_members(embeddings, labels, cluster_id, *, nearest, random, seed) -> list[int]`:
  member row indices; centroid = mean of the L2-normalised member vectors; rank members by cosine
  similarity to it (descending, ties by index); take the first `nearest`; then `random` indices
  drawn WITHOUT replacement from the remainder with `np.random.default_rng(seed + cluster_id)`;
  if `len(members) <= nearest + random` return ALL members in similarity order. Raises
  `ValueError` for `cluster_id == -1` or an absent id.
- `cluster_centroids_2d(coords, labels) -> dict[int, tuple[float, float]]` — mean 2D point per
  non-noise cluster.
- `cluster_sizes(labels) -> dict[int, int]` — non-noise only.
- `noise_count(labels) -> int`.

**3. Warnings hygiene** — add to `[tool.pytest.ini_options].filterwarnings`:
`"ignore:n_jobs value 1 overridden to 1 by setting random_state:UserWarning"` (umap-learn emits
it whenever `random_state` is set; deterministic runs are the point). The sklearn `copy`
FutureWarning is avoided by passing `copy=True` explicitly — do NOT filter it.

**4. Layout test** (`tests/unit/memory/test_package_layout.py`): top-level set gains
`"clustering"`; new class `TestClusteringNeverDependsOnGraph`: no module under `clustering/`
imports `tree.memory.graph*` or `tree.memory.pipeline`; `rag/` additionally must not import
`tree.memory.clustering*`; new class `TestHeavyImportsAreLazy`: an AST walk asserts no
MODULE-LEVEL `import umap|sklearn|numba|pynndescent` anywhere under `tree/memory/` (function-scope
imports are the only allowed form); the mirror test covers `clustering/`.

## Acceptance Criteria

- [ ] `tree/memory/clustering/{__init__,types,core}.py` exist; `import tree.memory.clustering.core` in a bare subprocess leaves `umap`, `sklearn`, `numba` out of `sys.modules`.
- [ ] With `_umap_reduce` and `_hdbscan_labels` patched by fakes (fake reduce = first `k` columns; fake labels = `row_index // 10`), `reduce_and_cluster` on a `(30, 8)` array returns 30 labels `[0]*10 + [1]*10 + [2]*10` and 30 `(x, y)` pairs equal to the first two columns; the fake reducer is called exactly twice (`n_components=5` then `2`), BOTH times with the raw `(30, 8)` input.
- [ ] `_umap_reduce` (fake `umap.UMAP` injected via `sys.modules`) receives `n_neighbors=7` for an 8-row input with `config.umap.n_neighbors == 15`, and `15` for a 100-row input; `min_dist`, `metric`, `random_state`, `n_jobs=1` are forwarded verbatim.
- [ ] `reduce_and_cluster` on `(14, 8)` rows with `min_cluster_size=15` raises `ValueError` whose message contains `14` and `15`, and NEITHER helper is called.
- [ ] Labels `-2`/`-3` from the fake clusterer are returned as `-1`.
- [ ] `sample_cluster_members` on a cluster of 50 rows with `nearest=10, random=10, seed=42`: returns 20 distinct indices; the first 10 are the 10 rows with the highest cosine similarity to the normalised centroid (checked independently in the test); calling twice returns the identical list; `seed=43` changes the random tail but not the nearest head; a cluster of 12 members with `(10, 10)` returns all 12 in similarity order; `cluster_id=-1` raises `ValueError`.
- [ ] `cluster_centroids_2d` excludes `-1`; `cluster_sizes` excludes `-1`; `noise_count` counts `-1` only.
- [ ] `@pytest.mark.slow` real-library test: 3 Gaussian blobs × 40 points in 32-d (L2-normalised, `np.random.default_rng(0)`), `min_cluster_size=10`, `random_state=42` → exactly 3 non-noise clusters, every blob maps to a single label, and two consecutive calls return identical labels AND identical 2D coordinates (`np.array_equal`). Wall time of the second call < 2 s (assert with a generous 10 s bound; the first call pays the numba JIT).
- [ ] `ClusterSummary(label="a b c d e f g", ...)` raises (7 words); `keywords=["x", "y"]` raises (fewer than 3); `keywords=["Ai", "ai", "ml", "nlp"]` normalises to 3 entries and passes; `summary` of 101 words raises; `ClusteringResult(labels=[0], coords=[])` raises.
- [ ] Layout guards: `TestClusteringNeverDependsOnGraph` and `TestHeavyImportsAreLazy` exist and go red when (a) `core.py` gains `import umap` at module level, (b) `rag/search.py` gains `from tree.memory.clustering import core` (SWE demonstrates the red run in the log, then reverts).
- [ ] `make memory-format-check && make memory-lint-check && make memory-tests` green; the full suite's wall time grows by < 60 s on a warm machine (state before/after in the log).

## User Stories

### Story: Reader runs the recipe on a notebook without Mongo or Prefect
1. Reader does `from tree.memory.clustering.core import reduce_and_cluster` and passes a `(500, 1024)` numpy array plus `app_config.memory.clustering`.
2. They get 500 labels with a handful of clusters and `-1`s, and 500 `(x, y)` pairs, in under 10 s after the first JIT-warm call.
3. Running it again returns the identical labels and coordinates.

### Story: Operator clusters a tiny corpus
1. Only 12 child chunks exist and `min_cluster_size` is 15.
2. `reduce_and_cluster` refuses with `12 embeddings < min_cluster_size 15` — nothing heavy is imported, no half-result is produced.

### Story: Summaries stay honest about what was sampled
1. A cluster has 40 members; sampling returns 10 nearest the centroid plus 10 seeded random ones.
2. Re-running the pipeline with the same corpus and seed samples the same 20 chunks, so the LLM sees the same evidence and its cached summary stays valid.

### Story: A developer accidentally imports UMAP at module scope
1. They add `import umap` to the top of `store.py` in a later task.
2. `make memory-tests` fails in `TestHeavyImportsAreLazy` with the offending file named.

## Out of scope

- LLM summaries and prompt (#117), Mongo reads/writes (#117), the flow (#117).
- Automatic parameter search, DBCV/silhouette scoring, clustering on the 2D projection.
- Clustering entity nodes.

---

Blocked by: #115

## Log
