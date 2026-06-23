# substack batch-ETL tasks + article per-item sub-flow collapse

Status: pending
Tags: `data`, `prefect`, `refactor`
Depends on: #078
Blocks: #082

## Scope

Apply the #078 batch-ETL pattern to BOTH Substack leaf pipelines, and collapse the
Substack-article per-item sub-flow. Two distinct shapes share the LOAD:

- **substack RSS** builds Documents from FEED-EMBEDDED content (1 feed fetch → N docs, NO
  per-article re-scrape). It KEEPS its feed-obtain step and gets batch Transform + Load.
- **substack article** SCRAPES each URL (`fetch_and_extract`), so its Extract+Transform
  FUSE (one scrape yields the Document). Its per-item sub-flow `ingest_substack_article`
  collapses into a plain core fn `_ingest_substack_article_one`, with a THIN MCP-only flow
  wrapper retained.

The LOAD is already shared: `substack_article.load_article_document` delegates to
`substack_rss.load_document`. We preserve that shared tail at the batch layer.
Batch-flow names + signatures (`ingest_substack_rss_feed_batch(feed_urls, user_id)`,
`ingest_substack_article_batch(article_urls, user_id)`) are UNCHANGED stable seams.

### 1. substack RSS — keep feed-obtain; batch Transform + Load (NO re-fetch)

In `substack_rss_pipeline.py`, DELETE the per-row `extract_document_task` and
`load_document_task` `@task`s; keep `fetch_feed_task` (Extract, per feed, `retries=2`).
Restructure per the brief's locked design (decision 4):

- **`fetch_feed`** (Extract, per feed) — the existing `fetch_feed_task` over one feed URL
  → `list[dict]` raw entries. KEEP reading feed-embedded content; do NOT re-scrape articles.
- **`transform_batch`** (`@task`, pure map, `retries=0`) — `(list[dict], user_id) →
  list[Document]` via the existing pure `substack_rss.extract_document(feed_entry, user_id)`
  per entry. No network.
- **`load_batch`** (`@task`, `retries=1`, `retry_delay_seconds=2`) — awaits the existing
  `substack_rss.load_document(doc, raw_entry)` per `(doc, entry)` pair under
  `asyncio.gather(return_exceptions=True)`; returns the successful, non-`None` subset;
  per-element failures logged + skipped. `load_document` still resolves references from the
  feed-embedded `raw_entry` exactly as today (no behavior change to reference resolution).

**Batch boundary (per-feed, default).** `ingest_substack_rss_feed_batch(feed_urls, user_id)`
keeps `init_mongodb` once at the top, then PER FEED: `entries = await fetch_feed(feed_url)`
→ `docs = transform_batch(entries, user_id)` → `await load_batch(docs, entries)`. Keep
per-feed isolation (a bad feed must not sink the others) — wrap the per-feed work so one
feed's failure is logged + skipped (mirror the existing `asyncio.gather` over feeds, but
the inner per-feed body now uses batch tasks). DELETE the inner single-feed
`ingest_substack_rss_feed` `@flow` (it was a per-feed sub-flow); fold its body into the
per-feed batch path. (Note: `ingest_substack_rss_feed` is NOT an MCP entry point — RSS
feeds are rejected by `ingest_url` — so NO thin MCP flow is needed for RSS.)

### 2. substack article — collapse the per-item sub-flow; keep a thin MCP flow

In `substack_article_pipeline.py`:

- Demote the body of the `ingest_substack_article` `@flow` into a plain async CORE function
  `_ingest_substack_article_one(article_url, user_id) -> Document | None` (fetch+extract
  via the pure `substack_article.fetch_and_extract`, then load via the shared
  `substack_article.load_article_document`). NO `@flow`/`@task` decorators on the core.
- Keep `ingest_substack_article(article_url, user_id) -> Document | None` as a 1-line
  `@flow` wrapper around `_ingest_substack_article_one`, used ONLY by the MCP URL router
  (`tree.data.ingest._ingest_substack_article` → `ingest_substack_article`). MCP single-URL
  ingest still gets its own Prefect flow run + Opik trace.
- DELETE the per-row `fetch_and_extract_task` / `load_article_document_task` `@task`s.
  Replace with batch ETL-phase tasks over the whole handed-in URL list (volume is tens →
  ONE batch):
  - **`extract_batch`** (`@task`, network Extract+Transform FUSED, `retries=2`,
    `retry_delay_seconds=5`) — `(list[str], user_id) → list[tuple[Document, str]]`: scrapes
    each URL via the pure `substack_article.fetch_and_extract` under
    `asyncio.gather(return_exceptions=True)`; per-URL scrape failures logged + skipped.
  - **`load_batch`** (`@task`, `retries=1`, `retry_delay_seconds=2`) — awaits the SHARED
    `substack_article.load_article_document(doc, body_html)` per element under
    `asyncio.gather(return_exceptions=True)`; returns the successful non-`None` subset.
- `ingest_substack_article_batch(article_urls, user_id)` keeps `init_mongodb` once at the
  top, then `docs = await extract_batch(urls, user_id)` → `await load_batch(docs)`. The
  batch path MUST NOT call the thin `ingest_substack_article` flow (no per-item sub-flow
  runs).

