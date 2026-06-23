# Feature Plan: Batch-grain ETL task topology for the leaf pipelines

## Summary
Every batch ETL leaf pipeline (arXiv/HF, substack rss+article, youtube video+rss, web)
today invokes its Extract/Transform/Load helpers as PER-ROW Prefect `@task`s. With the live
arXiv config (`max_samples: 1000`, `batch_size: 50`, `num_workers: 2`) one HF **Window**
worker emits ~1000 task runs for ~500 docs — `extract-arxiv-document` + `load-arxiv-document`
per row (× `fetch_content`) — the explosion in the owner's Prefect screenshot. This feature
restructures the Prefect TASK topology so each task operates at BATCH grain reflecting the
ETL phases: a window worker drops from ~1000 task runs to a few tens. The flow-level topology
(orchestrator → worker → per-source batch flow) is UNCHANGED. The three direct-link pipelines
also collapse their per-item sub-flows into plain `_ingest_<x>_one` core functions, keeping a
thin `@flow` wrapper used ONLY by the MCP `ingest_url` router. Every stable seam
(`ingest_<x>_batch` names/signatures, `_BATCHED_VARIANTS`, `_HUGGINGFACE_DATASET_HANDLERS`,
the orchestrator/worker flows, `tree.data.ingest`) is preserved.

This is a task-grain refinement of ADR-002 §3 (pipeline topology), absorbed as an AMENDMENT
to ADR-002 (Status stays `Accepted`), exactly as #055/#061/#066/#070–#074 were — NOT a new
ADR-003.

## Locked design (decisions are final — do not relitigate)

1. **Batch-grain tasks.** Every batch ETL flow's Prefect tasks operate on a **Batch** (HF
   `batch_size`-chunk / one feed's entries / the whole handed-in URL list), NEVER per record.
   Target: window worker ~1000 → tens of task runs.

2. **Collapse the per-item sub-flows of the DIRECT-LINK pipelines.** Demote the bodies of
   `ingest_substack_article`, `ingest_youtube_video`, `ingest_web_url` into plain async CORE
   functions (`_ingest_<x>_one(...)`). The batch flows STOP fanning out per-item sub-flows;
   batch tasks call the core per element with per-element isolation.

3. **MCP dual-use — keep a thin per-item flow for MCP only.** `ingest_web_url` /
   `ingest_substack_article` / `ingest_youtube_video` are also the entry points the MCP
   `ingest_url` router (`tree.data.ingest`) invokes for a single URL. KEEP each as a 1-line
   `@flow` wrapper around its new core fn, used ONLY by the MCP router (MCP single-URL ingest
   still gets its own Prefect flow run + Opik trace). The BATCH path must NOT call these
   wrappers.

4. **RSS pipelines: keep feed-obtain; share only the build/load tail — do NOT re-fetch.**
   - **Substack RSS:** KEEP reading feed-embedded content (`fetch_feed` +
     `extract_document(feed_entry)→Document`); 1 feed fetch → N docs, NO per-article
     re-scrape. Batch tasks: `fetch_feed` (Extract, per feed) → `transform_batch` (feed
     entries → Documents via the existing `extract_document`) → `load_batch` (the
     already-shared `load_document`). substack_article keeps its scrape-extract; the two
     share the LOAD (already shared). Do NOT unify substack RSS onto the article scrape path.
   - **YouTube RSS + video:** SHARE the bulk-transcript-fetch + build + load at the BATCH
     layer (`build_document`/`load_video_document` already shared). The direct-video batch
     ADOPTS the bulk `fetcher.fetch_many(all_urls)` (today per-video `fetch_many([url])`
     inside per-URL sub-flows). Metadata SOURCE stays distinct: oEmbed (per video, direct) vs
     feed (`feed_entry_to_metadata`, RSS). One shared "(url, metadata) list → bulk transcripts
     → build_batch → load_batch" core, called by both with their own metadata source. ONE
     bulk fetch per feed; no per-video re-fetch regression.

