---
id: 094-brightdata-transport-error-typing
feature: brightdata-youtube-transcripts-followups
status: pending
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

- [ ] `httpx.TimeoutException` and `httpx.ConnectError` raised from either HTTP seam
      surface as `BrightDataRequestError`, with the request URL in the message and the
      original exception preserved as `__cause__`.
- [ ] The catch is scoped to transport failures — a genuine programming bug must still
      surface as itself, not be mislabelled as a Bright Data request error. State in the
      log which exception base you caught and why.
- [ ] `fetch_transcripts_batch` routes the whole batch to Gemini on a Bright Data
      transport failure, with the standard cost WARNING naming the reason — asserted end
      to end with the transport error injected at the seam.
- [ ] Existing behaviour unchanged: non-2xx → `BrightDataRequestError`, `failed` status →
      `BrightDataRequestError`, poll timeout → `BrightDataTimeoutError`, 2xx-with-non-JSON
      → `BrightDataRequestError`.
- [ ] A decision on `web_unlocker.fetch_url` is recorded in the log, and if it changed,
      its docstring's "Propagated as-is" line changed with it.
- [ ] NO live integration test added; `make memory-format-check && make memory-lint-check
      && make pre-commit` clean; `make memory-unit-tests` green, 0 warnings.

## Out of scope

- Retry/backoff policy inside `collect` (Prefect owns retries at the task level).
- Any change to the fallback chain's structure or to ADR-004's other decisions.

## Log
