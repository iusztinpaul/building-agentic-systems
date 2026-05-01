# Wire YouTube source entries + dispatcher + URL router + Prefect deployments

Status: pending
Tags: `data`, `enhancement`, `youtube`, `config`
Depends on: #001, #002, #003, #004
Blocks: #006

## Scope

Plumb the two YouTube ETLs into every place a Substack pipeline is plumbed: the typed `SourceEntry` discriminated union, the unified `data_pipeline` dispatcher, the `tree.data.core.ingest.ingest_url` URL router, and the Prefect `orchestrator.serve(...)` call. **No new ETL code in this task** — purely wiring.

### Files to modify

1. **`apps/memory/src/tree/config/app_config.py`** — add two new variants and extend the discriminated union:

   ```python
   class YouTubeVideoSource(BaseModel):
       """A YouTube video URL (or 11-char video id)."""
       type: Literal["youtube_video"] = "youtube_video"
       uri: str = Field(min_length=1)

   class YouTubeRssSource(BaseModel):
       """A YouTube channel feed: youtube.com/feeds/videos.xml?channel_id=…"""
       type: Literal["youtube_rss"] = "youtube_rss"
       uri: str = Field(min_length=1)

   SourceEntry = Annotated[
       Union[
           SubstackRssSource,
           SubstackArticleSource,
           HuggingFaceDatasetSource,
           YouTubeVideoSource,
           YouTubeRssSource,
           WebSource,
       ],
       Field(discriminator="type"),
   ]
   ```

   Also extend `_normalize_untyped_entry` to detect bare YouTube URLs and infer the right type:
   - Host matches `youtube.com`/`youtu.be`/`m.youtube.com`/`www.youtube.com` AND path `/feeds/videos.xml` AND query has `channel_id` → `youtube_rss`.
   - Host is a YouTube host AND looks like a video URL (path is `/watch`, `/shorts/...`, or host is `youtu.be`) → `youtube_video`.
   - Otherwise falls through to existing logic. (Keep this small; the dispatcher in `data/core/ingest.py` is the primary URL classifier — `_normalize_untyped_entry` is just convenience for untyped YAML entries.)

2. **`apps/memory/src/tree/data/pipeline.py`** — add two new dispatch branches following the Substack pattern:

   ```python
   from tree.config.app_config import YouTubeRssSource, YouTubeVideoSource
   from tree.data.youtube.youtube_rss_pipeline import ingest_youtube_rss_feed_batch
   from tree.data.youtube.youtube_video_pipeline import ingest_youtube_video_batch

   # --- YouTube RSS (batched) ---
   yt_rss_entries = [s for s in sources if isinstance(s, YouTubeRssSource)]
   if yt_rss_entries:
       feed_urls = [s.uri for s in yt_rss_entries]
       logger.info("Starting YouTube RSS pipeline with %d feeds", len(feed_urls))
       yt_rss_docs = await ingest_youtube_rss_feed_batch(feed_urls)
       all_ingested.extend(yt_rss_docs)
       logger.info("YouTube RSS pipeline ingested %d documents", len(yt_rss_docs))
   else:
       logger.info("YouTube RSS pipeline skipped: no youtube_rss entries configured")

   # --- YouTube videos (batched) ---
   yt_video_entries = [s for s in sources if isinstance(s, YouTubeVideoSource)]
   # ... mirror the substack-articles block.
   ```

   Place the two new blocks immediately after the Substack-articles block, before the HuggingFace block, so the `data_pipeline` flow log reads in a sensible order.

3. **`apps/memory/src/tree/data/core/ingest.py`** — add YouTube routing to the URL dispatcher used by the MCP `ingest_url` tool:

   ```python
   async def _ingest_youtube_video(url: str) -> Document | None:
       from tree.data.youtube.youtube_video_pipeline import ingest_youtube_video
       return await ingest_youtube_video(url)

   # Note: youtube_rss is feed-shaped, not document-shaped, so it is NOT
   # routed by ingest_url (which returns a single Document or None). RSS
   # feeds enter the pipeline via the unified dispatcher (data/pipeline.py)
   # only. ingest_url stays single-document.
   ```

   Update `_URL_HANDLERS` to match YouTube hosts BEFORE the `substack.com` entry:

   ```python
   _URL_HANDLERS: list[tuple[str, Callable[[str], Awaitable[Document | None]]]] = [
       ("youtube.com", _ingest_youtube_video),
       ("youtu.be", _ingest_youtube_video),
       ("substack.com", _ingest_substack_article),
   ]
   ```

   Add a guard so `youtube.com/feeds/videos.xml?channel_id=…` URLs are rejected by `ingest_url` with a clear `ValueError`: "RSS feed URLs are not supported by ingest_url; configure them as `youtube_rss` in app config." This prevents a user from passing the wrong URL shape via the MCP tool.

