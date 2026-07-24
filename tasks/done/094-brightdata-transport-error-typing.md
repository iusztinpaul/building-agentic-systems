---
id: 094-brightdata-transport-error-typing
feature: brightdata-youtube-transcripts-followups
status: done
---

# Type httpx transport errors so a Bright Data outage falls back to Gemini

Tags: `data`
Depends on: None (builds on #090, #092 — merged in PR #34)
Implements: ADR-004 (§3 trigger list — extends it)

## Scope

Surfaced by the PR-Reviewer on PR #34 as Nit 2, and it is the same class of bug the
Tester caught on #090: an untyped exception escaping `collect()` bypasses the Transcript
fallback chain.

`apps/memory/src/tree/data/web/web_scraper_api.py` lets httpx transport errors —
`httpx.TimeoutException`, `httpx.ConnectError`, and their siblings — propagate untyped
out of `collect()`. ADR-004 Decision 3 has the fallback chain in
`youtube_ingest.fetch_transcripts_batch` catch exactly three types
(`BrightDataConfigurationError`, `BrightDataRequestError`, `BrightDataTimeoutError`) to
trigger the batch-wide Gemini fallback. A transport error is none of them, so a Bright
Data network outage HARD-FAILS the Prefect task and burns its retries instead of falling
back to Gemini — even though Gemini is available and would have succeeded.

Note the asymmetry this fixes: a Bright Data *poll timeout* already falls back
(`BrightDataTimeoutError`), but a *connect timeout* does not. Both are "Bright Data is
unreachable right now", and the ADR's intent is that any such condition falls back.

Wrap transport failures into `BrightDataRequestError` at the two HTTP seams
(`_post_json` / `_get_json`), preserving the original exception via `from exc` and
naming the request URL — the same shape the existing non-2xx and non-JSON-body guards
already produce.

Decide and record in the task log whether `web_unlocker.fetch_url` should get the same
treatment. Its docstring currently documents `httpx.TimeoutException: Propagated as-is`,
and `web_scrape.scrape_one` already catches `httpx.ConnectError` / `httpx.TimeoutException`
explicitly — so that caller is unaffected either way. Changing it is optional; changing
it silently is not.

Unit tests only, fully mocked — NO live Bright Data call, ever (ADR-004 Decision 8; this
is code for a book).

## Acceptance criteria

- [x] `httpx.TimeoutException` and `httpx.ConnectError` raised from either HTTP seam
      surface as `BrightDataRequestError`, with the request URL in the message and the
      original exception preserved as `__cause__`.
- [x] The catch is scoped to transport failures — a genuine programming bug must still
      surface as itself, not be mislabelled as a Bright Data request error. State in the
      log which exception base you caught and why.
- [x] `fetch_transcripts_batch` routes the whole batch to Gemini on a Bright Data
      transport failure, with the standard cost WARNING naming the reason — asserted end
      to end with the transport error injected at the seam.
- [x] Existing behaviour unchanged: non-2xx → `BrightDataRequestError`, `failed` status →
      `BrightDataRequestError`, poll timeout → `BrightDataTimeoutError`, 2xx-with-non-JSON
      → `BrightDataRequestError`.
- [x] A decision on `web_unlocker.fetch_url` is recorded in the log, and if it changed,
      its docstring's "Propagated as-is" line changed with it.
- [x] NO live integration test added; `make memory-format-check && make memory-lint-check
      && make pre-commit` clean; `make memory-unit-tests` green, 0 warnings.

## Out of scope

- Retry/backoff policy inside `collect` (Prefect owns retries at the task level).
- Any change to the fallback chain's structure or to ADR-004's other decisions.

## Log

### [SWE] 2026-07-24 18:01 — Implementation

**Files modified**

- `apps/memory/src/tree/data/web/web_scraper_api.py` — wrap transport failures at both
  HTTP seams (`_post_json` / `_get_json`) into `BrightDataRequestError` via a new
  `_transport_error(url, exc)` helper; `collect`'s `Raises:` contract updated (the
  `httpx.TimeoutException: Propagated as-is` line is gone — it is no longer true).
- `apps/memory/tests/unit/data/web/test_web_scraper_api.py` — `TestTransportErrorTyping`:
  8 transport types × both seams → `BrightDataRequestError` (URL in message, original as
  `__cause__`), plus 3 non-transport errors that must still surface as themselves.