### 3. Per-element isolation + shared helper decision

Same isolation contract as #078 (`gather(return_exceptions=True)` → successes +
failure-count; log + skip per-element; hard-fail only batch-wide → idempotent retry). If
#078 inlined the isolation logic and it now recurs across substack RSS load + article
extract + article load (plus #078's two), the SWE MAY lift it into a shared
`tree.data.batch` helper here (the 4+-call-site threshold from #078 §3). SWE's discretion;
either inline or extract is acceptable as long as behavior matches.

### Files touched

- `apps/memory/src/tree/data/substack/substack_rss_pipeline.py` — keep `fetch_feed_task`;
  replace per-row extract/load tasks with `transform_batch` + `load_batch`; fold the
  per-feed sub-flow into the batch flow's per-feed loop; delete `ingest_substack_rss_feed`.
- `apps/memory/src/tree/data/substack/substack_article_pipeline.py` — add
  `_ingest_substack_article_one` core; keep `ingest_substack_article` as a thin MCP-only
  `@flow`; replace per-row tasks with `extract_batch` + `load_batch`; rewire
  `ingest_substack_article_batch` to call the core via batch tasks (NOT the thin flow).
- `apps/memory/src/tree/data/substack/substack_rss.py`,
  `apps/memory/src/tree/data/substack/substack_article.py` — UNCHANGED pure cores
  (`extract_document`, `fetch_feed`, `load_document`, `fetch_and_extract`,
  `load_article_document`). Do NOT fix the pre-existing `except ValueError, TypeError:` in
  `substack_rss.parse_date` — out of scope.
- `apps/memory/src/tree/data/ingest.py` — UNCHANGED (still imports + calls
  `ingest_substack_article`, now the thin flow). Confirm import resolves.
- `apps/memory/tests/unit/data/substack/test_substack_rss_pipeline.py`,
  `test_substack_article.py` (+ a new `test_substack_article_pipeline.py` if absent) —
  rework for the batch shape + the core/thin-flow split (see Test guidance).
- `apps/memory/tests/integration/data/substack/test_substack_rss_pipeline.py` — keep
  flow-level assertions; assert NO per-article re-fetch (see Test guidance).

## Acceptance Criteria

- [ ] substack RSS: `ingest_substack_rss_feed_batch` builds Documents from FEED-EMBEDDED
      content only — there is NO per-article HTTP scrape (one `fetch_feed` per feed, then
      `transform_batch` + `load_batch`). The article-scrape path (`fetch_and_extract`) is
      NOT invoked anywhere in the RSS flow.
- [ ] substack RSS: per-row `extract_document_task` / `load_document_task` tasks are
      removed; `transform_batch` (pure, `retries=0`) and `load_batch` (`retries=1`) operate
      over a feed's entries; the inner `ingest_substack_rss_feed` sub-flow is gone, folded
      into the per-feed loop with per-feed failure isolation preserved.
- [ ] substack article: `_ingest_substack_article_one(url, user_id)` exists as a plain
      async core fn (no decorators); `ingest_substack_article` remains a 1-line `@flow`
      wrapper around it, used by the MCP router; the BATCH flow does NOT call that wrapper.
- [ ] substack article: `extract_batch` (network, `retries=2`) scrapes per URL with
      per-element isolation; `load_batch` (`retries=1`) awaits the SHARED
      `load_article_document` per element with per-element isolation; both are single
      gathers over the whole URL list.
- [ ] The shared LOAD tail is preserved: article load still delegates to
      `substack_rss.load_document` (reference resolution identical).
- [ ] Stable seams unchanged: `ingest_substack_rss_feed_batch` /
      `ingest_substack_article_batch` names + signatures intact; `_BATCHED_VARIANTS` in
      `data/pipeline.py` resolves both by name (the no-mock guard test passes); the MCP
      `ingest_url` route to `ingest_substack_article` still works.
- [ ] `make memory-format-fix && make memory-lint-fix && make memory-format-check &&
      make memory-lint-check` clean; `make pre-commit` clean.
- [ ] `make memory-unit-tests` passes, 0 warnings.
- [ ] `make memory-integration-tests` (fast tail) passes — RSS batch flow ingests N docs
      for an N-entry feed; the article batch flow ingests/persists via the core.
- [ ] [HUMAN] Deferred to #082: Prefect UI shows a substack worker with batch ETL-phase
      tasks and NO per-article/per-feed sub-flow runs.

## BDD scenarios

### Scenario: substack RSS does not re-fetch articles
- **Given** a feed whose entries carry embedded `content`
- **When** `ingest_substack_rss_feed_batch` ingests it
- **Then** exactly one feed fetch occurs, `substack_article.fetch_and_extract` is NEVER
  called, and N Documents are built from the feed-embedded content.

### Scenario: the article batch path creates no per-item sub-flow
- **Given** a list of 10 article URLs
- **When** `ingest_substack_article_batch` runs
- **Then** the `ingest_substack_article` thin `@flow` is NOT invoked (no per-item sub-flow
  runs); `extract_batch` + `load_batch` each run once over the 10 URLs.

### Scenario: the thin MCP flow still ingests a single URL
- **Given** the MCP `ingest_url` router routes a substack.com URL
- **When** it calls `ingest_substack_article(url, user_id)`
- **Then** the URL is fetched, extracted, and persisted (returns the Document or `None` for
  a duplicate) — single-URL MCP ingest still gets its own flow run.

### Scenario: a failed scrape is isolated in the article batch
- **Given** one of 10 URLs raises during `fetch_and_extract`
- **When** `extract_batch` runs
- **Then** that URL is logged + skipped, the other 9 are extracted, and the task does not
  hard-fail.

## User Stories

### Story: An operator confirms RSS ingest is cheap (one fetch per feed)
1. The operator runs the data pipeline with several `substack_rss` feeds configured.
2. They inspect the substack worker run in the Prefect UI.
3. Each feed shows ONE fetch + a `transform-batch` + a `load-batch` task — not a per-article
   scrape and not a per-entry task explosion.

### Story: A user ingests one Substack article from the assistant
1. The user pastes a substack.com article URL to the MCP `ingest_url` tool.
2. The router calls the thin `ingest_substack_article` flow, which scrapes + persists the
   one article.
3. The user gets back a single ingested Document (or a "duplicate" no-op) — unchanged from
   before this refactor.

### Story: A maintainer sees the article and RSS paths share the load
1. A maintainer reads both substack pipelines.
2. They confirm `load_batch` in the article path delegates to the same
   `substack_rss.load_document` the RSS path uses (via `load_article_document`).
3. Reference resolution and dedup behave identically across both — one load implementation,
   two ingest fronts.

## Test guidance

- Call `/testing-python`. Run ONLY via `make memory-*` (LOCAL env).
- substack RSS unit: rework `test_substack_rss_pipeline.py` — drop the removed per-row task
  tests; add `transform_batch.fn(entries, user_id)` (pure map) and `load_batch.fn(docs,
  entries)` with `load_document` patched `side_effect=[doc, None]` → `[doc]`, and
  `side_effect=[doc, RuntimeError]` → `[doc]` (isolated). Assert `fetch_and_extract` is NOT
  imported/called by the RSS pipeline (the no-re-fetch invariant).
- substack article unit: test `_ingest_substack_article_one` directly (patch the pure
  `fetch_and_extract` + `load_article_document`); test the thin `ingest_substack_article.fn`
  delegates to the core; test `extract_batch.fn` / `load_batch.fn` isolation; assert the
  batch flow does NOT call `ingest_substack_article` (e.g. spy/patch the thin flow and
  assert not-awaited).
- Integration (`test_substack_rss_pipeline.py`): keep the existing flow assertions; ADD an
  assertion that the article-scrape client is never constructed during RSS ingest (mock
  `substack_article.fetch_article` / `httpx.AsyncClient` in `substack_article` and assert
  zero calls), proving no re-fetch.
- Retry-metadata asserts mirror `test_web_pipeline.py::TestTaskAndFlowMetadata`
  (`transform_batch.retries == 0`, `extract_batch.retries == 2`, `load_batch.retries == 1`).

---

Blocked by: #078

## Log

### [PA] 2026-06-23 — Grooming

**Summary**
Batch-ETL the two Substack leaf pipelines and collapse the article per-item sub-flow.
RSS keeps its feed-obtain step and builds from feed-embedded content (NO re-scrape) via
`fetch_feed` → `transform_batch` → `load_batch` per feed. Article fuses Extract+Transform
(scrape) into `extract_batch`, loads via `load_batch`, demotes `ingest_substack_article`'s
body to `_ingest_substack_article_one`, and keeps a thin `ingest_substack_article` `@flow`
for the MCP router only. Both share the existing `load_document` tail.

**Key decisions**
- RSS batch boundary is PER-FEED (default in the brief) — one fetch + transform + load per
  feed, keeping a bad feed isolated; the cross-feed `gather` stays.
- Article Extract+Transform FUSE (a single scrape yields the Document) → one `extract_batch`
  task, per the brief's pragmatic E/T/L rule; Load stays separate (`load_batch`).
- The thin MCP flow is retained ONLY for `tree.data.ingest`'s single-URL path; the batch
  path calls the core directly — no per-item sub-flow runs.
- Shared isolation helper: lift into `tree.data.batch` here ONLY if it now hits the 4+
  call-site threshold from #078; else keep inline. SWE discretion.
- No re-fetch regression: the RSS path provably never calls the article scrape; asserted in
  both unit and integration tests.

**Dependencies**
- #078 — establishes the batch-task + per-element-isolation pattern this copies.

**User stories**
- 3 stories: operator confirms cheap RSS ingest; user ingests one article via MCP; a
  maintainer sees the shared load.

**Documentation discipline**
- Canonical glossary terms (Batch, ETL-phase task, Thin MCP flow, URL router). No new
  terms; the Thin-MCP-flow + ETL-phase-task rows added in the grooming commit cover this.

Ready for implementation.