4. **`apps/memory/src/tree/orchestrator.py`** — register the new flows so they're served as deployments alongside the Substack ones:

   ```python
   from tree.data.youtube.youtube_rss_pipeline import ingest_youtube_rss_feed_batch
   from tree.data.youtube.youtube_video_pipeline import ingest_youtube_video_batch

   serve(
       # … existing deployments …
       ingest_youtube_video_batch.to_deployment(
           name="ingest-youtube-video-batch-etl",
           tags=["data-pipeline", "youtube"],
       ),
       ingest_youtube_rss_feed_batch.to_deployment(
           name="ingest-youtube-rss-feed-batch-etl",
           tags=["data-pipeline", "youtube"],
       ),
   )
   ```

   These are called from `data_pipeline` directly (in-process), so registering them as deployments is for parity + ad-hoc triggering, not strictly required. Match Substack's surface to keep the orchestrator clean.

### Files to modify (tests)

5. **`apps/memory/tests/unit/config/`** (or the existing `test_app_config.py` — the SWE checks the file layout) — add unit tests:
   - YAML with `type: youtube_video` parses to `YouTubeVideoSource`.
   - YAML with `type: youtube_rss` parses to `YouTubeRssSource`.
   - Untyped entry `https://www.youtube.com/watch?v=eYaWxljC4sA` is normalized to `youtube_video`.
   - Untyped entry `https://youtu.be/eYaWxljC4sA` is normalized to `youtube_video`.
   - Untyped entry `https://www.youtube.com/feeds/videos.xml?channel_id=UC…` is normalized to `youtube_rss`.

6. **`apps/memory/tests/unit/data/test_pipeline.py`** — extend with:
   - When `app_config.sources` includes a `YouTubeRssSource`, `ingest_youtube_rss_feed_batch` is called with the right URL list.
   - When `app_config.sources` includes a `YouTubeVideoSource`, `ingest_youtube_video_batch` is called with the right URL list.
   - When neither is configured, neither function is called and a "skipped: no … entries configured" line is logged.
   - (Mirror existing Substack test patterns; reuse `mocker.patch` on the imported names in `tree.data.pipeline`.)

7. **`apps/memory/tests/unit/data/core/`** (existing test file for `ingest_url`) — extend with:
   - `await ingest_url("https://www.youtube.com/watch?v=eYaWxljC4sA")` routes to `_ingest_youtube_video` (assert via `mocker.patch`).
   - `await ingest_url("https://youtu.be/eYaWxljC4sA")` routes to `_ingest_youtube_video`.
   - `await ingest_url("https://www.youtube.com/feeds/videos.xml?channel_id=UC…")` raises `ValueError` with the documented message.

## Acceptance Criteria

- [x] `YouTubeVideoSource` and `YouTubeRssSource` Pydantic models exist in `apps/memory/src/tree/config/app_config.py` and are members of the `SourceEntry` discriminated union.
- [x] A YAML entry `{type: youtube_video, uri: https://www.youtube.com/watch?v=eYaWxljC4sA}` round-trips through `load_app_config` to a `YouTubeVideoSource` instance — verified by a unit test.
- [x] A YAML entry `{type: youtube_rss, uri: https://www.youtube.com/feeds/videos.xml?channel_id=UC…}` round-trips to a `YouTubeRssSource` — verified by a unit test.
- [x] Untyped YAML entries with a YouTube URI are normalized to the correct discriminator before validation — verified by a parametrized unit test.
- [x] `data_pipeline()` dispatches `YouTubeRssSource` entries to `ingest_youtube_rss_feed_batch` and `YouTubeVideoSource` entries to `ingest_youtube_video_batch` — verified by unit tests with `mocker.patch`.
- [x] When neither variant is configured, both YouTube branches are skipped with the documented log lines — verified by a unit test.
- [x] `ingest_url("https://www.youtube.com/watch?v=…")` and `ingest_url("https://youtu.be/…")` route through the new YouTube handler — verified by unit tests.
- [x] `ingest_url("https://www.youtube.com/feeds/videos.xml?channel_id=…")` raises `ValueError` with a message that mentions `youtube_rss` — verified by a unit test.
- [x] `tree.orchestrator.__main__` (i.e. `make memory-serve-workflows`) imports cleanly with the two new deployments registered (no ImportError, no Prefect registration error) — verified by running the script for a few seconds and confirming `ingest-youtube-video-batch-etl` and `ingest-youtube-rss-feed-batch-etl` appear in `uv run prefect deployment ls` output.
- [x] `make memory-unit-tests` is green with zero new warnings.
- [x] `make memory-format-fix && make memory-lint-fix && make memory-format-check && make memory-lint-check && make pre-commit` clean.
- [x] No regression: the Substack and HuggingFace dispatch branches still pass their existing tests untouched.

