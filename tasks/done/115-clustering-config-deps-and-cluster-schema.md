---
id: 115-clustering-config-deps-and-cluster-schema
feature: embedding-clusters-viz
status: done
---

# `memory.clustering` config + `umap-learn`/`scikit-learn` deps + `MemoryCluster` collection + chunk `cluster_id`/`viz` fields

Tags: `config`, `entities`, `deps`, `memory`
Depends on: None
Blocks: #116, #117, #118
Implements: ADR-007 — Decisions 1 (knobs), 3 (storage), 6 (dependencies)

## Scope

The mechanical foundations for clustering. Nothing computes yet; this task makes the knobs
readable, the packages installable, and the row shapes writable.

**1. Config** — `MemoryConfig.clustering: ClusteringConfig` (`tree/config/app_config.py`),
mirrored in `configs/default.yaml` (with a comment block: the BERTopic-shaped recipe, why we
cluster in the 5-d space and never on the 2D projection, that the ON/OFF switch is the
`run_clustering` flow parameter — there is deliberately NO `enabled` key) and in
`tests/unit/config/fixtures/frozen_config.yaml`:

```yaml
memory:
  clustering:
    umap:
      n_neighbors: 15       # local neighbourhood size; clamped to n-1 on small corpora
      min_dist: 0.0         # 0 = tightest clumps (BERTopic default for clustering)
      metric: cosine        # embeddings are cosine-normalised
      n_components: 5       # the intermediate space HDBSCAN clusters in (display is always 2D)
      random_state: 42      # one seed for both UMAP fits + the sampling RNG
    hdbscan:
      min_cluster_size: 15
      min_samples: null     # null = min_cluster_size (sklearn default)
    sampling:
      nearest: 10           # chunks nearest the centroid (cosine, original 1024-d space)
      random: 10            # seeded random chunks from the remainder
    summaries:
      llm_concurrency: 5    # concurrent Gemini calls (one call per cluster)
```

Pydantic: `UmapConfig(n_neighbors: int >= 2, min_dist: float in [0, 1], metric:
Literal["cosine", "euclidean"], n_components: int >= 2, random_state: int)`,
`HdbscanConfig(min_cluster_size: int >= 2, min_samples: int | None with >= 1 when set)`,
`ClusterSamplingConfig(nearest: int >= 0, random: int >= 0, cross-validator nearest + random >= 1)`,
`ClusterSummariesConfig(llm_concurrency: int >= 1)`, `ClusteringConfig(umap, hdbscan, sampling,
summaries)`. Every field has `Field(description=...)`. Env hatch unchanged:
`TREE_MEMORY__CLUSTERING__HDBSCAN__MIN_CLUSTER_SIZE=5`.

