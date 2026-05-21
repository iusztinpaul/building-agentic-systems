# R3: parallelize the read-only dedupe task via bounded asyncio.gather

Status: pending
Tags: `memory`, `performance`
Depends on: #054
Blocks: —

## Scope

Behavior-preserving refactor: run the per-entity dedup decisions concurrently under a bounded
`asyncio.gather`. `dedupe_entity` (`dedup.py:156`) is a read-only `$vectorSearch` on PRECOMPUTED
vectors — no Voyage call, independent per entity — so it parallelizes safely. Plan Part B, R3.

- **`pipeline.py:898-933` (`dedupe_entities` task):** replace the sequential
  `for key, resolved_entity in resolved.resolved_by_key.items()` loop with a bounded
  `asyncio.gather` over the same items, gated by `asyncio.Semaphore(app_config.extraction.dedup_concurrency)`
  (default 8). Each task computes the same `DedupDecision` for its key.
- The result `decisions` dict and the `n_merged/n_flagged/n_none` tallies must be IDENTICAL to the
  sequential version (order of insertion into the dict must not affect the final mapping; tallies are
  commutative). The early-continue branches (`not embedding or not dedup_config.enabled` → `action="none"`)
  must be preserved per key.
- The `dedupe_entities:` summary log line must report the same counts.
- Uses the existing `dedup_concurrency` knob added in #054 (no new knob here).

## Acceptance Criteria

- [x] `dedupe_entities` runs decisions concurrently under `Semaphore(dedup_concurrency)`; no
      sequential per-entity `await dedupe_entity` loop remains (diff-verified).
- [x] For a fixed resolved-entity input, the produced `decisions` mapping and the
      `n_merged/n_flagged/n_none` tallies are identical to the pre-change sequential output
      (unit test compares both implementations on the same fixture, or asserts golden counts).
- [x] Setting `TREE_EXTRACTION__DEDUP_CONCURRENCY=1` reproduces the exact sequential behavior
      (concurrency=1 degenerate case) — test asserts identical output.
- [x] No Voyage embed call is introduced in the dedupe task (it only reads precomputed vectors) —
      diff confirms `dedupe_entity` is unchanged and no `.embed(` appears in the task.
- [x] `make memory-format-fix && make memory-lint-fix && make memory-format-check && make memory-lint-check && make pre-commit` clean.
- [x] `make memory-unit-tests` passes; `make memory-integration-tests` (fast) passes.
- [x] `make memory-integration-tests-all` passes with the mongot stack up.
- [ ] [HUMAN/Tester] Behavior-preservation e2e: a single
      `make memory-run-memory-pipeline-extraction USER_ID=<oid>` logs IDENTICAL
      `dedupe_entities: n_merged=… n_flagged=… n_none=…` to a pre-change run (plan §4), and the
      downstream `apply_writes` counts are unchanged.

## User Stories

### Story: Engineer confirms parallel dedup with bounded concurrency
1. Engineer reads the diff on `dedupe_entities`.
2. The decisions are gathered concurrently under a semaphore sized by `dedup_concurrency` (8).
3. No Voyage embed call was added — only the precomputed-vector `$vectorSearch` read runs in parallel.

### Story: Tester confirms identical dedup outcomes
1. Tester runs extraction on a fixed fixture before and after.
2. The `dedupe_entities:` log shows identical `n_merged/n_flagged/n_none`.
3. The set of merged/flagged node `_id`s is identical between runs.

---

Blocked by: #054

## Log

### [SWE] 2026-05-21 14:30 — Implementation

**Files modified**
- `apps/memory/src/tree/memory/extraction/pipeline.py` — `_dedupe_entities` task body: replaced
  the sequential `for key, resolved_entity in resolved.resolved_by_key.items()` loop with a bounded
  `asyncio.gather` over an inner `_one(key, resolved_entity)` coroutine gated by
  `asyncio.Semaphore(_live_app_config().extraction.dedup_concurrency)`. `dedupe_entity` in
  `dedup.py` is UNCHANGED.
- `apps/memory/tests/unit/memory/extraction/test_pipeline.py` — new `TestDedupeEntitiesParallelization`
  class (6 tests) + a sequential-reference oracle helper and `asyncio` import.

**Before → after of the loop**
- Before (sequential): a single `for` loop computed `prospective_id`, awaited `dedupe_entity`
  in-line, called `_to_decision`, inserted into `decisions[key]`, and incremented
  `n_merged/n_flagged/n_none` — one entity at a time.
