# Feature acceptance: Add YouTube ingestion (single videos + channel RSS)

Refs:
- Plan: `tracker/feature-add-youtube-ingestion-plan.md`
- Per-task tracker files: `tracker/001-…done.md` … `tracker/006-…done.md`
- Branch: `feat/add-youtube-ingestion` (7 commits ahead of `main`; merge-base `a2c041e`)
- Tester verdicts: PASS for #001–#005; PASS-WITH-USER-ACTION-REQUIRED for #006

## Log

### [PM] 2026-05-01 17:35 — Acceptance Review

**VERDICT: ACCEPT**

The feature delivers exactly what the Tasks Plan promised — Substack-shaped parity for YouTube, with a chained free→paid transcript-fetcher and clean MCP-routing semantics. Walked all six tasks from the user's perspective, spot-checked the diff (36 files, +5 374 / −3 vs. merge-base `a2c041e`), and confirmed the per-task commits map cleanly to the Plan with no scope creep.

**User-perspective walkthrough (every promise in the launch brief)**
1. **User edits `apps/memory/configs/default.yaml`.** The two example URIs land at lines 46–57 with a 6-line comment block that names the two backends, names the WARNING points, and names the hard-skip terminal state. A real user reading only `default.yaml` understands what will happen at run time. PASS.
2. **User runs `make memory-run-data-pipeline`.** `tree/data/pipeline.py` walks the typed `SourceEntry` list, batches `YouTubeRssSource` and `YouTubeVideoSource` separately, and logs `Starting YouTube RSS pipeline with N feeds` / `Starting YouTube video pipeline with N URLs` (concrete, action-oriented copy — matches the Substack messaging style). The "skip when empty" branch logs `… pipeline skipped: no <type> entries configured`. The Tester's run produced one `source_type=youtube` Document with `title`, `authors=["Paul Iusztin"]`, tz-aware `date`, and 30 KB of transcript content. PASS.
3. **User pipes a YouTube video URL through MCP `ingest_url`.** `_URL_HANDLERS` lists `youtube.com` and `youtu.be` BEFORE `substack.com`, so a stray `*.substack.com`-shaped match cannot intercept a YouTube URL. Confirmed by reading `tree/data/core/ingest.py:67-71`. PASS.
4. **User pipes a YouTube channel-feed URL through MCP `ingest_url`.** `tree/data/core/ingest.py:130-137` short-circuits with a clear `ValueError`: `"RSS feed URLs are not supported by ingest_url; configure them as 'youtube_rss' in app config."` — action-oriented, names the right config knob, no stack-trace dump. PASS.
5. **User pastes a non-canonical URL shape (`youtu.be/<id>`, `/shorts/<id>`, `/embed/<id>`, `m.youtube.com/watch?v=<id>`).** Independent walk via `extract_video_id` confirms every common shape resolves to the bare 11-char ID, and `canonical_video_url` collapses them onto a single `Document.source_uri` so dedup is stable. Output captured below.
   ```
   https://www.youtube.com/watch?v=eYaWxljC4sA   -> id='eYaWxljC4sA'   video_url=True
   https://youtu.be/eYaWxljC4sA                  -> id='eYaWxljC4sA'   video_url=True
   https://www.youtube.com/shorts/eYaWxljC4sA    -> id='eYaWxljC4sA'   video_url=True
   https://www.youtube.com/embed/eYaWxljC4sA     -> id='eYaWxljC4sA'   video_url=True
   https://m.youtube.com/watch?v=eYaWxljC4sA     -> id='eYaWxljC4sA'   video_url=True
   eYaWxljC4sA                                   -> id='eYaWxljC4sA'   video_url=False (bare id)
   https://www.youtube.com/feeds/videos.xml?channel_id=UCabc -> rss=True (correctly NOT a video URL)
   ```
   PASS.
