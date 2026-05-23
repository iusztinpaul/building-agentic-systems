# 065 — Fix `serve()` runner-limit kwarg name (`global_limit` → `limit`)

## Scope

`apps/memory/src/tree/orchestrator.py` calls
`serve(..., global_limit=app_config.concurrency.runner_global_limit)`. In the
installed **Prefect 3.6.19**, `prefect.serve` has the signature
`(*args, pause_on_shutdown=True, print_starting_message=True, limit: Optional[int]=None, **kwargs)`.
The admission-control parameter is **`limit`**, not `global_limit`; `global_limit`
falls through `**kwargs` into `Runner.__init__`, which rejects it:

```
TypeError: Runner.__init__() got an unexpected keyword argument 'global_limit'
```

`make memory-serve-workflows` crashes immediately on startup. This shipped green
because CI never starts serve-workflows and the fan-out tests mock
`run_deployment` (they never call `serve()`).

Fix scope (minimal):
1. Extract a module-level, testable helper `serve_deployments(limit: int) -> None`
   that builds the existing `RunnerDeployment` list (including the dream cron
   deployment) and calls `serve(*deployments, limit=limit)`.
2. `if __name__ == "__main__":` calls
   `serve_deployments(app_config.concurrency.runner_global_limit)`.
3. Change ONLY the runner-limit kwarg name (`global_limit` → `limit`) and perform
   the extraction. Keep every deployment registration and the dream `cron=`
   exactly as-is.

## Acceptance Criteria

- [x] `serve_deployments(limit)` is a module-level helper (importable, testable
      without `__main__`). Verified by import smoke + `test_orchestrator.py`.
- [x] The `serve(...)` call uses `limit=`, not `global_limit=`.
- [x] All existing deployment registrations preserved verbatim, including the
      dream `cron=app_config.dream.cron`. Verified by
      `test_serve_deployments_registers_all_deployments`.
- [x] Regression test in `apps/memory/tests/unit/test_orchestrator.py` patches
      `serve` with a spy, calls `serve_deployments(limit=4)`, and asserts the spy
      received `limit=4` and that `"global_limit"` is NOT among the kwargs.
      Test failed red on the old `global_limit` code; passes green after the fix.
- [x] `make memory-serve-workflows` reaches "polling for scheduled runs" without
      the `TypeError` (smoke check).
- [x] Format/lint/pre-commit clean; unit + fast integration tests pass.

## Log

### [SWE] 2026-05-23 16:25 — Implementation

**Files modified**
- `apps/memory/src/tree/orchestrator.py` — extracted module-level
  `serve_deployments(limit: int) -> None` helper from `__main__`; changed the
  runner-limit kwarg from `global_limit=...` to `limit=...` (the real param name
  in Prefect 3.6.19); `__main__` now calls
  `serve_deployments(app_config.concurrency.runner_global_limit)`. All 8
  deployment registrations + dream `cron=` preserved verbatim.
- `apps/memory/tests/unit/test_orchestrator.py` — new regression test file
  guarding the `serve(...)` call contract (kwarg named `limit`, no `global_limit`,
  binds to real `inspect.signature(prefect.serve)`, all deployments registered).

**Tests**
- Unit: 1416 passing, 0 failing (`make memory-unit-tests`) — includes the 3 new
  `test_orchestrator.py` tests.
- Integration (fast, excludes slow): 153 passed, 1 skipped, 115 deselected
  (`make memory-integration-tests`) on a quiesced shared stack. N/A as direct
  coverage of this change (no infra behavior changed) — run as a no-regression
  guard.

**Regression test red → green evidence**
- RED (pre-fix, `global_limit=limit` in the helper):
  ```
  FAILED tests/unit/test_orchestrator.py::test_serve_deployments_passes_limit_not_global_limit
    AssertionError: assert None == 4
      where None = {'global_limit': 4}.get('limit')
  FAILED tests/unit/test_orchestrator.py::test_serve_deployments_kwargs_bind_to_real_serve_signature
    KeyError: 'limit'
  ======================= 2 failed, 1414 passed in 44.47s =======================
  ```
- GREEN (post-fix, `limit=limit`):
  ```
  tests/unit/test_orchestrator.py ...                                      [ 99%]
  ============================ 1416 passed in 47.69s =============================
  ```

**Smoke check — serve starts clean**
```
$ make memory-serve-workflows        # ran ~15s, then killed only this process tree (pids 2336/2340/2346/2347)
uv run python -m tree.orchestrator
Your deployments are being served and polling for scheduled runs!

Deployments
┌─────────────────────────────────────────────────────────────────────┐
│ data-pipeline-etl/data-pipeline-etl                                 │
│ memory-extraction-etl/memory-extraction-etl                         │
│ memory-indexing-etl/memory-indexing-etl                             │
│ ingest-file-etl/ingest-file-etl                                     │
│ ingest-conversation-etl/ingest-conversation-etl                     │
│ ingest-youtube-video-batch-etl/ingest-youtube-video-batch-etl       │
│ ingest-youtube-rss-feed-batch-etl/ingest-youtube-rss-feed-batch-etl │
│ dream-consolidation-all-users/dream-consolidation-etl               │
└─────────────────────────────────────────────────────────────────────┘
```
No `TypeError: Runner.__init__() got an unexpected keyword argument 'global_limit'`.
Confirmed the serve process I started was killed afterward (`none (clean)`); no
stale stack-wide runner left behind.

**QA loop**
- `make memory-format-fix && make memory-lint-fix` → all checks passed
  (1 file reformatted: the new test).
- `make memory-format-check` → 265 files already formatted.
- `make memory-lint-check` → All checks passed.
- `make pre-commit` → all hooks Passed.

