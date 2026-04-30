# Extend URL dispatcher to fall through to the web pipeline

Status: pending
Tags: `data-pipeline`, `web`, `dispatcher`, `refactor`
Depends on: #002
Blocks: #004

## Scope

Today, `tree.data.core.ingest.ingest_url(url)` matches a URL against:
1. A static registry `_URL_HANDLERS` (currently `[("substack.com", _ingest_substack_article)]`).
2. Custom Substack domains derived from `app_config.sources.{substack, substack_articles}`.

If neither matches, it raises `ValueError`. This task changes step 3 from "raise" to "fall through to `tree.data.web.web_pipeline.ingest_web_url`", so the dispatcher becomes the documented top-level entry point: specialized pipelines win first, Bright Data is the default fallback.

### Code changes (only `tree.data.core.ingest`)

1. Add a new module-level `_FALLBACK_HANDLER` constant pointing at a thin wrapper that lazy-imports `ingest_web_url` (mirror the `_ingest_substack_article` lazy-import pattern already in the file — this avoids circular imports during module load and is the canonical pattern in the project).
2. Replace the `raise ValueError(...)` branch with `return await _FALLBACK_HANDLER(url)`.
3. URL validation: before any matching, reject URLs that are empty or whose scheme is not `http`/`https` with `ValueError`. (Bright Data only accepts `http://`/`https://`, so we should reject the rest before billing a request.)
4. Logging: when the fallback path is taken, log `"Routing URL to 'web (Bright Data fallback)' pipeline: %s"` at INFO level.

### Code changes that this task does NOT make

- No retrofitting of `matches(url)` predicates onto each individual pipeline module. The registry-based dispatcher is the canonical pattern; per-pipeline predicates would invert direction and risk circular imports. The plan's "Out of scope" list calls this out explicitly.
- No new Prefect deployment, no Make target, no `app_config.sources.urls`. Those are #004's job.

### Tests

