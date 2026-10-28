---
id: 224-stream-substack-web-youtube-in-batches
status: pending
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

- [ ] For each of Web / Substack / YouTube: with `batch_size=2` and 5 items, the extract/transcript
      step is called 3 times with chunk sizes `[2, 2, 1]` and load 3 times; returned documents
      equal the non-chunked result in order (unit tests with patched I/O).
- [ ] Substack: RSS items are never re-scraped; a chunk containing only RSS items makes no scrape call.
- [ ] YouTube: `invalid_url` rows are written exactly once across all chunks; one failing chunk's
      Bright Data error falls back only that chunk (others keep their Bright Data transcripts).
- [ ] A per-item failure in chunk 1 does not stop chunk 2; `ItemFailureTally.count` sums both chunks.
- [ ] Grep shows no stale "ONE/SINGLE isolated gather" / "WHOLE handed-in" wording in `tree/data`.
- [ ] Cadence green (memory targets, local env).
- [ ] [HUMAN] Time one Bright Data YouTube collection of 500 videos (or the largest affordable
      sample, extrapolated) against `youtube.brightdata_timeout_seconds: 600`. Run it with
      `GOOGLE_API_KEY` unset so a timeout cannot fan 500 videos out to Gemini. If it exceeds the
      timeout, raise the timeout (with the measurement in the YAML comment) — record the numbers here.

## Notes

- Write tests with `/squid-testing-python`.
- Dedup on `(user_id, source_uri)` keeps per-chunk retries idempotent; no new idempotency work.

## Log

### [Orchestrator] 2026-10-28 — Filed