5. **Pragmatic E/T/L (Load always separate).** ETL vocabulary + batch grain uniformly; task
   COUNT follows genuinely-separable phases. FUSE Extract+Transform into one `extract_batch`
   where a single scrape yields the Document (web / substack-article / youtube-video-metadata
   path); keep a separate `transform_batch` where transform is a genuine pure map (arXiv
   dict→Document; substack-RSS feed-entry→Document). LOAD is ALWAYS its own batch task. arXiv:
   streamed read (`fetch_dataset_batches`) stays the flow loop (Extract);
   `extract_document`→`transform_batch`; optional `fetch_paper_content`→`enrich_batch`
   (network Extract, only when `fetch_content`); `load_document`→`load_batch`.

6. **Per-element isolation INSIDE each batch task.** `asyncio.gather(return_exceptions=True)`
   (preserve existing per-element concurrency — arXiv `asyncio.Semaphore(concurrency)`,
   youtube bulk `fetch_many`); log + SKIP per-element (bad-data) failures, return the
   successful subset + a per-batch failure COUNT; do NOT hard-fail the task on per-element
   failures. The task hard-fails only on a batch-WIDE infra failure → Prefect retries the
   whole batch, SAFE because `load_document` is idempotent (dedup on `(user_id, source_uri)`).

7. **Retry relocation.** Keep batch-task retries for batch-wide infra (fetch/extract
   `retries=2`; load `retries=1`; pure transform `retries=0`). Per-element transient FETCH
   retries move INTO / stay in the fetch layer (existing httpx retries / the chain's per-slot
   fallback). Do NOT regress existing network-fetch retry behavior.

8. **Batch boundary.** HF keeps `batch_size` chunking (E/T/L per chunk). URL/feed batch flows
   treat the whole handed-in list as ONE batch (volume is tens). RSS: fetch per feed, then
   `transform_batch` + `load_batch` over that feed's entries — boundary is **per-feed**
   (default) to keep a bad feed isolated and, for youtube, one bulk `fetch_many` per feed.

9. **Stable seams (do NOT change).** Batch flow NAMES + signatures
   (`ingest_<x>_batch(uris, user_id) -> list[Document]`, `ingest_arxiv_dataset(...)`) stay
   identical so `_BATCHED_VARIANTS` / `_HUGGINGFACE_DATASET_HANDLERS` / `_ingest_sources` in
   `data/pipeline.py` + the orchestrator/worker flows keep working UNCHANGED.
   `data-etl-orchestrator` / `data-etl-worker` are out of scope. The MCP `ingest_url` router
   (`tree.data.ingest`) is unchanged.

10. **Don't persist task results; preserve Opik trace structure.** Prefect-3 result
    persistence is OFF by default (the repo sets no `persist_result`/`result_storage`/
    `cache_policy`/`PREFECT_RESULTS_PERSIST_BY_DEFAULT`), so the side-effecting load/extract
    tasks already persist nothing — do NOT add `persist_result=False` unless a cache policy is
    introduced (none is). Opik spans are per-batch-PHASE (the batch-flow `span(...)` is
    unchanged; there were never per-doc Opik spans — the explosion is in Prefect TASK runs,
    not Opik); preserve the batch-flow-level trace-header forwarding/span structure.

