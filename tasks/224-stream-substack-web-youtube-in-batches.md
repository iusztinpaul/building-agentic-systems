---
id: 224-stream-substack-web-youtube-in-batches
status: in-progress
feature: data-batching
---

# Stream the Substack, Web and YouTube batch flows in `data.batch_size` chunks (extract → load per chunk)

Tags: `memory`, `data`, `prefect`, `substack`, `web`, `youtube`
Depends on: 223
Blocks: 226
Implements: ADR-002 amendment #078–#082 (batch-grain ETL tasks), extended by task 226.

## Problem

The three platform flows run each ETL phase ONCE over the whole flattened shard:

- `ingest_web_url_batch` (`data/web/web_pipeline.py`): `extract_batch(all urls)` → `load_batch(all docs)`.
- `ingest_substack_batch` (`data/substack/substack_pipeline_batch.py`): scrape all articles, then
  `load_batch(all items)`.
- `ingest_youtube_batch` (`data/youtube/youtube_pipeline_batch.py`): ONE `_batch_build_and_load`
  → ONE Bright Data collection over every video in the shard.

With 100,000 items that means (a) every scraped `Document` (with content) held in RAM before the
first load — several GB; (b) one Prefect task retry replays the whole shard, and for Web
(`extract-web-batch`, `retries=2`, Bright Data Web Unlocker) re-bills all 100,000 scrapes;
(c) one 100,000-video Bright Data collection against a 600 s `youtube.brightdata_timeout_seconds`,
whose timeout falls the WHOLE batch back to Gemini (video-token cost). Good: the
`ingest_arxiv_dataset` shape — `for chunk in …: extract → load`.

## Scope

1. In each flow, after flattening, iterate `itertools.batched(items, app_config.data.batch_size)`
   and run the existing ETL tasks per chunk, accumulating the loaded documents:
   - **Web:** `extract_batch(chunk)` → `load_batch(extracted)`.
   - **Substack:** feeds are still resolved up front (`gather_isolated`, capped by task 223); then
     chunk the combined work list (resolved RSS items + article URLs, configured order) so each
     chunk scrapes only its article URLs via `extract_article_batch` and calls `load_batch` once.
   - **YouTube:** oEmbed/feed resolution stays up front; call `_batch_build_and_load(chunk, …)` per
     chunk (one Bright Data collection per chunk). `invalid_inputs` are persisted exactly once
     (e.g. with the first chunk, or one trailing call with no items) — never dropped, never duplicated.
2. Keep task names, retry counts and per-element isolation unchanged; only the number of task runs
   changes (≈ phases × ⌈N / 500⌉ per shard).
3. Fix every docstring/comment these modules now contradict: "ONE isolated gather", "SINGLE
   isolated gather", "over the WHOLE handed-in URL list", "ONE `fetch_many` … over ALL items",
   "small constant task-run count whatever the shard size" (grep `tree/data` for them).

## Out of scope

- HF (task 225), ADR text (task 226), the online single-document path (`online_pipeline.py`).

## Acceptance criteria

- [x] For each of Web / Substack / YouTube: with `batch_size=2` and 5 items, the extract/transcript
      step is called 3 times with chunk sizes `[2, 2, 1]` and load 3 times; returned documents
      equal the non-chunked result in order (unit tests with patched I/O).
- [x] Substack: RSS items are never re-scraped; a chunk containing only RSS items makes no scrape call.
- [x] YouTube: `invalid_url` rows are written exactly once across all chunks; one failing chunk's
      Bright Data error falls back only that chunk (others keep their Bright Data transcripts).
- [x] A per-item failure in chunk 1 does not stop chunk 2; `ItemFailureTally.count` sums both chunks.
- [x] Grep shows no stale "ONE/SINGLE isolated gather" / "WHOLE handed-in" wording in `tree/data`.
- [x] Cadence green (memory targets, local env).
- [ ] [HUMAN] Time one Bright Data YouTube collection of 500 videos (or the largest affordable
      sample, extrapolated) against `youtube.brightdata_timeout_seconds: 600`. Run it with
      `GOOGLE_API_KEY` unset so a timeout cannot fan 500 videos out to Gemini. If it exceeds the
      timeout, raise the timeout (with the measurement in the YAML comment) — record the numbers here.

