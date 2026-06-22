# Offset-aware arXiv windowed ingest

Status: pending
Tags: `data`, `huggingface`
Depends on: #070
Blocks: #072

## Scope

Make the arXiv HuggingFace ingest path window-aware so a single `data-etl-worker` run
can ingest exactly ONE disjoint offset-window of the streaming dataset. This is the LEAF
of the HuggingFace offset-windowing fan-out: #072's orchestrator computes the window
coordinates and stamps `offset` onto each dispatched entry; THIS task makes the ingest
honor that `offset` (skip the first `offset` rows, then stream `max_samples`).

The change threads a single new `offset` parameter down three layers, all defaulting so
that `offset=None`/absent reproduces today's behavior exactly:

### 1. `fetch_dataset_batches` — add `offset`

In `apps/memory/src/tree/data/huggingface/arxiv_dataset.py`, change the signature to
`fetch_dataset_batches(offset: int | None, max_samples: int, batch_size: int)`
(offset first to read naturally as a window coordinate; the call sites are updated in
step 2, so pick whichever parameter order is cleanest — keyword-pass at the call site so
order is not load-bearing). Before the existing streaming loop, after
`ds = load_dataset(ARXIV_HF_DATASET, split="train", streaming=True)`, add:

```python
if offset:
    ds = ds.skip(offset)
```

`ds.skip(n)` on a streaming `IterableDataset` returns a new dataset that discards the
first `n` examples of the stream; the subsequent `for entry in ds:` then yields rows
`[offset, offset + max_samples)`. `offset` falsy (`None` or `0`) ⇒ no skip ⇒ today's
exact path. The `max_samples` cap (the `count >= max_samples: break`) is applied to the
POST-skip stream, so the window covers exactly `max_samples` rows starting at `offset`.
Update the leading log line to include `offset` so disjoint windows are visible in logs.

Confirm `IterableDataset.skip()` semantics via the `context7` MCP server
(`huggingface/datasets`, `IterableDataset.skip`) at implementation time: `.skip(n)`
must be applied to the streaming dataset BEFORE iteration and skips the first `n` rows of
the stream (it is O(n) — it walks and discards — which is acceptable here because runs
are `max_samples`-capped; see the feature plan caveat).

### 2. `ingest_arxiv_dataset` — accept + forward `offset`

In `apps/memory/src/tree/data/huggingface/arxiv_dataset_pipeline.py`, add
`offset: int | None = None` to the `ingest_arxiv_dataset` flow signature and forward it
into the `_fetch_dataset_batches(...)` call (keyword-pass `offset=offset`). Everything
else (the `_get_huggingface_arxiv_defaults()` resolution of
`max_samples`/`fetch_content`/`batch_size`/`concurrency`, the semaphore, the
per-batch gather, the dedup-on-load) is UNCHANGED. `offset=None` ⇒ identical to today.

### 3. `_ingest_arxiv_dataset_entry` — pass the entry's `offset`

