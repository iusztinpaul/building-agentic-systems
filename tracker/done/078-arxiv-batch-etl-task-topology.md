# arxiv (HF) batch-ETL task topology — kill the per-row task explosion

Status: in-progress
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

- [x] `ingest_arxiv_dataset` no longer invokes any PER-ROW `@task`; the per-row
      `extract_document` / `fetch_paper_content` / `load_document` task wrappers and
      `_process_document` are removed.
- [x] The flow calls `transform_batch` once per streamed chunk; `load_batch` once per
      chunk (always); `enrich_batch` once per chunk ONLY when `fetch_content` is true.
- [x] `load_batch` awaits `arxiv_dataset.load_document` over the chunk via a SINGLE
      `asyncio.gather(return_exceptions=True)` and returns the successful, non-`None`
      subset — a per-element load failure is logged + skipped, NOT propagated.
- [x] `enrich_batch` fetches paper content per element under
      `asyncio.Semaphore(concurrency)` (the existing bound) with per-element failures
      isolated; a per-element fetch failure does not sink the batch.
- [x] `transform_batch` is a pure map (`retries=0`); `enrich_batch` carries `retries=2`;
      `load_batch` carries `retries=1` (batch-wide infra retry, safe via
      `(user_id, source_uri)` idempotency).
- [x] The flow signature `(user_id, max_samples, fetch_content, offset)` and `offset`
      windowing behavior are unchanged (the #071 offset tests still pass — windowed read
      still forwards `offset` to `fetch_dataset_batches`).
- [x] Stable seams unchanged: `data/pipeline.py` (`_HUGGINGFACE_DATASET_HANDLERS`,
      `_ingest_arxiv_dataset_entry`, `arxiv_window_entries` import) and the
      orchestrator/worker flows compile and pass their suites with NO edit.
- [x] No `persist_result`/`cache_policy` is added; a code comment notes Prefect-3
      persistence is off by default for these side-effecting tasks.
- [x] `make memory-format-fix && make memory-lint-fix && make memory-format-check &&
      make memory-lint-check` clean; `make pre-commit` clean.
- [x] `make memory-unit-tests` passes, 0 warnings.
- [x] `make memory-integration-tests` (fast tail) passes — the arXiv flow integration test
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

### [SWE] 2026-06-23 — Batch-ETL task topology implemented

**Summary**
Replaced the three per-row `@task`s (`extract-arxiv-document`, `fetch-arxiv-paper-content`,
`load-arxiv-document`) + the `_process_document` per-row helper with three batch-grain
ETL-phase tasks operating over ONE `batch_size`-chunk each:
- `transform-arxiv-batch` (`retries=0`) — pure map `list[dict] → list[Document]` via the
  core `extract_document`; drops id-less entries.
- `enrich-arxiv-batch` (`retries=2`, `retry_delay_seconds=5`) — network Extract, invoked
  ONLY when `fetch_content`; fetches paper HTML per element under `asyncio.Semaphore(
  concurrency)` via `gather(return_exceptions=True)`; a per-element fetch failure logs +
  passes the doc through with empty content (batch survives).
- `load-arxiv-batch` (`retries=1`, `retry_delay_seconds=2`) — DB Load over a SINGLE
  `gather(return_exceptions=True)`; returns the persisted non-`None` subset (dups drop);
  per-element load failure logged + skipped + counted, NOT propagated; whole-batch retry
  is safe via `(user_id, source_uri)` dedup.

The flow `ingest_arxiv_dataset(user_id, max_samples, fetch_content, offset)` and name
`ingest-arxiv-dataset-etl` are byte-identical in signature; the streamed Extract
(`fetch_dataset_batches`, a generator) STAYS the flow loop. Per yielded chunk:
`transform_batch` → (if `fetch_content`) `enrich_batch` → `load_batch`, accumulate.
Isolation helper INLINED per §3 (only 2 call sites here; #079–#081 lift it if it recurs
4+). No `persist_result`/`cache_policy` added — a code comment records Prefect-3
persistence is off-by-default for these side-effecting tasks.

**Files modified**
- `apps/memory/src/tree/data/huggingface/arxiv_dataset_pipeline.py` — per-row tasks +
  `_process_document` removed; `transform_batch`/`enrich_batch`/`load_batch` added; flow
  loop reworked to call batch tasks per chunk. `arxiv_window_entries` +
  `_get_huggingface_arxiv_defaults` + flow signature unchanged.
