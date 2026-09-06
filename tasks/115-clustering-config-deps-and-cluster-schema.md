---
id: 115-clustering-config-deps-and-cluster-schema
feature: embedding-clusters-viz
status: pending
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

- [ ] `load_app_config(frozen_config_path).memory.clustering` equals the YAML block above (`umap.n_components == 5`, `hdbscan.min_samples is None`, `sampling == (10, 10)`, `summaries.llm_concurrency == 5`); `configs/default.yaml` carries the same values and the comment block contains the strings `run_clustering` and `never on the 2D`.
- [ ] `TREE_MEMORY__CLUSTERING__HDBSCAN__MIN_CLUSTER_SIZE=5` overrides `min_cluster_size`; `TREE_MEMORY__CLUSTERING__UMAP__METRIC=manhattan` raises a `ValidationError` naming `'cosine'` and `'euclidean'`; `TREE_MEMORY__CLUSTERING__ENABLED=true` is a hard `ValidationError` (`extra="forbid"` on `ClusteringConfig`) — the YAML section must never grow a silent switch.
- [ ] `UmapConfig(n_neighbors=1)`, `HdbscanConfig(min_cluster_size=1)`, `HdbscanConfig(min_samples=0)`, `ClusterSamplingConfig(nearest=0, random=0)`, `ClusterSummariesConfig(llm_concurrency=0)` each raise.
- [ ] `grep -n "umap-learn\|scikit-learn" apps/memory/pyproject.toml` shows both under `[project].dependencies`; `uv lock --check` passes; `uv run python -c "import umap, sklearn.cluster"` succeeds; the `slow` marker is registered (`uv run pytest --markers | grep slow`).
- [ ] A subprocess test asserts that `import tree.memory.pipeline`, `import tree.mcp.server` (rag and graphrag) and `import tree.offline` do NOT put `umap`, `sklearn`, `numba` or `pynndescent` in `sys.modules` (this task adds no imports; the test guards #116/#117 from regressing).
- [ ] `MemoryCluster(...)` with `cluster_id=-1` raises `ValueError` mentioning `noise`; with a naive `created_at` raises mentioning `timezone-aware`; a valid row inserted in the unit-test database lands in the `memory_clusters` collection (`list_collection_names()` contains it); `build_cluster_id(uid, 3) == f"{uid}:cluster:3"`.
- [ ] `tree.db.ALL_DOCUMENT_MODELS` contains `MemoryCluster`; `tree.entities` exports `MemoryCluster`, `ClusterCentroid`, `build_cluster_id`, `MEMORY_CLUSTERS_COLLECTION`.
- [ ] `MemoryEntry(kind="node", type="chunk", subtype="child", cluster_id=2, viz={"x": 0.1, "y": -3.2, "run_id": "r1"}, ...)` validates; the same fields on a `document` row or a `subtype="parent"` row raise `ValueError` mentioning `child`; a row without the fields round-trips with `cluster_id is None and viz is None`; every new field has a non-empty `description` (`test_field_descriptions.py` pattern).
- [ ] `tests/unit/entities/snapshots/ontology_schema.json` unchanged (`chunk` is not LLM-extractable); if it changes the SWE explains why in the log.
- [ ] `apps/memory/README.md` "`default.yaml` sections" bullet for `memory` lists `clustering` (`umap`, `hdbscan`, `sampling`, `summaries`) and states the switch is the flow parameter, not YAML.
- [ ] `make memory-format-check && make memory-lint-check && make memory-tests` green.

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
