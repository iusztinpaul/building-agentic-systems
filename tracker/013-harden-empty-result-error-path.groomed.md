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

- [ ] `apps/memory/src/tree/data/web/web_serp.py` distinguishes legitimate-empty from unexpected-shape in its parsing path. The change matches the description in Scope (INFO on legitimate-empty, WARNING on unexpected-shape, return `[]` in both cases).
- [ ] `apps/memory/tests/unit/data/web/test_web_serp.py::TestSearchEmptyResultLogging::test_logs_info_on_legitimate_empty_serp` exists and passes. Asserts INFO log on the legitimate-empty path, no WARNING.
- [ ] `apps/memory/tests/unit/data/web/test_web_serp.py::TestSearchEmptyResultLogging::test_logs_warning_on_unexpected_response_shape` exists and passes. Parametrized over at least 3 unexpected-shape sub-cases. Asserts WARNING log with `engine`, `status`, `content_type`, body preview (≤ 200 chars); asserts no exception raised; asserts no API key in any log record.
- [ ] The legitimate-empty path returns `[]` AND emits exactly one INFO log line containing `engine=google` and a substring like `"0 organic"` (or equivalent phrasing chosen by the SWE — assert on substring, not full equality).
- [ ] The unexpected-shape path returns `[]` AND emits exactly one WARNING log line. The warning message includes engine, status, content type, and a body preview truncated to ≤ 200 chars.
- [ ] No log record (INFO, WARNING, ERROR, or otherwise) emitted by `web_serp.py` contains the raw API key. Verified by an explicit assertion in the new WARNING test.
- [ ] `search`'s public signature, return type, raised exceptions, and `SearchResult` model are byte-identical to post-#012. Verified by `inspect.signature` REPL output captured in the log.
- [ ] All other existing unit tests in `tests/unit/data/web/test_web_serp.py` still pass.
- [ ] All existing live integration tests in `tests/integration/data/web/test_web_serp.py` still pass — the new branching is only triggered by malformed responses, not by the live API.
- [ ] All MCP-tool unit and integration tests still pass.
- [ ] `tree.mcp.tools.search_web`, `apps/memory/scripts/search_web.py`, `apps/memory/Makefile`, `.env.example`, `apps/memory/src/tree/config/settings.py` are unchanged. Verified by `git diff`.
- [ ] No new dependencies in `apps/memory/pyproject.toml`. Verified by `git diff`.
- [ ] `make memory-format-check && make memory-lint-check && make memory-unit-tests && make memory-integration-tests && make pre-commit` all pass. Output captured in the SWE log.

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

(empty — SWE will append on pickup)
