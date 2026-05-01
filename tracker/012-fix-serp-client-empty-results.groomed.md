# Fix `tree.data.web.web_serp.search` so it returns organic results

Status: pending
Tags: `bug`, `web`, `bright-data`, `search`, `mcp`
Depends on: #010, #011
Blocks: #013

## Scope

Apply the smallest change to `apps/memory/src/tree/data/web/web_serp.py` (and possibly its mocked unit tests) so that:

- The failing regression test `test_common_query_returns_at_least_one_organic_result` from #011 turns GREEN against the live Bright Data SERP zone.
- The existing live tests in `apps/memory/tests/integration/data/web/test_web_serp.py` (`test_returns_results_with_titles_and_urls`, `test_empty_query_returns_empty_list`) stay GREEN. (If `test_returns_results_with_titles_and_urls` was also red on `main`, it should turn green here too — same root cause.)
- The 32 existing unit tests in `apps/memory/tests/unit/data/web/test_web_serp.py` stay GREEN. Where the fix changes the request shape or response key path, update the corresponding test fixture(s) to match — but do NOT delete or weaken assertions.
- The MCP tool unit tests (`apps/memory/tests/unit/mcp/test_search_web.py`) and integration tests (`apps/memory/tests/integration/mcp/test_search_web_tool.py`) stay GREEN.

### What to change (informed by the #010 diagnosis)

The diagnosis from #010 names the exact change. This task implements that change. The most likely vectors, ordered by likelihood (the SWE picks based on #010's recommendation, NOT this list — the diagnosis is binding):

