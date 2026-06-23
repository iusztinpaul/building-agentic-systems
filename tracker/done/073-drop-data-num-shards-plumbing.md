# Drop data num_shards plumbing (script + Make + README)

Status: pending
Tags: `data`, `infra`, `docs`
Depends on: #072
Blocks: #074

## Scope

Now that the data orchestrator declares parallelism per-source (group-by-platform +
HuggingFace `num_workers`, #072) and no longer accepts a `num_shards` parameter, remove
the now-dead `--num-shards`/`NUM_SHARDS` plumbing from the DATA entrypoint script and the
Makefile, and update the operator-facing docs. This is a wiring + docs task — no flow or
config logic changes.

IMPORTANT scoping guard: this touches ONLY the DATA pipeline. The MEMORY pipeline keeps
its `num_shards` knob end-to-end — `scripts/run_memory_pipeline.py`'s `--num-shards`, the
Makefile `run-memory-pipeline-extraction` `NUM_SHARDS` thread, and the memory README row
are ALL untouched.

### 1. `scripts/run_data_pipeline.py`

- Remove the `--num-shards` Click option and the `num_shards` parameter from `main` and
  `_run`. Remove the `if num_shards is not None and num_shards < 1: SystemExit(1)` guard
  and the `parameters["num_shards"] = num_shards` forwarding — the orchestrator takes only
  `user_id` now.
- `DEPLOYMENT_NAME` stays `"data-etl-orchestrator/data-etl-orchestrator"`.
- Keep `init_logger()` at module level, the `USER_ID`/`USER_IDENTIFIER` resolution, the
  log-streaming poll loop, and the final-state exit handling exactly as-is.
- Rewrite the module docstring + the `Usage:` block to describe the new model: the
  operator triggers `data-etl-orchestrator`; it groups the configured sources by platform
  (one `data-etl-worker` per non-HF platform) and fans HuggingFace out into `num_workers`
  offset-windows (declared per-source in `default.yaml`); there is no `--num-shards` and
  no trailing index.

### 2. `apps/memory/Makefile` — `run-data-pipeline` target

- Drop the `$(if $(NUM_SHARDS),--num-shards "$(NUM_SHARDS)",)` fragment from the
  `run-data-pipeline` recipe.
- Rewrite the target's help comment: it triggers `data-etl-orchestrator`, which groups the
  configured `sources.sources` by platform and dispatches one `data-etl-worker` per non-HF
  platform plus `num_workers` HuggingFace offset-window workers; no trailing index; no
  `NUM_SHARDS` flag (parallelism is per-source — platform bucketing + the HF source's
  `num_workers` in `default.yaml`).
- Keep the `USER_ID`/`USER_IDENTIFIER` passthrough.
- Do NOT touch `run-memory-pipeline-extraction` (its `NUM_SHARDS` thread stays).

### 3. `apps/memory/README.md` — data-pipeline row

- Update the `make memory-run-data-pipeline` row (currently describing
  `min(NUM_SHARDS, N)` balanced shards) to describe platform bucketing + HF `num_workers`
  windows, and remove the `NUM_SHARDS` mention. Make clear there is no trailing index and
  that HuggingFace fan-out width is the source's `num_workers` (not a global flag).
- If the README "deployments registered" bullet for `data-etl-orchestrator` mentions
  `sources:` sharding, update its phrasing to "groups by platform + windows HuggingFace".
- Leave every MEMORY row (the `NUM_SHARDS` memory-extraction row) unchanged.

### Files touched

- `apps/memory/scripts/run_data_pipeline.py` — remove `--num-shards`/`num_shards`; rewrite
  docstring + usage.
- `apps/memory/Makefile` — drop `NUM_SHARDS` from `run-data-pipeline`; rewrite help.
- `apps/memory/README.md` — update the data-pipeline row + the data orchestrator bullet.
- (verify-green) any unit test that asserted the `--num-shards` guard for the DATA script
  — remove/retarget it (the data script no longer has the flag); the memory script's
  `--num-shards` guard test stays.

## Acceptance Criteria

- [x] `scripts/run_data_pipeline.py` has NO `--num-shards` Click option and NO
      `num_shards` parameter; it forwards only `{"user_id": …}` to the orchestrator.
- [x] `init_logger()` is still called at module level; `DEPLOYMENT_NAME` is still
      `data-etl-orchestrator/data-etl-orchestrator`; the `USER_ID`/`USER_IDENTIFIER`
      resolution + log-streaming + exit handling are intact.
- [x] `uv run python scripts/run_data_pipeline.py --num-shards 2` now FAILS with a Click
      "no such option" error (the flag is gone) — not a silent accept.
- [x] The Makefile `run-data-pipeline` recipe no longer passes `--num-shards`, references
      no `NUM_SHARDS`, and its help text describes platform bucketing + HF `num_workers`
      with no trailing index.
- [x] `make run-data-pipeline` (no args) still guards on a resolvable user and triggers the
      orchestrator; `make run-data-pipeline NUM_SHARDS=2` ignores the (now meaningless)
      var rather than passing a flag the script rejects.
- [x] The MEMORY pipeline's `num_shards` is fully intact: `scripts/run_memory_pipeline.py`
      `--num-shards`, the Makefile `run-memory-pipeline-extraction` `NUM_SHARDS` thread,
      and the memory README row are UNCHANGED (verify by diff scope).
- [x] The README `make memory-run-data-pipeline` row describes platform bucketing + HF
      windows and removes the `NUM_SHARDS` reference; no stale `min(NUM_SHARDS, N)` text
      remains for the data pipeline.
- [x] No live reference to a data `--num-shards`/`NUM_SHARDS` remains in
      `scripts/run_data_pipeline.py` or the data half of the Makefile (`grep` clean).
- [x] `make memory-format-fix && make memory-lint-fix && make memory-format-check && make memory-lint-check && make pre-commit` all clean.
- [x] `make memory-unit-tests` passes, 0 warnings.

## BDD scenarios

### Scenario: the data script rejects the removed flag
- **Given** the updated `run_data_pipeline.py`
- **When** I run `uv run python scripts/run_data_pipeline.py --num-shards 2`
- **Then** Click errors with "no such option: --num-shards" and a non-zero exit — the knob
  is gone, not silently ignored.

### Scenario: a default data run still works without any shard flag
- **Given** the stack up and `make memory-serve-workflows` running
- **When** I run `make memory-run-data-pipeline USER_ID=507f1f77bcf86cd799439011`
- **Then** the script triggers `data-etl-orchestrator` with only `user_id`, and the
  orchestrator fans out per-platform + per-HF-window — no `num_shards` is read anywhere.

### Scenario: the memory pipeline's num_shards is untouched
- **Given** this data-only change
- **When** I run `make memory-run-memory-pipeline-extraction USER_ID=<oid> NUM_SHARDS=2`
- **Then** it still passes `--num-shards 2` to `run_memory_pipeline.py` and fans the
  memory extraction into 2 shards — the memory knob is unaffected.

## User Stories

### Story: Operator triggers data ingestion without guessing a shard count
1. Operator runs `make memory-run-data-pipeline USER_ID=<oid>`.
2. There is no `NUM_SHARDS` to choose — parallelism is already declared in `default.yaml`
   (platform bucketing is automatic; the HuggingFace source carries `num_workers`).
3. The run streams logs and completes; the operator never had to reason about a global
   shard width.

### Story: Operator reads the README and sees the per-source model
1. Operator opens `apps/memory/README.md` to learn how data fan-out works.
2. The `make memory-run-data-pipeline` row explains: one worker per non-HF platform plus
   `num_workers` HuggingFace offset-window workers, no trailing index, no global flag.
3. The operator updates the HuggingFace source's `num_workers` in `default.yaml` to change
   fan-out width — there is no CLI flag to remember.

## Test guidance

- The script-flag removal is best verified by a small CLI-level assertion (Click rejects
  `--num-shards`) and by the absence of `num_shards` forwarding — UNIT where a test exists;
  otherwise the grep ACs + the live `--num-shards` rejection in #074's pass cover it.
- No new Mongo/Prefect tests needed. Per CLAUDE.md, call `/testing-python` if you
  add/retarget any test, and run via `make memory-*` targets.

---

Blocked by: #072

## Log

### [PA] 2026-06-22 14:15 — Grooming

**Summary**
Remove the dead `--num-shards`/`NUM_SHARDS` plumbing from the DATA entrypoint script and
Makefile target, and update the README data-pipeline row to describe the per-source
parallelism model (platform bucketing + HF `num_workers`). Wiring + docs only; no flow or
config change. The MEMORY pipeline's `num_shards` is explicitly out of scope and stays.

**Key decisions**
- Strictly data-only: the memory `num_shards` (script flag + Make thread + README row) is
  untouched — guarded by an explicit AC + a "memory unchanged" BDD scenario.
- The data script keeps `DEPLOYMENT_NAME`, `init_logger()`, user resolution, and the
  log-streaming loop; only the shard flag/forwarding is excised.
- Splitting this out of #072 keeps the flow change (testable in isolation) separate from
  the operator-surface change, matching the repo's one-concern-per-task decomposition.

**Dependencies**
- #072 — the orchestrator must already have dropped the `num_shards` parameter, or the
  script would forward a param the flow rejects.

**User stories**
- 2 stories: operator triggers data ingestion without a shard count; operator reads the
  README and sees the per-source model.

Ready for implementation.

### [SWE] 2026-06-22 15:10 — Implementation

**Files modified**
- `apps/memory/scripts/run_data_pipeline.py` — removed the `--num-shards` Click option,
  the `num_shards` param from `main`/`_run`, the `< 1` guard, and the
  `parameters["num_shards"]` forwarding; rewrote docstring + `Usage:` to the per-source
  (platform-bucketing + HF `num_workers`) model. `DEPLOYMENT_NAME`, `init_logger()`,
  user resolution, log-streaming loop, and exit handling untouched.
- `apps/memory/Makefile` — dropped the `$(if $(NUM_SHARDS),--num-shards ...)` fragment
  from the `run-data-pipeline` recipe and rewrote its help comment (platform bucketing +
  HF `num_workers`; no trailing index; no `NUM_SHARDS`). Kept the `USER_ID`/
  `USER_IDENTIFIER` passthrough. `run-memory-pipeline-extraction` untouched.
- `apps/memory/README.md` — rewrote the `make memory-run-data-pipeline` row (platform
  bucketing + HF windows, no trailing index, no `min(NUM_SHARDS, N)` / `NUM_SHARDS`) and
  the `data-etl-orchestrator` deployments bullet ("groups by platform + windows
  HuggingFace"). Memory rows unchanged.
- `README.md` (root) — End-to-end quick-start note now scopes `NUM_SHARDS` to
  `run-memory-pipeline-extraction` only; `run-data-pipeline` described as per-source.

**Tests**
- Unit: 1597 passing, 0 failing, 0 warnings — `make memory-unit-tests`.
- Integration: N/A — no flow/infra changes (wiring + docs only; the data orchestrator's
  dropped `num_shards` param is already covered by #072's `test_orchestrator_data.py`).
- No test referenced the data script's `--num-shards` CLI flag (verified by
  `grep -rln "run_data_pipeline" apps/memory/tests/` → none), so none needed retargeting.
  The `num_shards` tests under `tests/unit/{data,memory}/...` and `test_sharding.py` are
  about the orchestrator/sharding helpers, not the data CLI — left as-is.

**Acceptance criteria**
- [x] `run_data_pipeline.py` has NO `--num-shards` option / `num_shards` param; forwards
  only `{"user_id": …}` — verified by `--help` (only `--user-id`/`--user-identifier`) and
  the script diff (all `num_shards` lines removed, zero added).
- [x] `init_logger()` still at module level; `DEPLOYMENT_NAME` unchanged; user resolution
  + log-streaming + exit handling intact (diff shows those blocks unmodified).
- [x] `uv run python scripts/run_data_pipeline.py --num-shards 2` FAILS with Click
  "No such option: --num-shards", exit code 2 (not silent accept).
- [x] Makefile `run-data-pipeline` recipe no longer passes `--num-shards`, references no
  live `NUM_SHARDS` thread, help describes platform bucketing + HF `num_workers`.
- [x] `make run-data-pipeline NUM_SHARDS=2` ignores the var (no `$(if $(NUM_SHARDS)...)`
  fragment remains, so nothing is appended).
- [x] MEMORY `num_shards` fully intact — `run_memory_pipeline.py` `--num-shards` (12
  refs), Makefile `run-memory-pipeline-extraction` `NUM_SHARDS` thread (line 186), memory
  README rows UNCHANGED (`git diff --name-only` excludes `run_memory_pipeline.py`).
- [x] README data row describes platform bucketing + HF windows, removes `NUM_SHARDS`; no
  stale `min(NUM_SHARDS, N)` remains (`grep` clean).
- [x] No live data `--num-shards`/`NUM_SHARDS` reference in the script or data half of the
  Makefile (`grep` clean — only the descriptive "no NUM_SHARDS flag" phrase in help).
- [x] format-fix / lint-fix / format-check / lint-check / pre-commit all clean.
- [x] `make memory-unit-tests` passes, 0 warnings.

**Evidence**
```
$ uv run python scripts/run_data_pipeline.py --help
Usage: run_data_pipeline.py [OPTIONS]
  Trigger the data-etl-orchestrator deployment for the resolved user.
Options:
  --user-id TEXT          Override the target tenant by Mongo ObjectId. ...
  --user-identifier TEXT  Override the target tenant by stable handle ...
  --help                  Show this message and exit.

$ uv run python scripts/run_data_pipeline.py --num-shards 2; echo "exit=$?"
Error: No such option: --num-shards
exit=2

$ uv run python scripts/run_memory_pipeline.py --help | grep num-shards
  --num-shards INTEGER    Optional document-shard fan-out width (#067). ...   # MEMORY intact

$ make memory-format-fix && make memory-lint-fix && make memory-format-check && make memory-lint-check
All checks passed!  (282 files formatted / lint clean)

$ make pre-commit
prettier ... Passed | ruff check ... Passed | ruff format ... Passed | biome check ... Passed

$ make memory-unit-tests
============================ 1597 passed in 48.15s =============================
```

**Notes**
- Working tree also shows `docs/adrs/002_...md` as modified — that is a PRE-EXISTING change
  on this refactor branch, NOT touched by this task (out of scope).
- e2e: ran the `--help`/`--num-shards`-rejection smoke (the operator-surface this task
  changes). Did not run a live `make memory-run-data-pipeline` end-to-end since this is a
  wiring/docs change to the trigger surface and the orchestrator flow itself is unchanged
  (and covered by #072). Code is local and UNCOMMITTED — ready for Tester review.

### [Tester] 2026-06-22 16:05 — QA

**Test summary**
- Format / lint / pre-commit: PASS (prettier / ruff check / ruff format / biome — all Passed)
- Unit tests: 1597 passed / 0 failed
- Integration tests: N/A (wiring + docs only; no flow/infra change — data orchestrator's
  dropped `num_shards` param is covered by #072's `test_orchestrator_data.py`)
- Warnings: 0

**E2E adversarial pass** (data CLI operator surface — the only behavior this task changes)
- Happy path: `uv run python scripts/run_data_pipeline.py --help` → shows only
  `--user-id` / `--user-identifier` / `--help`; no `--num-shards`; usage docstring describes
  per-source (platform bucketing + HF `num_workers`) model (PASS)
- Break path 1 (removed flag): `uv run python scripts/run_data_pipeline.py --num-shards 2`
  → `Error: No such option: --num-shards`, exit=2 (vs expected non-zero Click rejection) (PASS)
- Break path 2 (boundary — flag with no value): `... --num-shards` (no arg) →
  `Error: No such option: --num-shards`, non-zero (no crash, no silent accept) (PASS)
- Break path 3 (Click prefix-matching leak): `... --num 2` → `Error: No such option: --num`,
  non-zero — abbreviation does NOT resurrect the removed flag (PASS)
- CRITICAL memory-path regression check: `uv run python scripts/run_memory_pipeline.py --help`
  → STILL shows `--num-shards INTEGER`; Makefile `run-memory-pipeline-extraction` still
  threads `$(if $(NUM_SHARDS),--num-shards ...)` (line 186); `memory_extract_etl_orchestrator`
  still carries `num_shards: int = 1` (pipeline.py:1636). Memory knob unaffected (PASS)

**Acceptance criteria**
- [x] PASS — data script has NO `--num-shards` option / `num_shards` param; forwards only
      `{"user_id": …}` — Evidence: `run_data_pipeline.py:60` (`parameters = {"user_id": ...}`,
      no num_shards key), `--help` shows only user flags, `grep num_shards run_data_pipeline.py`
      → clean.
- [x] PASS — `init_logger()` at module level (`run_data_pipeline.py:43`); `DEPLOYMENT_NAME`
      unchanged (line 46); user resolution (line 55) + log-streaming loop (lines 73-94) +
      `sys.exit(1)` on non-completed final state (line 92) intact — diff shows those blocks
      unmodified.
- [x] PASS — `--num-shards 2` FAILS Click "No such option: --num-shards", exit=2 (see break path 1).
- [x] PASS — Makefile `run-data-pipeline` recipe (line 180) no longer passes `--num-shards`;
      no live `$(if $(NUM_SHARDS)...)` thread; help (line 179) describes platform bucketing +
      HF `num_workers`, no trailing index. Only `NUM_SHARDS` token on line 179 is the
      descriptive "no NUM_SHARDS flag" phrase.
- [x] PASS — `make run-data-pipeline NUM_SHARDS=2` ignores the var: no `$(if $(NUM_SHARDS)...)`
      fragment remains in the recipe, so nothing is appended; user-resolution passthrough kept.
- [x] PASS — MEMORY `num_shards` fully intact: `run_memory_pipeline.py --help` still shows
      `--num-shards` (12 refs in file); Makefile line 186 still threads `NUM_SHARDS`;
      `extraction/pipeline.py:1636` `num_shards: int = 1`. `git diff --name-only` excludes
      `run_memory_pipeline.py` and `extraction/pipeline.py`.
- [x] PASS — README data row (`apps/memory/README.md:122`) describes platform bucketing + HF
      `num_workers` windows, removes `NUM_SHARDS`; `grep "min(NUM_SHARDS"` → clean.
- [x] PASS — no live data `--num-shards`/`NUM_SHARDS` in script or data half of Makefile
      (grep clean; sole token is the descriptive help phrase).
- [x] PASS — format-fix / lint-fix / format-check / lint-check / pre-commit all clean.
- [x] PASS — `make memory-unit-tests` → 1597 passed, 0 warnings.

**Evidence**
```
$ uv run python scripts/run_data_pipeline.py --help
Options:
  --user-id TEXT          Override the target tenant by Mongo ObjectId. ...
  --user-identifier TEXT  Override the target tenant by stable handle ...
  --help                  Show this message and exit.

$ uv run python scripts/run_data_pipeline.py --num-shards 2; echo exit=$?
Error: No such option: --num-shards
exit=2

$ uv run python scripts/run_memory_pipeline.py --help | grep num-shards
  --num-shards INTEGER    Optional document-shard fan-out width (#067). ...   # MEMORY intact

$ make pre-commit
prettier ... Passed | ruff check ... Passed | ruff format ... Passed | biome check ... Passed

$ make memory-unit-tests
============================ 1597 passed in 46.40s =============================
```

**Other issues found**
- None. Diff is surgical (4 in-scope files: data script, Makefile data target, both READMEs).
  Out-of-scope working-tree artifacts (`docs/adrs/002_*.md`, `docs/glossary.md`,
  `tracker/feature-*`, `tracker/074-*`) confirmed NOT part of the 073 change — ignored per
  task scope.
- code-review plugin is enabled in `.claude/settings.json` but is a slash-command/agent, not
  a Bash-invocable binary; performed the equivalent manual checklist review instead — no
  defects on a logic-free wiring+docs diff.
- No unit test asserts the data CLI `--num-shards` flag (grep clean); the `num_shards` refs in
  `tests/unit/data/test_orchestrator_data.py` correctly target the #072 orchestrator-flow
  param removal, not the CLI — left as-is, complementary not orphaned.

**VERDICT: PASS**