- `apps/memory/tests/unit/data/youtube/test_youtube_ingest.py` —
  `TestBrightDataTransportFailure`: the batch-wide Gemini fallback asserted end to end
  with the transport error injected at the HTTP seam (real `BrightDataTranscriptFetcher`
  + real `collect`, mocked `httpx.AsyncClient`).

**Which exception base, and why**

Caught `httpx.TransportError`, NOT `httpx.HTTPError` and not `httpx.RequestError`.
The hierarchy (httpx 0.28.1, verified in the installed package):

```
HTTPError
├── RequestError
│   ├── TransportError ← caught
│   │   ├── TimeoutException (ConnectTimeout, ReadTimeout, WriteTimeout, PoolTimeout)
│   │   ├── NetworkError (ConnectError, ReadError, WriteError, CloseError)
│   │   ├── ProtocolError (Local/RemoteProtocolError)
│   │   ├── ProxyError
│   │   └── UnsupportedProtocol
│   ├── DecodingError
│   └── TooManyRedirects
└── HTTPStatusError          ← deliberately NOT caught
```

`TransportError` is exactly the "the request never completed" branch — the machine
meaning of "Bright Data is unreachable right now", which is the same operational
condition as the poll timeout ADR-004 §3 already routes to Gemini. `HTTPError` is too
wide: it also covers `HTTPStatusError` (a genuine HTTP response, which `_parse_json`
already types on status code) and `DecodingError` / `TooManyRedirects`, which are
response-shape problems, not reachability. Everything outside `TransportError` — a
`TypeError` from a bad kwarg, `httpx.InvalidURL` from a malformed constant — stays
unwrapped and surfaces as itself; that is asserted, not just asserted-in-prose, by
`test_non_transport_failure_surfaces_as_itself`.

**Decision: `web_unlocker.fetch_url` is left UNCHANGED** (docstring's
`httpx.TimeoutException: Propagated as-is` line stays, because it stays true).

- Every caller already discriminates transport failure as its own outcome:
  `web_scrape.scrape_one` and both MCP tools (`ingest_url`, `web_search`) catch
  `(httpx.ConnectError, httpx.TimeoutException)` and return `error_type:
  "network_error"`, distinct from the `"fetch_failed"` they return for
  `BrightDataRequestError`. Wrapping inside `fetch_url` would collapse `network_error`
  into `fetch_failed` and delete an operator-visible signal.
- There is no fallback chain on the Web Unlocker path — nothing keys on
  `BrightDataRequestError` to route work elsewhere — so there is no bug to fix there;
  the change would be churn, and the task lists fallback-structure changes as out of
  scope.
- Consequence to accept knowingly: the two Bright Data clients now differ in error
  vocabulary. That is intentional and follows the callers: `web_scraper_api` feeds a
  three-type fallback chain that must not see raw httpx; `web_unlocker` feeds
  envelope-returning callers that want the transport case named separately.

**Tests**

- Unit: 1845 passing, 0 failing, 0 warnings (was 1824 before this task; +21 new —
  `pytest --collect-only` on the two new classes reports 19 in
  `TestTransportErrorTyping` + 2 in `TestBrightDataTransportFailure`).
- Integration: N/A — no infra touched (pure error-typing change in a mocked HTTP client).
- Red/green confirmed: 18 of the 21 failed with the RAW `httpx` error escaping `collect`
  (`E httpx.TimeoutException: bright data is unreachable`) before the fix — i.e. failed
  for exactly the reason the bug describes, not on an import or fixture. The other 3
  (`test_non_transport_failure_surfaces_as_itself`) were green before AND after, which
  is the point: they fail only if the catch is later widened.

**Acceptance criteria**

- [x] Transport errors from either seam → `BrightDataRequestError`, URL in message,
      original as `__cause__` — `test_web_scraper_api.py::TestTransportErrorTyping::
      test_trigger_transport_failure_raises_request_error` and
      `::test_poll_transport_failure_raises_request_error` (8 httpx types each).
- [x] Catch scoped to transport failures — `::test_non_transport_failure_surfaces_as_itself`
      (`TypeError`, `AttributeError`, `httpx.InvalidURL`).