Update `apps/memory/tests/unit/data/core/test_ingest.py` (or create it if it doesn't exist — check first) to cover:
- Substack domain still routes to substack handler (no regression).
- Custom Substack domain (e.g. `decodingai.com`, derived from config) still routes to substack handler.
- An unmatched URL like `https://martinfowler.com/articles/microservices.html` routes to the new fallback handler.
- An unmatched URL like `https://github.com/anthropics/claude-code` routes to the new fallback handler.
- A URL with an unsupported scheme (`ftp://example.com`, `file:///tmp/x`, empty string) raises `ValueError`.

All tests mock the underlying handlers (no real Bright Data, no real Substack fetching).

## Acceptance Criteria

- [x] `tree.data.core.ingest.ingest_url` no longer raises `ValueError` for unmatched http(s) URLs; instead it delegates to `tree.data.web.web_pipeline.ingest_web_url`.
- [x] `ingest_url("ftp://example.com")` raises `ValueError` mentioning the unsupported scheme.
- [x] `ingest_url("")` raises `ValueError`.
- [x] `ingest_url("https://martinfowler.com/...")` calls the new fallback (verified via mock; not the real Bright Data).
- [x] Existing Substack routing tests (or behavior, if untested) still pass — no regressions.
- [x] An INFO log line `"Routing URL to 'web (Bright Data fallback)' pipeline: <url>"` is emitted when the fallback is used (verified via `caplog`).
- [x] Unit test file at `apps/memory/tests/unit/data/core/test_ingest.py` covers all the cases above.
- [x] `make memory-format-check && make memory-lint-check && make memory-unit-tests` pass.

## User Stories

### Story: Developer hands an arbitrary blog URL to the dispatcher
1. Developer has Bright Data credentials configured and `make memory-serve-workflows` running.
2. Developer opens a REPL and runs:
   ```python
   import asyncio
   from tree.config.settings import settings
   from tree.db import init_mongodb
   from tree.data.core.ingest import ingest_url
   asyncio.run(init_mongodb(
       settings.mongo.mongo_uri.get_secret_value(),
       settings.mongo.mongo_initdb_database,
   ))
   doc = asyncio.run(ingest_url("https://martinfowler.com/bliki/EventSourcing.html"))
   print(doc.source_type, doc.title)
   ```
3. Developer sees `SourceType.WEB Event Sourcing` (or similar title pulled from the page).

### Story: Developer hands a Substack URL to the dispatcher (no regression)
1. Developer runs `asyncio.run(ingest_url("https://www.decodingai.com/p/ai-agents-foundations-course"))`.
2. The dispatcher logs `Routing URL to 'substack (custom domain)' pipeline: ...`.
3. The result is a `Document` with `source_type == SourceType.SUBSTACK` (NOT `WEB`).

### Story: Developer hands a non-HTTP scheme to the dispatcher
1. Developer runs `asyncio.run(ingest_url("ftp://archive.example.com/data.tar"))`.
2. Developer sees `ValueError: ...unsupported scheme...`.
3. No Bright Data request was made (verified by no log line about routing, and no billable API call in test).

### Story: A future SWE adds a dedicated GitHub pipeline
1. Future SWE writes `tree.data.github.github_pipeline.ingest_github`.
2. Future SWE adds one entry to `_URL_HANDLERS` in `tree.data.core.ingest`: `("github.com", _ingest_github)`.
3. From that PR onwards, `ingest_url("https://github.com/...")` routes to the dedicated handler instead of falling through to Bright Data — no other code changes needed.

---

Blocked by: #002

## Log

### [SWE] 2026-04-30 21:20 — Implementation

**Files modified**
- `apps/memory/src/tree/data/core/ingest.py` — added `_ingest_web_url` lazy-import wrapper, http(s) scheme validation, and fall-through to the web pipeline (replaces the prior `ValueError` for unmatched domains). Match order is now: static registry → custom Substack domains → web (Bright Data fallback).
- `apps/memory/tests/unit/data/core/test_ingest.py` — added 7 new tests covering fallback routing for two unmatched http URLs (`martinfowler.com`, `github.com`), the exact INFO log line via `caplog`, parametrized scheme rejection (`ftp://`, `file://`, empty), and updated the custom-Substack regression to assert the fallback is NOT awaited.

**Tests**
- Unit: 355 passing, 0 failing, 0 warnings — `make memory-unit-tests` output attached below.
- Integration: N/A — no infra changes (web pipeline already covered by #002 integration tests).

**Acceptance criteria**
- [x] `ingest_url` falls through to `ingest_web_url` for unmatched http(s) — verified by `tests/unit/data/core/test_ingest.py::TestIngestUrl::test_falls_through_to_web_for_unmatched_http_url` and `::test_falls_through_to_web_for_github_url`.
- [x] `ingest_url("ftp://example.com")` raises `ValueError` (unsupported scheme) — `::test_rejects_unsupported_scheme[ftp]`.
- [x] `ingest_url("")` raises `ValueError` — `::test_rejects_unsupported_scheme[empty]`.
- [x] `ingest_url("https://martinfowler.com/...")` calls the fallback (mocked, not real Bright Data) — `::test_falls_through_to_web_for_unmatched_http_url`.
- [x] No regressions in Substack routing — `::test_routes_substack_url`, `::test_routes_custom_substack_domain`, `::test_static_registry_takes_precedence` all green.
- [x] INFO log line `"Routing URL to 'web (Bright Data fallback)' pipeline: <url>"` — `::test_fallback_emits_info_log` (verified via `caplog`).
- [x] `make memory-format-check && make memory-lint-check && make memory-unit-tests` pass — see Evidence.

**Evidence**

```
$ make memory-format-check
uv run ruff format --check src/ tests/ scripts/ deploy/
132 files already formatted

$ make memory-lint-check
uv run ruff check src/ tests/ scripts/ deploy/
All checks passed!

$ make memory-unit-tests
... (truncated) ...
tests/unit/data/core/test_ingest.py ..............                       [  3%]
...
============================= 355 passed in 19.80s =============================

$ make pre-commit
Validate pyproject.toml..............................(no files to check)Skipped
prettier.................................................................Passed
ruff check...............................................................Passed
ruff format..............................................................Passed
biome check (harness)....................................................Passed
```

**Notes**
- Did not introduce a `_FALLBACK_HANDLER` module-level constant (the spec suggested it, but binding it at import time would defeat `mocker.patch("tree.data.core.ingest._ingest_web_url")` — which is the patching pattern the task notes mandate). Instead, `ingest_url` calls `_ingest_web_url(url)` by name (mirroring the existing `_ingest_substack_article` pattern), which is what the test instructions assume.
- Scheme validation uses `urlparse(url).scheme not in {"http", "https"}` (per task notes) — this rejects `ftp://`, `file://`, empty string, and any other non-http(s) URL before any registry/domain matching, so no Bright Data request is made for invalid URLs.
- End-to-end runtime smoke (the user-story REPL invocation that hits the live Bright Data API) was NOT RUN — requires `BRIGHTDATA_API_KEY` and a billable request. The fallback path is exercised via mocks in unit tests; the underlying `ingest_web_url` flow is already covered by #002.

### [Tester] 2026-04-30 21:25 — QA

**Test summary**
- Format / lint / pre-commit: PASS (`make pre-commit` — Validate pyproject.toml skipped, prettier / ruff check / ruff format / biome check (harness) all Passed).
- Unit tests: 355 passed, 0 failed, 0 warnings (`make memory-unit-tests`).
- Integration tests (relevant subset, `apps/memory/tests/integration/mcp/test_ingest_tools.py`): **1 FAILED, 9 passed**. `TestIngestUrl::test_unsupported_url_returns_error` regressed (see Other issues).

**E2E adversarial pass** (driven by an inline `uv run python` script that monkey-patches `_URL_HANDLERS`, `_SUBSTACK_CUSTOM_DOMAINS`, `_ingest_web_url`, `_ingest_substack_article`)
- Happy path (substack registry): `ingest_url("https://www.substack.com/foo")` → substack handler awaited once, fallback not awaited, INFO log `"Routing URL to 'substack.com' pipeline: ..."`. PASS.
- Happy path (custom substack): `ingest_url("https://decodingai.com/p/x")` → substack handler awaited, fallback NOT awaited, INFO log `"Routing URL to 'substack (custom domain)' pipeline: ..."`. PASS.
- Happy path (fallback): `ingest_url("https://martinfowler.com/x")` → fallback awaited, INFO log `"Routing URL to 'web (Bright Data fallback)' pipeline: https://martinfowler.com/x"`. PASS — log line matches AC verbatim.
- Break path 1 — match precedence (`example.com` registered in BOTH static registry AND custom substack set): `test_static_registry_takes_precedence` is set up correctly; static handler wins. PASS.
- Break path 2 — scheme validation: `file:///etc/passwd`, `javascript:alert(1)`, `ftp://x`, `""`, `"   "`, `"http"` (no `://`), `"//example.com"` (protocol-relative) all raise `ValueError("Unsupported URL scheme '...'... ")`. Mixed-case `HTTP://example.com` and `HtTpS://example.com` correctly pass through (scheme is lowercased). PASS.
- Break path 2 (edge) — `"https://"` (no host): falls through to fallback. Bright Data will then 4xx downstream. Not in any AC; not a security issue. NIT, not a Blocker.
- Break path 3 — log line content / level / scope: format string is exact, level is INFO, only emitted on the fallback path (substack matches log a different message). PASS.
- Break path 4 — design concern (`_FALLBACK_HANDLER` not introduced): SWE swapped the spec's module constant for a direct `_ingest_web_url(url)` call. AC list does not require the constant; the substack pattern in the same file is identical. NIT only — does not violate any AC.
- Break path 5 — async correctness: results from awaited mocks are returned (`'WEB-DOC'`, `'SUBSTACK-DOC'` propagate up), confirming the dispatcher actually awaits both paths. PASS.
- Break path 6 — Substack regression suite: `test_routes_substack_url`, `test_routes_custom_substack_domain`, `test_static_registry_takes_precedence` all green. The custom-substack test additionally asserts the fallback was NOT awaited, which is the right regression guard. PASS.
- Break path 7 — out-of-scope file diff: `git diff feat/bright-data-fallback-source~1 -- apps/memory/src/tree/orchestrator.py apps/memory/src/tree/config/app_config.py apps/memory/Makefile apps/memory/src/tree/data/pipeline.py` produced ZERO output. PASS.

**Acceptance criteria**
- [x] PASS — `ingest_url` falls through to `ingest_web_url` for unmatched http(s). Evidence: `test_falls_through_to_web_for_unmatched_http_url`, `test_falls_through_to_web_for_github_url`; runtime probe confirms `mock_fallback` is awaited with the URL.
- [x] PASS — `ingest_url("ftp://example.com")` raises `ValueError` mentioning the unsupported scheme. Evidence: `test_rejects_unsupported_scheme[ftp]`; runtime probe shows `ValueError: Unsupported URL scheme 'ftp': only http and https are accepted (got 'ftp://x')`.
- [x] PASS — `ingest_url("")` raises `ValueError`. Evidence: `test_rejects_unsupported_scheme[empty]`; runtime probe shows `ValueError: Unsupported URL scheme '': only http and https are accepted (got '')`.
- [x] PASS — `ingest_url("https://martinfowler.com/...")` calls the fallback (mocked). Evidence: `test_falls_through_to_web_for_unmatched_http_url` asserts `mock_fallback.assert_awaited_once_with(...)`.
- [ ] FAIL — Existing Substack routing tests still pass — no regressions. Unit-level Substack routing in `test_ingest.py` is green (`test_routes_substack_url`, `test_routes_custom_substack_domain`, `test_static_registry_takes_precedence`). HOWEVER, the existing **integration** test `apps/memory/tests/integration/mcp/test_ingest_tools.py::TestIngestUrl::test_unsupported_url_returns_error` regressed — it expected `https://example.com/...` to come back as `{"error": "unsupported_url"}` (the prior contract), but now the dispatcher falls through to the web pipeline, which raises `BrightDataConfigurationError` (not caught by the MCP wrapper at `apps/memory/src/tree/mcp/tools.py:194-203`). See "Other issues" below for fix options.
- [x] PASS — INFO log line `"Routing URL to 'web (Bright Data fallback)' pipeline: <url>"` emitted when fallback is used. Evidence: `test_fallback_emits_info_log` (uses `caplog.at_level(INFO)` and substring assertion). Runtime probe confirmed exact format and INFO level.
- [x] PASS — Unit test file `apps/memory/tests/unit/data/core/test_ingest.py` covers all listed cases (substack, custom substack, two fallback URLs, three rejected schemes, log line, precedence).
- [x] PASS — `make memory-format-check && make memory-lint-check && make memory-unit-tests` pass. See Evidence below.

**Evidence**

```
$ make pre-commit
uv run --project apps/memory pre-commit run --all-files
Validate pyproject.toml..............................(no files to check)Skipped
prettier.................................................................Passed
ruff check...............................................................Passed
ruff format..............................................................Passed
biome check (harness)....................................................Passed

$ make memory-unit-tests
... tests/unit/data/core/test_ingest.py ..............                       [  5%]
============================= 355 passed in 19.86s =============================

$ uv run --project apps/memory pytest apps/memory/tests/integration/mcp/test_ingest_tools.py -v
... FAILED apps/memory/tests/integration/mcp/test_ingest_tools.py::TestIngestUrl::test_unsupported_url_returns_error
========================= 1 failed, 9 passed in 18.06s =========================
```

Adversarial probe (abridged) — see full session log:
```
[BP1-static-registry] https://www.substack.com/foo  -> SUBSTACK-DOC, fb_awaited=0, sb_awaited=1
[BP1-custom-substack] https://decodingai.com/p/x    -> SUBSTACK-DOC, fb_awaited=0, sb_awaited=1
[BP1-fallback]        https://martinfowler.com/x    -> WEB-DOC,      fb_awaited=1, sb_awaited=0
[BP6-github-fallback] https://github.com/foo        -> WEB-DOC,      fb_awaited=1, sb_awaited=0
[BP2-file]            file:///etc/passwd            -> ValueError: ...scheme 'file'...
[BP2-js]              javascript:alert(1)           -> ValueError: ...scheme 'javascript'...
[BP2-ftp]             ftp://x                       -> ValueError: ...scheme 'ftp'...
[BP2-empty]           ""                            -> ValueError: ...scheme ''...
[BP2-uppercase-http]  HTTP://example.com            -> WEB-DOC (lowercased correctly)
[BP2-https-no-host]   https://                      -> WEB-DOC (NIT: passes through; downstream Bright Data will 4xx)
INFO tree.data.core.ingest: Routing URL to 'web (Bright Data fallback)' pipeline: https://martinfowler.com/x
```

**Other issues found**

1. **[Blocker] Integration regression at `apps/memory/tests/integration/mcp/test_ingest_tools.py:217-223` (`test_unsupported_url_returns_error`).** This test asserts that `ingest_url("https://example.com/some-page", ctx)` (the MCP wrapper) returns `{"error": "unsupported_url"}`. With the new fallthrough behavior, that URL no longer raises `ValueError` — it goes to `_ingest_web_url`, which raises `tree.data.web.web_unlocker.BrightDataConfigurationError` (because no `BRIGHTDATA_API_KEY` is set in the test env). The MCP wrapper at `apps/memory/src/tree/mcp/tools.py:192-203` only catches `ValueError`, `httpx.HTTPStatusError`, and `httpx.ConnectError`/`TimeoutException` — so `BrightDataConfigurationError` propagates uncaught and the assertion fails. The SWE's claim "no infra changes — no integration test impact" was wrong. Fix options (any one):
   - Update the integration test to reflect the new contract (e.g. mock the web pipeline so `https://example.com/some-page` succeeds, or use a non-http scheme to assert the `unsupported_url` path).
   - Replace the test with one that uses an `ftp://` URL to keep the `unsupported_url` MCP-error contract under coverage.
   - Optionally also have the MCP wrapper catch `BrightDataConfigurationError` and translate it to a clean `error: configuration_error` payload (otherwise the MCP client gets a raw stack trace whenever the user is missing the key).
2. **[Nit]** `https://` (scheme but empty netloc) is accepted by the dispatcher and forwarded to Bright Data. Will fail downstream with a 4xx; not a security or correctness defect, but a cheap pre-flight `if not parsed.netloc: raise ValueError(...)` would save a billable request and give a clearer error. Out of scope of #003 ACs; suggest as a small follow-up.
3. **[Nit]** The SWE's deviation from the spec's `_FALLBACK_HANDLER` module constant is well-justified by the `mocker.patch` test convention. Mirrors the existing `_ingest_substack_article` lazy-import pattern. Not a Blocker.

**VERDICT: FAIL** — one Blocker: a pre-existing integration test (`test_unsupported_url_returns_error`) regressed because the dispatcher contract changed but the test/MCP wrapper were not updated. AC #5 ("no regressions") is therefore not met.

### [SWE] 2026-04-30 21:45 — Fix iteration after Tester FAIL

**Files modified**
- `apps/memory/src/tree/mcp/tools.py` — imported `BrightDataConfigurationError` and `BrightDataRequestError` from `tree.data.web.web_unlocker`; extended the `ingest_url` MCP wrapper's `try/except` to translate them to `{"error": "configuration_error", ...}` and `{"error": "fetch_failed", ...}` respectively (matching the wrapper's existing `detail`-keyed shape; kept logging style consistent — the existing branches don't log).
- `apps/memory/src/tree/data/core/ingest.py` — pre-flight reject `https://` / `http://` with empty netloc via `if not parsed.netloc: raise ValueError(f"URL is missing a host: {url!r}")`. Saves a billable Bright Data request and gives a clear error instead of falling through to a downstream 4xx.
- `apps/memory/tests/integration/mcp/test_ingest_tools.py` — replaced `test_unsupported_url_returns_error` (which encoded the old contract) with two tests reflecting the new contract: (1) `test_unsupported_scheme_returns_error` uses `ftp://example.com/file.tar` to assert the legitimate `unsupported_url` MCP-error path still works; (2) `test_fallthrough_without_brightdata_credentials_returns_config_error` patches `settings.brightdata_api_key` to empty and asserts `https://example.com/some-page` falls through to the web pipeline and surfaces `{"error": "configuration_error", "detail": "...BRIGHTDATA_API_KEY..."}` from Fix A.
- `apps/memory/tests/unit/data/core/test_ingest.py` — added `test_rejects_missing_host` parametrized over `https://` and `http://` (no host), asserting `ValueError(match="missing a host")` and that neither pipeline handler is awaited.

**Tests**
- Unit: 357 passing, 0 failing, 0 warnings (`make memory-unit-tests`) — was 355 pre-fix; +2 for the new missing-host parametrized cases.
- Integration (`apps/memory/tests/integration/mcp/test_ingest_tools.py`): 11 passing, 0 failing — including both replacement tests for the previously-regressed scenario.

**Acceptance criteria**
- [x] AC #5 — "Existing Substack routing tests (or behavior, if untested) still pass — no regressions." Now PASSES: the `apps/memory/tests/integration/mcp/test_ingest_tools.py` file is fully green, and the contract change is correctly exercised by the two replacement tests.
- All other ACs remain green from the prior pass; no behavior outside #003's scope was touched.

**Evidence**

```
$ make memory-format-fix && make memory-lint-fix
132 files left unchanged
All checks passed!

$ make memory-format-check && make memory-lint-check
132 files already formatted
All checks passed!

$ make memory-unit-tests
... tests/unit/data/core/test_ingest.py ................                     [  5%]
============================= 357 passed in 19.83s =============================

$ make pre-commit
Validate pyproject.toml..............................(no files to check)Skipped
prettier.................................................................Passed
ruff check...............................................................Passed
ruff format..............................................................Passed
biome check (harness)....................................................Passed

$ uv run --project apps/memory pytest apps/memory/tests/integration/mcp/test_ingest_tools.py -v
... TestIngestUrl::test_unsupported_scheme_returns_error PASSED
... TestIngestUrl::test_fallthrough_without_brightdata_credentials_returns_config_error PASSED
... TestIngestUrl::test_duplicate_url_skipped PASSED
============================= 11 passed in 17.91s ==============================
```

**Notes**
- The Tester suggestion proposed `"message"` as the error-payload key, but the existing wrapper convention uses `"detail"` (see `unsupported_url` / `http_error` / `network_error` branches). I matched the existing convention as the spec instructed ("Use whatever the wrapper's existing error-shape convention is").
- The wrapper's existing branches do not call `logger.warning` / `logger.exception` either — they just return the JSON. I kept the new branches consistent with that (no logging on the translated-error path).
- Test (2) for the configuration-error fall-through stays in **integration** (not unit) because it exercises the full MCP wrapper -> dispatcher -> web_pipeline -> web_unlocker path. The only thing mocked is `settings.brightdata_api_key` — everything else runs for real, which is the right level for verifying the wrapper translates the exception into a clean payload.
- No new surprises during the QA loop. The full 11-test integration file went green on the first run after the fixes.

### [Tester] 2026-04-30 22:05 — Re-QA after fix iteration

**Test summary**
- Format / lint / pre-commit: PASS (`make pre-commit` — Validate pyproject.toml skipped, prettier / ruff check / ruff format / biome check (harness) all Passed).
- Unit tests: 357 passed, 0 failed, 0 warnings (`make memory-unit-tests`) — +2 vs. prior pass (the new `test_rejects_missing_host` parametrization).
- Integration tests (full memory suite): 58 passed, 0 failed (`make memory-integration-tests` — 58.47s).
- Integration tests (previously regressed file): `apps/memory/tests/integration/mcp/test_ingest_tools.py` — **11 passed, 0 failed**, including both replacement tests (`test_unsupported_scheme_returns_error`, `test_fallthrough_without_brightdata_credentials_returns_config_error`) and `test_duplicate_url_skipped`.

**E2E adversarial pass — Re-QA focus on the three claimed fixes**
- Spot-check Fix A — wrapper inspection (`apps/memory/src/tree/mcp/tools.py:14-17, 196-211`):
  - `BrightDataConfigurationError` and `BrightDataRequestError` imported at module top (lines 14-17), NOT lazy. PASS.
  - Order: `except ValueError` (line 198) → `except BrightDataConfigurationError` (200) → `except BrightDataRequestError` (202) → `except httpx.HTTPStatusError` (204) → `except (httpx.ConnectError, httpx.TimeoutException)` (208). The two new branches come AFTER `ValueError` BUT neither inherits from `ValueError`, so no shadowing. Verified: `issubclass(BrightDataConfigurationError, ValueError) == False` and same for `BrightDataRequestError` (both inherit from `Exception` per `apps/memory/src/tree/data/web/web_unlocker.py:27,31`). PASS.
  - Both branches use `str(exc)` for the `detail` value (not class name, not hardcoded). PASS.
  - The wrapper's existing `ValueError` / `http_error` / `network_error` branches do not log; the two new branches also do not log — convention preserved. PASS.
- Spot-check Fix A — runtime probe of `BrightDataRequestError` (no test in the suite explicitly covers this branch, only the integration test for the configuration branch):
  - `BrightDataRequestError('upstream 502 from bright data')` → `{"error": "fetch_failed", "detail": "upstream 502 from bright data"}`. PASS.
  - `BrightDataConfigurationError('BRIGHTDATA_API_KEY is not set')` → `{"error": "configuration_error", "detail": "BRIGHTDATA_API_KEY is not set"}`. PASS.
- Spot-check Fix B — test inspection (`apps/memory/tests/integration/mcp/test_ingest_tools.py:217-247`):
  - `grep -n "test_unsupported_url_returns_error"` → no matches. Old test name fully removed (not just renamed in spirit). PASS.
  - `test_unsupported_scheme_returns_error` (217-223) uses `ftp://example.com/file.tar` and asserts `error == "unsupported_url"` — keeps the legitimate `ValueError` → `unsupported_url` contract under coverage. PASS.
  - `test_fallthrough_without_brightdata_credentials_returns_config_error` (225-247) actually monkey-patches `tree.data.web.web_unlocker.settings.brightdata_api_key` to return `""` via a `MagicMock(get_secret_value=...)` — real "no credentials" simulation, not a hardcoded short-circuit. Asserts BOTH the dict shape (`error == "configuration_error"`) AND that the message contains `BRIGHTDATA_API_KEY`, which is generated inside `web_unlocker.py:50` — proving the URL flowed through dispatcher → `_ingest_web_url` → `web_unlocker` → caught by the new wrapper branch. The fallback path was genuinely exercised. PASS.
  - `test_duplicate_url_skipped` and the other adjacent tests are still in the file unchanged and all pass. PASS.
- Spot-check Fix C — unit test inspection (`apps/memory/tests/unit/data/core/test_ingest.py:202-226`):
  - `test_rejects_missing_host` parametrized over `["https://", "http://"]` with ids `["https-no-host", "http-no-host"]`, asserts `ValueError(match="missing a host")` AND that neither pipeline handler is awaited. Targeted run: `pytest ... test_rejects_missing_host -v` → 2 passed. PASS.
- Spot-check — out-of-scope file diff:
  - `git diff feat/bright-data-fallback-source~1 -- apps/memory/src/tree/orchestrator.py apps/memory/src/tree/config/app_config.py apps/memory/Makefile apps/memory/src/tree/data/pipeline.py` → ZERO output. PASS.
- Adversarial probes (per Re-QA brief):
  - Configuration-error message ordering: `web_unlocker.py:48-54` checks `BRIGHTDATA_API_KEY` FIRST (line 50), then `BRIGHTDATA_UNLOCKER_ZONE` (line 54). With both empty, the user sees `"BRIGHTDATA_API_KEY is not set"`. If only the zone is empty (key set), the user sees `"BRIGHTDATA_UNLOCKER_ZONE is not set"`. Each message points at exactly one variable and is actionable. PASS.
  - Does the wrapper now catch `BrightDataRequestError`? Verified via live probe (above). PASS — though the suite has no explicit test for this branch (the integration test only covers the configuration branch). NIT, not a Blocker — the integration with the real Bright Data path implicitly covers `BrightDataRequestError` once credentials exist.
  - `parsed.netloc` for `https://?query=foo`: trace shows `urlparse("https://?query=foo").netloc == ''`, so the new pre-flight `if not parsed.netloc: raise ValueError("URL is missing a host")` correctly rejects this case. PASS.
  - `https:/example.com` (single-slash typo): `urlparse` treats this as `scheme='https', netloc='', path='/example.com'`. The new pre-flight catches it (empty netloc) and raises `ValueError("URL is missing a host: 'https:/example.com'")` instead of falling through. Bonus correctness from Fix C. PASS.

**Acceptance criteria** (all 8 now PASS)
- [x] PASS — `ingest_url` falls through to `ingest_web_url` for unmatched http(s). Evidence: `test_falls_through_to_web_for_unmatched_http_url`, `test_falls_through_to_web_for_github_url` green; integration test `test_fallthrough_without_brightdata_credentials_returns_config_error` confirms the path is exercised end-to-end.
- [x] PASS — `ingest_url("ftp://example.com")` raises `ValueError` mentioning the unsupported scheme. Evidence: unit `test_rejects_unsupported_scheme[ftp]`; integration `test_unsupported_scheme_returns_error`.
- [x] PASS — `ingest_url("")` raises `ValueError`. Evidence: unit `test_rejects_unsupported_scheme[empty]`.
- [x] PASS — `ingest_url("https://martinfowler.com/...")` calls the fallback. Evidence: unit `test_falls_through_to_web_for_unmatched_http_url` asserts `mock_fallback.assert_awaited_once_with(...)`.
- [x] PASS (was the previous FAIL) — Existing Substack routing tests still pass — no regressions. Evidence: full integration suite 58/58 green; the previously-regressed `test_unsupported_url_returns_error` was correctly replaced with two contract-aware tests; the additional pre-flight host check (Fix C) adds an extra safety net without breaking any existing test.
- [x] PASS — INFO log line `"Routing URL to 'web (Bright Data fallback)' pipeline: <url>"` emitted on fallback. Evidence: unit `test_fallback_emits_info_log` (uses `caplog.at_level(INFO, logger="tree.data.core.ingest")`).
- [x] PASS — Unit test file at `apps/memory/tests/unit/data/core/test_ingest.py` covers all listed cases (substack registry, custom substack, two fallback URLs, three rejected schemes, two missing-host cases, log line, precedence). 16 unit tests in `TestIngestUrl` + the helper class.
- [x] PASS — `make memory-format-check && make memory-lint-check && make memory-unit-tests` pass. See Evidence below.

**Evidence**

```
$ make pre-commit
uv run --project apps/memory pre-commit run --all-files
Validate pyproject.toml..............................(no files to check)Skipped
prettier.................................................................Passed
ruff check...............................................................Passed
ruff format..............................................................Passed
biome check (harness)....................................................Passed

$ make memory-unit-tests
... tests/unit/data/core/test_ingest.py ................                     [  5%]
============================= 357 passed in 23.45s =============================

$ uv run --project apps/memory pytest apps/memory/tests/unit/data/core/test_ingest.py::TestIngestUrl::test_rejects_missing_host -v
apps/memory/tests/unit/data/core/test_ingest.py::TestIngestUrl::test_rejects_missing_host[https-no-host] PASSED
apps/memory/tests/unit/data/core/test_ingest.py::TestIngestUrl::test_rejects_missing_host[http-no-host] PASSED
============================== 2 passed in 0.16s ===============================

$ make memory-integration-tests
... tests/integration/mcp/test_ingest_tools.py ...........                   [ 65%]
============================= 58 passed in 58.47s ==============================

$ uv run --project apps/memory pytest apps/memory/tests/integration/mcp/test_ingest_tools.py -v
... TestIngestUrl::test_unsupported_scheme_returns_error PASSED
... TestIngestUrl::test_fallthrough_without_brightdata_credentials_returns_config_error PASSED
... TestIngestUrl::test_duplicate_url_skipped PASSED
============================= 11 passed in 17.65s ==============================

$ git diff feat/bright-data-fallback-source~1 -- apps/memory/src/tree/orchestrator.py apps/memory/src/tree/config/app_config.py apps/memory/Makefile apps/memory/src/tree/data/pipeline.py
(no output — out-of-scope files untouched)

# Live runtime probe of the new wrapper branches
$ uv run python -c "... BrightDataRequestError side-effect probe ..."
BrightDataConfigurationError is ValueError? False
BrightDataRequestError is ValueError? False
Request-error result: {"error": "fetch_failed", "detail": "upstream 502 from bright data"}
Config-error result: {"error": "configuration_error", "detail": "BRIGHTDATA_API_KEY is not set"}
```

**Other issues found**

1. **[Nit, not a Blocker]** No explicit unit/integration test in the suite specifically for the `BrightDataRequestError → "fetch_failed"` wrapper branch. The branch is correct (verified via live probe above), and exercising it for real would require a billable Bright Data request, but a small mocked unit test covering this branch would be cheap insurance. Suggest as a small follow-up — does not block #003.
2. **[Nit, addressed]** The earlier nit ("`https://` with empty netloc passes through to Bright Data") has been **fixed** by Fix C. Bonus: it also catches `https:/example.com` (single-slash typo) and `https://?query=foo` (query without host).

**VERDICT: PASS** — every non-`[HUMAN]` AC verified end-to-end; the previously-regressed `test_unsupported_url_returns_error` has been replaced with two contract-aware tests both green; full memory test suite is 357 unit + 58 integration = 415 tests passing, 0 warnings; pre-commit clean; out-of-scope files untouched; live runtime probe confirms both new wrapper branches translate exceptions correctly. Hand off to PM for acceptance review.
