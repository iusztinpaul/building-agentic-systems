---
id: 092-youtube-fallback-orchestration
feature: brightdata-youtube-transcripts
status: done
---

# Bright Data primary / Gemini fallback chain + persisted ingest failures in the YouTube ETL

Tags: `data`
Depends on: #089, #091
Blocks: #093
Implements: ADR-004

## Scope

Rewire the SHARED bulk core (`youtube_ingest.py`) so Bright Data is the primary
Transcript fetcher and Gemini the fallback — the SAME code path on both the single-video
MCP path (`youtube_pipeline.ingest_youtube_video`) and the RSS/offline batch path
(`youtube_pipeline_batch`), with NO per-path branching. Persist exhausted failures as
Ingest error rows.

**1. Fallback chain in `fetch_transcripts_batch`** (fetchers still constructed INSIDE
the task body — the unpicklable-client-never-a-task-input property is preserved):

- Up-front credential gate, BEFORE any billable call: read
  `settings.brightdata_api_key` and `settings.google_api_key`. When NEITHER is
  configured → `RuntimeError` naming both (`"Neither BRIGHTDATA_API_KEY nor
  GOOGLE_API_KEY is configured; see .env.example"`). When only one is configured, run
  with it — never construct the unavailable fetcher (`GeminiTranscriptFetcher.__init__`
  raises on a missing key, so it must not be instantiated when `GOOGLE_API_KEY` is
  absent; same for the Bright Data fetcher).
- Primary: ONE `BrightDataTranscriptFetcher().fetch_many(urls)` over ALL URLs in the
  batch (one collection per batch).
- Per-video fallback: only the slots that came back transcript-less (`None`) go to
  Gemini, in a SECOND bulk `fetch_many` over just those URLs.
- Batch-WIDE triggers — missing Bright Data credentials, trigger rejected
  (`BrightDataRequestError`), poll timeout (`BrightDataTimeoutError`) — send the WHOLE
  batch to Gemini (poll timeout is just another fallback trigger; never fails the task
  while Gemini is available).
- EVERY fallback logs a WARNING explicitly stating it consumes Gemini tokens and incurs
  API cost, plus the reason and slot count — e.g. "Falling back to Gemini for 3/12
  videos (reason=no_brightdata_transcript) — consumes Gemini tokens and incurs API
  cost". Bright-Data-only setups log a WARNING per batch for slots Gemini can't rescue.

**2. Metadata merge (uniform, both branches).** Add a pure helper
`merge_video_metadata(base: VideoMetadata, override: VideoMetadata) -> VideoMetadata`
(suggest `tree/data/youtube/types.py`, beside the model): every non-None `override`
field wins; base survives where override is None. Apply it in the build step to
`override=transcript.metadata` over the CALLER's base metadata (oEmbed via
`_resolve_video_item` for single video; Atom feed entry for RSS — both resolved exactly
as today, oEmbed KEPT unchanged). Bright Data's record metadata therefore wins on
conflict; the Gemini branch's `transcript.metadata` carries only `video_id`, so base
metadata survives intact there — one merge, no branch-specific logic. **Behavioural
improvement to call out and assert:** `build_document`'s
`date = metadata.publish_date or now(UTC)` fallback now receives Bright Data's real
`date_posted` — Bright-Data-sourced Documents get their true publish date instead of
ingest time.

