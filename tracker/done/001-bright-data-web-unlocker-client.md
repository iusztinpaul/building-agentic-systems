# Bright Data Web Unlocker client + settings + SourceType.WEB

Status: pending
Tags: `data-pipeline`, `web`, `bright-data`, `infrastructure`
Depends on: None
Blocks: #002

## Scope

Add the foundational pieces the new Bright Data web pipeline will sit on, with no Prefect flow yet. Three concerns:

1. **Settings.** Extend `apps/memory/src/tree/config/settings.py` with two new fields:
   - `brightdata_api_key: SecretStr = SecretStr("")` (env var `BRIGHTDATA_API_KEY`)
   - `brightdata_unlocker_zone: str = ""` (env var `BRIGHTDATA_UNLOCKER_ZONE`)

   Both keys use the canonical names from `.claude/skills/bright-data-best-practices/SKILL.md`. Update `.env.example` with both keys + comments matching the rest of the file (placeholder `your-brightdata-api-key`, `your-brightdata-unlocker-zone`).

2. **`SourceType.WEB` enum value.** Extend `tree.entities.documents.SourceType` with `WEB = "web"`. Used by all documents persisted by the new pipeline.

3. **Web Unlocker HTTP client.** Add a new module `tree.data.web` (package: `apps/memory/src/tree/data/web/__init__.py` + `web_unlocker.py`) exposing one async function:

   ```python
   async def fetch_url(
       url: str,
       *,
       data_format: Literal["markdown", "html"] = "markdown",
       timeout_seconds: float = 60.0,
   ) -> str:
       """Fetch the rendered content of a URL via Bright Data Web Unlocker.

       Posts to https://api.brightdata.com/request with:
           Authorization: Bearer <BRIGHTDATA_API_KEY>
           json={"zone": <BRIGHTDATA_UNLOCKER_ZONE>,
                 "url": url,
                 "format": "raw",
                 "data_format": data_format}

       Returns the response body (markdown text by default, raw HTML if requested).

       Raises:
           BrightDataConfigurationError: if BRIGHTDATA_API_KEY or BRIGHTDATA_UNLOCKER_ZONE is empty.
           BrightDataRequestError: on non-2xx responses (wraps response.status_code + body).
           httpx.TimeoutException: on network timeout (caller-friendly, propagated as-is).
       """
   ```

   Implementation rules:
   - Use `httpx.AsyncClient` (already in the dep tree; matches `tree.data.substack.substack_article.fetch_article` style).
   - Read settings via the existing `from tree.config.settings import settings` singleton.
   - Define `BrightDataConfigurationError` and `BrightDataRequestError` as module-level exception classes (sub of `Exception`) in the same file.
   - URL validation: a non-empty string starting with `http://` or `https://`. Reject with `ValueError` otherwise (do not let it reach Bright Data with a billable request).
   - Keep this module **pure** — no MongoDB, no Prefect, no `Document`. Just the HTTP wrapper. The Prefect flow + persistence layer arrive in #002.

No new third-party dependency (use `httpx`, already in `apps/memory/pyproject.toml`).

## Acceptance Criteria

- [x] `tree.entities.documents.SourceType.WEB == "web"` exists and is importable.
- [x] `tree.config.settings.settings.brightdata_api_key` is a `SecretStr` and reads from env var `BRIGHTDATA_API_KEY`.
- [x] `tree.config.settings.settings.brightdata_unlocker_zone` is a `str` and reads from env var `BRIGHTDATA_UNLOCKER_ZONE`.
- [x] `.env.example` lists both env vars with placeholder values and a comment line `# Bright Data Web Unlocker (fallback web scraping)`.
- [x] New module `tree.data.web.web_unlocker` exposes `fetch_url`, `BrightDataConfigurationError`, `BrightDataRequestError`.
- [x] `fetch_url` raises `BrightDataConfigurationError` (with a message naming the missing env var) when either credential is empty — verified by unit test.
- [x] `fetch_url` raises `ValueError` for URLs that are empty or don't start with `http://`/`https://` — verified by unit test.
- [x] `fetch_url` raises `BrightDataRequestError` on a 4xx/5xx response, with message containing the status code — verified by unit test (mock `httpx`).
- [x] On a 200 response, `fetch_url` returns the response body string verbatim — verified by unit test.
- [x] `fetch_url` posts the exact JSON body `{"zone": <zone>, "url": <url>, "format": "raw", "data_format": "markdown"}` (when default) — asserted by unit test against the mocked client.
- [x] All public functions/methods/exceptions have type annotations on parameters and return types.
- [x] `make memory-format-check && make memory-lint-check && make memory-unit-tests` pass.
- [x] Unit-test file at `apps/memory/tests/unit/data/web/test_web_unlocker.py` (mirroring the package layout) covers the cases above; uses `pytest-mock`'s `mocker` fixture, no real network calls.

