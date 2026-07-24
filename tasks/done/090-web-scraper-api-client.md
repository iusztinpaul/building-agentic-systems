---
id: 090-web-scraper-api-client
feature: brightdata-youtube-transcripts
status: done
---

# Generic Bright Data Web Scraper API client (trigger → poll → download)

Tags: `data`, `infra`
Depends on: None
Blocks: #091
Implements: ADR-004

## Scope

Create `apps/memory/src/tree/data/web/web_scraper_api.py` — a thin, pure async client
for the Bright Data Web Scraper API (dataset collections), beside the existing
`web_unlocker.py` and mirroring its style: module functions, no MongoDB, no Prefect, no
`Document`, REST via `httpx` (NOT the Node `brightdata` CLI). Purely additive — nothing
calls it yet. Reference: `.agents/skills/bright-data-best-practices/references/web-scraper-api.md`.

**API surface (verified live — do not re-derive):**

- `POST https://api.brightdata.com/datasets/v3/trigger?dataset_id=…&format=json` with
  body `{"input": [{"url": "…"}, …]}` → `{"snapshot_id": "sd_…"}`.
- `GET  https://api.brightdata.com/datasets/v3/progress/{snapshot_id}` → `status` in
  `starting | running | ready | failed`.
- `GET  https://api.brightdata.com/datasets/v3/snapshot/{snapshot_id}?format=json` →
  list of record dicts.
- Auth on every call: `Authorization: Bearer <BRIGHTDATA_API_KEY>` (existing
  `settings.brightdata_api_key`; NO zone — unlike Web Unlocker).

**Always-async, never sync `/scrape`** (ADR-004 Decision 2): the measured collection was
~173 s for ONE video, so essentially every call would exceed the sync 1-minute window
and 202-fallthrough into the same polling logic — sync-first would be a second code
path for nothing.

**Design:**