**2. Dependencies** — add `umap-learn>=0.5.12` and `scikit-learn>=1.9` to
`[project.dependencies]` in `apps/memory/pyproject.toml`, `uv lock`, commit `uv.lock`. Verified:
resolves on Python 3.14 against the pinned numpy 2.4.2 / scipy 1.17.1 (adds numba 0.67,
llvmlite 0.49, pynndescent 0.6, joblib, threadpoolctl). NOTHING imports them yet (the lazy-import
rule is asserted in #116). Register a `slow` pytest marker in `[tool.pytest.ini_options].markers`
("runs real UMAP/HDBSCAN; numba JIT on first call") — used by #116.

**3. `MemoryCluster` entity** — new module `tree/entities/clusters.py`:
- `MEMORY_CLUSTERS_COLLECTION = "memory_clusters"` (the one spelling).
- `ClusterCentroid(BaseModel)`: `x: float`, `y: float` — coordinates in the 2D **Embedding map**
  space.
- `MemoryCluster(BeanieDocument)`: `id: str` (deterministic `f"{user_id}:cluster:{cluster_id}"`,
  built by `build_cluster_id(user_id, cluster_id)`), `user_id: PydanticObjectId`, `run_id: str`,
  `cluster_id: int >= 0` (noise `-1` is never a row — validator), `label: str`, `summary: str`,
  `keywords: list[str]`, `size: int >= 1`, `sample_chunk_ids: list[str]`, `centroid:
  ClusterCentroid`, `created_at: datetime` (tz-aware validator, same pattern as
  `MemoryEntry._require_tz_aware_temporal`). `Settings.name = MEMORY_CLUSTERS_COLLECTION`,
  indexes `IndexModel([("user_id", 1), ("run_id", 1)], name="user_run")`.
- Registered in `tree/db.py::ALL_DOCUMENT_MODELS` and exported from `tree/entities/__init__.py`.

**4. Chunk fields on `MemoryEntry`** (`tree/entities/memory.py`), top-level graph-modeling meta
fields (ADR-001 §11), both defaulting to `None` so every existing row validates unchanged:
- `ChunkViz(BaseModel)`: `x: float`, `y: float`, `run_id: str` (the **Clustering run** that
  produced the coordinates).
- `MemoryEntry.cluster_id: int | None` (`-1` = noise in the latest run; `None` = never clustered)
  and `MemoryEntry.viz: ChunkViz | None`. Descriptions on all fields; a model validator rejects
  `cluster_id`/`viz` on any row that is not `type == "chunk" and subtype == "child"`.
- `_curated_meta`/renderer untouched (they read `properties`, not these fields).

## Acceptance Criteria

- [x] `load_app_config(frozen_config_path).memory.clustering` equals the YAML block above (`umap.n_components == 5`, `hdbscan.min_samples is None`, `sampling == (10, 10)`, `summaries.llm_concurrency == 5`); `configs/default.yaml` carries the same values and the comment block contains the strings `run_clustering` and `never on the 2D`.
- [x] `TREE_MEMORY__CLUSTERING__HDBSCAN__MIN_CLUSTER_SIZE=5` overrides `min_cluster_size`; `TREE_MEMORY__CLUSTERING__UMAP__METRIC=manhattan` raises a `ValidationError` naming `'cosine'` and `'euclidean'`; `TREE_MEMORY__CLUSTERING__ENABLED=true` is a hard `ValidationError` (`extra="forbid"` on `ClusteringConfig`) — the YAML section must never grow a silent switch.
- [x] `UmapConfig(n_neighbors=1)`, `HdbscanConfig(min_cluster_size=1)`, `HdbscanConfig(min_samples=0)`, `ClusterSamplingConfig(nearest=0, random=0)`, `ClusterSummariesConfig(llm_concurrency=0)` each raise.
- [x] `grep -n "umap-learn\|scikit-learn" apps/memory/pyproject.toml` shows both under `[project].dependencies`; `uv lock --check` passes; `uv run python -c "import umap, sklearn.cluster"` succeeds; the `slow` marker is registered (`uv run pytest --markers | grep slow`).
- [x] A subprocess test asserts that `import tree.memory.pipeline`, `import tree.mcp.server` (rag and graphrag) and `import tree.offline` do NOT put `umap`, `sklearn`, `numba` or `pynndescent` in `sys.modules` (this task adds no imports; the test guards #116/#117 from regressing).
- [x] `MemoryCluster(...)` with `cluster_id=-1` raises `ValueError` mentioning `noise`; with a naive `created_at` raises mentioning `timezone-aware`; a valid row inserted in the unit-test database lands in the `memory_clusters` collection (`list_collection_names()` contains it); `build_cluster_id(uid, 3) == f"{uid}:cluster:3"`.
- [x] `tree.db.ALL_DOCUMENT_MODELS` contains `MemoryCluster`; `tree.entities` exports `MemoryCluster`, `ClusterCentroid`, `build_cluster_id`, `MEMORY_CLUSTERS_COLLECTION`.
- [x] `MemoryEntry(kind="node", type="chunk", subtype="child", cluster_id=2, viz={"x": 0.1, "y": -3.2, "run_id": "r1"}, ...)` validates; the same fields on a `document` row or a `subtype="parent"` row raise `ValueError` mentioning `child`; a row without the fields round-trips with `cluster_id is None and viz is None`; every new field has a non-empty `description` (`test_field_descriptions.py` pattern).
- [x] `tests/unit/entities/snapshots/ontology_schema.json` unchanged (`chunk` is not LLM-extractable); if it changes the SWE explains why in the log.
- [x] `apps/memory/README.md` "`default.yaml` sections" bullet for `memory` lists `clustering` (`umap`, `hdbscan`, `sampling`, `summaries`) and states the switch is the flow parameter, not YAML.
- [x] `make memory-format-check && make memory-lint-check && make memory-tests` green.

## User Stories

### Story: Operator loosens clustering for a small corpus without editing YAML
1. Operator runs `TREE_MEMORY__CLUSTERING__HDBSCAN__MIN_CLUSTER_SIZE=5 uv --directory apps/memory run python -c "from tree.config.app_config import load_app_config; print(load_app_config().memory.clustering.hdbscan.min_cluster_size)"`.
2. The command prints `5`; without the variable it prints `15`.

### Story: Operator tries to switch clustering off in YAML
1. Operator adds `enabled: false` under `memory.clustering` in `configs/default.yaml`.
2. Every config-reading entry point fails at boot with a `ValidationError` naming `enabled` as an extra field — the docs point them at `run_clustering` instead.

### Story: Developer inspects the new collection shape
1. Developer runs `uv run python -c "from tree.entities import MemoryCluster; print(MemoryCluster.model_json_schema()['required'])"`.
2. The output lists `user_id, run_id, cluster_id, label, summary, keywords, size, sample_chunk_ids, centroid, created_at`.

### Story: Fresh install pulls the clustering stack once
1. Developer runs `uv sync` after pulling this task.
2. `umap-learn` and `scikit-learn` install; `make memory-tests` still starts in the same time as before (nothing imports them yet).

## Out of scope

- Any computation (`clustering/core.py`, #116), Mongo reads/writes (#117), rendering (#118).
- A `memory.clustering.enabled` switch (rejected — the flow parameter is the switch).
- Migrating existing rows (both new fields default to `None`).

---

Blocked by: (none)

## Log

### [SWE] 2026-09-06 — Implementation

**Files modified**

- `apps/memory/src/tree/config/app_config.py` — `UmapConfig` / `HdbscanConfig` / `ClusterSamplingConfig` / `ClusterSummariesConfig` / `ClusteringConfig` (`extra="forbid"`, every field described) on `MemoryConfig.clustering`.
- `apps/memory/configs/default.yaml` — the `memory.clustering` block + the comment block (why 5-d, `never on the 2D`, the `run_clustering` switch, the env-hatch example).
- `apps/memory/tests/unit/config/fixtures/frozen_config.yaml` — same values, short comment (prose stays in `default.yaml`).
- `apps/memory/pyproject.toml` — `umap-learn>=0.5.12` + `scikit-learn>=1.9` in `[project.dependencies]` (with a comment on why they are main, not an extra) and the `slow` marker.
- `apps/memory/uv.lock` — relocked: `+umap-learn 0.5.12`, `+numba 0.67.0`, `+llvmlite 0.49.0`, `+pynndescent 0.6.0`, `+narwhals 2.25.0`, `scikit-learn 1.8.0 -> 1.9.0`.
- `apps/memory/src/tree/entities/clusters.py` (new) — `MEMORY_CLUSTERS_COLLECTION`, `build_cluster_id`, `ClusterCentroid`, `MemoryCluster` (noise + tz-aware validators, `user_run` index).
- `apps/memory/src/tree/db.py`, `apps/memory/src/tree/entities/__init__.py` — register / export the cluster surface (+ `ChunkViz`).
- `apps/memory/src/tree/entities/memory.py` — `ChunkViz`, `MemoryEntry.cluster_id`, `MemoryEntry.viz`, `_check_cluster_fields_are_child_only`.
- `apps/memory/README.md` — the `memory` `default.yaml` bullet now lists `clustering` and states the switch is the flow parameter, not YAML.
- `apps/memory/tests/unit/config/test_app_config.py` — `TestClusteringConfig` (19 tests).
- `apps/memory/tests/unit/entities/test_clusters.py` (new) — 20 tests.
- `apps/memory/tests/unit/entities/test_memory.py` — `TestChunkClusterFields` (15 tests).
- `apps/memory/tests/unit/test_clustering_dependencies.py` (new) — dependency contract + the fresh-interpreter lazy-import guard (9 tests).

**Tests**

- Unit: 2529 passing, 0 failing (baseline 2452 → +77) — `make memory-tests`.
- Integration: N/A — this repo has no integration suite by design.

**Acceptance criteria**

- [x] `memory.clustering` loads from the frozen fixture and `default.yaml`; comment block carries `run_clustering` + `never on the 2D` — `tests/unit/config/test_app_config.py::TestClusteringConfig::{test_clustering_block_loaded_from_frozen_config,test_clustering_block_loaded_from_default_yaml,test_default_yaml_comment_block_explains_the_switch_and_the_2d_rule}`
- [x] Env hatch / bad metric / `enabled` — `…::TestClusteringConfig::{test_min_cluster_size_env_override,test_unknown_umap_metric_raises_naming_both_allowed_values,test_an_enabled_key_is_a_hard_validation_error,test_an_enabled_env_override_is_a_hard_validation_error}`
- [x] Bounds raise — `…::TestClusteringConfig::{test_umap_n_neighbors_below_two_raises,test_hdbscan_min_cluster_size_below_two_raises,test_hdbscan_min_samples_below_one_raises,test_sampling_with_no_chunks_at_all_raises,test_summaries_llm_concurrency_below_one_raises}`
- [x] Deps declared + locked + installed + `slow` marker — `tests/unit/test_clustering_dependencies.py::TestDeclaredDependencies` (3 tests) and the commands in Evidence
- [x] Lazy-import guard — `tests/unit/test_clustering_dependencies.py::TestNothingImportsTheClusteringStack::test_entry_point_leaves_the_clustering_stack_unimported[memory-pipeline|offline-pipeline|mcp-server-rag|mcp-server-graphrag]`
- [x] `MemoryCluster` noise / naive `created_at` / live insert / id builder — `tests/unit/entities/test_clusters.py::{TestNoiseIsNeverARow,TestTzAwareEnforcement,TestCollectionRegistration::test_inserted_row_lands_in_the_memory_clusters_collection,TestBuildClusterId}`
- [x] Registered + exported — `…::TestCollectionRegistration::test_registered_with_beanie`, `…::TestEntitiesExports::test_entities_package_exports_the_cluster_surface`
- [x] Chunk fields child-only, default `None`, described — `tests/unit/entities/test_memory.py::TestChunkClusterFields` (15 tests)
- [x] `ontology_schema.json` unchanged — `git status` shows no diff under `tests/unit/entities/snapshots/` (`chunk` is not LLM-extractable, and the new fields are top-level meta fields, not ontology properties)
- [x] README bullet updated
- [x] `make memory-format-check && make memory-lint-check && make memory-tests` green (plus `make pre-commit`)

**Evidence**

```
$ make memory-tests                      # baseline, before any change
============================ 2452 passed in 24.64s =============================
make memory-tests  19.20s user 2.86s system 76% cpu 28.927 total

$ make memory-tests                      # after uv sync, WITHOUT the new guard file
============================ 2520 passed in 22.23s =============================
make memory-tests  19.56s user 2.44s system 85% cpu 25.797 total

$ make memory-tests                      # final
============================ 2529 passed in 30.44s =============================
make memory-tests  27.01s user 3.37s system 89% cpu 34.118 total

$ make memory-format-fix && make memory-lint-fix && make memory-format-check && make memory-lint-check
All checks passed! / 269 files already formatted

$ make pre-commit
Validate pyproject.toml..............................(no files to check)Skipped
prettier.................................................................Passed
ruff check...............................................................Passed
ruff format..............................................................Passed
biome check (harness)....................................................Passed

$ grep -n "umap-learn\|scikit-learn" apps/memory/pyproject.toml
35:    "umap-learn>=0.5.12",
36:    "scikit-learn>=1.9",

$ uv lock --check
Resolved 247 packages in 2ms

$ uv --directory apps/memory run python -c "import umap, sklearn.cluster; print(umap.__version__, sklearn.__version__)"
0.5.12 1.9.0                              # cold: 21s wall (numba compile), warm: ~3s

$ uv --directory apps/memory run pytest --markers | grep -A1 "@pytest.mark.slow"
@pytest.mark.slow: runs real UMAP/HDBSCAN; numba JIT compiles on the first call

# Story 1 — operator loosens clustering for a small corpus
$ uv --directory apps/memory run python -c "...print(load_app_config().memory.clustering.hdbscan.min_cluster_size)"
15
$ TREE_MEMORY__CLUSTERING__HDBSCAN__MIN_CLUSTER_SIZE=5 uv --directory apps/memory run python -c "...same..."
5

# Story 2 — `enabled: false` added under memory.clustering in the REAL configs/default.yaml (restored after)
exit code: 1
pydantic_core._pydantic_core.ValidationError: 1 validation error for AppConfig
memory.clustering.enabled
  Extra inputs are not permitted [type=extra_forbidden, input_value=False, input_type=bool]

# Story 3 — developer inspects the new collection shape
$ uv --directory apps/memory run python -c "from tree.entities import MemoryCluster; print(MemoryCluster.model_json_schema()['required'])"
['_id', 'user_id', 'run_id', 'cluster_id', 'label', 'summary', 'keywords', 'size', 'sample_chunk_ids', 'centroid', 'created_at']
```

**Notes**

- **Story 4 (suite start time) measured, and it holds.** With the deps installed but the new guard file removed the suite is 22.23s vs the 24.64s baseline — installing `umap-learn`/`scikit-learn` costs the suite nothing because nothing imports them. The final 30.44s is entirely the new guard file: 4 fresh-interpreter boots (~2s each; two of them boot the whole MCP server). Deliberate trade-off — that guard is the only thing that will catch a module-scope `import umap` landing in #116/#117. If it becomes annoying it can be `slow`-marked later.
- **`cluster_id` has no `ge=0` Field constraint; a validator owns the bound.** With `ge=0` Pydantic short-circuits and the message reads "Input should be greater than or equal to 0", which never says *why*. The validator's message names noise, as the AC requires.
- **`keywords` / `sample_chunk_ids` are REQUIRED (no `default_factory`)** so `model_json_schema()['required']` matches Story 3 exactly. A failed summary writes explicit empty lists.
- **`scikit-learn` was already in the lock at 1.8.0** (transitively, via the `local-models` extra's `sentence-transformers`); it is now a direct dependency at 1.9.0. It was NOT installed in the default (no-extra) sync before, so this is a real addition to the Prefect Managed install.
- **`uv sync` without `--extra local-models` prunes torch/sentence-transformers/modal.** I ran a plain `uv sync` while verifying and restored the dev env with `make memory-build` (which syncs the extra). Nothing in the repo changed; noting it because the same trap will bite the next person.
- **prettier reformatted the YAML inline comments** (collapses the column alignment in the task's snippet to a single space). Values are identical to the spec; the alignment is not recoverable under the repo's pre-commit.
- **Ontology snapshot unchanged**, as predicted: `cluster_id`/`viz` are top-level graph-modeling meta fields (ADR-001 §11), not ontology `properties_schema` fields, and `chunk` is not LLM-extractable.

### [Tester] 2026-09-06 09:30 — QA

**Test summary**
- Format / lint / pre-commit: PASS (`make memory-format-check`, `make memory-lint-check`, `make pre-commit` all green)
- Unit tests: 2529 passed / 0 failed (`make memory-tests`, local env)
- Integration tests: N/A — no integration suite by design
- Warnings: 1 (`opik`'s `pydantic.v1` UserWarning) — pre-existing, sourced from `.venv` site-packages, present on `main` before this diff (unrelated dependency, filtered elsewhere in `filterwarnings`); not introduced by this task

**E2E adversarial pass**
- Happy path: `TREE_MEMORY__CLUSTERING__HDBSCAN__MIN_CLUSTER_SIZE=5 uv run python -c "from tree.config.app_config import load_app_config; print(load_app_config().memory.clustering.hdbscan.min_cluster_size)"` → `5` (without the var → `15`) (PASS)
- Break path 1 (malformed/typo env override — nested sub-model has no `extra=forbid`): `TREE_MEMORY__CLUSTERING__UMAP__FOO=1 uv run python -c "...load_app_config()..."` → loads successfully, `FOO` silently dropped, no error. Judged against spec intent: the AC text explicitly scopes `extra="forbid"` to `ClusteringConfig` only, and `ClusteringConfig` is literally the *only* model in the entire `app_config.py` with `extra="forbid"` (grepped — zero other sub-models, old or new, have it). This is consistent with, not a regression against, existing codebase convention; a typo under `extraction.dedup.*` today has the identical silent-drop behavior. Not a FAIL — flagged as a pre-existing, codebase-wide footgun outside this task's scope (PASS with note).
- Break path 2 (hostile/edge schema inputs on `MemoryCluster`/`ChunkViz`): `size=0` → raises (ge=1, OK). `label=""` → allowed (no min-length constraint specified in the task's Pydantic contract — not a spec gap). `keywords=[]` → allowed (required-but-empty; matches ADR-007 §4's documented fail-open path: "a failed summary writes explicit empty lists"). `ChunkViz(x=nan, y=inf, run_id="r1")` → allowed, both values pass through untouched (Pydantic's default float validation does not reject NaN/Inf in Python-object mode). This is a legitimate corner case worth a note for #116 (the actual UMAP writer), since NaN/Inf coordinates would break the Sigma.js map — but out of scope for #115, which only builds the schema; no AC requires numeric-finiteness validation here (PASS with note).
- Break path 3 (validator ordering / cross-row-kind interaction): child chunk row with `cluster_id=-1, viz=None` → allowed (noise before summary/coords land — matches "−1 = noise" semantics). Parent row with explicit `cluster_id=None, viz=None` → allowed (short-circuits before the type/subtype check, as required). `document` row with `cluster_id=1` → raises `ValueError` naming `child`. Edge row (`kind="edge", type="part_of"`) with `cluster_id=5` explicitly set → raises `ValueError` naming `child` (edges never get `type == "chunk"`, so they correctly fall through to the rejection branch rather than being silently ignored) (PASS).
- Break path 4 (write-path pollution — probing the `model_dump()` → `cluster_id: null`/`viz: null` concern flagged by the orchestrator): grepped `src/tree/memory/rag/load.py` (`_build_node_op`) and `src/tree/memory/pipeline.py` (`_apply_writes` → `add_entity`/`_build_edge_op`) — every write path builds raw pymongo aggregation-pipeline `$set` dicts by hand; `MemoryEntry(...)` is never instantiated and `.model_dump()` is never called anywhere under `src/tree` outside the class's own definition (grep confirmed). So no write path today ever puts `cluster_id: null` / `viz: null` onto a document/parent/entity row — the concern doesn't materialize in #115's diff (PASS, no regression found).

**Acceptance criteria**
- [x] PASS — `memory.clustering` loads from frozen fixture + `default.yaml` with the exact ADR-007 §1 values, comment block carries `run_clustering` + `never on the 2D` — verified via `tests/unit/config/test_app_config.py::TestClusteringConfig` (all pass) and manual `load_app_config` calls against both YAML files
- [x] PASS — Env hatch / bad metric / `enabled` hard-fail — manually reran the exact literal commands: `TREE_MEMORY__CLUSTERING__HDBSCAN__MIN_CLUSTER_SIZE=5` → `5`; `TREE_MEMORY__CLUSTERING__UMAP__METRIC=manhattan` → `ValidationError` naming `'cosine'` and `'euclidean'`; `TREE_MEMORY__CLUSTERING__ENABLED=true` → `ValidationError: memory.clustering.enabled / Extra inputs are not permitted`
- [x] PASS — Every bound (`UmapConfig(n_neighbors=1)`, `HdbscanConfig(min_cluster_size=1)`, `HdbscanConfig(min_samples=0)`, `ClusterSamplingConfig(nearest=0, random=0)`, `ClusterSummariesConfig(llm_concurrency=0)`) raises — manually re-ran each in a Python one-liner, all raised `ValidationError`
- [x] PASS — Deps declared, locked, installed, `slow` marker registered — `grep -n "umap-learn\|scikit-learn" apps/memory/pyproject.toml` → both lines present; `uv lock --check` → `Resolved 247 packages in 2ms`; `uv run python -c "import umap, sklearn.cluster"` → `0.5.12 1.9.0`; `uv run pytest --markers | grep -A1 slow` → `@pytest.mark.slow: runs real UMAP/HDBSCAN...`
- [x] PASS — Lazy-import guard — `tests/unit/test_clustering_dependencies.py` (9 tests) pass in 8.55s; manually confirmed `import tree.entities.clusters` and `import tree.db` also leave `umap`/`sklearn`/`numba`/`pynndescent` out of `sys.modules` (not directly covered by the guard test but probed manually, both clean)
- [x] PASS — `MemoryCluster` noise / naive `created_at` / live insert / id builder — `tests/unit/entities/test_clusters.py` (20 tests) pass; manually inserted a valid row into a live test-DB collection, confirmed `memory_clusters` in `list_collection_names()` and `user_run` in `list_indexes()` names; `build_cluster_id(uid, 3) == f"{uid}:cluster:3"` confirmed
- [x] PASS — Registered + exported — `MemoryCluster in tree.db.ALL_DOCUMENT_MODELS` → `True`; `from tree.entities import MemoryCluster, ClusterCentroid, build_cluster_id, MEMORY_CLUSTERS_COLLECTION` → all import cleanly
- [x] PASS — Chunk fields child-only, default `None`, described — `tests/unit/entities/test_memory.py::TestChunkClusterFields` (15 tests) pass; manually confirmed the AC's exact example validates, a `document` row and a `subtype="parent"` row both raise naming `child`, and a plain child row round-trips with both fields `None`
- [x] PASS — `ontology_schema.json` unchanged — `git diff --stat -- apps/memory/tests/unit/entities/snapshots/ontology_schema.json` → empty; `git status --porcelain` on the snapshots dir → empty
- [x] PASS — README bullet updated — `git diff -- apps/memory/README.md` shows the `memory` bullet now lists `clustering (umap, hdbscan, sampling, summaries)` and states the switch is the flow parameter
- [x] PASS — `make memory-format-check && make memory-lint-check && make memory-tests` green — reran independently, all green (2529 passed, 0 failed)

**Evidence**
```
$ make memory-tests
============================ 2529 passed in 29.88s =============================

$ make memory-format-check && make memory-lint-check
269 files already formatted
All checks passed!

$ make pre-commit
prettier.................................................................Passed
ruff check...............................................................Passed
ruff format..............................................................Passed
biome check (harness)....................................................Passed

$ TREE_MEMORY__CLUSTERING__ENABLED=true uv run python -c "from tree.config.app_config import load_app_config; load_app_config()"
pydantic_core._pydantic_core.ValidationError: 1 validation error for AppConfig
memory.clustering.enabled
  Extra inputs are not permitted [type=extra_forbidden, input_value=True, input_type=bool]

$ uv run python -c "from tree.entities import MemoryCluster; print(MemoryCluster.model_json_schema()['required'])"
['_id', 'user_id', 'run_id', 'cluster_id', 'label', 'summary', 'keywords', 'size', 'sample_chunk_ids', 'centroid', 'created_at']

# live-DB probe (index + collection):
INDEXES: ['_id_', 'user_run']
memory_clusters in collection names: True

# child-only validator probes:
document row cluster_id=1 -> raised OK: Value error, cluster_id may only be set on a child chunk row (type='chunk', subtype='child') ...
edge row cluster_id=5 -> raised OK: Value error, cluster_id may only be set on a child chunk row ... got type='part_of', subtype=None
child cluster_id=-1, viz=None -> ALLOWED
parent row cluster_id=None, viz=None -> ALLOWED (as expected)
```

**Other issues found**
- `ChunkViz`/`MemoryCluster.centroid` accept `NaN`/`Inf` float coordinates with no `allow_inf_nan=False` guard. Not a #115 spec gap (no AC requires it), but worth a follow-up note for #116 (the UMAP writer) to sanity-check its own output before writing, since a `NaN` coordinate would silently break the Sigma.js map in #118.
- `extra="forbid"` is scoped to `ClusteringConfig` only (confirmed: the *only* model in `app_config.py` with it), so a typo like `TREE_MEMORY__CLUSTERING__UMAP__N_NEIGHBOR=20` (missing the `s`) is silently dropped rather than erroring. Matches existing codebase-wide convention and the AC's literal scope — not a regression, but the same footgun exists everywhere else in the config system and could be a good target for a follow-up "harden the escape hatch" task.
- The 4-boot lazy-import guard (`test_clustering_dependencies.py::TestNothingImportsTheClusteringStack`) is un-`slow`-marked and costs ~8s of the ~30s suite. Judged acceptable: it is the only regression guard for #116/#117's lazy-import contract, and the SWE already flagged the tradeoff explicitly with a plan to `slow`-mark it later if it becomes annoying.

**VERDICT: PASS**

### [PA] 2026-09-06 22:32 — Acceptance Review

**VERDICT: no issues for this task** (feature-level verdict: REJECT via `tasks/120-pa-rejection-embedding-clusters-viz.md`; none of its items touch #115)

Config knobs, `extra="forbid"` on `memory.clustering` (the `enabled` trap reads as a clear ValidationError naming the key), the `memory_clusters` shape and the child-only `cluster_id`/`viz` validator all match the glossary (**Memory cluster**, **Child chunk**) and ADR-007 Decisions 1/3/6. Nothing to fix here; re-acceptance after the rollup is a formality.

### [PA] 2026-09-06 23:16 — Acceptance Review (round 2)

**VERDICT: ACCEPT**

Round-2 re-review after rollup `tasks/done/120`: only the `min_cluster_size` comment in `configs/default.yaml` changed (says where the override must live); schema and deps untouched. Hand off to the PR Reviewer.