In `apps/memory/src/tree/data/pipeline.py`, the HF handler
`_ingest_arxiv_dataset_entry(entry, user_id)` currently calls
`ingest_arxiv_dataset(user_id=…, max_samples=entry.max_samples, fetch_content=entry.fetch_content)`.
Add `offset=entry.offset` to that call. Because `HuggingFaceDatasetSource.offset` defaults
to `None` (#070), a non-windowed entry passes `offset=None` and the path is unchanged.
This is the ONLY worker-side change — `_ingest_sources` and the `data_etl_worker` flow
are otherwise untouched; the worker still receives a homogeneous shard and the existing
`isinstance` routing fires the HF branch.

### Idempotency note (no new work — verify only)

Disjoint offset windows ingest non-overlapping `arxiv_id` ranges, and `load_document`
(in `arxiv_dataset.py`) already dedups on `(user_id, source_uri)` with
`arxiv_id → source_uri` deterministic. So even if two windows overlap (e.g. a mis-set
`num_workers` vs `max_samples`), re-ingesting the same `arxiv_id` is a no-op upsert, not
a double-insert. No new idempotency code; the task log records this is already covered.

### Files touched

- `apps/memory/src/tree/data/huggingface/arxiv_dataset.py` — `fetch_dataset_batches`
  gains `offset` + the `if offset: ds = ds.skip(offset)` guard before the loop; log line
  includes `offset`.
- `apps/memory/src/tree/data/huggingface/arxiv_dataset_pipeline.py` —
  `ingest_arxiv_dataset` gains `offset: int | None = None` and forwards it to
  `_fetch_dataset_batches`.
- `apps/memory/src/tree/data/pipeline.py` — `_ingest_arxiv_dataset_entry` passes
  `offset=entry.offset` into `ingest_arxiv_dataset`.
- `apps/memory/tests/unit/data/huggingface/test_arxiv_dataset_pipeline.py` — add
  offset-forwarding + offset-default unit coverage (mock the dataset/skip; assert the
  call carries `offset`).
- (existing arxiv_dataset unit tests, if any cover `fetch_dataset_batches`) — extend for
  the skip path; otherwise add a focused unit test for the skip math with a fake
  `load_dataset`.

## Acceptance Criteria

- [x] `fetch_dataset_batches(offset=None, max_samples=N, batch_size=B)` yields the same
      batches as the pre-feature `fetch_dataset_batches(max_samples=N, batch_size=B)` —
      no skip when `offset` is falsy.
- [x] `fetch_dataset_batches(offset=K, …)` calls `ds.skip(K)` on the streaming dataset
      BEFORE the iteration loop, so the yielded rows start at stream index `K` and cover
      exactly `max_samples` rows (`[K, K + max_samples)`).
- [x] `offset=0` is treated as no-skip (the `if offset:` guard), identical to `None`.
- [x] `ingest_arxiv_dataset(..., offset=K)` forwards `offset=K` to
      `fetch_dataset_batches`; `ingest_arxiv_dataset(...)` with no `offset` forwards
      `offset=None`.
- [x] `_ingest_arxiv_dataset_entry(entry, user_id)` calls `ingest_arxiv_dataset` with
      `offset=entry.offset` (so a non-windowed entry with `offset=None` is unchanged, and
      a windowed entry with `offset=K` ingests its window).
- [x] The log line for the streaming fetch includes the `offset` so disjoint windows are
      distinguishable in run logs.
- [x] No change to the dedup/load path: `load_document` still dedups on
      `(user_id, source_uri)`; overlapping windows re-upsert (no double-insert) — recorded
      in the task log as already-covered idempotency.
- [x] `make memory-format-fix && make memory-lint-fix && make memory-format-check && make memory-lint-check && make pre-commit` all clean.
- [x] `make memory-unit-tests` passes, 0 warnings (new offset tests green).
- [x] `make memory-integration-tests` (fast tail) passes for the touched data path.

## BDD scenarios

### Scenario: no offset reproduces today's stream exactly
- **Given** `fetch_dataset_batches(offset=None, max_samples=10, batch_size=5)`
- **When** I drive it over a fake `load_dataset` returning 20 rows
- **Then** `ds.skip` is NEVER called, and the yielded rows are the first 10 (two batches
  of 5) — byte-identical to the pre-feature behavior.

### Scenario: a non-zero offset skips then windows
- **Given** `fetch_dataset_batches(offset=5, max_samples=10, batch_size=5)` over a fake
  `load_dataset` returning 30 indexed rows
- **When** I collect the yielded entries
- **Then** `ds.skip(5)` was applied before iteration, and the yielded rows are stream
  indices `[5, 15)` (exactly `max_samples=10` rows starting at offset 5).

### Scenario: the worker forwards the entry's offset
- **Given** a `HuggingFaceDatasetSource(uri="librarian-bots/arxiv-metadata-snapshot",
  max_samples=250, offset=250)` handed to `_ingest_arxiv_dataset_entry`
- **When** the handler runs (with `ingest_arxiv_dataset` mocked)
- **Then** `ingest_arxiv_dataset` is awaited with `offset=250, max_samples=250` so the
  worker ingests rows `[250, 500)` and nothing outside its window.

### Scenario: overlapping windows do not double-insert
- **Given** two windows whose offset ranges accidentally overlap on some `arxiv_id`
- **When** both windows ingest that `arxiv_id`
- **Then** `load_document` dedups on `(user_id, source_uri)` and the second write is a
  no-op upgrade/skip, not a duplicate document (idempotency already holds — no new code).

## User Stories

### Story: A worker ingests exactly its arXiv window
1. The orchestrator (#072) dispatches a `data-etl-worker` with
   `sources=[HuggingFaceDatasetSource(uri="librarian-bots/arxiv-metadata-snapshot",
   max_samples=250, offset=250)]`.
2. The worker's HF branch calls `ingest_arxiv_dataset(..., max_samples=250, offset=250)`.
3. `fetch_dataset_batches` skips the first 250 rows then streams the next 250, so the
   worker persists arXiv documents for rows `[250, 500)` only.
4. The run log shows `offset=250` so the operator can see which window this worker owns.

### Story: A plain (single-window) arXiv ingest is unchanged
1. The orchestrator dispatches a HF entry with `num_workers=1` ⇒ `offset=None`.
2. The worker calls `ingest_arxiv_dataset(..., offset=None)`.
3. `fetch_dataset_batches` does NOT skip; it streams the first `max_samples` rows exactly
   as the pre-feature single run did.

## Test guidance

- Offset-forwarding (`ingest_arxiv_dataset` → `fetch_dataset_batches`, and
  `_ingest_arxiv_dataset_entry` → `ingest_arxiv_dataset`): UNIT, mock the inner callable,
  assert the `offset` kwarg — mirror the existing
  `TestIngestArxivDataset::test_processes_batches_in_parallel` pattern (patch
  `_fetch_dataset_batches`, `init_mongodb`, `_load_document`).
- The skip math (`ds.skip(K)` applied before iteration, window `[K, K+max_samples)`):
  UNIT with a fake `load_dataset` returning an indexed iterable whose `.skip(n)` returns
  the tail — assert the yielded rows are the expected window. No network, no Mongo.
- The end-to-end "windowed worker persists only its range" is an INTEGRATION concern
  deferred to #074's live e2e (it needs the real dataset + Mongo); do NOT add a live HF
  integration test here.
- Per CLAUDE.md, call the `/testing-python` skill when writing the tests. Run tests via
  `make memory-*` targets (LOCAL env), never bare `uv run pytest`.

---

Blocked by: #070

## Log

### [PA] 2026-06-22 14:05 — Grooming

**Summary**
Thread a single `offset` parameter through the arXiv ingest path
(`fetch_dataset_batches` → `ingest_arxiv_dataset` → `_ingest_arxiv_dataset_entry`) so a
worker ingests exactly one disjoint offset-window of the streaming dataset. Adds
`if offset: ds = ds.skip(offset)` before the existing streaming loop. All defaults
(`offset=None`) reproduce today's behavior exactly.

**Key decisions**
- `if offset:` (truthy guard) treats both `None` and `0` as no-skip, so the common
  single-window case never pays the `skip` walk.
- The window is `[offset, offset + max_samples)` because `max_samples` is applied to the
  POST-skip stream — this is what makes #072's `window_size = max_samples // num_workers`
  + last-worker-remainder math produce disjoint, exhaustive windows.
- Idempotency is already covered by `load_document`'s `(user_id, source_uri)` dedup +
  deterministic `arxiv_id → source_uri`; overlapping windows re-upsert, never
  double-insert. No new code — recorded as a verify-only AC.
- `.skip()` is O(offset) on a streaming dataset; acceptable because runs are
  `max_samples`-capped (feature-plan caveat). The `split_dataset_by_node` upgrade for an
  uncapped whole-dataset run is explicitly out of scope.

**Dependencies**
- #070 — `HuggingFaceDatasetSource.offset` must exist before `_ingest_arxiv_dataset_entry`
  can read `entry.offset`.

**User stories**
- 2 stories: a worker ingests exactly its arXiv window; a single-window ingest is
  unchanged.

**Open questions**
- None. SWE should confirm `IterableDataset.skip()` semantics via context7 at
  implementation time (documented: skip-first-n on the stream, applied before iteration).

Ready for implementation.

### [SWE] 2026-06-22 15:30 — Implementation

**Files modified**
- `apps/memory/src/tree/data/huggingface/arxiv_dataset.py` — `fetch_dataset_batches`
  gains `offset: int | None = None`; rejects a negative `offset` with `ValueError`
  (strict, per the #070 Tester note — `.skip` never called negative); applies
  `if offset: ds = ds.skip(offset)` after `load_dataset(..., streaming=True)` and
  before the loop; leading log line now carries `offset=…`, closing line records the
  window `[offset, offset+count)` for #074's disjoint-window verification.
- `apps/memory/src/tree/data/huggingface/arxiv_dataset_pipeline.py` —
  `ingest_arxiv_dataset` gains `offset: int | None = None` and keyword-forwards it
  into `_fetch_dataset_batches(max_samples, batch_size, offset=offset)`. Nothing else
  changed (defaults resolution, semaphore, gather, dedup-on-load untouched).
- `apps/memory/src/tree/data/pipeline.py` — `_ingest_arxiv_dataset_entry` now passes
  `offset=entry.offset` into `ingest_arxiv_dataset` (the only worker-side change).
- `apps/memory/tests/unit/data/huggingface/test_arxiv_dataset.py` — new
  `TestFetchDatasetBatchesOffset` (offset None/0/default no-skip; offset=K skips then
  windows `[K, K+max_samples)`; window log line includes `offset`; negative offset
  raises and never calls `.skip`). Fake streaming dataset records `.skip` calls.
- `apps/memory/tests/unit/data/huggingface/test_arxiv_dataset_pipeline.py` — new
  offset-forwarding tests (`offset=250` forwarded; default `offset=None`).
- `apps/memory/tests/unit/data/test_pipeline.py` — new
  `test_forwards_huggingface_dataset_offset_window` (windowed entry forwards
  `offset=250`); updated three existing arxiv-dispatch assertions to include
  `offset=None`.
- `apps/memory/tests/integration/data/test_pipeline.py` +
  `tests/integration/data/web/test_web_pipeline.py` — updated the local
  `_fetch_dataset_batches` fakes to accept the new `offset=None` kwarg and the
  five-variant dispatch assertion to include `offset=None`.

**Tests**
- Unit: 1570 passing, 0 failing, 0 warnings (`make memory-unit-tests`).
- Integration (fast tail): 178 passing, 1 skipped (a `@pytest.mark.slow` deselect),
  0 failing (`make memory-integration-tests`). The two `@pytest.mark.slow` arxiv
  data-pipeline tests (`test_runs_all_three_pipelines`,
  `test_runs_only_arxiv_when_no_substack`) also run green directly.

**Acceptance criteria**
- [x] offset None/0/absent ⇒ no skip, byte-for-byte today's stream — verified by
  `test_arxiv_dataset.py::TestFetchDatasetBatchesOffset::{test_offset_none_never_skips,
  test_offset_zero_never_skips, test_offset_default_matches_pre_feature_stream}`.
- [x] offset=K ⇒ `ds.skip(K)` before iteration, window `[K, K+max_samples)` —
  `…::test_offset_skips_then_windows`.
- [x] `ingest_arxiv_dataset` forwards `offset` (K and default None) —
  `test_arxiv_dataset_pipeline.py::TestIngestArxivDataset::{test_forwards_offset_to_fetch_dataset_batches,
  test_defaults_offset_to_none}`.
- [x] `_ingest_arxiv_dataset_entry` passes `offset=entry.offset` —
  `test_pipeline.py::TestDataWorker::{test_forwards_huggingface_dataset_offset_window,
  test_passes_huggingface_dataset_overrides}`.
- [x] Streaming fetch log line includes `offset` —
  `…::test_window_log_line_includes_offset`.
- [x] Dedup/load path unchanged; overlapping windows re-upsert — verify-only, see Notes.
- [x] format/lint/pre-commit clean; unit + fast-integration green (see Tests).

**Evidence**
```
$ make memory-unit-tests
============================ 1570 passed in 48.70s =============================

$ make memory-integration-tests
========== 178 passed, 1 skipped, 104 deselected in 165.41s (0:02:45) ==========

# End-to-end runtime exercise of fetch_dataset_batches (realistic streaming stand-in):
Streaming arxiv dataset ... (offset=None, max_samples=10, batch_size=5)
Finished streaming. Window [offset=None, count=10] → rows [0, 10)   # no .skip
Streaming arxiv dataset ... (offset=10, max_samples=10, batch_size=5)
  >> .skip(10) called
Finished streaming. Window [offset=10, count=10] → rows [10, 20)     # disjoint window
offset=-1 → rejected ValueError: offset must be >= 0, got -1         # .skip never called
```

**Notes**
- Validation policy (#070 carry-over): chose the STRICT option — a negative `offset`
  raises `ValueError("offset must be >= 0, got …")` rather than clamp-to-0, so a
  mis-set window coordinate fails loudly instead of silently re-ingesting from 0.
  `None`/`0` are no-ops via the `if offset:` truthy guard; `.skip` is never called
  with a non-positive value.
- `IterableDataset.skip()` semantics confirmed at implementation time against the
  installed `datasets` lib docstring/example (and the task's context7 note): returns a
  new dataset skipping the first `n` examples, applied before iteration — matches the
  `[offset, offset+max_samples)` window math.
- Idempotency AC is verify-only (no new code): `load_document` in `arxiv_dataset.py`
  still dedups on `(user_id, source_uri)` with a deterministic `arxiv_id → source_uri`,
  so overlapping windows re-upsert (no double-insert). Unchanged by this task.
- Live HF e2e (windowed worker persists only its range against the real dataset +
  Mongo) is intentionally deferred to #074 per the task's test guidance — NOT added
  here.
- Behavior-preserving: `offset=None`/`0` (the default at every layer) reproduces
  today's single-run ingest exactly; asserted by
  `test_offset_default_matches_pre_feature_stream`.

### [Tester] 2026-06-22 16:40 — QA

**Test summary**
- Format / lint / pre-commit: PASS (`make memory-format-check` 280 files OK; `make memory-lint-check` all checks passed; `make pre-commit` all hooks Passed)
- Unit tests: 1570 passed / 0 failed (`make memory-unit-tests`, 0 warnings)
- Integration tests (fast tail): 178 passed / 0 failed (1 slow-deselect skipped; 104 deselected) (`make memory-integration-tests`)
- Warnings: 0
- Scope check: reviewed only the 071 diff (arxiv_dataset.py, arxiv_dataset_pipeline.py,
  pipeline.py + the touched unit/integration tests). `docs/adrs/002_*.md`, `docs/glossary.md`
  and the sibling `tracker/07{2,3,4}-*.groomed.md` are planning/sibling artifacts, excluded.

**E2E adversarial pass** (real `fetch_dataset_batches`, `load_dataset` mocked — NO HF network call)
- Happy path: `fetch_dataset_batches(offset=None, max_samples=10, batch_size=5)` over a 100-row
  fake stream → 2 batches, rows `[0,10)`, `.skip` never called (PASS — byte-equivalent to today)
- Regression (most important): `offset=0` vs `offset=None` → identical batches, no `.skip`
  on either (PASS — behavior-preserving invariant holds)
- Window: `offset=10, max_samples=10` → `.skip(10)` called exactly once before iteration,
  window `[10,20)` (PASS)
- Disjointness (#074): window A `[0,10)` vs window B `[10,20)` → set intersection empty (PASS)
- Break path 1 (boundary: negative offset): `offset=-1` → `ValueError("offset must be >= 0,
  got -1")`, `.skip` NEVER called (PASS). Mutation-sanity: without the guard `ds.skip(-1)`
  would silently mis-window to the last row — the strict reject is load-bearing.
- Break path 2 (boundary: offset past end): `offset=200` on 100 rows → `.skip(200)` once, 0
  rows yielded, no crash (PASS — graceful empty window)
- Break path 3 (boundary: max_samples > remaining): `offset=95, max_samples=50` on 100 rows →
  yields the 5 remaining rows `[95,100)`, no crash (PASS)
- Break path 4 (boundary: offset at last row): `offset=99` → exactly 1 row `2103.00099` (PASS)
- Break path 5 (malformed type: `offset=2.5`): raises `TypeError` from the underlying slice/skip
  — fails loudly, no silent corruption. NOTE not FAIL: `offset` is always `int`/`None` through
  the real call chain (a Pydantic `int | None` field stamped via `model_copy`), so a float is
  unreachable in production; the real HF `IterableDataset.skip` also requires an int.
- Worker threading (real `_ingest_arxiv_dataset_entry`, `ingest_arxiv_dataset` mocked):
  - windowed `HuggingFaceDatasetSource(max_samples=250, offset=250)` →
    `ingest_arxiv_dataset(offset=250, max_samples=250)` → rows `[250,500)` (PASS)
  - non-windowed entry → `offset=None` (PASS — unchanged)
  - `base.model_copy(update={"offset": 500})` (#072's stamp mechanism) round-trips and threads
    `offset=500` through (PASS)

**Acceptance criteria**
- [x] PASS — `offset=None`/falsy yields same batches as pre-feature, no skip —
      `test_arxiv_dataset.py::TestFetchDatasetBatchesOffset::test_offset_none_never_skips` +
      `test_offset_default_matches_pre_feature_stream`; adversarial regression check above.
- [x] PASS — `offset=K` calls `ds.skip(K)` before iteration, window `[K, K+max_samples)` —
      `…::test_offset_skips_then_windows` (`.skip(5)`, rows `[5,15)`); adversarial window check.
- [x] PASS — `offset=0` treated as no-skip (identical to None) — `…::test_offset_zero_never_skips`;
      arxiv_dataset.py:108 `if offset:` guard.
- [x] PASS — `ingest_arxiv_dataset(offset=K)` forwards `offset=K`; default forwards `offset=None` —
      `test_arxiv_dataset_pipeline.py::TestIngestArxivDataset::{test_forwards_offset_to_fetch_dataset_batches,
      test_defaults_offset_to_none}`; arxiv_dataset_pipeline.py:119.
- [x] PASS — `_ingest_arxiv_dataset_entry` passes `offset=entry.offset` —
      `test_pipeline.py::TestDataWorker::{test_forwards_huggingface_dataset_offset_window,
      test_passes_huggingface_dataset_overrides}`; pipeline.py:126; live threading check above.
- [x] PASS — streaming-fetch log line includes `offset` — `…::test_window_log_line_includes_offset`;
      arxiv_dataset.py:99 (leading line) + :129-135 (window `[offset, offset+count)` close line).
- [x] PASS — dedup/load path unchanged, overlapping windows re-upsert — verify-only; no diff to
      `load_document`; idempotency on `(user_id, source_uri)` intact (production diff is additive only).
- [x] PASS — format/lint/pre-commit clean — see Test summary.
- [x] PASS — `make memory-unit-tests` green, 0 warnings — see Test summary.
- [x] PASS — `make memory-integration-tests` (fast tail) green for the touched data path —
      `tests/integration/data/test_pipeline.py` arxiv-dispatch assertion includes `offset=None`.

**Evidence**
```
$ make memory-format-check && make memory-lint-check
280 files already formatted
All checks passed!

$ make pre-commit
ruff check ... Passed / ruff format ... Passed / prettier ... Passed / biome check (harness) ... Passed

$ make memory-unit-tests
============================ 1570 passed in 47.13s =============================

$ make memory-integration-tests
========== 178 passed, 1 skipped, 104 deselected in 176.98s (0:02:56) ==========
```

**Other issues found**
- Minor (non-blocking): `fetch_dataset_batches(offset=2.5)` raises a raw `TypeError` from the slice
  rather than a friendly validation message. Unreachable through the real call chain (`offset` is a
  Pydantic `int | None` field), and it fails loudly. Optional follow-up if a stricter type guard is
  ever wanted; not required for this task.
- All arXiv-stream access in the touched tests is mocked (`_FakeStreamingDataset` + patched
  `load_dataset`; patched `_fetch_dataset_batches`; `batch_gen`/`_empty_batches` fakes) — verified
  NO test makes a real HuggingFace network call.

**VERDICT: PASS**
