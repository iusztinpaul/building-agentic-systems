# web batch-ETL tasks + `ingest_web_url` per-item sub-flow collapse

Status: pending
Tags: `data`, `prefect`, `refactor`
Depends on: #078
Blocks: #082

## Scope

Apply the #078 batch-ETL pattern to the generic web (Bright Data) leaf pipeline and collapse
its per-item sub-flow. The web fetch is a single Bright Data Web Unlocker scrape that yields
the Document, so Extract+Transform FUSE into one `extract_batch` task; Load is a separate
`load_batch`. The per-item sub-flow `ingest_web_url` collapses into a plain core fn
`_ingest_web_url_one`, with a THIN MCP-only `@flow` wrapper retained (the web pipeline is
both the data-pipeline's last batched variant AND the `ingest_url` router's generic-web
fallback). The batch-flow name + signature
`ingest_web_url_batch(urls, user_id) -> list[Document]` is an UNCHANGED stable seam.

### 1. Collapse the per-item sub-flow; keep a thin MCP flow

In `web_pipeline.py`:

- Demote the body of the `ingest_web_url` `@flow` into a plain async core
  `_ingest_web_url_one(url, user_id) -> Document | None` (fetch+extract via the pure
  `web.fetch_and_extract_web`, then load via the pure `web.load_web_document`). NO
  decorators on the core.
- Keep `ingest_web_url(url, user_id) -> Document | None` as a 1-line `@flow` wrapper around
  `_ingest_web_url_one`, used ONLY by the MCP URL router (`tree.data.ingest._ingest_web_url`,
  the generic-web fallback). MCP single-URL ingest still gets its own Prefect flow run +
  Opik trace.

### 2. Batch ETL-phase tasks over the whole URL list

DELETE the per-row `fetch_and_extract_web_task` / `load_web_document_task` `@task`s. The
handed-in `urls` list is treated as ONE batch (volume is tens):

- **`extract_batch`** (`@task`, network Extract+Transform FUSED, `retries=2`,
  `retry_delay_seconds=5`) — `(list[str], user_id) → list[Document]`: scrapes each URL via
  the pure `web.fetch_and_extract_web` under `asyncio.gather(return_exceptions=True)`;
  per-URL scrape failures logged + skipped (NEVER sink the batch).
- **`load_batch`** (`@task`, `retries=1`, `retry_delay_seconds=2`) — `list[Document] →
  list[Document]`: awaits the pure `web.load_web_document` per element under
  `asyncio.gather(return_exceptions=True)`; returns the successful non-`None` subset (drops
  duplicates), logs a per-batch failure COUNT.

`ingest_web_url_batch(urls, user_id)` keeps `init_mongodb` once at the top, then `docs =
await extract_batch(urls, user_id)` → `await load_batch(docs)`. The batch path MUST NOT call
the thin `ingest_web_url` flow (no per-item sub-flow runs).

### 3. Per-element isolation + shared helper

Same isolation contract as #078. If the `tree.data.batch` isolation helper was extracted in
#079/#080, reuse it here; else inline. `load_web_document` is idempotent (LATENT-upgrade /
dedup on `(user_id, source_uri)` / `DuplicateKeyError` race handling), so a batch-wide
`load_batch` retry is safe.

### Files touched

- `apps/memory/src/tree/data/web/web_pipeline.py` — add `_ingest_web_url_one` core; keep
  `ingest_web_url` as a thin MCP-only `@flow`; replace per-row tasks with `extract_batch` +
  `load_batch`; rewire `ingest_web_url_batch` to call the core via batch tasks (NOT the thin
  flow).
- `apps/memory/src/tree/data/web/web.py` — UNCHANGED pure core
  (`fetch_and_extract_web`, `load_web_document`).
- `apps/memory/src/tree/data/ingest.py` — UNCHANGED (imports + calls the thin
  `ingest_web_url`). Confirm import resolves.
