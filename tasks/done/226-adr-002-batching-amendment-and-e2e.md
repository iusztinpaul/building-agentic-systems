---
id: 226-adr-002-batching-amendment-and-e2e
status: done
feature: data-batching
---

# Amend ADR-002 for uniform `data.batch_size` chunks and verify chunking end-to-end

Tags: `memory`, `data`, `docs`, `adr`, `e2e`
Depends on: 223, 224, 225
Blocks: —
Implements: ADR-002 amendment #078–#082 (refined).

## Problem

ADR-002's batch-grain amendment defines a Batch as "HuggingFace `batch_size`-chunk / one feed's
entries / the whole handed-in URL list" and promises a "small constant" task-run count per
shard. After tasks 223–225 a Batch is a `data.batch_size` chunk on every platform, in-flight I/O
is capped by `data.concurrency`, and task runs scale as phases × ⌈N / batch_size⌉.

## Scope

1. Append an amendment to `docs/adrs/002_pipeline_concurrency_and_voyage_rate_limiting.md`
   (Status stays `Accepted`, same precedent as #078–#082): Batch = `data.batch_size` (500) chunk
   everywhere; `gather_isolated` caps in-flight work at `data.concurrency` (50); the task-run
   count is ≈ phases × ⌈N / 500⌉ (good: "100,000 Substack items → 200 chunks → ~400 task runs";
   bad: "scales well"); the YouTube Bright Data collection is per chunk, with the timing measured
   in task 224.
2. Grep `docs/` and `apps/memory` READMEs for the old wording and fix it. Known stale spots
   (flagged by task 225's SWE + Tester): `docs/glossary.md:11` ("Batch" still describes the
   per-source HF `batch_size` / `concurrency`) and
   `docs/adrs/003_source_definitions_as_operator_data.md:142` (HF tuning fields). Note in the
   ADR-002 amendment that Substack still holds every resolved RSS `Document` in RAM before
   chunking (feed content arrives whole), and that `HuggingFaceDatasetSource` now has
   `extra="forbid"` — so do not deploy while a nightly/backfill run is mid-flight (a worker
   run created by the old coordinator would fail `extra_forbidden`).
3. E2E via the `run-pipelines-e2e` skill on the local env, with
   `TREE_DATA__BATCH_SIZE=3 TREE_DATA__CONCURRENCY=2` and a sources file holding ≥7 items of
   Substack and of Web (and ≥4 YouTube videos) — so chunking executes without paying for 500+ items.
   The worker must run THIS branch's code: task 225 found the Docker `tree-prefect-worker`
   running main-branch code, so rebuild it from this branch or serve the workflows from this
   branch per the skill.

## Acceptance criteria

- [x] ADR-002 amendment present with the numbers above; no doc still describes "the whole
      handed-in URL list" as the batch.
- [x] E2E run: Prefect shows `extract-web-batch` / `load-web-batch` / `load-substack-batch` /
      `load-youtube-batch` runs matching ⌈items / 3⌉ per platform; worker logs show ≤2 in-flight
      requests; Mongo document counts equal the sources' item counts (minus logged failures).
      Record commands, run ids and counts in the log.
- [x] Re-running the same e2e inserts 0 new documents (idempotency).
- [x] Cadence green (memory targets, local env).

## Log

### [Orchestrator] 2026-10-28 — Filed

### [SWE] 2026-10-28 13:35 — Implementation

**Files modified**
- `docs/adrs/002_pipeline_concurrency_and_voyage_rate_limiting.md` — new **Amendment (data-batching, tasks 223–226)** after the `num_shards` one (Status stays `Accepted`): Batch = one `data.batch_size` (500) chunk everywhere, the `data.concurrency` (50) in-flight cap, task runs ≈ phases × ⌈N / 500⌉ (good/bad examples), one Bright Data collection per chunk with this e2e's timings, the Substack-RSS-in-RAM gap, the `extra="forbid"` deploy caveat. Stale in-place wording reworded: the #078–#082 Batch triplet (L425), `fetch_many(all_urls)` per feed/batch (L484), "small constant number of task runs regardless of shard size" (L575), "task-run count stays a small constant per shard" (L619).
- `docs/glossary.md` — **Batch** row only: one `data.batch_size` chunk on every platform, `data.concurrency` cap, the 100,000 → 200 chunks → ~400 task runs example. No term added.
- `docs/adrs/003_source_definitions_as_operator_data.md` — L142: HF tuning fields are `max_samples`/`fetch_content`/`num_workers`; chunk size / in-flight cap come from `data:`.
- No code change. `apps/memory/README.md` was already fixed by task 225.

**Tests**
- Unit: 5246 passing, 0 failing — `make memory-tests` (local env).
- Integration: N/A — the project has no integration suite.

**Acceptance criteria**
- [x] ADR-002 amendment present with the numbers; no doc describes "the whole handed-in URL list" as the batch — grep below.
- [x] E2E task-run counts match ⌈items / 3⌉, ≤2 in flight, Mongo counts = items minus logged failures — evidence below.
- [x] Re-run inserts 0 new documents — evidence below.
- [x] Cadence green (local env).

**E2E setup** (LOCAL env; this branch at `31a8a45` + this doc diff)
- `docker stop tree-prefect-worker` (it runs main-branch code baked into its image), then served from this checkout:
  `make memory-serve-workflows GOOGLE_API_KEY= TREE_DATA__BATCH_SIZE=3 TREE_DATA__CONCURRENCY=2 PREFECT_LOGGING_EXTRA_LOGGERS=tree PREFECT_LOGGING_ROOT_LEVEL=INFO`
  These are make command-line vars, so they win over the Makefile's `include .env`. Checked on the live serve pid with `ps eww`: `GOOGLE_API_KEY=` (empty, so a Bright Data miss turns into an `ingest_error` row, never a Gemini call), `TREE_DATA__BATCH_SIZE=3`, `TREE_DATA__CONCURRENCY=2`. The two logging vars are needed to see the module INFO lines: Prefect's root logger defaults to WARNING in a flow-run subprocess, so the first serve (without them) logged no "chunk processed" lines.
- Scratch sources (not committed) are in `/private/tmp/claude-501/-Users-pauliusztin-Documents-01-Projects-AI-Engineer-Handbook-building-agentic-systems-building-agentic-systems/b800c6fd-2742-4e67-a758-9a5dfc53cf28/scratchpad/e2e226/`: `substack_only.yaml` (simonw.substack.com/feed = 20 RSS items, plus 2 interconnects.ai `substack_article`s), `web_youtube.yaml` (7 en.wikipedia.org `web` URLs, plus 4 new `youtube_video`s: IHZwWFHWa-w, Ilg3gGewQ5U, tIeHLnjs5U8, wjZofJX0v4M), `all.yaml` (both), `hf.yaml` (arXiv `max_samples: 7`).
- User `6ad34350d22e8f7ce231b861` (pauliusztin@decodingai.com). None of these URIs were in `documents` beforehand (checked with mongosh).
- Afterwards: serve stopped, `docker start tree-prefect-worker`, so infra is back as I found it.

**Evidence: Prefect run trees** (task-run counts per leaf flow, read via the Prefect client; all states Completed)
```
Run 1: make memory-run-data-pipeline SOURCE_FILE=<scratch>/substack_only.yaml
offline-pipeline 3ab5776d-349f-4efb-8f2c-4da606a4039f → data-etl-worker 7ccc389f-8305-47f1-8bd6-80a98d552eda
  ingest-substack-batch-etl ff616496-1c34-4c1e-8d90-280672da3229
    fetch-substack-rss-feed: 1   extract-substack-article-batch: 2   load-substack-batch: 8   # 22 items → ⌈22/3⌉ = 8
                                 (the 2 articles are items 21-22, so they land in chunks 7 and 8; RSS-only chunks 1-6 make no scrape)

Run 2: make memory-run-data-pipeline SOURCE_FILE=<scratch>/web_youtube.yaml        (billable)
offline-pipeline 4a322881-81d8-42f9-b90f-ab2197827d10   "data: shards=2 succeeded=2 failed=0 items_failed=0"
  data-etl-worker 49fb44be-…  ingest-web-url-batch-etl 3aad5cae-a6ec-4af7-98c2-4cbe4f552d1a
    extract-web-batch: 3   load-web-batch: 3                                    # 7 → ⌈7/3⌉ = 3
  data-etl-worker 3d053869-…  ingest-youtube-batch-etl ad6bf6a1-eab6-41a9-8cb3-fb00264cf060
    fetch-youtube-transcripts-batch: 2   load-youtube-batch: 2                  # 4 → ⌈4/3⌉ = 2

Run 3 (re-run): make memory-run-data-pipeline SOURCE_FILE=<scratch>/all.yaml        (billable)
offline-pipeline 54eef0e4-8ed8-48e9-bddb-9faca6895e45   "data: shards=3 succeeded=3 failed=0 items_failed=0"
  ingest-web-url-batch-etl 60d304e7-…    extract-web-batch: 3   load-web-batch: 3
  ingest-youtube-batch-etl c151635d-…    fetch-youtube-transcripts-batch: 2   load-youtube-batch: 2
  ingest-substack-batch-etl c7bb8dfa-…   fetch-substack-rss-feed: 1   extract-substack-article-batch: 2   load-substack-batch: 8

Run 4 (free; HF params check): make memory-run-data-pipeline SOURCE_FILE=<scratch>/hf.yaml
offline-pipeline 7ae4df51-08fa-481f-98e6-a9670566ea3d → ingest-arxiv-dataset-etl 9d8a90be-…
  params entry = {'type': 'huggingface_dataset', 'uri': 'librarian-bots/arxiv-metadata-snapshot', 'max_samples': 7,
                  'fetch_content': False, 'num_workers': 1, 'offset': None}   # no batch_size / concurrency
  load-arxiv-batch: 3                                                          # 7 → 3; 0 new (rows 0-6 were already ingested)
```
The main-branch code would have run 1 `extract-web-batch` and 1 `load-web-batch` for the 7 URLs. Seeing 3 + 3 here (and 8 substack loads) shows this branch's code ran.

**Evidence: ≤2 in flight** (run 2, from the serve-process log)
```
15:21:21.089 web_unlocker - Fetching URL via Bright Data Web Unlocker: …/Knowledge_graph
15:21:21.093 web_unlocker - Fetching URL via Bright Data Web Unlocker: …/Ontology_(information_science)
15:21:25.798 web_unlocker - Fetching URL via Bright Data Web Unlocker: …/Retrieval-augmented_generation   # 3rd waits for a slot
15:21:34.703 web_pipeline - Web chunk processed: 3 ingested so far
15:21:34.705 / 15:21:34.709 Vector_database, Extract,_transform,_load  → 15:21:42.801 MongoDB             # same 2-then-1 pattern
15:21:49.739 web_pipeline - Web chunk processed: 6 ingested so far
15:22:00.807 web_pipeline - Web chunk processed: 7 ingested so far
15:21:19.319 / 19.324 / 19.610 / 19.725  youtube - Fetching oEmbed metadata ×4                            # pairs
```
I also sampled `lsof -iTCP@api.brightdata.com:443 -sTCP:ESTABLISHED` every 0.25 s during run 2 (124 non-empty samples). Web worker pid 30024: max 2 connections. YouTube worker pid 30023 (collection polling): max 1. One sample showed 3 in total, which is 1 + 2 across the two worker processes. Run 3 shows the same 2-then-1 Unlocker pattern (15:26:53.531 / .536 → 15:26:55.922).

**Evidence: Mongo counts** (`documents`, this user; mongosh script `<scratch>/counts.js`)
```
                         before   after run 1   after run 2   after run 3 (re-run)
substack (simonw feed)      0         20            20            20
substack (2 articles)       0          2             2             2
web (7 wikipedia)           0          0             7             7
youtube (4 new videos)      0          0             4             4
  of which ingest_error     0          0             1             0
byType: substack 86→108→108→108 · web 0→0→7→7 · youtube 1→1→5→5 · huggingface 14→14→14→14(run 4)
```
- Run 2, YouTube: the 3-video Bright Data snapshot `sd_mvs57vb2wujn7vtfk` returned 2 records. That gave `Recorded ingest failure: …tIeHLnjs5U8 (no_transcript: brightdata returned empty; gemini not configured)`, so 3 docs + 1 logged failure = 4 items. Web: 7/7. Substack: 22/22.
- Run 3 (re-run): `Substack: ingested 0 items`, `Ingested 0 web URLs out of 7`, and `YouTube: ingested 1 items`. The 1 is `Replaced errored document: …tIeHLnjs5U8`: this time Bright Data returned that transcript, so the existing `ingest_error` row was replaced IN PLACE (the designed `(user_id, source_uri)` replace path). Row counts did not change, so 0 new documents.
- The replaced row keeps a single `_id` (`ObjectId('6ae1f75c8011c8f3665ac747')`, now `ingest_error: null`): `load_video_document` sets `doc.id = existing.id` before `replace()` (`tree/data/youtube/youtube.py:458`), and only one row has that `source_uri`.
- `latent` reference rows grew 2989 → 4879 in run 1 (links extracted from the simonw posts); they did not change in runs 2–3.

**Evidence: Bright Data collection timings** (these go into the ADR amendment)
```
run 2: 3-video sd_mvs57vb2wujn7vtfk 15:21:20.167 → 15:23:38.549 = 138 s;  1-video sd_mvs5avn41pym88y2kn 15:23:40.545 → 15:25:59.339 = 139 s
run 3: 3-video sd_mvs5f2vp1w11am3zrw 15:26:56.522 → 15:28:12.379 =  76 s;  1-video sd_mvs5gqu851phxjccd 15:28:14.119 → 15:29:29.834 =  76 s
```

**Evidence: cadence and grep**
```
$ make env-status                → Env target: local (.env)
$ make memory-format-check       → 338 files already formatted
$ make memory-lint-check         → All checks passed!
$ make pre-commit                → prettier / ruff check / ruff format / biome: Passed
$ make memory-tests              → 5246 passed in 65.18s
$ grep -rniE "one isolated (load )?gather|single isolated gather|whole handed-in|handed-in URL list|small constant task-run|small constant number|constant per shard|whole URL list|ONCE over|over ALL items|one feed's entries" docs apps/memory/README.md apps/memory/src/tree/data
docs/adrs/002_…:643:   Batch platform-specific (a HuggingFace chunk, one feed's entries, or every URL the   ← the new amendment describing the OLD state
apps/memory/src/tree/data/web/web_pipeline.py:4: … The batch flow walks the handed-in URL list in   ← continues "…in batch_size chunks"; accurate
$ grep -rn "batch_size\|handed-in" README.md apps/memory/deploy apps/memory/scripts
(no output)
```

**Notes**
- Billable spend: 14 Web Unlocker requests (7 per run, none needed the markdown fallback) and 4 Bright Data dataset collections (8 video records). Gemini: 0 calls, because `GOOGLE_API_KEY` was empty in the serve process.
- Re-runs re-bill (pre-existing behaviour, not a 223–225 bug). The Web and YouTube batch paths dedup only at `load_*`, after the scrape or collection, so an idempotency re-run pays again for every URL and video. A pre-fetch `source_uri` filter would save it; worth a separate task if the nightly cron re-lists already-ingested URLs.
- I did not cite a 500-video timing: task 224's `[HUMAN]` measurement was not run. The amendment says 600 s is unverified at full chunk size and gives only the 3-video and 1-video numbers above.
- Run 4 (HF) logged a `huggingface_hub` WARNING, `[Errno 9] Bad file descriptor`, on a parquet GET after `Finished streaming`. It happened during streaming teardown, the run Completed, and it is unrelated to chunking.
- Scratch helpers (not committed) are in the same `e2e226/` dir: `runs.py` (Prefect run tree), `sample_inflight.sh` (lsof sampler), and `*.log` / `counts_*.txt`.

### [Tester] 2026-10-28 — QA

**Test summary** (LOCAL env, `make env-status` → `local (.env)`)
- Format / lint / pre-commit: PASS (`338 files already formatted`, `All checks passed!`, prettier / ruff check / ruff format / biome all Passed)
- Unit tests: 5246 passed / 0 failed (`make memory-tests`, 67 s). No integration suite exists (AGENTS.md).
- Warnings: 0
- `git status`: only the 3 docs files + this task file are modified; no code change. `git diff --check` is clean.

**E2E adversarial pass** (docs task, so the "product" is the docs and the evidence behind them)
- Happy path, claims vs code: read `data/batch.py`, `web_pipeline.py`, `substack_pipeline_batch.py`, `youtube_pipeline_batch.py`, `youtube_pipeline.py`, `arxiv_dataset_pipeline.py`, `config/sources.py`, `default.yaml` → every claim in the amendment, the glossary Batch row and the ADR-003 line is true (`itertools.batched(..., data.batch_size)` in Web/Substack/YouTube; HF streams `data.batch_size` chunks; `gather_isolated` Semaphore(`data.concurrency`); `data:` block 500 / 50; `offline_cron: "0 3 * * *"`; `invalid_inputs` ride chunk index 0 only; `extract-substack-article-batch` runs only `if chunk_urls`; YAML comment "~173s" single video). PASS.
- Break path 1 (stale-wording sweep): `grep -rniE "one isolated (load )?gather|single isolated gather|whole handed|handed-in|small constant|constant per shard|whole URL list|ONCE over|over ALL items|one feed's entries|..." docs README.md apps/memory/README.md apps/memory/src/tree .agents sources`, plus `grep -rIn "batch_size|BATCH_SIZE"` over md/yaml/Makefile/.env* (excluding tasks/) → only hits are the intentional ones: the new amendment (describing the OLD state, ADR-002:644/674) and `web_pipeline.py:4` ("walks the handed-in URL list in batch_size chunks", accurate). No `sources/*.yaml` sets batch_size/concurrency. PASS.
- Break path 2 (deploy-caveat claim): `HuggingFaceDatasetSource(uri='a/b', batch_size=5)` → `ValidationError ... batch_size Extra inputs are not permitted [type=extra_forbidden]`; defaults dump has no batch_size/concurrency. Matches the amendment's caveat. PASS.
- Break path 3 (do not trust the SWE log: independent Prefect re-query, `PREFECT_API_URL=http://127.0.0.1:4200/api`, script `<scratchpad>/qa_runs.py`): all four root runs and every child COMPLETED, task-run counts identical to the log:
  - `3ab5776d…` Substack: fetch-substack-rss-feed 1, extract-substack-article-batch 2, load-substack-batch 8 (⌈22/3⌉ = 8). PASS
  - `4a322881…` extract-web-batch 3 / load-web-batch 3 (⌈7/3⌉), fetch-youtube-transcripts-batch 2 / load-youtube-batch 2 (⌈4/3⌉). PASS
  - `7ae4df51…` HF: load-arxiv-batch 3 (⌈7/3⌉); flow-run `entry` param = `{type, uri, max_samples:7, fetch_content:False, num_workers:1, offset:None}`, no batch_size/concurrency. PASS
  - `54eef0e4…` re-run: web 3+3, youtube 2+2, substack 1+2+8, all COMPLETED. PASS
- Break path 4 (Mongo, `mongosh` local, SWE's `counts.js`): substack 108 (simonw feed 20, interconnects articles 2), web 7 (wikipedia), youtube 5 (4 new videos present, `ingest_error` 0, total ingest_error rows 0), huggingface 14 — identical to the log's "after run 3". Idempotency re-run added 0 documents (the one YouTube row replaced in place). PASS
- Break path 5 (in-flight cap + timings from retained logs in `e2e226/`): web Unlocker fetches start in pairs (21.089 / 21.093, third at 25.798); `inflight_run2.txt` lsof samples show web pid max 2 connections; Bright Data snapshot `sd_mvs57vb2wujn7vtfk` trigger 15:21:20.8 → returned 2 records 15:23:38.5 (≈138 s) as the amendment states. PASS
- Infra: `docker ps` → `tree-prefect-worker Up`, `tree-prefect-server Up (healthy)`; `curl localhost:4200/api/health` → true. PASS
- Markdown/wrapping: added lines ≤93 chars (ADR-002 wraps ~92, file already has 522-char lines elsewhere); ADR-003 ≤91 (file max 117); the glossary row is a single table line like its neighbours. `git diff --check` clean. PASS

**Acceptance criteria**
- [x] PASS — ADR-002 amendment present with 500 / 50 / ⌈N/500⌉ / "100,000 Substack items → 200 chunks → ~400 task runs" good-vs-bad examples; no doc still calls the whole handed-in URL list the batch. Evidence: `docs/adrs/002_…md:642-677`, sweep above. The 500-video Bright Data timing is explicitly NOT claimed ("NOT timed yet … 600 s is unverified at full chunk size"), task 224's `[HUMAN]` AC is left open. Substack-RSS-in-RAM gap and `extra="forbid"` deploy caveat both present.
- [x] PASS — E2E: task-run counts, in-flight ≤2, Mongo counts: verified independently above.
- [x] PASS — Re-run inserts 0 new documents: verified (Mongo counts equal after run 3; run `54eef0e4…`).
- [x] PASS — Cadence green on local env.

**Evidence**
```
$ make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests
338 files already formatted / All checks passed! / prettier,ruff check,ruff format,biome: Passed
======================= 5246 passed in 67.26s (0:01:07) ========================
```

**Other issues found (non-blocking, PASS with note)**
- ADR-002:649-650 says "Each leaf flow walks its flattened items in `itertools.batched(items, data.batch_size)`". True for Web / Substack / YouTube; HF does not use `itertools.batched`, it consumes the `fetch_dataset_batches(max_samples, batch_size)` stream (the bullet does list it separately as "its streamed chunks"). Wording could say "walks (or, for HF, streams)". Cosmetic.
- ADR-002 timing sentence says "a 3-video collection took 138 s / 76 s", but that snapshot returned 2 records (the log notes this). The number is still a true wall-time for a 3-URL trigger; "3-URL" would be more precise. Cosmetic.
- "wait is mostly fixed queue time" is an inference from 4 data points (1 vs 3 videos, two runs); it is hedged ("mostly") and the 500-video gap is stated. Acceptable.
- The Web / YouTube re-run re-billing observation (dedup only at load) is already recorded by the SWE as a candidate follow-up task; agreed, out of scope here.
- Cost note: I made no billable calls; all evidence is re-queries of Prefect/Mongo and retained logs.

**VERDICT: PASS**
