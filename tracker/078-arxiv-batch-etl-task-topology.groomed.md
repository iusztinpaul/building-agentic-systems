# arxiv (HF) batch-ETL task topology — kill the per-row task explosion

Status: pending
Tags: `data`, `prefect`, `refactor`
Depends on: None
Blocks: #079, #080, #081, #082

## Scope

Convert the arXiv HuggingFace leaf pipeline (`ingest_arxiv_dataset`) from PER-ROW Prefect
tasks to BATCH-grain ETL-phase tasks, and ESTABLISH the reusable batch-task +
per-element-isolation pattern that #079–#081 follow. This is THE exploder: with the live
config (`max_samples: 1000`, `batch_size: 50`, `num_workers: 2`), each HF **Window**
worker today emits ~1000 Prefect task runs (`extract-arxiv-document` per row +
`load-arxiv-document` per row, plus `fetch-arxiv-paper-content` per row when
`fetch_content`). After this task a window worker emits a few TENS of task runs (a small
constant per `batch_size`-chunk, not per row). The flow-level topology
(orchestrator → worker → `ingest_arxiv_dataset`) and ALL stable seams are UNCHANGED.

### 1. Replace per-row `@task`s with batch-grain ETL-phase tasks

In `arxiv_dataset_pipeline.py`, DELETE the three per-row task wrappers
(`extract_document`, `fetch_paper_content`, `load_document` — the `@task`-decorated
functions at lines 77–92) and the `_process_document` per-row helper (lines 95–105).
Replace with ETL-phase tasks that each operate over ONE `batch_size`-chunk (the list of
raw dicts `fetch_dataset_batches` already yields):

- **`transform_batch`** — a `@task` (pure map, `retries=0`): `list[dict] → list[Document]`.
  Maps each raw entry through the existing pure `arxiv_dataset.extract_document(raw, user_id)`
  (import the core fn, do NOT re-wrap it as a task). Drops `None` results (entries with no
  id) WITH the existing warning. No network, no DB → no retries.
- **`enrich_batch`** — a `@task` (network Extract, `retries=2`, `retry_delay_seconds=5`):
  `list[Document] → list[Document]`, invoked ONLY when `fetch_content` is true. Fetches
  paper HTML per element under the EXISTING `asyncio.Semaphore(concurrency)` via
  `asyncio.gather(return_exceptions=True)`; sets `doc.content` on success; per-element
  fetch failures are logged + the doc passes through with empty content (NEVER sinks the
  batch). Preserves today's concurrency bound. When `fetch_content` is false this task is
  not called at all.
- **`load_batch`** — a `@task` (DB Load, `retries=1`, `retry_delay_seconds=2`):
  `list[Document] → list[Document]`. Awaits the existing pure
  `arxiv_dataset.load_document(doc)` per element under
  `asyncio.gather(return_exceptions=True)`; returns the successful, non-`None` subset
  (drops duplicates) + logs a per-batch failure COUNT. A per-element load failure is
  logged + skipped, NOT propagated. Load is ALWAYS its own task.

Per-element isolation rule (uniform across all three): bad-DATA / per-element transient
failures are caught by `gather(return_exceptions=True)`, logged at WARNING, and the
element is skipped — the task returns the successful subset. The task hard-fails (and
Prefect retries the WHOLE batch) only on a batch-WIDE infra failure — SAFE because
`load_document` dedups on `(user_id, source_uri)` so a retried batch never double-inserts.

### 2. Rework the flow body to call batch tasks per chunk