11. **Scope.** ONLY the 5 batch ETL leaf pipelines (arxiv, substack rss+article, youtube
    video+rss, web) + the 3 direct-link per-item flow collapses + 3 MCP core-fn extractions.
    EXCLUDE `conversation`/`file` (single-doc, already module-ized in #077). The data
    orchestrator/worker, the fan-out axis, and the MCP router are unchanged.

## Caveats (state them — they are NOT blockers)

- **The shared per-element-isolation helper is introduced ONLY if it genuinely DRYs 4+ call
  sites.** #078 DEFAULTS to inlining the `gather(return_exceptions=True)` + log-and-skip +
  count logic; #079–#081 may lift it into a `tree.data.batch` module once the duplication
  actually appears. No speculative abstraction (CLAUDE.md: prefer removing instructions; the
  recent ponytail audit, commit `7642f2b`, cut over-engineering from `data/`).

- **The Opik "win" is in Prefect, not Opik.** The per-doc explosion is in Prefect TASK runs;
  the batch flows already own ONE Opik trace via `span(...)` with no per-doc spans. So the
  observability change is "no change to Opik structure" — the spec is precise about this to
  avoid a phantom requirement.

- **Pre-existing `except ValueError, TypeError:` (no parens) lines** in `arxiv_dataset.py`,
  `substack_rss.py`, `youtube_rss.py` are OUT OF SCOPE (noted in #074's log) — the pinned
  CPython 3.14 build accepts the grammar; do not touch these in this feature.

- **The arXiv `transform_batch` is the only genuine standalone pure-map transform among the
  scrape pipelines.** Web/substack-article/youtube-video FUSE Extract+Transform because the
  scrape itself yields the Document; only arXiv (dict→Document) and substack-RSS
  (feed-entry→Document) keep a separate `transform_batch`. This asymmetry is intentional
  (decision 5), not an inconsistency.

## Tasks (in order)

1. **078** — arxiv (HF) batch-ETL conversion — the exploder. Replace per-row
   `extract_document`/`fetch_paper_content`/`load_document` `@task`s + `_process_document`
   with `transform_batch` (pure map) / `enrich_batch` (optional network) / `load_batch`
   ETL-phase tasks over each streamed chunk; per-element isolation via
   `gather(return_exceptions=True)`; establish the reusable pattern (isolation helper inlined
   unless it DRYs 4+ sites). Flow signature, `offset` windowing, and all seams unchanged.
   Lands first. (file: `tracker/078-arxiv-batch-etl-task-topology.groomed.md`)
2. **079** — substack (rss + article) — substack-RSS keeps feed-content transform
   (`fetch_feed` → `transform_batch` → `load_batch`, per feed, NO re-fetch); substack-article
   collapses its per-item sub-flow → `_ingest_substack_article_one` core + thin
   `ingest_substack_article` MCP flow, with `extract_batch` (scrape, E+T fused) + `load_batch`
   sharing the existing `load_document` tail. Depends on #078. (file:
   `tracker/079-substack-batch-etl-and-article-flow-collapse.groomed.md`)
3. **080** — youtube (video + rss) — factor the shared "(url, metadata) list → bulk
   `fetch_many` → build_batch → load_batch" core; direct-video collapses its per-item sub-flow
   → `_ingest_youtube_video_one` core + thin `ingest_youtube_video` MCP flow (ADOPTING the
   bulk transcript fetch); RSS supplies feed metadata. Depends on #078. (file:
   `tracker/080-youtube-batch-etl-shared-bulk-core-and-video-flow-collapse.groomed.md`)
4. **081** — web — `extract_batch` (Bright Data scrape, E+T fused) + `load_batch` over the URL
   list; collapse `ingest_web_url` → `_ingest_web_url_one` core + thin `ingest_web_url` MCP
   flow (the `ingest_url` generic-web fallback). Depends on #078. (file:
   `tracker/081-web-batch-etl-and-url-flow-collapse.groomed.md`)
5. **082** — ADR-002 §3 amendment + glossary verify + full acceptance + live e2e — land the
   proposed amendment (batch-not-document granularity, pragmatic E/T/L, per-item sub-flow
   collapse + thin MCP flow, per-element isolation + idempotent whole-batch retry, retry
   relocation, RSS no-re-fetch, result-persistence-off, unchanged invariants); verify the
   glossary rows; run `make memory-integration-tests-all`; `[HUMAN]` live e2e (Prefect UI
   shows a window worker with tens of task runs NOT ~1000, batch ETL-phase tasks per batch, no
   per-item sub-flow runs, one bulk transcript fetch per feed, idempotent re-run, single-URL
   MCP smoke). Defer the `[HUMAN]` ACs to the owner like #074. Depends on #078–#081. (file:
   `tracker/082-etl-task-granularity-adr-and-live-e2e-acceptance.groomed.md`)

## Out of scope (intentional)