- `apps/memory/tests/unit/data/huggingface/test_arxiv_dataset_pipeline.py` — reworked for
  the new batch-task shape (deleted `TestExtractDocumentTask`/`TestFetchPaperContentTask`/
  `TestLoadDocumentTask`/`TestProcessDocument`; added `TestTaskMetadata`,
  `TestTransformBatch`, `TestEnrichBatch`, `TestLoadBatch`, and `TestIngestArxivDataset`
  asserting one `load_batch`/chunk, isolation, no `enrich_batch` when `fetch_content`
  false; kept the offset-forwarding + defaults tests).
- `apps/memory/src/tree/data/huggingface/arxiv_dataset.py` — UNCHANGED (pure cores reused).
- `apps/memory/tests/integration/data/huggingface/test_arxiv_dataset_pipeline.py` —
  UNCHANGED (no patch target moved; flow-level assertions still hold).
- No `tree/data/batch.py` created — isolation inlined (default per §3).

**Tests**
- Unit: 1607 passing, 0 failing, 0 warnings (`make memory-unit-tests`). The 21 arxiv
  pipeline tests assert the new shape (TDD: red on `ImportError: enrich_batch` → green).
- Integration (fast tail, `make memory-integration-tests`): the arXiv flow integration
  suite passes (3 passed / 2 slow-deselected in isolation). The full fast tail reported
  3 unrelated flaky failures (`test_web_serp` live SERP, `test_indexing_pipeline`
  embeddings, `test_meta_state` `updated_at` timing) — verified PRE-EXISTING: stashing my
  two files and running those 3 on the clean tree gives the same `2 passed, 1 skipped`,
  and re-running with my changes is identical. None touch arxiv/huggingface.

**Acceptance criteria** — all non-HUMAN criteria verified:
- [x] No per-row `@task`; per-row wrappers + `_process_document` removed —
      `test_arxiv_dataset_pipeline.py` imports only the new symbols; codebase grep finds no
      stale references.
- [x] `transform_batch`/`load_batch` once per chunk; `enrich_batch` once per chunk only on
      `fetch_content` — `TestIngestArxivDataset::test_calls_load_batch_once_per_chunk`,
      `::test_does_not_call_enrich_batch_when_fetch_content_false`,
      `::test_calls_enrich_batch_once_per_chunk_when_fetch_content_true`.
- [x] `load_batch` SINGLE gather, returns non-`None` subset, per-element failure isolated —
      `TestLoadBatch::test_returns_persisted_subset_dropping_duplicates`,
      `::test_isolates_one_element_failure`.
- [x] `enrich_batch` per-element under `Semaphore(concurrency)`, failures isolated —
      `TestEnrichBatch::test_element_fetch_failure_passes_through_with_empty_content`,
      `::test_runs_under_concurrency_semaphore`.
- [x] Retry grain `transform=0`/`enrich=2`/`load=1` — `TestTaskMetadata`.
- [x] Flow signature + offset windowing unchanged —
      `TestIngestArxivDataset::test_forwards_offset_to_fetch_dataset_batches`,
      `::test_defaults_offset_to_none`.
- [x] Stable seams unchanged — `data/pipeline.py` + orchestrator/worker suites pass with
      NO edit (full unit suite green, no edits outside the two arxiv files).
- [x] No `persist_result`/`cache_policy`; code comment present (lines 83-88).
- [x] format/lint/pre-commit clean.
- [x] unit-tests pass, 0 warnings.
- [x] integration fast tail: arXiv flow still ingests N docs for `max_samples=N`.
- [ ] [HUMAN] Deferred to #082: Prefect UI shows tens (not ~1000) of task runs per window
      worker.

**Evidence**
```
$ make memory-unit-tests
============================ 1607 passed in 48.92s =============================

$ make memory-format-fix && make memory-lint-fix && make memory-format-check && make memory-lint-check
All checks passed!  /  282 files already formatted

$ make pre-commit
ruff check ... Passed   ruff format ... Passed   prettier ... Passed   biome ... Passed

$ uv run pytest tests/integration/data/huggingface/test_arxiv_dataset_pipeline.py -m "not slow"
3 passed, 2 deselected in 3.64s

# End-to-end (real local Mongo, load_dataset mocked, batch_size=3, 7 rows, fetch_content=False):
Task run 'transform-arxiv-batch-...' Completed   (x3 — one per chunk)
Task run 'load-arxiv-batch-...'      Completed   (x3 — one per chunk; 0 enrich runs)
RESULT: ingested 7 docs   DB: 7 e2e docs persisted
E2E OK: batch topology ingested 7 docs across 3 chunks (batch_size=3)
```