`ingest_arxiv_dataset` keeps its signature `(user_id, max_samples=None, fetch_content=None,
offset=None) -> list[Document]`, its `_get_huggingface_arxiv_defaults` resolution, its
`init_mongodb` boundary, its `offset` windowing (#071), and the streamed
`fetch_dataset_batches(max_samples, batch_size, offset=offset)` loop (Extract stays the
flow loop — streamed read, NOT a task). Per yielded chunk: `docs = transform_batch(chunk,
user_id)`; if `fetch_content`: `docs = await enrich_batch(docs, concurrency)`;
`ingested = await load_batch(docs)`; accumulate. The chunk loop and final logging are
preserved.

### 3. Per-element isolation helper — inline, do NOT pre-abstract

The "run-async-over-list under a semaphore → (successes, failure_count)" shape recurs in
`enrich_batch` and `load_batch` here, and will recur in #079–#081. Per the brief: introduce
a SHARED helper (e.g. `tree.data.batch._gather_isolated(...)` or similar) ONLY if it
genuinely DRYs 4+ later call sites; otherwise INLINE the `asyncio.gather(return_exceptions
=True)` + log-and-skip + count logic in each task. Decision is the SWE's at implementation
time — DEFAULT to inlining in #078 and let #079–#081 pull a shared helper up if the
duplication actually appears. Do NOT build the abstraction speculatively here.

### 4. Result persistence (Prefect 3)

Result persistence is OFF by default in Prefect 3.6 (the repo sets no `persist_result`,
`result_storage`, `cache_policy`, nor `PREFECT_RESULTS_PERSIST_BY_DEFAULT`), so these
side-effecting load/extract tasks already do NOT persist results — matching every other
data-layer task. Do NOT add `persist_result=False` unless a `cache_policy` is introduced
(none is). State this in a code comment; add no new config.

### Files touched

- `apps/memory/src/tree/data/huggingface/arxiv_dataset_pipeline.py` — replace the three
  per-row `@task`s + `_process_document` with `transform_batch` / `enrich_batch` /
  `load_batch` ETL-phase tasks; rework the flow loop to call them per chunk. Keep
  `arxiv_window_entries`, `_get_huggingface_arxiv_defaults`, and the flow signature
  UNCHANGED.
- `apps/memory/src/tree/data/huggingface/arxiv_dataset.py` — UNCHANGED (the pure
  `extract_document` / `fetch_paper_content` / `load_document` / `fetch_dataset_batches`
  cores stay; the pipeline now calls them directly inside batch tasks). Do NOT fix the
  pre-existing `except ValueError, TypeError:` line — out of scope (noted in #074's log).
- `apps/memory/tests/unit/data/huggingface/test_arxiv_dataset_pipeline.py` — rework for the
  new batch-task shape (see Test guidance).
- `apps/memory/tests/integration/data/huggingface/test_arxiv_dataset_pipeline.py` — keep the
  flow-level assertions (still pass `max_samples`, still get N persisted docs); adjust only
  if a patch target moved.
- `(optional)` `apps/memory/src/tree/data/batch.py` — ONLY if the shared isolation helper is
  extracted (see §3); otherwise not created.

## Acceptance Criteria

- [ ] `ingest_arxiv_dataset` no longer invokes any PER-ROW `@task`; the per-row
      `extract_document` / `fetch_paper_content` / `load_document` task wrappers and
      `_process_document` are removed.
- [ ] The flow calls `transform_batch` once per streamed chunk; `load_batch` once per
      chunk (always); `enrich_batch` once per chunk ONLY when `fetch_content` is true.
- [ ] `load_batch` awaits `arxiv_dataset.load_document` over the chunk via a SINGLE
      `asyncio.gather(return_exceptions=True)` and returns the successful, non-`None`
      subset — a per-element load failure is logged + skipped, NOT propagated.
- [ ] `enrich_batch` fetches paper content per element under
      `asyncio.Semaphore(concurrency)` (the existing bound) with per-element failures
      isolated; a per-element fetch failure does not sink the batch.
- [ ] `transform_batch` is a pure map (`retries=0`); `enrich_batch` carries `retries=2`;
      `load_batch` carries `retries=1` (batch-wide infra retry, safe via
      `(user_id, source_uri)` idempotency).
- [ ] The flow signature `(user_id, max_samples, fetch_content, offset)` and `offset`
      windowing behavior are unchanged (the #071 offset tests still pass — windowed read
      still forwards `offset` to `fetch_dataset_batches`).
- [ ] Stable seams unchanged: `data/pipeline.py` (`_HUGGINGFACE_DATASET_HANDLERS`,
      `_ingest_arxiv_dataset_entry`, `arxiv_window_entries` import) and the
      orchestrator/worker flows compile and pass their suites with NO edit.
- [ ] No `persist_result`/`cache_policy` is added; a code comment notes Prefect-3
      persistence is off by default for these side-effecting tasks.
- [ ] `make memory-format-fix && make memory-lint-fix && make memory-format-check &&
      make memory-lint-check` clean; `make pre-commit` clean.
- [ ] `make memory-unit-tests` passes, 0 warnings.
- [ ] `make memory-integration-tests` (fast tail) passes — the arXiv flow integration test
      still ingests N docs for `max_samples=N`.
- [ ] [HUMAN] Deferred to #082: Prefect UI shows a window worker with a few TENS of task
      runs (NOT ~1000) and ETL-phase tasks (`transform-batch` / `load-batch`) visible per
      chunk. (Verified live in the #082 bookend.)

## BDD scenarios

### Scenario: one chunk → one load task, not one task per row
- **Given** a streamed batch of 50 raw arXiv entries
- **When** `ingest_arxiv_dataset` processes that chunk
- **Then** exactly one `load_batch` task run is created for the chunk (awaiting all 50
  loads inside it), NOT 50 `load-arxiv-document` task runs.

### Scenario: a bad-data element is skipped, the batch survives
- **Given** a chunk where one `load_document` call raises and the others succeed
- **When** `load_batch` runs
- **Then** the failing element is logged + skipped, the task returns the successful subset,
  and the task does NOT hard-fail.

### Scenario: a batch-wide infra failure retries the whole batch idempotently
- **Given** `load_batch` hard-fails once on a transient Mongo error, then succeeds on retry
- **When** Prefect retries the task (`retries=1`)
- **Then** the re-run does not double-insert (dedup on `(user_id, source_uri)`), and the
  flow's final document count is unchanged from a single clean run.

### Scenario: enrich runs only when fetch_content is set
- **Given** `fetch_content=False`
- **When** the flow processes a chunk
- **Then** `enrich_batch` is not called at all (no paper-content fetch task run); with
  `fetch_content=True` it runs once per chunk under the `concurrency` semaphore.

## User Stories

### Story: An operator sees a readable Prefect graph for an arXiv window worker
1. The operator runs the data pipeline with the arXiv HF source (`max_samples: 1000`,
   `num_workers: 2`).
2. They open a `data-etl-worker` HF-window run in the Prefect UI.
3. Instead of a wall of ~1000 `extract-arxiv-document` / `load-arxiv-document` task runs,
   they see a small number of `transform-batch` / `load-batch` task runs (one pair per
   `batch_size`-chunk) — the graph is legible and the run-list is tens of rows, not
   thousands.

### Story: A maintainer copies the arXiv batch-task pattern to the next pipeline
1. A maintainer picks up #079 (substack).
2. They read `arxiv_dataset_pipeline.py` and see the canonical shape: streamed/Extract in
   the flow loop, a pure `transform_batch`, an optional network `enrich_batch`, and a
   `load_batch` that isolates per-element failures with `gather(return_exceptions=True)`.
3. They reuse that shape (and, if it now recurs 4+ times, lift the isolation helper into a
   shared module) without re-deriving the design.

### Story: A bad arXiv row no longer fails a whole window
1. The arXiv stream yields a chunk containing one malformed entry that errors on load.
2. The window worker logs a WARNING for that one element and skips it.
3. The remaining entries in the chunk persist normally and the worker finishes green — one
   bad row never sinks ~500 good documents.

## Test guidance

- Call the `/testing-python` skill for test design. Run ONLY via `make memory-*` (LOCAL
  env; the Makefile loads `.env`).
- Unit (`test_arxiv_dataset_pipeline.py`): assert the NEW shape, deleting the per-row task
  tests (`TestExtractDocumentTask` / `TestFetchPaperContentTask` / `TestLoadDocumentTask` /
  `TestProcessDocument` as they reference removed symbols). Add:
  - `transform_batch.fn([raw, bad_raw], user_id)` → drops the id-less entry, returns the
    valid `Document`s (pure map; patch `extract_document` core).
  - `load_batch.fn([doc_a, doc_b])` with the core `load_document` patched to
    `side_effect=[doc_a, None]` → returns `[doc_a]` (duplicate filtered); with
    `side_effect=[doc_a, RuntimeError(...)]` → returns `[doc_a]` and logs the skip (the
    raise is isolated, NOT propagated). Assert ONE awaited gather, not N task runs.
  - `enrich_batch.fn([doc], concurrency)` with the core `fetch_paper_content` patched →
    sets `content`; with it raising for one element → that doc passes through with empty
    content, batch survives.
  - Keep/adapt the offset-forwarding tests (`test_forwards_offset_to_fetch_dataset_batches`,
    `test_defaults_offset_to_none`) and `_get_huggingface_arxiv_defaults` tests unchanged in
    intent.
  - Retry-metadata asserts: `transform_batch.retries == 0`, `enrich_batch.retries == 2`,
    `load_batch.retries == 1` (mirror `test_web_pipeline.py::TestTaskAndFlowMetadata`).
- Integration: the existing `ingest_arxiv_dataset(...)` flow test (patches `load_dataset`,
  `init_mongodb`) must still persist N docs for `max_samples=N` and return them — the
  batch refactor is behavior-preserving end-to-end. Adjust only patch targets that moved.

---

Blocked by: (none)

## Log

### [PA] 2026-06-23 — Grooming

**Summary**
Convert the arXiv HF leaf pipeline from per-row Prefect tasks (`extract_document` /
`fetch_paper_content` / `load_document` invoked per element → ~1000 task runs per window
worker) to batch-grain ETL-phase tasks (`transform_batch` pure map, optional network
`enrich_batch`, always-separate `load_batch`), with per-element isolation via
`asyncio.gather(return_exceptions=True)` INSIDE each task. Establishes the reusable pattern
#079–#081 copy. Flow signature, `offset` windowing, and every stable seam unchanged.

**Key decisions**
- Streamed Extract (`fetch_dataset_batches`) STAYS the flow loop (it's a generator, not a
  task) — only Transform/enrich/Load become per-chunk tasks. arXiv is the one pipeline with
  a genuine pure `transform_batch` (dict→Document) separate from Load.
- Per-element failures are logged + skipped (return the successful subset); the task
  hard-fails only on a batch-WIDE infra failure, where Prefect's `retries=1` re-runs the
  whole batch — safe because `load_document` dedups on `(user_id, source_uri)`.
- Retry grain relocates to the batch: `transform=0`, `enrich=2`, `load=1`. Existing
  per-call httpx behavior in `fetch_paper_content` is untouched (no network-retry
  regression).
- Result persistence is Prefect-3-off-by-default; no `persist_result` flag added.
- The shared isolation helper is INLINED for now; #079–#081 lift it into a shared module
  only if it genuinely recurs 4+ times. No speculative abstraction (CLAUDE.md: prefer
  removing instructions; the prior ponytail audit cut over-engineering from `data/`).

**Dependencies**
- None. Lands FIRST; #079–#081 depend on the pattern this establishes.

**User stories**
- 3 stories: operator reads a legible Prefect graph; maintainer copies the pattern;
  a bad row no longer sinks a window.

**Documentation discipline**
- Uses canonical glossary terms (Batch, Window, Worker, ETL-phase task). The new
  **ETL-phase task** glossary row is added in the grooming commit; this task is its first
  implementation.

Ready for implementation.