**3. Persisted failures** (Ingest error rows, loaded through the normal `load_batch` →
`load_video_document` path so #089's replace-on-retry semantics apply):

- Both backends exhausted for a slot → a `Document` keyed on the canonical `watch?v=`
  URL, carrying whatever base metadata we have (title etc.), `content=None`,
  `source_type=YOUTUBE`, `ingest_error="no_transcript: brightdata + gemini both
  returned empty"` — or, when a backend never ran, a normalized variant naming the
  actual chain (e.g. `"no_transcript: brightdata returned empty; gemini not
  configured"`). Suggest a `build_failure_document(...)` helper in `youtube_video.py`.
- Unresolvable input (`extract_video_id` returns None — `youtube_pipeline.py:47`) → a
  row keyed on the RAW input string as `source_uri`,
  `ingest_error="invalid_url: no video id in input"`. Both paths route through
  `_resolve_video_item`, so hook its call sites (the MCP single path and the batch
  path's loose-video resolve). Feed-derived entries with no resolvable id stay
  WARNING-only (no stable key exists).
- Load/DB failures stay WARNING-only as today (writing a DB-failure row to the failing
  DB is circular).
- Error strings are NORMALIZED and cleaned (short stable prefix code + message) — never
  raw exception dumps.

`_bulk_build_and_load` / `fetch_transcripts_batch` may change internal shape (e.g.
returning failed slots alongside transcribed ones) — SWE's choice; the flows' public
signatures and Prefect task names stay stable. Never let one bad video sink a batch.

Unit tests only (call the `/squid-testing-python` skill), fully mocked at each fetcher's
thin seam — NO live calls: chain order (BD first, Gemini only over missing slots, exact
URL subset asserted); batch-wide trigger variants (missing BD key / request error /
timeout → whole batch to Gemini); neither-key RuntimeError raised before any fetcher
construction; BD-only setup (Gemini never constructed, missing slots → no_transcript
rows, batch survives); cost-WARNING text asserted via caplog; merge (BD non-None wins,
None preserved from base; Gemini branch leaves base intact); real-publish-date
assertion; failure-row shapes for both error codes; retry-over-errored-row WARNING path
end-to-end through `load_batch`.

## Acceptance criteria

- [x] Same fallback chain code path on both pipelines: `git grep GeminiTranscriptFetcher
      apps/memory/src/tree` shows pipeline-layer construction ONLY inside the shared
      task body's fallback branch; no per-path branching in `youtube_pipeline.py` /
      `youtube_pipeline_batch.py`.
- [x] ONE Bright Data collection over all URLs in the batch; Gemini receives EXACTLY
      the transcript-less subset in ONE second bulk `fetch_many` (unit-asserted).
- [x] Batch-wide triggers (missing BD credentials, trigger rejected, poll timeout) send
      the whole batch to Gemini; the Prefect task does not fail when a fallback exists.
- [x] Every fallback WARNING names the reason + slot count AND explicitly states Gemini
      token consumption / cost (caplog-asserted).
- [x] Neither backend configured → up-front `RuntimeError` naming both
      `BRIGHTDATA_API_KEY` and `GOOGLE_API_KEY` and pointing at `.env.example`, raised
      before any billable call. Bright-Data-only setup does NOT raise: missed videos →
      WARNING + Ingest error row, batch completes.
- [x] Metadata merge: Bright Data record metadata wins on every non-None field; base
      (oEmbed / feed) survives where Bright Data is null; Gemini branch leaves base
      metadata fully intact; oEmbed resolve unchanged. A Bright-Data-sourced Document's
      `date` equals the record's `date_posted` (tz-aware UTC) — no `now()` fallback.
- [x] Exhausted slot → failure Document keyed on canonical `watch?v=` URL with base
      metadata, `content=None`, normalized `no_transcript: …` error; unresolvable input
      → row keyed on the RAW input with `ingest_error="invalid_url: no video id in
      input"` on BOTH the MCP single path and the batch loose-video path; load/DB
      failures remain WARNING-only; no raw exception dumps in `ingest_error`.
- [x] Failure rows flow through `load_video_document`, so a later successful run
      replaces them (#089 semantics; unit-asserted end-to-end through `load_batch`).
- [x] Fetchers are constructed inside the task body (no fetcher appears as a Prefect
      task/flow parameter); `gemini_transcript_fetcher.py` unchanged.
- [x] NO live integration test added; all new tests mock the fetcher seams.
- [x] `make memory-format-check && make memory-lint-check && make pre-commit` clean;
      `make memory-unit-tests` green, 0 warnings; `make memory-integration-tests` green
      (infra-adjacent change).

## Out of scope

- Live verification of either backend (that is #093). Any change to
  `web_scraper_api.py` / `brightdata_transcript_fetcher.py` mapping internals beyond
  what wiring strictly requires. Failure rows for non-YouTube sources. Attempt caps.

## Log

### [SWE] 2026-07-24 15:15 — Implementation

**Files modified**

- `apps/memory/src/tree/data/youtube/youtube_ingest.py` — the fallback chain:
  up-front credential gate, Bright Data primary over all URLs, Gemini second bulk
  `fetch_many` over ONLY the missing slots, batch-wide triggers, costed WARNINGs,
  normalized `no_transcript: …` strings; `build_batch` now merges metadata and
  builds failure rows; `_bulk_build_and_load` takes `invalid_inputs` and returns
  only genuinely ingested Documents.
- `apps/memory/src/tree/data/youtube/types.py` — pure `merge_video_metadata(base,
  override)`.
- `apps/memory/src/tree/data/youtube/youtube_video.py` — `INVALID_URL_ERROR`,
  `build_failure_document(...)`, and a non-misleading load log for failure rows
  ("Recorded ingest failure", not "Ingested").
- `apps/memory/src/tree/data/youtube/youtube_pipeline.py` — unresolvable MCP input
  now persists an `invalid_url` row through the core; `_partition_video_inputs`
  (shared with the batch path).
- `apps/memory/src/tree/data/youtube/youtube_pipeline_batch.py` — loose videos are
  partitioned before oEmbed; unresolvable raw inputs flow to the core.
- `apps/memory/tests/unit/data/youtube/test_youtube_ingest.py` — rewritten: chain
  order/grain, batch-wide triggers, credential gate, cost WARNINGs, error strings,
  merge, real publish date, failure-row shapes, errored-row replace via `load_batch`.
- `apps/memory/tests/unit/data/youtube/test_types.py` — new: `merge_video_metadata`.
- `apps/memory/tests/unit/data/youtube/test_youtube_video.py` —
  `build_failure_document` shapes + failure-row load log.
- `apps/memory/tests/unit/data/youtube/test_youtube_pipeline{,_batch}.py` —
  invalid-input routing; the real-core test now drives the Bright Data primary.
- `apps/memory/tests/integration/data/youtube/test_youtube_pipeline.py` — patches
  BOTH backends (was Gemini only — with Bright Data primary that would have hit the
  live API); adds persisted-failure-row, invalid-url-row and replace-on-retry cases.

**Tests**

- Unit: 1824 passing, 0 failing, 0 warnings — `make memory-unit-tests`.
- Integration: 169 passing, 1 skipped — `make memory-integration-tests`. The 2
  failures are PRE-EXISTING and unrelated (`test_indexing_pipeline.py::test_embeds_nodes`,
  `test_meta_state.py::TestRecordDreamRun::test_updated_at_is_recent`), confirmed
  by the human before this task started.
- NO live Bright Data / Gemini call in any committed test: both backends are
  patched at their construction point, credential presence is faked via `settings`.

**Acceptance criteria**

- [x] One chain, both pipelines — `git grep` shows fetcher construction only in
      `youtube_ingest.py` (task body + its private primary helper); no per-path
      branching. Verified by `tests/unit/data/youtube/test_youtube_pipeline*.py`.
- [x] ONE Bright Data collection / exact Gemini subset —
      `test_youtube_ingest.py::TestFallbackChain::test_gemini_receives_exactly_the_transcript_less_subset`
- [x] Batch-wide triggers → whole batch to Gemini, task does not fail —
      `TestFallbackChain::test_batch_wide_trigger_sends_the_whole_batch_to_gemini`
      (parametrized over config error / request error / timeout).
- [x] Cost WARNING (reason + slot count + token/cost wording) —
      `TestFallbackWarnings::test_warning_names_reason_slot_count_and_gemini_cost`
- [x] Neither key → up-front `RuntimeError`; BD-only does not raise —
      `TestCredentialGate` (3 tests).
- [x] Metadata merge + real publish date — `TestMergeVideoMetadata` (test_types.py)
      and `TestBuildBatch::test_document_date_is_the_real_publish_date_not_ingest_time`.
- [x] Failure rows (`no_transcript: …` / `invalid_url: …`), normalized, DB failures
      WARNING-only — `TestNoTranscriptErrorStrings`, `TestBuildBatch`,
      `TestBulkBuildAndLoad`, `test_youtube_video.py::TestBuildFailureDocument`.
- [x] Replace-on-retry through `load_batch` —
      `TestLoadBatch::test_success_replaces_a_previously_errored_row` plus the
      integration `test_later_success_replaces_a_persisted_failure_row`.
- [x] Fetchers constructed in the task body; `gemini_transcript_fetcher.py`
      untouched (`git status` shows no change to it).
- [x] No live test added.
- [x] format-check / lint-check / pre-commit clean.

**Evidence**

```
$ make memory-unit-tests
tests/unit/data/youtube/test_types.py .....                              [ 30%]
tests/unit/data/youtube/test_youtube_ingest.py ......................... [ 32%]
tests/unit/data/youtube/test_youtube_pipeline.py .........               [ 34%]
tests/unit/data/youtube/test_youtube_pipeline_batch.py .....             [ 34%]
tests/unit/data/youtube/test_youtube_video.py ..................         [ 36%]
============================ 1824 passed in 41.12s =============================

$ make memory-integration-tests
FAILED tests/integration/memory/test_indexing_pipeline.py::TestMemoryIndexingPipeline::test_embeds_nodes
FAILED tests/integration/memory/test_meta_state.py::TestRecordDreamRun::test_updated_at_is_recent
===== 2 failed, 169 passed, 1 skipped, 105 deselected in 161.78s (0:02:41) =====
(both failures pre-existing + unrelated)

$ make memory-format-check && make memory-lint-check && make pre-commit
294 files already formatted / All checks passed! / ruff+prettier+biome Passed
```

End-to-end: ran the REAL `ingest-youtube-batch-etl` Prefect flow against local
MongoDB with both backends replaced by in-process fakes (no live call — #093 owns
live verification). 4 inputs: BD hit, BD miss → Gemini rescue, both miss, raw
unresolvable input.

```
WARNING | tree.data.youtube.youtube_ingest - Falling back to Gemini for 2/3 videos (reason=no_brightdata_transcript) — consumes Gemini tokens and incurs API cost
[fake gemini] transcribing 2 url(s): ['…v=bbbbbbbbbbb', '…v=ccccccccccc']
WARNING | tree.data.youtube.youtube_ingest - No transcript for …v=ccccccccccc (no_transcript: brightdata + gemini both returned empty)
INFO    | tree.data.youtube.youtube_video - Ingested: …v=aaaaaaaaaaa
INFO    | tree.data.youtube.youtube_video - Ingested: …v=bbbbbbbbbbb
INFO    | tree.data.youtube.youtube_video - Recorded ingest failure: …v=ccccccccccc (no_transcript: brightdata + gemini both returned empty)
INFO    | tree.data.youtube.youtube_video - Recorded ingest failure: please transcribe this thing (invalid_url: no video id in input)
INFO    | tree.data.youtube.youtube_pipeline_batch - YouTube: ingested 2 items (0 feeds, 4 single videos)

=== persisted rows ===
- …v=aaaaaaaaaaa  title='Bright Data title' authors=['Bright Data channel'] date=2024-03-01 12:00:00+00:00  ingest_error=None
- …v=bbbbbbbbbbb  title='oEmbed title'      authors=['oEmbed channel']      date=2026-07-24 (ingest time)   ingest_error=None
- …v=ccccccccccc  title='oEmbed title'      content=None                    ingest_error='no_transcript: brightdata + gemini both returned empty'
- please transcribe this thing  title=None   content=None                   ingest_error='invalid_url: no video id in input'
```

The first row is the ADR-004 §5 side effect in the flesh: Bright Data metadata wins
and `date` is the record's real `date_posted`, while the Gemini-rescued row keeps
its oEmbed base metadata and still falls back to ingest time.

**Notes**

- Shape changes (all internal, public flow/task names untouched):
  `fetch_transcripts_batch` now returns `(transcribed, failed)`; `build_batch` takes
  `(transcribed, failed, user_id)`; `_bulk_build_and_load` takes a keyword-only
  `invalid_inputs` and filters failure rows out of its return, so
  `ingest_youtube_video` still answers `None` when nothing was ingested and the
  batch flow's "ingested N items" count stays honest.
- Failure rows are persisted through the normal `load_batch`, so a pre-existing
  SUCCESSFUL row is never overwritten by a later failure (`load_video_document`
  skips it) — deliberate: a transient backend outage must not erase good content.
- Error strings are composed from a fixed vocabulary
  (`returned empty` / `not configured` / `unavailable (trigger rejected)` /
  `unavailable (poll timeout)`), never from exception text. Bright Data's exception
  message is logged at WARNING but never persisted.
- One small adjacent fix inside #089's `load_video_document`: a new failure row used
  to log "Ingested: <raw input>", which read as a success. It now logs
  "Recorded ingest failure: <uri> (<error>)". One test covers it.
- `make env-status` → local for every run.
- NOT RUN — live Bright Data / Gemini verification: out of scope here and explicitly
  forbidden for committed tests; #093 owns it.

### [Tester] 2026-07-24 15:32 — QA

**Test summary**
- Format / lint / pre-commit: PASS (`make memory-format-check`, `make
  memory-lint-check`, `make pre-commit` all clean).
- Unit tests: 1824 passed / 0 failed, 0 warnings (`make memory-unit-tests`).
- Integration tests: 169 passed / 1 skipped / 2 failed (`make
  memory-integration-tests`) — the 2 failures are the pre-existing, unrelated ones
  named in the task (`test_indexing_pipeline.py::test_embeds_nodes`,
  `test_meta_state.py::TestRecordDreamRun::test_updated_at_is_recent`); the 10
  YouTube integration tests all pass in isolation.
- Warnings: 0.
- `make env-status` → local for every run.

**Live-call audit (highest-priority check)**
`git grep -rn "BrightDataTranscriptFetcher\|GeminiTranscriptFetcher\|web_scraper_api\|\.collect("
tests/` across the WHOLE test tree: every construction point of both fetchers is
patched (unit + integration), `web_scraper_api.collect` is patched at its module
seam in `test_brightdata_transcript_fetcher.py`, and the rewritten
`test_youtube_pipeline.py` integration suite now patches BOTH
`BrightDataTranscriptFetcher` and `GeminiTranscriptFetcher` at their
`youtube_ingest`-module construction points (previously Gemini-only — confirmed
this was fixed). No live Bright Data or Gemini call is reachable from any
committed test.

**E2E adversarial pass**
- Happy path: real `ingest_youtube_batch` flow, 2 videos, Bright Data returns
  transcripts for BOTH → `GeminiTranscriptFetcher` ctor `assert_not_called()`,
  2/2 ingested. PASS — this is the cost-saving premise of the whole feature and it
  holds.
- Break path 1 (batch-wide trigger, no fallback available):
  `BrightDataTranscriptFetcher.fetch_many` raises `BrightDataRequestError` AND
  `GOOGLE_API_KEY` unset → `ingest_youtube_batch` returns `[]`, Gemini ctor never
  called, persisted row has
  `ingest_error="no_transcript: brightdata unavailable (trigger rejected); gemini not configured"`,
  task does not crash. PASS.
- Break path 2 (all-unresolvable batch): batch of 2 garbage strings →
  `ingest_youtube_batch` returns `[]`, NEITHER fetcher constructed (no billable
  call for unresolvable input), both rows persisted keyed on the raw string with
  `ingest_error="invalid_url: no video id in input"`. PASS.
- Break path 3 (mixed batch: success + transcript-less + unresolvable): 1 BD hit +
  1 double-miss + 1 raw garbage string in one batch → exactly 1 ingested Document,
  2 failure rows with the correct normalized errors, batch survives. PASS.
- Break path 4 (MCP single-video vs batch path, same URL): same canonical URL run
  once through `ingest_youtube_video` and once through `ingest_youtube_batch` →
  identical `source_uri` / `title` / `content`. PASS — no path drift.
- Break path 5 (re-run over an existing errored row): first run both-miss →
  errored row persisted; second run BD hit on the SAME URL → row REPLACED in place
  (1 row, not 2), `ingest_error` cleared, WARNING
  "Re-attempting previously failed ingest" logged. PASS.
- Break path 6 (boundary/hostile inputs via the MCP path): empty string,
  whitespace-only, unicode (`🎥🎬 ñ`), a 5000-char string, a SQL-fragment string,
  and a path-traversal-shaped string all run through `ingest_youtube_video` with
  no exception, each landing exactly one `invalid_url` row keyed on the raw input
  (Beanie/Mongo stores the raw string as data — no injection surface). PASS.
- Break path 7 (existing SUCCESS row + a later FAILURE attempt for the same
  video): loaded a real Document with real content, then ran
  `load_video_document` again with a `content=None` / `ingest_error=...` doc for
  the SAME `(user_id, source_uri)` → the successful row's `content` and
  `ingest_error=None` are UNCHANGED (skip path fires regardless of the incoming
  doc's own status). PASS — confirms the SWE's judgement call in Log item 2:
  a transient backend outage can never erase previously-ingested good content.
- Break path 8 (Gemini raises an unexpected exception mid-fallback — NOT one of
  the three named `BrightData*Error`s): with a fake Gemini `fetch_many` raising a
  bare `RuntimeError` while Bright Data already had 1 real hit + 1 miss in the
  same batch, `fetch_transcripts_batch` does NOT catch it — the exception
  propagates out of the task (Prefect's `retries=2` will retry the WHOLE batch,
  including the video Bright Data already successfully transcribed; nothing is
  persisted on this attempt, no `ingest_error` row for it either). This mirrors
  the codebase's pre-existing "batch-wide infra failure hard-fails the task,
  Prefect retries" pattern (`tree/data/batch.py`'s `gather_isolated` module
  docstring, unmodified by this task) and matches this task's own docstring
  ("Network → `retries=2` for batch-WIDE failures the chain cannot absorb"). Not a
  regression introduced by this task and not counted against the verdict, but
  flagged below as a real cost/efficiency risk worth a follow-up.

**Acceptance criteria**
- [x] PASS — Same fallback chain code path, no per-path branching — `git grep -n
      GeminiTranscriptFetcher apps/memory/src/tree` shows import + construction
      ONLY in `youtube_ingest.py`; `youtube_pipeline.py` /
      `youtube_pipeline_batch.py` contain no fetcher references.
- [x] PASS — ONE Bright Data collection over all URLs, Gemini gets EXACTLY the
      missing subset — `test_youtube_ingest.py::TestFallbackChain::test_brightdata_runs_first_over_every_url`
      and `::test_gemini_receives_exactly_the_transcript_less_subset`, both green;
      confirmed live via my own happy-path and mixed-batch adversarial runs above.
- [x] PASS — Batch-wide triggers (missing BD key, request error, poll timeout)
      send the whole batch to Gemini, task does not fail —
      `TestFallbackChain::test_batch_wide_trigger_sends_the_whole_batch_to_gemini`
      (parametrized ×3) plus my adversarial break path 1.
- [x] PASS — Every fallback WARNING names reason + slot count + Gemini token/cost
      wording — `TestFallbackWarnings::test_warning_names_reason_slot_count_and_gemini_cost`
      asserts the literal wording; the batch-wide variants fire the SAME warning
      line (verified by code read), so the wording assertion generalizes.
- [x] PASS — Neither key → up-front `RuntimeError` naming both env vars +
      `.env.example`, raised before any construction; BD-only does not raise —
      `TestCredentialGate` (3 tests); `.env.example` contains both
      `BRIGHTDATA_API_KEY` and `GOOGLE_API_KEY` (grep-confirmed).
- [x] PASS — Metadata merge: BD non-None wins, base survives nulls, Gemini branch
      leaves base intact, real `date_posted` reaches `Document.date` — `test_types.py::TestMergeVideoMetadata`
      (5 tests) + `test_youtube_ingest.py::TestBuildBatch` (5 tests, incl.
      `test_document_date_is_the_real_publish_date_not_ingest_time`).
- [x] PASS — Exhausted-slot / invalid-url failure rows, normalized errors, no raw
      exception dumps, DB failures WARNING-only — `TestNoTranscriptErrorStrings`
      (5 tests, incl. the poll-timeout case asserting `"BrightDataTimeoutError" not
      in error` and `"Traceback" not in error`), `test_youtube_video.py::TestBuildFailureDocument`
      (3 tests); confirmed both call sites (MCP + batch loose-video) route through
      `_partition_video_inputs` in adversarial break paths 2, 3, 6.
- [x] PASS — Failure rows replaceable via `load_batch` (#089 semantics) AND a
      SUCCESSFUL row is never overwritten by a later failure —
      `TestLoadBatch::test_success_replaces_a_previously_errored_row` +
      integration `test_later_success_replaces_a_persisted_failure_row`; the
      success-never-overwritten direction is NOT covered by a committed test, but
      I verified it directly against `load_video_document` (break path 7 above)
      and by code read (`existing.ingest_error is None` skip is independent of the
      incoming doc's status). Suggest the SWE add a regression test for this exact
      direction (see Other issues found).
- [x] PASS — Fetchers constructed inside the task body only, `gemini_transcript_fetcher.py`
      untouched — `git diff --stat` confirms no change to that file;
      `TestFallbackChain::test_fetchers_are_constructed_inside_the_task`.
- [x] PASS — No live test added; all new/rewritten tests mock the fetcher seams —
      exhaustive grep across `tests/` confirms every construction point and the
      `web_scraper_api.collect` seam are patched everywhere (see live-call audit
      above).
- [x] PASS — `make memory-format-check && make memory-lint-check && make
      pre-commit` clean; `make memory-unit-tests` green, 0 warnings; `make
      memory-integration-tests` green modulo the 2 named pre-existing failures.

**Evidence**
```
$ make memory-unit-tests
============================ 1824 passed in 41.34s =============================

$ make memory-integration-tests
FAILED tests/integration/memory/test_indexing_pipeline.py::TestMemoryIndexingPipeline::test_embeds_nodes
FAILED tests/integration/memory/test_meta_state.py::TestRecordDreamRun::test_updated_at_is_recent
===== 2 failed, 169 passed, 1 skipped, 105 deselected in 164.26s (0:02:44) =====

$ uv --directory apps/memory run pytest tests/integration/data/youtube -v
10 passed in 8.49s
```

**Other issues found**
- (PASS with note) An unexpected exception from Gemini's bulk `fetch_many` (i.e.
  anything other than the three named `BrightData*Error`s, which only Bright Data
  raises) is NOT caught anywhere in `fetch_transcripts_batch`; it propagates and
  fails the whole task, discarding any Bright-Data-sourced transcripts already
  fetched earlier in the SAME batch (Prefect's `retries=2` re-runs the whole
  chain, re-billing Bright Data for the videos it already answered). This
  matches the codebase's pre-existing "batch-wide infra failure hard-fails the
  task" pattern and is explicitly acknowledged in this task's own docstring, so
  it is not a regression and does not block this PASS — but since
  `GeminiTranscriptFetcher._fetch_one` already swallows essentially every
  Gemini-side failure into a per-slot `None`, an escaping exception here is by
  definition unusual/unrecoverable, and a follow-up that catches it and folds
  the remaining missing slots into per-slot `no_transcript` rows (mirroring the
  Bright-Data-batch-wide-trigger handling) would avoid re-billing Bright Data for
  work it already did. Worth a follow-up task, not a blocker.
- (PASS with note) No committed test drives the "existing SUCCESSFUL row + later
  FAILURE attempt" direction of `load_video_document`'s skip logic explicitly
  (only "success→success" and "errored→success/failure" are covered). I verified
  it directly (break path 7) and by code read; recommend the SWE add one unit
  test for this exact case for regression coverage, since it is the specific
  judgement call called out for this task.
- `docs/glossary.md` additions (`Ingest error`, `Transcript fallback chain`,
  `Transcript fetcher`) are present, topically match ADR-004, and were not
  authored by the SWE (grooming artifact, as expected per the task instructions).

**VERDICT: PASS**