- After (parallel): an inner `async def _one(key, resolved_entity) -> DedupDecision` does the same
  per-key work (split key, resolve embeddable text + precomputed vector, early-return
  `DedupDecision(action="none")` on `not embedding or not dedup_config.enabled`, else `await
  dedupe_entity` *inside* `async with semaphore`, then `_to_decision`). Coroutines for all items
  are launched with `results = await asyncio.gather(*[_one(k, e) for k, e in items])`. Tallies and
  the `decisions` dict are then rebuilt by zipping `items` with `results` in the ORIGINAL key
  order, so the mapping and counts are byte-identical to the sequential version.
- The early-continue branches are preserved (now `return DedupDecision(action="none")` from
  `_one`). The `dedupe_entities: n_merged=… n_flagged=… n_none=…` summary log line is unchanged.

**Tests**
- Unit: 1401 passing, 0 failing (`make memory-unit-tests`). New class:
  `TestDedupeEntitiesParallelization` (6 tests) — confirmed RED first
  (`test_no_sequential_await_loop_remains` + `test_decisions_run_concurrently_under_semaphore`
  failed before the edit), then GREEN after.
- Integration (fast): 153 passed, 1 skipped, 113 deselected (`make memory-integration-tests`),
  including `tests/integration/memory/test_extraction_pipeline.py` (9 passed) and
  `test_dedup.py` (14 passed). Run isolated on a quiesced shared docker stack.

**Acceptance criteria**
- [x] Concurrent under `Semaphore(dedup_concurrency)`, no sequential loop — verified by
      `test_decisions_run_concurrently_under_semaphore` (max in-flight == bound) and
      `test_no_sequential_await_loop_remains` (source-introspection diff-guard).
- [x] Identical `decisions` + tallies vs sequential reference —
      `test_identical_output_to_sequential_reference` (15-entity fixture exercising all 3 buckets).
- [x] `TREE_EXTRACTION__DEDUP_CONCURRENCY=1` reproduces sequential —
      `test_concurrency_one_reproduces_sequential`.
- [x] No `.embed(` in the dedupe task — `test_no_embed_call_in_dedupe_task`; `dedup.py` untouched.
- [x] Per-key early-continue preserved — `test_disabled_and_missing_embedding_short_circuit_per_key`.
- [x] format/lint/pre-commit clean.
- [x] unit + fast integration pass.
- [ ] `make memory-integration-tests-all` (mongot stack) — Tester gate.
- [ ] [HUMAN/Tester] behavior-preservation e2e (identical `dedupe_entities:` counts + unchanged
      `apply_writes` counts) — Tester gate.

**Evidence**
```
$ make memory-unit-tests
============================ 1401 passed in 41.40s =============================

$ make memory-integration-tests
========== 153 passed, 1 skipped, 113 deselected in 160.42s (0:02:40) ==========

$ # End-to-end: real app_config load + parallel task body over 20 entities
dedup_concurrency = 8
INFO:tree.memory.extraction.pipeline:dedupe_entities: n_merged=7 n_flagged=7 n_none=6
RESULT decisions: 20 tallies: {'merged': 7, 'flagged': 7, 'none': 6}
```
(20-entity fixture cycling merged/flagged/none → expected 7/7/6; tallies match and sum to 20,
identical to what the sequential code would produce.)

**Notes**
- Uses the existing `app_config.extraction.dedup_concurrency` knob (default 8) read via the same
  `_live_app_config()` helper that the LLM stage uses for `llm_concurrency`. No new knob.
- `dedupe_entity` (`dedup.py:156`) is untouched — it remains a read-only `$vectorSearch` on
  precomputed vectors; no `.embed(` call exists in the dedupe task.
- DO NOT COMMIT — handing to Tester. `integration-tests-all` (mongot) and the identical-counts e2e
  on a real user are the Tester's acceptance gate; must be run isolated on a quiesced stack per
  CLAUDE.md (shared docker stack across worktrees).

### [Tester] 2026-05-21 16:10 — QA

**Diff scope (refactor — output-identity is the whole game)**
- `git diff --name-only` → exactly `pipeline.py` + `tests/unit/memory/extraction/test_pipeline.py`
  (tracker files untracked). `dedup.py` is NOT modified — confirmed. ✓
- Per-key body of `_dedupe_entities` is line-for-line identical to the pre-change HEAD loop
  (compared `git show HEAD:…pipeline.py` lines 885–957 against the working tree). The ONLY change
  is execution order: sequential `for` → `asyncio.gather` over an inner `_one(key, entity)` coroutine
  gated by `asyncio.Semaphore(_live_app_config().extraction.dedup_concurrency)`; `decisions` + tallies
  rebuilt by `zip(items, results)` in ORIGINAL key order. Mirrors the existing `llm_concurrency`
  pattern at pipeline.py:328. ✓

