---
id: 091-brightdata-transcript-fetcher
feature: brightdata-youtube-transcripts
status: done
---

# BrightDataTranscriptFetcher: YouTube record mapping + `youtube:` config knobs

Tags: `data`, `config`
Depends on: #090
Blocks: #092
Implements: ADR-004

## Scope

Create `apps/memory/src/tree/data/youtube/brightdata_transcript_fetcher.py`, mirroring
`gemini_transcript_fetcher.py`'s shape (a class, `__init__` raising on a missing key,
`async fetch_many`), plus the two YAML knobs. Purely additive — the pipelines still use
Gemini until #092. **Complete separation from `GeminiTranscriptFetcher`: no shared base
class, no inheritance, no restructuring of the Gemini fetcher.** The two share only the
contract `async fetch_many(list[str]) -> list[FetchedTranscript | None]`.

**Config** (ADR-004 Decision 7): add a `YouTubeConfig` Pydantic model to
`apps/memory/src/tree/config/app_config.py` and a matching top-level block to
`apps/memory/configs/default.yaml`:

```yaml
youtube:
  # Bounded wait for one Bright Data collection. Measured: ~173s for one video;
  # 600 matches the Bright Data CLI default.
  brightdata_timeout_seconds: 600
  brightdata_poll_interval_seconds: 10
```

Chosen over the sources surface because ADR-003 made source entries operator DATA under
`sources/` (and removed `AppConfig.sources`); these knobs are static app tuning, like
`concurrency:`. `dataset_id` and the API base URL stay module constants (API identity).
NO new env vars; do NOT extend `_apply_env_overrides` beyond its documented
extraction-only scope. Do NOT touch `.env.example`.

**Fetcher:**

