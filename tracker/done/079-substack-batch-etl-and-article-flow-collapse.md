# substack batch-ETL tasks + article per-item sub-flow collapse

Status: in-progress
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

- [x] substack RSS: `ingest_substack_rss_feed_batch` builds Documents from FEED-EMBEDDED
      content only — there is NO per-article HTTP scrape (one `fetch_feed` per feed, then
      `transform_batch` + `load_batch`). The article-scrape path (`fetch_and_extract`) is
      NOT invoked anywhere in the RSS flow.
- [x] substack RSS: per-row `extract_document_task` / `load_document_task` tasks are
      removed; `transform_batch` (pure, `retries=0`) and `load_batch` (`retries=1`) operate
      over a feed's entries; the inner `ingest_substack_rss_feed` sub-flow is gone, folded
      into the per-feed loop with per-feed failure isolation preserved.
- [x] substack article: `_ingest_substack_article_one(url, user_id)` exists as a plain
      async core fn (no decorators); `ingest_substack_article` remains a 1-line `@flow`
      wrapper around it, used by the MCP router; the BATCH flow does NOT call that wrapper.
- [x] substack article: `extract_batch` (network, `retries=2`) scrapes per URL with
      per-element isolation; `load_batch` (`retries=1`) awaits the SHARED
      `load_article_document` per element with per-element isolation; both are single
      gathers over the whole URL list.
- [x] The shared LOAD tail is preserved: article load still delegates to
      `substack_rss.load_document` (reference resolution identical).
- [x] Stable seams unchanged: `ingest_substack_rss_feed_batch` /
      `ingest_substack_article_batch` names + signatures intact; `_BATCHED_VARIANTS` in
      `data/pipeline.py` resolves both by name (the no-mock guard test passes); the MCP
      `ingest_url` route to `ingest_substack_article` still works.
- [x] `make memory-format-fix && make memory-lint-fix && make memory-format-check &&
      make memory-lint-check` clean; `make pre-commit` clean.
- [x] `make memory-unit-tests` passes, 0 warnings.
- [x] `make memory-integration-tests` (fast tail) passes — RSS batch flow ingests N docs
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

### [SWE] 2026-06-23 — Batch-ETL both substack pipelines + collapse article sub-flow

