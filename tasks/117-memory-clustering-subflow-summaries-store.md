---
id: 117-memory-clustering-subflow-summaries-store
feature: embedding-clusters-viz
status: pending
---

# `memory_clustering` sub-flow (phase 4, `run_clustering`) with LLM cluster summaries and the `memory_clusters` store

Tags: `pipeline`, `offline`, `memory`, `clustering`, `scripts`
Depends on: #114, #116
Blocks: #119
Implements: ADR-007 — Decisions 3 (storage), 4 (summaries), 5 (pipeline shape)

## Scope

**1. `tree/memory/clustering/summaries.py`** — prompt + call:
- `SUMMARY_PROMPT_VERSION = "v1"` (a task input so a prompt edit busts the task cache);
  `_SYSTEM_PROMPT` / `build_summary_prompt(samples: list[str]) -> str`: "These are N text chunks
  from one cluster of a personal knowledge base. Return JSON `{"label": …, "summary": …,
  "keywords": […]}`: `label` ≤ 6 words naming the topic; `summary` ≤ 100 words describing what
  the chunks share; `keywords` 3–5 short lowercase phrases." Samples are numbered and each
  truncated to 600 characters.
- `async summarise_cluster(llm: BaseLLM, samples: list[str]) -> ClusterSummary`: one
  `llm.generate_json(prompt, system=_SYSTEM_PROMPT)` call, validated into `ClusterSummary`
  (a `ValidationError` propagates — the task retries).
- `fallback_summary(cluster_id: int) -> ClusterSummary`-like value `label=f"Cluster {id}"`,
  `summary=""`, `keywords=[]` (built without validation) — used when retries are exhausted.