**No-embed invariant**
- `grep '\.embed('` over the `_dedupe_entities` body (lines 885–957) → NONE. The three `.embed(`
  hits in pipeline.py (854, 1226, 1290) are all in OTHER functions. Dedupe reads precomputed vectors
  only via `embeddings.vectors.get(...)`. Safe to parallelize under the Voyage limiter. ✓

**Oracle faithfulness (read, not trusted)**
- `_sequential_reference` re-implements the OLD loop INDEPENDENTLY — it does NOT call
  `_dedupe_entities`. It re-derives `prospective_id` via `build_node_id`/`_normalize`, calls the mocked
  `dedupe_fn` directly, calls `_to_decision`, and tallies — exactly the pre-change body. It imports
  only the unchanged shared helpers (`_to_decision`, `_normalize`), which is correct. The oracle
  proves output identity, not "calls new code." ✓

**E2E adversarial pass** (ran `_dedupe_entities` directly + a teeth probe)
- Happy path: `make memory-integration-tests-all` drives the REAL parallel task body through
  `memory_extraction` → 266 passed / 1 skipped / 0 warnings, exit 0. (PASS)
- Break path 1 (boundary: empty resolved set): `_dedupe_entities` over 0 entities → `decisions == {}`,
  no crash (gather of zero). (PASS)
- Break path 2 (state edge: dedup disabled / all-`none`): `enabled=False`, 5 entities → all
  `action="none"`, `dedupe_entity` NEVER called (per-key short-circuit preserved). (PASS)
- Break path 3 (boundary: single entity, gather of 1): → correct `merged` decision. (PASS)
- Break path 4 (concurrency mis-zip): completion order REVERSED via per-name sleeps so later keys
  finish first; every `decisions[key]` still carries its OWN key's `matched_node_id`. No mis-assign
  from the `zip` rebuild. (PASS)
- Teeth check: simulated the OLD sequential body under the concurrency probe → `max_in_flight==1`
  (would FAIL the `==4` assertion); parallel → `max_in_flight==4`. The concurrency assertion is NOT
  vacuous — it distinguishes sequential from parallel. (PASS)

**Test summary**
- Format / lint / pre-commit: PASS (`265 files already formatted`, `All checks passed!`, all hooks Passed)
- Unit tests: 1401 passed / 0 failed / 0 warnings. New `TestDedupeEntitiesParallelization` = 6 passed.
- Integration (full, `-all`, slow + requires_mongot, quiesced + isolated stack): 266 passed / 1 skipped
  / 0 failed / 0 warnings — exit 0, 613s. Regression guards: `test_extraction_pipeline.py` 9 passed
  (real parallel `_dedupe_entities` through the pipeline), `test_dedup.py` 14 passed (live `$vectorSearch`).
  Known `test_web_serp` flake did NOT trigger (3 passed); no Prefect stderr issue.

**Acceptance criteria**
- [x] PASS — Concurrent under `Semaphore(dedup_concurrency)`, no sequential loop —
      `test_decisions_run_concurrently_under_semaphore` (max_in_flight==bound) +
      `test_no_sequential_await_loop_remains`; diff-verified pipeline.py:904/936.
- [x] PASS — Identical `decisions` + `n_merged/n_flagged/n_none` vs sequential oracle —
      `test_identical_output_to_sequential_reference` (15-entity fixture, all 3 buckets); oracle
      confirmed faithful by reading; per-field equality asserted.
- [x] PASS — `TREE_EXTRACTION__DEDUP_CONCURRENCY=1` reproduces sequential —
      `test_concurrency_one_reproduces_sequential`.
- [x] PASS — No Voyage embed in dedupe task — `test_no_embed_call_in_dedupe_task` + grep of body;
      `dedup.py` untouched (not in `git diff --name-only`).
- [x] PASS — format/lint/pre-commit clean.
- [x] PASS — unit + fast integration pass.
- [x] PASS — `make memory-integration-tests-all` (mongot stack up) — 266 passed / 1 skipped, exit 0.
- [ ] NOT RUN (per spec allowance) — [HUMAN/Tester] deployment-triggered
      `make memory-run-memory-pipeline-extraction` e2e: requires `serve-workflows` against the shared
      docker stack bound to another worktree — not feasible to run isolated right now. In-process
      integration coverage of the REAL parallel dedupe path PASSED (`test_extraction_pipeline.py`),
      the live `dedupe_entity` primitive PASSED (`test_dedup.py`), and the unit oracle proves
      `n_merged/n_flagged/n_none` count identity. Dedupe counts are sane. Awaiting human verification
      on a quiesced serve-workflows run, but no behavior-divergence risk surfaced.

**Other issues found**
- None. Clean refactor; per-key logic byte-identical, order recovered by original-order zip.

**VERDICT: PASS**