**Summary**
Converted BOTH substack leaf pipelines to batch-grain ETL-phase tasks (mirroring #078)
and collapsed the article per-item sub-flow into a plain core, keeping a thin MCP-only
flow wrapper. Lifted the per-element isolation into a shared helper `tree.data.batch`.

- **substack_rss** — deleted the per-row `extract_document_task` / `load_document_task`
  and the inner `ingest_substack_rss_feed` @flow. New shape per feed:
  `fetch_feed_task` (`retries=2`) → `transform_batch` (pure map, `retries=0`, via the
  pure `extract_document` — builds from FEED-EMBEDDED content, NO re-scrape) →
  `load_batch` (`retries=1`, shared `load_document` per `(doc, raw_entry)` under the
  isolation helper). The per-feed body is INLINED in `ingest_substack_rss_feed_batch`'s
  cross-feed `gather(return_exceptions=True)` loop, preserving per-feed isolation (a bad
  feed is logged + skipped). `fetch_and_extract` is intentionally not imported here.
- **substack_article** — demoted the `ingest_substack_article` @flow body to the plain
  async core `_ingest_substack_article_one(url, user_id) -> Document | None` (NO
  decorators); `ingest_substack_article` is now a 1-line thin @flow wrapper used ONLY by
  the MCP router (`tree.data.ingest._ingest_substack_article`). Deleted the per-row
  `fetch_and_extract_task` / `load_article_document_task`. New batch shape over the whole
  URL list: `extract_batch` (network E+T FUSED, `retries=2`, via pure `fetch_and_extract`)
  → `load_batch` (`retries=1`, SHARED `load_article_document`), both single isolated
  gathers. `ingest_substack_article_batch` calls the batch tasks directly — NEVER the thin
  flow (no per-item sub-flow runs).
- **Shared helper** — per #079 §3, the gather-isolate-drop-failures shape now recurs 4
  times (rss `load_batch`, article `extract_batch`, article `load_batch`, + #078's
  `load_batch`), crossing the 4+ threshold, so I lifted it into
  `tree.data.batch.gather_isolated(items, work) -> (successes, failure_count)`. Used by
  the 3 NEW substack tasks. Did NOT retrofit #078's committed arxiv file (out of scope).
- Shared LOAD tail preserved: article load still delegates to `substack_rss.load_document`
  via `load_article_document` (reference resolution identical). No `persist_result` /
  `cache_policy` added (Prefect-3 off by default; noted in each module docstring). The
  pure cores (`substack_rss.py`, `substack_article.py`) and the pre-existing
  `except ValueError, TypeError:` are UNCHANGED (out of scope).

**Files modified**
- `apps/memory/src/tree/data/batch.py` — NEW shared `gather_isolated` isolation helper.
- `apps/memory/src/tree/data/substack/substack_rss_pipeline.py` — batch-grain rewrite;
  per-row tasks + per-feed sub-flow removed; `fetch_feed_task` / `transform_batch` /
  `load_batch` + per-feed-isolated batch flow.
- `apps/memory/src/tree/data/substack/substack_article_pipeline.py` — `_ingest_substack_article_one`
  core + thin `ingest_substack_article` @flow; `extract_batch` / `load_batch`; batch flow
  calls the batch tasks (not the thin flow).
- `apps/memory/tests/unit/data/test_batch.py` — NEW; covers the isolation helper.
- `apps/memory/tests/unit/data/substack/test_substack_rss_pipeline.py` — reworked for the
  batch shape (task metadata, no-re-fetch / no-sub-flow / no-per-row-task guards,
  transform/load isolation, one-fetch-per-feed, one-load_batch-per-feed, bad-feed isolation).
- `apps/memory/tests/unit/data/substack/test_substack_article_pipeline.py` — NEW; core +
  thin-flow split, extract/load isolation, batch-does-not-call-thin-flow.
- `apps/memory/tests/integration/data/substack/test_substack_rss_pipeline.py` — driven via
  the batch flow now; added `test_does_not_re_fetch_articles` (spies the article-scrape
  entry points → zero calls). Note: substack_rss + substack_article share the SAME `httpx`
  module, so the no-re-fetch proof spies `fetch_article` / `fetch_and_extract`, not the
  shared `httpx.AsyncClient`.
- `apps/memory/src/tree/data/{ingest.py,pipeline.py}` — UNCHANGED (confirmed imports
  resolve; the `_BATCHED_VARIANTS` no-mock guard test passes).

**Tests**
- Unit: 1640 passing, 0 failing, 0 warnings (`make memory-unit-tests`). New: 7 batch
  helper + 21 rss-pipeline + 17 article-pipeline tests. TDD red→green (helper red on
  `ModuleNotFoundError`; pipelines red on missing `_ingest_substack_article_one` /
  `load_batch` symbols).
- Integration: substack suite 8 passing (incl. slow `test_idempotent_on_rerun`); MCP
  `test_ingest_url_after_dispatcher_migration` passing. Full fast tail: 177 passed / 2
  failed / 1 skipped — the 2 failures (`test_indexing_pipeline::test_embeds_nodes`,
  `test_meta_state::test_updated_at_is_recent`) are the SAME pre-existing flakes #078's SWE
  + Tester logged (embeddings-convergence + `updated_at` timing); both PASS in isolation on
  this tree, both live in `memory/` with zero overlap with `tree.data.substack` /
  `tree.data.batch`.

**Acceptance criteria** — all non-HUMAN criteria verified:
- [x] RSS builds from feed-embedded content, no per-article scrape —
      `TestNoArticleReFetch`, `TestIngestSubstackRssFeedBatch::test_one_fetch_per_feed_no_article_scrape`,
      integration `test_does_not_re_fetch_articles`; e2e (feed_fetches=1, article_scrapes=0).
- [x] RSS per-row tasks removed, `transform_batch`(0)/`load_batch`(1), per-feed sub-flow
      folded with isolation — `TestTaskMetadata`, `TestNoArticleReFetch::test_per_feed_sub_flow_is_gone`,
      `::test_isolates_one_bad_feed`.
- [x] `_ingest_substack_article_one` plain core; `ingest_substack_article` thin @flow;
      batch does not call it — `TestIngestOne::test_is_a_plain_function_not_a_flow_or_task`,
      `TestThinFlow::test_delegates_to_core`, `TestIngestSubstackArticleBatch::test_does_not_call_thin_flow`.
- [x] `extract_batch`(2)/`load_batch`(1) single isolated gathers over the URL list —
      `TestExtractBatch`, `TestLoadBatch`, `TestTaskAndFlowMetadata`.
- [x] Shared LOAD tail preserved — `TestLoadBatch::test_returns_persisted_subset...`
      asserts delegation to `load_article_document` with the body HTML; integration ref-doc
      + latent-upgrade tests still green.
- [x] Stable seams unchanged — `test_pipeline.py` batched-variant no-mock guard passes;
      `ingest.py`/`pipeline.py` untouched; MCP `ingest_url` route green; e2e single-URL via
      `ingest_url` persists 1 doc.
- [x] format/lint/pre-commit clean; unit 0 warnings; integration fast tail (substack)
      ingests N docs / persists via the core.
- [ ] [HUMAN] Deferred to #082: Prefect UI shows a substack worker with batch ETL-phase
      tasks and NO per-article/per-feed sub-flow runs.

**Evidence**
```
$ make memory-unit-tests
============================ 1640 passed in 55.49s =============================

$ make memory-format-check && make memory-lint-check
285 files already formatted   /   All checks passed!

$ make pre-commit
prettier ... Passed   ruff check ... Passed   ruff format ... Passed   biome ... Passed

$ uv run pytest tests/integration/data/substack/ tests/integration/mcp/test_ingest_url_after_dispatcher_migration.py
8 passed in 6.17s

$ make memory-integration-tests   (full fast tail)
===== 2 failed, 177 passed, 1 skipped, 104 deselected in 165.47s =====
# the 2 = pre-existing memory/ flakes; re-run in isolation:
$ uv run pytest <test_embeds_nodes> <test_updated_at_is_recent>
2 passed in 7.25s

# E2E (real local Mongo, real Prefect engine, network boundary mocked):
RSS: returned=3 db_substack=3 feed_fetches=1 article_scrapes=0
ARTICLE batch: returned=4 db=4 thin_flow_calls=0
ARTICLE isolation: returned=2 (1 of 3 URLs failed scrape, skipped)
MCP ingest_url: returned_doc=True db=1
E2E 079 OK: all paths verified
```

**Notes**
- Shared-helper decision: EXTRACTED `tree.data.batch.gather_isolated` (4+ call sites now,
  per #079 §3) rather than re-inlining. #078's arxiv file was NOT retrofitted (committed +
  out of scope) — a follow-up could migrate it onto the helper to fully DRY.
- Out-of-scope stale comment: `tests/unit/data/web/test_web_pipeline.py:36` references
  `substack_article_pipeline.fetch_and_extract_task`, which this task removed. The web
  pipeline is not in #079's scope and its own `fetch_and_extract_web_task` is unchanged, so
  I left it — flagging for the orchestrator (a one-line comment fix could be a rollup).
- The `make memory-integration-tests` exit code is non-zero only due to the 2 pre-existing
  unrelated flakes; the substack-specific suite is fully green.
- NOT COMMITTED — handed to Tester for review.

### [Tester] 2026-06-23 — QA

**Test summary**
- Format / lint / pre-commit: PASS (`285 files already formatted`; `All checks passed!`;
  prettier/ruff-check/ruff-format/biome all Passed)
- Unit tests: 1640 passed / 0 failed — 0 warnings (`make memory-unit-tests`, 55.97s)
- Integration (fast tail): 176 passed / 3 failed / 1 skipped / 104 deselected. ALL 3
  failures are in files UNTOUCHED by #079 (none import `tree.data.batch` /
  `tree.data.substack`) and PASS or SKIP in isolation — proven unrelated (see below).
