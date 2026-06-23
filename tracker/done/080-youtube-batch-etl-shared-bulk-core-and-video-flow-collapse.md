# youtube batch-ETL: shared bulk-transcript core + video per-item sub-flow collapse

Status: in-progress
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

- [x] A single shared core does "(url, metadata) list → ONE bulk `fetcher.fetch_many(...)`
      → `build_document` per slot → `load_video_document` per slot (isolated)", called by
      BOTH the video batch path and the RSS per-feed path.
- [x] The direct-video BATCH path issues exactly ONE `fetch_many(all_urls)` for the whole
      batch (NOT per-video `fetch_many([url])`) — the per-video-fetch regression is gone.
- [x] Metadata source stays distinct: video path uses oEmbed
      (`fetch_oembed_metadata`/`parse_oembed_metadata`); RSS path uses
      `feed_entry_to_metadata`. The shared core does not fetch metadata itself.
- [x] `_ingest_youtube_video_one(url, user_id, fetcher)` exists as a plain async core;
      `ingest_youtube_video` remains a 1-line `@flow` wrapper (with
      `validate_parameters=False`) used by the MCP router; the BATCH flow does NOT call it.
- [x] Per-row `fetch_video_task` / `load_video_task` (video) and `load_video_task` (RSS)
      `@task`s are removed; build is a batch task, load is a SEPARATE batch task
      (`retries=1`) with per-element isolation.
- [x] `None`-transcript slots are skipped silently (chain already warned) and unresolvable
      feed ids still emit the pipeline WARNING — behavior preserved.
- [x] Stable seams unchanged: `ingest_youtube_video_batch` / `ingest_youtube_rss_feed_batch`
      names + signatures (incl. `fetcher=` kwarg) intact; `_BATCHED_VARIANTS` resolves both;
      the MCP route to `ingest_youtube_video` still works; the `ingest_url` RSS-feed
      rejection guard is untouched.
- [x] `make memory-format-fix && make memory-lint-fix && make memory-format-check &&
      make memory-lint-check` clean; `make pre-commit` clean.
- [x] `make memory-unit-tests` passes, 0 warnings.
- [x] `make memory-integration-tests` (fast tail) passes — both flows persist the expected
      docs with the injected fake `TranscriptFetcher`. (All youtube tests pass; the only
      failures in the full tail are pre-existing flakes in unrelated layers — see Log.)
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

### [SWE] 2026-06-23 — Implementation: shared bulk-transcript core + video sub-flow collapse

