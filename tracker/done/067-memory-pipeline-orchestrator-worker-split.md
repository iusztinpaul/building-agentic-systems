# Memory pipeline: split into orchestrator + worker deployments

Status: pending
Tags: `infra`, `memory`, `refactor`
Depends on: #066
Blocks: #069

## Scope

Replace the single `memory-extraction-etl` deployment (current: one
`memory_extraction(user_id, document_ids=None, num_shards=1)` flow with a
worker/orchestrator branch + recursive self-dispatch) with TWO separate named
deployments. This implements ADR-002 §3 as amended by #066. Dispatches go to the
WORKER deployment — no recursion. `memory-indexing-etl` is UNCHANGED.

### Worker flow — `memory-extract-etl-worker`

- Prefect `@flow(name="memory-extract-etl-worker", log_prints=True)`.
- Signature: `(user_id: PydanticObjectId, document_ids: list[str] | None = None) -> WriteSummary`.
- Body: TODAY'S worker-path extraction — the six-task pipeline that currently lives
  in `memory_extraction` after the `if num_shards > 1` branch
  (`apps/memory/src/tree/memory/extraction/pipeline.py`, the block from the config
  re-validation through `apply_writes_task` and the final `WriteSummary` return).
  Move that body verbatim into the worker flow.
- NO `num_shards` parameter, NO orchestrator branch, NO `_orchestrate_sharded_extraction`
  call, NO `run_deployment`, NO `memory-indexing-etl` trigger. Pure extraction.
- Fetch semantics preserved exactly: explicit `document_ids` → fetch those user-scoped
  docs; `document_ids is None` → fetch all `content != None` docs for the user; zero
  docs → `WriteSummary(documents_processed=0)`. The config-revalidation-at-entry,
  first-person resolver, preference canonicalization/supersession, and the
  `_CachedSingleEmbedding` reuse all carry over unchanged.
- Registered as deployment `memory-extract-etl-worker` in `tree/orchestrator.py`.

### Orchestrator flow — `memory-extract-etl-orchestrator`

- Prefect `@flow(name="memory-extract-etl-orchestrator", log_prints=True)`.
- Signature: `(user_id: PydanticObjectId, document_ids: list[str] | None = None, num_shards: int = 1) -> FanOutStats`.
- Body (reuse the existing pure helpers from #066/sharding):
  1. `_resolve_num_shards(num_shards)` (clamps non-positive → 1).
  2. When `document_ids is None`: `_resolve_pending_document_ids(database, user_id)`
     to compute the pending set; else use the explicit list verbatim.
  3. Empty resolved/explicit set → clean no-op: zero dispatch, zero index run,
     `FanOutStats(shards_total=0)`.
  4. `_partition_into_shards(ids, effective_num_shards)` → `min(num_shards, N)`
     balanced contiguous shards.
  5. Dispatch ONE `memory-extract-etl-worker` run per shard via `run_deployment`
     under `asyncio.gather(return_exceptions=True)`, each with
     `parameters={"user_id": str(user_id), "document_ids": shard}` — NOTE: NO
     `num_shards` key (the worker has no such param). One shard's failure is caught,
     recorded in `FanOutStats.failures[str(idx)]`, and never aborts the others.
  6. After the gather settles: ONE trailing `run_deployment("memory-indexing-etl/memory-indexing-etl", parameters={"user_id": str(user_id)})`,
     fired regardless of how many shards failed (a partial extraction is still
     indexed). Never per-shard.
- The dispatch target constant changes from
  `_EXTRACTION_DEPLOYMENT = "memory-extraction-etl/memory-extraction-etl"` to
  `"memory-extract-etl-worker/memory-extract-etl-worker"`. Update `_fan_out_extraction`
  in `sharding.py` accordingly (it must dispatch the WORKER, not self).
- Registered as deployment `memory-extract-etl-orchestrator` in `tree/orchestrator.py`.

### `_fan_out_extraction` change

The existing `_fan_out_extraction(user_id, shards, run_deployment)` in
`sharding.py` keeps its gather + failure-isolation + single-index shape, but:
- dispatches the WORKER deployment name (above) instead of the extraction deployment;
- drops the `"num_shards": 1` key from each child's `parameters` (the worker has no
  `num_shards` param — passing it would be a Prefect parameter error). Children carry
  only `{"user_id", "document_ids"}`.