- Substack integration + MCP `ingest_url`: 8 passed (incl. slow `test_idempotent_on_rerun`).
- Warnings: 0.

**The 3 fast-tail failures are all pre-existing / environmental + disjoint from #079:**
- `memory/test_indexing_pipeline::test_embeds_nodes` + `memory/test_meta_state::test_updated_at_is_recent`
  — the SAME two #078 flakes (embeddings-convergence + `updated_at` timing). Re-ran both in
  isolation: `2 passed in 7.41s`. Both live in `memory/`, zero overlap with this task.
- `data/web/test_web_serp::test_returns_results_with_titles_and_urls` — a LIVE Bright Data
  network test (docstring: "Live integration tests"). Run in ISOLATION it SKIPS
  (`1 skipped` — `pytest.mark.skipif` on placeholder creds); the full-tail "failure" is a
  flaky live SERP call. `web_serp.py` is untouched by #079. Environmental, not a regression.

**E2E adversarial pass** (live local Mongo + real Prefect engine; network boundary mocked)
- Happy path (real `@flow`): `ingest_substack_article(url, uid)` → ingested Document
  `https://real.sub.com/p/single` (PASS); duplicate (load→None) → returns `None`, no crash (PASS).
- Happy path (real `@flow`): `ingest_substack_rss_feed_batch([feed], uid)` → feed_fetches=1,
  article_scrapes=0, docs=1 (PASS) — proves NO per-article re-scrape end-to-end.
