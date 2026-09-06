---
id: 116-clustering-core-reduce-cluster-sample
feature: embedding-clusters-viz
status: done
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

- [x] `tree/memory/clustering/{__init__,types,core}.py` exist; `import tree.memory.clustering.core` in a bare subprocess leaves `umap`, `sklearn`, `numba` out of `sys.modules`.
- [x] With `_umap_reduce` and `_hdbscan_labels` patched by fakes (fake reduce = first `k` columns; fake labels = `row_index // 10`), `reduce_and_cluster` on a `(30, 8)` array returns 30 labels `[0]*10 + [1]*10 + [2]*10` and 30 `(x, y)` pairs equal to the first two columns; the fake reducer is called exactly twice (`n_components=5` then `2`), BOTH times with the raw `(30, 8)` input.
- [x] `_umap_reduce` (fake `umap.UMAP` injected via `sys.modules`) receives `n_neighbors=7` for an 8-row input with `config.umap.n_neighbors == 15`, and `15` for a 100-row input; `min_dist`, `metric`, `random_state`, `n_jobs=1` are forwarded verbatim.
- [x] `reduce_and_cluster` on `(14, 8)` rows with `min_cluster_size=15` raises `ValueError` whose message contains `14` and `15`, and NEITHER helper is called.
- [x] Labels `-2`/`-3` from the fake clusterer are returned as `-1`.
- [x] `sample_cluster_members` on a cluster of 50 rows with `nearest=10, random=10, seed=42`: returns 20 distinct indices; the first 10 are the 10 rows with the highest cosine similarity to the normalised centroid (checked independently in the test); calling twice returns the identical list; `seed=43` changes the random tail but not the nearest head; a cluster of 12 members with `(10, 10)` returns all 12 in similarity order; `cluster_id=-1` raises `ValueError`.
- [x] `cluster_centroids_2d` excludes `-1`; `cluster_sizes` excludes `-1`; `noise_count` counts `-1` only.
- [x] `@pytest.mark.slow` real-library test: 3 Gaussian blobs × 40 points in 32-d (L2-normalised, `np.random.default_rng(0)`), `min_cluster_size=10`, `random_state=42` → exactly 3 non-noise clusters, every blob maps to a single label, and two consecutive calls return identical labels AND identical 2D coordinates (`np.array_equal`). Wall time of the second call < 2 s (assert with a generous 10 s bound; the first call pays the numba JIT).
- [x] `ClusterSummary(label="a b c d e f g", ...)` raises (7 words); `keywords=["x", "y"]` raises (fewer than 3); `keywords=["Ai", "ai", "ml", "nlp"]` normalises to 3 entries and passes; `summary` of 101 words raises; `ClusteringResult(labels=[0], coords=[])` raises.
- [x] Layout guards: `TestClusteringNeverDependsOnGraph` and `TestHeavyImportsAreLazy` exist and go red when (a) `core.py` gains `import umap` at module level, (b) `rag/search.py` gains `from tree.memory.clustering import core` (SWE demonstrates the red run in the log, then reverts).
- [x] `make memory-format-check && make memory-lint-check && make memory-tests` green; the full suite's wall time grows by < 60 s on a warm machine (state before/after in the log).

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

### [SWE] 2026-09-06 18:54 — Implementation

**Docs verified first (mandatory, context7 MCP unavailable in this session — fell back to the
INSTALLED sources, which are authoritative for the pinned versions: umap-learn 0.5.12 /
scikit-learn 1.9.0 in `apps/memory/.venv`)**

- `umap.UMAP.__init__` defaults confirmed: `n_neighbors=15`, `n_components=2`, `metric="euclidean"`,
  `min_dist=0.1`, `random_state=None`, `n_jobs=-1`. All six are therefore passed EXPLICITLY.
- `n_neighbors` truncation confirmed at `umap/umap_.py:2455-2470`: when `X.shape[0] <= n_neighbors`,
  umap-learn warns `"n_neighbors is larger than the dataset size; truncating to X.shape[0] - 1"`
  and sets `_n_neighbors = n - 1`. Our `min(config.umap.n_neighbors, n - 1)` reproduces that value
  without the warning, and matches it exactly at the boundary (n = 15, cfg 15 -> 14 both ways).