## User Stories

### Story: User adds two YouTube entries to YAML and runs the unified pipeline
1. User edits `apps/memory/configs/default.yaml` (in #005) and adds `{type: youtube_rss, uri: …}` and `{type: youtube_video, uri: …}` entries.
2. User runs `make memory-run-data-pipeline`.
3. Logs show: `Starting YouTube RSS pipeline with 1 feeds`, `Starting YouTube video pipeline with 1 URLs`, alongside the existing Substack/HF/Web lines.
4. `db.documents.countDocuments({source_type: "youtube"})` increases by the expected number.

### Story: User pastes a YouTube URL into the MCP `ingest_url` tool
1. User invokes `ingest_url("https://www.youtube.com/watch?v=eYaWxljC4sA")` via the MCP server.
2. The dispatcher matches `youtube.com` and routes to `ingest_youtube_video`.
3. The single-video flow runs and persists the Document. The user gets back the Document id (or `None` for duplicates), exactly like Substack.

### Story: User pastes the channel-feed URL into the MCP `ingest_url` tool by mistake
1. User invokes `ingest_url("https://www.youtube.com/feeds/videos.xml?channel_id=UCkyHDwRWMEluOEYmOGJ_2nw")`.
2. The dispatcher raises `ValueError: RSS feed URLs are not supported by ingest_url; configure them as 'youtube_rss' in app config.`
3. User sees the error message via the MCP tool response, knows what to do, edits `default.yaml` instead.

### Story: User omits `type:` on a YouTube YAML entry — config still parses
1. User writes `- uri: https://www.youtube.com/watch?v=eYaWxljC4sA` with no `type:`.
2. `load_app_config()` infers `youtube_video` and validates successfully.
3. The entry is dispatched correctly when the unified pipeline runs.

### Story: SWE inspects served Prefect deployments after wiring
1. SWE runs `make memory-serve-workflows` in one terminal, then `uv --directory apps/memory run prefect deployment ls` in another.
2. Output includes `ingest-youtube-video-batch-etl/ingest-youtube-video-batch-etl` and `ingest-youtube-rss-feed-batch-etl/ingest-youtube-rss-feed-batch-etl`.
3. SWE sees Substack, HuggingFace, web, and YouTube deployments side by side — the system surface is symmetric.

---

Blocked by: #001, #002, #003, #004

## Log

### [SWE] 2026-05-01 18:38 — Implementation

**Files modified**
- `apps/memory/src/tree/config/app_config.py` — added `YouTubeVideoSource`/`YouTubeRssSource` discriminated-union variants; added `_YOUTUBE_HOSTS` + `_is_youtube_host` helpers; extended `_normalize_untyped_entry` to infer `youtube_rss` (path `/feeds/videos.xml` + `channel_id` query) and `youtube_video` (host `youtu.be`, path `/watch`, or path starting with `/shorts/`) before falling through to existing Substack/web logic.
- `apps/memory/src/tree/data/pipeline.py` — imported the two YouTube batch flows and added two new dispatch branches between the Substack-articles block and the HuggingFace block, mirroring the Substack-RSS shape (single batch call, log skip-line when no entries).
- `apps/memory/src/tree/data/core/ingest.py` — added `_ingest_youtube_video` lazy-import handler; prepended `("youtube.com", _ingest_youtube_video)` and `("youtu.be", _ingest_youtube_video)` to `_URL_HANDLERS` so YouTube wins over the substack-com substring check; added an explicit `ValueError` guard inside `ingest_url` for `youtube.com/feeds/videos.xml`-shaped URLs whose error message contains the literal `youtube_rss` token.
- `apps/memory/src/tree/orchestrator.py` — registered `ingest_youtube_video_batch` (deployment name `ingest-youtube-video-batch-etl`) and `ingest_youtube_rss_feed_batch` (`ingest-youtube-rss-feed-batch-etl`) inside the existing `serve(...)` call, both tagged `["data-pipeline", "youtube"]` to mirror Substack/conversation/file deployments.
- `apps/memory/tests/unit/config/test_sources_config.py` — extended import list, added `test_youtube_video_validates`/`test_youtube_rss_validates`, parametrized `test_missing_uri_raises_validation_error` over the two new types, added a parametrized `test_untyped_entry_with_youtube_video_url_normalizes_to_youtube_video` covering all four YouTube hosts plus `/shorts/`, added `test_untyped_entry_with_youtube_rss_feed_normalizes_to_youtube_rss`, extended the YAML round-trip type-tuple to accept the new variants.
- `apps/memory/tests/unit/data/test_pipeline.py` — extended import list, added `test_dispatches_youtube_rss_entries`, `test_dispatches_youtube_video_entries`, and `test_skips_youtube_branches_when_absent` (also asserts the documented skip log lines via `caplog`). Mocks `ingest_youtube_rss_feed_batch`/`ingest_youtube_video_batch` on `tree.data.pipeline` namespace.
- `apps/memory/tests/unit/data/core/test_ingest.py` — added `test_routes_youtube_video_url` (parametrized over `youtube.com`/`m.youtube.com`/`youtube.com`/`youtu.be`) that patches `_URL_HANDLERS` wholesale (registry captures handler refs at module load), and `test_rejects_youtube_rss_feed_url` (parametrized over feed URLs on three YouTube hosts) — asserts `ValueError` mentions `youtube_rss`.

**Tests**
- Unit: 567 passing, 0 failing — `make unit-tests` (full suite). New tests added: 4 in `test_sources_config.py` (incl. parametrized expansions), 3 in `test_pipeline.py`, 2 parametrized blocks (7 cases) in `test_ingest.py`.
- Integration: N/A — no infra changes; pure wiring, no new ETL/IO.

**Acceptance criteria**
- [x] `YouTubeVideoSource` and `YouTubeRssSource` Pydantic models exist in `tree.config.app_config` and are members of `SourceEntry` — `tests/unit/config/test_sources_config.py::TestVariantValidation::test_youtube_video_validates`, `::test_youtube_rss_validates`.
- [x] `{type: youtube_video, uri: …}` round-trips to `YouTubeVideoSource` — `test_youtube_video_validates`.
- [x] `{type: youtube_rss, uri: …}` round-trips to `YouTubeRssSource` — `test_youtube_rss_validates`.
- [x] Untyped YouTube entries are normalized to the right discriminator — `test_untyped_entry_with_youtube_video_url_normalizes_to_youtube_video` (parametrized over 5 URL shapes incl. `/shorts/`) and `test_untyped_entry_with_youtube_rss_feed_normalizes_to_youtube_rss`.
- [x] `data_pipeline()` dispatches both YouTube variants to the correct batch flows — `tests/unit/data/test_pipeline.py::TestDataPipeline::test_dispatches_youtube_rss_entries`, `::test_dispatches_youtube_video_entries`.
- [x] When neither variant is configured, both branches are skipped with the documented log lines — `::test_skips_youtube_branches_when_absent` (asserts both `YouTube RSS pipeline skipped: no youtube_rss entries configured` and `YouTube video pipeline skipped: no youtube_video entries configured` via `caplog`).
- [x] `ingest_url(youtube watch / youtu.be)` routes to YouTube handler — `tests/unit/data/core/test_ingest.py::TestIngestUrl::test_routes_youtube_video_url` (parametrized).
- [x] `ingest_url(feed URL)` raises `ValueError` mentioning `youtube_rss` — `::test_rejects_youtube_rss_feed_url` (parametrized over three YouTube hosts).
- [x] `tree.orchestrator.__main__` imports cleanly and registers both new deployments — verified by running the orchestrator for ~7s and listing deployments via `uv run prefect deployment ls` (see Evidence below).
- [x] `make memory-unit-tests` green with zero new warnings — 567 passed in 20.30s.
- [x] format-fix / lint-fix / format-check / lint-check / pre-commit all clean — see Evidence.
- [x] No regression: Substack and HuggingFace branches keep passing — confirmed by the full unit suite (567/567) and existing tests being untouched.

**Evidence**

```
$ make format-fix && make lint-fix
uv run ruff format src/ tests/ scripts/ deploy/
2 files reformatted, 158 files left unchanged
uv run ruff check --fix src/ tests/ scripts/ deploy/
All checks passed!

$ make format-check && make lint-check
uv run ruff format --check src/ tests/ scripts/ deploy/
160 files already formatted
uv run ruff check src/ tests/ scripts/ deploy/
All checks passed!

$ make pre-commit  (from repo root)
uv run --project apps/memory pre-commit run --all-files
Validate pyproject.toml..............................(no files to check)Skipped
prettier.................................................................Passed
ruff check...............................................................Passed
ruff format..............................................................Passed
biome check (harness)....................................................Passed

$ make unit-tests
... (truncated) ...
============================= 567 passed in 20.30s =============================
```

E2E orchestrator smoke (deployments registered):

```
$ uv run python -m tree.orchestrator   # served for ~7s
Deployments
┌─────────────────────────────────────────────────────────────────────┐
│ data-pipeline-etl/data-pipeline-etl                                 │
│ memory-extraction-etl/memory-extraction-etl                         │
│ memory-indexing-etl/memory-indexing-etl                             │
│ ingest-file-etl/ingest-file-etl                                     │
│ ingest-conversation-etl/ingest-conversation-etl                     │
│ ingest-youtube-video-batch-etl/ingest-youtube-video-batch-etl       │
│ ingest-youtube-rss-feed-batch-etl/ingest-youtube-rss-feed-batch-etl │
└─────────────────────────────────────────────────────────────────────┘

$ uv run prefect deployment ls   # while served
... ingest-youtube-rss-feed-batc… │ 09df28e4-... │
... ingest-youtube-video-batch-e… │ 03fd606e-... │
```

Feed-URL guard + normalization smoke:

```
$ uv run python -c "...ingest_url(youtube_feed_url)..."
GUARD WORKS: RSS feed URLs are not supported by ingest_url; configure them as 'youtube_rss' in app config.
YouTubeVideoSource -> https://www.youtube.com/watch?v=eYaWxljC4sA
YouTubeVideoSource -> https://youtu.be/eYaWxljC4sA
YouTubeRssSource  -> https://www.youtube.com/feeds/videos.xml?channel_id=UC1
YouTubeVideoSource -> https://www.youtube.com/watch?v=ABC
```

**Notes**
- Pure wiring task per spec — no new ETL business logic. Per-flow logic is covered by tests added in #003 / #004.
- Did NOT register the per-flow `ingest_youtube_video` / `ingest_youtube_rss_feed` (single-doc) flows as deployments — only the `_batch` variants, mirroring how Substack registers `ingest-substack-rss-feed-batch-etl` rather than the single-feed flow. The single-doc YouTube flow is reachable via the `ingest_url` MCP tool and via `data_pipeline`'s in-process call to the batch flow.
- `_URL_HANDLERS` order matters: YouTube is listed before `substack.com`. The reference is captured at module load, so tests patch the list itself (mirroring `test_routes_substack_url`'s pattern).
- The feed-URL guard is checked inside `ingest_url` (after the empty/scheme/host validation) rather than inside `_URL_HANDLERS`, so the error fires before any handler is dispatched — keeps the registry shape symmetric with Substack.
- Did not commit — Tester goes first.

### [Tester] 2026-05-01 18:42 — QA

**Test summary**
- Format / lint / pre-commit: PASS (`make format-check`, `make lint-check`, `make pre-commit` all clean — 160 files already formatted, all ruff checks passed, prettier/biome/ruff hooks passed).
- Unit tests: 567 passed / 0 failed (`make unit-tests`, 22.07s, 0 warnings).
- Integration tests: 78 passed / 9 skipped / 0 failed (`make integration-tests`, 84.21s, 0 warnings). Skips are pre-existing (Bright Data SERP zone unset etc.) and unrelated to this task.

**E2E adversarial pass**
- Happy path A — typed YAML round-trip: `load_app_config(<yaml with type: youtube_video and type: youtube_rss>)` → `[YouTubeVideoSource, YouTubeRssSource]`. PASS.
- Happy path B — mixed dispatch: `data_pipeline()` over a 6-variant `SourcesConfig` (substack_rss, substack_article, huggingface_dataset, web, youtube_video, youtube_rss) → every batch ingest mock awaited exactly once with the correct entries; `result` length 6. PASS.
  Evidence: `rss_sub: [call(['https://example.com/feed'])]`, `art_sub: [call(['https://example.com/p/x'])]`, `arxiv: [call(max_samples=3, fetch_content=False)]`, `iurl: [call('https://anthropic.com/post')]`, `yt_rss: [call(['https://www.youtube.com/feeds/videos.xml?channel_id=UC_test'])]`, `yt_vid: [call(['https://www.youtube.com/watch?v=AAA'])]`.
- Break path 1 (boundary: untyped bare watch URL) — `{uri: https://www.youtube.com/watch?v=...}` → `YouTubeVideoSource` (NOT WebSource). PASS.
- Break path 2 (boundary: untyped bare feed URL) — `{uri: https://www.youtube.com/feeds/videos.xml?channel_id=UC...}` → `YouTubeRssSource`. PASS.
- Break path 3 (boundary: short `youtu.be/<id>`) — normalizes to `YouTubeVideoSource`. PASS.
- Break path 4 (boundary: `/shorts/<id>`) — normalizes to `YouTubeVideoSource`. PASS.
- Break path 5 (boundary: `m.youtube.com/watch`) — normalizes to `YouTubeVideoSource`. PASS.
- Break path 6 (state edge: feed URL via single-doc `ingest_url`) — `await ingest_url("https://www.youtube.com/feeds/videos.xml?channel_id=...")` raises `ValueError: RSS feed URLs are not supported by ingest_url; configure them as 'youtube_rss' in app config.` (mentions `youtube_rss`). Also confirmed for `m.youtube.com` host. PASS.
- Break path 7 (state edge: watch URL via `ingest_url`) — patched `_URL_HANDLERS` with mocked YouTube/Substack/web handlers; calling `ingest_url("https://www.youtube.com/watch?v=eYaWxljC4sA")` and `ingest_url("https://youtu.be/eYaWxljC4sA")` only awaits the YouTube handler (substack and web mocks zero awaits). PASS.
- Break path 8 (malformed: empty `uri` on typed `youtube_video`) — Pydantic raises ValidationError as expected. PASS.
- Break path 9 (malformed: `youtube.com/feeds/videos.xml` without `channel_id`) — falls through to `WebSource` (does NOT silently misclassify as `youtube_rss`). Reasonable per spec (rule explicitly requires `channel_id=` token). PASS.
- Break path 10 (malformed: `youtube.com/playlist?list=...`) — falls through to `WebSource` (not a recognized video shape). PASS.
- Break path 11 (large input: 100 untyped `watch?v=...` entries) — all 100 normalized to `YouTubeVideoSource`; no perf regression. PASS.
- Break path 12 (orchestrator deployment registration) — ran `uv run python -m tree.orchestrator` for ~12s with shared infra running, then `uv run prefect deployment ls` from a parallel shell. Output included `ingest-youtube-rss-feed-batch-etl/...` (id 09df28e4) and `ingest-youtube-video-batch-etl/...` (id 03fd606e) alongside the substack/file/conversation deployments. No ImportError, no Prefect registration error. PASS.

**Acceptance criteria**
- [x] PASS — `YouTubeVideoSource` and `YouTubeRssSource` exist in `tree.config.app_config` and are members of `SourceEntry`. Evidence: `apps/memory/src/tree/config/app_config.py:90-121`; `tests/unit/config/test_sources_config.py::TestVariantValidation::test_youtube_video_validates`, `::test_youtube_rss_validates` pass.
- [x] PASS — `{type: youtube_video, uri: ...}` round-trips to `YouTubeVideoSource`. Evidence: `test_youtube_video_validates` (test_sources_config.py:67-81). Also covered by `test_yaml_round_trip_typed_and_untyped_mix`.
- [x] PASS — `{type: youtube_rss, uri: ...}` round-trips to `YouTubeRssSource`. Evidence: `test_youtube_rss_validates` (test_sources_config.py:83-100).
- [x] PASS — Untyped YouTube YAML entries normalize to the right discriminator. Evidence: parametrized `test_untyped_entry_with_youtube_video_url_normalizes_to_youtube_video` covers 5 URL shapes (watch, youtube.com/watch, m.youtube.com/watch, youtu.be, shorts) and `test_untyped_entry_with_youtube_rss_feed_normalizes_to_youtube_rss`. Also re-verified via direct adversarial run.
- [x] PASS — `data_pipeline()` dispatches `YouTubeRssSource` → `ingest_youtube_rss_feed_batch` and `YouTubeVideoSource` → `ingest_youtube_video_batch`. Evidence: `tests/unit/data/test_pipeline.py::TestDataPipeline::test_dispatches_youtube_rss_entries` and `::test_dispatches_youtube_video_entries`. Also reconfirmed by mixed-dispatch adversarial run with all 6 source types.
- [x] PASS — Both YouTube branches log the documented skip line when no entries are configured. Evidence: `::test_skips_youtube_branches_when_absent` asserts both `YouTube RSS pipeline skipped: no youtube_rss entries configured` and `YouTube video pipeline skipped: no youtube_video entries configured` via `caplog`.
- [x] PASS — `ingest_url("https://www.youtube.com/watch?v=...")` and `ingest_url("https://youtu.be/...")` route to the YouTube handler. Evidence: parametrized `tests/unit/data/core/test_ingest.py::TestIngestUrl::test_routes_youtube_video_url` (4 hosts). Reconfirmed via direct adversarial run with mocked handlers.
- [x] PASS — `ingest_url("https://www.youtube.com/feeds/videos.xml?channel_id=...")` raises `ValueError` mentioning `youtube_rss`. Evidence: parametrized `::test_rejects_youtube_rss_feed_url` over 3 YouTube hosts. Direct adversarial: `ValueError: RSS feed URLs are not supported by ingest_url; configure them as 'youtube_rss' in app config.`
- [x] PASS — `tree.orchestrator` imports cleanly and registers the two new deployments. Evidence: served orchestrator for ~12s; `prefect deployment ls` shows `ingest-youtube-rss-feed-batch-etl` (09df28e4-...) and `ingest-youtube-video-batch-etl` (03fd606e-...).
- [x] PASS — `make memory-unit-tests` is green with zero new warnings. Evidence: `567 passed in 22.07s` (no warnings line).
- [x] PASS — format-fix / lint-fix / format-check / lint-check / pre-commit clean. Evidence: `160 files already formatted`, `All checks passed!`, all pre-commit hooks Passed.
- [x] PASS — No regression: substack and HuggingFace branches still pass. Evidence: full unit suite (567/567) and full integration suite (78 passed, 9 skipped pre-existing) green; existing substack/HF tests untouched (only additions).

**Evidence**

```
$ make format-check && make lint-check
uv run ruff format --check src/ tests/ scripts/ deploy/
160 files already formatted
uv run ruff check src/ tests/ scripts/ deploy/
All checks passed!

$ make pre-commit  (from repo root)
Validate pyproject.toml..............................(no files to check)Skipped
prettier.................................................................Passed
ruff check...............................................................Passed
ruff format..............................................................Passed
biome check (harness)....................................................Passed

$ make unit-tests
============================= 567 passed in 22.07s =============================

$ make integration-tests
=================== 78 passed, 9 skipped in 84.21s (0:01:24) ===================

$ uv run python -m tree.orchestrator &  (then) uv run prefect deployment ls
│ ingest-youtube-rss-feed-batc… │ 09df28e4-6344-4e89-972d-81a… │
│ ingest-youtube-video-batch-e… │ 03fd606e-84c9-4585-bba6-40f… │
```

**Other issues found**
- None. Pure-wiring task; implementation matches the spec exactly. The `_normalize_untyped_entry` rules are conservative (require literal `channel_id=` token in feed query, require trailing `/` in `/shorts/`) — falls through cleanly to `WebSource` for ambiguous cases, which is the right call.
- Minor observation (not a Blocker, not a Nit — just for awareness): `/shorts` without trailing slash falls through to `WebSource`. Spec explicitly says `/shorts/...`, so the implementation is correct; the only edge is if a user copies a malformed URL. No action required.

**VERDICT: PASS**