- [x] Whole batch → Gemini with the cost WARNING — `test_youtube_ingest.py::
      TestBrightDataTransportFailure::test_transport_failure_sends_the_whole_batch_to_gemini`.
- [x] Existing behaviour unchanged — the pre-existing `TestCollectFailures` /
      `TestHttpErrorPropagation` classes (non-2xx, `failed` status, poll timeout,
      2xx-with-non-JSON) are untouched and green.
- [x] `web_unlocker.fetch_url` decision recorded above; unchanged, so its docstring is
      unchanged.
- [x] No live call anywhere: no integration test added, every new test mocks
      `httpx.AsyncClient`, and the E2E below hits 127.0.0.1 only.

**Evidence**

```
$ make memory-unit-tests
============================ 1845 passed in 42.70s =============================

$ make memory-format-fix && make memory-lint-fix && make memory-format-check && make memory-lint-check
294 files left unchanged
All checks passed!
294 files already formatted
All checks passed!

$ make pre-commit
Validate pyproject.toml..............................(no files to check)Skipped
prettier.................................................................Passed
ruff check...............................................................Passed
ruff format..............................................................Passed
biome check (harness)....................................................Passed

$ make env-status
Env target: local (.env)
```

End-to-end (scratch script, NOT committed): the three `_BRIGHTDATA_*_URL` constants
repointed at the discard port `127.0.0.1:9` so REAL httpx over a REAL socket produces a
REAL `ConnectError`; Gemini replaced by a stub class. Zero Bright Data / Gemini traffic.

```
18:00:54.588 | WARNING | tree.data.youtube.youtube_ingest - Bright Data collection unavailable for 1 videos (reason=brightdata_request_error): Bright Data Web Scraper API request to http://127.0.0.1:9/datasets/v3/trigger failed at the transport layer: ConnectError: All connection attempts failed
18:00:54.589 | WARNING | tree.data.youtube.youtube_ingest - Falling back to Gemini for 1/1 videos (reason=brightdata_request_error) — consumes Gemini tokens and incurs API cost

=== 1. collect() against an unreachable host ===
raised   : BrightDataRequestError
message  : Bright Data Web Scraper API request to http://127.0.0.1:9/datasets/v3/trigger failed at the transport layer: ConnectError: All connection attempts failed
__cause__: ConnectError: All connection attempts failed

=== 2. fetch_transcripts_batch over the same outage ===
transcribed: [('https://www.youtube.com/watch?v=eYaWxljC4sA', 'fallback text')]
failed     : []
```

**Notes**

- The asymmetry named in the spec is gone: a CONNECT timeout now falls back exactly like
  the POLL timeout already did. The two arrive with different reasons
  (`brightdata_request_error` vs `brightdata_timeout`) and therefore different
  `no_transcript:` strings — deliberate, since the operator distinction "unreachable"
  vs "too slow" is worth keeping and ADR-004 §3's trigger list is extended, not
  rewritten.
- `httpx.UnsupportedProtocol` is a `TransportError` and so gets wrapped, although it is
  arguably a bad-constant bug. Left as-is: it is unreachable with the module's hardcoded
  `https://` constants, and narrowing the catch to a hand-listed tuple would silently
  miss future httpx transport types — the failure mode this task exists to fix.
- Wrapped the whole `async with` block, not just the request line, so a transport error
  raised while closing the connection pool is typed too.
- No new dependency, no config knob, no ADR-level decision required: the fix is the same
  shape as the two guards already in `_parse_json`, one layer out.

### [Tester] 2026-07-24 18:45 — QA

**Test summary**
- Format / lint / pre-commit: PASS (`make memory-format-check`, `make memory-lint-check`,
  `make pre-commit` all clean)
- Unit tests: 1845 passed / 0 failed (`make memory-unit-tests`, 45.33s)
- Integration tests: 169 passed / 2 failed / 1 skipped (`make memory-integration-tests`,
  run solo/sequentially, 203.01s) — the 2 failures are exactly the pre-existing
  flaky/unrelated tests named in the task brief
  (`test_indexing_pipeline.py::test_embeds_nodes`,
  `test_meta_state.py::TestRecordDreamRun::test_updated_at_is_recent`); no other
  integration test regressed. (Note: an earlier attempt run concurrently with another
  integration suite in this session produced 2 extra index-related failures purely from
  the shared-DB collision the Makefile warns about — discarded as environment noise, not
  re-run in parallel again.)
