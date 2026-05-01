# Bright Data SERP API client + settings

Status: pending
Tags: `data-pipeline`, `web`, `bright-data`, `search`, `infrastructure`
Depends on: None
Blocks: #007

## Scope

Add the foundational SERP (Search Engine Results Page) HTTP client the new on-demand search tool will sit on. No MCP wiring, no Prefect, no ingestion side-effects — just settings + a pure async HTTP wrapper + unit tests. Modeled after the existing `tree.data.web.web_unlocker` (task #001) which already targets `https://api.brightdata.com/request` for the Web Unlocker zone — this task adds a sibling `tree.data.web.web_serp` module pointing at the SERP zone.

Three concerns:

1. **Settings.** Extend `apps/memory/src/tree/config/settings.py` with one new field on `Settings`:
   - `brightdata_serp_zone: str = ""` (env var `BRIGHTDATA_SERP_ZONE`).

   Reuse the existing `brightdata_api_key: SecretStr` field — Bright Data uses the same account-wide API key across all zones. Update `.env.example` with the new env var alongside the existing Web Unlocker block:

   ```
   # Bright Data SERP API (on-demand web search)
   BRIGHTDATA_SERP_ZONE=your-brightdata-serp-zone
   ```

2. **Result type.** Add a Pydantic model `SearchResult` in a new file `apps/memory/src/tree/data/web/types.py`:

   ```python
   from pydantic import BaseModel, Field

   class SearchResult(BaseModel):
       """A single organic SERP entry returned by Bright Data's SERP API."""

       rank: int = Field(..., description="Position within the organic results, 1-indexed.")
       title: str
       url: str
       snippet: str = Field(default="", description="Description / page summary; may be empty.")
   ```

   `tree.data.web.types` is a new module — keep it small and focused on web-data-layer types. Re-export `SearchResult` from `tree.data.web.__init__`.

3. **SERP HTTP client.** New module `apps/memory/src/tree/data/web/web_serp.py` exposing one async function:

   ```python
   from typing import Literal

   SearchEngine = Literal["google", "bing", "yandex"]

   async def search(
       query: str,
       *,
       engine: SearchEngine = "google",
       num_results: int = 10,
       country: str | None = None,
       language: str | None = None,
       timeout_seconds: float = 30.0,
   ) -> list[SearchResult]:
       """Run a SERP query via Bright Data's SERP API and return organic results.

       POSTs to https://api.brightdata.com/request with:
           Authorization: Bearer <BRIGHTDATA_API_KEY>
           json={
               "zone": <BRIGHTDATA_SERP_ZONE>,
               "url": <built SERP URL with brd_json=1 + locale + start>,
               "format": "raw",
           }

       Returns up to `num_results` organic entries. Pagination is handled
       internally via Google's `start` offset (pages of 10). Empty result
       sets are returned as an empty list — never raised.

       Raises:
           BrightDataConfigurationError: if BRIGHTDATA_API_KEY or
               BRIGHTDATA_SERP_ZONE is empty.
           BrightDataRequestError: on any non-2xx response.
           ValueError: if `query` is empty / whitespace-only, or
               `num_results` is < 1.
       """
   ```

   Implementation rules:
   - Build the SERP URL per `.claude/skills/bright-data-best-practices/references/serp-api.md` — always pass `brd_json=1` so Bright Data returns parsed JSON (never HTML scraping).
     - Google: `https://www.google.com/search?q=<urlencoded>&brd_json=1[&gl=<country>&hl=<language>&start=<offset>]`.
     - Bing:   `https://www.bing.com/search?q=<urlencoded>&brd_json=1[&cc=<country>&setLang=<language>&first=<offset+1>]`.
     - Yandex: `https://yandex.com/search/?text=<urlencoded>&brd_json=1[&lr=<country>]`.
   - Pagination: when `num_results > 10`, loop fetches with `start=0,10,20,...` (Google) / `first=1,11,21,...` (Bing). Stop when the response returns fewer organic entries than the page size or when the requested `num_results` is reached. Truncate the final list to exactly `num_results` (or fewer if the engine returned fewer).
   - Parse `response.json()["organic"]` into `SearchResult` instances. Map fields:
     - `rank` ← entry's `rank` (or 1-indexed position within the cumulative list when missing — defensive).
     - `title` ← entry's `title` (default `""`).
     - `url` ← entry's `link` (default `""`); skip entries with no link.
     - `snippet` ← entry's `description` (default `""`).
   - Reuse the existing `BrightDataConfigurationError` and `BrightDataRequestError` from `tree.data.web.web_unlocker` (import them; do **not** redefine). Extend the credential check to also require `brightdata_serp_zone`.
   - Use `httpx.AsyncClient` with the provided `timeout_seconds`. One client per call (matches the unlocker module's pattern).
   - Module-level `logger = logging.getLogger(__name__)`. Log the query (truncated to 100 chars) and the engine; never log the API key.
   - Do **not** persist anything. Do **not** import MongoDB / Prefect / `Document`. Pure HTTP wrapper.

Re-export `search` and `SearchResult` from `tree.data.web.__init__`.

No new third-party dependency (`httpx`, `pydantic` already in `apps/memory/pyproject.toml`).

## Acceptance Criteria

- [ ] `tree.config.settings.settings.brightdata_serp_zone` is a `str` and reads from env var `BRIGHTDATA_SERP_ZONE`. Verified by REPL: `uv --directory apps/memory run python -c "from tree.config.settings import settings; print(repr(settings.brightdata_serp_zone))"` returns the configured value (or `''` when unset).
- [ ] `.env.example` lists `BRIGHTDATA_SERP_ZONE` with a placeholder value `your-brightdata-serp-zone` and a section comment `# Bright Data SERP API (on-demand web search)`.
- [ ] `tree.data.web.types.SearchResult` is a Pydantic v2 `BaseModel` with fields `rank: int`, `title: str`, `url: str`, `snippet: str` (default `""`). Importable as `from tree.data.web import SearchResult`.
- [ ] `tree.data.web.web_serp` module exposes an async `search(query, *, engine, num_results, country, language, timeout_seconds) -> list[SearchResult]` function. Importable as `from tree.data.web import search`.
- [ ] `search` raises `ValueError("query must not be empty")` (or similar message; assertion is on the type) when called with `""`, `"   "`, or `None`. Verified by unit test (parametrize over the cases).
- [ ] `search` raises `ValueError` when `num_results < 1`. Verified by unit test.
- [ ] `search` raises `BrightDataConfigurationError` (with a message naming the missing env var) when either `BRIGHTDATA_API_KEY` or `BRIGHTDATA_SERP_ZONE` is empty. Verified by unit test (parametrize the two missing-credential cases).
- [ ] `search` raises `BrightDataRequestError` on a 4xx/5xx response. Verified by unit test (parametrize 400/401/403/404/429/500/502/503).
- [ ] On a 200 response with two organic entries, `search(query="python", engine="google", num_results=10)` returns two `SearchResult` instances with `rank`, `title`, `url`, `snippet` populated from the JSON payload. Verified by unit test mocking `httpx.AsyncClient.post` with a fixture payload mirroring the SERP API JSON shape from `.claude/skills/bright-data-best-practices/references/serp-api.md`.
- [ ] `search` posts the exact JSON body `{"zone": <serp_zone>, "url": <built-serp-url>, "format": "raw"}` and `Authorization: Bearer <api_key>` header. Verified by unit test asserting on the mocked client's call args.
- [ ] The built SERP URL for `engine="google"` includes `brd_json=1`, `q=<urlencoded query>`, and (when passed) `gl=<country>`, `hl=<language>`, `start=<offset>`. Verified by parametrized unit test inspecting the URL built per call.
- [ ] When `num_results=15` and the first response returns 10 organic entries, `search` issues a second request with `start=10` (Google) — verified by unit test asserting two POST calls and the offset in the second URL.
- [ ] When the SERP API returns `{"organic": []}`, `search` returns `[]` (empty list, not an exception). Verified by unit test.
- [ ] All public functions and types have type annotations on parameters and return types.
- [ ] No `print()` calls in source — uses `logging.getLogger(__name__)`.
- [ ] `make memory-format-check && make memory-lint-check && make memory-unit-tests && make pre-commit` all pass. Output captured in the SWE log.
- [ ] Unit-test file at `apps/memory/tests/unit/data/web/test_web_serp.py` mirrors the package layout, uses `pytest-mock`'s `mocker` fixture, makes **no real network calls**, covers every case above.

## User Stories

### Story: Operator configures the SERP zone
1. Operator copies `.env.example` to `.env` (or amends an existing one).
2. Operator pastes their Bright Data SERP zone name into `BRIGHTDATA_SERP_ZONE=...` (their existing `BRIGHTDATA_API_KEY` already works).
3. Operator runs `uv --directory apps/memory run python -c "from tree.config.settings import settings; print(bool(settings.brightdata_api_key.get_secret_value()), settings.brightdata_serp_zone)"`.
4. Operator sees `True my-serp-zone` printed — confirms the new setting is wired.

### Story: Developer runs a Google SERP query in the REPL
1. Developer ensures `.env` has both `BRIGHTDATA_API_KEY` and `BRIGHTDATA_SERP_ZONE` set.
2. Developer runs `uv --directory apps/memory run python` to open a REPL.
3. Developer imports: `from tree.data.web import search; import asyncio`.
4. Developer runs: `results = asyncio.run(search("knowledge graphs", num_results=5))`.
5. Developer sees `len(results) == 5`, each item is a `SearchResult` with non-empty `title` and a URL starting with `http`.

### Story: Developer hits the client with bad input
1. Developer runs `asyncio.run(search(""))` — sees `ValueError: query must not be empty`.
2. Developer runs `asyncio.run(search("hi", num_results=0))` — sees `ValueError: num_results must be >= 1`.
3. Developer unsets `BRIGHTDATA_SERP_ZONE`, restarts the REPL, runs `asyncio.run(search("hi"))` — sees `BrightDataConfigurationError: BRIGHTDATA_SERP_ZONE is not set`.

### Story: SWE on #007 imports the client
1. SWE writing the MCP tool imports `from tree.data.web import search, SearchResult`.
2. The import succeeds. `SearchResult.model_fields.keys()` contains `{"rank", "title", "url", "snippet"}`.
3. Calling `search("foo")` (with creds set) returns a `list[SearchResult]` — composes cleanly with the next task.

---

Blocked by: (none)

## Log

### [SWE] 2026-05-01 12:00 — Implementation

**Files modified**
- `apps/memory/src/tree/config/settings.py` — added `brightdata_serp_zone: str = ""` to `Settings`.
- `.env.example` — added `# Bright Data SERP API (on-demand web search)` block with `BRIGHTDATA_SERP_ZONE`.
- `apps/memory/src/tree/data/web/types.py` — new `SearchResult` Pydantic model.
- `apps/memory/src/tree/data/web/web_serp.py` — new async `search(...)` client; reuses `BrightDataConfigurationError` / `BrightDataRequestError` from `web_unlocker`.
- `apps/memory/src/tree/data/web/__init__.py` — re-exports `SearchResult` and `search` (existing exports preserved).
- `apps/memory/tests/unit/data/web/test_web_serp.py` — 32 mocked unit tests using `pytest-mock`.

**Tests**
- Unit: 394 passing, 0 failing (32 new in `test_web_serp.py`) — `make memory-unit-tests`.
- Integration: N/A — task #006 is unit-tests only; integration lives in #009.

**Acceptance criteria**
- [x] `settings.brightdata_serp_zone` is `str`, env-var `BRIGHTDATA_SERP_ZONE` — verified by REPL (`ENV_FILE_PATH=$(pwd)/.env uv --directory apps/memory run python -c ...` returned `'your-brightdata-serp-zone'`; default empty string when unset).
- [x] `.env.example` has the new section + placeholder.
- [x] `tree.data.web.types.SearchResult` is a Pydantic v2 BaseModel with `rank/title/url/snippet`; importable as `from tree.data.web import SearchResult`.
- [x] `tree.data.web.web_serp.search(...)` async signature matches spec; importable as `from tree.data.web import search`.
- [x] `ValueError` on empty / whitespace / `None` query — `TestSearchInputValidation`.
- [x] `ValueError` on `num_results < 1` — `TestSearchInputValidation::test_raises_value_error_when_num_results_below_one`.
- [x] `BrightDataConfigurationError` for missing API key / SERP zone — `TestSearchConfiguration` (parametrized).
- [x] `BrightDataRequestError` on 4xx/5xx — `TestSearchHttpBehavior::test_raises_request_error_on_non_2xx` (parametrized 400/401/403/404/429/500/502/503).
- [x] 200 response with two organic entries → two `SearchResult` instances with all fields populated — `test_returns_search_results_on_200`.
- [x] Posts exact JSON body + `Authorization: Bearer ...` header — `TestSearchRequestShape::test_posts_expected_body_and_headers`.
- [x] Google URL has `brd_json=1`, `q=...`, optional `gl/hl/start` — `test_google_url_includes_required_params` (parametrized).
- [x] `num_results=15` → second request has `start=10` — `test_paginates_when_num_results_exceeds_page_size`.
- [x] Empty `organic` returns `[]` — `test_returns_empty_list_when_no_organic_entries`.
- [x] Full type annotations on all public functions / types.
- [x] No `print()` calls — uses `logging.getLogger(__name__)`.
- [x] `make memory-format-check && make memory-lint-check && make memory-unit-tests && make pre-commit` all pass.
- [x] Test file at `apps/memory/tests/unit/data/web/test_web_serp.py` mirrors layout, uses `mocker`, makes no real network calls.

**Evidence**
```
$ make memory-format-check
uv run ruff format --check src/ tests/ scripts/ deploy/
138 files already formatted

$ make memory-lint-check
uv run ruff check src/ tests/ scripts/ deploy/
All checks passed!

$ make pre-commit
uv run --project apps/memory pre-commit run --all-files
Validate pyproject.toml..............................(no files to check)Skipped
prettier.................................................................Passed
ruff check...............................................................Passed
ruff format..............................................................Passed
biome check (harness)....................................................Passed

$ make memory-unit-tests
... (full suite) ...
tests/unit/data/web/test_web_serp.py ................................    [ 49%]
...
======================== 394 passed in 75.06s (0:01:15) ========================
```

REPL verification of the new setting:
```
$ ENV_FILE_PATH=$(pwd)/.env uv --directory apps/memory run python -c \
    "from tree.config.settings import settings; print(repr(settings.brightdata_serp_zone))"
'your-brightdata-serp-zone'
```

Re-export verification:
```
$ uv --directory apps/memory run python -c \
    "from tree.data.web import search, SearchResult; \
     print(list(SearchResult.model_fields.keys())); print(search)"
['rank', 'title', 'url', 'snippet']
<function search at 0x...>
```

**Notes**
- One small enhancement beyond the literal AC: when an organic entry is missing the `link` field, it is skipped (defensive — see `_parse_organic`). This is documented in the docstring and covered by `test_skips_entries_without_link`.
- Per the spec's "defensive" note, when an organic entry has no `rank`, the parser assigns the next 1-indexed positional rank within the cumulative result list (covered by `test_assigns_positional_rank_when_entry_lacks_rank`).
- Bing pagination uses `first` (1-indexed) per Bright Data docs; covered by `TestSearchEngines::test_bing_url_uses_first_offset`.
- Yandex's `language` parameter is not modeled (the SERP-API reference does not enumerate one); documented in the docstring.
- The worktree didn't have a `.env`, so I created one from `.env.example` (placeholders only — no secrets) so `make` targets that include `.env` would run. The file should not be committed and is gitignored at the repo root.
- Did NOT commit per the `/day` flow — Tester gate is next.

### [Tester] 2026-05-01 13:30 — QA

**Test summary**
- Format / lint / pre-commit: PASS (`make memory-format-check`, `make memory-lint-check`, `make pre-commit` all clean)
- Unit tests: 394 passed / 0 failed / 0 warnings (`make memory-unit-tests`, 21.36s)
- Integration tests: NOT RUN — task #006 spec explicitly defers live tests to #009 (mocked-only task)

**E2E adversarial pass** (no live network — exercising the contract via a REPL harness that patches `tree.data.web.web_serp.settings` and `httpx.AsyncClient`):
- Happy path: `await search("hi", num_results=10)` against a 2-entry mocked payload → 2 `SearchResult` instances with rank/title/url/snippet populated. PASS
- Break path 1 (boundary: empty / whitespace / `None` query): all three raise `ValueError("query must not be empty")`. PASS
- Break path 2 (boundary: `num_results=0/-1/-10`): raises `ValueError("num_results must be >= 1")`. PASS
- Break path 3 (config: empty `BRIGHTDATA_API_KEY`): raises `BrightDataConfigurationError("BRIGHTDATA_API_KEY is not set")`. PASS
- Break path 4 (config: empty `BRIGHTDATA_SERP_ZONE`): raises `BrightDataConfigurationError("BRIGHTDATA_SERP_ZONE is not set")`. PASS
- Break path 5 (HTTP: 400/401/403/404/429/500/502/503): each raises `BrightDataRequestError` whose message includes the status code. PASS
- Break path 6 (HTTP: timeout via `httpx.TimeoutException`): propagates as `httpx.TimeoutException` (matches the pattern documented in `web_unlocker.fetch_url`). PASS
- Break path 7 (parse: `{"organic": []}` and missing `organic` key): both return `[]`, no exception. PASS
- Break path 8 (parse: entries missing `link`): silently skipped, only entries with a link are returned. PASS
- Break path 9 (parse: entries missing `rank`): defensive positional 1-indexed cumulative rank assigned (`[1, 2]`). PASS
- Break path 10 (boundary: 10kB query, unicode "résumé 北京 🐍", SQL-injection-like `'; DROP TABLE users; --`): all run without crash; `urlencode` handles the values safely. PASS
- Break path 11 (pagination: `num_results=15`): two POSTs issued; first URL has no `start` param, second URL contains `start=10`. PASS
- Break path 12 (request shape): POST goes to `https://api.brightdata.com/request`, body=`{"zone": "my-serp-zone", "url": "...brd_json=1...q=hello+world", "format": "raw"}`, header `Authorization: Bearer my-key`. PASS

**Acceptance criteria**
- [x] PASS — `tree.config.settings.settings.brightdata_serp_zone` is `str` from env `BRIGHTDATA_SERP_ZONE`
      Evidence: `apps/memory/src/tree/config/settings.py:39` adds `brightdata_serp_zone: str = ""`; REPL `ENV_FILE_PATH=$(pwd)/.env uv --directory apps/memory run python -c "from tree.config.settings import settings; print(repr(settings.brightdata_serp_zone))"` → `'your-brightdata-serp-zone'`; default-empty case verified by REPL without `ENV_FILE_PATH` → `''`.
- [x] PASS — `.env.example` has the new block
      Evidence: `.env.example:28-29` shows `# Bright Data SERP API (on-demand web search)` followed by `BRIGHTDATA_SERP_ZONE=your-brightdata-serp-zone`.
- [x] PASS — `tree.data.web.types.SearchResult` Pydantic v2 BaseModel with `rank/title/url/snippet`
      Evidence: `apps/memory/src/tree/data/web/types.py:8-18`; REPL `from tree.data.web import SearchResult; print(list(SearchResult.model_fields.keys()))` → `['rank', 'title', 'url', 'snippet']`. Confirmed `issubclass(SearchResult, pydantic.BaseModel)` and `hasattr(SearchResult, 'model_fields')`.
- [x] PASS — `tree.data.web.web_serp.search` async signature, importable from `tree.data.web`
      Evidence: `apps/memory/src/tree/data/web/web_serp.py:130-138`; REPL `inspect.iscoroutinefunction(search)` → `True`; signature matches spec exactly with full annotations and `-> 'list[SearchResult]'`.
- [x] PASS — `ValueError` on empty / whitespace / `None` query
      Evidence: `tests/unit/data/web/test_web_serp.py::TestSearchInputValidation::test_raises_value_error_for_empty_query[empty/spaces/whitespace]` and `test_raises_value_error_for_non_string_query`; reproduced live in adversarial harness.
- [x] PASS — `ValueError` on `num_results < 1`
      Evidence: `test_web_serp.py::TestSearchInputValidation::test_raises_value_error_when_num_results_below_one[zero/neg-1/neg-10]`.
- [x] PASS — `BrightDataConfigurationError` with named env var on missing creds
      Evidence: `test_web_serp.py::TestSearchConfiguration::test_raises_configuration_error_when_api_key_empty` (matches `BRIGHTDATA_API_KEY`) and `test_raises_configuration_error_when_serp_zone_empty` (matches `BRIGHTDATA_SERP_ZONE`); reproduced live.
- [x] PASS — `BrightDataRequestError` on 4xx/5xx
      Evidence: `test_web_serp.py::TestSearchHttpBehavior::test_raises_request_error_on_non_2xx[400/401/403/404/429/500/502/503]`; live harness exercised all eight codes — each raised `BrightDataRequestError` with the status in the message.
- [x] PASS — 200 with two organic entries → two populated `SearchResult`s
      Evidence: `test_web_serp.py::TestSearchHttpBehavior::test_returns_search_results_on_200`.
- [x] PASS — Posts exact JSON body and Authorization header
      Evidence: `test_web_serp.py::TestSearchRequestShape::test_posts_expected_body_and_headers`; live harness confirmed `body={"zone": "my-serp-zone", "url": "...", "format": "raw"}` and `Authorization: Bearer my-key`.
- [x] PASS — Google URL has `brd_json=1`, `q=...`, optional `gl/hl/start`
      Evidence: `test_web_serp.py::TestSearchRequestShape::test_google_url_includes_required_params[none/us-en/us-only/en-only]`.
- [x] PASS — `num_results=15` → second POST has `start=10`
      Evidence: `test_web_serp.py::TestSearchPagination::test_paginates_when_num_results_exceeds_page_size`; live harness confirmed `await_count == 2` and second URL contains `start=10`.
- [x] PASS — `{"organic": []}` returns `[]`
      Evidence: `test_web_serp.py::TestSearchHttpBehavior::test_returns_empty_list_when_no_organic_entries` and `test_returns_empty_list_when_organic_key_missing`.
- [x] PASS — Type annotations on all public functions / types
      Evidence: `inspect.signature(search)` shows full annotations on every parameter and return; `_build_serp_url` and `_parse_organic` also fully typed.
- [x] PASS — No `print()` in source
      Evidence: `grep -rn "print(" apps/memory/src/tree/data/web/web_serp.py apps/memory/src/tree/data/web/types.py` returns no matches; module uses `logger = logging.getLogger(__name__)` at line 27.
- [x] PASS — Format / lint / unit / pre-commit all green
      Evidence: see test summary above.
- [x] PASS — Test file at correct path, uses `mocker`, no real network
      Evidence: `apps/memory/tests/unit/data/web/test_web_serp.py` exists alongside `test_web_unlocker.py`; uses `pytest-mock`'s `mocker` fixture throughout (`_patch_settings`, `_patch_async_client`); zero references to live URLs / sockets — all responses are constructed locally via `httpx.Response`.

**Code-shape spot checks**
- `web_serp.py` imports `BrightDataConfigurationError` and `BrightDataRequestError` from `web_unlocker` (lines 22-25) — does NOT redefine. Confirmed by `grep ^class BrightData` finding the classes only in `web_unlocker.py`. Live identity check: `web_serp.BrightDataConfigurationError is web_unlocker.BrightDataConfigurationError` → `True`.
- `tree.data.web.__init__` re-exports `SearchResult` and `search` and preserves the existing `BrightDataConfigurationError`, `BrightDataRequestError`, `fetch_url` exports — `__all__` is alphabetized.
- One small enhancement beyond strict AC: when an organic entry has no `link`, it's skipped. Documented in the `_parse_organic` docstring and covered by `test_skips_entries_without_link`. Aligns with the spec's defensive-parse intent.

**Evidence**
```
$ make memory-format-check
138 files already formatted

$ make memory-lint-check
All checks passed!

$ make pre-commit
prettier ............ Passed
ruff check .......... Passed
ruff format ......... Passed
biome check (harness) Passed

$ make memory-unit-tests
... tests/unit/data/web/test_web_serp.py ................................    [ 49%]
============================= 394 passed in 21.36s =============================
```

**Other issues found**
- None. The module surface is small, the test coverage is dense (32 tests across input validation, configuration, HTTP behavior, request shape, pagination, and engine-specific URL building), and every adversarial break path I attempted behaved correctly.

**VERDICT: PASS**

