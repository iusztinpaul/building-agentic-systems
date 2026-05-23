# Serve-registration test rework + full live e2e acceptance (both pipelines)

Status: pending
Tags: `infra`, `tests`
Depends on: #067, #068
Blocks: —

## Scope

Final task of the orchestrator/worker re-architecture. Two parts: (1) rework the
`tree.orchestrator` serve-registration unit test so it asserts the FINAL four-name
deployment set after BOTH splits land, preserving the #065 guards; (2) run the full
acceptance suite and the `[HUMAN]` live e2e for BOTH pipelines (the distinct-name UI
check that is the whole point of the feature), plus the stale-deployment cleanup.

### 1. Rework `tests/unit/test_orchestrator.py`

The existing `test_serve_deployments_registers_all_deployments` asserts the registered
deployment-name set equals the OLD set (which includes `data-pipeline-etl` and
`memory-extraction-etl`). After #067 + #068 those two are gone and four new ones exist.
Update the expected set to:

```
{
    "data-etl-worker",
    "data-etl-orchestrator",
    "memory-extract-etl-worker",
    "memory-extract-etl-orchestrator",
    "memory-indexing-etl",
    "ingest-file-etl",
    "ingest-conversation-etl",
    "ingest-youtube-video-batch-etl",
    "ingest-youtube-rss-feed-batch-etl",
    "dream-consolidation-etl",
}
```

- Additionally assert the two RETIRED names are NOT present:
  `assert "memory-extraction-etl" not in deployment_names` and
  `assert "data-pipeline-etl" not in deployment_names`.
- PRESERVE the two #065 guards unchanged:
  `test_serve_deployments_passes_limit_not_global_limit` (kwarg is `limit`, not
  `global_limit`) and `test_serve_deployments_kwargs_bind_to_real_serve_signature`
  (binds to the real `prefect.serve` signature). Do not weaken either.
- Keep the dream-cron registration in the set (it is registered with `cron=`).

This single test is reworked HERE (not in #067/#068) so the name-set assertion churns
exactly once, after both splits have landed — avoiding a mid-feature edit that #068
would immediately invalidate.

### 2. Full acceptance suite

Run, on a quiesced + isolated mongot stack (per CLAUDE.md — never against a contended
shared stack, run `requires_mongot` suites in isolation):
- `make memory-format-check && make memory-lint-check && make pre-commit`
- `make memory-unit-tests`
- `make memory-integration-tests-all`

Append the full output to this task's log.

### 3. `[HUMAN]` live e2e — the distinct-name UI check (the whole point)

With the docker stack up (`make local-start`), `make memory-serve-workflows` running
to register the new deployments, and a real (rate-paced) Voyage key in `.env`, run BOTH:

**Memory:**
1. `make memory-run-memory-pipeline-extraction USER_ID=<oid> NUM_SHARDS=2`
2. Confirm in the Prefect UI: the PARENT run is named `memory-extract-etl-orchestrator`
   and exactly TWO child runs are named `memory-extract-etl-worker`.
3. Confirm exactly ONE `memory-indexing-etl` run fires after the workers complete.

**Data:**
4. `make memory-run-data-pipeline USER_ID=<oid> NUM_SHARDS=2`
5. Confirm in the Prefect UI: the PARENT run is named `data-etl-orchestrator` and
   exactly TWO child runs are named `data-etl-worker`.
6. Confirm NO index run fires for the data pipeline.

**Stale-deployment cleanup (live):**
7. Run `prefect deployment delete memory-extraction-etl/memory-extraction-etl` and
   `prefect deployment delete data-pipeline-etl/data-pipeline-etl` (and
   `memory-extraction-fanout-etl` if it lingers). Confirm via `prefect deployment ls`
   that only the four new deployments + `memory-indexing-etl` + the other unchanged
   deployments remain.

Record the UI observations (parent + child run names, index-run presence/absence) and
the cleanup output in the task log.

## Acceptance Criteria

- [x] `tests/unit/test_orchestrator.py` asserts the registered-deployment-name set is
      exactly the ten-name FINAL set (four new names + `memory-indexing-etl` + file +
      conversation + 2 youtube + dream), and explicitly asserts `memory-extraction-etl`
      and `data-pipeline-etl` are ABSENT.
- [x] The two #065 guards (`limit` not `global_limit`; binds to real `prefect.serve`
      signature) are preserved and still pass.
