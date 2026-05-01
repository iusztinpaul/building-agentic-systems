# Feature Plan: Fix `search_web` MCP tool returning empty results

## Summary

The `search_web` MCP tool wraps `tree.data.web.web_serp.search`, which posts to Bright Data's SERP REST endpoint (`POST https://api.brightdata.com/request`) with `format: "raw"` and a Google URL containing `brd_json=1`. Calling the tool returns `{"query": "...", "engine": "google", "results": []}` even though the same zone + key + base URL succeed via direct `curl`. We need to reproduce the bug locally, diagnose what response shape Bright Data actually returns for this zone, fix the SERP client (request and/or parser), and harden the empty-result path so future regressions surface as a clear log/error rather than a silent `[]`.

The decomposition is investigation-first because the user's hypothesis (the client sends `format=markdown`) does not match the shipped code (`format: "raw"`). The real failure mode must be confirmed empirically before the fix is written — otherwise the fix will be a guess and the regression test will be shaped wrong. Each task ships independently and leaves the codebase in a working state.

## Tasks (in order)

1. **#010** — Reproduce + diagnose `search_web` empty-results bug — Write a one-shot diagnostic script that exercises `tree.data.web.web_serp.search` and a parallel direct `httpx` POST against the configured `BRIGHTDATA_SERP_ZONE`, capture the exact request payload and the exact response body Bright Data returns (status, content-type, first 2 KB of body, parsed JSON keys if applicable). Produces a written diagnosis pinned in the task log: which response shape Bright Data actually returns for this zone, why `_parse_organic` returns `[]`, and the recommended fix vector (parser change vs request-shape change vs both). No production code changes in this task.
2. **#011** — Failing regression integration test for `search_web` — Add an integration test that hits the real Bright Data SERP API with the configured zone and asserts ≥1 organic result for a stable query (e.g. `pizza`). The test must be RED on the current `main` branch (i.e. fail with `assert len(results) >= 1` because `results == []`). Lives in `apps/memory/tests/integration/data/web/test_web_serp.py` next to existing live tests; gated on real (non-placeholder) `BRIGHTDATA_API_KEY` + `BRIGHTDATA_SERP_ZONE`. Depends on #010 (diagnosis informs whether a separate MCP-tool-level integration test is also warranted). No production code changes.
3. **#012** — Fix the SERP client per the diagnosis from #010 — Adjust the request shape and/or the response parser in `apps/memory/src/tree/data/web/web_serp.py` so `search("pizza")` returns ≥1 organic result against the configured zone. Acceptance = the failing regression test from #011 turns green; all existing unit tests in `tests/unit/data/web/test_web_serp.py` (32 tests) and existing integration tests in `tests/integration/data/web/test_web_serp.py` stay green; existing search_web MCP tool unit/integration tests stay green; MCP tool's public signature and JSON envelope unchanged. Depends on #010 + #011.
4. **#013** — Tighten the empty-result error path so silent `[]` becomes observable — Distinguish "Bright Data returned a parseable response with zero organic entries" (legitimate empty SERP — return `[]`, log at INFO) from "Bright Data returned a non-JSON or unexpected-shape response we couldn't parse" (regression signal — log at WARNING with status, content-type, first 200 chars of body; still return `[]` so the public contract is preserved). Add unit tests covering both paths. Depends on #012 (the fix should land first so the new logging fires only on genuinely unexpected shapes).

## Out of scope (intentional)

- Web Unlocker path (`BRIGHTDATA_UNLOCKER_ZONE`, `tree.data.web.web_unlocker`). Untouched.
- New search engines beyond Google / Bing / Yandex (already shipped in #006).
- Changes to the MCP tool's public signature, default arguments, or JSON envelope shape (`{"query", "engine", "results"}`).
- Refactoring the FastMCP server registration or `tree.data.web.__init__` re-exports.
- New settings or env vars (`.env.example` already has `BRIGHTDATA_SERP_ZONE`).
- Adding pagination, locale, or async-mode features beyond what #006/#007/#008/#009 already shipped.

## Open questions

None blocking. The investigation in #010 is explicitly designed to answer the one open question (what Bright Data actually returns for this zone) before #012 commits to a fix.

---

## [PM] 2026-05-01 16:23 — Feature Acceptance Summary

**VERDICT: ACCEPT** (feature-level)

All four tasks (#010, #011, #012, #013) individually ACCEPTED — see per-task `[PM] Acceptance Review` log entries.

**User-perspective evidence (independently reproduced by PM):**
- `make memory-search-web QUERY="Harness Engineering"` → **10 real organic results** (top: martinfowler.com, openai.com, addyosmani.com); JSON envelope well-formed; zero google/gstatic/googleusercontent infra URLs leaked.
- `make memory-unit-tests` → **442 passed in 22.01s, 0 warnings**.
- `make memory-integration-tests` → **70 passed in 137.23s (0:02:17), 0 failed, 0 skipped** — the full integration suite, including all 3 live `TestLiveSerpSearch` tests and both live `tests/integration/mcp/test_search_web_tool.py` tests.

**Commit log:** 4 commits, in plan order, each with a `Closes-tracker:` trailer (`7efe2a6 → 010`, `e0d02fe → 011`, `647f512 → 012`, `4c12937 → 013`).

**Silently-dropped scope check:** none. The 4 feature commits touch only `apps/memory/src/tree/data/web/web_serp.py`, `apps/memory/tests/unit/data/web/test_web_serp.py`, `apps/memory/tests/integration/data/web/test_web_serp.py`, `apps/memory/scripts/diagnose_search_web.py`, and tracker files — exactly the scope the plan defined. "Out of scope (intentional)" items remain out of scope and weren't quietly bundled in.

**Headline user story bound to a passing test:** "search_web returns ≥1 result for queries the curl reference returns results for" → `tests/integration/data/web/test_web_serp.py::TestLiveSerpSearch::test_common_query_returns_at_least_one_organic_result` (RED on main, GREEN post-#012, GREEN in tonight's full integration run).

**Operator-observability story bound to passing tests:** future regressions to the response shape will surface as a WARNING with engine/status/content_type/body_preview, not as silent `[]` — bound to `TestSearchEmptyResultLogging` (4 unit-test cases all GREEN).

The user who typed *"use the tree mcp to search the web for 'Harness Engineering'"* will now get exactly what they expected. Pipeline cleared to push and proceed to On-Call + PR Reviewer gates.