**2. `tree/memory/clustering/store.py`** — every Mongo touch of the feature (raw pymongo on
`MEMORY_COLLECTION` / `MEMORY_CLUSTERS_COLLECTION`, `user_id`-scoped, following
`rag/indexing.py`'s style):
- `ChildEmbeddingRow(BaseModel)`: `chunk_id, embedding: list[float], title, heading_path, content`.
- `async load_child_embeddings(client, database, user_id) -> list[ChildEmbeddingRow]` — filter
  `{user_id, kind: "node", type: "chunk", subtype: "child", embedding.0: {"$exists": true}}`,
  projection `_id, embedding, properties.title, properties.heading_path, properties.content`,
  sorted by `_id` (deterministic input order → deterministic labels).
- `async write_clustering_run(client, database, user_id, *, run_id, chunk_ids, labels, coords,
  clusters: list[MemoryClusterInfo], now) -> ClusterWriteCounts(clusters_written, chunks_updated,
  clusters_deleted)`: (a) `delete_many({user_id, run_id: {"$ne": run_id}})` on `memory_clusters`;
  (b) one `bulk_write(ordered=False)` of `UpdateOne(..., upsert=True)` per cluster on `_id =
  build_cluster_id(user_id, cluster_id)` with `$set` of the full row (`created_at = now`);
  (c) one `bulk_write(ordered=False)` of `UpdateOne({"_id": chunk_id}, {"$set": {cluster_id,
  viz: {x, y, run_id}, updated_at: now}})` per chunk. Idempotent for a given `run_id`; a
  cluster id absent from this run but present from the previous one is removed by (a) —
  with deterministic `_id`s per `(user, cluster_id)` the same id from an OLDER run is
  overwritten by (b), so no stale label survives.
- `async latest_run_id(client, database, user_id) -> str | None`: newest `memory_clusters` row's
  `run_id` (`sort created_at desc`); when no cluster rows exist, the `viz.run_id` of any child
  row carrying `viz` (an all-noise run) — else `None`.
- `async load_embedding_map(client, database, user_id) -> EmbeddingMap | None`: `None` when no
  run; else clusters (sorted `size` desc) + `points` = children with `viz.run_id == run_id`
  (projection: `_id, cluster_id, viz, properties.title, properties.heading_path,
  properties.content` → `MapPoint`, snippet 160 chars) + `total_children` (children with a
  non-empty embedding) + `unclustered = total_children - len(points)`.

**3. The sub-flow** — in `tree/memory/pipeline.py`, sibling of `memory_indexing`:
```python
@flow(name="memory-clustering-etl", log_prints=True)
async def memory_clustering(user_id: PydanticObjectId, opik_trace_headers=None) -> ClusteringStats
```
`ClusteringStats(BaseModel)`: `run_id: str | None, chunks_total: int, clustered: int,
noise: int, clusters: int, summaries_failed: int, skipped_reason: str | None`.
Body (Opik span `memory-clustering-etl`, tags `TAGS_CLUSTERING = [TAG_MEMORY_PIPELINE]` — new
constant in `tree/config/constants.py`): `run_id` = the current Prefect flow-run id
(`prefect.runtime.flow_run.id`) or `uuid4().hex` outside a run;
1. `load_child_embeddings_task` (`NO_CACHE`, `retries=3, retry_delay_seconds=5`) — if
   `len(rows) < config.hdbscan.min_cluster_size` → log WARNING
   `"clustering skipped: N child embeddings < min_cluster_size M"`, return
   `ClusteringStats(skipped_reason=...)` WITHOUT touching the store (the previous run stays).
2. `reduce_and_cluster_task` (`NO_CACHE`, `retries=1, retry_delay_seconds=5`; task-worthy
   as long compute; the single retry covers a cold numba-cache race on a fresh container, and
   a re-run after compile is cheap) — wraps `core.reduce_and_cluster` on a stacked
   `np.asarray(embeddings, dtype=np.float32)`.
3. Per non-noise cluster, `summarise_cluster_task(samples, cluster_id, prompt_version)`
   (`_INPUTS_NO_HEADERS` cache, `cache_expiration` 90 days, `retries=2, retry_delay_seconds=5`
   `# billable — capped at 2`), fanned out with `asyncio.gather(return_exceptions=True)` under
   `asyncio.Semaphore(config.summaries.llm_concurrency)`; `get_llm()` built inside the task. A
   cluster whose task fails after retries gets `fallback_summary` + WARNING; `summaries_failed`
   counts them. Samples come from `core.sample_cluster_members(..., seed=config.umap.random_state)`
   and the rows' `content`.
4. `write_clustering_run_task` (`NO_CACHE`, `retries=3, retry_delay_seconds=5`).
Logs a summary line: `"clustering run {run_id}: {clusters} clusters, {clustered} chunks, {noise}
noise, {summaries_failed} fallback summaries"`; an all-noise run logs WARNING
`"0 clusters found — all N chunks are noise; lower memory.clustering.hdbscan.min_cluster_size"`.
Mode-orthogonal: identical in `rag` and `graphrag`; writes NO graph rows and NO edges.

**4. Phase 4 on `offline_pipeline`** — `run_clustering: bool = False` after `run_indexing`,
forwarded by `dispatch_offline_pipeline`; per target user `with tags(*TAGS_CLUSTERING): stats =
await memory_clustering(user_id=uid)`, per-user failure isolation, result key
`"clustering": {uid: stats.model_dump() | {"error": ...}}`; user resolution happens when ANY of
phases 2–4 is on; all-four-off no-op returns the four empty keys. The nightly cron keeps
defaults → clustering OFF.

**5. Script + Make target (glue only):** `scripts/run_clustering_pipeline.py` →
`dispatch_offline_pipeline(user_id=..., run_data=False, run_extraction=False,
run_indexing=False, run_clustering=True)` + `wait_for_dispatch` + `flush_opik()`; `USER_ID` /
`USER_IDENTIFIER` via `user_options`; `apps/memory/Makefile` `run-clustering-pipeline` target
("CLUSTERING phase only … needs served workflows"). `run_data_pipeline.py`,
`run_memory_pipeline.py`, `run_indexing_pipeline.py` do NOT pass `run_clustering` (default False).
README: "Memory clustering" section after "Memory indexing" (what it computes, where it lands,
the JIT cold-start note, how to loosen `min_cluster_size`), "Pipelines at a glance" row.

## Acceptance Criteria

- [ ] `build_summary_prompt(["a", "b"])` numbers the samples, contains the words `label`, `summary`, `keywords`, `6 words`, `100 words`, `3–5`; a 2,000-char sample is cut to 600 chars in the prompt.
- [ ] `summarise_cluster` with a fake `BaseLLM` returning `{"label": "Agent memory design", "summary": "...", "keywords": ["memory", "agents", "rag"]}` returns the validated `ClusterSummary`; a fake returning `{"label": "one two three four five six seven", ...}` raises `ValidationError`; the LLM is awaited exactly once per call.
- [ ] `load_child_embeddings` issues a `find` whose filter is exactly `{"user_id": uid, "kind": "node", "type": "chunk", "subtype": "child", "embedding.0": {"$exists": True}}` and returns rows sorted by `_id`; parents, documents and an unembedded child are excluded (Beanie fixture in the unit-test DB).
- [ ] `write_clustering_run` on the unit-test DB with a previous run `r0` (clusters 0,1,2) and a new run `r1` (clusters 0,1): afterwards `memory_clusters` holds exactly 2 rows, both `run_id == "r1"`, `_id`s `{uid}:cluster:0/1`; every listed chunk has `cluster_id` and `viz == {x, y, run_id: "r1"}`; a chunk labelled `-1` gets `cluster_id == -1` AND coordinates; calling it again with the same `r1` args leaves counts unchanged (idempotent); `created_at` is tz-aware.
- [ ] `latest_run_id` returns `"r1"` after the write above; returns the child `viz.run_id` when `memory_clusters` is empty but children carry `viz` (all-noise run); returns `None` on a fresh user.
- [ ] `load_embedding_map` on a fixture of 5 embedded children (3 in `r1`, 1 with `viz.run_id == "r0"`, 1 with `cluster_id is None`) returns `total_children == 5`, `len(points) == 3`, `unclustered == 2`, clusters sorted by `size` desc, each point's `snippet` ≤ 160 chars and `heading_path` a list; returns `None` when no run exists.
- [ ] `memory_clustering` (all four tasks patched; store fakes): with 40 rows and `min_cluster_size=15` it calls `reduce_and_cluster` once, `summarise_cluster` once per non-noise label, `write_clustering_run` once with `run_id == <flow run id>` and clusters carrying `size`, `sample_chunk_ids` (20 ids for a 40-member cluster), `centroid_x/centroid_y`; returns `ClusteringStats(clusters=k, clustered=n_nonnoise, noise=n_noise)`.
- [ ] With 10 rows (< 15) the flow returns `skipped_reason` containing `10` and `15`, and neither `reduce_and_cluster` nor the store's write is called; `umap` is not in `sys.modules` afterwards (subprocess variant).
- [ ] A `summarise_cluster_task` that raises on every attempt yields a written cluster with `label == "Cluster {id}"`, `summary == ""`, `keywords == []`, `summaries_failed == 1`, and the flow still COMPLETES; the semaphore bounds concurrent summary calls to `llm_concurrency` (existing `TestDedupeEntitiesParallelization` pattern).
- [ ] `summarise_cluster_task`'s cache key excludes `opik_trace_headers` and includes `prompt_version` (existing `TestTraceHeadersCacheExclusion` pattern); its `retries == 2` with an inline `# billable — capped at 2` comment.
- [ ] `offline_pipeline(run_clustering=True, user_id=U)` awaits `memory_clustering(user_id=U)` after `memory_indexing`; default `run_clustering=False` never awaits it and returns `"clustering": {}`; `offline_pipeline(run_data=False, run_extraction=False, run_indexing=False, run_clustering=True)` still resolves users; one user's clustering error is isolated; `dispatch_offline_pipeline` forwards `run_clustering`.
- [ ] `tests/unit/scripts/test_run_clustering_pipeline.py` (mirrors `test_run_indexing_pipeline.py`): dispatches with exactly `run_data=False, run_extraction=False, run_indexing=False, run_clustering=True`; a failed run exits non-zero; `make memory-run-clustering-pipeline USER_IDENTIFIER=paul` is wired (`grep -n run-clustering-pipeline apps/memory/Makefile`).
- [ ] `orchestrator._DEPLOYMENT_SPECS` still has exactly 5 entries; `grep -rn "memory_clustering\|memory-clustering" apps/memory/src/tree/orchestrator.py` is empty (no new deployment).
- [ ] `grep -rn "kind.*edge\|build_edge_id" apps/memory/src/tree/memory/clustering` returns nothing (no graph rows, no edges); the layout guards from #116 still pass.
- [ ] `make memory-format-check && make memory-lint-check && make memory-tests` green.

## User Stories

### Story: Operator clusters the memory for the first time
1. Operator runs `make memory-serve-workflows` and `make memory-run-clustering-pipeline USER_IDENTIFIER=paul`.
2. The CLI streams ONE `offline-pipeline` run containing only a `memory-clustering-etl` subflow with tasks `load-child-embeddings`, `reduce-and-cluster`, `summarise-cluster` (×k) and `write-clustering-run`.
3. The final log line reads `clustering run <id>: 7 clusters, 412 chunks, 63 noise, 0 fallback summaries`; `mongosh` shows 7 rows in `memory_clusters` with labels such as `Agent memory architectures`, and every child chunk carries `cluster_id` and `viz`.

### Story: Re-running replaces the previous run entirely
1. The operator re-runs the command the next day after ingesting new documents.
2. `memory_clusters` holds only the new run's rows (no leftovers from the old id set); every child, old and new, carries the new `run_id`.

### Story: Corpus too small to cluster
1. A fresh user has 9 child chunks.
2. The run completes with `skipped_reason: "9 child embeddings < min_cluster_size 15"`, nothing is written, and the log tells the operator to lower `TREE_MEMORY__CLUSTERING__HDBSCAN__MIN_CLUSTER_SIZE`.

### Story: Gemini hiccups on one cluster
1. During summarisation, one cluster's call fails three times (429).
2. The run still completes; that cluster is stored as `Cluster 4` with an empty summary and a WARNING names it; the other clusters have real labels.

### Story: Nightly run is unaffected
1. The `offline-pipeline` cron fires with its defaults.
2. No `memory-clustering-etl` subflow appears; `memory_clusters` is untouched; no `umap` import happens in the worker (the phase is off).

## Out of scope

- Rendering, the MCP tool, the CLI visualiser (#118, #119).
- Run history / comparing runs; a new Prefect deployment; clustering entity nodes.
- Reusing `extraction.llm_concurrency` (clustering is mode-orthogonal; it owns its knob).

---

Blocked by: #114, #116

## Log
