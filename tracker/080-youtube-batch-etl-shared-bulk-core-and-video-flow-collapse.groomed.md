# youtube batch-ETL: shared bulk-transcript core + video per-item sub-flow collapse

Status: pending
Tags: `data`, `prefect`, `refactor`
Depends on: #078
Blocks: #082

## Scope

Apply the #078 batch-ETL pattern to BOTH YouTube leaf pipelines and factor the shared
bulk-transcript core. The direct-video pipeline currently does a PER-VIDEO
`fetcher.fetch_many([url])` inside per-URL sub-flows; the RSS pipeline already does ONE
bulk `fetcher.fetch_many(all_urls)` per feed. This task unifies the downstream tail at the
BATCH layer so BOTH paths do ONE bulk transcript fetch, then share build + load — with the
metadata SOURCE staying distinct (oEmbed per video for the direct path; feed metadata for
RSS). `build_document` + `load_video_document` are ALREADY shared functions; we add a
shared "(url, metadata) list → bulk transcripts → build_batch → load_batch" core called by
both. The direct-video per-item sub-flow `ingest_youtube_video` collapses into a core fn
with a thin MCP-only flow retained. Batch-flow names + signatures
(`ingest_youtube_video_batch`, `ingest_youtube_rss_feed_batch`, both with the `fetcher=`
swap kwarg) are UNCHANGED stable seams.

### 1. Shared bulk core — "(url, metadata) list → bulk fetch → build → load"

Add a shared core (suggested module `youtube_video.py` or a new `youtube_ingest.py`):

- **`_bulk_build_and_load(items, user_id, fetcher) -> list[Document]`** where `items` is a
  `list[tuple[str, VideoMetadata]]` of `(canonical_video_url, metadata)`. It:
  1. Does ONE `await fetcher.fetch_many([url for url, _ in items])` (the single bulk
     transcript fetch — NO per-video re-fetch).
  2. Zips transcripts back to `(url, metadata, transcript)`; for each non-`None`
     transcript, calls the existing shared `build_document(video_id=…, metadata=metadata,
     transcript=transcript, user_id=user_id)` (resolve `video_id` from the canonical URL).
     `None` transcripts are skipped silently (the chain wrapper already warned) — preserve
     today's behavior exactly.
  3. Loads via the existing shared `load_video_document` under
     `asyncio.gather(return_exceptions=True)` (per-element isolation: failures logged +
     skipped), returns the successful non-`None` subset.

  This is plain async core logic (NOT a `@flow`). The two batch flows wrap the build+load
  parts as ETL-phase `@task`s (`build_batch`, `load_batch`) so they appear as batch tasks in
  the Prefect UI — OR the SWE may expose `build_batch`/`load_batch` as the `@task`s and keep
  `_bulk_build_and_load` as their thin orchestration; either is acceptable provided: ONE
  bulk `fetch_many` per feed/list, build is a batch task, load is a SEPARATE batch task with
  per-element isolation.

### 2. RSS pipeline — supply feed metadata to the shared core

In `youtube_rss_pipeline.py`, keep `fetch_feed_task` (Extract, per feed, `retries=2`).
Restructure the per-feed body: resolve `(video_url, entry)` (skipping unresolvable ids with
the existing pipeline WARNING) → build `items = [(video_url, feed_entry_to_metadata(entry))
…]` → `await <shared build+load>(items, user_id, fetcher)`. DELETE the per-row
`load_video_task` `@task` (the load now happens in the shared `load_batch`). Keep the
single-feed `ingest_youtube_rss_feed` as a per-feed unit folded into the batch loop, OR keep
it as-is if it's not a sub-flow exploder — the key change is routing through the shared core
so the bulk fetch + build + load are batch-grain. Metadata source = feed
(`feed_entry_to_metadata`).

### 3. Video pipeline — adopt the bulk fetch; collapse the per-item sub-flow

In `youtube_video_pipeline.py`:

- Demote the body of the `ingest_youtube_video` `@flow` into a plain async core
  `_ingest_youtube_video_one(video_url, user_id, fetcher) -> Document | None` (resolve id →
  canonical url → per-video metadata via oEmbed (`fetch_oembed_metadata` +
  `parse_oembed_metadata`) → single-item bulk build+load through the shared core). This
  preserves the existing single-video oEmbed metadata path.
- Keep `ingest_youtube_video(video_url, user_id, fetcher=None) -> Document | None` as a
  1-line `@flow` wrapper around `_ingest_youtube_video_one`, used ONLY by the MCP URL router
  (`tree.data.ingest._ingest_youtube_video`). Keep `validate_parameters=False`.
- Rewire `ingest_youtube_video_batch(video_urls, user_id, fetcher=None)`: `init_mongodb`
  once; resolve each URL → `(canonical_url, oEmbed metadata)`; build `items`; call the
  SHARED core ONCE so there is ONE bulk `fetch_many(all_urls)` for the whole batch (this is
  the fix — today it does per-video `fetch_many([url])` inside per-URL sub-flows). The batch
  path MUST NOT call the thin `ingest_youtube_video` flow.
- DELETE the per-row `fetch_video_task` / `load_video_task` `@task`s (their work moves into
  the shared bulk core + batch tasks).

**Metadata distinction (preserve):** direct-video path enriches per video via oEmbed; RSS
path uses `feed_entry_to_metadata`. The SHARED part is only the bulk transcript fetch +
`build_document` + `load_video_document` — metadata is supplied by each caller.

### 4. Per-element isolation + shared helper

Same isolation contract as #078/#079. If the `tree.data.batch` isolation helper was
extracted in #079, reuse it in `load_batch` here; else inline. The bulk `fetch_many` ALREADY
provides per-slot resilience (the chain returns `None` per failed slot) — do NOT wrap each
transcript fetch in its own retry; preserve the existing chain-fallback behavior.

### Files touched

- `apps/memory/src/tree/data/youtube/youtube_video.py` (or a new
  `apps/memory/src/tree/data/youtube/youtube_ingest.py`) — add the shared
  `_bulk_build_and_load` core + the `build_batch` / `load_batch` ETL-phase tasks (placement
  is SWE discretion; keep `build_document` / `load_video_document` as the shared cores).
- `apps/memory/src/tree/data/youtube/youtube_video_pipeline.py` — add
  `_ingest_youtube_video_one` core; keep `ingest_youtube_video` as a thin MCP-only `@flow`;
  rewire `ingest_youtube_video_batch` to ONE bulk fetch via the shared core; delete per-row
  tasks.
- `apps/memory/src/tree/data/youtube/youtube_rss_pipeline.py` — route the per-feed body
  through the shared core; delete the per-row `load_video_task`; keep `fetch_feed_task` +
  `_default_chained_fetcher` import.
- `apps/memory/src/tree/data/youtube/youtube_rss.py`, `youtube_video.py` pure helpers
  (`extract_video_url`, `feed_entry_to_metadata`, `fetch_oembed_metadata`,
  `parse_oembed_metadata`, `build_document`, `load_video_document`) — UNCHANGED cores. Do
  NOT fix the pre-existing `except ValueError, TypeError:` in `youtube_rss._parse_published`.
- `apps/memory/src/tree/data/ingest.py` — UNCHANGED (imports + calls the thin
  `ingest_youtube_video`). Confirm import resolves.
- `apps/memory/tests/unit/data/youtube/test_youtube_video.py`, `test_youtube_rss.py` (+ new
  pipeline test modules if needed) — rework for the shared-core + thin-flow split.
- `apps/memory/tests/integration/data/youtube/test_youtube_video_pipeline.py`,
  `test_youtube_rss_pipeline.py` — assert ONE bulk fetch per feed/batch (see Test guidance).

## Acceptance Criteria

- [ ] A single shared core does "(url, metadata) list → ONE bulk `fetcher.fetch_many(...)`
      → `build_document` per slot → `load_video_document` per slot (isolated)", called by
      BOTH the video batch path and the RSS per-feed path.