- `n_jobs` override confirmed at `umap/umap_.py:1950-1953`: the warning fires only when
  `n_jobs != 1 and random_state is not None`, and the message interpolates the ALREADY-overridden
  value, hence the literal text `"n_jobs value 1 overridden to 1 by setting random_state"`. Because
  we pass `n_jobs=1` ourselves the branch is never taken — the filterwarnings entry the task asks
  for is a belt-and-braces guard for UMAP instances umap-learn builds internally.
- `sklearn.cluster.HDBSCAN.__init__` confirmed: `min_cluster_size=5, min_samples=None,
  metric="euclidean", ..., copy="warn"` — `copy` defaults to the string `"warn"` in 1.9 and becomes
  `True` in 1.10, so passing `copy=True` explicitly avoids the FutureWarning without a filter.
- `labels_` semantics confirmed from the class docstring: `-1` noisy sample, `-2` sample with
  infinite elements, `-3` sample with missing data (NaN). Hence the `{-2, -3} -> -1` mapping.

**Files modified**

- `apps/memory/src/tree/memory/clustering/__init__.py` — new NEUTRAL package; no re-exports, so
  `clustering.types` never drags in `core`/numpy.
- `apps/memory/src/tree/memory/clustering/types.py` — `ClusteringResult` (equal-length validator),
  `ClusterSummary` (label <= 6 words + non-empty, summary <= 100 words, keywords stripped/deduped
  case-insensitively then bounded 3-5), `MemoryClusterInfo`, `MapPoint`, `EmbeddingMap`.
- `apps/memory/src/tree/memory/clustering/core.py` — `reduce_and_cluster` (refusal BEFORE any heavy
  import -> `_umap_reduce` to `n_components` -> `_hdbscan_labels` -> a SEPARATE 2-D `_umap_reduce`
  on the RAW embeddings -> `-2/-3 -> -1` -> finite check -> plain-python scalars),
  `_umap_reduce` (function-scope `import umap`, explicit `n_neighbors` clamp, `n_jobs=1`),
  `_hdbscan_labels` (function-scope `from sklearn.cluster import HDBSCAN`, `copy=True`),
  `sample_cluster_members`, `cluster_centroids_2d`, `cluster_sizes`, `noise_count`, `NOISE_LABEL`.
- `apps/memory/pyproject.toml` — the umap `n_jobs` filterwarnings entry (with the reason inline).
- `apps/memory/tests/unit/memory/clustering/test_types.py` — 25 tests on the shapes + LLM contract.
- `apps/memory/tests/unit/memory/clustering/test_core.py` — 27 fast tests (fake reducer/clusterer,
  fake `umap` + `sklearn.cluster` modules injected via `sys.modules`) + 2 `@pytest.mark.slow` tests
  on the real stack.
- `apps/memory/tests/unit/memory/test_package_layout.py` — top-level set gains `clustering`;
  `TestClusteringNeverDependsOnGraph` (3 guards incl. `rag/` -/-> `clustering/`);
  `TestHeavyImportsAreLazy` (AST walk that skips function bodies, plus its own red/green
  self-checks); the mirror test now covers `clustering/`.
- `apps/memory/tests/unit/test_clustering_dependencies.py` — one more subprocess case:
  `tree.memory.clustering.core` itself must leave the stack unimported.

**Tests**

- Unit: 2643 passing, 0 failing (baseline before this task: 2529) — `make memory-tests`.
- Integration: N/A — this app has no integration suite by design (CLAUDE.md).
- Suite wall time: **30.18 s before -> 37.32 s after (+7.1 s)**, well inside the < 60 s budget. The
  `slow` class costs ~8 s of that (real `import umap` + 6 real UMAP fits); everything else is fakes.

**Acceptance criteria**