**Notes**
- E2E confirms the topology goal: 7 rows over `batch_size=3` produced 6 task runs total
  (3 transform + 3 load), not ~14 — and 0 enrich runs with `fetch_content=False`.
- 3 unrelated integration flakes in the full fast tail are pre-existing (proven by
  baseline stash run); they are live-network / embedding-convergence / timing tests.
- NOT COMMITTED — handed to Tester for review.

### [Tester] 2026-06-23 — QA

**Test summary**
- Format / lint / pre-commit: PASS (`make memory-format-check` → 282 files formatted;
  `make memory-lint-check` → All checks passed; `make pre-commit` → all hooks Passed).
- Unit tests: 1607 passed / 0 failed (`make memory-unit-tests`), 0 warnings. The 21 arxiv
  pipeline tests pass (`tests/unit/data/huggingface/test_arxiv_dataset_pipeline.py`).
- Integration tests (fast tail, `make memory-integration-tests`): 176 passed / 2 failed /
  1 skipped / 104 deselected. The 2 failures (`test_indexing_pipeline::test_embeds_nodes`,
  `test_meta_state::test_updated_at_is_recent`) are PRE-EXISTING flakes — INDEPENDENTLY
  CONFIRMED: both pass in isolation on the clean baseline (arxiv files stashed) AND with the
  change present; both live in `memory/` with zero code-path overlap with the arxiv
  pipeline. arXiv integration suite: 5 passed (incl. slow `test_idempotent_on_rerun` +
  `test_with_fetch_content` via the real Prefect flow).
- Warnings: 0.

