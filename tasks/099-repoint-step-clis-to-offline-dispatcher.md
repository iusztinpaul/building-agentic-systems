---
id: 099-repoint-step-clis-to-offline-dispatcher
feature: free-tier-deployments
status: done
---

# Repoint `run_data_pipeline` / `run_memory_pipeline` to `dispatch_offline_pipeline`

Tags: `cli`, `infra`
Depends on: #098
Blocks: #100
Implements: ADR-002 amendment (`free-tier-deployments`)

## Scope

The two step CLIs stop triggering the coordinator DEPLOYMENTS by name and funnel through
`dispatch_offline_pipeline` + `wait_for_dispatch`, exactly like `scripts/run_pipeline.py` already
does. This MUST land before #100 — after the coordinator deployments are dropped, the old
`trigger_deployment` calls would hard-fail on `read_deployment_by_name`.

**`apps/memory/scripts/run_data_pipeline.py`** — `_run_offline`:

- Replace `trigger_deployment("data-etl-coordinator/data-etl-coordinator", …)` +
  `wait_for_flow_run(...)` (line ~78) with `dispatch_offline_pipeline(user_id=resolved_user_id,
  source_files=source_files or None, sources=inline_sources or None, run_extraction=False)`
  followed by `await wait_for_dispatch(result)`.
- Add a `flush_opik()` before returning (the in-process fallback's spans belong to this short-lived
  process — mirror `_run_online` in the same file).
- Source-selector semantics unchanged: forward only what the operator passed; neither → the
  coordinator's default backfill+listen set (it owns resolution).

**`apps/memory/scripts/run_memory_pipeline.py`** — `_run`:

- Replace the `DEPLOYMENT_NAME` trigger (line ~66) with `dispatch_offline_pipeline(
  user_id=resolved_user_id, document_ids=document_ids, num_shards=num_shards if num_shards is not
  None else 1, run_data=False)` + `await wait_for_dispatch(result)` + `flush_opik()`. Delete the
  `DEPLOYMENT_NAME` constant. (`connect_and_resolve_user` always yields a `user_id`, so the
  `document_ids` guard is always satisfied.)

**`apps/memory/src/tree/cli.py`:**

- KEEP `trigger_deployment` — `scripts/run_indexing_pipeline.py:44` still uses it for
  `memory-indexing-etl` (which stays a core deployment). Do NOT delete it.
- Update the module docstring's mention of "triggering a core deployment by name" only if the
  wording becomes inaccurate; no code change.

**Docstrings:** rewrite both scripts' module docstrings — they currently document
coordinator-deployment dispatch. New wording: both modes of both scripts dispatch through the
`offline-pipeline` / `online-pipeline` glue flows with the relevant phase disabled; the dispatcher
falls back in-process when the deployment isn't registered.

**Interim behavior (accepted, note in the commit message):** until #100 lands, `offline-pipeline` is
not registered under the default `deploy_optional: false`, so these commands run via the
dispatcher's designed in-process fallback — every command keeps working; the tree is never broken.

**Tests** (`tests/unit/scripts/test_run_data_pipeline.py`, `tests/unit/scripts/test_run_memory_pipeline.py`,
per `/squid-testing-python`): the existing Click-surface tests mock `_run_offline` / `_run` and stay
valid; ADD wiring tests that mock `dispatch_offline_pipeline` + `wait_for_dispatch` and assert the
forwarded kwargs.

## Acceptance criteria

- [x] `grep -rn "data-etl-coordinator/data-etl-coordinator\|memory-extract-etl-coordinator/memory-extract-etl-coordinator" apps/memory/scripts/`
      returns nothing — no script triggers a coordinator deployment by name.
- [x] `trigger_deployment` still exists in `src/tree/cli.py` and `scripts/run_indexing_pipeline.py`
      still works through it (its existing tests stay green).
- [x] New test: `run_data_pipeline` offline mode calls `dispatch_offline_pipeline` with
      `run_extraction=False`, forwards `source_files`/`sources` (None when absent), then awaits
      `wait_for_dispatch` — asserted on mocks.
- [x] New test: `run_memory_pipeline` calls `dispatch_offline_pipeline` with `run_data=False`,
      forwards parsed `document_ids` and `num_shards`, then awaits `wait_for_dispatch`.
- [x] Existing operator surfaces are UNCHANGED: all current Click-validation tests in both script
      test files pass unmodified (flags, modes, error messages, `--doc-ids` parsing, the
      `huggingface_dataset` fast-fail).
