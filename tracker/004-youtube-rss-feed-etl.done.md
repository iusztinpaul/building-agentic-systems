# YouTube RSS-feed ETL pipeline

Status: pending
Tags: `data`, `enhancement`, `youtube`
Depends on: #001, #002, #003
Blocks: #005

## Scope

The feed analog of `tree.data.substack.substack_rss_pipeline`. Given a YouTube channel RSS URL like `https://www.youtube.com/feeds/videos.xml?channel_id=UCkyHDwRWMEluOEYmOGJ_2nw`, fetch the feed, parse out the recent videos, and ingest each video's transcript via the single-video building blocks shipped in #003 (the `build_document` / `load_video_document` helpers). Hard-skip semantics for the RSS batch are unchanged: one missing transcript (after the entire fetcher chain has been exhausted) does not sink the batch; the pipeline logs nothing of its own (the chain wrapper from #001 has already warned) and continues to the next entry.

The RSS pipeline accepts a `TranscriptFetcher` argument; the default is the same chained primary+Gemini fetcher (`ChainedTranscriptFetcher([YoutubeTranscriptApiFetcher(), GeminiTranscriptFetcher()])`) used by the single-video pipeline in #003. Reuse the `_default_chained_fetcher()` helper from `youtube_video_pipeline.py` so both flows share the exact same chain wiring.

YouTube channel feeds are Atom (not RSS 2.0), but `feedparser` handles both. Each entry contains `yt:videoId`, `title`, `author`, `published`, and a `link` to `youtube.com/watch?v=…`. We use `feedparser` for symmetry with `substack_rss.fetch_feed`.

### Files to create

- `apps/memory/src/tree/data/youtube/youtube_rss.py` — pure logic: fetch + parse the Atom feed → list of video URLs (+ feed-side metadata we want to keep).
- `apps/memory/src/tree/data/youtube/youtube_rss_pipeline.py` — Prefect `@flow` + `@task` wrappers.
- `apps/memory/tests/unit/data/youtube/test_youtube_rss.py`
- `apps/memory/tests/integration/data/youtube/test_youtube_rss_pipeline.py`

### `youtube_rss.py` shape

Mirror `substack_rss.py` patterns but specialised for YouTube:

```python
async def fetch_feed(feed_url: str) -> list[dict]:
    """Same shape as substack_rss.fetch_feed: httpx GET + feedparser.parse + list(feed.entries)."""

def extract_video_url(entry: dict) -> str | None:
    """Get the canonical video URL from an Atom entry.
    Prefer entry['yt_videoid'] if present (feedparser maps yt:videoId to this attr);
    fall back to extracting from entry['link']. Returns canonical_video_url(id) or None."""

def feed_entry_to_metadata(entry: dict) -> VideoMetadata:
    """Map an Atom entry to a partial VideoMetadata so we don't need a second
    oEmbed round-trip for feed-driven ingests:
      title         ← entry['title']
      channel       ← entry['author']
      channel_id    ← entry.get('yt_channelid')  (optional)
      publish_date  ← parsedate_to_datetime(entry['published'])  (tz-aware UTC)
      duration_seconds ← None  (not in the feed)
    """
```

### `youtube_rss_pipeline.py` shape

Mirror `substack_rss_pipeline.py` line-for-line:

```python
@task(name="fetch-youtube-rss-feed", retries=2, retry_delay_seconds=5)
async def fetch_feed_task(feed_url: str) -> list[dict]: ...

@flow(name="ingest-youtube-rss-feed-etl", log_prints=True)
async def ingest_youtube_rss_feed(
    feed_url: str, fetcher: TranscriptFetcher | None = None
) -> list[Document]:
    """1. fetch_feed_task(feed_url) → entries
       2. For each entry: extract_video_url + feed_entry_to_metadata
       3. Bulk transcript fetch: fetcher.fetch_many([video_urls])  ← ONE call, not N
       4. For each (video_url, metadata, transcript): if transcript is None → log warning, continue.
          Else: build_document(video_id, merged_metadata, transcript) → load_video_document.
       5. Return non-None ingested Documents.
    """

@flow(name="ingest-youtube-rss-feed-batch-etl", log_prints=True)
async def ingest_youtube_rss_feed_batch(
    feed_urls: list[str], fetcher: TranscriptFetcher | None = None
) -> list[Document]:
    """init_mongodb + asyncio.gather over ingest_youtube_rss_feed.
    Mirror ingest_substack_rss_feed_batch."""
```