**Shared-core design**
- New module `apps/memory/src/tree/data/youtube/youtube_ingest.py` factors the single tail
  BOTH YouTube pipelines run: "(url, metadata) list → ONE bulk `fetch_many` → build → load".
  Three batch-grain ETL-phase `@task`s (per feed/batch, NOT per video):
  - `fetch_transcripts_batch` (`retries=2`) — the SINGLE bulk `fetch_many(all_urls)`; zips
    transcripts back, drops `None` slots silently (chain already warned).
  - `build_batch` (`retries=0`) — pure map via the already-shared `build_document`.
  - `load_batch` (`retries=1`) — per-element-isolated load via the already-shared
    `load_video_document` under `tree.data.batch.gather_isolated` (#079 helper, REUSED).
  - `_bulk_build_and_load(items, user_id, fetcher)` — plain async orchestration both
    pipelines call once per feed/batch. Metadata is the CALLER's job → no metadata regression.

**New structure (both pipelines)**
- `youtube_video_pipeline.py`: per-item `@flow` body demoted to plain `_ingest_youtube_video_one`
  (resolve id → canonical URL → oEmbed metadata → one-item shared core). `ingest_youtube_video`
  kept as a thin 1-line `@flow` (`validate_parameters=False`) — MCP router only. `ingest_youtube_video_batch`
  rewired: resolve each URL → `(canonical, oEmbed metadata)` items → shared core ONCE ⇒ ONE bulk
  `fetch_many(all_urls)` for the whole batch (the #080 fix; was per-video `fetch_many([url])`).
  Deleted `fetch_video_task` + `load_video_task`.
- `youtube_rss_pipeline.py`: kept `fetch_feed_task` (`retries=2`) + `_default_chained_fetcher`
  import; per-feed body folded into plain `_ingest_one_feed` (fetch_feed → `_resolve_feed_items`
  via `feed_entry_to_metadata`, NO oEmbed → shared core). Deleted per-row `load_video_task` and
  the standalone non-batch `ingest_youtube_rss_feed` flow (confirmed not an MCP entry point via
  grep — only the removed integration test referenced it). `ingest_youtube_rss_feed_batch`
  signature unchanged.
- Pure cores UNCHANGED: `youtube_video.py`, `youtube_rss.py` (incl. the pre-existing
  `except ValueError, TypeError:` in `_parse_published`, left untouched per scope — valid on
  Python 3.14). `ingest.py` router UNCHANGED (still imports/calls thin `ingest_youtube_video`).

**Files modified**
- `apps/memory/src/tree/data/youtube/youtube_ingest.py` — NEW shared bulk core + 3 ETL tasks.
- `apps/memory/src/tree/data/youtube/youtube_video_pipeline.py` — `_ingest_youtube_video_one`
  core, thin MCP flow, batch rewired to ONE bulk fetch, per-row tasks deleted.
- `apps/memory/src/tree/data/youtube/youtube_rss_pipeline.py` — per-feed body through shared
  core, `load_video_task` + non-batch feed flow deleted.
- `apps/memory/tests/unit/data/youtube/test_youtube_ingest.py` — NEW shared-core unit tests.
- `apps/memory/tests/unit/data/youtube/test_youtube_video_pipeline.py` — NEW pipeline unit tests.
- `apps/memory/tests/unit/data/youtube/test_youtube_rss_pipeline.py` — NEW pipeline unit tests.
- `apps/memory/tests/integration/data/youtube/test_youtube_video_pipeline.py` — reworked: ONE
  bulk-fetch call-count assertion; batch retargeted to the shared-core fetcher contract.
- `apps/memory/tests/integration/data/youtube/test_youtube_rss_pipeline.py` — reworked: all
  cases drive `ingest_youtube_rss_feed_batch` (the non-batch feed flow is gone); one-bulk-fetch
  + no-oEmbed assertions preserved.

**Tests**
- Unit: full suite `make memory-unit-tests` → 1676 passed, 0 failures, 0 warnings. New youtube
  unit tests: 105 collected (36 net-new pipeline/core tests), all pass.
- Integration (fast tail, `make memory-integration-tests`): all 15 youtube integration tests
  pass in every run. The full tail had pre-existing FLAKES in layers I did not touch — run 1:
  `test_meta_state::test_updated_at_is_recent` + `test_indexing_pipeline::test_embeds_nodes`;
  run 2: those two PLUS the LIVE-network `test_web_serp::test_common_query_returns_at_least_one_organic_result`.
  Non-deterministic set across runs ⇒ flaky (shared-DB cross-test pollution + live Bright Data
  SERP). All three PASS in isolation and in their own subset; none are in the youtube data layer.

**Acceptance criteria** — all non-`[HUMAN]` verified:
- [x] Shared core "(url, metadata) → ONE bulk fetch → build → load" called by both paths —
      `test_youtube_ingest.py::TestBulkBuildAndLoad`, `TestFetchTranscriptsBatch`.
- [x] Direct-video batch issues exactly ONE `fetch_many(all_urls)` —
      `test_youtube_video_pipeline.py::TestIngestYoutubeVideoBatch::test_one_bulk_fetch_over_all_canonical_urls`
      (unit) + `integration::test_ingests_multiple_videos_with_one_bulk_fetch`.
- [x] Metadata distinct (oEmbed vs feed); shared core fetches no metadata —
      `test_youtube_video_pipeline.py::TestIngestOne::test_resolves_oembed_metadata_then_calls_shared_core`,
      `test_youtube_rss_pipeline.py::TestResolveFeedItems` + `integration::test_uses_feed_metadata_no_oembed_call`.
- [x] `_ingest_youtube_video_one` plain core; `ingest_youtube_video` thin `@flow`; batch does
      NOT call it — `TestIngestOne`, `TestThinFlow`, `TestIngestYoutubeVideoBatch::test_does_not_call_thin_flow`.
- [x] Per-row tasks removed; build/load separate batch tasks (`retries=1`, isolated) —
      `TestTaskAndFlowMetadata::test_per_row_tasks_are_gone`, `test_per_row_load_task_is_gone`,
      `TestLoadBatch::test_isolates_one_element_failure`.
- [x] `None` slots skip silently; unresolvable feed id WARNs — `TestFetchTranscriptsBatch::test_none_transcript_slot_dropped`,
      `TestResolveFeedItems::test_unresolvable_entry_warns_and_is_dropped`,
      `integration::test_chain_exhausted_slot_skips_silently` + `test_unresolvable_entry_is_skipped_with_warning`.
- [x] Stable seams: names + signatures (incl. `fetcher=`) intact; `_BATCHED_VARIANTS` resolves both;
      MCP route works — `test_batch_flow_signature_unchanged`, `test_pipeline.py::test_every_batched_variant_resolves_without_mocks`,
      `test_ingest.py::test_routes_youtube_video_url`.
- [x] format/lint/pre-commit clean; unit + youtube-integration green.

**Evidence**
```
$ make memory-unit-tests
======================= 1676 passed in 67.92s (0:01:07) ========================

$ uv run pytest tests/unit/data/youtube/{test_youtube_ingest,test_youtube_video_pipeline,test_youtube_rss_pipeline}.py -q
36 passed in 11.56s

$ uv run pytest tests/integration/data/youtube/ -q
15 passed in 24.15s

$ make memory-format-fix && make memory-lint-fix && make memory-format-check && make memory-lint-check
All checks passed!  /  289 files already formatted  /  All checks passed!

$ make pre-commit
ruff check ... Passed   ruff format ... Passed   biome check (harness) ... Passed

# End-to-end runtime exercise (fake fetcher, throwaway DB e2e_task080_twin, cleaned up):
VIDEO BATCH -> ingested 3 docs; fetch_many call count = 1 ; first call urls = 3   # ONE bulk fetch, oEmbed metadata
RSS BATCH   -> ingested 3 docs; fetch_many call count = 1 ; first call urls = 3   # ONE bulk fetch, oEmbed NEVER called
RSS titles (FEED metadata): ['V0', 'V1', 'V2']   RSS authors: ['Feed Channel']
```

**Notes**
- The full fast-integration tail is RED only on pre-existing flakes outside my scope
  (`tests/integration/memory/*` shared-DB pollution + `test_web_serp` live Bright Data SERP).
  They pass in isolation and in their own subsets, and the failure set differs run-to-run.
  Recommend the Tester confirm against `main`/baseline; not introduced by #080.
- `AGENTS.md` shows as modified in `git status` — that is the owner's pre-existing WIP, NOT
  part of this changeset. Left untouched.
- DO NOT COMMIT — handing off to the Tester.

### [Tester] 2026-06-23 — QA: PASS

**Test summary**
- Format / lint / pre-commit: PASS (`289 files already formatted`; `All checks passed!`;
  pre-commit prettier/ruff-check/ruff-format/biome all Passed).
- Unit tests: 1676 passed / 0 failed (`make memory-unit-tests`, 0 warnings). Youtube subset:
  105 passed (`tests/unit/data/youtube/`).
- Integration tests (fast tail): youtube subset 15/15 passed (`tests/integration/data/youtube/`).
  Full `make memory-integration-tests`: 177 passed / 2 failed / 1 skipped — the 2 failures are
  PROVEN pre-existing flakes (see below), NONE in the youtube layer.
- Warnings: 0.

**Pre-existing-flake proof (decisive)**
- `make memory-integration-tests` failed on `test_indexing_pipeline::test_embeds_nodes` +
  `test_meta_state::test_updated_at_is_recent` (both `tests/integration/memory/*`).
- Both PASS in isolation (`2 passed in 7.38s`).
- Stashed ALL #080 changes (`git stash -- <8 youtube files>`) and re-ran the full fast tail on
  baseline: SAME 2 failures (`2 failed, 177 passed`). ⇒ pre-existing shared-DB cross-test
  pollution, NOT introduced by #080. Stash restored. The claimed third flake (`test_web_serp`,
  live Bright Data) did not fire this run — consistent with the "non-deterministic set" claim.

**E2E adversarial pass** (real `@flow` objects, real Beanie/Mongo throwaway DB `e2e_task080_tester`,
spy fetcher recording every `fetch_many`; DB dropped after):
- Happy path / Break1 (THE #080 fix): `ingest_youtube_video_batch([5 urls])` → `fetch_many`
  await_count == 1 with all 5 canonical URLs, 5 docs built+loaded → PASS (NOT 5× per-video).
- Break2a (metadata): direct-video path applies oEmbed metadata (title/authors) → PASS.
- Break2b (shared core / distinct metadata): `ingest_youtube_rss_feed_batch` → ONE `fetch_many`,
  feed metadata (`feed_entry_to_metadata`), oEmbed spy call_count == 0 (raises if touched) → PASS.
- Break3 (isolation): None-transcript slot + a load-raising slot both dropped → 3/5 survive,
  still ONE fetch, batch does not hard-fail; `gather_isolated` logs WARNING + skips → PASS.
- Break4 (boundary: empty batch): `[]` → no `fetch_many`, returns `[]`, no crash → PASS.
- Break5 (malformed: `example.com`, `ftp://garbage`, empty string mixed in): unresolvable URLs
  skipped, resolvable 2 → ONE fetch, 2 docs, no crash → PASS.
- Break6 (thin MCP flow): `ingest_youtube_video(single)` → oEmbed metadata, ONE fetch, persists → PASS.
- Break7 (state edge: RSS feed all-unresolvable entries): no fetch, returns `[]`, WARNING per
  bad entry preserved → PASS.

**Acceptance criteria**
- [x] PASS — Shared core "(url, metadata) list → ONE bulk fetch → build → load" called by both
      paths — `youtube_ingest._bulk_build_and_load`; unit `test_youtube_ingest.py::TestBulkBuildAndLoad`;
      e2e Break1+Break2b both route through it.
- [x] PASS — Direct-video batch issues exactly ONE `fetch_many(all_urls)` — e2e Break1
      (await_count==1, 5 URLs) + unit `TestIngestYoutubeVideoBatch::test_one_bulk_fetch_over_all_canonical_urls`
      + integration `test_ingests_multiple_videos_with_one_bulk_fetch`.
- [x] PASS — Metadata distinct (oEmbed vs feed), core fetches none — e2e Break2a/2b (RSS oEmbed
      call_count==0 via raising spy); unit `test_one_bulk_fetch_per_feed_with_feed_metadata`.
- [x] PASS — `_ingest_youtube_video_one` plain (no `.fn`); `ingest_youtube_video` thin `@flow`
      with `should_validate_parameters=False` (verified at runtime); batch does NOT call it —
      unit `TestThinFlow`, `test_does_not_call_thin_flow`.
- [x] PASS — Per-row tasks gone (`fetch_video_task`/`load_video_task` removed, `hasattr`==False);
      `build_batch` retries==0, `load_batch` retries==1 with `gather_isolated` per-element isolation
      — verified at runtime + `test_youtube_ingest.py::TestTaskMetadata`, `TestLoadBatch::test_isolates_one_element_failure`.
- [x] PASS — None-transcript slots skip silently (no redundant pipeline WARNING — integration
      `test_missing_transcript_skips_quietly`); unresolvable feed id still WARNs — e2e Break7 + unit
      `test_unresolvable_entry_warns_and_is_dropped`.
- [x] PASS — Stable seams: `ingest_youtube_video_batch`/`ingest_youtube_rss_feed_batch` names +
      signatures (incl. `fetcher=`) intact; `_BATCHED_VARIANTS` resolves both
      (`test_every_batched_variant_resolves_without_mocks`); MCP route works
      (`test_routes_youtube_video_url`) + `ingest_url` RSS rejection intact (`test_rejects_youtube_rss_feed_url`);
      `data/pipeline.py` + `tree.data.ingest` + `batch.py` unmodified (git status confirms).
- [x] PASS — format/lint/pre-commit clean.
- [x] PASS — `make memory-unit-tests` 1676 passed, 0 warnings.
- [x] PASS — `make memory-integration-tests` youtube subset 15/15; only failures are proven
      pre-existing flakes outside the youtube layer.
- [ ] [HUMAN] Deferred to #082 — Prefect-UI verification. Not assessable here; left unchecked.

**Other issues found (non-blocking)**
- Leftover EMPTY throwaway DB `e2e_task080_twin` (0 docs) from the SWE's own e2e run — harmless
  namespace, no test-data pollution; not a code defect. Mine (`e2e_task080_tester`) was dropped.
- Pre-existing `except ValueError, TypeError:` in `youtube_rss._parse_published` is VALID on
  Python 3.14 (PEP 758 parenthesis-free except) and behaves correctly (bad-date → None verified).
  Explicitly out of scope per the task; left untouched. Not a defect.

**VERDICT: PASS**
