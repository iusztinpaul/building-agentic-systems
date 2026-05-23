# 062 — Remove the inert `concurrency.fanout_max_parallel` knob

## Scope

The `concurrency.fanout_max_parallel` config knob is INERT. Its only consumer is
`_orchestrate_sharded_extraction` (`apps/memory/src/tree/memory/extraction/pipeline.py`),
which calls `_resolve_num_shards(num_shards, default=cfg.concurrency.fanout_max_parallel)`.
But that helper's `default` only fires when `num_shards is None`, and the orchestrator
path is reached only with an explicit `num_shards > 1` (always an int), so the default
never fires. The shard count is always an explicit per-run choice
(`NUM_SHARDS` / `--num-shards` / `-p num_shards`).

Per user decision: REMOVE the knob (shard count stays an explicit per-run choice;
`runner_global_limit` remains the config-level concurrency cap). Also clean up stale
comments that reference the deleted `memory_extraction_sharded` "sharded parent flow"
(the #061 rework folded fan-out into the single `memory_extraction` deployment).

Out of scope (explicitly do NOT touch):
- `max_total_tokens` in `default.yaml` (single clean value, no duplicate).
- `dispatch_concurrency` (intentional forward seam).

## Acceptance Criteria

- [x] `apps/memory/configs/default.yaml`: `fanout_max_parallel: 4` line and its comment removed; stale `doc_concurrency` "sharded parent flow" comment reworded to reflect the `memory_extraction` orchestrator path (`num_shards>1`).
- [x] `apps/memory/src/tree/config/app_config.py`: `fanout_max_parallel` field removed from `ConcurrencyConfig`, its docstring bullet removed; the stale "parent flow, see ADR-002" comment on `doc_concurrency` reworded to point at the `num_shards` orchestrator path.
- [x] `apps/memory/src/tree/memory/extraction/sharding.py`: `_resolve_num_shards` simplified to `def _resolve_num_shards(num_shards: int) -> int: return max(1, num_shards)`; docstring drops the `None`/`default`/`fanout_max_parallel` reference but keeps the clamp rationale.
- [x] `apps/memory/src/tree/memory/extraction/pipeline.py`: call site updated to `effective_num_shards = _resolve_num_shards(num_shards)`; now-unused `cfg = _live_app_config()` removed from `_orchestrate_sharded_extraction`.
- [x] Tests updated: `tests/unit/config/test_app_config.py` drops `fanout_max_parallel` assertions and YAML fixture lines; `tests/unit/memory/extraction/test_fanout.py` updated to the new `_resolve_num_shards(int)` signature, keeping the `max(1, …)` clamp coverage (`0→1`, `-3→1`, positive unchanged + `4→4`).
- [x] `grep -rn "fanout_max_parallel" apps/memory` returns NOTHING.
- [x] Format/lint/pre-commit clean; unit tests pass; fast integration tests pass (isolated stack).
- [x] `ConcurrencyConfig` loads with no `fanout_max_parallel` and intact `voyage_rpm` / `voyage_tpm` / `runner_global_limit`.

## Log

### [SWE] 2026-05-23 14:30 — Implementation

**Files modified**
- `apps/memory/configs/default.yaml` — removed `fanout_max_parallel: 4` + its comment; reworded the stale `doc_concurrency` "sharded parent flow" comment to the `memory_extraction` orchestrator path (`num_shards>1`).
- `apps/memory/src/tree/config/app_config.py` — removed the `fanout_max_parallel: int = 4` field and its docstring bullet from `ConcurrencyConfig`; reworded the stale `doc_concurrency` "parent flow, see ADR-002" comment to point at the `num_shards` orchestrator path.
- `apps/memory/src/tree/memory/extraction/sharding.py` — simplified `_resolve_num_shards` to `(num_shards: int) -> int: return max(1, num_shards)`; dropped the `None`/`default`/`fanout_max_parallel` docstring branch, kept the clamp rationale.
- `apps/memory/src/tree/memory/extraction/pipeline.py` — call site now `effective_num_shards = _resolve_num_shards(num_shards)`; removed the now-unused `cfg = _live_app_config()` from `_orchestrate_sharded_extraction` (verified `cfg` was used only for the deleted `fanout_max_parallel` reference within that function).
- `apps/memory/tests/unit/config/test_app_config.py` — dropped the three `fanout_max_parallel` assertions + the YAML fixture line; updated the "four knobs" docstring wording.
- `apps/memory/tests/unit/memory/extraction/test_fanout.py` — updated to the new `_resolve_num_shards(int)` signature (dropped `default=` kwargs), removed the now-impossible `None`-fallback test, kept clamp coverage (`0/-1/-3 → 1`) and positive-unchanged (added `4`).

**Tests**
- Unit: 1413 passing, 0 failing — `make memory-unit-tests`.
- Integration (fast, `make memory-integration-tests`): 153 passing, 1 skipped, 115 deselected. Ran isolated on a quiesced shared stack (containers `tree-mongodb`/`tree-mongot`/`tree-prefect-server` up). The fan-out integration test (`test_extraction_fanout.py`) is in the deselected slow tail — Tester's `integration-tests-all` gate covers it.

**Acceptance criteria** — all non-HUMAN criteria verified (see checkboxes above).

**Evidence**
```
$ grep -rn "fanout_max_parallel" apps/memory
(no matches; exit 1)

$ grep -rn "sharded parent flow\|memory_extraction_sharded" apps/memory
(no matches)

$ make memory-format-fix && make memory-lint-fix && make memory-format-check && make memory-lint-check && make pre-commit
ruff format: 264 files left unchanged
ruff check --fix: All checks passed!
ruff format --check: 264 files already formatted
ruff check: All checks passed!
pre-commit: Validate pyproject.toml (skipped), prettier Passed, ruff check Passed,
            ruff format Passed, biome check (harness) Passed, KGQuery discipline Passed

$ make memory-unit-tests
============================ 1413 passed in 48.19s =============================

$ uv run python -c "from tree.config.app_config import app_config; c=app_config.concurrency; print(c); print('has fanout_max_parallel:', hasattr(c,'fanout_max_parallel'))"
voyage_rpm=3 voyage_tpm=10000 runner_global_limit=4
has fanout_max_parallel: False

$ make memory-integration-tests
========== 153 passed, 1 skipped, 115 deselected in 183.37s (0:03:03) ==========
```

**Notes**
- Pure refactor (removing an inert, never-fired config knob + stale-comment cleanup) — no behavior change, so no new red/green TDD; existing fan-out/sharding unit + integration tests are the regression net and were updated to the new signature.
- Did NOT touch `max_total_tokens` (single clean value at default.yaml; no duplicate) or `dispatch_concurrency` (intentional forward seam), per the task.
- Remaining `fanout_max_parallel` mentions in the repo are only in `tracker/` files (the #061 history note that flagged this cleanup, and this #062 task) — not code/config/deploy/docs consumers.
- Full `make memory-integration-tests-all` (incl. slow + `requires_mongot` fan-out e2e) is the Tester's acceptance gate.
- NOT COMMITTED — handing off to Tester first.

### [Tester] 2026-05-23 16:10 — QA

**Test summary**
- Format / lint / pre-commit: PASS (`make pre-commit` — all hooks Passed; ruff check + format clean)
- Unit tests: 1413 passed / 0 failed / 0 warnings (`make memory-unit-tests`; also reproduced synchronously)
- Integration tests (ACCEPTANCE GATE `make memory-integration-tests-all`, slow + `requires_mongot`): 268 passed / 1 skipped / 0 failed in 675.66s, exit 0, 0 warnings. Ran QUIESCED + ISOLATED (killed the lingering backgrounded unit run first; no other pytest against the shared stack). `test_web_serp` PASSED this run (no flake); no exit-code-affecting Prefect stderr.

**E2E adversarial pass** (the fan-out path is the thing that could break)
- Happy path (orchestrator fan-out): integration `test_extraction_fanout.py::test_orchestrator_fans_out_per_shard_then_indexes_once` → 6 ids + `num_shards=4` ⇒ 4 disjoint extraction child runs (each `num_shards=1`) + exactly 1 index run. PASS. Whole `test_extraction_fanout.py` file (9 tests, incl. resolve-pending, empty-no-op, one-shard-failure-isolation, worker-path-default/`=1` zero-self-dispatch) PASSED.
- Break path 1 (boundary: zero/negative shard count via direct Prefect trigger): `_resolve_num_shards(0)==1`, `(-3)==1` (probed live + unit `test_resolve_num_shards_clamps_nonpositive_to_one[0/-1/-3]`). Clamp preserved → no silent zero-shard no-op. PASS.
- Break path 2 (positive shard counts unchanged on orchestrator path): live probe `_resolve_num_shards(4)==4`, `(2)==2`, `(1)==1`, `(7)==7`; unit `[1,2,4,7]` unchanged. PASS — fan-out partitions identically to pre-#062 (removed knob was never consulted on this path).
- Break path 3 (malformed type / dangling-ref hunt — would `max(1, None)` TypeError if any caller passed None): live probe `_resolve_num_shards(None)` → `TypeError` AS EXPECTED for the new `int` signature; grep confirms NO caller/test passes None — `memory_extraction(num_shards: int = 1)` defaults to int, orchestrator entered only at `>1`, `_orchestrate_sharded_extraction(num_shards: int)` passes the int straight through. No reachable None path. PASS.
- Worker path untouched (`num_shards=1` / default): `test_worker_path_default_*` + `test_worker_path_num_shards_one_*` → ZERO `run_deployment`, ZERO index. PASS.

**Acceptance criteria**
- [x] PASS — `default.yaml`: `fanout_max_parallel: 4` + comment removed; `doc_concurrency` "sharded parent flow" comment reworded to `memory_extraction` orchestrator path (`num_shards>1`) — `git diff configs/default.yaml` (lines 103-106, 167-170).
- [x] PASS — `app_config.py`: `fanout_max_parallel` field + docstring bullet removed from `ConcurrencyConfig`; stale `doc_concurrency` "parent flow, see ADR-002" comment reworded to the `num_shards` orchestrator path — `git diff src/tree/config/app_config.py`.
- [x] PASS — `sharding.py`: `_resolve_num_shards` simplified to `def _resolve_num_shards(num_shards: int) -> int: return max(1, num_shards)`; docstring drops `None`/`default`/`fanout_max_parallel`, keeps clamp rationale — `sharding.py:101-113`.
- [x] PASS — `pipeline.py`: call site `effective_num_shards = _resolve_num_shards(num_shards)`; unused `cfg = _live_app_config()` removed from `_orchestrate_sharded_extraction` — `pipeline.py:1490`.
- [x] PASS — Tests updated: `test_app_config.py` drops 3 `fanout_max_parallel` assertions + YAML fixture line; `test_fanout.py` on new `_resolve_num_shards(int)` signature, clamp coverage `0/-1/-3→1`, positive unchanged `[1,2,4,7]`, removed impossible None-fallback test. 59 passed in the two files.
- [x] PASS — `grep -rn "fanout_max_parallel" apps/memory` → NOTHING (exit 1). Also `grep "sharded parent flow\|memory_extraction_sharded" apps/memory/{src,configs}` → NOTHING. Remaining mentions only in `tracker/` history (061/062/done-054/done-056).
- [x] PASS — Format/lint/pre-commit clean; unit pass; integration (full acceptance gate, not just fast) pass.
- [x] PASS — `ConcurrencyConfig` loads: `voyage_rpm=3 voyage_tpm=10000 runner_global_limit=4`, `hasattr(c,'fanout_max_parallel')=False`. No dangling `app_config.concurrency.fanout_max_parallel` access anywhere in src/scripts/deploy.

**Diff scope** — confined to the 6 intended code files + tracker. Extra tracker edits (`061-...in-progress.md`, `feature-...plan.md`) are doc-only (a #061 PR-Reviewer log entry + plan additions); NOTHING in rate-limiter/bulk_write/dedupe/chunking. Verified via `git diff --stat` + per-file diff.

**Evidence**
```
$ grep -rn "fanout_max_parallel" apps/memory          → (exit 1, no matches)
$ grep -rn "sharded parent flow\|memory_extraction_sharded" apps/memory/src apps/memory/configs → (exit 1)
$ uv run python -c "from tree.memory.extraction.sharding import _resolve_num_shards; ..."
_resolve_num_shards(4)=4  (2)=2  (1)=1  (0)=1  (-3)=1  (7)=7   None→TypeError (expected; no caller passes None)
$ uv run python -c "from tree.config.app_config import app_config; print(app_config.concurrency)"
voyage_rpm=3 voyage_tpm=10000 runner_global_limit=4   has fanout_max_parallel: False
$ make memory-unit-tests
============================ 1413 passed in 48.35s =============================
$ make memory-integration-tests-all
========== 268 passed, 1 skipped in 675.66s (0:11:15) ==========  (exit 0)
```

**Other issues found**
- None. Pure refactor; behavior on both the orchestrator (`num_shards>1`) and worker (`num_shards=1`) paths is byte-for-byte equivalent and proven by the green fan-out e2e suite.

**VERDICT: PASS**
