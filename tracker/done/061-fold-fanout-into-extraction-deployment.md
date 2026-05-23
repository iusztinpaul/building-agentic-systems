# Fold the document-shard fan-out into the single memory-extraction-etl deployment

Status: pending
Tags: `infra`, `memory`, `prefect`, `rework`
Depends on: #054, #055, #056
Blocks: —
Refs: ADR-002 §3 (amended #061); supersedes #056's separate-entrypoint design

## Scope

#056 shipped the document-shard fan-out as a SECOND Prefect deployment
(`memory-extraction-fanout-etl`, backed by a standalone `memory_extraction_sharded`
parent flow). On review of open PR #24 — before merge — the project owner rejected
the two-entrypoint shape: operators should not have to choose between two extraction
commands. This task folds the fan-out INTO the existing `memory-extraction-etl`
deployment via a `num_shards` parameter using recursive self-dispatch, then DELETES
the separate flow / deployment / script / Make target. One extraction entrypoint,
one knob. Implements the amended ADR-002 §3.

The fan-out semantics from #056 — document-shard axis, balanced contiguous
partitioning, `asyncio.gather(return_exceptions=True)` failure-isolation, exactly ONE
trailing index — are PRESERVED. Only the deployment topology changes.

### Mechanism — recursive self-dispatch on one deployment

- **`memory_extraction` signature gains `num_shards: int = 1`.** The flow
  (`apps/memory/src/tree/memory/extraction/pipeline.py`, `@flow(name="memory-extraction-etl")`)
  becomes the single deployment for both paths:

  - **`num_shards <= 1` (default) — WORKER path.** Runs TODAY'S extraction logic
    directly and unchanged (fetch docs scoped to `user_id`, the six-task pipeline,
    return `WriteSummary`). It does NOT resolve pending docs as a fan-out set, does
    NOT partition, does NOT call `run_deployment` (no self-dispatch), and does NOT
    trigger indexing. `num_shards=1` (and the default, and `None`-via-direct-trigger,
    and any clamped non-positive) is byte-for-byte equivalent to `memory_extraction`
    before this feature. The existing `document_ids` semantics (explicit subset vs.
    `content != None` fetch) are unchanged on this path.

  - **`num_shards > 1` — ORCHESTRATOR path.** The flow:
    1. Resolves the user's pending documents when `document_ids is None` (a
       `Document` is pending iff its `_id` is absent from every
       `knowledge_graph.sources` array for that `user_id`; only `content != None`
       docs are eligible; deterministic sorted order). An explicit `document_ids`
       list is used verbatim.
    2. Empty resolved/explicit set → clean no-op: NO self-dispatch, NO indexing run,
       returns a zero `FanOutStats`-shaped report.
    3. Partitions the ids into `min(num_shards, N)` contiguous, disjoint, balanced
       shards (sizes differ by ≤1, larger shards lead; e.g. `(6,4)→[2,2,1,1]`,
       `(7,3)→[3,2,2]`).
    4. Fans out one child per shard:
       `run_deployment("memory-extraction-etl/memory-extraction-etl",
       parameters={"user_id": str(user_id), "document_ids": shard, "num_shards": 1})`
       under `asyncio.gather(*[...], return_exceptions=True)`. Each child gets
       `num_shards=1`, so children take the WORKER path — recursion terminates after
       exactly one level. One child raising is caught, recorded in `failures`, and
       does NOT abort the others.
    5. After the gather settles, fires exactly ONE trailing
       `run_deployment("memory-indexing-etl/memory-indexing-etl",
       parameters={"user_id": str(user_id)})` — never per-shard, and regardless of how
       many shards failed (a partial extraction is still indexed).
    6. Returns the `FanOutStats`-shaped report (`shards_total` / `succeeded` /
       `failed` / `failures`).

  Note the orchestrator path returns a `FanOutStats`-shaped report while the worker
  path returns a `WriteSummary`. The SWE picks the return-type representation (e.g. a
  union return type, or `FanOutStats` carrying the fields callers read); the live
  `make` script only logs the run state, so either is acceptable as long as the
  worker path's `WriteSummary` is unchanged and the orchestrator path reports
  shards_total/succeeded/failed.

### Helpers — move, don't reinvent

Keep the reusable PURE helpers from #056 and have `memory_extraction` consume them.
They may live in `pipeline.py` directly OR in a small sharding-helper module imported
by `pipeline.py` — the SWE decides, but `extraction/fanout.py` as a separate
*flow/deployment* module goes away. Preserve verbatim behavior:

- `_partition_into_shards(document_ids, num_shards)` — balanced contiguous split into
  exactly `min(num_shards, N)` shards; `N==0 → []`; union-in-order == input.