- Public entrypoint: `async def collect(dataset_id: str, inputs: list[dict[str, Any]],
  *, timeout_seconds: float, poll_interval_seconds: float) -> list[dict[str, Any]]` —
  trigger, poll `progress` every `poll_interval_seconds` until `ready`/`failed`/timeout,
  download and return the parsed record list. All knobs are parameters — this module
  reads NO YAML config (the caller owns the knobs; #091 wires them).
- Errors, reusing `web_unlocker`'s error-class style: import `BrightDataConfigurationError`
  and `BrightDataRequestError` from `tree.data.web.web_unlocker` (no duplication); define
  `BrightDataTimeoutError(Exception)` here for the bounded-wait expiry. Missing/empty
  API key → `BrightDataConfigurationError` BEFORE any HTTP call. `failed` status or any
  non-2xx → `BrightDataRequestError` including status/body context. Timeout →
  `BrightDataTimeoutError` naming the snapshot_id and the elapsed bound.
- Base URL(s) as module constants, mirroring `_BRIGHTDATA_REQUEST_URL`.
- Keep thin, individually patchable private HTTP helpers (e.g. `_post_json` / `_get_json`)
  so unit tests patch exactly one seam per call — the same "kept thin so tests can patch
  one method" property as `GeminiTranscriptFetcher._call_gemini`.
- Poll sleeping via `asyncio.sleep`; native `logging` (never prints); full type
  annotations including `-> None`.

Unit tests only (call the `/squid-testing-python` skill), fully mocked HTTP — NO live
Bright Data call, ever: happy path (trigger → running → ready → records), `failed`
status, trigger rejected (non-2xx), missing key, timeout expiry (patch sleep or clock),
empty `inputs` (return `[]` without any HTTP call).

## Acceptance criteria

- [x] `apps/memory/src/tree/data/web/web_scraper_api.py` exists with `collect(...)` as
      specified; trigger/progress/download URLs and auth header match the verified API
      facts; `format=json` on trigger and snapshot download.
- [x] Missing `BRIGHTDATA_API_KEY` raises `BrightDataConfigurationError` before any HTTP
      request (assert zero HTTP calls in the test).
- [x] `status == "failed"` and any non-2xx response raise `BrightDataRequestError` with
      the status/body in the message; exceeding `timeout_seconds` raises
      `BrightDataTimeoutError` naming the snapshot_id.
- [x] Polling honors `poll_interval_seconds` and stops immediately on `ready`;
      `collect(dataset_id, [])` returns `[]` with zero HTTP calls.
- [x] The two existing exception classes are imported from `web_unlocker`, not
      redefined; only `BrightDataTimeoutError` is new.
- [x] No YAML/config read inside the module; no MongoDB/Prefect imports; NO live
      integration test added (unit tests fully mocked).
- [x] Nothing outside this module + its tests changes (`git diff --stat` shows only the
      new module and new test file).
- [x] `make memory-format-check && make memory-lint-check && make pre-commit` clean;
      `make memory-unit-tests` green, 0 warnings.

## Out of scope

- Anything YouTube-specific (dataset id, record mapping) — #091. Config knobs in YAML —
  #091. Sync `/scrape`, webhooks (`notify`), S3 delivery, `custom_output_fields`,
  ndjson streaming — not needed at this batch size (ADR-004).

## Log

### [SWE] 2026-07-24 — Implementation

**Files modified**

- `apps/memory/src/tree/data/web/web_scraper_api.py` (new) — generic async Bright Data
  Web Scraper API client: `collect()` = trigger → poll `/progress` → download
  `/snapshot`; thin patchable `_post_json` / `_get_json` seams; `BrightDataTimeoutError`.
- `apps/memory/tests/unit/data/web/test_web_scraper_api.py` (new) — 18 fully mocked unit
  tests (no live Bright Data traffic anywhere).
- `tasks/090-web-scraper-api-client.md` — `status: in-progress`, ACs ticked, this entry.

**Tests**

- Unit: 1750 passing, 0 failing, 0 warnings (18 of them new in
  `tests/unit/data/web/test_web_scraper_api.py`) — `make memory-unit-tests`.
- Integration: N/A — module is purely additive, touches no infra; per the spec NO live
  integration test was added.

**Acceptance criteria**

- [x] Module + `collect(...)` with verified URLs/auth/`format=json` — verified by
      `test_web_scraper_api.py::TestCollectHappyPath::test_calls_trigger_progress_and_snapshot_endpoints`
      and `TestHttpErrorPropagation::test_trigger_sends_bearer_auth_header`.
- [x] Missing key → `BrightDataConfigurationError` before any HTTP call —
      `TestCollectConfiguration::test_raises_configuration_error_when_api_key_empty`
      (asserts both HTTP seams `assert_not_awaited`).
- [x] `failed` / non-2xx → `BrightDataRequestError`; timeout → `BrightDataTimeoutError`
      naming the snapshot id — `TestCollectFailures::test_raises_request_error_when_status_is_failed`,
      `TestHttpErrorPropagation::test_trigger_non_2xx_raises_request_error_with_status_and_body`
      (7 status codes), `TestCollectFailures::test_raises_timeout_error_naming_snapshot_id`.
- [x] Poll interval honored, stops immediately on `ready`, `collect(ds, [])` → `[]` with
      zero HTTP — `test_sleeps_the_requested_poll_interval_between_polls`,
      `test_stops_polling_immediately_when_first_status_is_ready`,
      `TestCollectEmptyInputs::test_returns_empty_list_without_any_http_call`.
- [x] Error classes imported from `web_unlocker`; only `BrightDataTimeoutError` new.
- [x] No YAML/config read, no MongoDB/Prefect imports, no live test.
- [x] Only the new module + new test file changed (plus this task file).
- [x] format-check / lint-check / pre-commit clean; unit tests green with 0 warnings.

**Evidence**

```
$ make env-status
Env target: local (.env)

$ make memory-unit-tests
tests/unit/data/web/test_web_scraper_api.py ..................           [ 25%]
============================ 1750 passed in 43.27s =============================

$ make memory-format-check && make memory-lint-check
291 files already formatted
All checks passed!

$ make pre-commit
prettier.................................................................Passed
ruff check...............................................................Passed
ruff format..............................................................Passed
biome check (harness)....................................................Passed
```

End-to-end run (real `httpx` against a local stub HTTP server serving the three
endpoints — deliberately NOT the live API: a real collection costs money and ~173 s):

```
--- happy path ---
records: [{'url': 'https://www.youtube.com/watch?v=abc123', 'title': 'Stubbed video', ...}]
polls: 3
trigger path: /datasets/v3/trigger?dataset_id=gd_lk56epmy2i5g7lzu0k&format=json
trigger auth: Bearer local-stub-key
trigger body: {'input': [{'url': 'https://www.youtube.com/watch?v=abc123'}]}
progress path: /datasets/v3/progress/sd_local_stub
snapshot path: /datasets/v3/snapshot/sd_local_stub?format=json
--- empty inputs ---
result: []
--- missing key ---
raised: BrightDataConfigurationError BRIGHTDATA_API_KEY is not set
--- timeout ---
raised: BrightDataTimeoutError Bright Data snapshot sd_local_stub still 'running' after 0.5s
```

**Notes**

- NOT RUN — no live Bright Data call was made at any point (hard constraint); the live
  probe stays in #093.
- Two decisions beyond the literal spec, both cheap to reverse:
  1. `_HTTP_REQUEST_TIMEOUT_SECONDS = 60.0` module constant for the PER-REQUEST httpx
     timeout, distinct from `timeout_seconds` (the whole-collection bound). httpx
     defaults to 5 s, too short for a snapshot download; 60 s mirrors
     `web_unlocker.fetch_url`. Not a config knob — API-shape constant, like the URLs.
  2. `collect()` returns `[]` for empty `inputs` BEFORE reading credentials, so a no-op
     call never needs an API key. Documented in the docstring.
- The `_download_snapshot` guard rejects a non-list snapshot body with
  `BrightDataRequestError` so the `list[dict]` return annotation cannot lie to #091.
- Poll waits `min(poll_interval_seconds, remaining)` so the last sleep never overshoots
  the caller's bound.

### [SWE] 2026-07-24 — Fixes (QA break path 6: 2xx + non-JSON body)

**Issue**

`_parse_json` validated the status code, then called `response.json()` unconditionally.
A WAF / rate-limit / captcha HTML page or an empty body served with HTTP 200 leaked a
raw `json.decoder.JSONDecodeError` out of `collect()`. That is not one of the three
Bright Data error types #092's fallback chain catches (ADR-004, Decision 3), so it would
have escaped the batch-wide Gemini fallback and hard-failed the Prefect task — the exact
failure the design exists to prevent.

**Files modified**

- `apps/memory/src/tree/data/web/web_scraper_api.py` — `_parse_json` now wraps
  `response.json()` in `try` / `except ValueError` and re-raises
  `BrightDataRequestError` with the same status / request-URL / body context shape as
  the non-2xx branch; new `_body_excerpt()` helper + `_MAX_ERROR_BODY_CHARS = 500`
  constant truncate long bodies.
- `apps/memory/tests/unit/data/web/test_web_scraper_api.py` — 5 new regression tests in
  `TestHttpErrorPropagation`.

**Fix detail**

- Catching `ValueError` covers BOTH decode failures `response.json()` can raise —
  `json.JSONDecodeError` (not JSON) and `UnicodeDecodeError` (binary body), both
  `ValueError` subclasses — while still letting unrelated exceptions surface as real
  bugs. Written as a comment in the module so it is not "simplified" away later.
- Truncation is applied via the shared `_body_excerpt()` used by BOTH branches, not just
  the new one: the non-2xx branch could dump a full HTML page into a log line too. One
  helper, root cause fixed once.

**Tests**

- Regression tests written FIRST and confirmed red with the reported
  `json.decoder.JSONDecodeError` before the fix landed.
- 5 new tests: `test_2xx_with_non_json_body_raises_request_error` (parametrized —
  captcha HTML, empty body, whitespace body; asserts type plus status + URL in the
  message), `test_non_json_body_is_truncated_in_the_error_message` (10 KB body →
  message < 1 000 chars), `test_non_json_snapshot_body_raises_request_error` (proves the
  guard covers the snapshot GET, not only the trigger POST).
- Unit: 1755 passing, 0 failing, 0 warnings (23 in this module's file, up from 18).
- Integration: N/A — no infra touched.

**Evidence**

```
$ make env-status
Env target: local (.env)

$ make memory-unit-tests
tests/unit/data/web/test_web_scraper_api.py .......................      [ 25%]
============================ 1755 passed in 44.42s =============================

$ make memory-format-check && make memory-lint-check
291 files already formatted
All checks passed!

$ make pre-commit
prettier.................................................................Passed
ruff check...............................................................Passed
ruff format..............................................................Passed
biome check (harness)....................................................Passed
```

Re-ran the end-to-end smoke over REAL `httpx` against a local `ThreadingHTTPServer`
returning HTTP 200 with a 12 KB captcha HTML page (no Bright Data traffic):

```
type: BrightDataRequestError
is BrightDataRequestError: True
body len: 12057 -> message len: 669
message: Bright Data Web Scraper API returned HTTP 200 with a non-JSON body for
http://127.0.0.1:56020/datasets/v3/trigger?dataset_id=gd_x&format=json:
<html><body>Atten ... dpadpadpadpadp… (12057 chars, truncated)
```

**Notes**

- `_HTTP_REQUEST_TIMEOUT_SECONDS` and the empty-inputs-before-credential-check ordering
  were left exactly as reviewed and accepted — untouched.
- Scope unchanged: still only the new module + its test file (plus this task file). No
  live Bright Data call was made at any point.

### [Tester] 2026-07-24 14:05 — QA

**Test summary**
- Format / lint / pre-commit: PASS (`make memory-format-check`, `make memory-lint-check`,
  `make pre-commit` all clean; 291 files already formatted, ruff/prettier/biome all Passed)
- Unit tests: 1750 passed / 0 failed (`make memory-unit-tests`, 45.42s)
- Integration tests: 167 passed / 2 failed / 1 skipped / 105 deselected
  (`make memory-integration-tests`) — the 2 failures are
  `test_indexing_pipeline.py::test_embeds_nodes` and
  `test_meta_state.py::TestRecordDreamRun::test_updated_at_is_recent`, both confirmed
  pre-existing (task 089 QA) and unrelated to this module (this task touches no
  MongoDB/Prefect/embedding code). Not attributed to this task.
- Warnings: 0

**Scope check**
- `git status` / `git diff --stat`: only `web_scraper_api.py` (new) and
  `test_web_scraper_api.py` (new) are this task's actual changes. `docs/glossary.md`
  (modified), `docs/adrs/004_*.md`, `tasks/091-093-*.md`, and
  `tests/unit/data/youtube/fixtures/` are pre-existing uncommitted grooming artifacts
  for later tasks, untouched by this diff — confirmed, not flagged as scope creep.
- `env-status` → local (.env), as required.

**E2E adversarial pass**
- Happy path: `collect(dataset_id, [{"url": "..."}], timeout_seconds=60, poll_interval_seconds=1)`
  against a mocked `running → ready → [records]` sequence → returns the record list
  verbatim; trigger/progress/snapshot URLs, `format=json`, and `Authorization: Bearer`
  header all verified via `test_calls_trigger_progress_and_snapshot_endpoints` and
  `test_trigger_sends_bearer_auth_header`. Also re-verified with a **real httpx client
  against a local `ThreadingHTTPServer` stub** (no live Bright Data). PASS.
- Break path 1 (malformed: `progress` response with the `status` key missing entirely):
  scripted `collect()` against `{"foo": "bar"}` on every poll → `BrightDataTimeoutError`
  raised cleanly after the bound expired (`status` treated as `None`, loop just keeps
  polling until timeout). PASS.
- Break path 2 (state edge: `poll_interval_seconds` (10s) larger than `timeout_seconds`
  (1s), real `asyncio.sleep` + real clock): exactly one sleep call of `~0.9999s` (not
  10s), `BrightDataTimeoutError` raised at ~1.0s wall-clock elapsed — confirms
  `min(poll_interval_seconds, remaining)` clamps correctly and the final wait never
  overshoots the caller's bound. PASS.
- Break path 3 (state edge: `failed` status on the very first poll): `_get_json`
  returns `{"status": "failed", ...}` on poll #1 → `BrightDataRequestError` raised
  immediately, exactly 1 progress call made (no wasted second poll). PASS.
- Break path 4 (malformed: trigger response missing `snapshot_id` entirely, extra
  unrelated keys instead): `BrightDataRequestError` raised naming the payload, zero
  further calls. PASS.
- Break path 5 (state edge: missing API key + empty `inputs`): `collect(ds, [],
  timeout_seconds=60, poll_interval_seconds=1)` with `BRIGHTDATA_API_KEY=""` returns
  `[]` silently — **no** `BrightDataConfigurationError` is raised, because the
  empty-inputs short-circuit runs before the credential check. This is the ordering the
  orchestrator flagged for scrutiny. Verdict: **acceptable, not a defect** — both
  relevant ACs are satisfied literally as written (AC2 is exercised only with non-empty
  `inputs` in the committed test; AC4 says `collect(dataset_id, [])` returns `[]` with
  zero HTTP calls, full stop, no credential carve-out), the behavior is documented in
  the docstring and the Log, and per ADR-004 Decision 7 the real credential gate
  ("neither backend configured → RuntimeError") lives in #091's shared task, one level
  above this module — so this module silently no-op'ing on an empty batch regardless of
  credentials does not weaken that gate. Flagged as a note for #091's SWE to double
  check their up-front credential check does not itself rely on calling `collect()`
  with a possibly-empty list to detect a missing key.
- **Break path 6 (malformed: 2xx response whose body is not valid JSON) — FAIL.**
  Reproduced two ways: (a) mocked `httpx.Response(status_code=200, text="<html>...</html>")`
  and (b) a **real** local `ThreadingHTTPServer` returning `200` with an HTML body
  (`"<html><body>rate limited, try again</body></html>"`) for the trigger call, real
  `httpx.AsyncClient` making the real request. Both reproduce:
  ```
  json.decoder.JSONDecodeError: Expecting value: line 1 column 1 (char 0)
  ```
  propagating raw out of `collect()`, uncaught, with **zero logging** and **not one of
  the three documented exception types** (`BrightDataConfigurationError` /
  `BrightDataRequestError` / `BrightDataTimeoutError`) or `httpx.TimeoutException`. This
  is a realistic Bright Data failure mode — a WAF/rate-limit/captcha page or empty body
  returned with a 2xx status is a known scraping-API behavior, not a contrived edge
  case. `_parse_json` (`web_scraper_api.py:257-266`) checks the status code but calls
  `response.json()` unconditionally afterward with no exception handling, unlike the
  non-2xx path which is wrapped with context (status/body/URL). Caller (#091's fetcher)
  would see a bare `JSONDecodeError` instead of a `BrightDataRequestError` it can catch
  and treat as a fallback trigger per ADR-004 Decision 3.
  **Fix:** wrap the `response.json()` call in `_parse_json` in a
  `try/except (ValueError,)` (or the narrower `json.JSONDecodeError`) and re-raise as
  `BrightDataRequestError` including status code, response text, and request URL — same
  context shape already used for the non-2xx branch. Add a regression test (2xx status,
  non-JSON body) alongside the existing 7-status-code `TestHttpErrorPropagation`
  parametrization.

**Acceptance criteria**
- [x] PASS — module + `collect(...)` with verified URLs/auth/`format=json` —
      `test_calls_trigger_progress_and_snapshot_endpoints`,
      `test_trigger_sends_bearer_auth_header`; code read confirms
      `_BRIGHTDATA_TRIGGER_URL`/`_PROGRESS_URL`/`_SNAPSHOT_URL` match the spec exactly.
- [x] PASS — missing key → `BrightDataConfigurationError` before any HTTP request —
      `TestCollectConfiguration::test_raises_configuration_error_when_api_key_empty`
      (asserts both seams `assert_not_awaited`); re-verified with scripted repro.
- [x] PASS — `failed`/non-2xx → `BrightDataRequestError`; timeout →
      `BrightDataTimeoutError` naming snapshot_id —
      `TestCollectFailures::test_raises_request_error_when_status_is_failed`,
      `TestHttpErrorPropagation::test_trigger_non_2xx_raises_request_error_with_status_and_body`
      (7 status codes), `test_raises_timeout_error_naming_snapshot_id`. NOTE: this AC is
      satisfied as literally written (non-2xx status codes), but the adversarial pass
      found an adjacent, un-covered failure mode — a 2xx response with a non-JSON body —
      that leaks a raw `JSONDecodeError` instead. See Break path 6 above; this is the
      basis of the FAIL verdict even though the AC's literal wording passes.
- [x] PASS — poll interval honored, stops immediately on `ready`, `collect(ds, [])` → `[]`
      with zero HTTP — `test_sleeps_the_requested_poll_interval_between_polls`,
      `test_stops_polling_immediately_when_first_status_is_ready`,
      `TestCollectEmptyInputs::test_returns_empty_list_without_any_http_call`; poll
      clamping (`min(poll_interval_seconds, remaining)`) independently verified with a
      real clock/real sleep (break path 2).
- [x] PASS — error classes imported from `web_unlocker`, not redefined — code read,
      `web_scraper_api.py:29-32`; only `BrightDataTimeoutError` newly defined (line 49).
- [x] PASS — no YAML/config read, no MongoDB/Prefect imports, no live test —
      `grep '^import\|^from'` shows only `asyncio`, `logging`, `time`, `httpx`,
      `tree.config.settings` (env-var settings, not YAML `app_config`), and
      `tree.data.web.web_unlocker`.
- [x] PASS — only the new module + new test file changed — `git status`/`git diff --stat`
      confirmed; all other untracked/modified files are pre-existing grooming artifacts
      for #091-093, untouched by this diff.
- [x] PASS — format/lint/pre-commit clean; unit tests green, 0 warnings — reproduced
      independently (see Test summary).

**Evidence**
```
$ make memory-unit-tests
tests/unit/data/web/test_web_scraper_api.py ..................           [ 25%]
============================ 1750 passed in 45.42s =============================

$ make memory-integration-tests
FAILED tests/integration/memory/test_indexing_pipeline.py::TestMemoryIndexingPipeline::test_embeds_nodes
FAILED tests/integration/memory/test_meta_state.py::TestRecordDreamRun::test_updated_at_is_recent
2 failed, 167 passed, 1 skipped, 105 deselected in 153.55s   (both pre-existing, unrelated)

$ uv run python - <<'EOF'   # real httpx + real local HTTP server, 200 + HTML body
...
LEAKED via real httpx + real local server: json.decoder.JSONDecodeError: Expecting value: line 1 column 1 (char 0)
EOF
```

**Other issues found**
- None beyond Break path 6 above and the empty-inputs-before-credential-check ordering
  (judged acceptable, noted for #091).

**VERDICT: FAIL**

One issue to fix: `_parse_json` must not let a 2xx response with a non-JSON body leak a
raw `json.JSONDecodeError` — wrap it as `BrightDataRequestError` with status/body/URL
context, matching the existing non-2xx handling, and add a regression test. Everything
else (all 8 literal ACs, full unit/integration suite, format/lint/pre-commit, scope,
and the other 5 adversarial break paths) passes cleanly.

### [Tester] 2026-07-24 15:40 — Re-QA (fix verification)

Scope per orchestrator instruction: verify only the fix for the single blocker raised
above (raw `JSONDecodeError` leak on a 2xx non-JSON body) and check for regressions.
The 8 ACs, the `_HTTP_REQUEST_TIMEOUT_SECONDS` constant, and the empty-inputs-before-
credential-check ordering were NOT re-litigated — they already stood.

**Test summary**
- Format / lint / pre-commit: PASS (`make memory-format-check`, `make memory-lint-check`,
  `make pre-commit` — ruff format/check, prettier, biome all Passed; 291 files already
  formatted)
- Unit tests: 1755 passed / 0 failed (`make memory-unit-tests`, 40.32s) — up from 1750,
  the +5 matches the claimed new regression tests
- Integration tests: 167 passed / 2 failed / 1 skipped / 105 deselected
  (`make memory-integration-tests`, 172.86s) — same 2 pre-existing failures as before
  (`test_indexing_pipeline.py::test_embeds_nodes`,
  `test_meta_state.py::TestRecordDreamRun::test_updated_at_is_recent`), unchanged and
  still not attributable to this task
- Warnings: 0

**Fix verification — did NOT take the SWE's word for it**
- Re-ran my exact original break path 6 independently, two ways:
  1. Mocked `httpx.Response(status_code=200, text=...)` through the module's real
     `_parse_json` — a 12,000-char captcha-style HTML body now raises
     `BrightDataRequestError` (672-char message, truncated with a `… (N chars,
     truncated)` suffix), where it previously leaked `json.decoder.JSONDecodeError`.
  2. **Real** `httpx.AsyncClient` against a **real** local `ThreadingHTTPServer`
     returning HTTP 200 + a 12 KB HTML captcha body for the trigger call (no mocking of
     httpx internals at all) → same result: `BrightDataRequestError`, not a leaked
     decode error. Confirms the fix holds end-to-end, not just against mocks.
- Also independently drove the snapshot-GET path (not just trigger-POST) through
  `_parse_json` with an empty 200 body → correctly raises `BrightDataRequestError`
  naming the snapshot URL — confirms the fix is at the shared seam, not path-specific,
  matching the SWE's new `test_non_json_snapshot_body_raises_request_error`.
- **`from exc` chaining confirmed present and functional**: `e.__cause__` on the caught
  `BrightDataRequestError` is the original `json.decoder.JSONDecodeError` — the
  original traceback is not discarded.

**`except ValueError` breadth — scrutinized, judged correctly scoped**
- Confirmed both `json.JSONDecodeError` and `UnicodeDecodeError` are `ValueError`
  subclasses (`issubclass(UnicodeDecodeError, ValueError)` → `True`,
  `issubclass(json.JSONDecodeError, ValueError)` → `True`), so the SWE's stated
  reasoning for the exception class choice is factually correct.
- Checked the realistic "genuine bug gets mislabelled" risk the orchestrator asked
  about: `httpx.ResponseNotRead` (raised if `.json()` is called on an unread streamed
  response — the kind of thing a future refactor bug could trigger) has MRO
  `(ResponseNotRead, StreamError, RuntimeError, Exception, BaseException)` — a
  `RuntimeError`, NOT a `ValueError` — confirmed via
  `issubclass(httpx.ResponseNotRead, ValueError)` → `False`. It would surface as itself,
  uncaught by this handler, exactly as the SWE claimed.
- The `try` block is scoped to the single expression `response.json()` only (lines
  277-278) — it does not wrap the status-code check, the URL formatting, or anything
  upstream in `_post_json`/`_get_json`/`_trigger`/`_wait_until_ready`, so a `ValueError`
  raised by unrelated code elsewhere in the call chain (e.g. a bad `params` dict passed
  to `httpx.Client.get`) would not be caught here — that failure happens before
  `_parse_json` is ever invoked. Verdict: correctly scoped, no realistic mislabelling
  case found.

**Shared `_body_excerpt()` — checked for regressions on previously-passing assertions**
- Re-ran `test_trigger_non_2xx_raises_request_error_with_status_and_body` (7 status
  codes, `match="upstream boom"`) — still PASS; body is 12 chars, far under
  `_MAX_ERROR_BODY_CHARS = 500`, so truncation never engages and the full body still
  appears verbatim in the message. No previously-passing non-2xx assertion was weakened.
- Read `_body_excerpt()` (`web_scraper_api.py:290-297`): pure function, `<=500` chars
  returns the body unchanged, `>500` chars truncates with a `… (N chars, truncated)`
  suffix naming the true length — a reasonable, symmetric choice to fix the
  unreadable-log problem once at the shared seam (a 502 from a proxy is just as likely
  to be an HTML page as a 200 is) rather than only on the path the failing test named.

**New tests read and independently sanity-checked**
- `test_2xx_with_non_json_body_raises_request_error` (parametrized: captcha-HTML,
  empty string, whitespace-only) — all three are genuine `json.JSONDecodeError`
  triggers (`json.loads("")` and `json.loads("   ")` both raise
  `Expecting value: line 1 column 1`), so the parametrization is not padding.
- `test_non_json_body_is_truncated_in_the_error_message` — asserts message length
  `< 1000` against a 10,004-char body; consistent with `_MAX_ERROR_BODY_CHARS = 500`.
- `test_non_json_snapshot_body_raises_request_error` — patches `_post_json` and
  `_wait_until_ready` directly so the non-JSON body lands on the snapshot GET, not the
  trigger POST — correctly proves the guard is shared, not trigger-only.
- Total new-file count: 23 (18 + 5), matches the claim.

**Scope check (re-confirmed)**
- `git status --porcelain` unchanged from the prior review: only
  `web_scraper_api.py` and `test_web_scraper_api.py` are new/modified by the SWE, plus
  this task file. `docs/glossary.md`, `docs/adrs/004_*.md`, `tasks/091-093-*.md`, and
  `tests/unit/data/youtube/fixtures/` remain untouched pre-existing grooming artifacts.
- `make env-status` → local (.env), as required.

**Evidence**
```
$ make memory-unit-tests
============================ 1755 passed in 40.32s =============================

$ make memory-integration-tests
FAILED tests/integration/memory/test_indexing_pipeline.py::TestMemoryIndexingPipeline::test_embeds_nodes
FAILED tests/integration/memory/test_meta_state.py::TestRecordDreamRun::test_updated_at_is_recent
2 failed, 167 passed, 1 skipped, 105 deselected in 172.86s   (both pre-existing, unrelated, unchanged)

$ uv run python - <<'EOF'   # independent repro: real httpx + real local HTTP server, 200 + 12KB captcha body
...
Correctly wrapped: BrightDataRequestError
message length: 672
has __cause__ (from exc): Expecting value: line 1 column 1 (char 0) JSONDecodeError
EOF
```

**Other issues found**
- None. No new issues introduced by the fix; no regressions in previously-passing
  assertions.

**VERDICT: PASS**

All 8 ACs stand from the prior review (not re-litigated per orchestrator instruction).
The single blocker (raw `JSONDecodeError` leak on a 2xx non-JSON body) is fixed,
independently reproduced as fixed via both mocked and real-httpx-against-a-real-local-
server repros, covers both the trigger and snapshot call sites, preserves the original
exception via `from exc`, is narrowly scoped (`except ValueError` around a single
expression, verified not to swallow unrelated bugs like `httpx.ResponseNotRead`), and
introduces no regression in previously-passing non-2xx body assertions. Full suite
green (1755 unit / 0 failed, 167 integration / 2 pre-existing-unrelated failures),
format/lint/pre-commit clean, 0 warnings, scope unchanged.

Hand off to PA for acceptance review.
