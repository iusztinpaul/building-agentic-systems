# 060 — Retire stale vector-space runbook test

## Scope

`apps/memory/tests/integration/memory/test_dream_e2e_acceptance.py` defined
`test_vector_space_swap_runbook_is_discoverable`, which asserted that the
"Vector-space change" runbook (and the string `"vector space"`) is present in
`CLAUDE.md`. Commit `d9c9b7b` ("feat: Update CLAUDE.md") **intentionally
removed** that 26-line runbook block from `CLAUDE.md`, so the guarding test now
fails on `origin/main` itself — it guards documentation that was deliberately
deleted.

User-approved cleanup: retire (delete) the stale test, consistent with the
project principle "Always prioritize removing instructions over adding more".

1. Delete the `test_vector_space_swap_runbook_is_discoverable` function (and
   only that function — it is standalone and fixture-less, with a function-local
   `from pathlib import Path` that goes with it). Leave all other tests in the
   file untouched.
2. Confirm no other test references the removed runbook text / function.
3. Create this tracker file with a `### [SWE] ... — Implementation` log entry.

## Acceptance Criteria

- [x] `test_vector_space_swap_runbook_is_discoverable` no longer exists (grep returns nothing).
- [x] No other test asserts `"vector space"` presence in CLAUDE.md (grep to confirm).
- [x] The rest of `test_dream_e2e_acceptance.py` is unchanged (diff shows only the one function removed).
- [x] `make memory-format-fix && make memory-lint-fix && make memory-format-check && make memory-lint-check && make pre-commit` clean.
- [x] `make memory-unit-tests` passes.

## Log

### [SWE] 2026-05-21 — Implementation

**Files modified**
- `apps/memory/tests/integration/memory/test_dream_e2e_acceptance.py` — deleted the standalone, fixture-less `test_vector_space_swap_runbook_is_discoverable` function (with its function-local `from pathlib import Path`); updated the now-stale section header comment (`#048 routing + #049 runbook discoverability` → `#048 routing`) since the #049 discoverability test it named is gone.
- `tracker/060-retire-stale-vector-space-test.in-progress.md` — this tracker file.

**Tests**
- Unit: 1340 passing, 0 failing, 0 warnings — `make memory-unit-tests` (output below).
- Integration: N/A here — only a stale integration test was deleted; no source/infra behavior changed. The Tester runs the full `integration-tests-all` suite at the acceptance gate and confirms the previously-failing test is gone and the suite is green (modulo the known unrelated `test_web_serp` network flake).

**Acceptance criteria**
- [x] `test_vector_space_swap_runbook_is_discoverable` no longer exists — `grep -rn "test_vector_space_swap_runbook_is_discoverable" apps/memory/` returns nothing (exit 1).
- [x] No other test asserts `"vector space"` presence in CLAUDE.md — `grep -rn '"vector space"' apps/memory/tests/` returns nothing (exit 1). (The `test_pipeline.py:754` `persisted-vector space` match is an unrelated docstring fragment, not a CLAUDE.md assertion.)
- [x] Rest of file unchanged — `git diff` shows only the one function removed plus the corresponding stale comment cleanup.
- [x] Format/lint/pre-commit clean.
- [x] `make memory-unit-tests` passes.

**Evidence**

```
$ grep -rn "test_vector_space_swap_runbook_is_discoverable" apps/memory/
exit: 1   (no matches)

$ grep -rn '"vector space"' apps/memory/tests/
exit: 1   (no matches)

$ git diff apps/memory/tests/integration/memory/test_dream_e2e_acceptance.py
@@ -712,8 +712,8 @@
 # ===========================================================================
-# #048 routing + #049 runbook discoverability (cheap, no mongot needed but
-# kept in-suite so the headline acceptance file is self-contained)
+# #048 routing (cheap, no mongot needed but kept in-suite so the headline
+# acceptance file is self-contained)
 # ===========================================================================
@@ -725,17 +725,6 @@ def test_search_embedding_model_routes_through_voyage_text_client() -> None:
     assert isinstance(model, VoyageTextEmbeddingModel)
-
-
-def test_vector_space_swap_runbook_is_discoverable() -> None:
-    """The #049 vector-space-swap runbook is present in CLAUDE.md."""
-
-    from pathlib import Path
-
-    # apps/memory/tests/integration/memory/<this> → repo root is 5 parents up.
-    repo_root = Path(__file__).resolve().parents[5]
-    claude_md = (repo_root / "CLAUDE.md").read_text(encoding="utf-8")
-    assert "vector space" in claude_md

$ make memory-format-fix && make memory-lint-fix && make memory-format-check && make memory-lint-check && make pre-commit
... ruff format: 261 files left unchanged
... ruff lint-fix: All checks passed!
... ruff format-check: 261 files already formatted
... ruff lint-check: All checks passed!
... pre-commit: prettier/ruff check/ruff format/biome/KGQuery discipline all Passed

$ make memory-unit-tests
============================ 1340 passed in 41.66s =============================
```