## Notes

- Write tests with `/squid-testing-python`.
- Dedup on `(user_id, source_uri)` keeps per-chunk retries idempotent; no new idempotency work.

## Log

### [Orchestrator] 2026-10-28 — Filed

### [SWE] 2026-10-28 15:10 — Implementation

**Files modified**
- `apps/memory/src/tree/data/web/web_pipeline.py` — `ingest_web_url_batch` walks `urls` in `itertools.batched(..., app_config.data.batch_size)` chunks: `extract_batch(chunk)` → `load_batch(extracted)` per chunk; module/task/flow docstrings reworded (no "WHOLE handed-in", "SINGLE isolated gather", "each ONCE over the whole URL list").
- `apps/memory/src/tree/data/substack/substack_pipeline_batch.py` — feeds still resolved up front via `gather_isolated`; work list = resolved RSS items, then article URLs; per chunk, `extract_article_batch` runs ONLY when the chunk has article URLs, then one `load_batch`. Docstrings reworded ("ONE isolated gather", "small constant task-run count").
- `apps/memory/src/tree/data/youtube/youtube_pipeline_batch.py` — oEmbed/feed resolution stays up front; `_batch_build_and_load(chunk, ...)` per chunk, `invalid_inputs` passed with the first chunk only (`None` after). An invalid-only shard still makes one core call over `[]`. Module/flow docstrings reworded ("ONCE", "over ALL items").
- `apps/memory/src/tree/data/youtube/youtube_pipeline.py` — docstring/comment fixes only (`load_batch`, `fetch_transcripts_batch`, `_batch_build_and_load` "called ONCE per feed/batch", `resolve_video` "task-run count constant per shard", `_ingest_youtube_video_one`). No logic change.
- `apps/memory/tests/unit/data/web/test_web_pipeline.py` — new `TestIngestWebUrlBatchChunks` (per-chunk extract/load, chunked == single-chunk result, cross-chunk failure tally); renamed the "once over the url list" test to the one-chunk case.
- `apps/memory/tests/unit/data/substack/test_substack_pipeline_batch.py` — per-chunk scrape/load sizes, chunked == single-chunk result, RSS never scraped (mixed shard), RSS-only shard makes no scrape call, cross-chunk failure tally; module docstring updated.
- `apps/memory/tests/unit/data/youtube/test_youtube_pipeline_batch.py` — real-core tests: per-chunk `fetch_many`/`load_batch` sizes, chunked == single-chunk result, `invalid_url` rows written exactly once, a Bright Data timeout on chunk 2 falls back ONLY chunk 2 to Gemini, cross-chunk failure tally; bulk-mock test that `invalid_inputs` ride only the first chunk. Stale "ONE fetch_many" comments updated.
- `apps/memory/tests/unit/data/youtube/test_youtube_pipeline_bulk.py` — one docstring reworded ("single isolated gather").

**Tests**
- Unit: 5240 passing, 0 failing — `make memory-tests` (local env).
- Integration: N/A — the project has no integration suite.

**Acceptance criteria**
- [x] Per platform, `batch_size=2` + 5 items → extract/transcript called with `[2, 2, 1]`, load 3 times, result equal to the non-chunked result in order — `test_web_pipeline.py::TestIngestWebUrlBatchChunks::test_runs_extract_then_load_once_per_chunk` / `::test_chunked_result_equals_the_single_chunk_result`; `test_substack_pipeline_batch.py::test_articles_are_scraped_and_loaded_once_per_chunk` / `::test_chunked_result_equals_the_single_chunk_result`; `test_youtube_pipeline_batch.py::test_transcripts_are_fetched_and_loaded_once_per_chunk` / `::test_chunked_result_equals_the_single_chunk_result`.
- [x] Substack RSS never re-scraped; RSS-only chunk makes no scrape call — `test_substack_pipeline_batch.py::test_rss_items_are_never_scraped`, `::test_an_rss_only_shard_makes_no_scrape_call`.
- [x] YouTube `invalid_url` rows exactly once; one failing chunk falls back alone — `test_youtube_pipeline_batch.py::test_invalid_url_rows_are_written_exactly_once_across_chunks`, `::test_invalid_inputs_ride_only_the_first_chunk`, `::test_a_bright_data_failure_falls_back_only_its_chunk`.
- [x] Chunk-1 failure does not stop chunk 2; `ItemFailureTally.count` sums both — `::test_a_failure_in_one_chunk_does_not_stop_the_next` in all three test modules (tally == 2).
- [x] Stale wording grep clean (command below).
- [x] Cadence green (local env).
- [ ] [HUMAN] Time a 500-video Bright Data YouTube collection — NOT RUN (billable); left for the human.

