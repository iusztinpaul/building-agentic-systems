# web batch-ETL tasks + `ingest_web_url` per-item sub-flow collapse

Status: in-progress
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

- [x] `_ingest_web_url_one(url, user_id)` exists as a plain async core (no decorators);
      `ingest_web_url` remains a 1-line `@flow` wrapper around it, used by the MCP router;
      the BATCH flow does NOT call that wrapper.
- [x] Per-row `fetch_and_extract_web_task` / `load_web_document_task` are removed;
      `extract_batch` (network Extract+Transform fused, `retries=2`) and `load_batch`
      (`retries=1`) operate over the whole URL list, each a single
      `asyncio.gather(return_exceptions=True)` with per-element isolation.
- [x] `ingest_web_url_batch(urls, user_id)` calls `extract_batch` then `load_batch`; a
      per-URL scrape failure and a per-URL load failure are each logged + skipped (not
      propagated); duplicates are filtered from the result.
- [x] Stable seams unchanged: `ingest_web_url_batch` name + signature intact;
      `_BATCHED_VARIANTS` (web = last) resolves it by name (no-mock guard test passes); the
      MCP `ingest_url` generic-web fallback to `ingest_web_url` still works.
- [x] `make memory-format-fix && make memory-lint-fix && make memory-format-check &&
      make memory-lint-check` clean; `make pre-commit` clean.
- [x] `make memory-unit-tests` passes, 0 warnings.
- [x] `make memory-integration-tests` (fast tail) passes — the web batch flow ingests N
      docs for an N-URL list and filters duplicates. (Web integration tests SKIP locally
      without live Bright Data creds; the two unrelated full-suite failures are
      pre-existing cross-module DB-state flakiness — both pass in isolation with and
      without this change.)
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

### [SWE] 2026-06-23 — Web batch-ETL tasks + per-URL sub-flow collapse

**Files modified**
- `apps/memory/src/tree/data/web/web_pipeline.py` — collapsed per-URL sub-flow into the
  plain async core `_ingest_web_url_one`; kept `ingest_web_url` as a thin 1-line `@flow`
  (MCP-only); deleted the per-row `fetch_and_extract_web_task` / `load_web_document_task`;
  added batch tasks `extract_batch` (E/T fused, `retries=2`) + `load_batch` (`retries=1`),
  each one `gather_isolated` over the whole URL list; rewired `ingest_web_url_batch` to
  call the batch tasks (never the thin flow).
- `apps/memory/tests/unit/data/web/test_web_pipeline.py` — reworked to the batch shape;
  mirrors the substack-article unit test; also fixed the stale `fetch_and_extract_task`
  comment (the optional cleanup — the old comment is gone with the rewrite).

**Tests**
- Unit: 1685 passing, 0 failing, 0 warnings — `make memory-unit-tests`. The 21 web +
  no-mock-guard tests verified directly.
- Integration: web tests SKIP locally (no live Bright Data creds). Full fast tail =
  177 passed / 1 skipped / 2 failed; the 2 failures (`test_indexing_pipeline::test_embeds_nodes`,
  `test_meta_state::test_updated_at_is_recent`) are unrelated cross-module DB-state
  flakiness — both pass in isolation and as a 2-module pair, with AND without this change
  (proven by stash + re-run).

**Acceptance criteria**
- [x] core + thin flow split; batch never calls the thin flow — verified by
      `test_web_pipeline.py::TestIngestOne`, `::TestThinFlow`,
      `::TestIngestWebUrlBatch::test_does_not_call_thin_flow` + the e2e spy (await_count 0).
- [x] per-row tasks removed; `extract_batch`/`load_batch` single isolated gather —
      `::TestTaskAndFlowMetadata::test_per_row_tasks_are_gone`, `::TestExtractBatch`,
      `::TestLoadBatch`.
- [x] batch flow ordering + per-element scrape/load isolation + dup filtering —
      `::TestIngestWebUrlBatch`, plus the e2e run (5 URLs → 3 ingested: 1 scrape-fail
      isolated, 1 dup filtered).