- [x] package exists; bare-subprocess import leaves the stack out — `tests/unit/test_clustering_dependencies.py::TestNothingImportsTheClusteringStack::test_entry_point_leaves_the_clustering_stack_unimported[clustering-core]` (measured directly: 0.10 s, `heavy modules loaded: []`).
- [x] fake-reducer wiring on `(30, 8)` — `tests/unit/memory/clustering/test_core.py::TestReduceAndClusterWiring::{test_returns_one_label_and_one_2d_point_per_row,test_fits_umap_twice_on_the_raw_embeddings,test_clusters_the_intermediate_space_not_the_picture}`.
- [x] `n_neighbors` clamp 7/15 and the verbatim knobs — `...::TestUmapReduce::{test_clamps_n_neighbors_to_n_minus_one_on_a_small_corpus,test_keeps_the_configured_n_neighbors_when_the_corpus_is_large,test_forwards_the_configured_knobs_and_pins_single_threaded,test_fits_on_the_embeddings_it_was_given}`.
- [x] `(14, 8)` vs `min_cluster_size=15` refusal, no helper called — `...::TestReduceAndClusterRefusesTinyCorpora::{test_raises_naming_both_numbers,test_neither_helper_runs}`.
- [x] `-2`/`-3` -> `-1` — `...::TestDegenerateLabels::test_minus_two_and_minus_three_become_minus_one`.
- [x] sampling (20 distinct, independent top-10 check, determinism, seed sensitivity, all-12 case, `-1` and absent id raise) — `...::TestSampleClusterMembers` (8 tests).
- [x] helpers exclude noise — `...::TestClusterHelpers` (5 tests).
- [x] real-library blobs test — `...::TestTheRealRecipe::test_finds_one_cluster_per_blob_and_repeats_itself` (3 clusters, one label per blob, `np.array_equal` on labels AND coords, warm call asserted < 10 s).
- [x] `ClusterSummary` / `ClusteringResult` validators — `tests/unit/memory/clustering/test_types.py` (`TestClusterSummaryLabel`, `TestClusterSummaryText`, `TestClusterSummaryKeywords`, `TestClusteringResult`).
- [x] layout guards demonstrated red, then reverted (evidence below).
- [x] format/lint/pre-commit/tests green; wall time recorded above.

**Evidence**

```
$ make memory-tests            # BEFORE (baseline, this worktree at 009db16)
============================ 2529 passed in 30.18s =============================
real 33.78

$ make memory-tests            # AFTER
============================ 2643 passed in 37.32s =============================
real 41.56

$ make memory-format-fix && make memory-lint-fix && make memory-format-check && make memory-lint-check
4 files reformatted, 271 files left unchanged
All checks passed!
275 files already formatted
All checks passed!

$ make pre-commit
Validate pyproject.toml..............................(no files to check)Skipped
prettier.................................................................Passed
ruff check...............................................................Passed
ruff format..............................................................Passed
biome check (harness)....................................................Passed
```

Layout guards, deliberately broken (`import umap` at the top of `clustering/core.py` AND
`from tree.memory.clustering import core` in `rag/search.py`), then reverted:

```
$ uv run --no-sync pytest tests/unit/memory/test_package_layout.py -q
E       AssertionError: rag/search.py imports ['tree.memory.clustering']
E       AssertionError: clustering/core.py imports ['umap'] at module level; move it inside the function that uses it (ADR-007 §6)
FAILED ...::TestClusteringNeverDependsOnGraph::test_no_rag_module_imports_the_clustering_layer[rag/search.py]
FAILED ...::TestHeavyImportsAreLazy::test_no_module_imports_the_clustering_stack_at_module_level[clustering/core.py]
2 failed, 109 passed in 0.72s

$ # both files restored from backup
$ uv run --no-sync pytest tests/unit/memory/test_package_layout.py -q
111 passed in 0.63s
```

End-to-end, the way User Stories 1-3 describe it (real libraries, no Prefect/Mongo/LLM;
`uv --directory apps/memory run python scratchpad/e2e_clustering.py`):