**Acceptance criteria**
- [x] `serve_deployments(limit)` module-level helper — verified by import smoke +
      `test_orchestrator.py`.
- [x] `serve(...)` uses `limit=` not `global_limit=` —
      `test_serve_deployments_passes_limit_not_global_limit`.
- [x] All deployments + dream cron preserved —
      `test_serve_deployments_registers_all_deployments`.
- [x] Regression test red→green — evidence above.
- [x] serve-workflows reaches "polling for scheduled runs" w/o TypeError — smoke above.
- [x] Format/lint/pre-commit clean; unit + fast integration pass.

**Notes**
- Scope kept minimal per the task: only the kwarg name and the `serve_deployments`
  extraction. Config field `concurrency.runner_global_limit` left UNCHANGED (task
  said not to touch it).
- ADJACENT (not fixed — flagging for a follow-up task, not touched here):
  `apps/memory/src/tree/config/app_config.py:234` docstring still says
  `serve(global_limit=...)` — stale reference to the old (broken) API. It's a
  comment-only doc nit in a file the task told me not to modify; left for the
  orchestrator to decide whether to roll up.
- `make memory-integration-tests-all` (slow + requires_mongot) NOT RUN by SWE —
  that is the Tester's acceptance-gate target per PROCESS.md; this change does not
  touch infra/runtime behavior beyond the serve call, which the smoke check covers.
- DID NOT COMMIT — awaiting Tester review per PROCESS.md.

### [Tester] 2026-05-23 17:40 — QA

**Test summary**
- Format / lint / pre-commit: PASS (`make pre-commit` — all hooks Passed: prettier,
  ruff check, ruff format, biome, KGQuery discipline)
- Unit tests: 1416 passed / 0 failed (`make memory-unit-tests`, 43.65s)
- Integration tests (acceptance gate): 268 passed / 1 skipped / 0 failed
  (`make memory-integration-tests-all`, 11:17, slow + requires_mongot, quiesced +
  isolated stack). Known-unrelated `test_web_serp` passed clean this run.
- Warnings: 0

**E2E adversarial pass**
- HEADLINE — serve-workflows starts clean: started `make memory-serve-workflows`
  from this worktree (pids: shell 7179 → make 7181/7182 → uv 7188 → python 7189).
  Reached `Your deployments are being served and polling for scheduled runs!`,
  listed all 8 deployments incl. `dream-consolidation-all-users/dream-consolidation-etl`.
  NO `TypeError: Runner.__init__() got an unexpected keyword argument 'global_limit'`.
  Then killed ONLY that process tree (SIGTERM 7189..7179); confirmed no serve runner
  remains; left the unrelated `ruff server` LSP untouched. No stale runner left behind.
  (PASS)
- Regression guard reproduction (red→green, did NOT trust the SWE — reproduced):
  reverted helper to `serve(*deployments, global_limit=limit)` →
  `pytest tests/unit/test_orchestrator.py` → 2 failed, 1 passed:
  `test_serve_deployments_passes_limit_not_global_limit` (assert limit==4 → got
  global_limit) and `test_serve_deployments_kwargs_bind_to_real_serve_signature`
  (`KeyError: 'limit'` binding to real `inspect.signature(prefect.serve)`).
  Restored `limit=limit` → 3 passed. Test genuinely fails on the bug. (PASS)
- Adversarial — deployment set unchanged: `git show HEAD:orchestrator.py` vs working
  tree → identical 8 `name=` registrations + `cron=app_config.dream.cron`; the only
  delta is `global_limit=...` → `limit=limit` + the helper extraction. The
  `registers_all_deployments` test asserts the exact set of 8 names. Extraction
  dropped nothing. (PASS)
- Boundary — real Prefect signature: confirmed `prefect 3.6.19`
  `serve(*args, ..., limit: Optional[int]=None, **kwargs)` — `limit` is the real
  param; `global_limit` would silently fall through `**kwargs` into `Runner.__init__`
  and TypeError. The new test binds to this live signature so it cannot drift. (PASS)

**Acceptance criteria**
- [x] PASS — `serve_deployments(limit)` module-level helper, importable/testable.
      Evidence: `orchestrator.py:34`; imported in `test_orchestrator.py` without `__main__`.
- [x] PASS — `serve(...)` uses `limit=`, not `global_limit=`.
      Evidence: `orchestrator.py:87` `limit=limit`; diff removes `global_limit=`;
      `test_serve_deployments_passes_limit_not_global_limit`.
- [x] PASS — all deployment registrations + dream `cron=` preserved verbatim.
      Evidence: HEAD-vs-worktree grep identical; `test_serve_deployments_registers_all_deployments`.
- [x] PASS — regression test red→green. Evidence: reproduction above (2 failed on
      `global_limit`, 3 passed on `limit`).
- [x] PASS — serve-workflows reaches "polling for scheduled runs" without TypeError.
      Evidence: live serve run above (headline).
- [x] PASS — format/lint/pre-commit clean; unit + integration pass. Evidence: summary above.

**code-review plugin (enabled, advisory)**
- No Blockers. Helper fully typed (`limit: int) -> None`), no print(), no secrets,
  no dead/duplicate code (the ADR-002 comment was relocated into the docstring, not
  duplicated), test uses AAA + `mocker` + binds to the real signature.

**Other issues found (non-blocking, not in AC)**
- Nit (pre-existing, out of scope): `apps/memory/src/tree/config/app_config.py:234`
  docstring still references the old `serve(global_limit=...)` API. SWE flagged it;
  the task scoped that file out. Orchestrator may roll up as a follow-up.
- Untracked `tracker/063-pin-bun-version-in-ci.todo.md` present in the worktree —
  unrelated pre-existing tracker file, not part of this change. Not a code diff.

**VERDICT: PASS**
