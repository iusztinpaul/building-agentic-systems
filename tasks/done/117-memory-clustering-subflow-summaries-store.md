---
id: 117-memory-clustering-subflow-summaries-store
feature: embedding-clusters-viz
status: done
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

- [x] `build_summary_prompt(["a", "b"])` numbers the samples, contains the words `label`, `summary`, `keywords`, `6 words`, `100 words`, `3–5`; a 2,000-char sample is cut to 600 chars in the prompt.
- [x] `summarise_cluster` with a fake `BaseLLM` returning `{"label": "Agent memory design", "summary": "...", "keywords": ["memory", "agents", "rag"]}` returns the validated `ClusterSummary`; a fake returning `{"label": "one two three four five six seven", ...}` raises `ValidationError`; the LLM is awaited exactly once per call.
- [x] `load_child_embeddings` issues a `find` whose filter is exactly `{"user_id": uid, "kind": "node", "type": "chunk", "subtype": "child", "embedding.0": {"$exists": True}}` and returns rows sorted by `_id`; parents, documents and an unembedded child are excluded (Beanie fixture in the unit-test DB).
- [x] `write_clustering_run` on the unit-test DB with a previous run `r0` (clusters 0,1,2) and a new run `r1` (clusters 0,1): afterwards `memory_clusters` holds exactly 2 rows, both `run_id == "r1"`, `_id`s `{uid}:cluster:0/1`; every listed chunk has `cluster_id` and `viz == {x, y, run_id: "r1"}`; a chunk labelled `-1` gets `cluster_id == -1` AND coordinates; calling it again with the same `r1` args leaves counts unchanged (idempotent); `created_at` is tz-aware.
- [x] `latest_run_id` returns `"r1"` after the write above; returns the child `viz.run_id` when `memory_clusters` is empty but children carry `viz` (all-noise run); returns `None` on a fresh user.
- [x] `load_embedding_map` on a fixture of 5 embedded children (3 in `r1`, 1 with `viz.run_id == "r0"`, 1 with `cluster_id is None`) returns `total_children == 5`, `len(points) == 3`, `unclustered == 2`, clusters sorted by `size` desc, each point's `snippet` ≤ 160 chars and `heading_path` a list; returns `None` when no run exists.
- [x] `memory_clustering` (all four tasks patched; store fakes): with 40 rows and `min_cluster_size=15` it calls `reduce_and_cluster` once, `summarise_cluster` once per non-noise label, `write_clustering_run` once with `run_id == <flow run id>` and clusters carrying `size`, `sample_chunk_ids` (20 ids for a 40-member cluster), `centroid_x/centroid_y`; returns `ClusteringStats(clusters=k, clustered=n_nonnoise, noise=n_noise)`.
- [x] With 10 rows (< 15) the flow returns `skipped_reason` containing `10` and `15`, and neither `reduce_and_cluster` nor the store's write is called; `umap` is not in `sys.modules` afterwards (subprocess variant).
- [x] A `summarise_cluster_task` that raises on every attempt yields a written cluster with `label == "Cluster {id}"`, `summary == ""`, `keywords == []`, `summaries_failed == 1`, and the flow still COMPLETES; the semaphore bounds concurrent summary calls to `llm_concurrency` (existing `TestDedupeEntitiesParallelization` pattern).
- [x] `summarise_cluster_task`'s cache key excludes `opik_trace_headers` and includes `prompt_version` (existing `TestTraceHeadersCacheExclusion` pattern); its `retries == 2` with an inline `# billable — capped at 2` comment.
- [x] `offline_pipeline(run_clustering=True, user_id=U)` awaits `memory_clustering(user_id=U)` after `memory_indexing`; default `run_clustering=False` never awaits it and returns `"clustering": {}`; `offline_pipeline(run_data=False, run_extraction=False, run_indexing=False, run_clustering=True)` still resolves users; one user's clustering error is isolated; `dispatch_offline_pipeline` forwards `run_clustering`.
- [x] `tests/unit/scripts/test_run_clustering_pipeline.py` (mirrors `test_run_indexing_pipeline.py`): dispatches with exactly `run_data=False, run_extraction=False, run_indexing=False, run_clustering=True`; a failed run exits non-zero; `make memory-run-clustering-pipeline USER_IDENTIFIER=paul` is wired (`grep -n run-clustering-pipeline apps/memory/Makefile`).
- [x] `orchestrator._DEPLOYMENT_SPECS` still has exactly 5 entries; `grep -rn "memory_clustering\|memory-clustering" apps/memory/src/tree/orchestrator.py` is empty (no new deployment).
- [x] `grep -rn "kind.*edge\|build_edge_id" apps/memory/src/tree/memory/clustering` returns nothing (no graph rows, no edges); the layout guards from #116 still pass.
- [x] `make memory-format-check && make memory-lint-check && make memory-tests` green.

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