- Break 1 (helper — all elements fail): `gather_isolated([1,2,3,4], boom)` → `([], 4)`, no
  propagation (PASS).
- Break 2 (helper — falsy-not-None correctness): work returning `0`/`""`/`[]`/`5` →
  `([0,"",[],5], 0)` — only `None` dropped, falsy results KEPT (PASS). This is the subtle
  correctness edge; the impl uses `result is not None` (correct), not a truthiness check.
- Break 3 (helper — empty + large): `gather_isolated([], _)` → `([],0)`; 1000-elem batch
  every-7th-fails → exact survivor set, order preserved, 142 failures counted (PASS).
- Break 4 (article extract isolation): inject 1 bad URL of 3 → 2 survive, bad skipped+logged,
  task does NOT raise (PASS).
- Break 5 (article malformed input): inject `None` URL → isolated, 1 valid survives (PASS).
- Break 6 (article batch grain + no sub-flow): `ingest_substack_article_batch` over 10/3 URLs →
  thin `ingest_substack_article` spy await_count=0; extract_batch + load_batch each awaited
  once over the whole list (PASS).
- Break 7 (RSS per-feed isolation): 1 bad feed of 2 → bad logged+skipped, good feed ingests
  1 doc, flow does not sink (PASS).
- NOTE (helper, not a defect): `gather_isolated` keys isolation off
  `asyncio.gather(return_exceptions=True)`, which by Python semantics does NOT capture
  `KeyboardInterrupt`/`SystemExit`/`CancelledError` (BaseException-not-Exception) — those
  propagate (correct: a batch SHOULD abort on Ctrl-C / cancellation, not swallow it). All
  real work units (`load_document`, `fetch_and_extract`) raise normal `Exception` subclasses,
  which ARE captured and isolated. The `isinstance(result, BaseException)` post-loop is
  slightly broader than gather can deliver — a cosmetic smell, not a bug. PASS with note.

**Acceptance criteria** — all non-HUMAN verified with evidence:
- [x] PASS — RSS builds from feed-embedded content, NO per-article scrape (one `fetch_feed`
      per feed) — `substack_rss_pipeline.py` imports only `extract_document`/`fetch_feed`/
      `load_document` (no `fetch_and_extract`); unit `TestNoArticleReFetch::test_does_not_import_fetch_and_extract`;
      integration `test_does_not_re_fetch_articles` (spies → 0 calls); e2e real-flow feed_fetches=1/article_scrapes=0.
- [x] PASS — per-row tasks removed; `transform_batch`(retries=0)/`load_batch`(retries=1);
      inner `ingest_substack_rss_feed` sub-flow gone, folded into per-feed loop w/ isolation —
      `TestTaskMetadata`, `test_per_row_tasks_are_gone`, `test_per_feed_sub_flow_is_gone`,
      e2e RSS-isolation (1 bad feed of 2 skipped).
