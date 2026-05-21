# Parent fan-out flow (document-shards) + serve admission control

Status: pending
Tags: `infra`, `memory`, `prefect`
Depends on: #054, #055
Blocks: —

## Scope

The headline capability: run up to 4 memory-extraction runs concurrently over
disjoint document-shards of ONE user, safely under the Voyage limiter, then index
once. Implement ADR-002 §3 and §4. Mirror the fan-out shape of
`dream_consolidation_all_users` (`consolidation/dream.py:838-989`) but fan out over
document-shards instead of users.

- **New module `apps/memory/src/tree/memory/extraction/fanout.py`:**
  `memory_extraction_sharded(user_id, document_ids=None, num_shards=None)` as a Prefect
  `@flow`.
  1. **Resolve pending docs.** If `document_ids` is None, compute the user's
     not-yet-ingested documents: a `Document` is ingested iff its `_id` appears in some
     `knowledge_graph` object's `sources` array (no status flag on `Document`). Otherwise
     use the explicit list. Empty result → no-op (return a zero `FanOutStats`-shaped report).
  2. **Partition.** Split into `num_shards or app_config.concurrency.fanout_max_parallel`
     contiguous batches (~`ceil(len / num_shards)` each); collapse to fewer shards when
     `len < num_shards`.
  3. **Fan out extraction.**
     `results = await asyncio.gather(*[run_deployment("memory-extraction-etl/memory-extraction-etl", parameters={"user_id": str(user_id), "document_ids": shard}) for shard in shards], return_exceptions=True)`
     (`from prefect.deployments import run_deployment`). `return_exceptions=True` isolates
     one shard's failure; aggregate into a `FanOutStats`-shaped report (copy the dataclass
     shape from `dream.py:838-861`, adapted: shards_total / succeeded / failed / failures).
     The existing `memory_extraction(user_id, document_ids=None)` already accepts a subset —
     no signature change.
  4. **Index ONCE.** After the extraction gather completes, trigger a SINGLE
     `run_deployment("memory-indexing-etl/memory-indexing-etl", parameters={"user_id": str(user_id)})`.
     Never per-shard (it is a global backfill; per-shard would race 4 writers).
- **Orchestrator (`apps/memory/src/tree/orchestrator.py`):**
  - Register the parent: `memory_extraction_sharded.to_deployment(name="memory-extraction-fanout-etl", tags=["memory-pipeline", "extraction", "fanout"])`.
  - Set `serve(..., global_limit=app_config.concurrency.runner_global_limit)`.
  - Optionally `.to_deployment(concurrency_limit=4)` on `memory_extraction` (note in log if applied).
- **Make target (`apps/memory/Makefile`):** `run-memory-pipeline-extraction-fanout`
  requiring `USER_ID=<oid>`, optional `NUM_SHARDS=<n>`; invokes a script (mirror
  `run_memory_pipeline.py`) that triggers `memory-extraction-fanout-etl`. The script must
  `init_logger()` at module level and stream logs.

## Acceptance Criteria

- [x] `memory_extraction_sharded` exists in `extraction/fanout.py` as a Prefect flow with
      signature `(user_id, document_ids=None, num_shards=None)`.
- [x] Pending-doc resolution returns only `Document`s whose `_id` is NOT in any
      `knowledge_graph.sources` array (unit/integration test with a seeded ingested + pending doc).
- [x] Partitioning splits N ids into `min(num_shards, N)` contiguous, disjoint shards whose
      union equals the input ids and which never overlap (unit test over several N / num_shards combos,
      incl. `N < num_shards` collapsing to N shards, and N=0 → no-op).
- [x] One shard raising does not abort the batch (`return_exceptions=True`); the report records
      the failure and the other shards still complete (test with a fake `run_deployment` where one shard raises).
- [x] Exactly ONE `memory-indexing-etl` `run_deployment` is issued, AFTER the extraction gather
      (test asserts call order and single indexing call).
- [x] `orchestrator.py` registers `memory-extraction-fanout-etl` and calls
      `serve(global_limit=app_config.concurrency.runner_global_limit)` (read the diff).
- [x] `make memory-run-memory-pipeline-extraction-fanout USER_ID=<oid>` (and with `NUM_SHARDS=4`)
      triggers the parent deployment and streams logs. (Guard + `--help` + import verified; live trigger is the [HUMAN] AC.)