- `_resolve_num_shards(num_shards, default=...)` — `None` → default; non-positive
  (0/negative) clamps to `1`. (On the new design the default for the clamp is moot
  for `None` because the signature default is `1`, but the clamp on a hand-crafted
  negative direct-trigger value MUST still resolve to ≥1 so a negative never reaches
  `_partition_into_shards`.)
- Pending-doc resolution (`_resolve_pending_document_ids`-equivalent) — tenant-scoped
  `knowledge_graph.sources` membership test, `content != None`, deterministic order.
- The `FanOutStats`-shaped report (`shards_total`/`succeeded`/`failed`/`failures`).

The `default` fed to `_resolve_num_shards` on the orchestrator path stays
`app_config.concurrency.fanout_max_parallel` (so an operator can still pass a large
`num_shards` and have it cap sensibly if you choose to clamp to the configured max —
keep #056's behavior: it only clamps the lower bound to 1; the upper bound is bounded
by `min(num_shards, N)` in partitioning).

### Deletions (no second entrypoint anywhere)

- **DELETE** the standalone parent flow `memory_extraction_sharded` and the module
  `apps/memory/src/tree/memory/extraction/fanout.py` AS A FLOW MODULE. If the pure
  helpers are kept in a helper module, that module must NOT define a Prefect `@flow`
  or any deployment.
- **DELETE** the `memory-extraction-fanout-etl` deployment registration from
  `apps/memory/src/tree/orchestrator.py` (the
  `memory_extraction_sharded.to_deployment(...)` block and its import). The
  `memory-extraction-etl` `.to_deployment(...)` and `serve(global_limit=...)` stay
  exactly as they are.
- **DELETE** `apps/memory/scripts/run_extraction_fanout.py`.
- **DELETE** the `run-memory-pipeline-extraction-fanout` Make target in
  `apps/memory/Makefile`.

### One entrypoint, one knob

- **Extend the EXISTING `run-memory-pipeline-extraction` Make target** with an
  optional `NUM_SHARDS=<n>` arg:
  `run-memory-pipeline-extraction USER_ID=<oid> [DOC_IDS="id1,id2"] [NUM_SHARDS=<n>]`.
- **Extend `apps/memory/scripts/run_memory_pipeline.py`** with an optional
  `--num-shards` Click option (int). When provided, pass `num_shards` through in the
  deployment `parameters`. Reuse the existing `--num-shards >= 1` guard that
  `run_extraction_fanout.py` had (reject `< 1` with a logged error + exit 1). Keep
  `init_logger()` at module level and the existing log-streaming/poll loop. The
  script still targets `memory-extraction-etl/memory-extraction-etl` (it already
  does) — no new deployment name.

### KGQuery discipline

The pending-doc resolution does one raw `kg.find` on `knowledge_graph` threading
`user_id`. #056 added `fanout.py` to `scripts/check_kgquery_discipline.py`'s
`_ALLOWLIST`. Update the allowlist entry to whatever module now hosts the resolution
helper (`pipeline.py` is already allow-listed, or the new helper module). The
tenant-scoping behavior and its test carry over from #056.

## Acceptance Criteria

- [x] `memory_extraction` (`pipeline.py`, `@flow(name="memory-extraction-etl")`) has
      signature `(user_id, document_ids=None, num_shards=1)`; `num_shards` is a
      non-Optional int defaulting to `1`.
- [x] WORKER path (`num_shards=1`, and the default with `num_shards` omitted) is
      IDENTICAL to today's extraction: it issues NO `run_deployment` self-dispatch and
      NO `memory-indexing-etl` call, and returns the same `WriteSummary` as the prior
      `memory_extraction` for the same inputs. Verified by a unit/integration test that
      spies on `run_deployment` (patched into the flow module) and asserts ZERO calls
      on the `num_shards=1` and default paths.
- [x] ORCHESTRATOR path (`num_shards > 1`) issues exactly `min(num_shards, N)`
      `memory-extraction-etl` self-dispatch runs (one per shard), each with
      `parameters={"user_id": str(user_id), "document_ids": <shard>, "num_shards": 1}`,
      followed by exactly ONE `memory-indexing-etl` run with
      `parameters={"user_id": str(user_id)}`. A test asserts the per-shard count, the
      child params (including `num_shards == 1` on every child), the single index call,
      and that the index call is LAST (after the gather).
- [x] Children are dispatched with `num_shards == 1` (no recursion beyond one level) —
      asserted explicitly in the orchestrator-path test.