```
config: {'umap': {'n_neighbors': 15, 'min_dist': 0.0, 'metric': 'cosine', 'n_components': 5, 'random_state': 42}, 'hdbscan': {'min_cluster_size': 15, 'min_samples': None}, 'sampling': {'nearest': 10, 'random': 10}, 'summaries': {'llm_concurrency': 5}}
input: (500, 1024) float64
INFO tree.memory.clustering.core Clustering 500 embeddings (umap 5-d -> hdbscan min_cluster_size 15)
INFO tree.memory.clustering.core Clustered 500 embeddings into 3 clusters (404 noise)
first call (cold numba): 8.9s
second call (warm): 1.27s
labels: 500 coords: 500
cluster sizes: {0: 18, 1: 59, 2: 19}
noise: 404
first 5 coords: [(4.278, 4.947), (5.271, 5.009), (3.808, 5.118), (3.91, 4.976), (4.329, 4.437)]
centroids 2d: {0: (3.02, 5.18), 1: (4.36, 4.63), 2: (3.76, 5.75)}
identical on re-run: True True
types: int float
cluster 1 sample (20 rows): [218, 216, 285, 385, 330, 309, 294, 353, 346, 242, 250, 373, 3, 213, 50, 259, 296, 144, 478, 6]
sample is reproducible: True
tiny corpus refused: Nothing to cluster: 12 embeddings < min_cluster_size 15. Embed more child chunks, or lower memory.clustering.hdbscan.min_cluster_size (TREE_MEMORY__CLUSTERING__HDBSCAN__MIN_CLUSTER_SIZE).
```

```
$ uv --directory apps/memory run python -c "import time,sys; t=time.perf_counter(); import tree.memory.clustering.core; ..."
import time: 0.10s
heavy modules loaded: []
numpy loaded: True
```

**Judgement calls**