**Notes**
- Pure test-retirement; no source code touched, so no TDD red/green dance applies.
- The section header comment update is in-scope cleanup (it named the deleted #049 discoverability test); kept minimal — only the comment text changed.
- DID NOT COMMIT — handing back to the Tester for review.

### [Tester] 2026-05-21 12:20 — QA

**Test summary**
- Format / lint / pre-commit: PASS (`make pre-commit` — prettier/ruff check/ruff format/biome/KGQuery discipline all Passed; no files to fix)
- Unit tests: 1340 passed / 0 failed / 0 warnings (`make memory-unit-tests`, 41.81s)
- Integration tests (acceptance gate `make memory-integration-tests-all`): 259 passed / 0 failed / 1 skipped / 0 warnings (589.75s, full stack incl. mongot + slow)
- Warnings: 0

**Headline before/after (the reason this task exists)**
- BEFORE (origin/main & HEAD base): `test_dream_e2e_acceptance.py::test_vector_space_swap_runbook_is_discoverable` exists at line 728 and runs `assert "vector space" in claude_md`. Verified `grep -ni "vector space" CLAUDE.md` → NOT FOUND (commit d9c9b7b removed the runbook), so that assert fails → the documented pre-existing `1 failed`.
- AFTER (this diff): function gone; `test_dream_e2e_acceptance.py` collects 8 tests, all green (`........`). The previously-failing test is GONE, not skipped/xfail — it no longer exists. Suite otherwise green.

**E2E adversarial pass** (this is a test-retirement; "the feature" is the suite itself + the no-resurfacing invariant)
- Happy path: `make memory-integration-tests-all` → 259 passed, 1 skipped, 0 failed. PASS.
- Break path 1 (resurrection of the deleted name): `grep -rn "test_vector_space_swap_runbook_is_discoverable" apps/memory/` → exit 1, no matches. PASS.
- Break path 2 (sibling that still asserts CLAUDE.md content → would re-fail): `grep -rn '"vector space"' apps/memory/tests/` → exit 1; case-insensitive `grep -rni "vector.space"` matches only `test_pipeline.py:754` ("persisted-vector space" docstring), `test_e2e_embedding_split_and_batching.py` ($vectorSearch retrieval-agreement asserts, NOT CLAUDE.md); `grep -rn "claude_md\|read_text.*CLAUDE"` → no `read_text(CLAUDE.md)` anywhere. No sibling will re-fail. PASS.
- Break path 3 (diff scope creep): `git diff --stat` → only `test_dream_e2e_acceptance.py` (2 ins / 13 del); full `git diff` = exactly the one function + its function-local `from pathlib import Path` removed, plus the 2-line section-header comment trim (`#048 routing + #049 runbook discoverability` → `#048 routing`). No other file, no other test touched. PASS.
- Known-flake watch: `test_web_serp.py` ran `...` (3 passed) — the documented network flake did NOT appear; no re-run needed.

**Acceptance criteria**
- [x] PASS — `test_vector_space_swap_runbook_is_discoverable` no longer exists — `grep -rn` returns exit 1 (no matches); diff confirms removal.
- [x] PASS — No other test asserts `"vector space"` presence in CLAUDE.md — `grep -rn '"vector space"' apps/memory/tests/` exit 1; case-insensitive sweep + `read_text` grep confirm no sibling CLAUDE.md assertion.
- [x] PASS — Rest of `test_dream_e2e_acceptance.py` unchanged — `git diff` shows only the one function + its local import removed and the matching section-header comment trim.
- [x] PASS — Format/lint/pre-commit clean — `make pre-commit` all hooks Passed.
- [x] PASS — `make memory-unit-tests` passes — 1340 passed, 0 warnings.

**Evidence**
```
$ grep -rn "test_vector_space_swap_runbook_is_discoverable" apps/memory/   # exit 1, no matches
$ grep -rn '"vector space"' apps/memory/tests/                              # exit 1, no matches
$ grep -ni "vector space" CLAUDE.md                                        # NOT FOUND (confirms the deleted assert was the red)

$ make memory-unit-tests
============================ 1340 passed in 41.81s =============================

$ make memory-integration-tests-all
tests/integration/memory/test_dream_e2e_acceptance.py ........            [ 57%]
================== 259 passed, 1 skipped in 589.75s (0:09:49) ==================
```

**Other issues found** (PASS-with-note — do NOT block; orchestrator/SWE may tidy in a follow-up)
- Stale module-docstring reference: `test_dream_e2e_acceptance.py:28-29` still enumerates "the #049 runbook discoverability" in the module's coverage list, but the test that proved it was just deleted. The SWE trimmed the inline section-header comment (line 715) but missed this top-of-file docstring leftover. Purely cosmetic — no functional impact, suite green, no AC requires docstring accuracy. Suggested one-line fix: drop ", the #049 runbook discoverability" from line 29 so the docstring matches reality.

**VERDICT: PASS**