- The `num_shards=1` recursion-termination comment/logic is removed (no recursion now —
  it dispatches a distinct worker deployment).

### Orchestrator registration (`tree/orchestrator.py`)

- REMOVE the `memory_extraction.to_deployment(name="memory-extraction-etl", …)`
  registration.
- ADD `memory_extract_etl_worker.to_deployment(name="memory-extract-etl-worker", tags=["memory-pipeline", "extraction", "worker"])`
  and `memory_extract_etl_orchestrator.to_deployment(name="memory-extract-etl-orchestrator", tags=["memory-pipeline", "extraction", "orchestrator"])`.
- KEEP `serve(..., limit=limit)` (the #065 fix — do NOT regress to `global_limit`) and
  the dream-cron registration and every other deployment.
- The data-pipeline registrations are #068's job; leave `data-pipeline-etl` registered
  for now (this task ships independently with data still on its old single deployment).

### Make target + script

- `apps/memory/Makefile` `run-memory-pipeline-extraction`: re-point the underlying
  script at the orchestrator. Keep the `NUM_SHARDS` and `DOC_IDS` optional flags and
  the `USER_ID` guard. Update the help text to say it triggers
  `memory-extract-etl-orchestrator`.
- `apps/memory/scripts/run_memory_pipeline.py`: change `DEPLOYMENT_NAME` from
  `"memory-extraction-etl/memory-extraction-etl"` to
  `"memory-extract-etl-orchestrator/memory-extract-etl-orchestrator"`. The
  `--num-shards >= 1` guard and `--doc-ids` parsing stay. Keep `init_logger()` at
  module level. Update the module docstring to describe the orchestrator/worker split
  (operators run the orchestrator; it dispatches workers + one index).

### Test rework

- `tests/unit/memory/extraction/test_fanout.py`: keep the `_partition_into_shards` /
  `_resolve_num_shards` tests. Rework the `_fan_out_extraction` tests so each child
  carries `{"user_id", "document_ids"}` (NO `num_shards` key) and the dispatched
  deployment name contains `worker` (not `extraction`/self). The single-index,
  failure-isolation, no-shards-no-op, and ordering assertions stay.
- `tests/integration/memory/test_extraction_fanout.py`: re-target from
  `memory_extraction(num_shards=…)` to the new flows. Orchestrator-path tests call
  `memory_extract_etl_orchestrator(...)` and assert: N worker dispatches over disjoint
  shards (each `{user_id, document_ids}`, NO `num_shards`), exactly ONE trailing
  `memory-indexing-etl` run scoped to the user, pending-doc resolution when ids
  omitted, no-op when nothing pending, and one-shard-failure isolation with the index
  still firing. Worker-path tests call `memory_extract_etl_worker(...)` and assert it
  issues ZERO `run_deployment` calls (no self-dispatch, no index) and returns a
  `WriteSummary` (zero-doc no-op for a fresh user keeps it mongot-free). Keep the spy
  pattern + test-DB redirect; patch `run_deployment` and the embedding-model factories
  in the new flow modules.
- `check-kgquery-discipline` allowlist: update any path entry that referenced the old
  flow if the worker/orchestrator bodies moved to new module(s).

### Stale-deployment cleanup (ops note — include in the task log / verification)

After this task is deployed (serve-workflows re-run), the old `memory-extraction-etl`
server-side deployment is orphaned. Document — in the task log and as an explicit
verification step — that the operator must run
`prefect deployment delete memory-extraction-etl/memory-extraction-etl` (and, if it
ever lingered, `memory-extraction-fanout-etl`) to remove the stale definition. This is
an ops action, not a code change; the AC verifies the note exists, the `[HUMAN]` AC
in #069 verifies the live deletion.

## Acceptance Criteria

- [x] A `memory-extract-etl-worker` flow exists with signature
      `(user_id, document_ids=None) -> WriteSummary`, containing the six-task extraction
      body, with NO `num_shards` param, NO orchestrator branch, NO `run_deployment`
      call, and NO `memory-indexing-etl` trigger (grep the worker module: zero
      `run_deployment` / `indexing` references).
- [x] A `memory-extract-etl-orchestrator` flow exists with signature
      `(user_id, document_ids=None, num_shards=1) -> FanOutStats` that resolves/partitions
      and dispatches one `memory-extract-etl-worker` run per shard, then one trailing
      `memory-indexing-etl` run.
- [x] Each worker dispatch carries `parameters={"user_id": str(user_id), "document_ids": shard}`
      with NO `num_shards` key (asserted in the reworked unit + integration tests).
- [x] The trailing index runs exactly ONCE, after the gather, scoped to
      `{"user_id": str(user_id)}`, even when a shard failed (asserted in tests).
- [x] An empty resolved/explicit doc set is a clean no-op: zero worker dispatches, zero
      index run, `FanOutStats(shards_total=0)` (asserted in the integration test).
- [x] One shard's failure is isolated (`gather(return_exceptions=True)`), recorded in
      `FanOutStats.failures`, and the index run still fires (asserted in tests).
- [x] `tree/orchestrator.py` no longer registers `memory-extraction-etl`; it registers
      `memory-extract-etl-worker` and `memory-extract-etl-orchestrator`, keeps
      `serve(..., limit=limit)` (NOT `global_limit`), and keeps the dream cron +
      `memory-indexing-etl` + all data/file/youtube/conversation registrations.
- [x] `python -c "import tree.orchestrator"` succeeds (registration imports resolve).
- [x] `scripts/run_memory_pipeline.py` `DEPLOYMENT_NAME` targets
      `memory-extract-etl-orchestrator/memory-extract-etl-orchestrator`; the
      `--num-shards >= 1` guard still exits 1 on `0`/negative; `init_logger()` is called
      at module level.
- [x] `make memory-run-memory-pipeline-extraction` help text and behavior reference the
      orchestrator; `USER_ID` guard intact; `NUM_SHARDS`/`DOC_IDS` still threaded.
- [x] The stale-`memory-extraction-etl` `prefect deployment delete` cleanup step is
      documented in the task log / verification section.
- [x] `make memory-format-fix && make memory-lint-fix && make memory-format-check && make memory-lint-check && make pre-commit` all clean.
- [x] `make memory-unit-tests` passes (reworked fanout + orchestrator-adjacent unit
      tests green, 0 warnings).
- [x] `make memory-integration-tests-all` passes on a quiesced + isolated mongot stack
      (the reworked memory fan-out integration suite green; run in isolation per
      CLAUDE.md — never against a contended shared stack). — Tester acceptance gate.
      Tester ran the full `-all` suite (slow + requires_mongot) on the quiesced shared
      stack: **269 passed, 1 skipped, 0 failed, 0 warnings in 598s** (no pytest contention;
      the live SERP flake did NOT trip this run). `test_extraction_fanout.py` 10 passed.
- [ ] [HUMAN] Live e2e (deferred to #069's combined live pass, recorded here as the
      memory half): with the docker stack up and `make memory-serve-workflows` running,
      `make memory-run-memory-pipeline-extraction USER_ID=<oid> NUM_SHARDS=2` shows the
      parent run as `memory-extract-etl-orchestrator` and exactly 2 child runs as
      `memory-extract-etl-worker` in the Prefect UI, followed by a single
      `memory-indexing-etl` run.

## User Stories

### Story: Operator triggers a 2-way sharded memory extraction
1. Operator has the stack up and `make memory-serve-workflows` running.
2. Operator runs `make memory-run-memory-pipeline-extraction USER_ID=507f1f77bcf86cd799439011 NUM_SHARDS=2`.
3. The script triggers the `memory-extract-etl-orchestrator` deployment.
4. The orchestrator resolves the user's pending documents and partitions them into 2
   balanced shards.
5. In the Prefect UI the operator sees ONE parent run named
   `memory-extract-etl-orchestrator` and TWO child runs named
   `memory-extract-etl-worker`.
6. After both workers finish, exactly ONE `memory-indexing-etl` run fires.

### Story: Operator runs a plain (default) extraction
1. Operator runs `make memory-run-memory-pipeline-extraction USER_ID=<oid>` (no
   NUM_SHARDS).
2. The orchestrator runs with `num_shards=1`, resolves pending docs into a single
   shard, dispatches ONE `memory-extract-etl-worker` run, then ONE
   `memory-indexing-etl` run.
3. Operator observes (per the accepted semantics change) a worker child + an index
   run — not an in-process plain run.

### Story: One shard fails mid-run
1. Operator triggers the orchestrator with `NUM_SHARDS=4` for a user with 8 pending
   docs.
2. One worker run raises a transient error.
3. The orchestrator isolates that failure (the other 3 workers complete), records it
   in `FanOutStats.failures`, and STILL fires the single `memory-indexing-etl` run so
   the successfully-extracted shards get indexed.

### Story: A maintainer triggers a bare extraction with no indexing
1. Maintainer wants extraction WITHOUT the trailing index (e.g. debugging).
2. They trigger `memory-extract-etl-worker` directly with a `user_id` (+ optional
   `document_ids`).
3. The worker runs the six-task pipeline and returns a `WriteSummary` with NO
   self-dispatch and NO indexing run.

### Story: Operator cleans up the orphaned old deployment
1. After deploying #067 and re-serving workflows, the operator sees the old
   `memory-extraction-etl` deployment still listed in Prefect.
2. Following the documented ops note, they run
   `prefect deployment delete memory-extraction-etl/memory-extraction-etl`.
3. The stale deployment is removed; only `memory-extract-etl-orchestrator`,
   `memory-extract-etl-worker`, and `memory-indexing-etl` remain for the memory
   pipeline.

---

Blocked by: #066

## Log

### [PM] 2026-05-23 — Grooming

**Summary**
Splits the single `memory-extraction-etl` deployment into two named deployments —
`memory-extract-etl-worker` (the six-task extraction body, no fan-out machinery) and
`memory-extract-etl-orchestrator` (resolve→partition→dispatch N workers→one trailing
index). Replaces #061's recursive self-dispatch: the orchestrator dispatches a
DISTINCT worker deployment, so there is no recursion and the per-child `num_shards=1`
key disappears. Reworks the memory fan-out unit + integration tests, re-points the
Make target + script at the orchestrator, and documents the stale-deployment cleanup.
`memory-indexing-etl` untouched.

**Key decisions**
- Worker body is the EXISTING worker-path extraction moved verbatim — preserves the
  config-revalidation-at-entry, first-person/supersession/canonicalization steps, and
  the `_CachedSingleEmbedding` reuse. The owner accepted that `num_shards=1` now means
  "1 worker + 1 index", not a byte-identical in-process run.
- `_fan_out_extraction` keeps its gather/failure-isolation/single-index shape but
  dispatches the WORKER deployment and drops the `num_shards` child key (the worker has
  no such param — passing it would be a Prefect parameter error). This is the concrete
  recursion-removal.
- Reuse `_resolve_num_shards`, `_resolve_pending_document_ids`, `_partition_into_shards`,
  `FanOutStats` from #066/sharding — no new partitioning code.
- Leave `data-pipeline-etl` registered (data split is #068) so #067 ships independently
  and leaves the codebase working. The serve-registration test that asserts the full
  name set is reworked in #069 (after both splits land) to avoid a mid-feature churn of
  that one test; #067's unit tests cover the memory flows + fan-out directly.
- Stale-deployment deletion is an ops action (Prefect server state, not code); the AC
  verifies the documented note, the live deletion is part of #069's `[HUMAN]` pass.

**Dependencies**
- #066 — needs the settled ADR-002 §3 amendment (topology of record) and the
  reusable shard helper.

**User stories**
- 5 stories covering: 2-way sharded run + UI distinct-name check, default run
  (semantics change), shard-failure isolation + still-index, bare worker-only
  extraction, and orphaned-deployment cleanup.

Ready for implementation.

### [SWE] 2026-05-23 16:30 — Implementation

**Files modified**
- `apps/memory/src/tree/memory/extraction/pipeline.py` — split the single
  `memory_extraction` flow into TWO flows: `memory_extract_etl_worker`
  (`@flow(name="memory-extract-etl-worker")`, `(user_id, document_ids=None) ->
  WriteSummary`, the pure six-task body, NO `num_shards`/orchestrator branch/`run_deployment`/index)
  and `memory_extract_etl_orchestrator` (`@flow(name="memory-extract-etl-orchestrator")`,
  `(user_id, document_ids=None, num_shards=1) -> FanOutStats`, delegates to
  `_orchestrate_sharded_extraction`). Updated the MCP shim docstrings.
- `apps/memory/src/tree/memory/extraction/sharding.py` — `_EXTRACTION_DEPLOYMENT`
  → `_WORKER_DEPLOYMENT = "memory-extract-etl-worker/memory-extract-etl-worker"`;
  `_fan_out_extraction` now dispatches the WORKER (no recursion) and drops the
  `"num_shards": 1` child key (children carry only `{user_id, document_ids}`).
  Module docstring re-pointed at the orchestrator/worker split.
- `apps/memory/src/tree/orchestrator.py` — REMOVED `memory-extraction-etl`
  registration; ADDED `memory-extract-etl-orchestrator` (tags
  `[memory-pipeline, extraction, orchestrator]`) + `memory-extract-etl-worker`
  (tags `[memory-pipeline, extraction, worker]`). Kept `serve(..., limit=limit)`
  (#065), the dream cron, `memory-indexing-etl`, and all data/file/youtube/conversation
  registrations.
- `apps/memory/scripts/run_memory_pipeline.py` — `DEPLOYMENT_NAME` →
  `memory-extract-etl-orchestrator/memory-extract-etl-orchestrator`; docstring +
  `--num-shards` help re-described for the split; `--num-shards >= 1` guard and
  `--doc-ids` parsing unchanged; `init_logger()` still at module level.
- `apps/memory/Makefile` — `run-memory-pipeline-extraction` help text re-points at the
  orchestrator; `USER_ID` guard + `NUM_SHARDS`/`DOC_IDS` threading unchanged.
- `apps/memory/scripts/migrate_multi_tenancy.py` — `_EXTRACTION_DEPLOYMENT` re-pointed
  at the orchestrator (was a live dispatch target that would otherwise warn-and-skip);
  docstring deployment name updated.
- `apps/memory/scripts/smoke_resolution_dedup.py` — `memory_extraction.fn(...)` →
  `memory_extract_etl_worker.fn(...)` (the smoke exercises the pure worker body).
- `apps/memory/scripts/check_kgquery_discipline.py` — allowlist comment for
  `sharding.py` updated (orchestrator/worker wording); the allowlisted PATHS are
  unchanged (worker + orchestrator bodies stayed in `pipeline.py`).
- `apps/memory/src/tree/config/app_config.py` — `doc_concurrency` comment re-worded.
- `apps/memory/tests/unit/memory/extraction/test_fanout.py` — reworked
  `_fan_out_extraction` tests: each child carries `{user_id, document_ids}` (NO
  `num_shards`), dispatch name contains `worker` (not `extraction`/self); kept
  partition/clamp/single-index/failure-isolation/no-op/ordering coverage; added a
  dedicated "dispatches the worker deployment" test.
- `apps/memory/tests/integration/memory/test_extraction_fanout.py` — re-targeted
  from `memory_extraction(num_shards=…)` to `memory_extract_etl_orchestrator(...)`
  (N worker dispatches over disjoint shards, each `{user_id, document_ids}` with NO
  `num_shards`; exactly ONE trailing `memory-indexing-etl`; pending-doc resolution;
  no-op; one-shard-failure isolation + index still fires; added a `num_shards=1`
  default → 1 worker + 1 index test) and `memory_extract_etl_worker(...)` (ZERO
  `run_deployment` calls, zero-doc `WriteSummary` no-op for a fresh user → mongot-free).
- `apps/memory/tests/unit/test_orchestrator.py` — registration name set updated to the
  worker + orchestrator names; asserts `memory-extraction-etl` is NOT registered.
- `apps/memory/tests/unit/memory/extraction/test_pipeline.py` +
  `test_pipeline_user_id_propagation.py`, and 9 integration files
  (`test_extraction_pipeline`, `test_validator_e2e`, `test_dedup_node_text_embedding`,
  `test_preference_supersession`, `test_fact_island`, `test_pole_o_extraction_e2e`,
  `test_two_user_isolation`, `test_e2e_embedding_split_and_batching`,
  `test_embedding_batching`) + `tests/integration/conftest.py` — mechanical rename of
  the worker-path callers `memory_extraction` → `memory_extract_etl_worker` (same body);
  flow-name assertion updated to `memory-extract-etl-worker` + added orchestrator-name
  assertion.

**Worker / orchestrator flow names + deployment registrations**
- Worker flow `memory_extract_etl_worker`, `@flow(name="memory-extract-etl-worker")`
  → deployment `memory-extract-etl-worker`.
- Orchestrator flow `memory_extract_etl_orchestrator`,
  `@flow(name="memory-extract-etl-orchestrator")` → deployment
  `memory-extract-etl-orchestrator`.
- `memory-extraction-etl` is no longer registered (removed from `serve_deployments`).

**Dispatch-targets-worker confirmation**
- `_fan_out_extraction` dispatches `_WORKER_DEPLOYMENT =
  "memory-extract-etl-worker/memory-extract-etl-worker"` once per shard under
  `asyncio.gather(return_exceptions=True)`; each child carries ONLY
  `{user_id, document_ids}` (NO `num_shards` key). No recursion: the orchestrator
  dispatches a DISTINCT worker deployment. `num_shards=1` → 1 worker + 1 index;
  empty doc set → no-op (zero dispatch, zero index, `FanOutStats(shards_total=0)`).

**Tests**
- Unit: 1432 passing, 0 failing, 0 warnings — `make memory-unit-tests`.
- Integration (fan-out, isolated): 10 passing — `pytest tests/integration/memory/test_extraction_fanout.py`.
- Integration (fast loop, isolated): 152 passing, 1 skipped, 116 deselected; the one
  reported failure was the unrelated live `TestLiveSerpSearch` Bright Data network test
  (zero references to extraction/orchestrator/sharding; SKIPS on isolated re-run) — NOT
  a regression from this task.

**Evidence**
```
$ make memory-unit-tests
============================ 1432 passed in 42.43s =============================

$ uv run pytest tests/integration/memory/test_extraction_fanout.py -q
10 passed in 9.13s

$ python -c "import tree.orchestrator"  (+ serve_deployments spy)
registered: ['data-pipeline-etl', 'dream-consolidation-etl', 'ingest-conversation-etl',
 'ingest-file-etl', 'ingest-youtube-rss-feed-batch-etl', 'ingest-youtube-video-batch-etl',
 'memory-extract-etl-orchestrator', 'memory-extract-etl-worker', 'memory-indexing-etl']
limit kwarg: 4
OK: import clean + registrations correct  (memory-extraction-etl NOT present)

$ grep -rn "memory-extraction-etl" apps/memory/src apps/memory/scripts apps/memory/Makefile
(none)

$ uv run python scripts/run_memory_pipeline.py --user-id <oid> --num-shards 0 ; echo $?
--num-shards must be >= 1 (got 0)
1

$ make memory-run-memory-pipeline-extraction   # no USER_ID
USAGE: make run-memory-pipeline-extraction USER_ID=<24-char Mongo ObjectId> [DOC_IDS="id1,id2"] [NUM_SHARDS=<n>]
(exit 1)
```

**Ops note — stale `memory-extraction-etl` deployment (#069 owns the live step)**
After this lands and `make memory-serve-workflows` is re-run, the server-side
`memory-extraction-etl` deployment becomes ORPHANED (the code no longer registers
it). The operator must delete it on each environment:
```
prefect deployment delete memory-extraction-etl/memory-extraction-etl
# and, if it ever lingered from #056:
prefect deployment delete memory-extraction-fanout-etl/memory-extraction-fanout-etl
```
This is an ops action (Prefect server state, not code). Per ADR-002 §3 (#066 ops
note) the actual live deletion is #069's `[HUMAN]` step — NOT run here.

**Notes**
- Renaming the `memory_extraction` flow function forced a mechanical rename across 11
  worker-path test files + `conftest.py` (they import/call the worker body); same body,
  no behavior change.
- `migrate_multi_tenancy.py` triggers extraction then indexing separately; the
  orchestrator now also fires its own trailing index, so a migrate run will index twice.
  Indexing is an idempotent backfill over unembedded nodes, so the second run is a
  harmless no-op. Flagging as a follow-up candidate (drop migrate's standalone index
  trigger now that the orchestrator owns it) — not in this task's scope.
- `make memory-integration-tests-all` (full + mongot, in isolation) is the Tester's
  acceptance gate per CLAUDE.md — NOT RUN by SWE to avoid contending the shared docker
  stack while a runner from this worktree serves the OLD names. Did NOT start
  serve-workflows (the live restart + distinct-name UI e2e is #069 / the `[HUMAN]` AC).
- DO NOT COMMIT — handing to the Tester first.

### [Tester] 2026-05-23 18:30 — QA

**Test summary**
- Format / lint / pre-commit: PASS (`267 files already formatted`; `All checks passed!`;
  pre-commit all hooks Passed incl. `KGQuery discipline (memory)`).
- Unit tests: 1432 passed / 0 failed, 0 warnings (`make memory-unit-tests`).
- Integration tests (`-all`, slow + requires_mongot, quiesced+isolated): 269 passed /
  1 skipped / 0 failed, 0 warnings, 598s, exit 0 (`make memory-integration-tests-all`).
- Warnings: 0.

**E2E adversarial pass** (script/Make surface + topology probes; the live distinct-name
UI check is the #069 `[HUMAN]` AC, NOT RUN here per the task brief — a runner is serving
the OLD names and #069 owns the restart)
- Happy path (registration probe): `import tree.orchestrator` + spy on `serve()` →
  registered `{data-pipeline-etl, dream-consolidation-etl, ingest-*, memory-extract-etl-
  orchestrator, memory-extract-etl-worker, memory-indexing-etl}`; `limit=4` threaded;
  dream cron `0 4 * * *` preserved; `memory-extraction-etl` ABSENT. PASS.
- Break path 1 (boundary: `--num-shards 0` and `-2`): script exits 1 with
  `--num-shards must be >= 1 (got N)` (verified without pipe-masking). PASS.
- Break path 2 (missing required input: no `USER_ID`): `make memory-run-memory-pipeline-
  extraction` → USAGE line, exit 2; script `--user-id` missing → exit 1
  `--user-id is required (... No silent fallback ...)`. PASS.
- Break path 3 (malformed input: `--user-id notanobjectid`): exit 1 with a clear
  `not a valid Mongo ObjectId` message — no stack trace leaked. PASS.
- Break path 4 (worker bare-extraction contract, fresh user): integration
  `test_worker_issues_no_run_deployment_or_index` + `..._with_explicit_doc_ids_...` →
  worker issues ZERO `run_deployment` calls, returns `WriteSummary(documents_processed=0)`.
  PASS.
- Break path 5 (shard-failure isolation): integration
  `test_orchestrator_isolates_one_shard_failure_and_still_indexes` → 4 worker dispatches,
  1 fails, `failed=1`/`succeeded=3`, single index run STILL fires last. PASS.

**Topology verification (the core)**
- Worker `memory_extract_etl_worker` `@flow(name="memory-extract-etl-worker")`
  `(user_id, document_ids=None)->WriteSummary`: NO `num_shards` param (signature read,
  `pipeline.py:1591-1595`), NO `run_deployment(` call site anywhere in `pipeline.py`
  (grep: 0 call sites; only the orchestrator helper passes `run_deployment=run_deployment`
  to `_fan_out_extraction`), NO indexing trigger. The six-task body is the verbatim
  pre-split worker path (diff: only flow name/signature/docstring + removal of the
  `if num_shards > 1` branch changed; chunk→llm→validate→first-person→canonicalize→
  supersession→has-edges→resolve→embed→dedup→apply-writes unchanged). PASS.
- Orchestrator `memory_extract_etl_orchestrator` `@flow(name=
  "memory-extract-etl-orchestrator")` `(user_id, document_ids=None, num_shards=1)->
  FanOutStats`: dispatches `_WORKER_DEPLOYMENT="memory-extract-etl-worker/
  memory-extract-etl-worker"` (sharding.py:74) one per shard under
  `gather(return_exceptions=True)`, each child param set EXACTLY
  `{user_id, document_ids}` with NO `num_shards` key (unit `test_fan_out_children_carry
  _no_num_shards_key` + integration assert `set(p)=={"user_id","document_ids"}`); exactly
  ONE trailing `memory-indexing-etl` AFTER the gather, scoped `{user_id}`, fired even on
  shard failure; empty doc set → `FanOutStats(shards_total=0)` no-op. NO recursion
  (distinct worker deployment). PASS.

**Registration / grep evidence**
```
$ uv run python -c "<spy serve()>"
REGISTERED: ['data-pipeline-etl','dream-consolidation-etl','ingest-conversation-etl',
 'ingest-file-etl','ingest-youtube-rss-feed-batch-etl','ingest-youtube-video-batch-etl',
 'memory-extract-etl-orchestrator','memory-extract-etl-worker','memory-indexing-etl']
limit kwarg: 4 ; dream cron: 0 4 * * * ; ALL ASSERTIONS PASS

$ grep -rn "memory-extraction-etl" apps/memory/src apps/memory/scripts apps/memory/Makefile
(none)
$ grep -rn "\bmemory_extraction\b" apps/memory --include="*.py"
(none — removed symbol has zero remaining importers; no ImportError risk)
```

**Acceptance criteria** — all non-`[HUMAN]` verified PASS:
- [x] Worker flow exists, correct signature, no num_shards/orchestrator/run_deployment/index — grep + signature read.
- [x] Orchestrator flow exists, resolves/partitions/dispatches worker + one trailing index — integration `test_orchestrator_fans_out_per_shard_then_indexes_once`.
- [x] Each worker dispatch carries `{user_id, document_ids}` NO num_shards — unit + integration asserts.
- [x] Trailing index runs exactly ONCE after gather, `{user_id}`-scoped, even on shard failure — integration tests.
- [x] Empty doc set → clean no-op `FanOutStats(shards_total=0)` — `test_orchestrator_no_pending_docs_is_noop`.
- [x] Shard failure isolated + recorded + index still fires — `test_orchestrator_isolates_one_shard_failure_and_still_indexes`.
- [x] orchestrator.py: no `memory-extraction-etl`; registers worker+orchestrator; keeps `serve(limit=limit)`, dream cron, indexing, all data/file/youtube/conversation — probe + diff.
- [x] `python -c "import tree.orchestrator"` succeeds — clean import.
- [x] `run_memory_pipeline.py` DEPLOYMENT_NAME targets orchestrator; `--num-shards>=1` guard exits 1; `init_logger()` at module level — diff + guard run.
- [x] Make target help + behavior reference orchestrator; USER_ID guard intact; NUM_SHARDS/DOC_IDS threaded — diff + guard run.
- [x] Stale-deployment `prefect deployment delete` cleanup documented in task log/verification.
- [x] format/lint/pre-commit clean.
- [x] unit tests pass, 0 warnings.
- [x] `make memory-integration-tests-all` PASS (269 passed/1 skipped/0 warnings, isolated).
- [ ] [HUMAN] Live distinct-name UI e2e — NOT RUN (deferred to #069 per task brief; a
      runner currently serves the OLD names — #069 owns the serve-workflows restart +
      live pass). Awaiting human verification.

**Adversarial / other findings**
- `migrate_multi_tenancy.py` double-index note CONFIRMED BENIGN: migrate triggers
  `_EXTRACTION_DEPLOYMENT` (now the orchestrator, which fires its own trailing index)
  AND `_INDEXING_DEPLOYMENT` in a loop (`migrate_multi_tenancy.py:355`), so a migrate run
  indexes twice. Indexing is an idempotent backfill over unembedded nodes → the second
  run is a near-no-op. Pre-existing one-off ops-script artifact of re-pointing migrate at
  the orchestrator, NOT a #067 orchestrator/worker regression; SWE flagged it as a
  follow-up candidate. Not a blocker.
- NIT (doc drift, NOT a blocker, outside any #067 AC and outside the AC's grep scope of
  src/scripts/Makefile): `apps/memory/README.md:108` still lists the old
  `memory-extraction-etl` deployment name (and omits the youtube deployments). The
  AC-scoped grep (src/scripts/Makefile) is clean. Suggest folding a README deployment-list
  refresh into #069 (which already owns the post-split serve/registration acceptance).

**VERDICT: PASS**
</content>