- [x] `make memory-format-check && make memory-lint-check && make pre-commit` clean.
- [x] `make memory-unit-tests` passes with 0 warnings.
- [x] `make memory-integration-tests-all` passes on a quiesced + isolated mongot stack;
      full output appended to the log. (Tester 2026-05-23: 269 passed / 1 skipped
      [unrelated Prefect-reachability gate] / 0 failed / 0 warnings, exit 0.)
- [ ] [HUMAN] Memory live e2e: `make memory-run-memory-pipeline-extraction USER_ID=<oid> NUM_SHARDS=2`
      shows a `memory-extract-etl-orchestrator` parent + exactly 2
      `memory-extract-etl-worker` children + exactly 1 `memory-indexing-etl` run in the
      Prefect UI. (Recorded with the observed run names.)
- [ ] [HUMAN] Data live e2e: `make memory-run-data-pipeline USER_ID=<oid> NUM_SHARDS=2`
      shows a `data-etl-orchestrator` parent + exactly 2 `data-etl-worker` children and
      NO index run in the Prefect UI. (Recorded with the observed run names.)
- [ ] [HUMAN] Stale deployments deleted: `prefect deployment delete` removes
      `memory-extraction-etl` and `data-pipeline-etl`; `prefect deployment ls` confirms
      only the new + unchanged deployments remain.

## User Stories

### Story: A reviewer confirms the serve registration matches the new topology
1. Reviewer runs `make memory-unit-tests`.
2. `test_serve_deployments_registers_all_deployments` passes, asserting the four new
   deployment names are registered and the two old names are gone.
3. The `limit`-not-`global_limit` and signature-binding guards still pass — the #065
   admission-control fix was not regressed by the topology change.

### Story: Operator visually confirms the orchestrator/worker split in the UI
1. Operator triggers each orchestrator with `NUM_SHARDS=2` (memory, then data).
2. Opening the Prefect UI, the operator sees, for memory: one
   `memory-extract-etl-orchestrator` parent, two `memory-extract-etl-worker` children,
   one `memory-indexing-etl` run.
3. For data: one `data-etl-orchestrator` parent, two `data-etl-worker` children, and no
   index run.
4. The orchestrator-vs-worker boundary the owner asked for is now visible by NAME — the
   whole point of the feature is confirmed.

### Story: Operator removes the orphaned old deployments
1. After re-serving workflows, the operator lists deployments and sees the stale
   `memory-extraction-etl` and `data-pipeline-etl`.
2. They delete both with `prefect deployment delete`.
3. `prefect deployment ls` shows a clean set: the four new deployments plus
   `memory-indexing-etl`, the ingest deployments, and the dream cron.

---

Blocked by: #067, #068

## Log

### [PM] 2026-05-23 — Grooming

**Summary**
Closes the feature: reworks the single serve-registration name-set unit test to the
final four-name topology (after both splits land — so it churns exactly once),
preserves the #065 `limit`-not-`global_limit` guards, then runs the full acceptance
suite and the `[HUMAN]` live distinct-name UI check for BOTH pipelines plus the live
stale-deployment cleanup.

**Key decisions**
- The serve name-set test is reworked HERE rather than in #067/#068 so it is edited
  once (post-both-splits) instead of being invalidated mid-feature. #067 and #068 each
  cover their own flows + fan-out via dedicated unit tests, leaving this aggregate
  registration assertion for the end.
- The distinct-name UI check is the feature's headline acceptance and is irreducibly
  `[HUMAN]` (the in-process suites mock `run_deployment`, so they cannot observe live
  child-run NAMES in the Prefect UI). It is consolidated here for both pipelines so the
  operator runs one live pass at the end.
- Live stale-deployment deletion is consolidated here (one ops pass) though each split
  task documents its own deletion command.

**Dependencies**
- #067 (memory split) + #068 (data split) — both must land before the final name set
  is correct and the live e2e can observe all four new deployments.

**User stories**
- 3 stories covering: the reworked registration test, the live UI distinct-name
  confirmation for both pipelines, and the live cleanup.

