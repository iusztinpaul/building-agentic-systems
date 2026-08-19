---
id: 098-offline-pipeline-phase-flags-and-doc-ids
feature: free-tier-deployments
status: done
---

# Add phase flags (`run_data`, `run_extraction`) and `document_ids` to `offline_pipeline`

Tags: `data`, `memory`, `infra`
Depends on: None
Blocks: #099
Implements: ADR-002 amendment (`free-tier-deployments`)

## Scope

Purely ADDITIVE change to `apps/memory/src/tree/offline.py` — with all defaults, behavior is
byte-identical to today. Groundwork that lets the step CLIs (#099) and the nightly cron (#100)
funnel through ONE flow.

**`offline_pipeline` flow — new parameters** (mirroring `online_pipeline`'s existing
`run_extraction` idiom in `src/tree/online.py`):

- `run_data: bool = True` — when False, SKIP the `data_etl_coordinator` inline subflow entirely;
  the result carries `"data": None`.
- `run_extraction: bool = True` — when False, SKIP the per-user `memory_extract_etl_coordinator`
  loop (and its target-user resolution); the result carries `"extraction": {}`.
- `document_ids: list[str] | None = None` — forwarded to EACH user's extraction coordinator call:
  `memory_extract_etl_coordinator(uid, document_ids=document_ids, num_shards=num_shards)` (the
  coordinator already accepts it — `src/tree/memory/extraction/pipeline.py:1633`). Preserves
  `DOC_IDS=` narrowing and the `run_memory_pipeline.py --mode online` path through this flow.
- Guard: `if document_ids and user_id is None: raise ValueError("document_ids is single-tenant —
  pass user_id too.")` — an explicit doc-id list fanned across ALL active users would extract
  other tenants' ids or fail confusingly.
- Both flags False → a LOGGED no-op, not an error: one INFO log stating both phases are disabled,
  return `{"data": None, "extraction": {}}` (a Completed flow run).

**`dispatch_offline_pipeline` — passthrough:**

- Same three parameters added to the signature; forwarded in the deployment `parameters` dict AND
  in the in-process fallback call.
- The `document_ids`-without-`user_id` guard also fires HERE, BEFORE `run_deployment` — dispatch is
  fire-and-forget, so edge validation is the only way the caller sees the error synchronously (same
  rationale as `validate_online_source`). Implementation shape (shared helper vs. repeated 3-line
  check) is the SWE's call.

**Docstrings:** update the module docstring, `offline_pipeline`, and `dispatch_offline_pipeline` in
`offline.py` — they currently describe an unconditional two-phase chain. Do NOT yet claim the
deployment is core / always registered (that happens in #100); keep the fallback description accurate.

**Tests** (`tests/unit/test_offline.py`, per `/squid-testing-python`): update existing assertions to
the new call/parameter shapes (e.g. `extract.assert_awaited_once_with(_USER_ID, document_ids=None,
num_shards=2)`; the dispatch parameters-dict assertions gain the three new keys), plus the new cases
in the acceptance criteria.

## Acceptance criteria

- [x] With no new arguments passed, `offline_pipeline` and `dispatch_offline_pipeline` behave exactly
      as before: existing `test_offline.py` cases pass with only call-shape updates (no semantic
      changes to what they assert).
- [x] `run_data=False` skips `data_etl_coordinator` (assert not awaited), still runs per-user
      extraction, and returns `"data": None` — new test.
- [x] `run_extraction=False` runs the data phase, never awaits `memory_extract_etl_coordinator` nor
      `resolve_target_user_ids`, and returns `"extraction": {}` — new test.
- [x] `run_data=False, run_extraction=False` returns `{"data": None, "extraction": {}}` without
      awaiting either coordinator and emits one INFO no-op log (assert via `caplog`) — new test.
- [x] `document_ids=["<id>"]` with `user_id` set is forwarded verbatim to every
      `memory_extract_etl_coordinator` call — new test.
- [x] `document_ids` without `user_id` raises `ValueError` from `offline_pipeline`, AND from
      `dispatch_offline_pipeline` BEFORE `run_deployment` is awaited (assert the mock was not
      called) — new tests.
- [x] `dispatch_offline_pipeline` includes `run_data` / `run_extraction` / `document_ids` in the
      deployment `parameters` dict and forwards them in the in-process fallback call — extend the
      two existing dispatch tests.
- [x] All function signatures fully typed, including returns (`CLAUDE.md` rule).
- [x] `make memory-tests` green; `make memory-format-check && make memory-lint-check &&
      make pre-commit` clean.

## User stories

### Story: Operator narrows an offline run to one document set
1. Operator (or a script) calls `await dispatch_offline_pipeline(user_id=<oid>,
   document_ids=["68a1..."], run_data=False)`.
2. The `offline-pipeline` deployment is submitted with parameters `{"user_id": "<oid>",
   "document_ids": ["68a1..."], "run_data": False, "run_extraction": True, ...}`.
3. The flow run skips ingestion and extracts exactly that document for exactly that user, then the
   coordinator fires the trailing index run.

### Story: Operator forgets the tenant on a narrowed run
1. Operator calls `dispatch_offline_pipeline(document_ids=["68a1..."])` with no `user_id`.
2. The call raises `ValueError: document_ids is single-tenant — pass user_id too.` synchronously —
   no flow run is ever created.

### Story: A misconfigured caller disables both phases
1. A caller dispatches with `run_data=False, run_extraction=False`.
2. The flow run completes successfully, logs that both phases are disabled, and returns
   `{"data": None, "extraction": {}}` — no crash, no silent hang.

## Out of scope

- Any change to the scripts (#099), the orchestrator specs (#100), or docs (#101).
- Any change to `memory_extract_etl_coordinator` / `data_etl_coordinator` themselves.

## Log

### [SWE] 2026-08-19 — Implementation (production code only)

**Files modified**
- `apps/memory/src/tree/offline.py` — added `run_data` / `run_extraction` / `document_ids` to
  `offline_pipeline` + `dispatch_offline_pipeline`, the shared
  `_validate_document_ids_scope` edge guard, the both-phases-off INFO no-op, and refreshed
  the module / flow / dispatcher docstrings.

**Implementation notes**
- `run_data=False` → `data_etl_coordinator` never awaited, result carries `"data": None`.
- `run_extraction=False` → neither `resolve_target_user_ids` nor
  `memory_extract_etl_coordinator` awaited, result carries `"extraction": {}`.
- Both false → one INFO log (`offline-pipeline: both phases disabled ... nothing to do`) and
  `{"data": None, "extraction": {}}` returned BEFORE `configure_opik()`, so it is a clean
  Completed run with no telemetry side effects.
- `document_ids` forwarded verbatim as
  `memory_extract_etl_coordinator(uid, document_ids=document_ids, num_shards=num_shards)`.
- Guard shared by both entry points via `_validate_document_ids_scope`, called in
  `dispatch_offline_pipeline` BEFORE `run_deployment`.
- Dispatch `parameters` dict and the in-process fallback call both carry the three new keys.

**Tests**
- NOT RUN — this run was scoped to production code only (no test-suite execution, no test
  authoring). `tests/unit/test_offline.py` still needs the call-shape updates and the new
  cases listed in the acceptance criteria:
  `extract.assert_awaited_once_with(_USER_ID, document_ids=None, num_shards=2)`, the two
  dispatch `parameters` / fallback-kwargs assertions, and the six new cases.

**QA**
- `make memory-format-fix && make memory-lint-fix && make memory-lint-check &&
  make memory-format-check` — all clean.

### [SWE] 2026-08-19 — Test catch-up + full-suite verification

**Files modified**
- `apps/memory/tests/unit/test_offline.py` — brought up to the shipped #098 surface: existing
  call-shape assertions updated, plus the new phase-flag / `document_ids` / guard cases.

**What changed in the tests**
- Stale assertions fixed: `extract.assert_awaited_once_with(_USER_ID, document_ids=None,
  num_shards=2)`; the dispatch `parameters` dict and the in-process fallback kwargs now carry
  `run_data` / `run_extraction` / `document_ids`.
- New `TestOfflinePipelinePhaseFlags` (5 cases): `run_data=False` skips `data_etl_coordinator`
  and yields `"data": None`; `run_extraction=False` skips BOTH `resolve_target_user_ids` and
  `memory_extract_etl_coordinator` and yields `"extraction": {}`; both flags False is a single
  INFO no-op log (asserted via `caplog` on the `tree.offline` logger) with no coordinator awaited;
  `document_ids` forwarded verbatim to every per-user call; `document_ids` without `user_id`
  raises `ValueError` before any phase runs.
- New dispatcher cases (3): non-default flags + `document_ids` survive into the deployment
  `parameters` dict; the same three survive into the in-process fallback call; `document_ids`
  without `user_id` raises BEFORE `run_deployment` (asserted `assert_not_awaited` on both the
  deployment mock and the flow mock).

**Tests**
- Unit: 1917 passing, 0 failing — `make memory-tests` (full suite, `make env-status` → local).
  `tests/unit/test_offline.py` alone: 13 passed.
- Integration: N/A — the repo has no integration suite by design.

**Evidence**
```
$ make env-status
Env target: local (.env)

$ make memory-tests
tests/unit/test_offline.py .............                                 [ 97%]
...
============================ 1917 passed in 16.02s =============================

$ make memory-format-fix && make memory-lint-fix && make memory-format-check && make memory-lint-check
245 files already formatted
All checks passed!

$ make pre-commit
prettier.................................................................Passed
ruff check...............................................................Passed
ruff format..............................................................Passed
biome check (harness)....................................................Passed
```

**Notes**
- No production code touched in this pass — the #098 implementation matched its spec exactly; the
  3 failures were purely stale test assertions. No production bug found.
- The #099 / #100 test files (`tests/unit/scripts/test_run_data_pipeline.py`,
  `test_run_memory_pipeline.py`, `test_orchestrator.py`, `test_observability_tags.py`) were never
  run by those tasks but pass as written — no fixes needed there.
- Uncommitted, per instruction; changes sit in the working tree on `feat/free-tier-deployments`.