- **Finite-coordinate guard (the Tester's #115 NaN/inf note): YES, added.** `reduce_and_cluster`
  runs one `np.isfinite` pass over the `(n, 2)` display coordinates and raises a `ValueError` naming
  how many rows are affected. Rationale: a NaN coordinate is not just undrawable, it is not JSON —
  `json.dumps` writes a bare `NaN` that the browser's `JSON.parse` rejects, so without the check the
  failure surfaces as a blank map with a console error, far from its cause. The check is O(2n) and
  runs once per run. Covered by `TestNonFiniteCoordinates`. I did NOT add an input-side NaN/inf
  rejection: the spec's `-2`/`-3` -> `-1` mapping is precisely the designed handling for non-finite
  INPUT rows, so rejecting them at the door would contradict it.
- `_umap_reduce(embeddings, *, n_components, config)` — the two non-array arguments are
  keyword-only, so the two call sites read as `n_components=config.umap.n_components` and
  `n_components=2` and can never be swapped positionally.
- `metric="euclidean"` on HDBSCAN is hard-coded (not `config.umap.metric`): its input is a UMAP
  embedding, whose space is euclidean whatever metric the projection was fitted with.
- `n_neighbors` is clamped with exactly `min(cfg, n - 1)` as specced — no extra `max(2, ...)` floor.
  A corpus small enough to hit that (n <= 2) is already refused by `min_cluster_size >= 2`.
- Labels and coordinates are converted to plain `int`/`float` inside `reduce_and_cluster`; `np.int64`
  reaches Mongo as an unencodable type and `np.float32` breaks `json.dumps` in the renderer.
- `numpy` is imported at module level in `core.py` and NOT added to `[project.dependencies]`: it is
  a hard requirement of scikit-learn/umap-learn (both already main deps), costs 0.10 s to import,
  and #115 owned the dependency block. Flagging it in case the PA wants it declared explicitly.
- `TestHeavyImportsAreLazy` counts `if TYPE_CHECKING:` imports as module-level (they are, textually,
  even though they do not execute). Stricter than needed, and deliberate: nobody needs a
  `TYPE_CHECKING` import of `umap`, and the looser rule is easy to abuse.
- Kept a second `slow` test (`test_emits_no_warnings`, +0.2 s) because the clamp and `copy=True`
  exist precisely to keep a real run silent — the fakes cannot prove that.

**Notes for the Tester**

- Probe the ORDER inside `reduce_and_cluster`: step C must run after B, and both fits must receive
  the raw array — `TestReduceAndClusterWiring::test_fits_umap_twice_on_the_raw_embeddings` is the
  guard; try inverting it (feed `intermediate` to the 2-D fit) and confirm it goes red.
- Probe the sampling tie-break: `np.argsort(-similarity, kind="stable")` gives descending similarity
  with ties broken by ROW INDEX. Swapping to the default quicksort should break
  `test_the_head_is_the_ten_nearest_the_normalised_centroid` only on tied inputs — that test uses
  random vectors, so a targeted duplicate-vector case would be a fair extra probe.
- The e2e run above shows 404/500 noise. That is a property of the SYNTHETIC data (isotropic
  Gaussian blobs in 1024-d whose radius dominates their separation), not of the recipe — the
  `slow` blobs test with a realistic 0.05 spread puts every point in its own blob's cluster. Worth
  a sanity check on real Voyage embeddings once #117 can load them.
- Nothing here reads config, Mongo, Prefect or an LLM; `make memory-tests` needs no `.env` for these
  files (I still ran the whole suite through the make target, env `local`).

**One-off suite hang seen once — reported, not reproduced (5 green full runs out of 6)**

The 4th full-suite run of the session hung and had to be killed after ~20 min (RSS 6.5 GB, physical
footprint 157 GB). `sample(1)` on the process showed the main thread inside an **asyncio task**
(`task_step_impl` -> `slot_tp_new` -> `_PyGC_Collect`) — NO numpy / umap / sklearn / numba frames
anywhere in the call graph, so it is not the clustering code or the `slow` test. Three consecutive
re-runs of the identical tree were green (47.73 s, 41.14 s, 39.91 s), as were the two runs before
it. Flagged for the Tester as a possible pre-existing async flake worth watching; I could not
reproduce it and did not chase it further inside this task's scope.

### [Tester] 2026-09-06 19:40 — QA

**Test summary**
- Format / lint / pre-commit: PASS (`make memory-format-check`, `make memory-lint-check`, `make pre-commit` all green)
- Unit tests: 2643 passed / 0 failed (`make memory-tests`, env `local`)
- Integration tests: N/A — no integration suite by project design (CLAUDE.md)
- Warnings: 0 (confirmed on 3 full `make memory-tests` runs and on the isolated `slow` test's `test_emits_no_warnings`)

**Suite hang re-probe (headline adversarial item)**
Ran `make memory-tests` THREE consecutive times under `/usr/bin/time -l` to watch RSS and wall
time: 44.75 s / 40.40 s / 40.43 s pytest-reported, 49.36 / 44.89 / 44.91 s real per `/usr/bin/time`,
peak process footprint ~1.3-1.4 GB each run — all green, 2643 passed each time, no hang observed.
This matches the SWE's own finding (5/6 green) and the one hang they captured showed no
numpy/umap/sklearn/numba frames in the stack (`slot_tp_new` / `_PyGC_Collect` under an asyncio
task) — not attributable to this task's code. Treating as a pre-existing flake to watch, not a
blocker for this task.

**E2E adversarial pass**
- Happy path: real `reduce_and_cluster` on a `(500, 1024)` L2-normalised synthetic array → 500
  labels + 500 coords, cold 8.25 s / warm 1.24 s (< 10 s bound), identical labels and coords on a
  second call, tiny-corpus (12 rows) refusal message, sample reproducible (PASS)
- Break path 1 (state edge — inverted wiring: feed the 5-d intermediate to the 2-D fit instead of
  raw embeddings): edited `core.py` line 98 to `_umap_reduce(intermediate, ...)`, ran
  `TestReduceAndClusterWiring` → `test_fits_umap_twice_on_the_raw_embeddings` goes red
  (`assert (30, 5) == (30, 8)`), reverted, file byte-identical to original (`diff` clean) (PASS)
- Break path 2 (structural/regression guard — both layout sabotages): added `import umap` at module
  level to `core.py` AND `from tree.memory.clustering import core` to `rag/search.py`, ran
  `tests/unit/memory/test_package_layout.py` → both `TestHeavyImportsAreLazy` and
  `TestClusteringNeverDependsOnGraph::test_no_rag_module_imports_the_clustering_layer[rag/search.py]`
  go red with the exact messages named in the module docstring; reverted both files, re-ran →
  111 passed, `git status`/`git diff --stat` clean afterwards (PASS)
- Break path 3 (boundary/degenerate numeric input): `(20, 4)` all-identical rows (UMAP degenerate
  case) → all rows labelled `-1` (noise), finite coords, no crash/hang; a NaN row and an Inf row in
  raw `(30, 8)` input → real UMAP's own sklearn input validation raises a clean `ValueError`
  ("Input contains NaN" / "Input contains infinity...") before HDBSCAN ever runs, so the spec's
  `-2`/`-3` → `-1` mapping is reached only via the fake-clusterer unit tests, never via a real NaN
  row in the raw embeddings — documented as a finding below, not a defect (fails loudly either way,
  no hang, no silent corruption) (PASS)
- Break path 4 (boundary — sampling edge cases): `nearest=0, random=20` on 50 rows → 20 distinct
  indices; 10 fully-duplicate unit vectors with `nearest=5, random=0` → head is `[0,1,2,3,4]`,
  confirming the stable-sort tie-break by row index; duplicate vectors with a random tail → 10
  distinct indices, no crash; a zero vector among cluster members → included without NaN poisoning
  the ranking (PASS)
- Break path 5 (state edge — cross-process determinism): ran the real 3-blob recipe as two separate
  `uv run python` subprocesses and diffed the JSON-serialised labels+coords → byte-identical (PASS)

**Acceptance criteria**
- [x] PASS — package exists; bare-subprocess import leaves the stack out — re-ran
      `tests/unit/test_clustering_dependencies.py::TestNothingImportsTheClusteringStack::test_entry_point_leaves_the_clustering_stack_unimported[clustering-core]`
      and independently verified via `uv run python -c "import tree.memory.clustering.core; ..."` →
      `heavy modules loaded: []`, `numpy loaded: True`
- [x] PASS — `(30, 8)` fake wiring, both fits fed raw input — `TestReduceAndClusterWiring` (3 tests), read + re-run, all green; confirmed the guard is load-bearing by inverting step C (break path 1 above)
- [x] PASS — `n_neighbors` clamp 7/15 and verbatim knob forwarding — `TestUmapReduce` (4 tests), read + re-run
- [x] PASS — `(14, 8)` vs `min_cluster_size=15` refusal naming both numbers, no helper called — `TestReduceAndClusterRefusesTinyCorpora` (2 tests), read + re-run
- [x] PASS — `-2`/`-3` → `-1` — `TestDegenerateLabels::test_minus_two_and_minus_three_become_minus_one`, read + re-run
- [x] PASS — sampling (20 distinct, independent top-10 check, determinism, seed sensitivity incl. `seed + cluster_id`, 12-member all-returned case, `-1`/absent-id raise) — `TestSampleClusterMembers` (8 tests), read + re-run, plus 4 additional adversarial probes above (nearest=0/random=20, duplicate-vector tie-break, duplicate+tail, zero vector)
- [x] PASS — helpers exclude noise — `TestClusterHelpers` (5 tests), read + re-run
- [x] PASS — real-library blobs test: 3 clusters, one label per blob, `np.array_equal` on labels AND coords, warm < 10 s bound — `TestTheRealRecipe::test_finds_one_cluster_per_blob_and_repeats_itself`, re-run (2 passed in 8.81s for both slow tests); verified the blob geometry is meaningful (inter-center cosine distance ~0.8-1.1 vs. intra-blob noise vector norm ~0.28) so the assertion is not trivially satisfied
- [x] PASS — `ClusterSummary`/`ClusteringResult` validators incl. 7-word label, `["x","y"]`, `["Ai","ai","ml","nlp"]` → 3 entries, 101-word summary, length-mismatch `ClusteringResult` — `test_types.py`, read + re-run, plus adversarial probes: 6-entries-after-dedupe raises, whitespace-only entries dropped-then-counted (raises when that leaves <3, passes at exactly 3)
- [x] PASS — layout guards go red under both sabotages — demonstrated myself independently (break path 2 above), not just re-reading the SWE's log; reverted cleanly
- [x] PASS — format/lint/pre-commit/tests green; wall-time delta < 60 s — measured 3 full `make memory-tests` runs at 40-45 s each (baseline pre-task was 30.18 s per the SWE's log, delta well under 60 s)

**Evidence**
```
$ make memory-tests   (run 1 of 3, via /usr/bin/time -l)
============================ 2643 passed in 44.75s =============================
       49.36 real        37.41 user         4.32 sys
          1345306624  maximum resident set size

$ make memory-tests   (run 2 of 3)
============================ 2643 passed in 40.40s =============================
       44.89 real

$ make memory-tests   (run 3 of 3)
============================ 2643 passed in 40.43s =============================
       44.91 real

$ uv run --no-sync pytest tests/unit/memory/clustering/ tests/unit/memory/test_package_layout.py tests/unit/test_clustering_dependencies.py -v
============================= 173 passed in 19.41s =============================

$ # break path 1: inverted step C
tests/unit/memory/clustering/test_core.py::TestReduceAndClusterWiring::test_fits_umap_twice_on_the_raw_embeddings FAILED
E           assert (30, 5) == (30, 8)
1 failed, 3 passed in 0.64s
$ # reverted; diff core.py against pre-edit backup: clean

$ # break path 2: both layout sabotages simultaneously
FAILED tests/unit/memory/test_package_layout.py::TestClusteringNeverDependsOnGraph::test_no_rag_module_imports_the_clustering_layer[rag/search.py]
FAILED tests/unit/memory/test_package_layout.py::TestHeavyImportsAreLazy::test_no_module_imports_the_clustering_stack_at_module_level[clustering/core.py]
2 failed, 109 passed in 0.89s
$ # reverted both files; re-run: 111 passed in 0.74s; git status clean

$ # 500x1024 real UMAP+HDBSCAN, two processes
cold: 8.25s  warm: 1.24s
identical labels: True
identical coords: True
tiny corpus refused: Nothing to cluster: 12 embeddings < min_cluster_size 15. ...
sample reproducible: True len: 20
$ diff run_a.json run_b.json   # separate `uv run python` subprocesses
(no output — byte-identical)
```

**Other issues found**
- `EmbeddingMap` has no cross-field validator for `unclustered <= total_children` (or non-negativity
  of either count) — confirmed by direct construction:
  `EmbeddingMap(run_id="r1", clusters=[], points=[], total_children=5, unclustered=999)` is accepted
  silently. Per the task's explicit "judge" instruction, this is NOT a blocker: `EmbeddingMap` is a
  read-side transit shape populated by `store.py` (#117) from trusted DB counts, and the spec does
  not list this as a validator requirement. Worth a one-line note for #117/#118 to consider a
  `model_validator` there if `store.py` ever computes these independently rather than as a single
  query.
- `numpy` is imported at module level in `core.py` but is not declared in `[project.dependencies]` —
  confirmed still true in the current `pyproject.toml`. This predates this task (numpy is a
  transitive dependency of scikit-learn / umap-learn / pymongo, already resolved and pinned in the
  lockfile) and the SWE flagged it explicitly as a judgement call rather than introducing it fresh.
  Reporting as a finding per the brief's instruction, not a blocker — AGENTS.md/CLAUDE.md do not
  require every transitive-but-directly-imported module to be declared, and #115 owns the
  dependency block.
- Real UMAP/sklearn reject raw NaN/Inf rows with their own `ValueError` before HDBSCAN's `-2`/`-3`
  codes are ever produced (see break path 3) — the `-2`/`-3` → `-1` mapping is real and correct code
  that is exercised end-to-end only through the fake-clusterer tests, never through the real stack,
  because UMAP itself is the first thing to see (and reject) non-finite raw input. Not a defect —
  arguably a GOOD thing (fails fast and clearly) — but worth knowing for #117:
  `store.load_child_embeddings` should not expect the `-2`/`-3` mapping to shield it from a NaN in
  the loaded embeddings; the real failure mode for that path is an uncaught `ValueError` from UMAP
  with a slightly less friendly message than the ones this module writes itself.

**VERDICT: PASS**

### [PA] 2026-09-06 22:32 — Acceptance Review

**VERDICT: no issues for this task** (feature-level verdict: REJECT via `tasks/120-pa-rejection-embedding-clusters-viz.md`; none of its items touch #116)

The recipe ships as ADR-007 Decision 1 records it (5-d cluster space, SEPARATE 2-D fit on the raw embeddings, seeded sampling, lazy imports), and the tiny-corpus refusal message names both numbers and the knob. The real run's output is good evidence the recipe works on Voyage embeddings (37 clusters / 99 noise over 1623 chunks, labels that read as real topics). `numpy` used directly but undeclared is noted for the PR Reviewer, not a product issue.