- Module constant `_YOUTUBE_DATASET_ID = "gd_lk56epmy2i5g7lzu0k"`.
- `__init__(*, api_key: SecretStr | None = None, timeout_seconds: float | None = None,
  poll_interval_seconds: float | None = None)` — key resolves from
  `settings.brightdata_api_key`, raises `BrightDataConfigurationError` when empty
  (mirrors Gemini's raise-on-missing-key shape; #092 never constructs it unconfigured);
  knobs default from the app config `youtube:` block.
- `fetch_many(video_urls_or_ids)`:
  1. Resolve each input via `extract_video_id`; unresolvable → `None` slot at debug
     level (mirrors Gemini; NEVER sent to Bright Data — invalid inputs are billable).
  2. ONE `web_scraper_api.collect(_YOUTUBE_DATASET_ID, [{"url": canonical_url}, …])`
     over ALL resolvable slots — the single collection per batch. This call is the one
     thin seam unit tests patch.
  3. Align records back to input order by `extract_video_id` of the record's
     `url`/`input.url` (records may arrive in any order); absent record → `None` slot.
  4. A record whose `transcript` is missing/empty/whitespace → `None` slot (transcript-
     less; #092 sends those to Gemini).
  5. Batch-WIDE failures (`BrightDataConfigurationError`, `BrightDataRequestError`,
     `BrightDataTimeoutError`) PROPAGATE — they must not be flattened into all-`None`,
     because #092 distinguishes batch-wide triggers (whole batch → Gemini) from
     per-slot misses. No WARNINGs at this layer (mirrors Gemini: user-facing WARNINGs
     live in the pipeline layer).

**Record → types mapping** (fixture-verified):

- `FetchedTranscript.plain_text` ← `transcript` (plain text).
- `segments` ← `formatted_transcript` `[{start_time, end_time, duration, text}]`, which
  is in MILLISECONDS → `TranscriptSegment.start_seconds = start_time / 1000`,
  `duration_seconds = duration / 1000` (SECONDS). Missing/null `formatted_transcript`
  with a present `transcript` → `segments=[]`.
- `language` ← record `transcription_language` when it is a non-empty string, else
  `None`. NEVER derived from `transcript_language` — that field is a LIST of languages
  YouTube *offers*, not what this transcript *is* (in the captured record it lists 6
  languages while `transcription_language` is null).
- `metadata` ← `VideoMetadata(video_id=record video_id, title=title,
  channel=handle_name or youtuber, channel_id=youtuber_id,
  publish_date=date_posted parsed tz-aware UTC (ISO-8601 with Z; unparseable → None —
  never a naive datetime, per the project rule), duration_seconds=video_length,
  description=description)`. Note `handle_name` is the display name ("Rick Astley"),
  `youtuber` the `@handle` — display name wins, matching what oEmbed/Atom put in
  `channel` today. Do NOT add new fields to `VideoMetadata`.

**Fixture:** the REAL captured snapshot payload is ALREADY committed at
`apps/memory/tests/unit/data/youtube/fixtures/brightdata_youtube_snapshot.json` (the
one-record array for `dQw4w9WgXcQ`, captured live during grooming — do NOT re-probe the
API, it costs money and ~3 minutes). Follow the existing `tests/unit/config/fixtures/`
convention. If the file is missing, STOP and ask — do not fabricate a payload.

Unit tests only (call the `/squid-testing-python` skill), fully mocked — NO live call to
Bright Data or Gemini, ever: mapping tested AGAINST THE COMMITTED FIXTURE (ms→s on
segments, tz-aware `publish_date` == 2009-10-25T06:57:33+00:00, `language is None`,
channel/channel_id/duration/description), order alignment with shuffled records, missing
record → None, empty-transcript record → None, unresolvable input → None without
billing, batch-wide error propagation, missing key raise, config defaults loaded from
YAML.

## Acceptance criteria

- [x] `BrightDataTranscriptFetcher` exists as specified; `git grep` shows NO import of
      `GeminiTranscriptFetcher` in the new module and no new shared base class anywhere.
- [x] `AppConfig` gains `youtube: YouTubeConfig` with `brightdata_timeout_seconds`
      (default 600) and `brightdata_poll_interval_seconds` (default 10); `default.yaml`
      gains the matching commented block; loading the app config yields those values.
- [x] `fetch_many` issues exactly ONE `collect(...)` per call over all resolvable slots
      and returns a list the same length/order as its input (unit-asserted with
      shuffled record order).
- [x] Fixture-driven mapping test passes against the committed REAL snapshot:
      `plain_text` == record `transcript`; first segment `start_seconds == 1.36`,
      `duration_seconds == 1.68` (1360/1680 ms ÷ 1000); `publish_date` tz-aware UTC;
      `language is None`; `channel == "Rick Astley"`, `channel_id ==
      "UCuAXFkgsw1L7xaCfnd5JJOw"`, `duration_seconds == 213`, `video_id ==
      "dQw4w9WgXcQ"`.
- [x] Transcript-less record → `None` slot; absent record → `None` slot; unresolvable
      input → `None` slot with ZERO Bright Data submission for it; batch-wide client
      errors propagate (not flattened to `None`s); missing `BRIGHTDATA_API_KEY` raises
      at construction.
- [x] `dataset_id` + base URL are module constants; NO new env vars; NO `enabled`
      toggle; `.env.example` untouched; `_apply_env_overrides` untouched;
      `gemini_transcript_fetcher.py` byte-for-byte unchanged.
- [x] NO live integration test added; pipelines still construct only
      `GeminiTranscriptFetcher` (wiring is #092).
- [x] `make memory-format-check && make memory-lint-check && make pre-commit` clean;
      `make memory-unit-tests` green, 0 warnings.

## Out of scope

- Wiring into `fetch_transcripts_batch` / any pipeline, the fallback chain, metadata
  merge, failure rows — all #092. New `VideoMetadata` fields (e.g. channel_url,
  views/likes) — not consumed by `build_document`.

## Log

### [SWE] 2026-07-24 — Implementation

**Files modified**

- `apps/memory/src/tree/data/youtube/brightdata_transcript_fetcher.py` (new) —
  `BrightDataTranscriptFetcher`: credential guard, ONE `collect(...)` per batch,
  order realignment by video id, record → `FetchedTranscript`/`VideoMetadata` mapping.
- `apps/memory/src/tree/config/app_config.py` — new `YouTubeConfig`
  (`brightdata_timeout_seconds=600.0`, `brightdata_poll_interval_seconds=10.0`) +
  `AppConfig.youtube`. `_apply_env_overrides` untouched.
- `apps/memory/configs/default.yaml` — top-level `youtube:` block with the two knobs.
- `apps/memory/tests/unit/config/fixtures/frozen_config.yaml` — matching `youtube:`
  block so the loader assertions stay decoupled from the operator-tuned default.yaml.
- `apps/memory/tests/unit/config/test_app_config.py` — 3 tests: YAML load, typed
  defaults when the block is absent, operator retune.
- `apps/memory/tests/unit/data/youtube/test_brightdata_transcript_fetcher.py` (new) —
  31 tests, `collect` patched in every one.
- `apps/memory/tests/unit/data/youtube/fixtures/brightdata_youtube_snapshot.json` —
  the REAL captured snapshot (1 record, 61 segments), staged with this task, used
  verbatim as the mapping fixture.

**Tests**

- Unit: 1789 passing, 0 failing, 0 warnings (`make memory-unit-tests`); 65 of them in
  the two files this task touches.
- Integration: N/A — no infra changes, and ADR-004 Decision 8 forbids a live test.

**Acceptance criteria**

- [x] Fetcher exists, no `GeminiTranscriptFetcher` import, no base class — the module's
      only `tree` imports are `app_config`, `settings`, `web_scraper_api.collect`,
      `web_unlocker.BrightDataConfigurationError`, `youtube.types`, `youtube.urls`;
      `grep -rn "class .*Fetcher.*("` over `src/` returns nothing.
- [x] `AppConfig.youtube` + `default.yaml` block —
      `test_app_config.py::TestLoadAppConfig::test_youtube_block_loaded_from_default_yaml`
      (+ `test_youtube_defaults_when_absent`, `test_youtube_timing_knobs_loaded_from_yaml`).
- [x] Exactly ONE `collect(...)`, order preserved —
      `test_brightdata_transcript_fetcher.py::TestCollectionShape::test_issues_exactly_one_collection_for_all_resolvable_slots`
      and `::test_records_are_realigned_to_input_order` (records shuffled).
- [x] Fixture-driven mapping — `TestRecordMapping::test_segments_convert_milliseconds_to_seconds`
      (1.36 / 1.68), `::test_publish_date_is_tz_aware_utc`, `::test_language_is_none_when_transcription_language_is_null`,
      `::test_metadata_is_mapped_from_the_record`, `::test_plain_text_is_the_record_transcript`.
- [x] `None`-slot + propagation + missing-key raise — `TestPerSlotMisses` (4 tests),
      `TestBatchWideFailures::test_client_errors_propagate_instead_of_flattening_to_none`
      (parametrized over configuration/request/timeout),
      `TestInit::test_missing_api_key_raises_configuration_error`.
- [x] Constants + no env surface — `_YOUTUBE_DATASET_ID` is a module constant, the API
      URLs stay in `web_scraper_api`; `git diff` on `.env.example` and on
      `gemini_transcript_fetcher.py` is empty; `app_config.py` is +26 lines, additive.
- [x] No live test; `youtube_ingest.py:47` still constructs only `GeminiTranscriptFetcher`
      and nothing outside the new module/test references `BrightDataTranscriptFetcher`.
- [x] `make memory-format-check && make memory-lint-check && make pre-commit` clean.

**Evidence**

```
$ make memory-unit-tests
tests/unit/config/test_app_config.py ..................................  [  1%]
tests/unit/data/youtube/test_brightdata_transcript_fetcher.py .......... [ 29%]
============================ 1789 passed in 40.31s =============================

$ make memory-format-check && make memory-lint-check
293 files already formatted
All checks passed!

$ make pre-commit
prettier.................................................................Passed
ruff check...............................................................Passed
ruff format..............................................................Passed
biome check (harness)....................................................Passed
```

End-to-end run outside pytest (`collect` stubbed with the captured snapshot — NO live
Bright Data call; live verification is #093):

```
$ uv run python scratchpad/e2e_brightdata_fetcher.py
dataset id       : gd_lk56epmy2i5g7lzu0k
timeout (yaml)   : 600.0
poll (yaml)      : 10.0
app_config.youtube: brightdata_timeout_seconds=600.0 brightdata_poll_interval_seconds=10.0
collect calls    : 1
collect inputs   : [{'url': 'https://www.youtube.com/watch?v=dQw4w9WgXcQ'}, {'url': 'https://www.youtube.com/watch?v=AAAaaaBBBcc'}]
slots            : [True, False, False]
video_id         : dQw4w9WgXcQ
title            : Rick Astley - Never Gonna Give You Up (Official Video) (4K Remaster)
channel          : Rick Astley
channel_id       : UCuAXFkgsw1L7xaCfnd5JJOw
publish_date     : 2009-10-25 06:57:33+00:00
duration_seconds : 213
language         : None
segments         : 61
segment[0]       : text='[♪♪♪]' start_seconds=1.36 duration_seconds=1.68
```

(input 2 has no record → `None` slot; input 3 is unresolvable → `None` slot and is
absent from the submitted inputs.)

**Notes**

- Two decisions the spec left implicit, both narrow:
  - Inputs are de-duplicated before submission (`dict.fromkeys`), so a video repeated in
    one batch is collected/billed once and both slots map from the same record.
  - `date_posted` that parses but is NAIVE is read as UTC rather than dropped (the
    vendor always sends `…Z`); unparseable/blank still → `None`. The project rule is
    "never a naive datetime", which both branches honour.
- `VideoMetadata.video_id` falls back to the slot's resolved id when the record's
  `video_id` is missing/blank, so a vendor null can't raise a `ValidationError`
  mid-batch.
- NOT RUN: any live Bright Data call, per the ADR-004 Decision 8 constraint — the
  cost-bounded live acceptance is task #093.
- Uncommitted grooming artifacts (`docs/glossary.md`, `docs/adrs/004_*.md`,
  `tasks/092-*`, `tasks/093-*`) were left untouched; they belong to a later commit.

### [Tester] 2026-07-24 15:30 — QA

**Test summary**
- Format / lint / pre-commit: PASS (`make memory-format-check`, `make memory-lint-check`, `make pre-commit` all clean)
- Unit tests: 1789 passed / 0 failed
- Integration tests (`make memory-integration-tests-ci`, mirrors CI): 92 + 42 + 43 = 177 passed, 43+27 deselected (mongot-marked), 0 failed
- Integration tests (`make memory-integration-tests-all`, full incl. mongot + slow, local Docker stack up): 135 + 42 + 70 = 247 passed, 1 skipped, 0 failed — the 2 pre-existing failures flagged in the brief (`test_indexing_pipeline.py::test_embeds_nodes`, `test_meta_state.py::TestRecordDreamRun::test_updated_at_is_recent`) did NOT reproduce this run (the former is `@pytest.mark.requires_mongot`-gated and only ran in the `-all` target where it passed; the latter is a known-flaky timing assertion that happened to pass). No new failures either way.
- Warnings: 0

**E2E adversarial pass** (all against the fetcher directly with `collect` monkeypatched — no live Bright Data/Gemini call; script at `scratchpad/e2e_adversarial_tester.py` in the Tester's scratch dir)
- Happy path: real committed fixture (`brightdata_youtube_snapshot.json`) through `fetch_many([VIDEO_A])` → `plain_text` matches record, 61 segments, `start_seconds=1.36`/`duration_seconds=1.68`, `publish_date=2009-10-25 06:57:33+00:00`, `channel="Rick Astley"`, `language=None` (PASS)
- Break 1 (malformed: `url` present/`input.url` absent, `url`+`input` both absent, `url=""` falling back to `input.url`): all three resolve correctly — matched-by-url, `None` slot with no crash, and fallback-to-`input.url` respectively (PASS)
- Break 2 (state edge: an unsolicited record for a video never requested mixed into the response): ignored cleanly, requested slot still correctly filled, no crash, no index collision (PASS)
- Break 3 (boundary: `formatted_transcript` present but `transcript` whitespace-only): → `None` slot, matches transcript-less contract (PASS)
- Break 4 (malformed: a segment dict missing the `start_time` key entirely, not just `None`): `_ms_to_seconds` treats the missing key as non-numeric → `0.0`, no `KeyError` (PASS)
- Break 5 (state edge: `collect` returns MORE records than were submitted, e.g. 3 records for 1 requested input): extra records ignored, correct single result returned (PASS)
- Break 6 (malformed: `date_posted` in a non-ISO US-style string `"01/25/2009"`): `datetime.fromisoformat` raises → caught → `publish_date=None`, no crash (PASS)
- Break 7 (boundary: same video submitted 3x in one batch — bare id, canonical URL, `youtu.be` shorthand): exactly ONE `collect(...)` call with ONE deduplicated input; all 3 output slots correctly populated with the same transcript, in order (PASS) — confirms SWE judgement call #1 is safe
- Break 8 (failure mode: `collect` raises `BrightDataRequestError`): propagates unchanged out of `fetch_many`, not flattened to `None`s (PASS) — confirms batch-wide vs per-slot distinction survives for #092
- Break 9 (state edge: 3 concurrent `fetch_many` calls on the same fetcher instance): each call gets its own correct result, `collect` invoked 3 times (once per call) with no cross-call state bleed — the fetcher is safely stateless across concurrent invocations (PASS)
- Break 10 (hostile: path traversal string, SQL-fragment string, `<script>` XSS payload as inputs): all fail `extract_video_id`'s regex/host check and resolve to `None` slots with zero submission — no injection surface exists in this module (it never touches a shell, SQL, or template) (PASS)
- Break 11 / 12 (boundary: empty input list, whitespace/empty-string input items): `[]` → `[]` with zero `collect` calls; `["   ", ""]` → `[None, None]` with zero `collect` calls (PASS)
- Break 13 / 14 (adversarial: a vendor record's own `video_id` field disagrees with the URL it was matched on): `VideoMetadata.video_id` reports the record's stated field (per spec: "video_id=record video_id"), not the URL-derived slot id — this is a vendor-data-integrity edge case outside what the fetcher is asked to defend against, and does NOT swap transcript *content* between slots (each slot's `plain_text`/`segments` still come from the record correctly matched to it by URL). Noted below, not a blocking defect.

**Acceptance criteria**
- [x] PASS — `BrightDataTranscriptFetcher` exists, no `GeminiTranscriptFetcher` import, no shared base class — `grep -n "GeminiTranscriptFetcher" apps/memory/src/tree/data/youtube/brightdata_transcript_fetcher.py` only matches a docstring comment, no `import`; `grep -rn "class .*Fetcher.*(" apps/memory/src/` returns only the two independent classes
- [x] PASS — `AppConfig.youtube: YouTubeConfig` with the two knobs, `default.yaml` block — `apps/memory/src/tree/config/app_config.py` diff (+26 lines, additive), `apps/memory/configs/default.yaml` diff; `test_app_config.py::TestLoadAppConfig::test_youtube_block_loaded_from_default_yaml` / `test_youtube_defaults_when_absent` / `test_youtube_timing_knobs_loaded_from_yaml` all pass
- [x] PASS — exactly ONE `collect(...)` per call, output length/order preserved — `TestCollectionShape::test_issues_exactly_one_collection_for_all_resolvable_slots` and `::test_records_are_realigned_to_input_order` pass; independently reconfirmed with my own shuffled-record + duplicate-input script (Break 7 above)
- [x] PASS — fixture-driven mapping matches every named value: `plain_text`, `start_seconds==1.36`, `duration_seconds==1.68`, tz-aware `publish_date==2009-10-25T06:57:33+00:00`, `language is None`, `channel=="Rick Astley"`, `channel_id=="UCuAXFkgsw1L7xaCfnd5JJOw"`, `duration_seconds==213`, `video_id=="dQw4w9WgXcQ"` — `TestRecordMapping` (9 tests) pass; reconfirmed live against the real fixture in my happy-path run above
- [x] PASS — transcript-less/absent/unresolvable → `None` slot with zero billing for the unresolvable slot; batch-wide client errors propagate; missing key raises at construction — `TestPerSlotMisses` (4 tests), `TestBatchWideFailures` (3 parametrized), `TestInit::test_missing_api_key_raises_configuration_error` all pass; independently reconfirmed (Break 3, 5, 8 above)
- [x] PASS — `_YOUTUBE_DATASET_ID` + base URLs stay module constants; no new env vars; `.env.example` untouched (`git status --short .env.example` empty); `_apply_env_overrides` untouched (not in the diff); `gemini_transcript_fetcher.py` byte-for-byte unchanged (`git diff apps/memory/src/tree/data/youtube/gemini_transcript_fetcher.py` is empty)
- [x] PASS — no live test added (fully mocked `collect` seam in all 31 tests); `youtube_ingest.py:47` still constructs only `GeminiTranscriptFetcher` — `grep -n "GeminiTranscriptFetcher\|BrightDataTranscriptFetcher" apps/memory/src/tree/data/youtube/*.py` confirms `BrightDataTranscriptFetcher` is referenced nowhere outside its own module/test
- [x] PASS — `make memory-format-check && make memory-lint-check && make pre-commit` clean; `make memory-unit-tests` green, 0 warnings (see Evidence)

**Evidence**
```
$ make memory-unit-tests
============================ 1789 passed in 40.32s =============================

$ make memory-integration-tests-all
tests/integration/memory: 135 passed
tests/integration/mcp: 42 passed
tests/integration (rest): 70 passed
tests/integration/data: 27 passed, 1 skipped
(0 failures across the entire local Docker + mongot stack)

$ make memory-format-check && make memory-lint-check
293 files already formatted
All checks passed!

$ make pre-commit
prettier.................................................................Passed
ruff check...............................................................Passed
ruff format..............................................................Passed
biome check (harness)....................................................Passed
```

**Judgement calls scrutinized**
1. **De-duplication before submission** — verified via Break 7: a video repeated 3 ways (bare id / canonical URL / `youtu.be` shorthand) in one batch submits exactly ONE input to `collect`, and all 3 slots correctly receive the transcript, in the original order. Matches ADR-004's cost framing. Accepted.
2. **Naive `date_posted` read as UTC** — the spec text ("unparseable → `None`, never a naive datetime") is satisfied either way; reading a naive-but-parseable timestamp as UTC (rather than discarding it) additionally serves the harder, project-wide "no naive datetimes" rule without losing real data, and is explicitly unit-tested (`test_naive_date_posted_is_read_as_utc`). The real vendor fixture always sends the `Z` suffix, so this branch is defensive/rarely-hit in practice. Accepted — reasonable, tested, and non-destructive.
3. **`VideoMetadata.video_id` falls back to the slot's resolved id** — confirmed the fallback path (used only when the record's own `video_id` is missing/blank) always uses the SAME id that was used to look up that record, so it cannot mis-attribute a transcript to the wrong video. Separately probed the primary (non-fallback) path with a synthetically inconsistent vendor record (`video_id` field disagreeing with the record's own `url`) — `VideoMetadata.video_id` then reports the vendor's stated field per the literal spec wording ("video_id=record video_id"), not the URL-derived id. This never swaps transcript *content* between slots; it is a vendor-data-integrity scenario outside this fetcher's stated responsibility, not represented in the real captured fixture. PASS with note — no code change requested.
- `frozen_config.yaml` edit — confirmed via `git stash` on just that file that all 1789 unit tests still pass without the edit (the new tests' asserted values equal `YouTubeConfig()`'s code-level defaults, so the block isn't strictly load-bearing for any assertion). Not masking a regression; harmless documentation-of-convention edit consistent with every other config block having a `frozen_config.yaml` entry. PASS with note.
- `gemini_transcript_fetcher.py` — `git diff` confirmed empty; `grep -rn "class .*Fetcher.*("` confirms no shared base class was introduced anywhere in `src/`.
- Batch-wide error propagation — confirmed both via the existing parametrized unit test and independently (Break 8): `BrightDataRequestError` raised by `collect` propagates unchanged out of `fetch_many`.
- Fixture verbatim — `apps/memory/tests/unit/data/youtube/fixtures/brightdata_youtube_snapshot.json` mtime (12:56, pre-SWE-work) predates every SWE-authored file (14:14+), confirming it's the untouched, pre-captured grooming artifact, not something the SWE wrote or edited.
- `docs/glossary.md` / ADR-004 / tasks 092-093 — mtimes (12:56–12:57) predate all SWE implementation files (14:14–14:19), confirming these are genuinely pre-existing grooming artifacts the SWE did not touch, not scope creep.

**Other issues found**
- None blocking. Two narrow judgement calls (naive-date-as-UTC, vendor-video_id-precedence-over-URL) and one harmless-but-not-strictly-required fixture edit (`frozen_config.yaml`) are documented above as PASS-with-note for visibility, not as defects requiring a fix.

**VERDICT: PASS**
