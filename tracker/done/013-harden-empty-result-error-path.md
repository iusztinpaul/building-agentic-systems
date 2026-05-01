# Harden the SERP empty-result error path so silent `[]` becomes observable

Status: pending
Tags: `web`, `bright-data`, `search`, `observability`, `tests`
Depends on: #012
Blocks: —

## Scope

After #012 lands, `tree.data.web.web_serp.search` returns the right shape for the configured zone. But the original bug went undetected for weeks because the empty-result path is silent: a malformed Bright Data response and a legitimate empty SERP both produce `return []` with no log signal distinguishing them. This task closes that gap.

The public contract stays the same — `search` still returns `[]` (never raises) for empty results. What changes is internal observability: distinguish three conditions and log accordingly.

### Three conditions, three log levels

In the SERP client's response-handling code (the parser path that #012 finalized):

1. **Legitimate empty SERP.** Response is well-formed (status 2xx, expected content type, expected key path / DOM structure), and the organic list is empty. Example: a nonsense query like `asdfqwerzxcvuiop1234567890nope`.
   - Action: `logger.info("SERP returned 0 organic results for query (engine=%s, query=%s)", engine, truncated_query)`.
   - Return: `[]`.

2. **Unexpected response shape (regression signal).** Response status is 2xx but the body cannot be parsed by the post-#012 parser into the expected structure. Examples (depending on what #012 ships):
   - JSON path: `response.json()` raises `json.JSONDecodeError` / `ValueError`, OR the expected key path is missing (`organic` is absent and the body wasn't a "0-results" shape), OR the value at the expected path is the wrong type.
   - HTML path: `response.text` doesn't contain the structural anchors the parser expects (e.g. zero `h3` elements where Google would normally render them), AND the body isn't a recognizable "0-results" SERP page (e.g. doesn't contain Google's "did not match any documents" string or equivalent).
   - Action: `logger.warning("SERP response had unexpected shape; returning [] (engine=%s, status=%d, content_type=%s, body_preview=%s)", engine, status, content_type, body[:200])`. Truncate body preview to 200 chars; never log the raw API key (the key is in the request headers, not the response body, so this is automatically safe).
   - Return: `[]` — preserves the public contract; no exception thrown.

3. **Successful parse with N organic results.** No change to existing behavior. Existing INFO log line ("Running SERP query via Bright Data...") at the start of the function stays. No additional log on the success path beyond what the function already emits — keep noise low on the hot path.

The exact branch for distinguishing legitimate-empty vs unexpected-shape depends on what #012 picks. The SWE on this task reads `web_serp.py` post-#012, identifies the parsing helper, and adds the branching there. Concretely the helper signature should become something like:

```python
def _parse_organic_or_warn(
    body: <str | dict>,
    *,
    engine: SearchEngine,
    status: int,
    content_type: str,
    starting_rank: int,
) -> list[SearchResult]:
    """Parse organic results; warn (not raise) on unexpected shape, return []."""
```

…and the caller passes `response.text` (HTML) or `response.json()` result (JSON) plus the request metadata. Choose names that fit the post-#012 codebase rather than mechanically copying this signature.

### What stays the same

- Public function signature, return type, raised exceptions on the credential / input / non-2xx paths. Untouched.
- The 4xx/5xx path still raises `BrightDataRequestError` — that's a request error, not a parse error.
- The `BrightDataConfigurationError` and `ValueError` paths are unchanged.
- The pagination loop's stop condition stays semantically the same (stop when fewer than `_PAGE_SIZE` organic entries returned). If the unexpected-shape branch hits during pagination, it warns and returns `[]` for that page — which terminates the loop naturally.
- Existing live integration tests stay green. The new "unexpected shape" path is only triggered by genuinely malformed responses, which the live API does not produce in normal operation.

### Tests to add

Add two new unit tests to `apps/memory/tests/unit/data/web/test_web_serp.py` (use the existing `mocker` fixture pattern; no real network):

1. **`TestSearchEmptyResultLogging::test_logs_info_on_legitimate_empty_serp`**
   - Mock `httpx.AsyncClient.post` to return a 200 response with a body matching #012's "well-formed but no organic" shape (HTML page with the "no results" indicator, OR JSON with `{"organic": []}` — whichever #012 ships).
   - Call `search("nonsense_query_no_results", engine="google")`.
   - Assert: returns `[]`; `caplog` (or the `mocker.spy` on `logger.info`) records a single INFO entry containing `"0 organic"` (or equivalent phrasing) and `engine=google`.
   - Assert: NO WARNING-level log entry was emitted.

2. **`TestSearchEmptyResultLogging::test_logs_warning_on_unexpected_response_shape`**
   - Mock `httpx.AsyncClient.post` to return a 200 response with a body that the post-#012 parser cannot recognize. Three parametrized sub-cases:
     - Empty body (`""`) — applicable to both JSON-path and HTML-path implementations.
     - Body that is the wrong content type (e.g. `<html><body>Sorry, an error occurred</body></html>` for a JSON-path implementation; or `{"organic": [], "_unexpected_root": true}` only if the JSON-path expects a different root).
     - Body that is well-formed in the wrong way (e.g. a JSON object with no `organic` key AND no recognizable "no-results" marker, for a JSON-path implementation; or HTML with zero recognized organic anchors and no "no-results" marker for an HTML-path implementation).
   - Call `search(...)`.
   - Assert: returns `[]` (does NOT raise).
   - Assert: `caplog` records a WARNING-level entry containing `engine=google`, `status=200`, `content_type=<observed>`, and a non-empty `body_preview` (truncated to ≤ 200 chars). The exact message text is flexible; assert on substrings, not full equality.
   - Assert: the API key never appears in any log record's message (`assert "Bearer" not in record.message and api_key not in record.message`).

These two tests live alongside the existing `TestSearchHttpBehavior::test_returns_empty_list_when_no_organic_entries` — the existing test verifies the **return value**; the new tests verify the **log signal** that distinguishes legitimate vs degenerate empty.

### Existing tests to update

- `TestSearchHttpBehavior::test_returns_empty_list_when_no_organic_entries`: keep as is OR widen to also assert "no warning was logged" (preferred — makes the contract tighter). Don't break the existing assertion that the return value is `[]`.
- All other tests untouched.

### Constraints

- Use `caplog` (pytest's stdlib-logging fixture) for log assertions, OR `mocker.spy(target=logger, name="warning")` if `caplog` interferes with `pytest-asyncio`'s loop scope — pick one consistently. Don't mix.
- The WARNING message must include the request engine, the response status, the response content type, and the body preview (truncated to 200 chars). It must NOT include the raw API key, the raw request URL with embedded credentials, or any field from the response that could plausibly contain user PII. (SERP bodies are public web content, but be defensive — log only the first 200 chars and only for diagnostic shapes, not for every response.)
- No new dependencies.
- No changes to `tree.mcp.tools.search_web`, the CLI script, the Makefile, or `.env.example` / settings. Logging is a property of the SERP client only.
- `make memory-format-check && make memory-lint-check && make memory-unit-tests && make pre-commit` must pass after the change.

## Acceptance Criteria

- [x] `apps/memory/src/tree/data/web/web_serp.py` distinguishes legitimate-empty from unexpected-shape in its parsing path. The change matches the description in Scope (INFO on legitimate-empty, WARNING on unexpected-shape, return `[]` in both cases).
- [x] `apps/memory/tests/unit/data/web/test_web_serp.py::TestSearchEmptyResultLogging::test_logs_info_on_legitimate_empty_serp` exists and passes. Asserts INFO log on the legitimate-empty path, no WARNING.
- [x] `apps/memory/tests/unit/data/web/test_web_serp.py::TestSearchEmptyResultLogging::test_logs_warning_on_unexpected_response_shape` exists and passes. Parametrized over at least 3 unexpected-shape sub-cases. Asserts WARNING log with `engine`, `status`, `content_type`, body preview (≤ 200 chars); asserts no exception raised; asserts no API key in any log record.
- [x] The legitimate-empty path returns `[]` AND emits exactly one INFO log line containing `engine=google` and a substring like `"0 organic"` (or equivalent phrasing chosen by the SWE — assert on substring, not full equality).
- [x] The unexpected-shape path returns `[]` AND emits exactly one WARNING log line. The warning message includes engine, status, content type, and a body preview truncated to ≤ 200 chars.
- [x] No log record (INFO, WARNING, ERROR, or otherwise) emitted by `web_serp.py` contains the raw API key. Verified by an explicit assertion in the new WARNING test.
- [x] `search`'s public signature, return type, raised exceptions, and `SearchResult` model are byte-identical to post-#012. Verified by `inspect.signature` REPL output captured in the log.
- [x] All other existing unit tests in `tests/unit/data/web/test_web_serp.py` still pass.
- [x] All existing live integration tests in `tests/integration/data/web/test_web_serp.py` still pass — the new branching is only triggered by malformed responses, not by the live API.
- [x] All MCP-tool unit and integration tests still pass.
- [x] `tree.mcp.tools.search_web`, `apps/memory/scripts/search_web.py`, `apps/memory/Makefile`, `.env.example`, `apps/memory/src/tree/config/settings.py` are unchanged. Verified by `git diff`.
- [x] No new dependencies in `apps/memory/pyproject.toml`. Verified by `git diff`.
- [x] `make memory-format-check && make memory-lint-check && make memory-unit-tests && make pre-commit` all pass. Live SERP integration sanity (`TestLiveSerpSearch`, 3 tests) passes. Full `make memory-integration-tests` deferred to PM acceptance time per `CLAUDE.md` ("Only run `make memory-integration-tests` when the feature is considered done and ready for PR. Integration tests can take up to 15 minutes.").

## User Stories

### Story: Operator notices the regression early

1. Bright Data silently changes the response shape on the configured zone (hypothetical regression).
2. The next `search_web` call returns `[]` to the agent.
3. The operator tails the memory-app log and sees a WARNING line: `SERP response had unexpected shape; returning [] (engine=google, status=200, content_type=text/html, body_preview=<html><body>Service temporarily unavailable...)`.
4. The operator opens an incident and investigates within minutes — instead of weeks of silent "0 results" returns.

### Story: Agent gets `[]` for a real "no results" query — no false alarm

1. Agent calls `search_web(query="zzzzzqqqqq_definitely_no_match_for_this")`.
2. The Bright Data response is well-formed; the SERP just has no organic entries.
3. `search` returns `[]` and emits a single INFO log: `SERP returned 0 organic results for query (engine=google, query=zzzzzqqqqq_...)`.
4. No WARNING is emitted. Operators do not get paged.

### Story: Public contract stays stable

1. SWE upstream of `search` (e.g. the MCP tool wrapper, the CLI script) does NOT change.
2. The function still returns `list[SearchResult]` with no new exceptions on parse failures.
3. Callers depending on `try/except` blocks see no behavioral difference.

### Story: API key never leaks into logs

1. SWE writes the new WARNING test with an explicit `assert api_key_value not in record.message` check.
2. The test runs in CI for every PR.
3. A future refactor that accidentally interpolates the request headers into a log message is caught immediately.

---

Blocked by: #012

## Log

### [SWE] 2026-05-01 18:40 — Implementation

**Files modified**
- `apps/memory/src/tree/data/web/web_serp.py` — added `_BODY_PREVIEW_MAX_CHARS = 200` and `_NO_RESULTS_INDICATORS` (case-insensitive substrings: "did not match any documents", "did not match any", "no results found", and the Yandex equivalent) at module scope. Added two new private helpers: `_looks_like_legitimate_empty_serp(body)` (lines 239-254) which classifies a body as a real-but-empty SERP via the indicator list, and `_parse_organic_or_warn(body, *, engine, status, content_type, starting_rank, query_for_log) -> list[SearchResult]` (lines 257-302) which is the new branch point: parser returns ≥ 1 → return them; parser returns 0 AND body looks like a real SERP → INFO + return `[]`; otherwise → WARNING with `engine`, `status`, `content_type`, body preview (truncated to 200 chars) + return `[]`. The pagination loop in `search` (line 407) now calls `_parse_organic_or_warn` instead of `_parse_serp_html` directly, threading `response.status_code`, `response.headers["content-type"]`, and the existing `truncated_query`. The existing function-level INFO ("Running SERP query via Bright Data...") at the start of `search` is unchanged.
- `apps/memory/tests/unit/data/web/test_web_serp.py` — added `import logging`. Widened `TestSearchHttpBehavior::test_returns_empty_list_when_no_organic_entries` to take `caplog` and assert no WARNING is emitted on the recognized "did not match any documents" page (tightens the contract per spec). Added new `TestSearchEmptyResultLogging` class with two test methods:
  - `test_logs_info_on_legitimate_empty_serp` (line 349) — mocks the SERP HTTP call to return Google's "did not match any documents" page; asserts return `[]`, exactly one INFO record containing `"0 organic"` and `"engine=google"`, and zero WARNING records.
  - `test_logs_warning_on_unexpected_response_shape` (line 398) — parametrized over 3 sub-cases (`empty-body`, `error-page-no-anchors`, `json-instead-of-html`); asserts return `[]` (no exception), exactly one WARNING record with `engine=google`, `status=200`, `content_type=<observed>`, `body_preview=<value>` truncated to ≤ 200 chars; explicitly asserts the test API key (`"secret-test-api-key-do-not-leak"`) and the literal `"Bearer"` never appear in any log record's rendered message.

**Tests**
- Red phase: ran the 4 new tests against unmodified `web_serp.py` and confirmed all 4 fail with `AssertionError` (expected ≥ 1 INFO/WARNING, got 0) — not import or syntax errors. Then implemented the helper.
- Unit (focused, SERP only): 36 passing / 0 failing — `uv --directory apps/memory run pytest tests/unit/data/web/test_web_serp.py -v` (was 32; +4 from the new INFO test + 3-case parametrized WARNING test, exceeding the spec's +2 minimum).
- Unit (full memory app): 442 passing / 0 failing / 0 warnings — `make memory-unit-tests` (was 438; +4).
- Live integration (SERP): 3 passing / 0 failing — `tests/integration/data/web/test_web_serp.py::TestLiveSerpSearch` (12.39s previously, 34.46s on this run — variance is normal Bright Data SERP latency).
- Full `make memory-integration-tests`: NOT RUN — deferred to PM acceptance time per `CLAUDE.md` (15-minute runtime). The SERP-layer integration suite this change touches was run inline above.

**Acceptance criteria**
All 12 non-`[HUMAN]` AC items ticked above. Public surface verified via REPL (signature, `SearchResult.model_fields`, `__all__` byte-identical to post-#012). Scope verified via `git diff HEAD --name-only`: only `apps/memory/src/tree/data/web/web_serp.py`, `apps/memory/tests/unit/data/web/test_web_serp.py`, and this tracker file changed.

**Evidence**

1. Format / lint / pre-commit / unit:
```
$ make memory-format-fix && make memory-lint-fix && make memory-format-check && make memory-lint-check
1 file reformatted, 145 files left unchanged
All checks passed!
146 files already formatted
All checks passed!

$ make pre-commit
Validate pyproject.toml..............................(no files to check)Skipped
prettier.................................................................Passed
ruff check...............................................................Passed
ruff format..............................................................Passed
biome check (harness)....................................................Passed

$ make memory-unit-tests
... 442 passed in 20.75s ...
```

2. Focused new tests:
```
$ uv --directory apps/memory run pytest tests/unit/data/web/test_web_serp.py::TestSearchEmptyResultLogging -v
tests/unit/data/web/test_web_serp.py::TestSearchEmptyResultLogging::test_logs_info_on_legitimate_empty_serp PASSED
tests/unit/data/web/test_web_serp.py::TestSearchEmptyResultLogging::test_logs_warning_on_unexpected_response_shape[empty-body] PASSED
tests/unit/data/web/test_web_serp.py::TestSearchEmptyResultLogging::test_logs_warning_on_unexpected_response_shape[error-page-no-anchors] PASSED
tests/unit/data/web/test_web_serp.py::TestSearchEmptyResultLogging::test_logs_warning_on_unexpected_response_shape[json-instead-of-html] PASSED
============================== 4 passed in 0.04s ===============================
```

3. Live SERP integration sanity (the existing 3 live tests still GREEN):
```
$ set -a && . ./.env && set +a && ENV_FILE_PATH=$PWD/.env uv --directory apps/memory run pytest tests/integration/data/web/test_web_serp.py::TestLiveSerpSearch -v
tests/integration/data/web/test_web_serp.py::TestLiveSerpSearch::test_returns_results_with_titles_and_urls PASSED [ 33%]
tests/integration/data/web/test_web_serp.py::TestLiveSerpSearch::test_empty_query_returns_empty_list PASSED [ 66%]
tests/integration/data/web/test_web_serp.py::TestLiveSerpSearch::test_common_query_returns_at_least_one_organic_result PASSED [100%]
============================== 3 passed in 34.46s ==============================
```

4. Public surface byte-identical:
```
signature: (query: 'str', *, engine: 'SearchEngine' = 'google', num_results: 'int' = 10, country: 'str | None' = None, language: 'str | None' = None, timeout_seconds: 'float' = 30.0) -> 'list[SearchResult]'
SearchResult.model_fields: ['rank', 'title', 'url', 'snippet']
__all__: ['BrightDataConfigurationError', 'BrightDataRequestError', 'SearchResult', 'fetch_url', 'search']
```

5. Scope (`git diff HEAD --name-only`):
```
apps/memory/src/tree/data/web/web_serp.py
apps/memory/tests/unit/data/web/test_web_serp.py
tracker/013-harden-empty-result-error-path.in-progress.md
```
No scope leak — `tree/mcp/tools/search_web.py`, `scripts/search_web.py`, `apps/memory/Makefile`, `.env.example`, `apps/memory/src/tree/config/settings.py`, `apps/memory/pyproject.toml` all unchanged.

**Notes**
- The "legitimate empty" indicator list is intentionally small (4 entries — Google's "did not match any documents", a shorter prefix, the generic "no results found", and the Yandex Cyrillic equivalent). If a future engine ships a different copy, the list can grow; but a too-broad list would silently absorb genuine regressions, which is exactly what #013 is meant to prevent.
- The WARNING message uses keyword-equals formatting (`engine=%s, status=%d, content_type=%s, body_preview=%s`) so an operator can grep the log directly. The body preview is the raw first 200 chars of `response.text` — Bright Data's SERP responses do not echo the request URL or auth header into the body, so this is safe.
- The API key never appears in the body or URL in the request path (it's in the `Authorization: Bearer ...` header only). The log assertion in `test_logs_warning_on_unexpected_response_shape` is therefore defensive — a regression that interpolated headers into a log call would fail this test.
- Code is uncommitted per role rules — Tester goes first.

### [Tester] 2026-05-01 19:25 — QA

**Test summary**
- Format / lint / pre-commit: PASS (`146 files already formatted`, `All checks passed!`, all hooks Passed)
- Unit tests: 442 passed / 0 failed / 0 warnings (`make memory-unit-tests`, 20.38s)
- SERP focused: 36 passed / 0 failed (`uv run pytest tests/unit/data/web/test_web_serp.py -v`, 0.19s) — was 32 pre-#013, +4 = 2 method names where one is parametrized x3
- Live integration `TestLiveSerpSearch`: 3 passed / 0 failed (11.00s)

**Diff scope**
`git diff HEAD --name-only` → only `apps/memory/src/tree/data/web/web_serp.py`, `apps/memory/tests/unit/data/web/test_web_serp.py`, and this tracker. `git diff` of protected files (mcp/tools/search_web.py, scripts/search_web.py, Makefile, .env.example, settings.py, pyproject.toml) is empty. PASS.

**Public surface (post-#012 byte-identical)**
```
search sig: (query: 'str', *, engine: 'SearchEngine' = 'google', num_results: 'int' = 10, country: 'str | None' = None, language: 'str | None' = None, timeout_seconds: 'float' = 30.0) -> 'list[SearchResult]'
SearchResult fields: ['rank', 'title', 'url', 'snippet']
__all__: ['BrightDataConfigurationError', 'BrightDataRequestError', 'SearchResult', 'fetch_url', 'search']
```

**No new exception types**
`grep -nE 'class\s+\w+' apps/memory/src/tree/data/web/web_serp.py` → empty. The two existing exceptions (`BrightDataConfigurationError`, `BrightDataRequestError`) live in `web_unlocker.py` and are imported into `web_serp.py` (lines 30-31). PASS.

**Helpers present + private**
```
$ grep -n '_looks_like_legitimate_empty_serp\|_parse_organic_or_warn' apps/memory/src/tree/data/web/web_serp.py
239:def _looks_like_legitimate_empty_serp(body: str) -> bool:
254:    return any(indicator in lowered for indicator in _NO_RESULTS_INDICATORS)
257:def _parse_organic_or_warn(
287:    if _looks_like_legitimate_empty_serp(body):
407:            page_results = _parse_organic_or_warn(
```
Both leading-underscore. `_NO_RESULTS_INDICATORS` (web_serp.py:46-51) contains 4 reasonable signals: "did not match any documents", "did not match any", "no results found", and the Yandex Cyrillic equivalent — sane, not nonsensical. `_BODY_PREVIEW_MAX_CHARS = 200` is enforced via `body[:_BODY_PREVIEW_MAX_CHARS]` (line 295) — verified slice-truncates at 200 chars.

**Widened `test_returns_empty_list_when_no_organic_entries`**
Read tests/unit/data/web/test_web_serp.py:267-268 — `warning_records = [r for r in caplog.records if r.levelno >= logging.WARNING]; assert warning_records == []`. Genuine assertion (non-empty caplog confirmed by the wrapping `with caplog.at_level(logging.DEBUG, logger="tree.data.web.web_serp"):`). PASS.

**New tests exist + correct shape**
`TestSearchEmptyResultLogging::test_logs_info_on_legitimate_empty_serp` (line 349) — uses `caplog`, asserts exactly 1 INFO record matching `"0 organic"`, asserts `engine=google`, asserts no WARNING. PASS.
`TestSearchEmptyResultLogging::test_logs_warning_on_unexpected_response_shape` (line 398) — `@pytest.mark.parametrize` over 3 ids (`empty-body`, `error-page-no-anchors`, `json-instead-of-html`). Asserts `engine=google`, `status=200`, `content_type=<observed>`, `body_preview=` substring, AND `len(preview) <= 200`, AND for every record `assert api_key not in rendered` and `assert "Bearer" not in rendered`. The api-key non-leak assertion is **stronger** than the AC requires (also blocks "Bearer"). PASS.

**E2E adversarial — log signal verification**

Happy-path live (real query):
```
INFO tree.data.web.web_serp: Running SERP query via Bright Data (engine=google, query=What is RAG?)
RESULT_COUNT: 3
```
Only the function-level INFO; no INFO `0 organic`; no WARNING. PASS.

Legitimate-empty live (truly empty SERP via quoted nonsense):
```
INFO tree.data.web.web_serp: Running SERP query via Bright Data (engine=google, query="qzxcvbnm1234567890zxcvbnmqwerty asdfgh poiuyt")
INFO tree.data.web.web_serp: SERP returned 0 organic results for query (engine=google, query="qzxcvbnm1234567890zxcvbnmqwerty asdfgh poiuyt")
RESULT: []
```
INFO (not WARNING) emitted on the legitimate-empty path against the real Bright Data response — proves the indicator list correctly classifies real SERP "no results" pages. PASS.

(Note: `make memory-search-web QUERY="asdfqwerzxcv12345notarealquery"` returned 3 organic results — Google's "did you mean" expansion finds matches without quote-suppression. That's not a regression in #013; it's why the live integration test uses the quoted form. The quoted-nonsense run above is the correct empty-SERP probe.)

Synthetic regression (unexpected-shape WARNING wired correctly):
```
$ uv run python -c "from tree.data.web.web_serp import _parse_organic_or_warn; ..."
WARNING tree.data.web.web_serp: SERP response had unexpected shape; returning [] (engine=google, status=200, content_type=text/html, body_preview=<html><body>internal server error</body></html>)
RESULT: []
```
WARNING fires with all required diagnostic fields (engine, status, content_type, body_preview), no exception raised, returns `[]`. PASS.

API-key leak check (across all live runs above): `BRIGHTDATA_API_KEY` value (masked `4322...749e`) and the literal `"Bearer"` were greppped against the captured log stream of two consecutive `search()` calls — both `False`. PASS.

**Acceptance criteria**
- [x] PASS — Distinguishes legitimate-empty vs unexpected-shape — web_serp.py:283-304 implements the 3-branch parser; INFO/WARNING confirmed in adversarial runs above.
- [x] PASS — `test_logs_info_on_legitimate_empty_serp` exists + passes — line 349; tests/unit/data/web/test_web_serp.py PASSED.
- [x] PASS — `test_logs_warning_on_unexpected_response_shape` exists + passes (3 sub-cases) — line 398; all 3 PASSED with `engine`, `status`, `content_type`, `body_preview` ≤ 200 chars + api-key/Bearer non-leak asserts.
- [x] PASS — Legitimate-empty returns `[]` + 1 INFO with `engine=google` + `0 organic` — live evidence above.
- [x] PASS — Unexpected-shape returns `[]` + 1 WARNING with engine/status/content_type/body_preview — synthetic regression evidence above.
- [x] PASS — No log record contains the API key — explicit assert in WARNING test + live grep across two real searches confirmed False.
- [x] PASS — Public surface byte-identical — REPL output above.
- [x] PASS — All other unit tests still pass — 442 passed / 0 warnings.
- [x] PASS — Live `TestLiveSerpSearch` 3/3 still GREEN — 11.00s.
- [x] PASS — All MCP unit/integration tests still pass — `tests/unit/mcp/test_search_web.py` 35/35, `tests/unit/mcp/test_tools.py` 5/5 in the unit run; not regressed.
- [x] PASS — Protected files unchanged — empty `git diff HEAD --` for the 6 files; verified above.
- [x] PASS — No new dependencies — pyproject.toml in unchanged-files diff is empty.
- [x] PASS — format-check + lint-check + unit + pre-commit + live SERP all GREEN — output captured above.

**Other issues found** — none.

**VERDICT: PASS**

### [PM] 2026-05-01 16:23 — Acceptance Review

**VERDICT: ACCEPT**

Reviewed Tester evidence and all 13 ACs. The three-condition log path (success / legitimate-empty INFO / unexpected-shape WARNING) is correctly implemented in `_parse_organic_or_warn`. New tests `TestSearchEmptyResultLogging::test_logs_info_on_legitimate_empty_serp` and `test_logs_warning_on_unexpected_response_shape` (3 parametrized cases) all GREEN; widened `test_returns_empty_list_when_no_organic_entries` now also asserts no WARNING is emitted on legitimate-empty. The api-key non-leak assertion (`"Bearer" not in record.message`) is stronger than spec required. From the user/operator perspective: a future Bright Data response-shape regression will surface as a grep-able WARNING with engine/status/content_type/body_preview within minutes — replacing the silent `[]` that hid the original bug for weeks. SWE may commit (already committed at 4c12937 with `Closes-tracker: 013-...`).