**Evidence**
```
$ make env-status
Env target: local (.env)

$ make memory-format-fix && make memory-lint-fix && make memory-format-check && make memory-lint-check
338 files already formatted
All checks passed!

$ make pre-commit
prettier.......Passed  ruff check.......Passed  ruff format.......Passed  biome check (harness).......Passed

$ make memory-tests
======================= 5240 passed in 62.91s (0:01:02) ========================

$ grep -rniE "one isolated (load )?gather|single isolated gather|whole handed-in|small constant task-run|constant per shard|whole URL list|ONCE over" apps/memory/src/tree/data
(no output)
$ grep -rniE "<same pattern>" apps/memory/tests/unit/data
(no output)

# E2E smoke: real Substack flow (plain httpx, free) in-process vs local Mongo, 3 chunks of 7
$ TREE_DATA__BATCH_SIZE=7 uv --directory apps/memory run --env-file ../../.env python <scratch>/substack_chunks.py \
    https://www.oneusefulthing.org/feed https://www.oneusefulthing.org/p/30-months-on-the-exponential-in-charts <user_id>
data.batch_size=7
Substack chunk processed: 7 ingested so far
Substack chunk processed: 14 ingested so far
Substack chunk processed: 20 ingested so far
Substack: ingested 20 items (1 feeds, 1 single articles)
# prefect task-run ls for the run: fetch-substack-rss-feed x1, load-substack-batch x2 (RSS-only chunks,
# no scrape), then extract-substack-article-batch x1 + load-substack-batch x1 (chunk 3: 6 RSS + 1 article;
# the article is the same post as a feed item, so it deduped). Rerun: "ingested 0 items" (idempotent).
```

**Notes**
- Substack work-list order is "RSS items first, then article URLs" — today's flatten order, kept so the chunked result equals the non-chunked one. I did not read "configured order" as entry order, because that would change the baseline.
- YouTube: `invalid_inputs` ride the FIRST chunk, not a trailing empty call, so no extra pair of task runs. An invalid-only shard (`items == []`) still makes exactly one core call over `[]`.
- Web: an empty `urls` list now makes zero task calls (it used to call `extract_batch([])` + `load_batch([])`); the flow still initialises Mongo and returns `[]`.
- Substack: a chunk whose article scrapes all fail still calls `load_batch([])` once (a no-op), so it stays one load per chunk.
- `batch_size` is read at call time from `tree.config.app_config.app_config` (tests monkeypatch `app_config.data`; operators can use `TREE_DATA__BATCH_SIZE`).
- Web and YouTube e2e NOT RUN — both bill Bright Data (Web Unlocker / dataset collection). They are covered by unit tests with patched I/O (the YouTube tests run the real `_batch_build_and_load` / `fetch_transcripts_batch` chain with fake fetchers).
- The e2e smoke wrote to the LOCAL DB for user `6ad34350d22e8f7ce231b861`: 20 `substack` docs from oneusefulthing.org, plus 32 `latent` reference rows that `load_document` created.
- Remaining `fetch_many … over all` wording in `brightdata_transcript_fetcher.py` describes one `fetch_many` call, and that is still accurate, so I left it.


### [Tester] 2026-10-28 15:00 — QA