Ready for implementation.

### [SWE] 2026-05-23 — Implementation (test rework)

**Files modified**
- `apps/memory/tests/unit/test_orchestrator.py` — finalized the serve-registration
  assertion to the FINAL post-split topology (#067 + #068 both landed): added
  `test_serve_deployments_registers_dream_with_its_cron` (robust dream-cron guard),
  added the `memory-extraction-fanout-etl` absence assertion, clarified docstrings,
  and imported `app_config` for the cron value. The full-set `==` comparison and the
  two retired-name absence checks were already correct from #067/#068 incremental
  edits; no change to `orchestrator.py` (it already registers the final four-name set).

**Tests**
- Unit: 1448 passing, 0 failing, 0 warnings — `make memory-unit-tests`. The 4
  orchestrator tests: `..passes_limit_not_global_limit`,
  `..kwargs_bind_to_real_serve_signature`, `..registers_all_deployments`,
  `..registers_dream_with_its_cron`.
- Integration: N/A here — `make memory-integration-tests-all` is the Tester's
  acceptance gate (per task scope, NOT run by SWE).

**Final expected-name set asserted (full-set `==`, drift-catching):**
`{data-etl-orchestrator, data-etl-worker, memory-extract-etl-orchestrator,
memory-extract-etl-worker, memory-indexing-etl, ingest-file-etl,
ingest-conversation-etl, ingest-youtube-video-batch-etl,
ingest-youtube-rss-feed-batch-etl, dream-consolidation-etl}` — 10 names.
Absent-asserted: `memory-extraction-etl`, `data-pipeline-etl`,
`memory-extraction-fanout-etl`. Dream cron asserted == `app_config.dream.cron`
(`"0 4 * * *"`), and asserted to be the ONLY scheduled deployment.

**Acceptance criteria**
- [x] Registered-name set is exactly the ten-name FINAL set + retired names ABSENT —
      `test_serve_deployments_registers_all_deployments`.
- [x] #065 guards (`limit` not `global_limit`; binds to real `prefect.serve`
      signature) preserved and passing —
      `test_serve_deployments_passes_limit_not_global_limit` +
      `test_serve_deployments_kwargs_bind_to_real_serve_signature`.
- [x] `make memory-format-check && make memory-lint-check && make pre-commit` clean.
- [x] `make memory-unit-tests` passes with 0 warnings (1448 passed).
- [ ] `make memory-integration-tests-all` — Tester's acceptance gate, NOT run by SWE.
- [ ] [HUMAN] Memory live e2e (NUM_SHARDS=2 distinct-name UI check) — orchestrator-driven.
- [ ] [HUMAN] Data live e2e (NUM_SHARDS=2 distinct-name UI check) — orchestrator-driven.
- [ ] [HUMAN] Stale-deployment deletion (`prefect deployment delete`) — orchestrator-driven.

**Evidence — mutation red → revert → green**
Mutation 1 (rename a registration `data-etl-worker` → `data-etl-worker-WRONG`):
```
E  AssertionError: assert {...} == {...}
E    Extra items in the left set:  'data-etl-worker-WRONG'
E    Extra items in the right set: 'data-etl-worker'
FAILED tests/unit/test_orchestrator.py::test_serve_deployments_registers_all_deployments
1 failed in 5.68s
```
Mutation 2 (drop `cron=app_config.dream.cron` from the dream deployment):
```
E  AssertionError: assert {} == {'dream-conso...['0 4 * * *']}
E    Right contains 1 more item: {'dream-consolidation-etl': ['0 4 * * *']}
FAILED tests/unit/test_orchestrator.py::test_serve_deployments_registers_dream_with_its_cron
1 failed in 5.77s
```
Both mutations reverted (`git diff --stat apps/memory/src/tree/orchestrator.py` empty);
suite green after revert:
```
$ make memory-unit-tests
... 1448 passed in 43.31s
$ uv run pytest tests/unit/test_orchestrator.py -v
test_serve_deployments_passes_limit_not_global_limit PASSED
test_serve_deployments_kwargs_bind_to_real_serve_signature PASSED
test_serve_deployments_registers_all_deployments PASSED
test_serve_deployments_registers_dream_with_its_cron PASSED
4 passed in 6.05s
```