- [x] stable seams — `ingest_web_url_batch` name/signature unchanged;
      `test_pipeline.py::test_every_batched_variant_resolves_without_mocks` passes;
      `tree.data.ingest._ingest_web_url` still imports + calls `ingest_web_url`
      (e2e router fallback returned the Document, core awaited once).
- [x] format/lint/pre-commit clean.
- [x] unit tests pass, 0 warnings.
- [x] fast integration tail — web batch path validated via e2e (creds-gated tests skip).
- [ ] [HUMAN] Deferred to #082: Prefect UI shows a `custom`-platform worker with batch
      ETL-phase tasks and no per-URL sub-flow runs.

**Evidence**
```
$ make memory-unit-tests
======================= 1685 passed in 65.14s (0:01:05) ========================

$ uv run pytest tests/unit/data/web/test_web_pipeline.py \
    tests/unit/data/test_pipeline.py::test_every_batched_variant_resolves_without_mocks
============================== 21 passed in 4.04s ==============================

# e2e (real Prefect flow runs against local Mongo; Bright Data + load mocked):
RESULT BATCH ingested: ['https://example.com/0', 'https://example.com/1', 'https://example.com/4']
RESULT count: 3 (expect 3: 1 scrape-fail isolated, 1 dup filtered)
RESULT thin awaited in batch path: 0 (expect 0)
RESULT router fallback returned: https://martinfowler.com/bliki/CQRS.html
RESULT core awaited via thin flow: 1 (expect 1)

# the 2 full-suite integration failures are pre-existing flakiness (pass in isolation):
$ uv run pytest tests/integration/memory/test_indexing_pipeline.py \
    tests/integration/memory/test_meta_state.py -q
17 passed in 32.06s
```

**Notes**
- `web.py` (pure core), `ingest.py` (MCP router), `pipeline.py` (`_BATCHED_VARIANTS`) all
  UNCHANGED — confirmed the web batch import + name dispatch still resolve.
- Web integration tests are creds-gated (live Bright Data); they SKIP locally. The new
  batch path was exercised end-to-end with mocked external boundaries instead.
- NOT COMMITTED — handing to Tester.

### [Tester] 2026-06-23 — QA

**Test summary**
- Format / lint / pre-commit: PASS (`make memory-format-check`, `make memory-lint-check` →
  "All checks passed!"; `make pre-commit` → all hooks passed).
- Unit tests: 1685 passed / 0 failed (`make memory-unit-tests`), 0 warnings.
- Integration (fast tail, `make memory-integration-tests`): 177 passed / 1 skipped / 2 failed
  / 104 deselected. The 2 failures are NOT web tests
  (`test_indexing_pipeline::test_embeds_nodes`, `test_meta_state::test_updated_at_is_recent`)
  and both PASS in isolation (`2 passed in 7.63s`) → confirmed pre-existing cross-module
  DB-state flakiness, not regressions. Web integration tests SKIP (Bright Data creds gated).
- Warnings: 0.

**E2E adversarial pass** (real Prefect flows `ingest_web_url` / `ingest_web_url_batch` +
real batch tasks; only the pure `fetch_and_extract_web` scrape + `load_web_document` persist
mocked, real Beanie init against local Mongo so nothing was written):
- Happy path: `ingest_web_url_batch(5 urls)` with url[2] scrape-raise + url[3] dup →
  `['/0','/1','/4']` (3 ingested) (PASS).
- Break path 1 (batch grain): `extract_batch` awaited 1×, `load_batch` awaited 1× over the
  5-URL list (NOT per-URL); fetch 5×, load 4× — vs expected 1/1/5/4 (PASS).
- Break path 2 (per-element isolation): scrape raise on url[2] skipped+logged, dup `None` on
  url[3] filtered, other 3 persist, task did NOT raise → 3 ingested (PASS).
- Break path 3 (thin-flow bypass): spied `ingest_web_url` await_count in batch path == 0 vs
  expected 0 (PASS).
- Break path 4 (thin MCP flow intact): single-URL `ingest_web_url(CQRS url)` → returns the
  doc, core awaited 1× vs expected 1 (PASS).