- [x] `make memory-format-fix && make memory-lint-fix && make memory-format-check && make memory-lint-check && make pre-commit` clean.
- [x] `make memory-unit-tests` passes; `make memory-integration-tests` (fast) passes.
- [x] A throttling integration test (extend the `test_dream_supersession_and_fanout.py` pattern)
      fans out runs against a MOCK `run_deployment` and asserts fan-out + single-index. (The live ~3/min
      embed-pacing assertion needs a real worker + Voyage key → folded into the [HUMAN] live-fan-out AC.)
- [x] `make memory-integration-tests-all` passes with the mongot stack up (acceptance gate). [Tester gate]
      Tester-verified: 266 passed, 1 skipped, 0 failed, 0 warnings (655s); `test_extraction_fanout.py` 7/7 passed.
- [ ] [HUMAN] Live 4-way fan-out (stack + `serve-workflows` running, real Voyage key): Prefect UI
      shows the parent spawning 4 child `memory-extraction-etl` runs over DISJOINT shards; ≤4 execute
      at once (`global_limit`); across all children Voyage embeds serialize to ~3/min with NO
      "rate-limit retries exhausted" / 429 warnings (`voyage_embedding.py:214`); a SINGLE
      `memory-indexing-etl` run fires after the shards complete (plan §3).
- [ ] [HUMAN] Negative check: temporarily delete/raise the `voyage-embeddings` limit and confirm
      429 warnings reappear under 4-way fan-out — proving the limiter holds the line (plan §7).

## User Stories

### Story: Operator runs a 4-way sharded extraction for one user
1. Operator runs `make local-start`, then `make memory-sync-concurrency-limits`, then `make memory-serve-workflows &`.
2. Operator runs `make memory-run-memory-pipeline-extraction-fanout USER_ID=<oid> NUM_SHARDS=4`.
3. In the Prefect UI they see one parent run and 4 child `memory-extraction-etl` runs over disjoint document-shards.
4. At most 4 runs execute at once; Voyage embeds across all children pace to ~3/min with no 429 warnings.
5. After all 4 shards finish, exactly one `memory-indexing-etl` run starts.
6. The parent flow returns a report: shards_total=4, succeeded=4, failed=0.

### Story: One shard fails but the batch survives
1. Operator triggers a fan-out where one shard hits a transient error.
2. The other 3 shards complete; the failing shard is recorded in the report's `failures` map.
3. The single indexing run still fires after the gather.
4. Operator sees `succeeded=3 failed=1` and the failure message in the logs.

### Story: Operator triggers fan-out with no pending documents
1. Operator runs the fan-out for a user whose documents are all already ingested.
2. The flow resolves zero pending docs and returns a zero report (no child runs, no indexing run).
3. Logs say there was nothing to do.

### Story: Operator passes an explicit document subset
1. Operator triggers the fan-out with an explicit `document_ids` list of 6 ids and `NUM_SHARDS=4`.
2. The 6 ids are partitioned into 4 disjoint contiguous shards (sizes 2,2,1,1).
3. Each shard maps to one child run; their union is exactly the 6 ids.

---

Blocked by: #054, #055

## Log

### [SWE] 2026-05-21 — Implementation

**Files modified**
- `apps/memory/src/tree/memory/extraction/fanout.py` (NEW) — `memory_extraction_sharded` Prefect flow + pure helpers (`_partition_into_shards`, `_resolve_pending_document_ids`, `_fan_out_extraction`, `FanOutStats`). Mirrors the dream fan-out shape over document-shards of one user.
- `apps/memory/src/tree/orchestrator.py` — registered `memory-extraction-fanout-etl` (tags `memory-pipeline,extraction,fanout`); set `serve(..., global_limit=app_config.concurrency.runner_global_limit)`.
- `apps/memory/scripts/run_extraction_fanout.py` (NEW) — triggers the fan-out deployment; `init_logger()` at module level, streams logs, polls to final state. Args `--user-id` (required, USER_ID env fallback), `--num-shards` (optional).
- `apps/memory/Makefile` — `run-memory-pipeline-extraction-fanout` target (requires `USER_ID`, optional `NUM_SHARDS`).
- `apps/memory/scripts/check_kgquery_discipline.py` — added `fanout.py` to `_ALLOWLIST` (the single `kg.find` threads `user_id`; tenant-isolation test added).
- `apps/memory/tests/unit/memory/extraction/test_fanout.py` (NEW) — 27 pure-logic unit tests (partitioning + gather/failure-isolation/single-index, `run_deployment` mocked).
- `apps/memory/tests/integration/memory/test_extraction_fanout.py` (NEW) — 7 tests: pending-doc resolution against Mongo (incl. tenant-scoping) + parent-flow fan-out spying on a faked `run_deployment`.