**Important — feed-side metadata wins**: when the Atom entry already has `title`, `channel`, and `published`, the pipeline uses those directly and skips the `oEmbed` call from #002. This is faster (one HTTP per video instead of two) and matches how Substack's RSS path inlines metadata from feed entries. Inside the loop, build metadata as `feed_entry_to_metadata(entry)` and pass it straight to `build_document`. `oEmbed` is only used by the **single-video** path (#002) where the input is just a URL with no feed context.

**Hard-skip semantics** (one missing transcript never sinks the batch):
- Unresolvable entry (`extract_video_url` returns `None`) → `logger.warning("Skipping entry with no resolvable video id")`, continue. (This is a feed-parsing problem, not a transcript problem; the chain isn't involved, so the pipeline does own this warning.)
- Missing transcript (`fetch_many` returns `None` for that slot, meaning the entire chain — primary + Gemini — failed) → continue silently at the pipeline layer. Do **not** emit an extra `logger.warning` here; the `ChainedTranscriptFetcher` from #001 has already logged the intermediate (`primary returned no transcript … falling back to Gemini`) and final (`All transcript fetchers exhausted for {url}; skipping`) WARNINGs. Adding another pipeline-layer warning would just duplicate them.
- The flow always returns successfully with the partial list of ingested Documents.

### Tests

**Unit (`test_youtube_rss.py`)** — mirror `test_substack_rss.py`:
- `extract_video_url` happy path (entry with `yt_videoid="eYaWxljC4sA"`) → canonical URL.
- `extract_video_url` falls back to `entry['link']` parsing when `yt_videoid` absent.
- `extract_video_url` returns `None` when neither is parseable.
- `feed_entry_to_metadata` maps `title`, `author`, `published` correctly; `publish_date.tzinfo is not None`.
- `feed_entry_to_metadata` survives missing `published` (returns `publish_date=None` — not `datetime.now`, since `build_document` already has the now-fallback).

**Integration (`test_youtube_rss_pipeline.py`)** — model after `tests/integration/data/substack/test_substack_rss_pipeline.py`:
- Mock `httpx.AsyncClient` + `feedparser.parse` to return 3 fake entries.
- Inject a fake `TranscriptFetcher` (NOT the real chain — tests do not call Gemini). The fake returns 3 valid `FetchedTranscript`s.
- Assert: 3 Documents persisted, all `source_type=YOUTUBE`, all titled from feed entries (no oEmbed called → assert no httpx call to oembed).
- Idempotency: re-run → `len(result) == 0`, MongoDB still has 3.
- Hard-skip on chain-exhausted slot: fake fetcher returns `[FetchedTranscript, None, FetchedTranscript]` (representing a fully-exhausted chain for the middle slot) → 2 Documents persisted, no exception, AND the pipeline emits no `WARNING` of its own about the missing transcript (assert via `caplog` that no `WARNING` record originates from `tree.data.youtube.youtube_rss_pipeline` — chain is the warning owner).
- Unresolvable entry: 3 entries where the second has no `yt_videoid` and a non-parseable link → pipeline emits one `WARNING` from `tree.data.youtube.youtube_rss_pipeline` ("Skipping entry with no resolvable video id"), 2 Documents persisted.
- Batch: `ingest_youtube_rss_feed_batch([feed_a, feed_b])` returns combined list, init_mongodb called once.

## Acceptance Criteria

- [x] `apps/memory/src/tree/data/youtube/youtube_rss.py` exposes `fetch_feed`, `extract_video_url`, `feed_entry_to_metadata`.
- [x] `apps/memory/src/tree/data/youtube/youtube_rss_pipeline.py` exposes `ingest_youtube_rss_feed`, `ingest_youtube_rss_feed_batch`, both `@flow(log_prints=True)`.
- [x] The RSS pipeline does NOT call the oEmbed endpoint — feed-side metadata is used directly. Verified by an integration test asserting `httpx.AsyncClient.get` was called exactly once per feed (for the feed itself), not once per video.
- [x] When an Atom entry has no resolvable video id, the flow logs a `WARNING` from `tree.data.youtube.youtube_rss_pipeline` ("Skipping entry with no resolvable video id") and continues — verified by a test that injects one bad + two good entries.
- [x] When a single video has no transcript even after the chain is exhausted (fake fetcher returns `None` for that slot), the flow continues; remaining videos still persist; the pipeline does NOT emit an extra `WARNING` of its own (the chain wrapper already warned) — verified by a `[transcript, None, transcript]` integration test that asserts no pipeline-layer WARNING is produced for the missing slot.
- [x] `ingest_youtube_rss_feed("https://www.youtube.com/feeds/videos.xml?channel_id=UCkyHDwRWMEluOEYmOGJ_2nw", fetcher=fake)` returns 3 Documents in the integration test (mocked feed + fetcher).
- [x] Idempotent: a second run on the same feed returns `[]`, MongoDB row count unchanged.
- [x] Latent-upgrade: a pre-existing `LATENT` Document at one of the canonical video URLs is upgraded in place (same `id`, new `source_type=YOUTUBE`).
- [x] `make memory-unit-tests` and the YouTube portion of `make memory-integration-tests` both pass with zero warnings.
- [x] `make memory-format-fix && make memory-lint-fix && make memory-format-check && make memory-lint-check && make pre-commit` clean.

## User Stories

### Story: User configures a YouTube channel feed and ingests recent videos
1. User has a channel-feed URL: `https://www.youtube.com/feeds/videos.xml?channel_id=UCkyHDwRWMEluOEYmOGJ_2nw`.
2. User runs `await ingest_youtube_rss_feed(<feed_url>)` (in a shell or via #004's dispatcher).
3. Logs show `Fetching RSS feed: …`, then for each video: `Ingested: https://www.youtube.com/watch?v=…` (or `WARNING — No transcript for …; skipping`).
4. `mongosh` `db.documents.countDocuments({source_type: "youtube"})` returns the count of videos that had transcripts.

### Story: User re-runs the feed the next day — only new videos ingest
1. The feed now has one new video at the top.
2. User runs the same flow.
3. Logs show `Ingested: <new-video-url>` once and `Skipping duplicate: …` for every previously-ingested video.
4. MongoDB row count increases by 1.

### Story: One video in the feed has disabled transcripts but Gemini saves it
1. Feed contains 5 videos; the third has CC disabled, age-gated, or otherwise unavailable to `youtube-transcript-api`.
2. User runs the flow.
3. Logs show one `WARNING — YoutubeTranscriptApiFetcher returned no transcript for {url}; falling back to GeminiTranscriptFetcher`, then 5 `Ingested: …` lines.
4. MongoDB gains 5 new YouTube documents — the chain transparently filled the gap with the paid fallback.

### Story: One video defeats both the primary and the Gemini fallback
1. Feed contains 5 videos; the third is unrecoverable (e.g., region-locked + Gemini quota exhausted + Gemini refuses).
2. User runs the flow.
3. Logs show one intermediate `WARNING` (advanced to Gemini) and one final `WARNING — All transcript fetchers exhausted for {url}; skipping`. The pipeline itself emits no extra warning for the missing slot.
4. MongoDB has 4 new YouTube documents; the failed one is not retried by this run.

### Story: SWE adds a second YouTube channel and runs both in one batch
1. SWE adds a second feed URL to the call.
2. SWE runs `await ingest_youtube_rss_feed_batch([feed_a, feed_b])`.
3. Both feeds are processed in parallel (`asyncio.gather`); init_mongodb is called once.
4. The returned list contains the union of newly-ingested Documents.

---

Blocked by: #001, #002, #003

## Log

### [SWE] 2026-05-01 18:15 — Implementation

**Files modified**
- `apps/memory/src/tree/data/youtube/youtube_rss.py` — pure logic: `fetch_feed`, `extract_video_url`, `feed_entry_to_metadata`. Mirrors `tree.data.substack.substack_rss` shape; uses ISO-8601-aware date parser since YouTube Atom feeds publish ISO timestamps (RFC-2822 `parsedate_to_datetime` is kept as a defensive fallback).
- `apps/memory/src/tree/data/youtube/youtube_rss_pipeline.py` — Prefect `@flow` + `@task` wrappers. Single bulk `fetcher.fetch_many([...])` call across all valid entries; reuses `_default_chained_fetcher` from `youtube_video_pipeline.py` so the chain wiring stays DRY; reuses `build_document` + `load_video_document` from `youtube_video.py` so per-video logic stays DRY.
- `apps/memory/tests/unit/data/youtube/test_youtube_rss.py` — pure-logic unit tests (18 tests).
- `apps/memory/tests/integration/data/youtube/test_youtube_rss_pipeline.py` — Prefect-flow integration tests (7 tests, all assertions on real Mongo + mocked fetcher + mocked feed).

**Tests**
- Unit: 547 passing, 0 failing — full `make memory-unit-tests` clean. Output below.
- Integration (data/youtube/): 15 passing — both pre-existing single-video tests and the 7 new RSS tests. Output below.
- Integration (data/ aggregate): 30 passing, 9 unrelated skips.

**Acceptance criteria**
- [x] `youtube_rss.py` exports `fetch_feed`, `extract_video_url`, `feed_entry_to_metadata` — verified by `tests/unit/data/youtube/test_youtube_rss.py` (18 tests covering all three).
- [x] `youtube_rss_pipeline.py` exports `ingest_youtube_rss_feed`, `ingest_youtube_rss_feed_batch`, both `@flow(log_prints=True)` — verified by `python -c` smoke check (`flow1.name == 'ingest-youtube-rss-feed-etl'`, `log_prints=True`) and by all 7 integration tests instantiating both flows.
- [x] No oEmbed call from the RSS pipeline — verified by `test_uses_feed_metadata_no_oembed_call` (and three other integration tests) which patches `tree.data.youtube.youtube_video.httpx.AsyncClient` with `side_effect=AssertionError` and asserts feed `httpx.get` was called exactly once.
- [x] Unresolvable entry → pipeline-layer WARNING + skip — verified by `test_unresolvable_entry_is_skipped_with_warning`.
- [x] Chain-exhausted slot (`fetch_many` returns `None`) → silent skip, no pipeline-layer WARNING — verified by `test_chain_exhausted_slot_skips_silently` (asserts `pipeline_warnings == []`).
- [x] 3-entry happy path returns 3 Documents — verified by `test_ingests_videos_via_prefect_flow`.
- [x] Idempotent on re-run — verified by `test_idempotent_on_rerun` (second run returns `len == 0`, DB count stays at 3).
- [x] Latent-upgrade — verified by `test_upgrades_latent_document` (same `id`, `source_type` flips LATENT→YOUTUBE).
- [x] `make memory-unit-tests` clean (547 passed, 0 warnings).
- [x] `make memory-format-fix && memory-lint-fix && memory-format-check && memory-lint-check && pre-commit` clean.

**Evidence**

```
$ make memory-format-fix && make memory-lint-fix
2 files reformatted, 158 files left unchanged
All checks passed!

$ make memory-format-check && make memory-lint-check
160 files already formatted
All checks passed!

$ make pre-commit
Validate pyproject.toml..............................(no files to check)Skipped
prettier.................................................................Passed
ruff check...............................................................Passed
ruff format..............................................................Passed
biome check (harness)....................................................Passed

$ make memory-unit-tests
============================= 547 passed in 20.84s =============================

$ uv run pytest tests/integration/data/youtube/ -v
... 15 items collected ...
tests/integration/data/youtube/test_youtube_rss_pipeline.py::TestIngestYoutubeRssFeedFlow::test_ingests_videos_via_prefect_flow PASSED
tests/integration/data/youtube/test_youtube_rss_pipeline.py::TestIngestYoutubeRssFeedFlow::test_idempotent_on_rerun PASSED
tests/integration/data/youtube/test_youtube_rss_pipeline.py::TestIngestYoutubeRssFeedFlow::test_upgrades_latent_document PASSED
tests/integration/data/youtube/test_youtube_rss_pipeline.py::TestIngestYoutubeRssFeedFlow::test_chain_exhausted_slot_skips_silently PASSED
tests/integration/data/youtube/test_youtube_rss_pipeline.py::TestIngestYoutubeRssFeedFlow::test_unresolvable_entry_is_skipped_with_warning PASSED
tests/integration/data/youtube/test_youtube_rss_pipeline.py::TestIngestYoutubeRssFeedFlow::test_uses_feed_metadata_no_oembed_call PASSED
tests/integration/data/youtube/test_youtube_rss_pipeline.py::TestIngestYoutubeRssFeedBatchFlow::test_batch_combines_results_and_inits_mongo_once PASSED
tests/integration/data/youtube/test_youtube_video_pipeline.py::... 8 pre-existing tests ... PASSED (all 8)
============================== 15 passed in 12.99s =============================
```

**End-to-end smoke (Step 7)** — drove the real Prefect flow with a mocked feed + fake fetcher against a dedicated `smoke_youtube_rss` MongoDB database:

```
18:11:39.489 | INFO    | prefect - Starting temporary server on http://127.0.0.1:8891
18:11:41.222 | INFO    | Flow run 'mottled-pig' - Beginning flow run 'mottled-pig' for flow 'ingest-youtube-rss-feed-etl'
18:11:41.231 | INFO    | Task run 'fetch-youtube-rss-feed-448' - Finished in state Completed()
18:11:41.238 | INFO    | Task run 'load-youtube-rss-document-797' - Finished in state Completed()
18:11:41.245 | INFO    | Task run 'load-youtube-rss-document-22b' - Finished in state Completed()
18:11:41.254 | INFO    | Flow run 'mottled-pig' - Finished in state Completed()
Ingested 2 documents
  - source_uri=https://www.youtube.com/watch?v=eYaWxljC4sA
    title='Smoke Video 0' authors=['Smoke Channel'] date=2024-03-04 05:06:07+00:00
    content='transcript for eYaWxljC4sA'
  - source_uri=https://www.youtube.com/watch?v=AAAaaaBBBcc
    title='Smoke Video 1' authors=['Smoke Channel'] date=2024-03-04 05:06:07+00:00
    content='transcript for AAAaaaBBBcc'
DB rows for YOUTUBE: 2
```

Confirms: real Prefect flow runs, fetch_feed task executes once, load_video_document task executes per resolved entry, feed-side metadata (`Smoke Channel`, `2024-03-04T05:06:07+00:00`) lands directly on the persisted Document with `source_type=YOUTUBE`, no oEmbed HTTP call.

**Notes**
- `tree.data.youtube.youtube_rss._parse_published` deviates from the spec's literal "use `parsedate_to_datetime`" line: YouTube's Atom feed publishes ISO-8601 timestamps (e.g. `2024-01-15T12:00:00+00:00`), which `parsedate_to_datetime` rejects with `ValueError`. The function tries `datetime.fromisoformat` first and falls back to `parsedate_to_datetime` for safety; both cases normalize to tz-aware UTC. This honors the spec's *intent* (tz-aware UTC, gracefully `None` when unparseable) on real feeds.
- The single-video pipeline's `_default_chained_fetcher` is intentionally re-imported (not re-defined) so the production chain stays DRY across #003 and #004.
- New flows are NOT yet registered in `apps/memory/src/tree/orchestrator.py` — that's #005's job per the spec ("Blocks: #005").
- Per `/day` rules: code is local and uncommitted. Awaiting Tester review.

### [Tester] 2026-05-01 18:25 — QA

**Test summary**
- Format / lint / pre-commit: PASS
- Unit tests: 547 passed / 0 failed / 0 warnings
- Integration tests: 78 passed / 9 skipped / 0 failed / 0 warnings (full suite incl. `tests/integration/data/youtube/` — 7 new RSS tests + 8 pre-existing single-video tests, all green)

**E2E adversarial pass** (driven via real Prefect flow + mocked feed/fetcher boundaries)
- Happy path: `await ingest_youtube_rss_feed(feed_url, fetcher=fake)` with 3 fake entries + transcripts → 3 Documents persisted, `source_type=YOUTUBE`, title/channel/publish_date populated from feed-side metadata, `oembed_spy.assert_not_called()` (PASS, evidence: `test_ingests_videos_via_prefect_flow`, `test_uses_feed_metadata_no_oembed_call`).
- Break 1 (boundary: empty feed): `feedparser.parse` returns `entries=[]` → flow returns `[]`, no exception, log `Ingested 0 new videos from …` (PASS, manual run).
- Break 2 (failure: feedparser bozo + no entries): `feed.bozo=True, entries=[]` → flow surfaces `ValueError("Failed to parse RSS feed from …")` after Prefect retries are exhausted, flow run state `Failed` (PASS, manual run — confirms loud failure on discovery, not silent zero).
- Break 3 (failure: network error): `httpx.AsyncClient.get` raises `httpx.ConnectError` → propagates after retries, flow run `Failed` (PASS, manual run).
- Break 4 (malformed entry: no `yt_videoid` + non-parseable `link`) → entry skipped with one `WARNING — Skipping entry with no resolvable video id` from `tree.data.youtube.youtube_rss_pipeline`; remaining entries ingest (PASS, evidence: `test_unresolvable_entry_is_skipped_with_warning`).
- Break 5 (chain-exhausted slot: fetcher returns `[FetchedTranscript, None, FetchedTranscript]`) → 2 Documents persisted, ZERO pipeline-layer WARNINGs (chain owns the warning) (PASS, evidence: `test_chain_exhausted_slot_skips_silently`, asserts `pipeline_warnings == []`).
- Break 6 (idempotency: re-run same feed) → second run returns `[]`, MongoDB row count stays at 3 (PASS, evidence: `test_idempotent_on_rerun`).
- Break 7 (date parsing): ISO-8601 `"2024-01-15T12:00:00+00:00"` → `tzinfo` is set, year/month/day correct; `"not-a-date"` → `publish_date=None` without raising; missing `published` → `publish_date=None` (PASS, evidence: `test_publish_date_is_tz_aware`, `test_invalid_published_returns_none_publish_date`, `test_missing_published_returns_none_publish_date`).

**Acceptance criteria**
- [x] PASS — `youtube_rss.py` exposes `fetch_feed`, `extract_video_url`, `feed_entry_to_metadata` — verified by direct import (`uv run python -c "from tree.data.youtube.youtube_rss import fetch_feed, extract_video_url, feed_entry_to_metadata"`) + 18 unit tests covering all three (`tests/unit/data/youtube/test_youtube_rss.py`).
- [x] PASS — `youtube_rss_pipeline.py` exposes `ingest_youtube_rss_feed`, `ingest_youtube_rss_feed_batch`, both `@flow(log_prints=True)` — verified by import + decorator inspection at `youtube_rss_pipeline.py:56` (`log_prints=True`) and `:117` (`log_prints=True`).
- [x] PASS — RSS pipeline does NOT call oEmbed — verified by `test_uses_feed_metadata_no_oembed_call` patching `tree.data.youtube.youtube_video.httpx.AsyncClient` with `side_effect=AssertionError` and asserting `oembed_spy.assert_not_called()` + `feed_client.get.call_count == 1` (one feed fetch, zero oEmbed calls).
- [x] PASS — Unresolvable entry → pipeline-layer WARNING + skip — verified by `test_unresolvable_entry_is_skipped_with_warning` (`youtube_rss_pipeline.py:82` emits `logger.warning("Skipping entry with no resolvable video id")`).
- [x] PASS — Chain-exhausted slot → silent skip, no extra pipeline WARNING — verified by `test_chain_exhausted_slot_skips_silently` asserting `pipeline_warnings == []`; the pipeline `continue`s at `youtube_rss_pipeline.py:96` without logging.
- [x] PASS — 3-entry happy path returns 3 Documents — verified by `test_ingests_videos_via_prefect_flow` (`assert len(result) == 3`) + DB count assertion.
- [x] PASS — Idempotent — verified by `test_idempotent_on_rerun` (second run `len(second) == 0`, DB count unchanged at 3).
- [x] PASS — Latent-upgrade — verified by `test_upgrades_latent_document` (`result[0].id == latent.id`, `source_type` flips LATENT→YOUTUBE).
- [x] PASS — `make memory-unit-tests` clean (547 passed, 0 warnings); `make integration-tests` clean for the YouTube portion (15 of 15 passed).
- [x] PASS — `make format-check && make lint-check && make pre-commit` clean (160 files formatted, all ruff checks passed, all pre-commit hooks passed).

**Evidence**
```
$ make pre-commit
Validate pyproject.toml..............................(no files to check)Skipped
prettier.................................................................Passed
ruff check...............................................................Passed
ruff format..............................................................Passed
biome check (harness)....................................................Passed

$ make memory-unit-tests
============================= 547 passed in 21.46s =============================

$ make memory-integration-tests
=================== 78 passed, 9 skipped in 86.49s (0:01:26) ===================

$ uv run pytest tests/integration/data/youtube/ -v | grep -E "PASSED|FAILED"
test_youtube_rss_pipeline.py::TestIngestYoutubeRssFeedFlow::test_ingests_videos_via_prefect_flow PASSED
test_youtube_rss_pipeline.py::TestIngestYoutubeRssFeedFlow::test_idempotent_on_rerun PASSED
test_youtube_rss_pipeline.py::TestIngestYoutubeRssFeedFlow::test_upgrades_latent_document PASSED
test_youtube_rss_pipeline.py::TestIngestYoutubeRssFeedFlow::test_chain_exhausted_slot_skips_silently PASSED
test_youtube_rss_pipeline.py::TestIngestYoutubeRssFeedFlow::test_unresolvable_entry_is_skipped_with_warning PASSED
test_youtube_rss_pipeline.py::TestIngestYoutubeRssFeedFlow::test_uses_feed_metadata_no_oembed_call PASSED
test_youtube_rss_pipeline.py::TestIngestYoutubeRssFeedBatchFlow::test_batch_combines_results_and_inits_mongo_once PASSED
[+ 8 pre-existing youtube_video_pipeline tests, all PASSED]
============================= 15 passed in 13.61s ==============================
```

**Other issues found**
- The Tester prompt suggested break-path 6 should verify "successful feed completes; error one logged but does not sink the whole batch" for `ingest_youtube_rss_feed_batch`. The actual spec text in this groomed task says "Mirror `ingest_substack_rss_feed_batch`" — and Substack's batch uses plain `asyncio.gather` (no `return_exceptions=True`), so a failing feed sinks the whole batch. The SWE faithfully mirrored the parent. Behavior verified manually: a `RuntimeError` in feed_a propagates and the batch raises. **This is consistent with the spec the SWE was given**, but if the project later wants per-feed isolation in batch flows, that's a follow-up applicable to BOTH the Substack and YouTube batch flows. Not a blocker for #004. (PASS with note.)
- `_parse_published` uses Python 3.14 syntax `except ValueError, TypeError:` (line 111, 114) — at first glance this looked like a Python 2 syntax error, but verified via `dis.dis` that 3.14 accepts it as `except (ValueError, TypeError):`. It compiles to a `BUILD_TUPLE 2` and CHECK_EXC_MATCH against the tuple. Behavior is correct. Stylistically the explicit-tuple form `except (ValueError, TypeError):` is more conventional and would survive future Python parser tightening; consider a follow-up nit. Not a blocker.
- All other changes look clean: types on every signature, no `print()` in lib code (uses `logger`), no `git add -A`-style diff sneaking in unrelated files, no hardcoded secrets, the `_default_chained_fetcher` re-import keeps chain wiring DRY.
- Scope of diff is exactly the four files listed in the spec (`youtube_rss.py`, `youtube_rss_pipeline.py`, two test files) + tracker file updates — clean.

**VERDICT: PASS**