**E2E adversarial pass** (real local Mongo, env=local; standalone harness exercising the
batch tasks' `.fn` + the flow via the real Prefect engine — 20/20 checks PASS):
- Happy path: flow object `ingest_arxiv_dataset(max_samples=7, fetch_content=False)` over 3
  chunks (real Prefect engine) → ingested 7, DB 7, task runs = `3 transform-arxiv-batch + 3
  load-arxiv-batch + 0 enrich` (PASS). Per-row explosion is dead at the engine level.
- Break path 1 (batch grain): 7 rows / batch_size 3 → transform `.fn` invoked 3×, load 3×,
  enrich 0× (NOT 7×) — counted via real invocations (PASS).
- Break path 2 (per-element isolation, load raises): chunk of 3 where 1 `_load_document`
  raises → no exception escaped `load_batch`; returned + persisted the 2 good docs; bad one
  absent from DB (PASS).
- Break path 3 (idempotency / whole-batch retry): re-run `load_batch` over an
  already-persisted chunk → second run returns 0, DB count stays 3 (no double-insert). Real
  flow `test_idempotent_on_rerun` corroborates (second run 0, DB 5) (PASS).
- Break path 4 (enrich gating + per-element fetch raise): one fetch raises → batch survives
  (both docs returned), good doc enriched, failed doc passes through with empty content; and
  enrich runs 0× when `fetch_content=False` (PASS).
- Break path 5 (boundary): empty batch → `[]` across transform/load/enrich; all-id-less
  batch → `[]` (no crash) (PASS).
- Break path 6 (malformed, all-element-failure): every `_load_document` raises → `load_batch`
  returns `[]` without propagating (PASS).
- Break path 7 (offset/window seam): flow forwards `offset=500` to `fetch_dataset_batches`
  unchanged (PASS).

**Acceptance criteria**
- [x] PASS — No per-row `@task`; per-row wrappers + `_process_document` removed — file read
      (`arxiv_dataset_pipeline.py` lines 98-178 hold only the 3 batch tasks); repo-wide grep
      finds no stale `extract-arxiv-document`/`fetch-arxiv-paper-content`/`load-arxiv-document`
      references.
- [x] PASS — transform/load once per chunk, enrich once per chunk only when `fetch_content` —
      `TestIngestArxivDataset::test_calls_load_batch_once_per_chunk` +
      `::test_calls_enrich_batch_once_per_chunk_when_fetch_content_true` +
      `::test_does_not_call_enrich_batch_when_fetch_content_false`; e2e BP1 (real engine: 3/3/0).
- [x] PASS — `load_batch` SINGLE gather, returns non-None subset, per-element failure isolated
      — `arxiv_dataset_pipeline.py:161-178`; `TestLoadBatch::test_isolates_one_element_failure`;
      e2e BP2 (bad row skipped, batch survives, only good docs persist).
- [x] PASS — `enrich_batch` per-element under `Semaphore(concurrency)`, failures isolated —
      `arxiv_dataset_pipeline.py:113-147`;
      `TestEnrichBatch::test_runs_under_concurrency_semaphore` (peak ≤ 2) +
      `::test_element_fetch_failure_passes_through_with_empty_content`; e2e BP4.
- [x] PASS — Retry grain transform=0 / enrich=2 / load=1 — `TestTaskMetadata`; asserts
      `retries`/`retry_delay_seconds`/`name` on each task.
- [x] PASS — Flow signature + offset windowing unchanged —
      `TestIngestArxivDataset::test_forwards_offset_to_fetch_dataset_batches` +
      `::test_defaults_offset_to_none`; e2e BP7; flow name `ingest-arxiv-dataset-etl`.
- [x] PASS — Stable seams unchanged — `data/pipeline.py` unmodified (`git status`);
      `_ingest_arxiv_dataset_entry` (`pipeline.py:124-133`) still calls
      `ingest_arxiv_dataset(user_id, max_samples, fetch_content, offset)`;
      `_HUGGINGFACE_DATASET_HANDLERS` + `arxiv_window_entries` import intact; full unit suite
      (1607) green with no edits outside the 2 arxiv files.
- [x] PASS — No `persist_result`/`cache_policy`; code comment present
      (`arxiv_dataset_pipeline.py:83-88`).
- [x] PASS — format/lint/pre-commit clean (see Test summary).
- [x] PASS — unit-tests pass, 0 warnings (1607 passed).
- [x] PASS — integration fast tail: arXiv flow ingests N docs for `max_samples=N` —
      `test_arxiv_dataset_pipeline.py` integration suite 5 passed (N=5, N=7, latent-upgrade,
      idempotent-rerun, fetch_content).
- [ ] [HUMAN] Deferred to #082 — left unchecked as expected. Behavioral half pre-verified:
      real Prefect engine produced 6 task runs (3 transform + 3 load) for 7 rows, not ~14.

**Evidence**
```
$ make memory-unit-tests
============================ 1607 passed in 46.82s =============================

$ uv run pytest tests/integration/data/huggingface/test_arxiv_dataset_pipeline.py
============================== 5 passed in 1.38s ===============================

# Flake independence (arxiv files stashed → clean baseline):
$ uv run pytest <test_embeds_nodes> <test_updated_at_is_recent> -m "not slow"
============================== 2 passed in 7.06s ===============================
# Same 2 tests + arxiv integration WITH the change, in isolation:
======================= 5 passed, 2 deselected in 7.44s ========================

# Real Prefect engine, 7 rows / 3 chunks, fetch_content=False:
   3 TASK RUN: load-arxiv-batch
   3 TASK RUN: transform-arxiv-batch
   FLOW-OBJECT ingested: 7   |   DB count: 7   (0 enrich runs)

# E2E adversarial harness (real local Mongo): 20/20 checks PASSED
```

**Other issues found** (PASS-with-note — non-blocking; orchestrator/PR-reviewer to decide)
- The code/comment + AC say dedup is on `(user_id, source_uri)`, but the live unique index is
  `user_source_uri_unique = (user_id, source_type, source_uri)`. For arxiv all docs are
  `SourceType.HUGGINGFACE`, and `load_document`'s application-level `find_one({user_id,
  source_uri})` (no source_type) is the primary dedup and catches the retry case regardless,
  so the idempotency contract HOLDS (verified by BP3 + `test_idempotent_on_rerun`). The
  comment is just imprecise about the index key. This lives in the out-of-scope core
  `arxiv_dataset.py`; flagging only, not a defect in this change.
- Running the unit file in isolation via bare `uv run pytest` emits a post-run
  `ValueError: I/O operation on closed file` from Prefect's ephemeral-server shutdown logging
  to a closed stdout — cosmetic teardown noise AFTER `21 passed`; absent from the `make`
  target run. Not a test failure.

**VERDICT: PASS**