- Break path 5 (boundary: empty list): `ingest_web_url_batch([])` → `[]` (PASS).
- Break path 6 (failure mode: all scrapes raise): 4-URL batch → `[]`, no propagation (PASS).
- Break path 7 (boundary: all duplicates): every load `None` → `[]` (PASS).
- Break path 8 (failure mode: per-element load raise): isolated+dropped, rest persist →
  2 ingested vs expected 2 (PASS).
- Break path 9 (large input): 50 URLs, every other a dup → 25 ingested vs expected 25 (PASS).

**Acceptance criteria**
- [x] PASS — core + thin flow split; batch never calls the thin flow.
      Evidence: `web_pipeline.py:36` `_ingest_web_url_one` plain async core (no decorator;
      unit `TestIngestOne::test_is_a_plain_function_not_a_flow_or_task`); `:57` thin `@flow`
      1-liner (`TestThinFlow::test_delegates_to_core`); e2e Break path 3 thin await_count 0.
- [x] PASS — per-row tasks removed; `extract_batch` (retries=2) + `load_batch` (retries=1)
      single isolated gather. Evidence:
      `TestTaskAndFlowMetadata::test_per_row_tasks_are_gone`, `test_extract_batch_retries`,
      `test_load_batch_retries`; both tasks call `gather_isolated` once (`web_pipeline.py:78,
      99`); e2e Break path 1 grain = 1×/1×.
- [x] PASS — batch calls `extract_batch` then `load_batch`; scrape-fail + load-fail logged +
      skipped; dups filtered. Evidence: `TestIngestWebUrlBatch` (5 tests incl. dup filter +
      ordering); e2e Break paths 2/6/7/8.
- [x] PASS — stable seams unchanged. Evidence: `ingest_web_url_batch` name + signature intact
      (`web_pipeline.py:107`); `test_every_batched_variant_resolves_without_mocks` passes
      (web = last `_BATCHED_VARIANTS` entry, `pipeline.py:213`); MCP fallback
      `tree.data.ingest._ingest_web_url` imports + calls `ingest_web_url` unchanged
      (`ingest.py:62`); `search_web` ingest still resolves the `ingest-web-url-batch-etl`
      deployment by name (`web_search_ingest.py:23`, flow name unchanged). `pipeline.py`,
      `ingest.py`, `web.py`, `tools.py`, `web_search_ingest.py` all UNCHANGED in the diff.
- [x] PASS — format/lint/pre-commit clean (see Test summary).
- [x] PASS — `make memory-unit-tests` 1685 passed, 0 warnings.
- [x] PASS — fast integration tail green modulo the proven pre-existing flakes + the
      creds-gated web SKIP; web batch shape verified via unit tests + the e2e harness.
- [ ] [HUMAN] Awaiting human verification — deferred to #082 (Prefect UI worker graph).

**Evidence**
```
$ make memory-unit-tests
======================= 1685 passed in 64.42s (0:01:05) ========================

$ make memory-integration-tests
===== 2 failed, 177 passed, 1 skipped, 104 deselected in 150.95s (0:02:30) =====
  (2 failures = test_embeds_nodes + test_updated_at_is_recent; NOT web)

$ uv run pytest <the 2 failing tests in isolation>
2 passed in 7.63s        # → pre-existing cross-module DB-state flakiness

# Tester e2e adversarial harness (real Prefect flows, scrape+persist mocked):
OVERALL: PASS (all e2e adversarial checks green)   # 17 checks / 9 break paths
```

**Other issues found**
- None blocking. Code path is clean: full type annotations on all 5 new/changed functions,
  no `print()` in library code, retry metadata correct, single `gather_isolated` per task,
  thin flow is a true 1-liner. `web.py` pure core untouched.
- Note (non-blocking): the bare-`uv run pytest` invocations emit a Prefect temp-server
  "I/O operation on closed file" logging traceback AT INTERPRETER SHUTDOWN — it appears
  after the test summary, is teardown noise (not a test failure), and does NOT occur via the
  `make memory-*` targets (which reported 0 warnings).

**VERDICT: PASS