- [x] PASS — `_ingest_substack_article_one` plain core (no `.fn`); `ingest_substack_article`
      thin `@flow`; batch does not call it — `TestIngestOne::test_is_a_plain_function...`,
      `TestThinFlow::test_delegates_to_core`, e2e thin_spy await_count=0.
- [x] PASS — `extract_batch`(retries=2)/`load_batch`(retries=1) single isolated gathers per
      URL list — `substack_article_pipeline.py:76,99`; `TestExtractBatch`/`TestLoadBatch`;
      e2e article-isolation + malformed-input.
- [x] PASS — shared LOAD tail preserved: `load_article_document` → `substack_rss.load_document`
      (`substack_article.py:145`, UNCHANGED); pure cores not in diff.
- [x] PASS — stable seams: `ingest_substack_rss_feed_batch(feed_urls, user_id)` /
      `ingest_substack_article_batch(article_urls, user_id)` signatures intact;
      `test_pipeline.py::test_every_batched_variant_resolves_without_mocks` PASS;
      `ingest.py`/`pipeline.py` UNCHANGED; MCP `ingest_url` route green; e2e single-URL ingests.
- [x] PASS — format/lint/pre-commit clean (evidence above).
- [x] PASS — `make memory-unit-tests`: 1640 passed, 0 warnings.
- [x] PASS — `make memory-integration-tests` (fast tail): substack batch flow ingests N docs
      for N-entry feed (`test_ingests_documents_via_prefect_flow` → 3); article batch persists
      via the core. The 3 fast-tail failures are proven unrelated/environmental.
- [ ] [HUMAN] Deferred to #082 — Prefect UI substack worker batch ETL-phase tasks / no
      per-item sub-flow runs. Awaiting human verification.

**Evidence**
```
$ make memory-unit-tests
============================ 1640 passed in 55.97s =============================

$ make memory-format-check && make memory-lint-check
285 files already formatted   /   All checks passed!

$ uv run pytest tests/integration/data/substack/ tests/integration/mcp/test_ingest_url_after_dispatcher_migration.py
8 passed in 3.60s

$ make memory-integration-tests
===== 3 failed, 176 passed, 1 skipped, 104 deselected in 173.42s =====
# 3 failed = 2 #078 memory/ flakes + 1 live-SERP net test; all in untouched files:
$ uv run pytest <test_embeds_nodes> <test_updated_at_is_recent>   -> 2 passed in 7.41s
$ uv run pytest <test_web_serp>                                   -> 1 skipped (placeholder creds)

# E2E adversarial (live Mongo + real Prefect engine):
HELPER: empty/all-fail/mixed/falsy-kept/1000-elem  -> ALL PASS
FLOWS: article-isolation, malformed-None, batch-no-thin-flow, rss-isolation -> ALL PASS
PREFECT: MCP-happy, MCP-duplicate, RSS feed_fetches=1/article_scrapes=0 -> ALL PASS
```

**Other issues found** (non-blocking)
- Stale COMMENT confirmed harmless: `tests/unit/data/web/test_web_pipeline.py:36`
  (`# Mirrors substack_article_pipeline.fetch_and_extract_task`) is PROSE only inside
  `test_fetch_task_retries`, which asserts on `fetch_and_extract_web_task` (unchanged). It is
  NOT an import/patch and cannot break the test — `test_web_pipeline.py` runs `11 passed`.
  Cosmetic; out of #079 scope. A rollup could drop the dangling reference.
- `gather_isolated` `isinstance(result, BaseException)` is broader than
  `gather(return_exceptions=True)` can deliver (it never returns BaseException-not-Exception);
  could be tightened to `Exception` to match reality, but behavior is correct. Cosmetic.
- `AGENTS.md` is modified in the working tree (owner WIP, env-switching + rule-discipline
  docs) — UNRELATED to #079, left untouched per instructions. Flagging so it isn't swept into
  this task's commit.
- Follow-up (already flagged by SWE): #078's arxiv file still inlines the isolation shape and
  could migrate onto the new shared `gather_isolated` to fully DRY.

**VERDICT: PASS**