**Test summary**
- Format / lint / pre-commit: PASS (`make memory-format-check`, `make memory-lint-check`, `make pre-commit`; env target local)
- Unit tests: 5240 passed / 0 failed (`make memory-tests`, re-run independently)
- Integration tests: N/A (project has no integration suite)
- Warnings: 0 new

**E2E adversarial pass** (no billable Bright Data / Gemini calls; flows run via `.fn` with patched I/O at the task/fetch boundary, real `gather_isolated` + `track_item_failures` where noted; script `/private/tmp/claude-501/-Users-pauliusztin-Documents-01-Projects-AI-Engineer-Handbook-building-agentic-systems-building-agentic-systems/b800c6fd-2742-4e67-a758-9a5dfc53cf28/scratchpad/qa/test_qa224.py`, 21 tests, all PASS)
- Happy path: SWE's in-process Substack run (batch_size=7, 20 RSS + 1 article -> 3 chunks, rerun ingested 0) accepted from the log; chunk behaviour re-verified by my own tests below. Web/YouTube e2e not run (billable).
- Boundary `batch_size=1`: Web 3 urls -> sizes [1,1,1], 3 loads; Substack (3 RSS + 2 articles) for n in 1..6 and 100 -> result order `[r0,r1,r2,a1,a2]` identical for every n, each article scraped exactly once, no chunk > n; YouTube 3 items -> [1,1,1] (PASS)
- Boundary `batch_size > len(items)` (100) and `== len` (3): exactly one chunk, one extract/load (PASS)
- Empty shards: Web `[]` -> `[]`, zero task calls; Substack `[]` -> `[]`, no scrape/load; YouTube `[]` -> `[]`, core not called (PASS)
- Substack all feeds fail: returns `[]`, load never called, tally.count == 2 (PASS)
- Web real `gather_isolated` with failing urls b,d in chunks [a,b | c,d | e], batch_size=2: result `[a,c,e]` in order, `ItemFailureTally.count == 2` accumulated across chunks (PASS) -> the worker (`offline_pipeline.py:268` wraps the whole `offline_ingest_batch` in one `track_item_failures()`) therefore sees the summed count and raises `PartialIngestError`; chunks run in the same task context, no new context scoping.
- State/failure: Web chunk 2 `extract_batch` raising a batch-wide error -> flow raises, chunk 3 never runs (fail loud, nothing swallowed) (PASS)
- YouTube `invalid_inputs`: for batch_size 1/2/3/100 with a feed + one invalid loose video, kwarg is the invalid list on chunk 0 and `None` on every later chunk; result order == item order (PASS). Invalid-only shard (2 junk urls) -> exactly one core call with `[]` items and both raw inputs (PASS).
- YouTube first chunk's `_batch_build_and_load` raises: flow raises after exactly 1 core call (invalid rows not written in that run; nothing later runs). The flow has no flow-level retry (`@flow(name="ingest-youtube-batch-etl", ...)` has no `retries`), and even a manual re-run is idempotent: `load_video_document` replaces an existing `ingest_error` row (`youtube.py:448-460`) and skips a duplicate success row, keyed on `(user_id, source_uri)` -> no duplicates (PASS; identical to the pre-change single-call behaviour).
- Mutation check: changing `invalid_inputs if index == 0 else None` to always pass `invalid_inputs` -> `make memory-tests` 2 failures (`test_invalid_url_rows_are_written_exactly_once_across_chunks`, `test_invalid_inputs_ride_only_the_first_chunk`); reverted, diff restored. Tests are not vacuous.
- Gemini fallback per chunk: `test_a_bright_data_failure_falls_back_only_its_chunk` runs the real `_batch_build_and_load`/`fetch_transcripts_batch` chain with fake fetchers; BD calls `[2,2,1]`, Gemini called once with only chunk 2's two urls (PASS).