1. **Most likely: drop `brd_json=1`, parse SERP HTML.** If #010 confirms Bright Data on this zone returns HTML when `brd_json=1` is unsupported (or the zone isn't configured for parsed JSON), the fix is to:
   - Remove the `("brd_json", "1")` tuple from `_build_serp_url` for all engines.
   - Replace `data = response.json(); organic = data.get("organic") or []` with an HTML-parsing helper that extracts organic results from Google's SERP HTML. Use `beautifulsoup4` (already in `apps/memory/pyproject.toml` per task #001) — do NOT add new dependencies. Selectors target the `<div>` elements that wrap each organic result; tolerate Google layout drift by selecting on stable structural anchors (e.g. links inside `h3` tags, with the heuristic "skip links pointing to google.com / accounts.google.com / their AMP redirector"). Keep the parser private and small.
   - Map the parsed HTML to `SearchResult(rank, title, url, snippet)` where `rank` is 1-indexed by appearance order, `snippet` is the visible description text (often `<span>` siblings of the link's parent), defaulting to `""` when absent.
   - Bing and Yandex parsers follow the same pattern — but if #010's diagnosis is Google-only and Bing/Yandex still return JSON for this zone, keep their JSON path and only change Google. Do not over-generalize.

2. **Alternative: keep `brd_json=1`, fix the parser key path.** If #010 confirms Bright Data IS returning JSON but the organic entries live under a different key (e.g. `data["body"]["organic"]`, `data["data"]["organic"]`, or the response wraps the SERP body inside a `body` field as a JSON-encoded *string*), the fix is the parser change only — leave the request shape alone. Update `_parse_organic`'s caller to traverse the correct key path.

3. **Alternative: change `format`.** If #010 confirms `format: "raw"` plus `brd_json=1` is fundamentally incompatible with this zone configuration and Bright Data needs `format: "json"` (the structured-response wrapper variant) instead, switch the value and adjust the parser to read from the structured wrapper (typically `data["body"]` decoded as JSON, then `["organic"]`).

4. **Combined.** #010 may prescribe two coordinated changes — implement them together.

### Public surface — DO NOT change

These are part of the contract with #007/#008's MCP tool and the existing tests. They stay byte-identical:

- Function signature: `async def search(query: str, *, engine: SearchEngine = "google", num_results: int = 10, country: str | None = None, language: str | None = None, timeout_seconds: float = 30.0) -> list[SearchResult]`.
- `SearchResult` Pydantic model (`rank`, `title`, `url`, `snippet`).
- Exception types: `BrightDataConfigurationError`, `BrightDataRequestError` (still imported from `web_unlocker`), `ValueError` for input validation.
- Return type: `list[SearchResult]`. Empty SERP → `[]` (do NOT raise).
- The MCP tool's JSON envelope: `{"query", "engine", "results": [r.model_dump() ...]}` is a property of the tool wrapper, not the SERP client — untouched by this task.
- `tree.data.web.__init__` re-exports — untouched.
- `.env.example` and `tree.config.settings` — untouched (no new env vars).
- Pagination semantics: when `num_results > 10`, paginate via the engine's offset (`start` for Google, `first` for Bing). The fix may need to adapt how pagination boundaries are detected (e.g. HTML page yields fewer than 10 results → stop), but the externally-observable behavior must match the docstring.

### Unit-test fixture updates (allowed)

The 32 existing unit tests at `apps/memory/tests/unit/data/web/test_web_serp.py` mock `httpx.AsyncClient.post` with JSON-shaped fixtures. If the fix changes the response shape the parser expects (HTML or different JSON key path), those fixtures must be updated to match — but the *assertions* must not be weakened. Specifically:

- `test_returns_search_results_on_200` — fixture body changes (HTML or different JSON path); assertion stays "two `SearchResult` instances with rank/title/url/snippet populated".
- `test_returns_empty_list_when_no_organic_entries`, `test_returns_empty_list_when_organic_key_missing` — fixtures change to "HTML with no organic results" / "JSON missing the new key path"; assertion stays "returns `[]`".
- `test_skips_entries_without_link`, `test_assigns_positional_rank_when_entry_lacks_rank` — fixtures change; defensive behavior (skip linkless / assign positional rank) stays.
- `test_paginates_when_num_results_exceeds_page_size` — fixture is two responses; the assertion that two POSTs are issued and the second URL has `start=10` stays.
- `TestSearchRequestShape::test_google_url_includes_required_params` — if `brd_json=1` is dropped, parametrize needs to drop the assertion for it; the assertions for `q`, `gl`, `hl`, `start` stay. Update the docstring comment accordingly.
- `TestSearchRequestShape::test_posts_expected_body_and_headers` — body still `{"zone", "url", "format"}`; if `format` value changes, the test changes accordingly.
- All HTTP-error path tests (`TestSearchHttpBehavior::test_raises_request_error_on_non_2xx` parametrized over 400/401/403/404/429/500/502/503), `BrightDataConfigurationError` tests, `ValueError` tests — UNCHANGED.

If the fix is purely a parser change (alternative 2) and the request shape is unchanged, only the parsing-related fixtures change.

### What is explicitly not in scope here

- Hardening the empty-result path with WARNING-level logging on unexpected response shapes — that lives in #013, not here.
- Adding new logging beyond what is necessary to make the fix observable in normal operation. The existing INFO log line ("Running SERP query via Bright Data...") stays.
- Refactoring unrelated code in `web_serp.py` (e.g. the credential resolver, the engine literals, the `_build_serp_url` Bing/Yandex branches if Google is the only one that needs touching).

### Verification

The SWE must produce in the log:

1. Output of `make memory-unit-tests` after the fix — all 394 (or current count) unit tests pass with 0 failures and 0 warnings.
2. Output of `make memory-integration-tests` after the fix, with real `BRIGHTDATA_API_KEY` + `BRIGHTDATA_SERP_ZONE` — the regression test from #011 passes, and existing live tests in `test_web_serp.py` and `test_search_web_tool.py` pass.
3. Output of an end-to-end smoke via the MCP tool's CLI: `make memory-search-web QUERY="Harness Engineering"` — must print ≥ 1 result and the JSON envelope `{"query": "Harness Engineering", "engine": "google", "results": [...]}` with non-empty `results`.
4. `make memory-format-check && make memory-lint-check && make pre-commit` — all green.

## Acceptance Criteria

- [ ] `apps/memory/src/tree/data/web/web_serp.py` is modified per the recommended fix vector from #010's Diagnosis log entry. The change set matches what the diagnosis prescribed (dropped `brd_json=1` / new parser key path / changed `format` value / combined).
- [ ] `apps/memory/tests/integration/data/web/test_web_serp.py::TestLiveSerpSearch::test_common_query_returns_at_least_one_organic_result` passes against the live Bright Data SERP zone. Output captured in the SWE log.
- [ ] Existing live tests `test_returns_results_with_titles_and_urls` and `test_empty_query_returns_empty_list` in the same file pass. Output captured.
- [ ] All 32 unit tests in `apps/memory/tests/unit/data/web/test_web_serp.py` pass (with fixtures updated to match the new request/response shape where required). Assertions are not weakened. Output captured.
- [ ] All MCP-tool unit tests in `apps/memory/tests/unit/mcp/test_search_web.py` pass — no signature/envelope changes were required at the tool layer. Output captured.
- [ ] All MCP-tool integration tests in `apps/memory/tests/integration/mcp/test_search_web_tool.py` pass against the live API. Output captured.
- [ ] `make memory-search-web QUERY="Harness Engineering"` prints a JSON envelope with `len(results) >= 1`, each result having non-empty `title` and `http`-prefixed `url`. Output captured (full JSON, no redaction needed — these are public SERP results).
- [ ] `tree.data.web.web_serp.search`'s public signature, `SearchResult` model, exception types, and return type are byte-identical to before the fix. Verified by `inspect.signature(search)` and `SearchResult.model_fields` REPL output captured in the log.
- [ ] `tree.data.web.__init__`'s `__all__` and re-exports are unchanged. Verified by `git diff` showing no edits to `apps/memory/src/tree/data/web/__init__.py`.
- [ ] `.env.example`, `apps/memory/src/tree/config/settings.py`, `apps/memory/scripts/search_web.py`, and `apps/memory/Makefile` are unchanged. Verified by `git diff`.
- [ ] No new third-party dependencies. `apps/memory/pyproject.toml` is unchanged. Verified by `git diff`.
- [ ] `make memory-format-check && make memory-lint-check && make memory-unit-tests && make memory-integration-tests && make pre-commit` all pass. Output captured.
- [ ] [HUMAN] Confirm `BRIGHTDATA_API_KEY` and `BRIGHTDATA_SERP_ZONE` are configured to non-placeholder values in `.env` so the SWE can run the live integration tests and the e2e smoke.

## User Stories

### Story: Operator runs the MCP tool and gets real SERP results

1. Operator runs `make memory-search-web QUERY="Harness Engineering"`.
2. The CLI logs each result as `[1] <title> — <url>` for at least 1 result.
3. The CLI prints a JSON envelope at the end with `"results": [...]` containing ≥ 1 entry, each with `rank`, `title`, `url`, and (often) `snippet`.
4. Exit code is 0.

### Story: Agent calls `search_web` over MCP and gets a non-empty list

1. The MCP-aware agent calls `search_web(query="pizza")`.
2. The response is a JSON string with shape `{"query": "pizza", "engine": "google", "results": [{"rank": 1, "title": "...", "url": "https://...", "snippet": "..."}, ...]}`.
3. The agent can `json.loads(response)["results"]` and iterate normally.

### Story: A failing live regression test now passes

1. SWE runs `make memory-integration-tests` with real Bright Data credentials.
2. `test_common_query_returns_at_least_one_organic_result` passes (`len(results) >= 1`, first result has title + http URL).
3. The other two live tests also pass.

### Story: Existing mocked unit tests remain a tight contract

1. SWE runs `make memory-unit-tests`.
2. All 32 unit tests in `tests/unit/data/web/test_web_serp.py` pass — input validation, configuration errors, HTTP errors, request shape, pagination, defensive parsing.
3. Where the fix changed the response shape, the corresponding fixture was updated, but the assertion ("returns two `SearchResult` instances with rank/title/url/snippet populated") is the same.

### Story: The MCP tool's contract is unchanged

1. SWE diffs `inspect.signature(tree.data.web.search)` before vs after the fix — identical.
2. SWE diffs `SearchResult.model_fields.keys()` — identical (`rank`, `title`, `url`, `snippet`).
3. SWE diffs `tree.data.web.__init__.__all__` — identical.
4. SWE confirms callers (`tree.mcp.tools.search_web` and `apps/memory/scripts/search_web.py`) need no edits.

---

Blocked by: #010, #011

## Log

(empty — SWE will append on pickup)