## User Stories

### Story: Operator configures Bright Data credentials
1. Operator copies `.env.example` to `.env`.
2. Operator pastes their Bright Data API key into `BRIGHTDATA_API_KEY=...`.
3. Operator pastes their Web Unlocker zone name into `BRIGHTDATA_UNLOCKER_ZONE=...`.
4. Operator runs `uv --directory apps/memory run python -c "from tree.config.settings import settings; print(bool(settings.brightdata_api_key.get_secret_value()), settings.brightdata_unlocker_zone)"`.
5. Output: `True my-zone-name` (or whatever they configured) — confirms settings are loaded.

### Story: Developer fetches a URL via the client in a Python REPL
1. Developer ensures `.env` has both credentials set.
2. Developer runs `uv --directory apps/memory run python` to open a REPL.
3. Developer imports: `from tree.data.web.web_unlocker import fetch_url; import asyncio`.
4. Developer runs: `asyncio.run(fetch_url("https://example.com"))`.
5. Developer sees a string of markdown content returned (the example.com page content).

### Story: Developer hits the client with bad input
1. Developer runs `asyncio.run(fetch_url(""))`.
2. Developer sees `ValueError: URL must start with http:// or https://`.
3. Developer runs `asyncio.run(fetch_url("ftp://example.com"))`.
4. Developer sees the same `ValueError`.
5. Developer unsets `BRIGHTDATA_API_KEY`, restarts the REPL, runs `asyncio.run(fetch_url("https://example.com"))`.
6. Developer sees `BrightDataConfigurationError: BRIGHTDATA_API_KEY is not set`.

### Story: SWE writing #002 uses the new SourceType
1. SWE imports `from tree.entities.documents import SourceType, Document`.
2. SWE constructs `Document(source_type=SourceType.WEB, source_uri="https://example.com", title="Example", content="...")`.
3. The Pydantic model accepts the value without raising.

---

Blocked by: (none)

## Log

### [SWE] 2026-04-30 20:50 — Implementation

**Files modified**
- `apps/memory/src/tree/entities/documents.py` — added `SourceType.WEB = "web"`.
- `apps/memory/src/tree/config/settings.py` — added `brightdata_api_key: SecretStr` and `brightdata_unlocker_zone: str` fields on `Settings`.
- `apps/memory/src/tree/data/web/__init__.py` — new package; re-exports `fetch_url`, `BrightDataConfigurationError`, `BrightDataRequestError`.
- `apps/memory/src/tree/data/web/web_unlocker.py` — new module: async `fetch_url` posting to `https://api.brightdata.com/request` via `httpx.AsyncClient`, with URL validation, credential checks, and 2xx-only success handling. Defines `BrightDataConfigurationError` and `BrightDataRequestError`.
- `.env.example` — added section comment + `BRIGHTDATA_API_KEY` / `BRIGHTDATA_UNLOCKER_ZONE` placeholders.
- `apps/memory/tests/unit/data/web/__init__.py` — package marker.
- `apps/memory/tests/unit/data/web/test_web_unlocker.py` — 18 unit tests covering credential errors, URL validation, 2xx body passthrough, 4xx/5xx → `BrightDataRequestError`, request body shape (zone/url/format=raw/data_format=markdown), and `data_format="html"` passthrough.

**Tests**
- Unit: 319 passing, 0 failing, 0 warnings — `make memory-unit-tests`.
- Integration: N/A — task adds only settings + a pure HTTP wrapper, no infra changes.