**Tests**
- Unit: 1381 passing, 0 failing (`make memory-unit-tests`) — incl. 27 new fanout tests.
- Integration (fast loop, `make memory-integration-tests`): 153 passed, 1 skipped, 113 deselected.
- Integration (new slow fanout file, isolated): 7 passed (`tests/integration/memory/test_extraction_fanout.py`). Stack quiesced; no serve-workflows started (run_deployment mocked).

**Acceptance criteria**
- [x] `memory_extraction_sharded` exists in `extraction/fanout.py` as a Prefect flow `(user_id, document_ids=None, num_shards=None)`.
- [x] Pending-doc resolution returns only `Document`s whose `_id` ∉ any `knowledge_graph.sources` — `test_resolution_returns_only_not_yet_ingested_docs`, `test_resolution_is_tenant_scoped`, `test_resolution_empty_when_all_ingested`.
- [x] Partitioning: `min(num_shards, N)` contiguous disjoint shards; union == input; `N<num_shards`→N shards; N=0→no-op — `test_partition_*`, `test_explicit_six_ids_four_shards_sizes_2_2_1_1`, `test_fan_out_no_shards_is_noop`.
- [x] One shard raising does not abort the batch (`return_exceptions=True`); failure recorded; others complete — `test_fan_out_isolates_one_shard_failure`, `test_flow_isolates_one_shard_failure_and_still_indexes`.
- [x] Exactly ONE `memory-indexing-etl` run AFTER the gather — `test_fan_out_*` (asserts order + count), `test_flow_fans_out_per_shard_then_indexes_once`.
- [x] `orchestrator.py` registers `memory-extraction-fanout-etl` + `serve(global_limit=...)` — import check below; read the diff.
- [x] `make memory-run-memory-pipeline-extraction-fanout USER_ID=<oid>` (+ `NUM_SHARDS`) triggers the deployment and streams logs — script `--help` + USER_ID guard verified; live trigger is part of the [HUMAN] AC.
- [x] Format/lint/pre-commit clean.
- [x] `make memory-unit-tests` passes; `make memory-integration-tests` (fast) passes.
- [x] Throttling-pattern integration test (extended the dream fan-out spy pattern) — `test_extraction_fanout.py` spies on a faked `run_deployment` and asserts fan-out + single-index. NOTE: the live rate-pacing assertion (~3/min embeds, all runs complete) needs a real worker + Voyage key → folded into the [HUMAN] live-fan-out AC; this test covers the parent-flow contract.
- [ ] `make memory-integration-tests-all` (mongot up) — NOT RUN by SWE; acceptance gate is the Tester's. Fast loop + new slow file run clean.
- [ ] [HUMAN] Live 4-way fan-out (serve-workflows + real Voyage key).
- [ ] [HUMAN] Negative check: delete/raise the `voyage-embeddings` limit, confirm 429s reappear.

**Evidence**
```
$ make memory-unit-tests
============================ 1381 passed in 42.01s =============================

$ make memory-integration-tests   # fast loop
========== 153 passed, 1 skipped, 113 deselected in 180.31s (0:03:00) ==========

$ uv run pytest tests/integration/memory/test_extraction_fanout.py -q
7 passed in 4.10s   (EXIT=0)

$ uv run python -c "import tree.orchestrator; from tree.memory.extraction.fanout import memory_extraction_sharded; ..."
orchestrator import OK
memory_extraction_sharded: memory-extraction-fanout-etl

$ make memory-run-memory-pipeline-extraction-fanout       # missing USER_ID guard
USAGE: make run-memory-pipeline-extraction-fanout USER_ID=<24-char Mongo ObjectId> [NUM_SHARDS=<n>]

$ uv run python scripts/run_extraction_fanout.py --help
  --user-id TEXT        Tenant id ...
  --num-shards INTEGER  How many document-shards ...
```