**Notes**
- CODE-only task. The live e2e for BOTH orchestrators (NUM_SHARDS=2, distinct
  parent/child run-name UI confirmation: `memory-extract-etl-orchestrator` parent +
  2 `memory-extract-etl-worker` children + 1 `memory-indexing-etl`; `data-etl-orchestrator`
  parent + 2 `data-etl-worker` children + NO index run) AND the stale-deployment
  deletion (`prefect deployment delete memory-extraction-etl/memory-extraction-etl`,
  `prefect deployment delete data-pipeline-etl/data-pipeline-etl`, plus
  `memory-extraction-fanout-etl` if it lingers) are the orchestrator-driven `[HUMAN]`
  steps — NOT performed here. Per task scope, `serve-workflows` was NOT started and
  `integration-tests-all` was NOT run (Tester's gate).
- NOT COMMITTED — handing to Tester first per workflow.

### [Tester] 2026-05-23 — QA (CODE portion + full-feature acceptance gate)

**Diff scope**
- `git diff --stat` confined to `apps/memory/tests/unit/test_orchestrator.py`
  (+53/-2). `git diff apps/memory/src/tree/orchestrator.py` EMPTY — orchestrator.py
  NOT modified (already final from #067/#068). Untracked: tracker files only.

**Test summary**
- Format check: PASS (`make memory-format-check` — 270 files already formatted)
- Lint check: PASS (`make memory-lint-check` — All checks passed)
- Pre-commit: PASS (prettier, ruff check, ruff format, biome, KGQuery discipline all Passed)
- Unit tests: 1448 passed / 0 failed / 0 warnings (`make memory-unit-tests`, 45.68s)
- Integration (FULL acceptance gate, slow + requires_mongot):
  **269 passed / 1 skipped / 0 failed / 0 warnings** (`make memory-integration-tests-all`,
  615.63s, exit 0) on a quiesced + isolated mongot stack (tree-mongot / tree-mongodb /
  tree-prefect-server up & healthy; no concurrent suite). The 1 skip is
  `test_web_search_ingest.py` — `skipif(not _prefect_server_reachable())`, a live
  serve-worker-trigger test gated on a running serve-workflows process; UNRELATED to
  this feature (CODE-only QA does not start serve-workflows). No SERP/Bright Data flake
  this run; no post-run Prefect stderr.

**Name-set comparison (test asserts vs orchestrator.py actually registers)**
Read `orchestrator.py` `serve_deployments(...)` → registers exactly these 10 names:
`data-etl-orchestrator`, `data-etl-worker`, `memory-extract-etl-orchestrator`,
`memory-extract-etl-worker`, `memory-indexing-etl`, `ingest-file-etl`,
`ingest-conversation-etl`, `ingest-youtube-video-batch-etl`,
`ingest-youtube-rss-feed-batch-etl`, `dream-consolidation-etl`. The test's full-set
`==` assertion is byte-for-byte identical to this set. Dream cron registered as
`cron=app_config.dream.cron` (`"0 4 * * *"`, confirmed via config + live import). The
3 retired names asserted absent: `memory-extraction-etl`, `data-pipeline-etl`,
`memory-extraction-fanout-etl`.

**Non-vacuity — mutation RED → revert → GREEN**
- Mutation A (rename `memory-extract-etl-worker` → `...-worker-MUTANT` in orchestrator.py):
  `test_serve_deployments_registers_all_deployments` FAILED —
  `Extra items in the left set: 'memory-extract-etl-worker-MUTANT'` /
  `Extra items in the right set: 'memory-extract-etl-worker'`.
- Mutation B (drop `cron=app_config.dream.cron` from the dream deployment):
  `test_serve_deployments_registers_dream_with_its_cron` FAILED —
  `assert {} == {'dream-consolidation-etl': ['0 4 * * *']}`.
- The two #065 guards (`...passes_limit_not_global_limit`,
  `...kwargs_bind_to_real_serve_signature`) correctly stayed GREEN under both mutations
  (name/cron-independent). 2 failed, 2 passed under mutation.
- Reverted via `git checkout` → `git diff orchestrator.py` EMPTY → all 4 orchestrator
  tests GREEN; full unit suite 1448 passed.

**Adversarial pass**
- Break path 1 (retired-name leakage into LIVE code):
  `grep -rn "memory-extraction-etl|data-pipeline-etl|memory-extraction-fanout-etl"
  apps/memory/src apps/memory/scripts apps/memory/deploy` → ZERO hits. Only refs are the
  test's own absence assertions/docstrings. PASS.
- Break path 2 (import cleanliness): `uv run python -c "import tree.orchestrator"` →
  `import OK`; `app_config.dream.cron == '0 4 * * *'`. No import-time crash. PASS.
- Break path 3 (run-target drift — do the Make targets/scripts point at the
  ORCHESTRATORS not retired names?): `run_data_pipeline.py` →
  `DEPLOYMENT_NAME = "data-etl-orchestrator/data-etl-orchestrator"`;
  `run_memory_pipeline.py` →
  `"memory-extract-etl-orchestrator/memory-extract-etl-orchestrator"`. Make targets
  `run-data-pipeline` / `run-memory-pipeline-extraction` invoke those scripts with
  `--num-shards`. Correct orchestrator targeting. PASS. (Read-only — NOT executed.)

**Acceptance criteria**
- [x] PASS — Registered set is exactly the ten-name FINAL set + 2 retired names ABSENT
      (third retired fan-out name also asserted absent). Evidence: full-set `==` in
      `test_serve_deployments_registers_all_deployments`; name set verified against
      `orchestrator.py:58-109`; mutation A goes red.
- [x] PASS — #065 guards (`limit` not `global_limit`; binds to real `prefect.serve`)
      preserved & passing. Evidence: `test_serve_deployments_passes_limit_not_global_limit`
      + `test_serve_deployments_kwargs_bind_to_real_serve_signature` GREEN; stayed green
      under both mutations.
- [x] PASS — `make memory-format-check && make memory-lint-check && make pre-commit` clean.
- [x] PASS — `make memory-unit-tests` passes with 0 warnings (1448 passed).
- [x] PASS — `make memory-integration-tests-all` passes on quiesced + isolated mongot
      stack: 269 passed / 1 skipped (unrelated, Prefect-reachability gate) / 0 failed /
      0 warnings. Worker-split integration tests included & green:
      `test_extraction_fanout` (10), `test_extraction_pipeline` (9), data `test_pipeline`
      (5), `test_indexing_pipeline` (6).
- [ ] [HUMAN] Memory live e2e (NUM_SHARDS=2 distinct-name UI check) — NOT RUN
      (orchestrator-driven; serve-workflows not started per task scope). Run target
      verified to point at `memory-extract-etl-orchestrator`.
- [ ] [HUMAN] Data live e2e (NUM_SHARDS=2 distinct-name UI check) — NOT RUN
      (orchestrator-driven). Run target verified to point at `data-etl-orchestrator`.
- [ ] [HUMAN] Stale-deployment deletion — NOT RUN (orchestrator-driven; no deployments
      deleted, serve-workflows not started).

**Evidence**
```
$ make memory-unit-tests
... 1448 passed in 45.68s

$ make memory-integration-tests-all
collected 270 items
... 269 passed, 1 skipped in 615.63s (0:10:15)   # exit 0

$ uv run pytest tests/unit/test_orchestrator.py -v   # under mutations A+B
test_serve_deployments_passes_limit_not_global_limit PASSED
test_serve_deployments_kwargs_bind_to_real_serve_signature PASSED
test_serve_deployments_registers_all_deployments FAILED
test_serve_deployments_registers_dream_with_its_cron FAILED
2 failed, 2 passed in 5.98s
# after git checkout revert: 4 passed
```

**Other issues found**
- None. The test rework matches the spec exactly (10-name full-set `==`, 3 retired names
  absent, both #065 guards intact, robust dream-cron schedule guard added). orchestrator.py
  untouched as required. No live code references the retired deployment names.

**VERDICT: PASS** (CODE portion + full-feature integration acceptance gate). The 3
`[HUMAN]` live-UI / stale-deletion criteria remain for the orchestrator to drive.