6. **Chained-fallback narrative (free → paid → hard-skip).** Read `tree/data/youtube/transcript_fetcher.py:165-224`. The chain is slot-by-slot: each fetcher is called only on the slots its predecessor returned `None` for, and a per-slot WARNING fires naming the previous fetcher class, the canonical URL, and the next fetcher class — `YoutubeTranscriptApiFetcher returned no transcript for https://www.youtube.com/watch?v=…; falling back to GeminiTranscriptFetcher`. After the last fetcher, any remaining `None` slot gets a final `All transcript fetchers exhausted for …; skipping`. Per-fetcher modules (`transcript_fetcher.py:115-131` for the primary, `gemini_transcript_fetcher.py:105-126` for the fallback) deliberately log only at DEBUG when they return `None`, so the chain owns the user-facing WARNING without competing log lines. PASS — the spec promise ("WARNING when chain advances; final WARNING only when both fail") is honoured exactly.
7. **RSS hard-skip semantics.** `tree/data/youtube/youtube_rss_pipeline.py:94-97` short-circuits a single `None` slot with `continue` — the batch keeps going, no extra warning at the pipeline layer (the chain already warned). One missing transcript does not sink the batch. PASS.
8. **Document shape parity with Substack.** `build_document` in `youtube_video.py:87-119` populates `source_type=YOUTUBE`, `source_uri=canonical watch?v=…`, transcript-as-content, oEmbed-derived `title`/`authors`, tz-aware `date` with a `now(UTC)` fallback (project rule honoured at line 109). The `feed_entry_to_metadata` path (`youtube_rss.py:124-160`) skips the per-video oEmbed round-trip when the Atom entry already carries the same fields — sound reuse, clearly documented in the docstring. PASS.
9. **Orchestrator deployment parity.** `tree/orchestrator.py:44-51` registers the two new batch flows with `ingest-youtube-video-batch-etl` / `ingest-youtube-rss-feed-batch-etl` deployment names — same shape as the Substack batch deployments. Future `make memory-serve-workflows` runs pick them up automatically. PASS.
10. **Documentation discipline (`docs/adr/`, `docs/glossary.md`).** Project does not maintain those directories — checked: no `docs/adr/` and no `docs/glossary.md` in the worktree. Documentation discipline is therefore not in scope for this acceptance, per `docs/PROCESS.md` lines 102-104. The Plan's "Documentation updates" section is omitted accordingly.

**Spec-vs-implementation diff check**
- All six tasks committed in the order the Plan listed them (`git log --oneline a2c041e..HEAD` matches Plan items 1→6).
- `1340e05` (the chore commit between #003 and #004 in the orchestrator's timeline) is contained, justified ("drop formatter-suppression in `youtube_rss._parse_published`"), and lives entirely inside the YouTube ETL surface area.
- 36-file diff against the true merge-base contains zero unrelated edits — every file is under `apps/memory/src/tree/data/youtube/`, `apps/memory/configs/default.yaml`, `apps/memory/src/tree/{config/app_config,data/core/ingest,data/pipeline,entities/documents,orchestrator}.py`, `apps/memory/pyproject.toml`/`uv.lock`, `tracker/00{1..6}-….done.md`, or matching test directories.
- Every "Out of scope (intentional)" item from the Plan is honoured: no Webshare proxy plumbing in `__init__`'s body (only the parked extension point), no Whisper link, no MCP-tool surface change, no per-video chapter segmentation, no `transcript_languages` or `gemini_model` YAML knobs.

**`USER ACTION REQUIRED: rotate GOOGLE_API_KEY` (from #006)**
Acknowledged and treated as environmental, NOT a feature defect. The same `GOOGLE_API_KEY` powers graph extraction for every source type in the project; rotating it unblocks YouTube extraction → indexing → query-graph identically to the way it would unblock Substack/Web. Ingestion (the YouTube-shaped delta this PR ships) ran cleanly to completion and produced a correct `documents` row before extraction was attempted. Per Part 2 rules, this alone is not a REJECT trigger — and I have not found a separate feature-level defect.

**Spot-checks I considered before accepting (each documented as PASS or "Nit, not a Blocker")**
- *Stylistic Nit.* `youtube_rss._parse_published` uses Python 3.14's PEP 758 unparenthesised `except A, B:` form (commit `1340e05` deliberately dropped a `noqa`-style suppression). It parses and behaves correctly under the project's `requires-python = ">=3.14"`, but it's the only place in the codebase using that shape. The PR Reviewer / On-Call may want to fold a parenthesised form into the squash if they care; PM does not block on style. **Not a Blocker.**
- *Cosmetic Prefect Nit.* `ChainedTranscriptFetcher` is not JSON-serializable, so Prefect's content-addressed cache-key computation logs `Error encountered when computing cache key … Unable to serialize unknown type` on the YouTube fetch tasks. Tasks complete successfully (`Finished in state Completed()`); only the per-task result-cache is disabled. Reasonable follow-up: pass `cache_policy=NONE` (or a custom hash) on the YouTube `@task`s. The Tester logged the same observation. **Not a Blocker** — the user's logs still resolve, and the actual work succeeds.
- *Substring host-match Nit.* The `_URL_HANDLERS` registry uses `pattern in domain` substring matching. A pathological domain like `notyoutube.com` would technically match — but the same shape is used for Substack and the project has lived with it; expanding the match here would inflate this PR's scope. **Not a Blocker.**

**Verdict line**
"If the user checks this right now (with `GOOGLE_API_KEY` rotated to a working key), they will be satisfied." — **YES.**

SWE may push for the Push gate (the PR description should call out the `GOOGLE_API_KEY` rotation requirement; the human merger will see it before merge).
