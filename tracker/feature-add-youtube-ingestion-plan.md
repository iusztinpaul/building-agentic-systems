# Feature Plan: Add YouTube ingestion (single videos + channel RSS)

## Summary

Mirror the existing Substack ingestion surface for YouTube. Add a swappable transcript-fetcher interface with a chained fallback architecture: a free `youtube-transcript-api` primary backed by a paid Gemini 2.5 Flash fallback for videos the primary cannot transcribe (CC disabled, age-gated, region-locked). Build two specialized ETLs (single-video and channel-feed) that consume the chain transparently, wire them into the typed `SourceEntry` discriminated union + the unified data-pipeline dispatcher + the MCP `ingest_url` URL router, register Prefect deployments, add the two example URIs from the spec to `configs/default.yaml`, and verify YouTube documents flow end-to-end through the data, extraction, indexing, and MCP-query paths into the unified `knowledge_graph` collection. No new MCP-tool surface — Substack-shaped parity throughout.

The chain wrapper from task #001 emits a `WARNING` when it advances from the primary to the Gemini fallback for a given video (so users see when paid calls fire), and a final `WARNING` only if BOTH backends fail. Hard-skip semantics for RSS batches stay the same: one missing transcript (after the entire chain has been exhausted) does not sink the batch — that single video becomes a `None` slot, the chain logs the final WARNING, and the pipeline continues.

## Tasks (in order)

1. **#001** — `TranscriptFetcher` Protocol + `YoutubeTranscriptApiFetcher` primary impl + `ChainedTranscriptFetcher` composite wrapper + URL/ID helpers + tests — `tracker/001-youtube-transcript-fetcher-interface.groomed.md`. Pure logic, no Prefect, no DB. Lays the swappable seam and the chain abstraction for #003 and #004.
2. **#002** — `GeminiTranscriptFetcher`: paid Gemini 2.5 Flash fallback fetcher conforming to the `TranscriptFetcher` Protocol — `tracker/002-youtube-gemini-transcript-fetcher.groomed.md`. Pure logic, fully unit-tested with mocks (no real Gemini calls in CI). Plugs into the chain shipped in #001. Depends on #001.
3. **#003** — YouTube single-video ETL (`youtube_video.py` + `youtube_video_pipeline.py`) — `tracker/003-youtube-single-video-etl.groomed.md`. Adds `SourceType.YOUTUBE`. Default fetcher is `ChainedTranscriptFetcher([YoutubeTranscriptApiFetcher(), GeminiTranscriptFetcher()])`. Mirrors `substack_article_pipeline.py`. Depends on #001 and #002.
4. **#004** — YouTube RSS-feed ETL (`youtube_rss.py` + `youtube_rss_pipeline.py`) — `tracker/004-youtube-rss-feed-etl.groomed.md`. Reuses the same default chain via the `_default_chained_fetcher()` helper from #003. Mirrors `substack_rss_pipeline.py`. Hard-skip-and-warn (chain owns the warning; pipeline stays silent on chain-exhausted slots) on missing transcripts. Depends on #001, #002, #003.
5. **#005** — Wire `YouTubeVideoSource` + `YouTubeRssSource` into `app_config.SourceEntry`, `data/pipeline.data_pipeline`, `data/core/ingest.ingest_url`, and `tree.orchestrator.serve(...)` — `tracker/005-wire-youtube-source-entries-and-dispatcher.groomed.md`. Pure wiring. Depends on #001–#004.
6. **#006** — Add the two example URIs to `configs/default.yaml` and run the full data → extraction → indexing → MCP-query path end-to-end with evidence captured in the task log — `tracker/006-youtube-config-and-e2e-verification.groomed.md`. Depends on #001–#005.

## Out of scope (intentional)

- **Webshare-proxy / IP rotation plumbing.** v1 leaves a clean `proxy_config` extension point on `YoutubeTranscriptApiFetcher.__init__` but does not consume it. If/when free-tier IP blocks bite, a follow-up task adds the env vars (e.g. `WEBSHARE_PROXY_USERNAME`, `_PASSWORD`) and threads them through.
- **A more powerful bulk-import primary transcript backend.** The `TranscriptFetcher` Protocol exists *for* this future swap; the swap itself ships separately when we have a chosen backend (e.g. a paid bulk transcript API). It would slot in as either a replacement primary or an additional link in the chain.
- **Self-hosted Whisper as another link in the chain.** Could become a third link (post-Gemini, before final hard-skip) in a follow-up task. Not in v1 — Gemini-as-fallback covers the user need at v1.
- **MCP-tool surface changes.** No new tools, no new params on existing tools. The feature spec explicitly says "no MCP-tool surface changes needed." Existing tools (`ingest_url`, `query_memory`, …) handle YouTube transparently.
- **Per-video chapter / timestamp segmentation in the knowledge graph.** v1 stores the transcript as a single `content` string; the existing chunking pipeline treats it like any other long document. Smarter timestamp-aware chunking is a future task.
- **Surfacing `transcript_languages` or `gemini_model` as YAML-level config knobs.** Both are hard-coded at constructor time in v1 (`languages=("en",)`, `model="gemini-2.5-flash"`). Promote to a `youtube` sub-config only if a user request comes in.

## Open questions (resolved)

1. **Proxy/auth in v1?** → **Resolved — none in v1.** Keep `proxy_config` as an interface-level extension point on `YoutubeTranscriptApiFetcher.__init__` only. Document "Reserved; not consumed in v1." in the docstring. Add real plumbing only when needed.
2. **Transcript language preference?** → **Resolved — `("en",)` default**, hard-coded on `YoutubeTranscriptApiFetcher.__init__`, not surfaced in YAML config in v1. The same `en` preference is reflected in the `GeminiTranscriptFetcher` prompt. Promote to a `youtube` sub-config later if requested.
3. **Missing-transcript behaviour?** → **Resolved — warn + Gemini paid fallback** via `ChainedTranscriptFetcher`. When `youtube-transcript-api` returns `None` for a video, the chain logs a `WARNING` ("YoutubeTranscriptApiFetcher returned no transcript for {url}; falling back to GeminiTranscriptFetcher") and calls Gemini 2.5 Flash with the video URL via `Part.from_uri(file_uri=..., mime_type="video/*")`. Only after BOTH fetchers return `None` does the chain emit a final `WARNING` ("All transcript fetchers exhausted for {url}; skipping") and the slot becomes a hard-skip. Hard-skip semantics for RSS batches are unchanged: one missing transcript does not sink the batch.

These resolutions are reflected in tasks #001 (chain wrapper + warnings), #002 (Gemini fallback fetcher), #003 (default chain wired into single-video ETL), #004 (default chain wired into RSS ETL — pipeline stays silent on chain-exhausted slots; the chain owns the warning), and #006 (e2e + YAML comment block describes the chain).