- `apps/memory/src/tree/data/pipeline.py` — UNCHANGED. The `WebSource` batched variant
  (`ingest_web_url_batch`, the last/catch-all entry of `_BATCHED_VARIANTS` per #075) keeps
  the same name + signature.
- `apps/memory/tests/unit/data/web/test_web_pipeline.py` — rework for the batch shape +
  core/thin-flow split (see Test guidance).
- `apps/memory/tests/integration/data/web/test_web_pipeline.py` — keep flow-level
  assertions; adjust patch targets only if moved.

## Acceptance Criteria

- [ ] `_ingest_web_url_one(url, user_id)` exists as a plain async core (no decorators);
      `ingest_web_url` remains a 1-line `@flow` wrapper around it, used by the MCP router;
      the BATCH flow does NOT call that wrapper.
- [ ] Per-row `fetch_and_extract_web_task` / `load_web_document_task` are removed;
      `extract_batch` (network Extract+Transform fused, `retries=2`) and `load_batch`
      (`retries=1`) operate over the whole URL list, each a single
      `asyncio.gather(return_exceptions=True)` with per-element isolation.
- [ ] `ingest_web_url_batch(urls, user_id)` calls `extract_batch` then `load_batch`; a
      per-URL scrape failure and a per-URL load failure are each logged + skipped (not
      propagated); duplicates are filtered from the result.
- [ ] Stable seams unchanged: `ingest_web_url_batch` name + signature intact;
      `_BATCHED_VARIANTS` (web = last) resolves it by name (no-mock guard test passes); the
      MCP `ingest_url` generic-web fallback to `ingest_web_url` still works.
- [ ] `make memory-format-fix && make memory-lint-fix && make memory-format-check &&
      make memory-lint-check` clean; `make pre-commit` clean.
- [ ] `make memory-unit-tests` passes, 0 warnings.
- [ ] `make memory-integration-tests` (fast tail) passes — the web batch flow ingests N
      docs for an N-URL list and filters duplicates.
- [ ] [HUMAN] Deferred to #082: Prefect UI shows a `custom`-platform worker with batch
      ETL-phase tasks and no per-URL sub-flow runs.

## BDD scenarios

### Scenario: the web batch path creates no per-item sub-flow
- **Given** a list of 5 URLs
- **When** `ingest_web_url_batch` runs
- **Then** the `ingest_web_url` thin `@flow` is NOT invoked; `extract_batch` + `load_batch`
  each run once over the 5 URLs.

### Scenario: the thin MCP flow ingests a single URL
- **Given** the MCP `ingest_url` router falls back to the generic-web pipeline for an
  arbitrary URL
- **When** it calls `ingest_web_url(url, user_id)`
- **Then** the URL is scraped + persisted (returns the Document or `None` for a duplicate) —
  single-URL MCP ingest still gets its own flow run.

### Scenario: a failed scrape is isolated
- **Given** one of 5 URLs raises during `fetch_and_extract_web`
- **When** `extract_batch` runs
- **Then** that URL is logged + skipped, the other 4 are extracted, and the task does not
  hard-fail.

### Scenario: a duplicate is filtered, the batch survives a load retry
- **Given** `load_web_document` returns `None` for a duplicate URL and the load task
  hard-fails once on a transient error
- **When** `load_batch` runs and Prefect retries it (`retries=1`)
- **Then** duplicates are filtered from the result and the retry does not double-insert
  (LATENT-upgrade / `(user_id, source_uri)` dedup).

## User Stories

### Story: An operator sees a clean web worker graph
1. The operator runs the data pipeline with several arbitrary URLs configured (the `custom`
   platform).
2. They inspect the `data-etl-worker` `custom` run in the Prefect UI.
3. They see one `extract-batch` + one `load-batch` task over the URL list — not a per-URL
   sub-flow + per-URL task pair.

### Story: A user ingests one arbitrary URL from the assistant
1. The user pastes a non-Substack, non-YouTube URL to the MCP `ingest_url` tool.
2. The router falls back to the thin `ingest_web_url` flow, which scrapes + persists the one
   URL via Bright Data.
3. The user gets back a single ingested Document (or a no-op) — unchanged from before.

### Story: A maintainer confirms web matches the other batch pipelines
1. A maintainer reads `web_pipeline.py` after this task.
2. They see the same shape as arXiv/substack/youtube: a fused `extract_batch`, a separate
   `load_batch`, per-element isolation, and a thin MCP-only flow over a `_ingest_<x>_one`
   core.
3. The four leaf pipelines now read uniformly.

## Test guidance

- Call `/testing-python`. Run ONLY via `make memory-*` (LOCAL env).
- Unit (`test_web_pipeline.py`): rework — drop the removed per-row task tests; add
  `extract_batch.fn(urls, user_id)` with the pure `fetch_and_extract_web` patched
  `side_effect=[doc_a, RuntimeError, doc_c]` → returns `[doc_a, doc_c]` (isolated);
  `load_batch.fn(docs)` with `load_web_document` patched `side_effect=[doc_a, None]` →
  `[doc_a]` (duplicate filtered), and with a raise → isolated. Test `_ingest_web_url_one`
  directly; test the thin `ingest_web_url.fn` delegates to the core; assert the batch flow
  does NOT call the thin flow. Keep the `test_initialises_mongodb_once` /
  `test_empty_url_list` flow tests (adapt to the new internals).
- Retry-metadata asserts (keep `TestTaskAndFlowMetadata` style): `extract_batch.retries ==
  2`, `load_batch.retries == 1`, plus the new task names.
- Integration: keep flow-level persist assertions against `mongo_client`; verify duplicate
  filtering still works end-to-end.

---

Blocked by: #078

## Log

### [PA] 2026-06-23 — Grooming

**Summary**
Batch-ETL the generic web pipeline and collapse its per-item sub-flow. Extract+Transform
fuse into `extract_batch` (one Bright Data scrape yields the Document); Load is a separate
`load_batch`; both isolate per-element failures. `ingest_web_url`'s body becomes
`_ingest_web_url_one`; a thin `ingest_web_url` `@flow` is retained for the MCP router's
generic-web fallback only. The `ingest_web_url_batch` seam (web = last batched variant, #075)
is unchanged.

**Key decisions**
- Web Extract+Transform FUSE (single scrape → Document) per the brief's pragmatic E/T/L
  rule → one `extract_batch`; Load always separate.
- Thin MCP flow retained for BOTH `ingest_url` callers of web (the explicit fallback in
  `tree.data.ingest`); batch path calls the core directly — no per-item sub-flow runs.
- `load_web_document` is idempotent (LATENT-upgrade + dedup + DuplicateKeyError race), so
  the batch-wide `load_batch` retry is safe.
- Reuse the `tree.data.batch` isolation helper if an earlier task extracted it; else inline.

**Dependencies**
- #078 — establishes the batch-task + isolation pattern. (Independent of #079/#080; depends
  only on #078.)

**User stories**
- 3 stories: operator sees a clean web worker graph; user ingests one URL via MCP; a
  maintainer confirms web reads like the other three.

**Documentation discipline**
- Canonical glossary terms (Batch, ETL-phase task, Thin MCP flow, URL router). No new terms.

Ready for implementation.