- **The flow-level topology + data fan-out axis.** orchestrator → worker → per-source batch
  flow, the group-by-platform partition + HF offset-windowing (§3 amendment #070–#074), and
  the two `data-etl-*` deployments are UNCHANGED. Only the per-flow Prefect TASK grain
  changes.
- **The MCP `ingest_url` URL router.** `tree.data.ingest` (static registry → custom-Substack
  domain → generic-web fallback) is byte-for-byte unchanged; it keeps calling the now-thin
  per-item flows.
- **`conversation` / `file`.** Single-doc pipelines (module-ized in #077); not batch ETL, not
  touched.
- **Unifying substack RSS onto the article scrape path / re-fetching RSS articles.** RSS keeps
  feed-embedded content; only the LOAD (substack) / build+load (youtube) tail is shared.
- **A new ADR-003.** This is a task-grain refinement of ADR-002 §3 → an AMENDMENT, not a new
  ADR (recommendation below).
- **The pure cores + the pre-existing two-class-except lines.** `extract_document`,
  `fetch_*`, `load_*`, `build_document`, `feed_entry_to_metadata`, etc. are unchanged; the
  `except ValueError, TypeError:` lines are left untouched (out of scope).

## Documentation updates (this grooming round)

- **Glossary** (`docs/glossary.md`): ADDED two rows in the grooming commit — **ETL-phase
  task** (Extract/Transform/enrich/Load as batch-grain Prefect tasks; Load always separate;
  per-element isolation inside; whole-batch-retry-safe-because-load-idempotent; retry grain
  on the batch) and **Thin MCP flow** (the 1-line `@flow` wrapper around `_ingest_<x>_one`,
  used ONLY by the URL router; the batch path calls the core directly). RECONCILED the
  **Batch** row (now defined as the ETL-phase task's grain) and the **Worker** row (a leaf
  batch flow runs ETL-phase tasks over its Batch(es); no per-item sub-flow per Document). Table
  style preserved; unchanged terms not restated. These land in the grooming commit; #082
  verifies them against the shipped code.
- **ADRs:** ADR-002 (`docs/adrs/002_pipeline_concurrency_and_voyage_rate_limiting.md`) §3 is
  AMENDED (not superseded; Status stays `Accepted`) to record the batch-grain task topology.
  Per the owner's brief the amendment text is DRAFTED in grooming (handed back as a PROPOSAL
  for the human gate; see the grooming hand-back) and AUTHORED to disk in task **#082** — it
  is NOT pre-written in the grooming commit (mirrors how #074 handled its amendment). ADR-001
  unchanged.

## ADR recommendation: AMENDMENT to ADR-002 §3 (NOT a new ADR-003)

**Recommendation: amend ADR-002 §3.** ADR-002 §3 already governs the pipeline fan-out +
topology, and has been refined four times by indented amendments (#055/#061/#066/#070–#074),
each `Status: Accepted`-preserving. The batch-grain task topology is the SAME decision at a
finer grain: it doesn't change the fan-out axis, the deployment count, the
gather-failure-isolation primitive, the GCL, or admission control — it changes how the WORK
INSIDE a worker is expressed as Prefect tasks. Authoring a new ADR-003 would split a single
evolving topology decision across two files and break the established §3 amendment trail. A
new ADR is warranted only when a decision is genuinely orthogonal to ADR-002's concerns; this
one is not. (Draft text to be authored in #082 below.)

## Open questions
- None blocking. Every decision (batch grain; per-item sub-flow collapse + thin MCP flow; RSS
  feed-obtain + shared build/load tail with no re-fetch; pragmatic E/T/L with Load always
  separate; per-element isolation + idempotent whole-batch retry; retry relocation; per-feed
  batch boundary; stable seams held fixed; result-persistence-off; ADR-002 amendment not a new
  ADR) is pinned in the locked design above. The only SWE-discretion items are (a) whether the
  per-element-isolation helper is inlined or lifted into `tree.data.batch` (governed by the 4+
  call-site threshold) and (b) exact module placement of the youtube shared bulk core.