- [ ] With the local stack up and workflows served (old topology — #100 not yet landed),
      `make memory-run-data-pipeline SOURCE_FILE="sources/listen.yaml"` completes via the in-process
      fallback (log line "running the flow in-process") and documents land — evidence in Log.
- [x] `make memory-tests` green; format/lint/pre-commit clean.

## User stories

### Story: Operator runs the data step exactly as before
1. Operator runs `make memory-run-data-pipeline SOURCE_FILE="sources/listen.yaml"`.
2. The CLI resolves the current-session user, dispatches `offline-pipeline` with
   `run_extraction=False`, and blocks streaming logs to the terminal.
3. Documents land in `documents`; no extraction and no indexing occur — identical operator-visible
   outcome to the old coordinator trigger.

### Story: Operator extracts one freshly ingested document
1. Operator runs `make memory-run-data-pipeline MODE=online SOURCE="https://…"` and copies the
   printed document id.
2. Operator runs `make memory-run-memory-pipeline MODE=online DOC_IDS="<id>"`.
3. The CLI dispatches `offline-pipeline` with `run_data=False, document_ids=["<id>"]`; extraction
   runs for exactly that document and the trailing index run fires; the command exits 0.

### Story: Operator still runs standalone indexing
1. Operator runs `make memory-run-indexing-pipeline`.
2. The script triggers the `memory-indexing-etl` deployment via the retained `trigger_deployment`
   and streams logs — untouched by this feature.

## Out of scope

- The orchestrator spec change (#100) — the coordinator deployments still exist after this task.
- `scripts/run_pipeline.py` (already on the dispatcher) and `scripts/run_indexing_pipeline.py`
  (stays on `trigger_deployment`).
- Makefile changes (targets call the same scripts with the same flags).

## Log

### [SWE] 2026-08-19 — Implementation

**Files modified**
- `apps/memory/scripts/run_data_pipeline.py` — `_run_offline` now dispatches `dispatch_offline_pipeline(run_extraction=False)` + `wait_for_dispatch` + `flush_opik`; docstring rewritten; dropped the `trigger_deployment`/`wait_for_flow_run` imports.
- `apps/memory/scripts/run_memory_pipeline.py` — `_run` now dispatches `dispatch_offline_pipeline(run_data=False, document_ids=..., num_shards=...)` + `wait_for_dispatch` + `flush_opik`; `DEPLOYMENT_NAME` deleted; docstring rewritten.
- `apps/memory/tests/unit/scripts/test_run_data_pipeline.py` — added `TestRunDataPipelineOfflineDispatch` (3 wiring tests) + `cli_module`/dispatcher/opik fixtures.
- `apps/memory/tests/unit/scripts/test_run_memory_pipeline.py` — added `TestRunMemoryPipelineDispatch` (2 wiring tests) + the same fixture set.
- `apps/memory/src/tree/cli.py` — UNCHANGED: `trigger_deployment` retained for `run_indexing_pipeline.py`; the docstring wording stays accurate.

**Tests**
- NOT RUN — this run was scoped to production code + the ACs' wiring tests; the suite is run in the follow-up verification pass.

**Acceptance criteria**
- [x] No script triggers a coordinator deployment by name — `grep -rn "data-etl-coordinator/data-etl-coordinator\|memory-extract-etl-coordinator/memory-extract-etl-coordinator" apps/memory/scripts/` exits 1 (no match).
- [x] `trigger_deployment` still in `src/tree/cli.py:111` and still used by `scripts/run_indexing_pipeline.py:44`.
- [x] New data-pipeline wiring tests assert `run_extraction=False`, `source_files`/`sources` forwarded (None when absent), and the awaited `wait_for_dispatch`.
- [x] New memory-pipeline wiring tests assert `run_data=False`, forwarded `document_ids`/`num_shards` (omitted → 1), and the awaited `wait_for_dispatch`.
- [x] Existing Click-surface tests untouched (only fixtures added; `cli_main` now derives from a new `cli_module` fixture).
- [ ] E2E `make memory-run-data-pipeline SOURCE_FILE="sources/listen.yaml"` via the in-process fallback — NOT RUN (out of scope for this run).
- [ ] `make memory-tests` green — NOT RUN; format/lint clean (`make memory-format-fix`, `memory-lint-fix`, `memory-lint-check`, `memory-format-check` all pass).

**Notes**
- Interim behavior as designed: until #100 lands, `offline-pipeline` isn't registered, so both commands take the dispatcher's in-process fallback ("running the flow in-process") — mention in the commit message.
- Uncommitted; #098's changes are also still in the working tree.

### [SWE] 2026-08-19 — Suite verification (no code change)

Ran the deferred verification pass while catching #098's tests up. The wiring tests added by this
task pass as written — no fixes were needed in
`tests/unit/scripts/test_run_data_pipeline.py` (11 passed) or
`tests/unit/scripts/test_run_memory_pipeline.py` (7 passed).

- `make memory-tests` (full suite, env target local): **1917 passed, 0 failed**.
- `make memory-format-check && make memory-lint-check && make pre-commit`: clean.

The live E2E acceptance box stays UNTICKED — that run is out of scope here and belongs to #102.