**Acceptance criteria**
- [x] `SourceType.WEB == "web"` — verified by REPL: `SourceType('web') == SourceType.WEB`.
- [x] `settings.brightdata_api_key` is `SecretStr` reading from `BRIGHTDATA_API_KEY` — verified by REPL (`type(...).__name__ == 'SecretStr'`).
- [x] `settings.brightdata_unlocker_zone` is `str` reading from `BRIGHTDATA_UNLOCKER_ZONE` — verified by REPL.
- [x] `.env.example` updated with section comment + both keys — verified by Read.
- [x] `tree.data.web.web_unlocker` exposes the three public symbols — verified by import + tests.
- [x] `BrightDataConfigurationError` raised when either credential is empty, message names the missing env var — `tests/unit/data/web/test_web_unlocker.py::TestFetchUrlConfiguration` (2 cases).
- [x] `ValueError` for bad URLs — `TestFetchUrlValidation::test_raises_value_error_for_bad_url` (5 parametrized cases).
- [x] `BrightDataRequestError` on 4xx/5xx with status code in message — `TestFetchUrlHttpBehavior::test_raises_request_error_on_non_2xx` (8 parametrized cases).
- [x] 200 response returns body verbatim — `test_returns_response_body_verbatim_on_200`.
- [x] Exact request body shape (`zone`, `url`, `format=raw`, `data_format=markdown`) + `Authorization: Bearer ...` header — `test_posts_expected_request_body_and_headers`.
- [x] Type annotations on all public surface — `fetch_url`, `_validate_url`, `_resolve_credentials` annotated on every parameter and return.
- [x] `make memory-format-check && make memory-lint-check && make memory-unit-tests` pass — outputs below.
- [x] Unit-test file at `apps/memory/tests/unit/data/web/test_web_unlocker.py` mirrors `src/tree/data/web/`, uses `mocker` fixture, no real network.

**Evidence**

```
$ make memory-format-check && make memory-lint-check
uv run ruff format --check src/ tests/ scripts/ deploy/
128 files already formatted
uv run ruff check src/ tests/ scripts/ deploy/
All checks passed!

$ make memory-unit-tests
... (snip) ...
tests/unit/data/web/test_web_unlocker.py ..................              [ 43%]
... (snip) ...
======================== 319 passed in 67.67s (0:01:07) ========================

$ make pre-commit
prettier.................................................................Passed
ruff check...............................................................Passed
ruff format..............................................................Passed
biome check (harness)....................................................Passed
```

End-to-end exercise (REPL):

```
$ uv --directory apps/memory run python -c "
import asyncio
from tree.data.web.web_unlocker import fetch_url, BrightDataConfigurationError
async def main():
    try: await fetch_url('')
    except ValueError as e: print('empty url ->', repr(str(e)))
    try: await fetch_url('ftp://example.com')
    except ValueError as e: print('ftp url   ->', repr(str(e)))
    try: await fetch_url('https://example.com')
    except BrightDataConfigurationError as e: print('no creds  ->', repr(str(e)))
asyncio.run(main())
"
empty url -> 'URL must start with http:// or https://'
ftp url   -> 'URL must start with http:// or https://'
no creds  -> 'BRIGHTDATA_API_KEY is not set'
```

**Notes**
- Did NOT exercise a real successful 200 fetch end-to-end (would require live Bright Data credentials + a billable request). All success paths covered by `httpx`-mocked unit tests per the spec ("no real network calls").
- Story #4 (SWE constructs `Document(source_type=SourceType.WEB, ...)`) verified at the Pydantic-validation level via `SourceType('web') == SourceType.WEB`. Constructing the full `Document` object also requires Beanie collection initialization (`init_beanie`) — out of scope for #001 and exercised in downstream tasks #002/#005.
- Created `apps/memory/tests/unit/data/web/__init__.py` to make the test directory a proper package (matches the pattern for `tests/unit/data/substack/`).
- Per orchestrator instructions, skipped Step 3 (branch creation) — already inside the worktree on `feat/bright-data-fallback-source`.
- Did NOT commit per `/night` flow — awaiting Tester PASS + PM ACCEPT.