**Acceptance criteria**
- [x] PASS — per platform `batch_size=2`, 5 items -> `[2,2,1]`, 3 loads, result == non-chunked in order — `TestIngestWebUrlBatchChunks::test_runs_extract_then_load_once_per_chunk` / `::test_chunked_result_equals_the_single_chunk_result` (web); `test_articles_are_scraped_and_loaded_once_per_chunk` / `test_chunked_result_equals_the_single_chunk_result` (substack, youtube); plus my parametrised order tests.
- [x] PASS — Substack RSS never re-scraped, RSS-only chunk makes no scrape call — `test_rss_items_are_never_scraped`, `test_an_rss_only_shard_makes_no_scrape_call`; my test asserts `scraped == [p/1, p/2]` for every batch_size.
- [x] PASS — YouTube `invalid_url` written once; failing chunk falls back alone — see above.
- [x] PASS — chunk-1 per-item failure does not stop chunk 2; tally sums — `test_a_failure_in_one_chunk_does_not_stop_the_next` x3 (tally == 2) + my web real-gather test.
- [x] PASS — stale wording grep clean: `grep -rniE "one isolated (load )?gather|single isolated gather|whole handed-in|small constant task-run|constant per shard|whole URL list|ONCE over|over ALL items" apps/memory/src/tree/data apps/memory/tests/unit/data` -> no output.
- [x] PASS — cadence green (format/lint/pre-commit/`make memory-tests` 5240 passed, local env).
- [ ] [HUMAN] Bright Data 500-video timing — awaiting human verification (billable, not run).

**Evidence**
```
$ make memory-tests            -> 5240 passed in 57.10s (exit 0)
$ pytest .../qa/test_qa224.py  -> 21 passed in 0.97s
$ make memory-format-check && make memory-lint-check && make pre-commit
  338 files already formatted / All checks passed! / prettier, ruff check, ruff format, biome: Passed
```

**Judgement of the SWE's 3 behaviour decisions**
1. Substack work-list order = RSS items first, then article URLs (not entry order): ACCEPT — identical to the pre-change flatten order, so chunked == non-chunked result (verified for every batch_size 1..6, 100). The task text's "configured order" is ambiguous; keeping the baseline is the safe reading.
2. YouTube `invalid_inputs` on the first chunk rather than a trailing empty call: ACCEPT — no extra task runs; written once (mutation-checked); invalid-only shard still gets one call. Failure mode (first chunk raises -> rows not written this run) is the same as before and re-run is idempotent.
3. Web empty `urls` makes zero task calls: ACCEPT — harmless; flow still inits Mongo and returns `[]`.

**Other issues found (not blocking)**
- DOCSTRING WRAPPING (fix before commit, trivial): `apps/memory/src/tree/data/youtube/youtube_pipeline_batch.py` module docstring line 9 is 142 chars ("RSS metadata comes from the feed, single-video metadata from oEmbed; once flattened to ``(url, VideoMetadata)`` the two are indistinguishable."); ruff does not flag it (no E501) so the formatter/linter won't. Re-wrap to <= ~88 chars. Other touched docstring lines at 89 chars (marginal, same as surrounding repo style): `substack_pipeline_batch.py` module docstring ("``substack.load_document``. The two kinds …") and flow docstring ("matching web. They run once per chunk …"); `web_pipeline.py` module docstring ("``app_config.data.batch_size`` chunks and runs two ETL-phase tasks …") and flow docstring ("(persist each) per chunk — two task runs per chunk. …"); `youtube_pipeline.py` `_ingest_youtube_video_one` ("directly, once per chunk of its URL list, instead. Returns the persisted Document, or"). Pre-existing lines in these files are already longer, so only the 142-char one is a real wrap defect.
- Substack: a chunk whose article scrapes all fail still runs `load_batch([])` (one wasted no-op Prefect task run). Documented by the SWE; could skip when `items` is empty. Optional.
- Substack still holds every resolved RSS `Document` in RAM up front (feeds are resolved before chunking, per spec scope) — only article scraping is streamed. Acceptable per scope; worth a line in task 226's ADR text.
- E2E smoke by the SWE left 20 substack docs + 32 `latent` rows in the LOCAL DB for user `6ad34350d22e8f7ce231b861` (local only).

**VERDICT: PASS** (one cosmetic fix requested: re-wrap the 142-char docstring line in `youtube_pipeline_batch.py`; not a functional defect)

[Orchestrator] Code committed; awaiting the [HUMAN] Bright Data timing AC before status: done.