**Notes**
- **Partition shape decision.** The spec's "~ceil(N/num_shards) each" plus the explicit AC "6 ids, num_shards=4 → 2,2,1,1 (4 shards)" are only jointly satisfiable by a BALANCED split into exactly `min(num_shards,N)` shards (sizes differ by ≤1, larger shards lead), not a fixed `ceil`-chunk (which would emit only 3 shards [2,2,2] for N=6). Implemented the balanced split; e.g. `(7,3)→[3,2,2]`.
- **Optional `concurrency_limit=4` on `memory_extraction`: NOT applied.** ADR-002 §4 names `serve(global_limit=runner_global_limit)` as the admission control; the spec marks the per-deployment `concurrency_limit` "optional". Adding a second overlapping cap on the same single deployment is redundant with the global limit and splits the admission knob across two places. Kept it single-sourced (global_limit only). Note per task instruction.
- **KGQuery discipline.** `fanout.py` does one raw `kg.find` on `knowledge_graph` for pending-doc resolution; it threads `user_id` into the filter and carries a tenant-isolation integration test (`test_resolution_is_tenant_scoped`). Added to `_ALLOWLIST` with that justification — the same audited-tenant-locked pattern `dream.py`/`meta_state.py` already use. Not an architectural fork.
- **Cosmetic teardown noise.** Running a real flow under pytest, Prefect's `APILogHandler` background worker flushes after pytest tears down stdout/stderr capture → a post-verdict `ValueError: I/O operation on closed file` / "Stopping temporary server" on stderr. It appears in `test_dream_supersession_and_fanout.py` runs too, is emitted AFTER the `N passed` verdict, and exit code stays 0. No pytest-level warnings (`-W error::DeprecationWarning` still 7 passed, exit 0).
- **NOT RUN — [HUMAN] acceptance; shared stack not quiesced for serve-workflows.** The live 4-way fan-out [HUMAN] AC (§3) and the limiter negative-check (§7) require `make memory-serve-workflows` + a real Voyage key on a quiesced stack. Per task instructions and CLAUDE.md (stale cross-worktree serve-workflows + a hung flow per #055 QA), serve-workflows was NOT started to avoid worsening contention. `make memory-integration-tests-all` (mongot acceptance gate) is the Tester's gate.

### [Tester] 2026-05-21 19:58 — QA

**Test summary**
- Format / lint / pre-commit: PASS (`make pre-commit` — all hooks Passed, incl. "KGQuery discipline (memory)")
- Unit tests: 1381 passed / 0 failed (incl. 27 in `test_fanout.py`)
- Integration tests (acceptance gate `make memory-integration-tests-all`): 266 passed / 0 failed / 1 skipped (655s, exit 0); `test_extraction_fanout.py` 7/7
- Warnings: 0

**E2E adversarial pass** (logic-level; live stack not started per task instruction — [HUMAN] ACs)
- Happy path (`_fan_out_extraction`, run_deployment mocked): 6 ids / 4 shards → 4 extraction runs (`{user_id, document_ids}`) + 1 trailing indexing run (`{user_id}` only), index LAST, `FanOutStats(4,4,0)` — PASS
- Break path 1 (partition boundaries: N=0/1/2/3/4/20/100, k=1/3/4/7/10000): every probe contiguous + disjoint + union==input-in-order + count==min(k,N); AC `(6,4)→[2,2,1,1]` and `(7,3)→[3,2,2]` confirmed — PASS
- Break path 2 (failure isolation): 2nd shard raises → isolated, recorded in `failures`, other 3 succeed, single index still fires last; ALL-shards-fail still issues exactly ONE index run (partial extraction still indexed) — PASS
- Break path 3 (no-op): `shards=[]` → zero `run_deployment` calls, `FanOutStats(shards_total=0)` — PASS
- Break path 4 (hostile CLI inputs): missing USER_ID → guarded exit 1; invalid ObjectId → guarded exit 1; `--num-shards -3` → guarded exit 1; `make ... ` w/o USER_ID → USAGE + exit — PASS
- Break path 5 (hostile `num_shards` reaching the helper directly): `_partition_into_shards(ids, 0)` raises `ZeroDivisionError`; negative → `[]` (silent no-op). NOTE below — NOT reachable via the documented script/Make path (guarded `< 1`); flow's `num_shards or default` neutralises `None`/`0`, but a negative passed via a direct Prefect-API trigger reaches the helper and yields a silent zero-shard no-op. Non-blocking (no crash/corruption; only entry path is guarded), flagged for follow-up.

**Acceptance criteria**
- [x] PASS — `memory_extraction_sharded` exists as a Prefect flow `(user_id, document_ids=None, num_shards=None)` — `fanout.py:265-270`; `@flow(name="memory-extraction-fanout-etl")`; `.name == "memory-extraction-fanout-etl"` verified.
- [x] PASS — Pending-doc resolution returns only docs whose `_id` ∉ any `knowledge_graph.sources`, tenant-scoped — `test_resolution_returns_only_not_yet_ingested_docs`, `test_resolution_is_tenant_scoped`, `test_resolution_empty_when_all_ingested` (all pass in the 7); `kg.find` threads `user_id` (`fanout.py:169`).
- [x] PASS — Partition → `min(num_shards,N)` contiguous disjoint shards, union==input, N<num_shards collapses, N=0 no-op — direct probe (11 N/k combos) + 27 unit tests; AC `(6,4)→[2,2,1,1]` confirmed.
- [x] PASS — One shard raising does not abort the batch; recorded; others complete — adversarial probe 2 + `test_fan_out_isolates_one_shard_failure` + `test_flow_isolates_one_shard_failure_and_still_indexes`.
- [x] PASS — Exactly ONE `memory-indexing-etl` run, AFTER the gather — probes 1-3 (index is `calls[-1]`, count==1, scoped to `{user_id}` only) + `test_fan_out_all_extraction_runs_precede_the_index_run`.
- [x] PASS — `orchestrator.py` registers `memory-extraction-fanout-etl` + `serve(global_limit=app_config.concurrency.runner_global_limit)` — diff read (`orchestrator.py:73-84`); `import tree.orchestrator` clean.
- [x] PASS — `make memory-run-memory-pipeline-extraction-fanout USER_ID=<oid>` (+ NUM_SHARDS) triggers + streams — Makefile target + script read; USER_ID guard, ObjectId guard, `--num-shards` guard exercised; live trigger is the [HUMAN] AC.
- [x] PASS — format/lint/pre-commit clean.
- [x] PASS — `make memory-unit-tests` (1381) + `make memory-integration-tests` (fast loop) green; full `-all` gate green below.
- [x] PASS — Throttling-pattern integration test (faked `run_deployment`, fan-out + single-index) — `test_extraction_fanout.py` parent-flow tests pass. Live ~3/min embed pacing is the [HUMAN] AC.
- [x] PASS — `make memory-integration-tests-all` (mongot up, quiesced + isolated): 266 passed / 1 skipped / 0 failed / 0 warnings (655s).
- [ ] NOT RUN — [HUMAN] acceptance; shared stack not quiesced for serve-workflows. Live 4-way fan-out (UI: 4 disjoint child runs, ≤4 concurrent, ~3/min embeds, single trailing index). Wiring verified by reading the Makefile target + script and exercising the guards; live run deliberately not triggered (stale cross-worktree workers + hung flow per #055).
- [ ] NOT RUN — [HUMAN] acceptance; shared stack not quiesced. Negative check (delete/raise `voyage-embeddings` limit → 429s reappear under 4-way fan-out). Requires a real Voyage key + isolated live worker.

**Evidence**
```
$ make pre-commit
ruff check ... Passed / ruff format ... Passed / KGQuery discipline (memory) ... Passed

$ make memory-unit-tests
============================ 1381 passed in 42.91s =============================

$ uv run pytest tests/unit/memory/extraction/test_fanout.py -q
27 passed in 0.94s

$ make memory-integration-tests-all       # acceptance gate, mongot up, quiesced
tests/integration/memory/test_extraction_fanout.py .......               [ 62%]
================== 266 passed, 1 skipped in 655.44s (0:10:55) ==================   (EXIT=0)

$ uv run python -c "import tree.orchestrator; from tree.memory.extraction.fanout import memory_extraction_sharded; print(memory_extraction_sharded.name)"
memory-extraction-fanout-etl
```

**Other issues found**
- Non-blocking: a negative `num_shards` reaching `_partition_into_shards` via a direct Prefect-API/UI trigger (bypassing the guarded script/Make path) yields a silent zero-shard no-op rather than an error. All documented entry points guard it (`--num-shards >= 1`; flow's `num_shards or default` handles `None`/`0`). Suggested cheap hardening for a follow-up: clamp/validate `effective_num_shards >= 1` at the top of `memory_extraction_sharded` so the flow itself is robust to a hand-crafted negative param. Not in scope for #056 ACs.
- Diff scope clean: changes confined to `fanout.py` (new), `orchestrator.py`, `run_extraction_fanout.py` (new), `Makefile`, `check_kgquery_discipline.py` (+ its allowlist), and the two new test files. No edits to `extraction/pipeline.py` or unrelated modules. (The `057/058/059 .groomed.md` untracked files are unrelated planning artifacts, not code.)
- Note: this `-all` run showed NO `APILogHandler` stderr noise and `test_web_serp` passed — neither of the SWE-noted flakes surfaced.

**VERDICT: PASS**
