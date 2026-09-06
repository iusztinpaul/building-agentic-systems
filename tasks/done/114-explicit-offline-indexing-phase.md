---
id: 114-explicit-offline-indexing-phase
feature: embedding-clusters-viz
status: done
---

# Indexing becomes an explicit **Offline phase** (`run_indexing`) — out of the extraction Coordinator

Tags: `pipeline`, `offline`, `scripts`, `docs`
Depends on: None
Blocks: #117
Implements: ADR-007 (`embedding-clusters-viz`) — Decision 5 (pipeline shape); supersedes ADR-002 §3's single-trailing-index rule

## Scope

Fix the pipeline-shape bug: today `memory_indexing` is hidden inside the extraction
Coordinator (`_fan_out_extraction` in `tree/memory/graph/sharding.py` runs it inline once after
the shards) and `scripts/run_indexing_pipeline.py` runs the flow in-process, unlike every other
script. After this task `tree.offline.offline_pipeline` is the "mother" pipeline with THREE
explicit, independently switchable **Offline phases** (the fourth, clustering, lands in #117):

**1. `offline_pipeline` (`tree/offline.py`)** — new parameter `run_indexing: bool = True`,
placed after `run_extraction`, forwarded by `dispatch_offline_pipeline`:
- Phase 1 `run_data` (unchanged) → phase 2 `run_extraction` (unchanged: one
  `memory_extract_etl_coordinator` inline subflow per target user) → **phase 3 `run_indexing`**:
  for each target user, ONE `memory_indexing(user_id=uid)` inline subflow under
  `with tags(*TAGS_INDEXING)`, per-user failure isolation identical to phase 2
  (`logger.exception` + `{"error": str(exc)}`), result key `"indexing": {user_id: {"embedded": n}
  | {"error": ...}}`.
- Phases run as sequential blocks (extraction for every user, THEN indexing for every user);
  `resolve_target_user_ids` is called once when `run_extraction or run_indexing`.
- All phases off → logged no-op returning `{"data": None, "extraction": {}, "indexing": {}}`.
- `memory_indexing` returns the embedded-row count (`int`) instead of `None` (the flow already
  computes it) so the phase can report it.

**2. Coordinator stops indexing.** Delete the trailing `memory_indexing` call, its function-scope
import and the `TAGS_INDEXING` import from `_fan_out_extraction`; update the module docstring
("4. Index ONCE" paragraph), `memory_extract_etl_coordinator`'s and `pipeline.py`'s docstrings.
`FanOutStats`, partitioning, the gather and failure isolation are untouched.
`tree/online.py` is UNTOUCHED (single-doc path keeps its inline `_run_indexing`).

**3. Scripts (glue only) + Make targets:**
- `scripts/run_indexing_pipeline.py` → `dispatch_offline_pipeline(user_id=..., run_data=False,
  run_extraction=False, run_indexing=True)` + `wait_for_dispatch` + `flush_opik()`, same shape as
  `run_memory_pipeline.py`; docstring rewritten (needs served workflows; no in-process path).
- `scripts/run_data_pipeline.py` offline branch passes `run_indexing=False` too (a data-only run
  must not index).
- `run_memory_pipeline.py` / `run_pipeline.py` keep their calls (defaults → extraction + indexing);
  docstrings say "then the indexing phase" instead of "trailing index run".
- `apps/memory/Makefile`: `run-indexing-pipeline` help text → "INDEXING phase only, as ONE
  offline-pipeline run (needs served workflows)"; `run-memory-pipeline` help → "+ the indexing
  phase".
- The nightly cron keeps defaults → data + extraction + indexing (behaviour unchanged).

**4. Docs:** `apps/memory/README.md` ("Serving workflows" paragraph about `memory_indexing`,
"Pipelines at a glance" table, "Memory pipeline" paragraph, "Memory indexing" section),
`docs/notes/prefect-execution-topologies.md` (the `memory_indexing` paragraph),
`docs/notes/deployment-runbook.md` step 3 ("first indexing run" now dispatches `offline-pipeline`,
which `GROUPS=data` already registers), `.agents/skills/run-pipelines-e2e/SKILL.md` step 2.
Glossary edits (**Offline phase**, **Coordinator**) are in the grooming commit.

## Acceptance Criteria

- [x] `inspect.signature(offline_pipeline).parameters` contains `run_indexing` with default `True`, positioned between `run_extraction` and `document_ids`; `dispatch_offline_pipeline` forwards it in the deployment `parameters` dict (`test_offline.py` dispatcher test extended: `parameters["run_indexing"] is False` when passed `False`).
- [x] With the coordinators patched, `offline_pipeline(user_id=U)` awaits `memory_indexing(user_id=U)` exactly once AFTER `memory_extract_etl_coordinator`; the result carries `result["indexing"][str(U)] == {"embedded": <returned int>}`.
- [x] `offline_pipeline(user_id=None)` with two active users awaits `memory_indexing` once per user, after BOTH extraction calls (call-order assertion on a shared mock).
- [x] `offline_pipeline(run_indexing=False)` never awaits `memory_indexing` and returns `"indexing": {}`; `offline_pipeline(run_data=False, run_extraction=False)` still resolves target users and indexes them.
- [x] One user's `memory_indexing` raising `RuntimeError("boom")` is isolated: the other user is still indexed and the result carries `{"error": "boom"}` for the failed one; the flow completes.
- [x] All three flags `False` → no coordinator, no user resolution, no indexing; returns `{"data": None, "extraction": {}, "indexing": {}}` and logs `both phases disabled` → wording updated to `all phases disabled`.
- [x] `grep -n "memory_indexing" apps/memory/src/tree/memory/graph/sharding.py` returns nothing; `test_fanout.py` no longer patches `tree.memory.pipeline.memory_indexing` and all fan-out tests pass (index-ordering tests deleted, not skipped).
- [x] `memory_indexing` returns an `int` equal to `embed_nodes`'s count (unit test with both tasks patched).
- [x] `tests/unit/scripts/test_run_indexing_pipeline.py`: the script calls `dispatch_offline_pipeline` with `run_data=False, run_extraction=False, run_indexing=True` and the resolved `user_id`, then `wait_for_dispatch`; a failed run exits non-zero; `memory_indexing` is NOT imported by the script (`grep -n "memory_indexing" apps/memory/scripts/run_indexing_pipeline.py` empty).
- [x] `tests/unit/scripts/test_run_data_pipeline.py`: the offline branch dispatches with `run_extraction=False` AND `run_indexing=False`.
- [x] `tree/online.py` diff is empty (`git diff --stat apps/memory/src/tree/online.py` shows nothing) and `test_online.py` passes unchanged.
- [x] `orchestrator._DEPLOYMENT_SPECS` still has exactly 5 entries (existing test).
- [x] `grep -rn "trailing index" apps/memory/README.md docs/notes .agents/skills apps/memory/scripts apps/memory/Makefile` returns nothing; README "Pipelines at a glance" lists `index` as a phase of `offline-pipeline`.
- [x] `make memory-format-check && make memory-lint-check && make memory-tests` green; test count does not drop below the post-#113 count minus the deleted index-ordering tests (state the numbers in the log).

## User Stories

### Story: Operator indexes without touching extraction
1. Operator runs `make memory-serve-workflows` in one terminal and `make memory-run-indexing-pipeline USER_IDENTIFIER=paul` in another.
2. The Prefect UI shows ONE `offline-pipeline` run whose only child is a `memory-indexing-etl` subflow (no data, no extraction coordinator).
3. The CLI streams the run's logs and exits 0 with `Indexing pipeline finished for user_id=… Embedded N nodes.` in the output.

### Story: Nightly cron behaves exactly as before
1. The `offline-pipeline` cron fires with `source_files=["sources/listen.yaml"]` and no `user_id`.
2. The run shows, per active user, an extraction coordinator subflow followed by a `memory-indexing-etl` subflow — the same work as yesterday, now as explicit siblings.

### Story: Data-only run never indexes
1. Operator runs `make memory-run-data-pipeline SOURCE_FILE="sources/listen.yaml"`.
2. The flow run contains the data coordinator only; the result JSON shows `"extraction": {}` and `"indexing": {}`.

### Story: One user's index failure does not sink the nightly run
1. The nightly run indexes users A and B; A's `ensure_indexes` raises (mongot down for that tenant's namespace).
2. B is still indexed; the flow result carries `{"error": ...}` under A, and the flow run is Completed, not Failed.