- Warnings: 0

**E2E adversarial pass**

Independently reproduced (own script, not the SWE's), no live Bright Data/Gemini traffic:
real local `aiohttp` stub server for trigger+progress, discard port `127.0.0.1:9` for
forced real `ConnectError`s, mocked `httpx.AsyncClient` for the parametrized cases.

- Happy path: unit-mocked trigger-seam transport failure → `BrightDataRequestError`
  with URL + `__cause__` (`TestTransportErrorTyping::test_trigger_transport_failure_raises_request_error[ConnectError]`)
  → PASS.
- Break path 1 (state edge — mid-collection failure on the THIRD seam, i.e. snapshot
  download, after a REAL trigger+poll succeeded against a real local server): real
  `ConnectError` on `GET .../datasets/v3/snapshot/sd_e2e_1` →
  `BrightDataRequestError("...failed at the transport layer: ConnectError...")`,
  `__cause__` is the real `ConnectError`. Matches expected (PASS). This closes a gap the
  SWE's own scratch script didn't specifically exercise (theirs failed at trigger only).
- Break path 2 (failure mode — transport error with `GOOGLE_API_KEY` NOT configured):
  `fetch_transcripts_batch.fn(items)` with a dead trigger URL and no Gemini key →
  no crash, `transcribed=[]`, `failed=[(url, metadata, "no_transcript: brightdata
  unavailable (trigger rejected); gemini not configured")]`, WARNING logged
  `reason=brightdata_request_error` then a second WARNING `GOOGLE_API_KEY is not
  configured — recording ingest_error rows`. Expected: graceful degrade to an
  `ingest_error` row, task does not hard-fail. Observed matches. PASS.
- Break path 3 (equivalence check — `httpx.ReadTimeout` vs `httpx.ConnectTimeout`):
  both raised at the trigger seam → both wrap into `BrightDataRequestError` with
  `__cause__` set to the respective original type. Identical treatment, as expected
  since both are `TransportError` subclasses. PASS.
- Break path 4 (negative control — non-transport `TypeError` injected at the same
  seam): escapes `collect()` untyped as `TypeError`, NOT wrapped into
  `BrightDataRequestError`. Expected per AC2 ("a genuine programming bug must still
  surface as itself"). Observed matches. PASS.

No crash, no silent corruption, no leaked raw stack trace to a caller that doesn't
already expect one, no hang, and every failure path logs before degrading — all break
paths green.

**Exception hierarchy verification (independent, not just trusting the SWE's prose)**

Ran directly against the installed `httpx==0.28.1` in the project's own venv:
`httpx.TransportError.__mro__` → `(TransportError, RequestError, HTTPError, Exception,
BaseException, object)`. Confirmed `DecodingError`, `TooManyRedirects` are
`RequestError`/`HTTPError` subclasses but NOT `TransportError`; confirmed
`HTTPStatusError` is `HTTPError` but neither `RequestError` nor `TransportError`;
confirmed `InvalidURL` is none of the three. All 8 parametrized transport types
(`TimeoutException`, `ConnectTimeout`, `ReadTimeout`, `PoolTimeout`, `ConnectError`,
`ReadError`, `RemoteProtocolError`, `ProxyError`) confirmed `TransportError`
subclasses. The SWE's stated hierarchy and choice of catch base are correct — nothing
material is over-caught (a genuine HTTP response or a decode/redirect problem still
surfaces as itself, per `_parse_json`'s existing typing) or under-caught (every
reachability failure a fallback should trigger on is covered).

On `UnsupportedProtocol`: agree with the SWE's call. It IS wrapped (it's a
`TransportError`), which is theoretically over-inclusive since the module's URL
constants are hardcoded `https://` literals and can't produce it today. Narrowing to a
hand-listed tuple to exclude it would reintroduce exactly the failure mode this task
fixes (a future httpx addition silently falling through unwrapped) for a case that is
currently unreachable. Judged not worth the trade.

On `web_unlocker.fetch_url` being left unchanged: independently verified the callers'
claim by reading the source, not just the log's prose —
`apps/memory/src/tree/data/web/web_scrape.py:122` and
`apps/memory/src/tree/mcp/tools.py:301,425` all catch
`(httpx.ConnectError, httpx.TimeoutException)` and return `error_type: "network_error"`,
genuinely distinct from the `error_type: "fetch_failed"` branch keyed on
`BrightDataRequestError` a few lines above each. Wrapping would collapse two
already-distinct, already-consumed envelopes into one. Decision is sound and the
docstring is correctly left untouched (`git diff` confirms `web_unlocker.py` has zero
changes).

**Acceptance criteria**
- [x] PASS — `httpx.TimeoutException`/`httpx.ConnectError` from either seam surface as
      `BrightDataRequestError` (URL in message, original as `__cause__`) — 16 parametrized
      unit tests (`TestTransportErrorTyping::test_trigger_transport_failure_raises_request_error`
      / `::test_poll_transport_failure_raises_request_error`, 8 httpx types × 2 seams), plus
      independently reproduced live over a real socket (break path 1 above, snapshot seam).
- [x] PASS — catch scoped to transport failures, non-transport surfaces as itself — verified
      the httpx hierarchy directly in the installed package; `TestTransportErrorTyping::
      test_non_transport_failure_surfaces_as_itself` (`TypeError`, `AttributeError`,
      `httpx.InvalidURL`) plus independently reproduced (break path 4).
- [x] PASS — `fetch_transcripts_batch` routes the whole batch to Gemini on transport
      failure with the cost WARNING —
      `test_youtube_ingest.py::TestBrightDataTransportFailure::
      test_transport_failure_sends_the_whole_batch_to_gemini` (2 parametrized types),
      plus independently reproduced against a real socket failure at the trigger seam and
      the Gemini-not-configured degrade path (break path 2 above).
- [x] PASS — existing behaviour unchanged (non-2xx, `failed` status, poll timeout,
      2xx-non-JSON) — `TestCollectFailures` / `TestHttpErrorPropagation` untouched
      (`git diff` shows zero changes to those classes) and green as part of the
      1845-passed full unit run.
- [x] PASS — `web_unlocker.fetch_url` decision recorded and consistent with the code —
      `git diff --stat` confirms `web_unlocker.py` untouched; independently verified the
      "distinct envelopes" claim by reading `web_scrape.py:122` and `mcp/tools.py:301,425`.
- [x] PASS — no live integration test added, format/lint/pre-commit clean, unit tests
      green with 0 warnings — evidence below.

**Evidence**
```
$ make memory-format-check
uv run ruff format --check src/ tests/ scripts/ deploy/
294 files already formatted

$ make memory-lint-check
uv run ruff check src/ tests/ scripts/ deploy/
All checks passed!

$ make pre-commit
Validate pyproject.toml..............................(no files to check)Skipped
prettier.................................................................Passed
ruff check...............................................................Passed
ruff format..............................................................Passed
biome check (harness)....................................................Passed

$ make memory-unit-tests
============================ 1845 passed in 45.33s =============================

$ make memory-integration-tests   (run solo, no concurrent test process)
===== 2 failed, 169 passed, 1 skipped, 105 deselected in 203.01s (0:03:23) =====
FAILED tests/integration/memory/test_indexing_pipeline.py::TestMemoryIndexingPipeline::test_embeds_nodes
FAILED tests/integration/memory/test_meta_state.py::TestRecordDreamRun::test_updated_at_is_recent
```

**Other issues found**
- Minor reporting inaccuracy, not a functional defect: the SWE's log says "+18 new
  tests" for this task; actual collected count is 21 (19 in
  `TestTransportErrorTyping` = 8 types × 2 seams + 3 non-transport, plus 2 in
  `TestBrightDataTransportFailure`). Verified via
  `pytest --collect-only -k TestTransportErrorTyping` (19) and the file diff (2 more).
  Does not affect correctness or coverage — coverage is actually slightly better than
  claimed — but flagging so the log stays accurate for future readers.
- No `code-review` plugin invocation was available from this agent's toolset (Bash/Read/
  Edit/Write only, no slash-command access) even though it's enabled in
  `.claude/settings.json`; compensated with a deeper-than-usual manual review (independent
  httpx hierarchy check in the installed package, independent re-implementation of the
  e2e adversarial script rather than re-running the SWE's, and grep-verification of the
  `web_unlocker` caller claim against actual source instead of trusting the log).

**VERDICT: PASS**