### [Tester] 2026-04-30 21:30 — QA

**Test summary**
- Format check: PASS (`make memory-format-check` — 128 files already formatted)
- Lint check: PASS (`make memory-lint-check` — All checks passed)
- Pre-commit: PASS (prettier, ruff check, ruff format, biome — all green)
- Unit tests: 319 passed / 0 failed / 0 warnings (`make memory-unit-tests`, 16.74s)
- Integration tests: 57 passed / 0 failed / 0 warnings (`make memory-integration-tests`, 59.15s) — confirms no regression in the existing suite (no new integration tests expected for #001).

**E2E adversarial pass**
- Happy path (mocked httpx, valid creds, `https://example.com/article`): request POSTs to `https://api.brightdata.com/request` with exact body `{"zone": "my-zone", "url": "https://example.com/article", "format": "raw", "data_format": "markdown"}` and `Authorization: Bearer my-key` header; returns response body verbatim. PASS (covered by `test_posts_expected_request_body_and_headers` + REPL probe).
- Break path 1 (boundary: missing creds, one at a time): Empty `BRIGHTDATA_API_KEY` → `BrightDataConfigurationError: BRIGHTDATA_API_KEY is not set`. Empty `BRIGHTDATA_UNLOCKER_ZONE` → `BrightDataConfigurationError: BRIGHTDATA_UNLOCKER_ZONE is not set`. Both name the missing env var exactly. PASS.
- Break path 2 (malformed URLs): empty/whitespace/`ftp://`/`javascript:`/plain text → `ValueError: URL must start with http:// or https://`. `None` → cleanly raises the same `ValueError` (caught by `not isinstance(url, str)` guard). `http://` (no host), `https:// space.com`, IDN `https://例え.jp` → URL-validation **passes** (spec is a literal prefix check; downstream Bright Data 4xx will surface them). Spec-compliant. PASS.
- Break path 3 (HTTP errors): mocked 400/401/403/404/429/500/502/503 all raise `BrightDataRequestError` with the status code embedded in the message (`"Bright Data Web Unlocker returned HTTP 503: ..."`). Probed 3xx (301/302/304) and 1xx (100/199) — also raise `BrightDataRequestError` (correct: success path is `200 <= status < 300`). PASS.
- Break path 4 (deviation #2 probe — 2xx vs strict 200): mocked 200/201/204/299 all return body verbatim; 199/300 raise. The "any 2xx" interpretation does not violate the AC ("On a 200 response, returns body verbatim") — strict 200 is a subset of the implemented behavior, and the broader band is sensible for a generic HTTP wrapper (204 returns "" cleanly rather than crashing). **Deviation accepted.**
- Break path 5 (test rigor — exact-shape vs key-presence): inspected `test_web_unlocker.py:156-161` — assertion uses `==` against a complete dict literal `{"zone", "url", "format", "data_format"}`, so any extra/missing key in the production payload would fail the test. PASS.

**Acceptance criteria**
- [x] PASS — `SourceType.WEB == "web"` exists and is importable. Evidence: `apps/memory/src/tree/entities/documents.py:15`; REPL `SourceType('web') == SourceType.WEB → True`.
- [x] PASS — `settings.brightdata_api_key` is a `SecretStr` reading from `BRIGHTDATA_API_KEY`. Evidence: `apps/memory/src/tree/config/settings.py:37`; REPL `type(settings.brightdata_api_key).__name__ == 'SecretStr'`.
- [x] PASS — `settings.brightdata_unlocker_zone` is a `str` reading from `BRIGHTDATA_UNLOCKER_ZONE`. Evidence: `apps/memory/src/tree/config/settings.py:38`.
- [x] PASS — `.env.example` lists both env vars + section comment `# Bright Data Web Unlocker (fallback web scraping)`. Evidence: `.env.example:24-26`.
- [x] PASS — `tree.data.web.web_unlocker` exposes `fetch_url`, `BrightDataConfigurationError`, `BrightDataRequestError`. Evidence: `apps/memory/src/tree/data/web/__init__.py` re-exports all three; direct import succeeds.
- [x] PASS — `BrightDataConfigurationError` raised on empty credentials, message names the missing var. Evidence: `tests/unit/data/web/test_web_unlocker.py::TestFetchUrlConfiguration` (2 cases) + REPL probe.
- [x] PASS — `ValueError` for empty / non-`http(s)` URLs. Evidence: `TestFetchUrlValidation::test_raises_value_error_for_bad_url` parametrized over 5 cases (empty, ftp, no-scheme, whitespace, javascript).
- [x] PASS — `BrightDataRequestError` on 4xx/5xx with status code in message. Evidence: `TestFetchUrlHttpBehavior::test_raises_request_error_on_non_2xx` parametrized over 8 codes (400/401/403/404/429/500/502/503).
- [x] PASS — 200 response returns body verbatim. Evidence: `test_returns_response_body_verbatim_on_200`.
- [x] PASS — Exact request body shape `{zone, url, format=raw, data_format=markdown}`. Evidence: `test_posts_expected_request_body_and_headers` uses `==` against a full dict literal (line 156-161); `Authorization: Bearer ...` header asserted at line 162.
- [x] PASS — Type annotations on all public surface. Evidence: `web_unlocker.py:35,45,59-64` — `_validate_url(url: str) -> None`, `_resolve_credentials() -> tuple[str, str]`, `fetch_url(url: str, *, data_format: Literal[...], timeout_seconds: float) -> str`. Exception classes have docstrings; no parameters to annotate.
- [x] PASS — `make memory-format-check && make memory-lint-check && make memory-unit-tests` all pass. Evidence: outputs above.
- [x] PASS — Unit-test file at `apps/memory/tests/unit/data/web/test_web_unlocker.py` mirrors package layout, uses `mocker` fixture, no real network. Evidence: file present, all `httpx.AsyncClient` calls mocked via `mocker.patch`.

**Spot-check items**
- `SourceType` literal aliases: `grep -rn "Literal\[" apps/memory/src` — no source-type Literal aliases need updating (only `data_format: Literal["markdown", "html"]` introduced by this task).
- Exception hierarchy: both `BrightDataConfigurationError` and `BrightDataRequestError` directly subclass `Exception` — confirmed via REPL `__bases__ == (Exception,)`.
- `timeout_seconds` propagation: probed via `unittest.mock.patch` on `httpx.AsyncClient` — `fetch_url(..., timeout_seconds=12.5)` → `AsyncClient(timeout=12.5)`. PASS.
- `.env` workaround: `git status --porcelain` does NOT include `.env` (line 141 of `.gitignore` covers it). Clean.
- No `print()` calls in library code — uses `logging.getLogger(__name__)` (line 22 of `web_unlocker.py`).
- All dates / mocks: N/A for this task (no datetime handling).
- `git diff --stat` clean: only the 3 expected files modified + 4 new files in `apps/memory/{src,tests}/.../web/` — no stray `git add -A` damage.

**Evidence**
```
$ make memory-unit-tests
... tests/unit/data/web/test_web_unlocker.py .................. [ 43%] ...
============================= 319 passed in 16.74s =============================

$ make memory-integration-tests
... ============================= 57 passed in 59.15s ==============================

$ make pre-commit
prettier..........Passed
ruff check........Passed
ruff format.......Passed
biome check.......Passed
```

**Other issues found (PASS-with-note, non-blocking)**
- Story #4 (constructing a full `Document(source_type=SourceType.WEB, ...)`) is end-to-end exercised only at the enum level here. Spec explicitly defers Beanie-init wiring to #002/#005, so this is a known and acceptable scope deferral — the enum value itself is wired in correctly.
- The "any 2xx success" interpretation differs slightly from the literal AC wording ("On a 200 response..."). I accept it as the more correct generic-HTTP-wrapper behavior; the SWE should call this out in the PR description so reviewers don't have to re-derive it. No fix required.
- URL validator is intentionally a prefix check, so `http://` (no host), `https:// space.com`, and IDN slip through to Bright Data. This is per spec ("non-empty string starting with `http://` or `https://`"). Mentioning here as a follow-up consideration if #002 ever wants stricter pre-flight URL parsing — not a #001 issue.

**VERDICT: PASS**