### [SWE] 2026-09-06 17:35 — Implementation

**Files modified**

- `apps/memory/src/tree/memory/clustering/summaries.py` (new) — `SUMMARY_PROMPT_VERSION`, `_SYSTEM_PROMPT`, `build_summary_prompt` (numbered samples, 600-char cut), `summarise_cluster` (one `generate_json` → validated `ClusterSummary`), `fallback_summary` (`model_construct`, no validation).
- `apps/memory/src/tree/memory/clustering/store.py` (new) — `ChildEmbeddingRow`, `ClusterWriteCounts`, `load_child_embeddings`, `write_clustering_run`, `latest_run_id`, `load_embedding_map`; raw pymongo, `user_id`-scoped, `rag/indexing.py` style.
- `apps/memory/src/tree/memory/clustering/types.py` — added the `unclustered <= total_children` validator on `EmbeddingMap` (Tester note from #116).
- `apps/memory/src/tree/memory/pipeline.py` — `ClusteringStats`, `_clustering_run_id`, `_stack_embeddings`, the four tasks (`load-child-embeddings` / `reduce-and-cluster` / `summarise-cluster` / `write-clustering-run`), `_summarise_clusters` (semaphore + `gather(return_exceptions=True)` + fail-open), `_build_cluster_infos`, and the `memory_clustering` (`memory-clustering-etl`) sub-flow beside `memory_indexing`.
- `apps/memory/src/tree/config/constants.py` — `TAGS_CLUSTERING = [TAG_MEMORY_PIPELINE]`.
- `apps/memory/src/tree/offline.py` — phase 4: `run_clustering: bool = False` on `offline_pipeline` and `dispatch_offline_pipeline`, per-user isolation, `"clustering"` result key, user resolution when ANY of phases 2–4 is on, four-key no-op.
- `apps/memory/scripts/run_clustering_pipeline.py` (new) + `apps/memory/Makefile` — `run-clustering-pipeline` target (glue only).
- `apps/memory/README.md` — "Memory clustering" section after "Memory indexing" + a row in "Pipelines at a glance".
- Tests: `tests/unit/memory/clustering/test_summaries.py` (new, 15), `tests/unit/memory/clustering/test_store.py` (new, 28), `tests/unit/scripts/test_run_clustering_pipeline.py` (new, 5), `tests/unit/memory/test_pipeline.py` (+39 clustering tests), `tests/unit/test_offline.py` (+8, phase-4 class + dispatcher forwarding; existing helper and 3 tests updated for the 4th phase), `tests/unit/test_clustering_dependencies.py` (+2, the fresh-interpreter skip-path probe), `tests/unit/test_orchestrator.py` (+1, no new deployment).

**Tests**

- Unit: 2750 passing, 0 failing, 0 warnings (baseline 2643 → +107). `make memory-tests`.
- Integration: N/A — the app has no integration suite; e2e is the real pipeline run below.

**Acceptance criteria** (each `- [x]` above → the test that proves it)

- `build_summary_prompt` → `test_summaries.py::TestBuildSummaryPrompt` (numbering, the 6 parametrised prompt fragments, the 600-char cut).
- `summarise_cluster` → `TestSummariseCluster::{test_returns_the_validated_summary,test_calls_the_model_exactly_once_with_the_system_prompt,test_a_chatty_label_raises_so_the_task_retries}`.
- `load_child_embeddings` → `test_store.py::TestLoadChildEmbeddings` (exact filter, exact projection, `_id` order, parents/documents/unembedded excluded, tenant scope).
- `write_clustering_run` → `TestWriteClusteringRun` (replaces r0, overwrites a surviving id, chunk `cluster_id`+`viz`, noise, tz-aware `created_at`, idempotent re-run, counts, other tenant untouched, all-noise, length + naive-datetime guards).
- `latest_run_id` → `TestLatestRunId` (4 tests, incl. the all-noise `viz.run_id` fallback).
- `load_embedding_map` → `TestLoadEmbeddingMap` (7 tests: the 5-child mixed corpus, size-desc legend, 160-char snippet, `None` on a fresh user).
- The flow → `test_pipeline.py::TestMemoryClusteringFlow` (11), incl. `test_stamps_the_prefect_flow_run_id_on_the_write` (asserts `stats.run_id == state.state_details.flow_run_id`) and `test_a_cluster_row_carries_its_evidence_and_centroid` (20 sample ids for the 40-member cluster).
- Skip path → `TestMemoryClusteringSkipsATinyCorpus` (3) + `test_clustering_dependencies.py::TestTheSkipPathImportsNothing` (the subprocess variant: skip reason AND `umap`/`sklearn`/`numba`/`pynndescent` absent from `sys.modules`).
- Fail-open + semaphore → `TestMemoryClusteringFailsOpenPerCluster` (4) and `TestSummariseClustersParallelization` (2).
- Task config → `TestClusteringTaskConfiguration` (retries/cache/`cache_expiration`/names + the inline `# billable — capped at 2` comment).
- Phase 4 → `test_offline.py::TestOfflineClusteringPhase` (7) + `TestDispatchOfflineIngest::test_forwards_the_clustering_phase_flag`.
- Script + Make target → `test_run_clustering_pipeline.py` (5).
- No new deployment → `test_orchestrator.py::test_clustering_did_not_take_a_deployment_slot`.
- No graph rows/edges → `test_pipeline.py::TestClusteringWritesNoGraphRows` + `grep` (empty).

**Evidence**

```
$ make memory-tests
============================ 2750 passed in 47.01s =============================

$ make memory-format-check && make memory-lint-check
281 files already formatted
All checks passed!

$ make pre-commit
ruff check ... Passed   ruff format ... Passed   prettier ... Passed   biome ... Passed

$ grep -rn "kind.*edge\|build_edge_id" apps/memory/src/tree/memory/clustering   # (exit 1, no output)
$ grep -rn "memory_clustering\|memory-clustering" apps/memory/src/tree/orchestrator.py   # (exit 1, no output)
```

Live e2e — served from THIS worktree (`tree-prefect-worker` stopped for the duration, restarted after),
env local, user `paul@example.com` (1448 embedded child chunks, so the default `min_cluster_size=15`
was NOT lowered — no env override needed, no extra ingest):

```
$ make memory-run-clustering-pipeline USER_IDENTIFIER=paul@example.com
Submitted flow run 2fff1de1-42bb-4dde-8f34-c6d2618a4196 (scheduled); waiting for it...
Beginning flow run 'literate-sidewinder' for flow 'offline-pipeline'
Finished in state Completed()          # 41 s wall clock, incl. both UMAP fits

# subflow memory-clustering-etl (run e9bc6378-eacf-4a03-97d0-8e24293d0975):
   summarise_cluster: cluster_id=0 n_samples=20 prompt_version=v1
   ... (37 calls, ≤ 5 in flight — the llm_concurrency semaphore)
   clustering run e9bc6378-eacf-4a03-97d0-8e24293d0975: 37 clusters, 1306 chunks, 142 noise, 0 fallback summaries

$ mongosh ... tree
cluster rows: 37
distinct run_ids: ["e9bc6378-eacf-4a03-97d0-8e24293d0975"]
{"_id":"…:cluster:11","label":"Frontier Open-Weight Model Architectures","size":100,
 "keywords":["open-weight models","mixture of experts","post-training optimization",
 "agentic workflows","llm distillation"],"centroid":{"x":8.71,"y":-2.96}, "summary":"…"}
{"_id":"…:cluster:23","label":"Building custom coding agent harnesses","size":93, …}
{"_id":"…:cluster:33","label":"Agent Knowledge Graph Memory Architectures","size":77, …}
# one child chunk:
{ _id: '…#parent-0#child-0', cluster_id: 32,
  viz: { x: 5.2886958, y: 13.6866636, run_id: 'e9bc6378-…' },
  updated_at: ISODate('2026-09-06T17:20:58.845Z') }
chunks with viz: 1448   noise chunks: 142
```

Re-run (idempotence / wholesale replacement):

```
$ make memory-run-clustering-pipeline USER_IDENTIFIER=paul@example.com   # 25 s
   clustering run 5202b062-8740-4d57-8c7d-cc7a46b55e2e: 37 clusters, 1306 chunks, 142 noise, 0 fallback summaries
   # NO `summarise_cluster:` lines — all 37 summaries served from the INPUTS cache
   # (seeded sampling ⇒ identical cache keys), so the re-run made ZERO LLM calls.
cluster rows: 37
distinct run_ids: ["5202b062-8740-4d57-8c7d-cc7a46b55e2e"]     # the r0 rows are gone
chunk viz run_ids: ["5202b062-8740-4d57-8c7d-cc7a46b55e2e"]    # every chunk re-stamped
chunks with viz: 1448   noise: 142                              # counts unchanged
```

Raw deployment run with the flags spelled out, and the nightly default:

```
$ uv run prefect deployment run offline-pipeline/offline-pipeline \
    -p run_data=false -p run_extraction=false -p run_indexing=false -p run_clustering=true \
    -p user_id=6a8ea9579a7aeb13175955c8
PARENT: Completed
SUBFLOW: memory-clustering-etl Completed
   clustering run 189cfd9c-e0b3-4630-af44-9871e63ad393: 37 clusters, 1306 chunks, 142 noise, 0 fallback summaries

# run_clustering OMITTED (the nightly default) -> no clustering child at all:
$ uv run prefect deployment run offline-pipeline/offline-pipeline \
    -p run_data=false -p run_extraction=false -p run_indexing=true -p user_id=…
PARENT: Completed
SUBFLOW: memory-indexing-etl Completed        # <- the ONLY child

$ uv run prefect deployment inspect offline-pipeline/offline-pipeline
'schedules': [{ 'cron': '0 3 * * *', 'parameters': {'source_files': ['sources/listen.yaml']} }]
# the cron sets no phase flag -> run_clustering falls through to False
```

**Notes**

- Judgement calls: `ClusteringStats` lives in `pipeline.py` (the spec puts it in the sub-flow section; `tree/memory/types.py` drags graph imports, which a mode-orthogonal phase should not). `ClusterWriteCounts` counts rows TOUCHED (upserted+matched / matched), not rows changed, so the numbers are stable across an idempotent re-run. `store.py` imports `NOISE_LABEL` from `clustering/core.py` rather than re-spelling `-1` — `core` is proven import-cheap by `test_clustering_dependencies.py::…[clustering-core]`.
- Tester notes from #116, both addressed: `EmbeddingMap` now validates `0 <= unclustered <= total_children`; NaN/inf and mixed-width embeddings are rejected by `_stack_embeddings` INSIDE `reduce-and-cluster` with a message that names the count and the fix, instead of surfacing as a sklearn `Input contains NaN` four frames down mid-fit.
- `latest_run_id`'s all-noise fallback deliberately queries children WITHOUT the `embedding.0` clause (the spec says "any child row carrying `viz`"); `load_embedding_map`'s point query keeps it, which is what guarantees `unclustered >= 0`.
- The memory watch-item (~6 GB RSS / SIGKILL near `test_voyage_embedding.py`) did NOT reproduce: 4 full-suite runs, 45–47 s each, all green.
- Not done deliberately (out of scope): rendering, the MCP tool, the CLI visualiser (#118/#119); no new Prefect deployment; no graph rows or edges.
- Local dev DB now carries the clustering run (37 rows + `viz` on 1448 chunks) — left in place as fixture data for #118/#119.

### [Tester] 2026-09-06 20:45 — QA

**Test summary**
- Format / lint / pre-commit: PASS (`ruff format`/`ruff check` clean, `pre-commit run --all-files` clean)
- Unit tests: 2750 passed / 0 failed (`make memory-tests`, local env)
- Integration tests: N/A (no integration suite in this repo, per CLAUDE.md — e2e substitutes)
- Warnings: 0

**E2E adversarial pass** (served from THIS worktree, `tree-prefect-worker` stopped for the duration and restarted after; user `paul@example.com` / `6a8ea9579a7aeb13175955c8`, 1448 embedded child chunks)
- Happy path: `make memory-run-clustering-pipeline USER_IDENTIFIER=paul@example.com` → `clustering run 26f79bb3-…: 37 clusters, 1306 chunks, 142 noise, 0 fallback summaries` (PASS)
- Break path 1 (idempotent re-run / state edge): re-ran the same command → new run_id `3c1f2dea-…`, `mongosh` shows `memory_clusters` distinct run_ids = exactly `["3c1f2dea-…"]`, `memory.viz.run_id` distinct = same one value, previous run's id gone, counts unchanged (37/1306/142) (PASS)
- Break path 2 (partial-failure simulation / write convergence): after a clean run, `db.memory_clusters.deleteOne({_id: "…:cluster:0"})` (36 rows left) → re-ran → row `…:cluster:0` recreated by the upsert, count back to 37 (PASS)
- Break path 3 (boundary: `min_cluster_size` far above corpus size): `TREE_MEMORY__CLUSTERING__HDBSCAN__MIN_CLUSTER_SIZE=2000` (>1448) → run completed in ~6s (load→warning was <1ms apart in the worker log, i.e. no UMAP cost paid), `skipped_reason="1448 child embeddings < min_cluster_size 2000"`, `memory_clusters` UNCHANGED (still 37 rows, previous run_id kept) (PASS)
- Break path 4 (all-noise corpus): `TREE_MEMORY__CLUSTERING__HDBSCAN__MIN_CLUSTER_SIZE=1400` → WARNING `"0 clusters found — all 1448 chunks are noise; lower memory.clustering.hdbscan.min_cluster_size"` logged verbatim; `memory_clusters` emptied (0 rows); all 1448 chunks got `cluster_id=-1` + `viz`; `latest_run_id()` called directly against prod DB still resolved (`9a532604-…`) via the child `viz.run_id` fallback (PASS)
- Break path 5 (dependency failure: bad LLM model, cache-poisoning probe): `TREE_MODELS__LLM__MODEL=nonexistent-model-xyz` + `TREE_MEMORY__CLUSTERING__SAMPLING__NEAREST=9` (to force fresh, uncached samples) → Gemini 404s on every non-cached cluster, each retried twice per the `# billable — capped at 2` policy, then written as `Cluster {id}` fallback; run COMPLETED with `37 clusters, 1306 chunks, 142 noise, 33 fallback summaries` (4 of 37 clusters were untouched cache hits from earlier — expected, since those small clusters' sample sets didn't change with the `NEAREST` tweak). Restored a working LLM model (kept the same `NEAREST=9` so the previously-failed clusters' cache keys were identical) and re-ran: `37 clusters … 0 fallback summaries` — every previously-failed cluster now has a real label. This confirms the key probe: **a failed `summarise-cluster` task attempt is never cached as a success** (PASS)

**Acceptance criteria**
- [x] PASS — `build_summary_prompt` numbering/contract fragments/600-char cut — `test_summaries.py::TestBuildSummaryPrompt` (6 parametrised fragments + numbering + truncation tests), read and verified against `summaries.py:59-87`
- [x] PASS — `summarise_cluster` validated happy path + `ValidationError` on a chatty/short response, one call per cluster — `test_summaries.py::TestSummariseCluster` (3 tests) + `TestFallbackSummary` (`model_construct`, no validation)
- [x] PASS — `load_child_embeddings` exact filter/projection/`_id` order/exclusions — `test_store.py::TestLoadChildEmbeddings` (6 tests against the real `unit_tests_twin` DB via Beanie fixtures + a filter/projection-recording collection)
- [x] PASS — `write_clustering_run` replace/overwrite/noise/idempotent/tz-aware/counts/tenant-scope/all-noise/length+naive-datetime guards — `test_store.py::TestWriteClusteringRun` (11 tests); live-verified the r0→r1 replace-wholesale and the delete-one-row-then-reupsert convergence directly against the real corpus (break paths 1–2 above)
- [x] PASS — `latest_run_id` 3 cases (newest run, all-noise fallback, fresh user) + tenant scope — `test_store.py::TestLatestRunId` (4 tests); live-verified the all-noise fallback by calling `latest_run_id()` directly against the prod DB after break path 4
- [x] PASS — `load_embedding_map` 5-child mixed corpus (`total_children`, `points`, `unclustered`, size-desc sort, 160-char snippet, `None` on fresh user) — `test_store.py::TestLoadEmbeddingMap` (7 tests)
- [x] PASS — `memory_clustering` flow orchestration (task-call pattern, `run_id`, cluster row shape incl. 20-sample cap on a 40-member cluster, returned `ClusteringStats`) — `test_pipeline.py::TestMemoryClusteringFlow` (11 tests, all four tasks patched)
- [x] PASS — skip path (reason names both numbers, no reduce/write, `umap` absent from `sys.modules`) — `test_pipeline.py::TestMemoryClusteringSkipsATinyCorpus` (3) + `test_clustering_dependencies.py::TestTheSkipPathImportsNothing` (fresh-interpreter subprocess variant); live-verified via break path 3 (near-zero wall time between load and the skip warning)
- [x] PASS — fail-open per cluster (`Cluster {id}` fallback, `summaries_failed`, flow COMPLETES, semaphore bound) — `test_pipeline.py::TestMemoryClusteringFailsOpenPerCluster` (4) + `TestSummariseClustersParallelization` (2, asserts `max_in_flight == llm_concurrency`); live-verified end-to-end via break path 5, including the no-cache-poisoning follow-up
- [x] PASS — `summarise_cluster_task` cache excludes `opik_trace_headers`, includes `prompt_version`; `retries == 2` with the inline `# billable — capped at 2` comment — `test_pipeline.py::TestClusteringTaskConfiguration` (source-grepped + attribute-asserted)
- [x] PASS — offline phase-4 wiring (awaits after indexing, default off + empty key, resolves users when clustering-only, per-user isolation, dispatcher forwarding) — `test_offline.py::TestOfflineClusteringPhase` (7) + `test_forwards_the_clustering_phase_flag`; diffed against `git show 009db16:apps/memory/tests/unit/test_offline.py` — every existing assertion in the 3 edited tests survives unchanged, only the extra `cluster` mock tuple slot and the new phase's assertions were added
- [x] PASS — `run_clustering_pipeline.py` dispatch args + non-zero exit on failure + Make target wiring — `test_run_clustering_pipeline.py` (5 tests, incl. `grep`-based Makefile check); live-confirmed via every `make memory-run-clustering-pipeline` invocation above
- [x] PASS — 5 deployment specs, no new clustering deployment — `test_orchestrator.py::test_clustering_did_not_take_a_deployment_slot`; `grep -rn "memory_clustering\|memory-clustering" apps/memory/src/tree/orchestrator.py` empty (re-ran, confirmed empty)
- [x] PASS — no graph rows/edges in `clustering/` — `test_pipeline.py::TestClusteringWritesNoGraphRows` + `grep -rn "kind.*edge\|build_edge_id" apps/memory/src/tree/memory/clustering` (re-ran, confirmed empty)
- [x] PASS — `make memory-format-check && make memory-lint-check && make memory-tests` green — re-ran independently, all green, 0 warnings

**Evidence**
```
$ make memory-tests
============================ 2750 passed in 46.77s =============================
maximum resident set size 1361461248 (≈1.3 GB) — memory watch item does not reproduce

$ mongosh … (break path 1, re-run)
clusters: 37
distinct cluster run_ids: ["3c1f2dea-93d3-41fc-af77-dc71d8cab157"]
distinct child viz run_ids: ["3c1f2dea-93d3-41fc-af77-dc71d8cab157"]
chunks with viz: 1448

$ uv run prefect flow-run inspect 26f79bb3-9b26-431b-95d7-61172bc3a283
name='courageous-perch' … parent_task_run_id=UUID('c0053e7b-…')   # a SUBFLOW, matches the stored run_id exactly — probe (f) confirmed

# break path 5, broken-LLM run:
20:38:26.956 | INFO | Flow run 'amusing-avocet' - clustering run 01d838d9-…: 37 clusters, 1306 chunks, 142 noise, 33 fallback summaries
# break path 5, restore-and-recheck:
20:40:16.874 | INFO | Flow run 'diamond-pigeon' - clustering run b79e55a3-…: 37 clusters, 1306 chunks, 142 noise, 0 fallback summaries
```

**Other issues found**
- None blocking. Minor observation (not a defect): with `sampling.nearest`/`random` unchanged, a re-run's `summarise-cluster` INPUTS cache hits regardless of which LLM is configured — expected and by design (ADR-007 §4), just worth knowing when reproducing this probe: forcing a fresh LLM call for an already-cached cluster requires changing a sampling knob (or the corpus) to bust the cache key.

**Cleanup**
- Restored defaults (no env overrides), left DB in a clean, fresh, valid state: `memory_clusters` = 37 rows, all real labels, `memory` chunks all carry a single consistent `run_id` (`bd757d3c-…`) across both `memory_clusters` and `memory.viz.run_id`, 142 noise chunks, 0 fallback labels.
- Served workflows process stopped; `tree-prefect-worker` Docker container restarted.
- `git status --short` after QA shows only the SWE's original changes — no stray files.

**VERDICT: PASS**

### [PA] 2026-09-06 22:32 — Acceptance Review

**VERDICT: REJECT** (feature-level, PR #42)

Read the real run via `mongosh` (37 rows, one `run_id` shared by every cluster row and every child's `viz.run_id`, 0 null assignments, 0 fallback labels, 0 empty summaries, all labels ≤ 6 words, 36/37 with 5 keywords; labels such as "LLM-maintained personal research wikis", "Implementing LLM Structured Outputs" and their summaries are genuinely useful). Skip / all-noise / fallback wording is clear. Found 2 issues carried into the rollup: (1) `apps/memory/README.md:286` tells the operator to prefix `make memory-run-clustering-pipeline` with the `MIN_CLUSTER_SIZE` env var, which is a silent no-op (the flow reads config in the serve process — the e2e skill says so correctly); (2) the CLI cannot distinguish a skipped run from a success (subflow-only logs). Filed rollup task `tasks/120-pa-rejection-embedding-clusters-viz.md` (Issues 1 and 3). Pipeline re-runs from the inner loop on the rollup; on green, re-run acceptance on this task.