### Story: Reader follows the offline pipeline in code
1. Reader opens `tree/offline.py` and sees three `if run_<phase>:` blocks in a row — data, extraction, indexing — with one docstring paragraph per phase.
2. Reader opens `graph/sharding.py` and finds no indexing at all.

## Out of scope

- The clustering phase, `run_clustering`, and its script (#117).
- Changing `tree/online.py`'s inline indexing.
- Any change to `memory_indexing`'s tasks, index definitions or the dimension gate.
- New Prefect deployments or renames.

---

Blocked by: (none)

## Log

### [SWE] 2026-09-06 18:05 — Implementation

**Files modified**
- `apps/memory/src/tree/offline.py` — `run_indexing: bool = True` phase flag (flow + dispatcher); phase 3 runs `memory_indexing` once per target user under `TAGS_INDEXING` with per-user failure isolation; users resolved once for both per-user phases; result gains `"indexing"`; docstrings rewritten as three phases.
- `apps/memory/src/tree/memory/pipeline.py` — `memory_indexing` returns the embedded-row count (`int`); coordinator/module docstrings no longer claim a trailing index.
- `apps/memory/src/tree/memory/graph/sharding.py` — deleted the trailing `memory_indexing` call, its function-scope import, the `TAGS_INDEXING` import and `prefect.tags`; docstrings updated.
- `apps/memory/scripts/run_indexing_pipeline.py` — now glue: `dispatch_offline_pipeline(run_data=False, run_extraction=False, run_indexing=True)` + `wait_for_dispatch` + `flush_opik()`.
- `apps/memory/scripts/run_data_pipeline.py` — offline branch also passes `run_indexing=False`.
- `apps/memory/scripts/run_memory_pipeline.py`, `run_pipeline.py` — docstrings/help say "the indexing phase".
- `apps/memory/Makefile` — `run-indexing-pipeline` / `run-memory-pipeline` / `run-pipeline` help text.
- `apps/memory/README.md`, `docs/notes/prefect-execution-topologies.md`, `docs/notes/deployment-runbook.md`, `.agents/skills/run-pipelines-e2e/SKILL.md`, `tutorials/2_4_1_serving_locally.md` — indexing documented as an Offline phase; no "trailing index" wording left.
- `apps/memory/src/tree/data/offline_pipeline.py` + `tests/unit/data/test_fanout_data.py` — the data pipeline's "NO trailing index" wording → "NO indexing".
- Tests: `tests/unit/test_offline.py` (+6, incl. the new `TestOfflineIndexingPhase`), `tests/unit/memory/test_pipeline.py` (+2 return-count tests), `tests/unit/scripts/test_run_indexing_pipeline.py` (rewritten to the dispatch contract, +1), `tests/unit/scripts/test_run_data_pipeline.py` (both memory phases off), `tests/unit/memory/graph/test_fanout.py` (index-ordering tests deleted, source guard added), `tests/unit/test_orchestrator.py` (comment accuracy).

**Tests**
- Unit: 2452 passing, 0 failing (`make memory-tests`). Baseline on this branch was 2444; +6 offline, +2 pipeline, +1 script, −1 fan-out (2 index-ordering tests deleted, 1 source guard added) = +8.
- Integration: N/A — the repo has no integration suite; e2e verification is the live Prefect run below.

**Acceptance criteria**
- [x] `run_indexing` in `inspect.signature(offline_pipeline)`, default `True`, between `run_extraction` and `document_ids`; dispatcher forwards it — `test_offline.py::TestOfflineIndexingPhase::test_run_indexing_sits_between_run_extraction_and_document_ids`, `TestDispatchOfflineIngest::test_forwards_the_phase_flags_and_document_ids_to_the_deployment`
- [x] One `memory_indexing(user_id=U)` after extraction, result `{"embedded": n}` — `TestOfflineIndexingPhase::test_indexes_the_target_user_once_after_extraction`
- [x] Two users: index once per user after BOTH extractions (call-order on a shared `MagicMock` manager) — `test_all_users_mode_indexes_every_user_after_all_extractions`
- [x] `run_indexing=False` never indexes / indexing-only run still resolves users — `test_run_indexing_false_never_indexes`, `test_indexing_only_run_still_resolves_target_users`
- [x] Per-user indexing failure isolated (`{"error": "boom"}`) — `test_one_users_indexing_failure_is_isolated`
- [x] All three flags off → logged no-op, `all phases disabled` — `TestOfflinePipelinePhaseFlags::test_all_phases_disabled_is_a_logged_no_op`
- [x] `grep -n "memory_indexing" .../graph/sharding.py` empty; `test_fanout.py` patches it nowhere; index-ordering tests deleted — `test_fanout.py::test_the_coordinator_fan_out_no_longer_indexes`
- [x] `memory_indexing` returns `embed_nodes`' count — `test_pipeline.py::TestIndexingFlowReturnsEmbeddedCount` (2 tests)
- [x] Indexing script dispatches + waits, non-zero on failure, no `memory_indexing` import — `test_run_indexing_pipeline.py` (4 tests, incl. `test_the_script_never_runs_the_flow_in_process`)
- [x] Data script's offline branch passes `run_extraction=False` AND `run_indexing=False` — `test_run_data_pipeline.py::test_offline_dispatches_offline_pipeline_with_memory_phases_off`
- [x] `git diff --stat apps/memory/src/tree/online.py` empty; `test_online.py` passes unchanged (12 tests)
- [x] `_DEPLOYMENT_SPECS` still exactly 5 — `test_orchestrator.py` (27 tests green; live serve listed 5)
- [x] No "trailing index" in README / docs/notes / .agents/skills / scripts / Makefile; README table lists `index` as a phase
- [x] `make memory-format-check && make memory-lint-check && make memory-tests` green

**Evidence**

```
$ make memory-tests
============================ 2452 passed in 23.92s =============================

$ make memory-format-check && make memory-lint-check
266 files already formatted
All checks passed!

$ make pre-commit
prettier / ruff check / ruff format / biome check (harness) .............Passed
```

Live check (Docker `tree-prefect-worker` stopped, `make memory-serve-workflows` from this
worktree — served exactly the 5 deployments):

```
$ make memory-run-indexing-pipeline USER_IDENTIFIER=paul@example.com
Submitted flow run 233b6ab8-411c-4473-9e37-ad16d9556ffc (scheduled); waiting for it...
Beginning flow run 'micro-avocet' for flow 'offline-pipeline'
Finished in state Completed()   → exit 0

parent params: {'run_data': False, 'run_extraction': False, 'run_indexing': True, ...}
  child: memory-indexing-etl | premium-rottweiler | COMPLETED | tags: ['memory-pipeline']
  (ONE child: no data coordinator, no extraction coordinator)

$ make memory-run-memory-pipeline USER_IDENTIFIER=paul@example.com   # phases 2+3
parent params: {'run_data': False, 'run_extraction': True, 'run_indexing': True, ...}
  14:48:47 | memory-extract-etl-coordinator | COMPLETED | tags=['offline', 'memory-pipeline']
  14:54:30 | memory-indexing-etl            | COMPLETED | tags=['memory-pipeline']
  memory-extract-etl-coordinator -> children: ['memory-extract-etl-worker']   (no indexing child)
```

Docker worker restarted afterwards; the local serve process was stopped.

**Notes**
- **User Story 1, step 3 deviation.** The CLI streams only the PARENT flow run's logs
  (`wait_for_flow_run` filters on `flow_run_id`), and `memory_indexing`'s
  `"Indexing pipeline finished for user_id=… Embedded N nodes."` line goes through the module
  logger inside the SUBFLOW run — so it now lands in the serving worker's output / the Prefect UI,
  not in the CLI. Exit code is 0 and the count is in the flow-run result (`{"embedded": n}`). Making
  that exact line appear in the CLI would need either a `print` (banned by CLAUDE.md) or streaming
  subflow logs in `tree.cli` — both out of scope, so I did not force it. Flagging for PA/Tester.
- Judgement call: the indexing phase does NOT forward `opik_trace_headers` (there is nothing to
  forward — `offline_pipeline` runs the subflow in-process under its own span, exactly as it
  already calls the two coordinators; nesting is by contextvars).
- Judgement call (small scope stretch): also fixed the now-false "memory indexing does the same
  within extraction" sentence in `tutorials/2_4_1_serving_locally.md` and the data pipeline's
  "NO trailing index" wording (`data/offline_pipeline.py` log line + its test docstring), so no
  stale claim survives the change.
- Per-user phase loops are O(users) sequential — deliberate, mirroring the existing extraction
  phase (Prefect admission slots are the bottleneck, not the loop).

### [Tester] 2026-09-06 15:20 — QA

**Test summary**
- Format / lint / pre-commit: PASS (`make memory-format-check`, `make memory-lint-check`, `make pre-commit` all green)
- Unit tests: 2452 passed / 0 failed (`make memory-tests`)
- Integration tests: N/A (repo has no integration suite; e2e verification below)
- Warnings: 1 pre-existing third-party `UserWarning` (`opik`'s Pydantic v1 shim on Python 3.14) at collection time; not test-level, not introduced by this diff

**E2E adversarial pass** (from THIS worktree; Docker `tree-prefect-worker` stopped, `make memory-serve-workflows` served the 5 core deployments; worker restarted afterward)
- Happy path — indexing-only run: `make memory-run-indexing-pipeline USER_ID=6a8ea9579a7aeb13175955c8` → flow run `weightless-rat` Completed, exit 0, parent params `run_data=False, run_extraction=False, run_indexing=True`, exactly ONE direct child flow run (`memory-indexing-etl`, COMPLETED) — PASS
- Break path 1 (state: data-only run must not cascade): `make memory-run-data-pipeline USER_ID=6a8ea9579a7aeb13175955c8 URI="https://example.com"` → parent params `run_data=True, run_extraction=False, run_indexing=False`; exactly ONE child (`data-etl-coordinator`), no extraction/indexing anywhere — PASS
- Break path 2 (state: extraction-only via raw `prefect deployment run`): `prefect deployment run offline-pipeline/offline-pipeline -p run_data=false -p run_extraction=true -p run_indexing=false -p user_id=6a8ea9579a7aeb13175955c8` → Completed; ONE direct child (`memory-extract-etl-coordinator`) whose ONLY grandchild is `memory-extract-etl-worker` — no `memory-indexing-etl` anywhere, neither sibling nor nested under the coordinator — PASS
- Break path 3 (nightly-cron params, cheap): `prefect deployment run offline-pipeline/offline-pipeline -p run_data=false -p 'source_files=["sources/listen.yaml"]'` → Completed with 0 children, because this DB replica has no `person:self` node with `properties.is_active_user=True` for the one seeded user (`select_active_user_ids` — untouched, pre-existing logic in `data/offline_pipeline.py`), so `resolve_target_user_ids(None)` correctly resolves an empty active-user set. Direct DB mutation to fabricate an active-user fixture was blocked by the sandbox's write-classifier, so the "extraction then indexing per active user, sequential blocks" behavior for `user_id=None` is verified via `tests/unit/test_offline.py::TestOfflineIndexingPhase::test_all_users_mode_indexes_every_user_after_all_extractions` instead (`uv run pytest -k TestOfflineIndexingPhase` → 6/6 passed), which pins call order across two mocked users on a shared `MagicMock` manager (`["extract", "extract", "index", "index"]`) — PASS by code path + unit evidence
- Break path 4 (unchanged path, regression check): `tree/online.py`'s single-document indexing — `git diff --stat apps/memory/src/tree/online.py` empty, `grep -n "memory_indexing\|_run_indexing" apps/memory/src/tree/online.py` shows the inline call at line 162 unchanged, `test_online.py` 12/12 passing in the full suite run — PASS

**Acceptance criteria** (all 14 checkboxes verified with evidence; task file already carried `[x]` from the SWE's self-check — confirmed correct, none reverted)
- [x] PASS — `run_indexing` signature position + default, dispatcher forwarding — `test_offline.py::TestOfflineIndexingPhase::test_run_indexing_sits_between_run_extraction_and_document_ids` (asserts `inspect.signature` ordering + default `True`) and `TestDispatchOfflineIngest` (asserts `parameters["run_indexing"] is False`)
- [x] PASS — one `memory_indexing(user_id=U)` after extraction, `{"embedded": n}` — `test_indexes_the_target_user_once_after_extraction` (call-order via `MagicMock` manager `["extract", "index"]`)
- [x] PASS — two users, index once per user after BOTH extractions — `test_all_users_mode_indexes_every_user_after_all_extractions` (`["extract", "extract", "index", "index"]`)
- [x] PASS — `run_indexing=False` never indexes; `run_data=False, run_extraction=False` still resolves + indexes — `test_run_indexing_false_never_indexes`, `test_indexing_only_run_still_resolves_target_users`; live-confirmed in the happy path above
- [x] PASS — per-user indexing failure isolated, `{"error": "boom"}`, flow completes — `test_one_users_indexing_failure_is_isolated`
- [x] PASS — all three flags off → no-op, `all phases disabled` wording — `TestOfflinePipelinePhaseFlags::test_all_phases_disabled_is_a_logged_no_op`
- [x] PASS — `grep -n "memory_indexing" apps/memory/src/tree/memory/graph/sharding.py` → empty (exit 1); `test_fanout.py` has no `fake_indexing` fixture / no patch of `tree.memory.pipeline.memory_indexing` anywhere; `test_the_coordinator_fan_out_no_longer_indexes` asserts `"memory_indexing" not in inspect.getsource(sharding)`; all 33 `test_fanout.py` tests pass
- [x] PASS — `memory_indexing` returns `int` = `embed_nodes`'s count — `test_pipeline.py::TestIndexingFlowReturnsEmbeddedCount` (2 tests: returns 42, returns 0)
- [x] PASS — script dispatches with the 3 phase flags + resolved `user_id`, then `wait_for_dispatch`; non-zero exit on failure; no `memory_indexing` import — `test_run_indexing_pipeline.py` (4 tests incl. `test_the_script_never_runs_the_flow_in_process`, asserting `"memory_indexing" not in inspect.getsource(cli_module)`); `grep -n "memory_indexing" apps/memory/scripts/run_indexing_pipeline.py` → empty
- [x] PASS — `run_data_pipeline.py` offline branch dispatches `run_extraction=False, run_indexing=False` — `test_offline_dispatches_offline_pipeline_with_memory_phases_off`; read `scripts/run_data_pipeline.py:76-79`
- [x] PASS — `git diff --stat apps/memory/src/tree/online.py` → empty; `test_online.py` 12/12 pass
- [x] PASS — `orchestrator._DEPLOYMENT_SPECS` still 5 — `test_orchestrator.py` 27/27 pass; live serve listed exactly 5 deployments (`data-etl-worker`, `memory-extract-etl-worker`, `online-pipeline`, `offline-pipeline`, `dream-consolidation-all-users`)
- [x] PASS — `grep -rn "trailing index" apps/memory/README.md docs/notes .agents/skills apps/memory/scripts apps/memory/Makefile` → empty (exit 1); README "Pipelines at a glance" table lists `index` phase for `run-memory-pipeline` / `run-indexing-pipeline` rows
- [x] PASS — `make memory-format-check && make memory-lint-check && make memory-tests` green, 2452 passed (baseline 2444 per SWE report; net +8 matches the stated +6/+2/+1/−1 breakdown)

**Evidence**
```
$ make memory-tests
============================ 2452 passed in 21.96s =============================

$ grep -n "memory_indexing" apps/memory/src/tree/memory/graph/sharding.py ; echo "exit=$?"
exit=1
$ grep -n "memory_indexing" apps/memory/scripts/run_indexing_pipeline.py ; echo "exit=$?"
exit=1
$ grep -rn "trailing index" apps/memory/README.md docs/notes .agents/skills apps/memory/scripts apps/memory/Makefile ; echo "exit=$?"
exit=1
$ git diff --stat apps/memory/src/tree/online.py
(empty)

# Live: indexing-only run
$ make memory-run-indexing-pipeline USER_ID=6a8ea9579a7aeb13175955c8
... Beginning flow run 'weightless-rat' for flow 'offline-pipeline' ... Finished in state Completed()
parent params: {'run_data': False, 'run_extraction': False, 'run_indexing': True, ...}
1 direct child: memory-indexing-etl (COMPLETED)

# Live: extraction-only via raw prefect deployment run
$ prefect deployment run offline-pipeline/offline-pipeline -p run_data=false -p run_extraction=true -p run_indexing=false -p user_id=...
... Completed
1 direct child: memory-extract-etl-coordinator (COMPLETED) -> 1 grandchild: memory-extract-etl-worker (no indexing anywhere)
```

**Other issues found**
- User Story 1 step 3 ("CLI streams ... `Indexing pipeline finished ... Embedded N nodes.` in the output") is not literally met: `tree.cli.wait_for_flow_run` filters logs by the PARENT `flow_run_id` only, so the `memory_indexing` subflow's log line never reaches the CLI stream (confirmed live: the indexing-only run's CLI output ends at "Finished in state Completed()" / "Done. Flow completed successfully.", no embedded-count line). Confirmed this is NOT indexing-specific: it is the pre-existing, unchanged behavior every other dispatched script already has (`run_data_pipeline.py`, `run_memory_pipeline.py`, `run_pipeline.py` — their coordinators' completion logs are equally invisible to the CLI, since those too log from inside a subflow). Before this task, `run_indexing_pipeline.py` was the one exception that ran `memory_indexing` in-process, so its log line printed directly to the terminal; ADR-007 Decision 5 intentionally makes it consistent with every other script's dispatch-and-wait shape, at the cost of that one line. Judgement: PASS with note, not a defect — recommend a small follow-up to either amend the User Story wording or (separately) teach `wait_for_flow_run` to include subflow logs across all scripts, rather than special-casing indexing.
- `memory_indexing`'s `opik_trace_headers` is not forwarded by the offline-pipeline's indexing phase (SWE's disclosed judgement call) — consistent with how the coordinators are already called (in-process, same-span nesting via contextvars); not a regression, out of scope for this task's ACs.
- The scope stretch into `tutorials/2_4_1_serving_locally.md` and `data/offline_pipeline.py` wording is accurate and net-positive (removes now-false claims); reviewed both diffs, no issues.

**VERDICT: PASS**

### [PA] 2026-09-06 22:32 — Acceptance Review

**VERDICT: REJECT** (feature-level, PR #42)

Reviewed from the operator's POV: the three explicit phases, the nightly-cron defaults (indexing on, clustering off), the data-only run, per-user isolation, the docs ("trailing index" gone, phases table) — all right. One issue is carried into the rollup: User Story 1 step 3 (`Embedded N nodes` in the CLI stream) is not met, and the same gap makes a skipped clustering run look like a success from the terminal. Filed rollup task `tasks/120-pa-rejection-embedding-clusters-viz.md` (Issue 3 is this task's). Pipeline re-runs from the inner loop on the rollup; on green, re-run acceptance on this task.

### [PA] 2026-09-06 23:16 — Acceptance Review (round 2)

**VERDICT: ACCEPT**

Round-2 re-review after rollup `tasks/done/120`: User Story 1 step 3 now met — the parent-level `indexing: user_id=… embedded=N` line reaches the CLI stream (Tester's live terminal evidence); phase semantics and nightly defaults unchanged. Hand off to the PR Reviewer.