- [ ] The direct-video BATCH path issues exactly ONE `fetch_many(all_urls)` for the whole
      batch (NOT per-video `fetch_many([url])`) — the per-video-fetch regression is gone.
- [ ] Metadata source stays distinct: video path uses oEmbed
      (`fetch_oembed_metadata`/`parse_oembed_metadata`); RSS path uses
      `feed_entry_to_metadata`. The shared core does not fetch metadata itself.
- [ ] `_ingest_youtube_video_one(url, user_id, fetcher)` exists as a plain async core;
      `ingest_youtube_video` remains a 1-line `@flow` wrapper (with
      `validate_parameters=False`) used by the MCP router; the BATCH flow does NOT call it.
- [ ] Per-row `fetch_video_task` / `load_video_task` (video) and `load_video_task` (RSS)
      `@task`s are removed; build is a batch task, load is a SEPARATE batch task
      (`retries=1`) with per-element isolation.
- [ ] `None`-transcript slots are skipped silently (chain already warned) and unresolvable
      feed ids still emit the pipeline WARNING — behavior preserved.
- [ ] Stable seams unchanged: `ingest_youtube_video_batch` / `ingest_youtube_rss_feed_batch`
      names + signatures (incl. `fetcher=` kwarg) intact; `_BATCHED_VARIANTS` resolves both;
      the MCP route to `ingest_youtube_video` still works; the `ingest_url` RSS-feed
      rejection guard is untouched.
- [ ] `make memory-format-fix && make memory-lint-fix && make memory-format-check &&
      make memory-lint-check` clean; `make pre-commit` clean.
- [ ] `make memory-unit-tests` passes, 0 warnings.
- [ ] `make memory-integration-tests` (fast tail) passes — both flows persist the expected
      docs with the injected fake `TranscriptFetcher`.
- [ ] [HUMAN] Deferred to #082: Prefect UI shows a youtube worker with batch ETL-phase tasks
      and ONE bulk transcript fetch per feed, no per-video sub-flow runs.

## BDD scenarios

### Scenario: the video batch does one bulk transcript fetch
- **Given** a batch of 5 video URLs and a fake `TranscriptFetcher`
- **When** `ingest_youtube_video_batch` runs
- **Then** `fetcher.fetch_many` is called exactly ONCE with all 5 canonical URLs (NOT 5
  times with one URL each), and 5 Documents are built + loaded.

### Scenario: RSS still does one bulk fetch per feed with feed metadata
- **Given** a feed with 3 resolvable entries
- **When** `ingest_youtube_rss_feed_batch` ingests it
- **Then** one feed fetch + one `fetch_many([3 urls])` occur, metadata comes from
  `feed_entry_to_metadata` (no oEmbed call), and 3 Documents persist.

### Scenario: the thin MCP flow ingests a single video with oEmbed metadata
- **Given** the MCP `ingest_url` router routes a youtube.com watch URL
- **When** it calls `ingest_youtube_video(url, user_id)`
- **Then** the video is resolved, oEmbed metadata is fetched, the transcript is fetched, and
  the Document persists (or `None` for a duplicate / no-transcript) — single-URL MCP ingest
  still gets its own flow run.

### Scenario: a missing transcript slot is isolated
- **Given** one of 5 videos has no transcript (chain returns `None` for that slot)
- **When** the batch runs
- **Then** that slot is skipped silently, the other 4 build + load, and the task does not
  hard-fail.

## User Stories

### Story: An operator confirms YouTube ingest fetches transcripts in bulk
1. The operator runs the data pipeline with `youtube_video` + `youtube_rss` sources.
2. They inspect the youtube worker run in the Prefect UI.
3. They see ONE bulk transcript fetch per feed/batch and batch `build`/`load` tasks — not a
   per-video fetch + per-video task explosion.

### Story: A user ingests one YouTube video from the assistant
1. The user pastes a youtube.com watch URL to the MCP `ingest_url` tool.
2. The router calls the thin `ingest_youtube_video` flow, which enriches via oEmbed,
   transcribes, and persists the one video.
