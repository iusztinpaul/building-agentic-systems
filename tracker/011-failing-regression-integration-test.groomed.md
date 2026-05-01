# Failing regression integration test for `search_web` empty-results bug

Status: pending
Tags: `bug`, `tests`, `integration`, `regression`, `web`, `search`
Depends on: #010
Blocks: #012

## Scope

Per `CLAUDE.md`'s TDD-for-known-bugs rule and the project's "MCP tests belong in integration" memory, add a failing regression test that asserts the bug exists, so #012 can prove the fix by turning it green. **No production code changes** in this task.

The test lives in `apps/memory/tests/integration/data/web/test_web_serp.py` (the file already exists — this task adds one new test method). It hits the real Bright Data SERP API; it is gated on real (non-placeholder) `BRIGHTDATA_API_KEY` and `BRIGHTDATA_SERP_ZONE` via the existing module-level `pytest.mark.skipif` at the top of that file. Without those env vars, the whole module is skipped — CI without secrets stays green.

### What to add

Add **one** new async test method to the existing `TestLiveSerpSearch` class in `apps/memory/tests/integration/data/web/test_web_serp.py`:

```python
async def test_common_query_returns_at_least_one_organic_result(self) -> None:
    """Regression for the search_web empty-results bug.

    Prior to the #012 fix, ``search("pizza")`` against the configured
    Bright Data SERP zone returned ``[]`` despite the same zone+key+URL
    succeeding via direct ``curl``. This test asserts the fix sticks: a
    common, stable query must return ≥ 1 organic result.

    The query is intentionally generic so SERP drift over time does not
    flake the test. ``pizza`` matches the user's working curl exactly, so
    if it returns ``[]`` we have a real regression — not a deflated SERP.
    """

    results = await search("pizza", engine="google", num_results=10)

    assert len(results) >= 1, (
        "Expected ≥ 1 organic result for the stable query 'pizza'; got 0. "
        "This is the regression the fix in #012 must close — the user's "
        "curl with the same zone+key returns a populated SERP."
    )
    # Shape assertions stay loose: SERP content drifts.
    first = results[0]
    assert first.title.strip(), f"empty title at rank {first.rank}: {first}"
    assert first.url.startswith("http"), (
        f"non-http url at rank {first.rank}: {first.url}"
    )
```

### Why a separate test (and not amend the existing `test_returns_results_with_titles_and_urls`)

The existing test queries `openai gpt-4`, asserts `len(results) >= 1`, and (per the task #009 log) was passing on `main` at the time of merge. If it is *now* also failing, that's strong corroborating evidence for #010's diagnosis but is also outside this task's narrow scope. Adding a new, dedicated regression test scoped to the user-reported reproducer query (`pizza`) preserves the audit trail: the new test is the binding contract for #012, and the existing test remains the broader smoke test.

The new test does NOT replace or modify the existing test. The existing test stays exactly as is.

### Verification before handing off

Before marking this task done, the SWE must:

1. Confirm the new test is **RED** on `main` (i.e. without #012's fix). Run:
   ```bash
   ENV_FILE_PATH=$(pwd)/.env BRIGHTDATA_API_KEY=<real> BRIGHTDATA_SERP_ZONE=<real> \
     make memory-integration-tests
   ```
   And capture the failure: `assert len(results) >= 1` should fail with `assert 0 >= 1`. Pin the failing output (with credentials redacted) in the SWE log.
2. Confirm the test is correctly gated: with placeholder env vars it must be SKIPPED, not RUN-AND-FAIL. Run the same command with `BRIGHTDATA_SERP_ZONE=your-brightdata-serp-zone` and confirm the skip message in the output.
3. Run the existing `test_returns_results_with_titles_and_urls` and `test_empty_query_returns_empty_list` alongside the new test — observe which (if any) of those also fail in the current state and note it in the log. (Diagnosis context for #012; not blocking acceptance of this task.)

### Constraints

- The test must use the existing `from tree.data.web.web_serp import search` import (already in the file).
- The test must inherit the existing module-level `pytestmark = pytest.mark.skipif(...)` — do not add a per-test skip.
- No mocking. No `mocker` fixture. No `httpx` patching. This is a real-API integration test by design.
- Do NOT add a new MCP-tool-level integration test in this task. The MCP integration tests at `apps/memory/tests/integration/mcp/test_search_web_tool.py` already cover the tool surface; the bug is in the SERP client they wrap, so testing at the SERP layer is sufficient. (Revisit only if #010's diagnosis explicitly recommends an MCP-layer assertion — unlikely.)
- One SERP credit per test run. The whole module already costs ≤ 4 credits per run; one more is acceptable.
- No changes outside `apps/memory/tests/integration/data/web/test_web_serp.py`.

## Acceptance Criteria

- [ ] `apps/memory/tests/integration/data/web/test_web_serp.py::TestLiveSerpSearch::test_common_query_returns_at_least_one_organic_result` exists.
- [ ] The new test queries `"pizza"` (matching the user's working curl) with `engine="google"` and `num_results=10`, and asserts `len(results) >= 1` plus that the first result has a non-empty `title` and an `http`-prefixed `url`.
- [ ] Running the test against the live Bright Data SERP API on `main` (i.e. without the #012 fix) produces a FAIL with `assert 0 >= 1`. Failing output captured in the SWE log (API key redacted).
- [ ] Running the test with placeholder env vars produces SKIPPED, not FAIL. Skip output captured in the SWE log.
- [ ] The existing tests in the same module (`test_returns_results_with_titles_and_urls`, `test_empty_query_returns_empty_list`) are NOT modified and still run as before.
- [ ] No production source files (`apps/memory/src/**`) are modified.
- [ ] No unit tests or other integration tests are modified.
- [ ] `make memory-format-check && make memory-lint-check && make memory-unit-tests && make pre-commit` all pass. Output captured in the SWE log.
- [ ] [HUMAN] Confirm `BRIGHTDATA_API_KEY` and `BRIGHTDATA_SERP_ZONE` are set to non-placeholder values in `.env` for the SWE to run the live integration test.

## User Stories

### Story: Test verifies the bug is real before the fix lands

1. SWE in the worktree runs `make memory-integration-tests` with real Bright Data credentials in `.env`.
2. The new `test_common_query_returns_at_least_one_organic_result` runs and FAILS with `assert 0 >= 1`.
3. SWE pins the failure in the log — the regression is now empirically captured.

### Story: Test verifies the fix in #012

1. SWE on #012 lands the fix from the diagnosis in #010.
2. SWE re-runs `make memory-integration-tests`.
3. The previously-failing test now passes: `len(results) >= 1`, first result has title + http URL.
4. The regression is closed and the green test guards against re-introduction.

### Story: Test is gated for CI without secrets

1. CI runs `make memory-integration-tests` with placeholder env vars (or none).
2. The test is SKIPPED with the reason `BRIGHTDATA_API_KEY / BRIGHTDATA_SERP_ZONE not configured (or set to placeholder)`.
3. CI stays green; secrets are not required to land the failing test on the feature branch.

### Story: A future regression won't go unnoticed

1. Six months from now someone refactors `web_serp.py` and accidentally re-introduces the empty-results behavior.
2. They run `make memory-integration-tests`.
3. `test_common_query_returns_at_least_one_organic_result` fails with the explanatory assertion message naming "the regression the fix in #012 must close".
4. The refactor is reverted or fixed before merge.

---

Blocked by: #010

## Log

(empty — SWE will append on pickup)