- [x] Partition/clamp/empty-docs/failure-isolation behaviors carry over from #056:
      - `_partition_into_shards` yields `min(num_shards, N)` contiguous disjoint
        balanced shards whose in-order union equals the input; `(6,4)→[2,2,1,1]`,
        `(7,3)→[3,2,2]`, `N=0→[]`, `N<num_shards` collapses to `N` shards.
      - `_resolve_num_shards`: `None`→default, non-positive (0/−1/−3)→`1`, positive
        unchanged.
      - `num_shards > 1` with an empty resolved/explicit doc set → no-op: zero
        self-dispatch, zero indexing run, zero `FanOutStats`.
      - One shard's child raising is isolated (`return_exceptions=True`), recorded in
        `failures`, the other shards still run, and the single trailing index STILL
        fires.
      (These are the #056 unit + integration tests, reworked to target
      `memory_extraction(num_shards=…)` rather than `memory_extraction_sharded`.)
- [x] Behavior-preservation of the worker path: for a fixed document set, the
      `apply_writes: nodes_written=… edges_written=…` and `dedupe_entities:
      n_merged=… n_flagged=… n_none=…` log lines / `WriteSummary` counts on the
      `num_shards=1` path are unchanged vs. the prior `memory_extraction` (the
      pre-#061 behavior). Verified via the existing extraction integration tests
      passing unchanged.
- [x] `apps/memory/src/tree/memory/extraction/fanout.py` no longer defines a Prefect
      `@flow` named `memory-extraction-fanout-etl` (the standalone parent flow is
      gone). `grep -rn "memory_extraction_sharded\|memory-extraction-fanout-etl"
      apps/memory/src apps/memory/scripts apps/memory/Makefile` returns NOTHING (the
      symbol and deployment name are fully removed from source/scripts/Makefile; test
      references are reworked too).
- [x] `orchestrator.py` registers exactly ONE extraction deployment
      (`memory-extraction-etl`) and keeps
      `serve(global_limit=app_config.concurrency.runner_global_limit)`. The
      `memory_extraction_sharded` import and its `.to_deployment(...)` block are
      removed. `uv --directory apps/memory run python -c "import tree.orchestrator"`
      succeeds.
- [x] `apps/memory/scripts/run_extraction_fanout.py` is deleted; the
      `run-memory-pipeline-extraction-fanout` Make target is deleted.
- [x] `make memory-run-memory-pipeline-extraction USER_ID=<oid> NUM_SHARDS=4` (and
      with `--num-shards` via the script directly) passes `num_shards=4` through to the
      `memory-extraction-etl` deployment; `make memory-run-memory-pipeline-extraction
      USER_ID=<oid>` (no `NUM_SHARDS`) triggers the worker path with `num_shards`
      defaulting to 1. The USER_ID guard, the ObjectId guard, and a `NUM_SHARDS < 1`
      guard (exit 1) are exercised. (Guard + `--help` + import verified here; the live
      trigger is the [HUMAN] AC below.)
- [x] `make memory-format-fix && make memory-lint-fix && make memory-format-check &&
      make memory-lint-check && make pre-commit` clean (incl. the "KGQuery discipline"
      hook — the allowlist entry points at the module that now hosts the resolution
      helper).
- [x] `make memory-unit-tests` passes; `make memory-integration-tests` (fast loop)
      passes. The reworked `tests/unit/memory/extraction/test_fanout.py` (or its
      successor location) and `tests/integration/memory/test_extraction_fanout.py`
      target `memory_extraction(num_shards=…)`; no test references
      `memory_extraction_sharded`.
- [x] `make memory-integration-tests-all` passes with the mongot stack up (Tester
      acceptance gate), 0 warnings.
- [ ] [HUMAN] Live worker-path parity (stack + `serve-workflows` + real Voyage key):
      `make memory-run-memory-pipeline-extraction USER_ID=<oid>` (no NUM_SHARDS) runs a
      single `memory-extraction-etl` run with NO child runs and NO trailing
      `memory-indexing-etl` run — identical to extraction before this feature.
- [ ] [HUMAN] Live 4-way fan-out: `make memory-run-memory-pipeline-extraction
      USER_ID=<oid> NUM_SHARDS=4` — the Prefect UI shows ONE parent
      `memory-extraction-etl` run spawning 4 CHILD `memory-extraction-etl` runs (each
      with `num_shards=1`) over DISJOINT shards; ≤4 execute at once (`global_limit`);
      across all children Voyage embeds serialize to ~3/min with NO "rate-limit retries
      exhausted" / 429 warnings; a SINGLE `memory-indexing-etl` run fires AFTER the
      shards complete.
- [ ] [HUMAN] Negative check: temporarily delete/raise the `voyage-embeddings` limit
      and confirm 429 warnings reappear under the 4-way fan-out — proving the limiter
      still holds the line through the in-flow self-dispatch path.
- [ ] [HUMAN] No second extraction entrypoint: `make memory-help` (or the Prefect UI
      deployment list after `serve-workflows`) shows exactly one extraction trigger —
      `run-memory-pipeline-extraction` — and no `…-fanout` target/deployment.

## User Stories

### Story: Operator runs a plain (non-sharded) extraction — unchanged from before
1. Operator runs `make local-start`, `make memory-serve-workflows &`.
2. Operator runs `make memory-run-memory-pipeline-extraction USER_ID=<oid>` (no
   `NUM_SHARDS`).
3. In the Prefect UI they see exactly ONE `memory-extraction-etl` run, no child runs.
4. No `memory-indexing-etl` run is triggered by this command (just like before #061).
5. The run logs the same `apply_writes`/`dedupe_entities` counts it always did.

### Story: Operator runs a 4-way sharded extraction via the SAME command
1. Operator runs `make local-start`, `make memory-sync-concurrency-limits`,
   `make memory-serve-workflows &`.
2. Operator runs `make memory-run-memory-pipeline-extraction USER_ID=<oid> NUM_SHARDS=4`.
3. In the Prefect UI they see ONE parent `memory-extraction-etl` run that spawns 4
   CHILD `memory-extraction-etl` runs over disjoint document-shards, each child with
   `num_shards=1`.
4. At most 4 runs execute at once; Voyage embeds across all children pace to ~3/min
   with no 429 warnings.
5. After all 4 shards finish, exactly one `memory-indexing-etl` run starts.
6. The parent run reports `shards_total=4, succeeded=4, failed=0`.

### Story: Operator passes an explicit document subset with shards
1. Operator runs `make memory-run-memory-pipeline-extraction USER_ID=<oid>
   DOC_IDS="a,b,c,d,e,f" NUM_SHARDS=4`.
2. The 6 ids are partitioned into 4 disjoint contiguous shards (sizes 2,2,1,1).
3. Four child `memory-extraction-etl` runs (each `num_shards=1`) process the shards;
   their union is exactly the 6 ids; one trailing `memory-indexing-etl` run follows.

### Story: One shard fails but the batch survives
1. Operator triggers a 4-shard fan-out where one shard hits a transient error.
2. The other 3 child runs complete; the failing shard is recorded in the parent
   report's `failures` map (`succeeded=3 failed=1`).
3. The single `memory-indexing-etl` run still fires after the gather.

### Story: Operator triggers a sharded run with no pending documents
1. Operator runs `make memory-run-memory-pipeline-extraction USER_ID=<oid>
   NUM_SHARDS=4` for a user whose documents are all already ingested.
2. The flow resolves zero pending docs and returns a zero report — no child runs, no
   indexing run.
3. Logs say there was nothing to do.

### Story: There is only one extraction command to learn
1. A new operator reads `make memory-help`.
2. They find exactly one extraction trigger, `run-memory-pipeline-extraction`, with an
   optional `NUM_SHARDS` knob — not a confusing pair of `extraction` /
   `extraction-fanout` commands.

---

Blocked by: #054, #055, #056

## Log

### [SWE] 2026-05-22 10:55 — Implementation

**Files modified**
- `apps/memory/src/tree/memory/extraction/sharding.py` — NEW. Holds the moved
  pure helpers (`FanOutStats`, `_resolve_num_shards`, `_partition_into_shards`,
  `_resolve_pending_document_ids`, `_fan_out_extraction`). NO Prefect `@flow`, NO
  deployment. `_fan_out_extraction` now dispatches each child with
  `num_shards=1`.
- `apps/memory/src/tree/memory/extraction/fanout.py` — DELETED (the standalone
  `memory_extraction_sharded` flow + `memory-extraction-fanout-etl` deployment).
- `apps/memory/src/tree/memory/extraction/pipeline.py` — `memory_extraction` gains
  `num_shards: int = 1`, return type `WriteSummary | FanOutStats`. Branches at the
  top: `num_shards > 1` → new `_orchestrate_sharded_extraction` helper (resolve
  pending → partition → `_fan_out_extraction` → single trailing index);
  `num_shards <= 1` → today's worker logic UNCHANGED (no self-dispatch, no index).
  Imports `run_deployment` + the sharding helpers.
- `apps/memory/src/tree/orchestrator.py` — removed the
  `memory_extraction_sharded` import and the `memory-extraction-fanout-etl`
  `.to_deployment(...)` block; kept the single `memory-extraction-etl` deployment
  and `serve(global_limit=…)`.
- `apps/memory/scripts/run_extraction_fanout.py` — DELETED.
- `apps/memory/scripts/run_memory_pipeline.py` — added optional `--num-shards`
  Click option (int, `< 1` guard → exit 1), threaded into the
  `memory-extraction-etl` trigger `parameters`.
- `apps/memory/Makefile` — extended `run-memory-pipeline-extraction` with optional
  `NUM_SHARDS=<n>`; deleted `run-memory-pipeline-extraction-fanout`.
- `apps/memory/scripts/check_kgquery_discipline.py` — re-pointed the allowlist
  entry from `extraction/fanout.py` to `extraction/sharding.py`.
- `apps/memory/tests/unit/memory/extraction/test_fanout.py` — re-targeted at
  `tree.memory.extraction.sharding`; added
  `test_fan_out_children_carry_num_shards_one`.
- `apps/memory/tests/integration/memory/test_extraction_fanout.py` — re-targeted at
  `memory_extraction(num_shards=…)` (orchestrator path) + added two worker-path
  tests asserting ZERO `run_deployment` self-dispatch on `num_shards=1`/default.

**Worker vs orchestrator branch**
- `num_shards <= 1` (default): runs the unchanged six-task extraction, returns
  `WriteSummary`, issues NO `run_deployment` and NO indexing trigger.
- `num_shards > 1`: resolves pending docs (when `document_ids is None`),
  partitions into `min(num_shards, N)` balanced shards, self-dispatches one
  `memory-extraction-etl` per shard with `{user_id, document_ids: shard,
  num_shards: 1}` under `asyncio.gather(return_exceptions=True)`, then fires
  exactly ONE trailing `memory-indexing-etl` run. Children carry `num_shards=1` →
  worker path → recursion ends at one level. Returns `FanOutStats`.

**Old symbols gone (grep)**
- `grep -rn "memory_extraction_sharded\|memory-extraction-fanout-etl" apps/memory/src
  apps/memory/scripts apps/memory/Makefile apps/memory/tests` → exit 1 (NOTHING).
- `grep -rn "run_extraction_fanout\|run-memory-pipeline-extraction-fanout"` in
  src/scripts/Makefile → exit 1 (NOTHING).

**Tests**
- Unit: 1413 passing, 0 failing — `make memory-unit-tests` (43.10s). The reworked
  `test_fanout.py` = 37 passing.
- Integration (fast loop): 153 passing, 1 skipped, 115 deselected (slow) — `make
  memory-integration-tests` (186s). No regressions.
- Integration (full, mongot up): 268 passing, 1 skipped, 0 warnings — `make
  memory-integration-tests-all` (650.78s). Includes the 9 reworked fanout
  integration tests, `test_extraction_pipeline.py` (worker-path behavior
  preservation), `test_two_user_isolation.py` (tenant-scoping).

**Acceptance criteria**
- [x] Signature `(user_id, document_ids=None, num_shards=1)` — `pipeline.py`.
- [x] Worker path zero self-dispatch + zero index + same `WriteSummary` — verified by
      `tests/integration/.../test_extraction_fanout.py::test_worker_path_default_issues_no_self_dispatch_or_index`
      and `::test_worker_path_num_shards_one_issues_no_self_dispatch_or_index`.
- [x] Orchestrator path `min(num_shards,N)` self-dispatches + child params + single
      index LAST — `::test_orchestrator_fans_out_per_shard_then_indexes_once`.
- [x] Children carry `num_shards == 1` — `unit::test_fan_out_children_carry_num_shards_one`
      + `integration::test_orchestrator_fans_out_per_shard_then_indexes_once`.
- [x] Partition/clamp/empty/failure-isolation carry over — reworked unit +
      integration tests.
- [x] Worker-path behavior preservation — existing extraction integration tests pass
      unchanged (full suite green).
- [x] `fanout.py` deleted; grep clean across src/scripts/Makefile/tests.
- [x] `orchestrator.py` registers exactly one extraction deployment + keeps
      `serve(global_limit=…)`; `import tree.orchestrator` succeeds.
- [x] `run_extraction_fanout.py` + `run-memory-pipeline-extraction-fanout` deleted.
- [x] Script `--num-shards` passthrough + USER_ID / ObjectId / `NUM_SHARDS < 1`
      guards (all exit 1) + `--help` verified.
- [x] format/lint/pre-commit clean (incl. KGQuery discipline hook).
- [x] unit + fast integration pass.
- [x] `integration-tests-all` passes with mongot, 0 warnings.
- [ ] [HUMAN] Live worker-path parity — NOT RUN — [HUMAN]; shared stack not
      quiesced for serve-workflows.
- [ ] [HUMAN] Live 4-way fan-out — NOT RUN — [HUMAN]; needs serve-workflows.
- [ ] [HUMAN] Negative limiter check — NOT RUN — [HUMAN]; needs serve-workflows.
- [ ] [HUMAN] No second entrypoint — `make memory-help` portion VERIFIED (exactly one
      `run-memory-pipeline-extraction` trigger, no `…-fanout`); Prefect-UI deployment
      list portion NOT RUN — [HUMAN]; needs serve-workflows.

**Evidence**
```
$ make memory-unit-tests
============================ 1413 passed in 43.10s =============================

$ make memory-integration-tests        # fast loop, slow deselected
========== 153 passed, 1 skipped, 115 deselected in 186.00s (0:03:05) ==========

$ make memory-integration-tests-all     # mongot stack up
================== 268 passed, 1 skipped in 650.78s (0:10:50) ==================

$ uv --directory apps/memory run python -c "import tree.orchestrator"
import OK
# orchestrator extraction deployments registered: ['memory-extraction-etl']
# 'memory-extraction-fanout-etl' not present; global_limit retained.

$ grep -rn "memory_extraction_sharded\|memory-extraction-fanout-etl" \
    apps/memory/src apps/memory/scripts apps/memory/Makefile apps/memory/tests
# (no output — exit 1)

$ uv run python scripts/run_memory_pipeline.py --help    # excerpt
  --num-shards INTEGER  Optional document-shard fan-out (#061). Omit or 1 → plain
                        worker extraction ... ``> 1`` → ... self-dispatches one
                        ``memory-extraction-etl`` child run per shard, then indexes
                        once. Must be ``>= 1``.

# guards (each exits 1):
$ run_memory_pipeline.py --user-id <oid> --num-shards 0   → "must be >= 1 (got 0)"  exit 1
$ run_memory_pipeline.py --user-id not-an-oid             → "not a valid ObjectId"  exit 1
$ run_memory_pipeline.py                                  → "--user-id is required"  exit 1
$ run_memory_pipeline.py --user-id <oid> --num-shards -3  → exit 1

$ make memory-help | grep extraction
run-memory-pipeline-extraction: Trigger memory extraction pipeline via Prefect.
  Requires USER_ID=<oid>. Optionally pass DOC_IDS="id1,id2" and/or NUM_SHARDS=<n>
  (>1 fans out document-shards + indexes once; default 1 = plain worker extraction).
# (no run-memory-pipeline-extraction-fanout target)
```

**Notes**
- Did NOT commit (Tester gate first). On `feat/pipeline-parallelism`; branch
  creation skipped per task.
- Worker-path integration tests stub the embedding-model factories
  (`get_resolution_embedding_model` / `get_search_embedding_model` /
  `_build_resolver`) at the boundary so the BRANCH assertion needs no live Voyage
  key and stays fast/mongot-free — verified to pass even with `VOYAGE_API_KEY`
  unset.
- The four `[HUMAN]` live ACs need `serve-workflows` against the shared docker
  stack (contended across worktrees per CLAUDE.md) — NOT started. They are the
  Tester's gate.
- `app_config.concurrency.fanout_max_parallel` (the default fed to
  `_resolve_num_shards` on the orchestrator path) is RETAINED, unchanged.

### [Tester] 2026-05-22 11:20 — QA

**Test summary**
- Format / lint / pre-commit: PASS (incl. KGQuery discipline hook — allowlist now
  points at `extraction/sharding.py`).
- Unit tests: 1413 passed / 0 failed (47.49s).
- Integration tests (full, mongot stack up, quiesced + isolated): 268 passed /
  1 skipped / 0 failed (637.32s). 0 warnings. Known `test_web_serp` flake did NOT
  surface; suite green.

**Critical semantic — worker path == pre-#061 (the #1 risk)**
- Diff comparison: extracted the worker-path body of the new `memory_extraction`
  (`pipeline.py:1594→1755`) and `git show HEAD:…/pipeline.py`'s `memory_extraction`
  body and ran `diff` → **IDENTICAL** (zero output). The only additions to the flow
  are: the `num_shards: int = 1` param, the `WriteSummary | FanOutStats` return type,
  the `if num_shards > 1: return await _orchestrate_sharded_extraction(...)` early
  branch, and a comment header. The worker (`num_shards <= 1`) body is byte-for-byte
  the pre-#061 logic — no self-dispatch, no index trigger.
- Integration: `test_worker_path_default_issues_no_self_dispatch_or_index` +
  `test_worker_path_num_shards_one_issues_no_self_dispatch_or_index` assert ZERO
  `run_deployment` calls on default/`num_shards=1`. `WriteSummary` parity guarded by
  `test_extraction_pipeline.py` (UNCHANGED, 9 tests, real doc writes, default path).

**Orchestrator path (num_shards>1)**
- `test_orchestrator_fans_out_per_shard_then_indexes_once`: 6 ids + num_shards=4 →
  exactly 4 extraction self-dispatches to `memory-extraction-etl/memory-extraction-etl`
  (sizes 2,2,1,1; union==input), every child `num_shards==1` + `user_id=str(user_id)`;
  then EXACTLY ONE `memory-indexing-etl/...` dispatch `{user_id}`, asserted LAST
  (`spy[-1]`). Recursion-termination (`num_shards==1` on every child) asserted
  explicitly here AND in `unit::test_fan_out_children_carry_num_shards_one`.

**No second entrypoint (the whole point)**
- `grep -rn "memory_extraction_sharded|memory-extraction-fanout-etl|run_extraction_fanout|run-memory-pipeline-extraction-fanout" apps/memory/`
  → NOTHING (exit 1), across src + scripts + Makefile + tests.
- `fanout.py` and `run_extraction_fanout.py` deleted (confirmed: `ls` → No such file).
- `uv run python -c "import tree.orchestrator"` → `import OK`. Exactly ONE extraction
  `to_deployment` (`memory_extraction`, line 45; no `_sharded`); `serve(global_limit=
  app_config.concurrency.runner_global_limit)` retained (line 80).
- `make memory-help` → exactly one extraction trigger `run-memory-pipeline-extraction`
  (with `NUM_SHARDS` knob); no `…-fanout` target (grep exit 1).

**Helpers preserved (unit)**
- `(6,4)→[2,2,1,1]`, `(7,3)→[3,2,2]`, `(3,4)→[1,1,1]`, `N=0→[]`; union==input,
  disjoint, contiguous — `test_partition_*`.
- `_resolve_num_shards`: `None`→default, `0/-1/-3`→1, positive unchanged.
- Empty docs → no-op (`test_fan_out_no_shards_is_noop` + `test_orchestrator_no_pending_docs_is_noop`):
  zero dispatch, zero index, `FanOutStats(shards_total=0)`.
- One shard raising → isolated, others counted, single index STILL fires
  (`test_orchestrator_isolates_one_shard_failure_and_still_indexes`: succeeded=3
  failed=1, index last).

**E2E adversarial pass** (direct flow calls, `run_deployment` + DB mocked — never
touched the shared stack)
- Happy path (orchestrator): `memory_extraction(document_ids=6 ids, num_shards=4)` →
  4 extraction dispatches (2,2,1,1) + 1 trailing index, `FanOutStats(4,4,0)` (PASS).
- Break path 1 (boundary: num_shards=0 via DIRECT flow call) → WORKER path: 0
  dispatch, `WriteSummary(documents_processed=0)` returned (PASS — `num_shards > 1`
  is False so 0 cleanly takes the worker branch; never enters orchestrator).
- Break path 2 (boundary: num_shards=-5 via direct call) → WORKER path: 0 dispatch,
  `WriteSummary` (PASS).
- Break path 3 (over-max: num_shards=10 > N=3 explicit ids) → `min(10,3)=3` shards,
  union==input, every child num_shards=1, 1 index, `FanOutStats(3,3,0)` (PASS).
- Break path 4 (explicit subset N=4, num_shards=2) → shards [2,2] == subset ONLY;
  `_resolve_pending_document_ids` NOT called (AssertionError side_effect never fired)
  (PASS — explicit ids used verbatim, no pending resolution).
- Script guards (exit-code captured): `--num-shards 0`→exit 1 ("must be >= 1 (got 0)");
  `--num-shards -3`→exit 1; `--user-id not-an-oid`→exit 1 ("not a valid ObjectId");
  missing user-id→exit 1 ("--user-id is required"). `--help` shows `--num-shards`.

**Acceptance criteria** (all 12 non-[HUMAN] verified; 4 [HUMAN] correctly deferred)
- [x] PASS — signature `(user_id, document_ids=None, num_shards=1)` — `pipeline.py:1546`.
- [x] PASS — worker path zero dispatch + zero index + same WriteSummary — byte-diff
      vs HEAD IDENTICAL; integration worker-path tests.
- [x] PASS — orchestrator `min(num_shards,N)` dispatches + child params + single index
      LAST — `test_orchestrator_fans_out_per_shard_then_indexes_once`.
- [x] PASS — children `num_shards==1` — explicit unit + integration assertions.
- [x] PASS — partition/clamp/empty/failure-isolation carry over — reworked unit + int.
- [x] PASS — worker-path WriteSummary parity — `test_extraction_pipeline.py` unchanged,
      9 passed in full suite.
- [x] PASS — `fanout.py` no `@flow`; grep clean across src/scripts/Makefile/tests.
- [x] PASS — orchestrator one extraction deployment + global_limit; import OK.
- [x] PASS — `run_extraction_fanout.py` + fanout Make target deleted.
- [x] PASS — script `--num-shards` passthrough + 3 guards (exit 1) + `--help` verified.
- [x] PASS — format/lint/pre-commit clean (KGQuery discipline included).
- [x] PASS — unit + fast integration pass (fast loop = subset of the full 268).
- [x] PASS — `integration-tests-all` 268 passed / 1 skipped / 0 warnings, mongot up.
- [ ] [HUMAN] Live worker-path parity — NOT RUN — [HUMAN]; shared stack not quiesced
      for serve-workflows. Script/Make wiring verified by reading + guard exercise.
- [ ] [HUMAN] Live 4-way fan-out — NOT RUN — [HUMAN]; needs serve-workflows.
- [ ] [HUMAN] Negative limiter check — NOT RUN — [HUMAN]; needs serve-workflows.
- [ ] [HUMAN] No second entrypoint (Prefect-UI list portion) — NOT RUN — [HUMAN];
      `make memory-help` portion VERIFIED (one trigger, no `…-fanout`).

**Evidence**
```
$ diff <HEAD memory_extraction worker body> <new worker body>   → IDENTICAL (no output)
$ make memory-unit-tests        → 1413 passed in 47.49s
$ make memory-integration-tests-all (mongot up, quiesced)
  → 268 passed, 1 skipped in 637.32s (0:10:37)   [test_extraction_fanout.py .........]
$ grep -rn "memory_extraction_sharded|memory-extraction-fanout-etl|run_extraction_fanout|run-memory-pipeline-extraction-fanout" apps/memory/
  → (no output, exit 1)
$ uv run python -c "import tree.orchestrator"   → import OK; one extraction deployment
$ uv run python /tmp/adversarial_probe.py       → ALL ADVERSARIAL PROBES PASSED
$ run_memory_pipeline.py --user-id <oid> --num-shards 0   → exit 1
```

**Other issues found**
- None blocking. Note (not a blocker): the worker-path INTEGRATION tests exercise the
  branch via the zero-doc early return rather than driving the full six-task pipeline
  on the worker path; full-pipeline worker parity is covered by the unchanged
  `test_extraction_pipeline.py` instead. Coverage is adequate; flagging only for
  awareness.
- Note: a 0/negative `num_shards` via a direct flow trigger takes the WORKER path
  (the flow branches on `num_shards > 1`), which is even safer than the
  `_resolve_num_shards` lower-bound clamp (the clamp is defense-in-depth on the
  orchestrator path, where it is still correct). Both behaviors verified.

**VERDICT: PASS**

### [PR Reviewer] 2026-05-22 12:05 — Re-review (after #061 rework)

**VERDICT: NO BLOCKERS**

Re-reviewed the full diff (`git diff $(git merge-base HEAD origin/main)...HEAD`), with fresh scrutiny on the #061 delta (commit `331a6fa`). 34 files; concurrency + recursive dispatch areas examined directly.

- Blockers: 0
- Nits: 1 NEW (doc-drift, appended to PR #24 description as item 2) + 1 carried over (`dispatch_concurrency` read-but-unwired, unchanged, already on PR description as item 1).

Key verifications (all confirmed):
- **Recursion termination — SAFE.** The sole `run_deployment(_EXTRACTION_DEPLOYMENT, ...)` (sharding.py:230) hardcodes `"num_shards": 1`. No path lets a child receive `num_shards>1`. Children fall through the `if num_shards > 1` guard to the worker path; recursion terminates at one level. Asserted by `test_fan_out_children_carry_num_shards_one` (unit) + `test_orchestrator_fans_out_per_shard_then_indexes_once` (integration).
- **Worker-path equivalence — byte-for-byte.** `git diff 1da6b65 HEAD -- pipeline.py` shows the worker body + MCP shim entirely unchanged; only additive insertions (import, `_orchestrate_sharded_extraction` helper, param, docstring, early-return guard). Covered by `test_worker_path_*` (zero `run_deployment` calls).
- **Orchestrator correctness.** Exactly one trailing index after the gather; `return_exceptions=True` failure isolation; `_resolve_num_shards` non-positive→1 clamp; balanced `_partition_into_shards`; empty-docs no-op (`shards_total=0`, zero dispatch). All unit + integration tested.
- **No dangling refs** to deleted `fanout.py` / `memory_extraction_sharded` / `memory-extraction-fanout-etl`. The only mentions are in ADR-002's "Amendment (#061)" supersession narrative (correctly labels them DELETED) — that is documentation discipline, not a live claim.
- `make memory-unit-tests` → 1413 passed (firsthand). No debug statements, no ownerless TODOs, no stray artifacts in the diff.

NEW Nit (PR #24 description, item 2): stale "sharded parent flow" wording for `fanout_max_parallel` (`app_config.py:237-238`) + the `fanout_max_parallel`/`doc_concurrency` comments in `default.yaml` — they reference the deleted standalone flow. Values/behavior correct; prose only.

No rollup task filed (zero Blockers). Pipeline may advance to hand-off.