3. The user gets back a single ingested Document (or a no-op) — unchanged from before.

### Story: A maintainer sees one transcript+build+load core, two metadata fronts
1. A maintainer reads both YouTube pipelines.
2. They confirm both call the same bulk-transcript + `build_document` + `load_video_document`
   core, differing only in where `VideoMetadata` comes from (oEmbed vs feed).
3. They can change the transcript/build/load behavior in one place for both pipelines.

## Test guidance

- Call `/testing-python`. Run ONLY via `make memory-*` (LOCAL env). Inject a fake
  `TranscriptFetcher` via the `fetcher=` kwarg (existing pattern in
  `test_youtube_rss_pipeline.py`) — no network, no Gemini.
- Video unit: assert `ingest_youtube_video_batch.fn(urls, user_id, fetcher=fake)` calls
  `fake.fetch_many` ONCE with all canonical URLs (a fake that records call count/args);
  test `_ingest_youtube_video_one` directly (oEmbed metadata path); test the thin
  `ingest_youtube_video.fn` delegates to the core; assert the batch flow does NOT call the
  thin flow.
- RSS unit: assert one `fetch_many` per feed with feed metadata; assert no oEmbed fetch in
  the RSS path; preserve the unresolvable-id WARNING + `None`-slot-skip tests.
- Shared-core unit: `_bulk_build_and_load(items, user_id, fake)` → one bulk fetch, builds
  per non-`None` slot, loads with isolation (patch `load_video_document`
  `side_effect=[doc, RuntimeError, doc]` → returns the 2 successes).
- Integration: keep existing flow assertions (persist docs against `mongo_client`); ADD a
  call-count assertion on the fake fetcher proving ONE bulk fetch per feed/batch.
- Retry-metadata asserts mirror `test_web_pipeline.py::TestTaskAndFlowMetadata`
  (`build_batch.retries == 0`, `load_batch.retries == 1`; `fetch_feed_task.retries == 2`).

---

Blocked by: #078

## Log

### [PA] 2026-06-23 — Grooming

**Summary**
Batch-ETL the two YouTube leaf pipelines and factor the shared bulk-transcript core. Both
paths now do ONE bulk `fetch_many` then share `build_document` + `load_video_document` at the
batch layer; metadata source stays distinct (oEmbed for direct video, feed for RSS). The
direct-video pipeline ADOPTS the bulk fetch (today it does per-video `fetch_many([url])`
inside per-URL sub-flows). `ingest_youtube_video`'s body collapses into
`_ingest_youtube_video_one`; a thin `ingest_youtube_video` `@flow` is retained for the MCP
router only.

**Key decisions**
- The shared core is "(url, metadata) list → bulk fetch → build → load", per the brief's
  grilled design (decision 4 youtube). Metadata is the caller's responsibility (oEmbed vs
  feed) so no regression to either metadata source.
- The direct-video batch's per-video `fetch_many([url])` is the concrete regression this
  fixes → ONE bulk fetch per batch; asserted by a call-count test on the fake fetcher.
- Bulk `fetch_many` already gives per-slot resilience + chain fallback; do NOT add an
  extra per-transcript retry (no network-retry regression). `None` slots skip silently as
  today.
- Thin MCP flow retained ONLY for `tree.data.ingest`'s single-URL path; batch path calls the
  core directly. Keep `validate_parameters=False` on the thin flow.
- Reuse the `tree.data.batch` isolation helper if #079 extracted it; else inline.

**Dependencies**
- #078 — establishes the batch-task + isolation pattern. (Independent of #079; both depend
  only on #078, but the queue runs 079 then 080 — either order is safe.)

**User stories**
- 3 stories: operator confirms bulk transcript fetch; user ingests one video via MCP; a
  maintainer sees one shared transcript/build/load core with two metadata fronts.

**Documentation discipline**
- Canonical glossary terms (Batch, ETL-phase task, Thin MCP flow, URL router). No new terms.

Ready for implementation.
