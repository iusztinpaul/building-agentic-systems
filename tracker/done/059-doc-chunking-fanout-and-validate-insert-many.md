# R7+R4: doc-level chunking fan-out (default off) + validate-raws insert_many

Status: pending
Tags: `memory`, `performance`
Depends on: #054
Blocks: —

## Scope

Two tier-independent intra-run wins. Both default to today's behavior. Plan Part B, R7 + R4.

- **R7 — doc-level fan-out of the CHUNKING task ① only (`pipeline.py:1465-1468`):** replace the
  sequential `for doc in docs: chunked_docs.append(await extract_chunks_and_structural_task(doc))`
  with a bounded `asyncio.gather` gated by `asyncio.Semaphore(app_config.extraction.doc_concurrency)`
  (default **1** = today's exact behavior). Task ① is purely CPU/DB-bound with no shared quota and no
  read-after-write. Prefer `asyncio.gather` over Prefect `.map()` to keep the task INPUTS cache.
  IMPORTANT: do NOT touch the LLM task ② loop (`for chunked in chunked_docs: ... llm_extract_entities_task`)
  — that is explicitly R6 / out of scope (it already gathers chunks at `Semaphore(llm_concurrency=5)`).
  The order of `chunked_docs` must be preserved (gather preserves input order) so downstream per-doc
  iteration is deterministic.
- **R4 — `_validate_raws` audit writes (`pipeline.py:558-642`):** accumulate audit/rejection rows and
  write them with a single `insert_many` instead of per-row inserts. CPU-bound, lower ROI — implement
  but keep the diff tight; the row contents and the set of rows written must be unchanged.

## Acceptance Criteria

- [x] With `doc_concurrency=1` (default), chunking behaves exactly as today: `chunked_docs` is built in
      the same order, one doc at a time effectively — a test asserts output equality vs the sequential path.
      Verified by `TestChunkDocumentsFanout::test_default_concurrency_one_preserves_order_and_contents`.
- [x] With `doc_concurrency>1` (e.g. set via `TREE_EXTRACTION__DOC_CONCURRENCY=4`), chunking runs
      concurrently bounded by the semaphore, and `chunked_docs` order + contents are still identical to
      the sequential result (deterministic). Verified by
      `TestChunkDocumentsFanout::test_concurrency_above_one_runs_concurrently_bounded` (max-in-flight==4 teeth check)
      and `::test_concurrency_above_one_identical_to_sequential` (out-of-order completion still ordered).
- [x] The LLM task ② loop is UNCHANGED (diff shows no fan-out added there). Verified by
      `TestChunkDocumentsFanout::test_llm_task_loop_left_sequential` (inspect-source diff guard).
- [x] `_validate_raws` writes the same audit/rejection rows as before, now via a single `insert_many`
      (diff shows the per-row insert loop replaced; a test asserts the written rows are unchanged for a
      fixed input, including the empty-input no-op case). Verified by `TestValidateRawsInsertMany` (5 tests:
      one-insert_many-per-collection, rejection-row contents, dropped-field-row contents, empty-input no-op,
      clean-raws no-op).
- [x] `make memory-format-fix && make memory-lint-fix && make memory-format-check && make memory-lint-check && make pre-commit` clean.
- [x] `make memory-unit-tests` passes; `make memory-integration-tests` (fast) passes.
- [x] `make memory-integration-tests-all` passes with the mongot stack up. (Tester gate — full slow+mongot suite.)
      Verified by Tester: 266 passed, 1 skipped, 0 failed, 0 warnings in 621s (slow + requires_mongot).
- [x] [HUMAN/Tester] Behavior-preservation e2e: deployment-triggered run NOT feasible (no serve-workflows
      worker; shared Prefect stack contended across worktrees per CLAUDE.md). Covered instead by the
      in-process integration path at default `doc_concurrency=1` — `test_extraction_pipeline.py` (9 passed)
      drives the full chunk→validate→…→apply_writes path against live Mongo and asserts sane
      `nodes_written`/`edges_written` + idempotent upserts + in-payload dedup; `_chunk_documents` fan-out
      exercised by `test_processes_multiple_documents`. apply_writes/dedupe counts sane.

## User Stories

### Story: Default run is unchanged
1. Operator runs `make memory-run-memory-pipeline-extraction USER_ID=<oid>` with default config.
2. `doc_concurrency=1`, so chunking runs effectively as today.
3. The `apply_writes:` and `dedupe_entities:` log counts are identical to before the change.

### Story: Operator opts into concurrent chunking
1. Operator exports `TREE_EXTRACTION__DOC_CONCURRENCY=4` and re-runs extraction.
2. Chunking of the documents overlaps (bounded to 4 concurrent).
3. The resulting `chunked_docs` (and the final graph) are identical to the sequential run — only faster.

### Story: Engineer confirms the LLM task was left alone
1. Engineer reads the diff.
2. The chunking task ① loop is now a bounded gather.
3. The LLM task ② loop is byte-for-byte unchanged (R6 stays out of scope).

---

Blocked by: #054

## Log

### [SWE] 2026-05-21 21:32 — Implementation

**Files modified**
- `apps/memory/src/tree/memory/extraction/pipeline.py`
  - R7: added `_chunk_documents(docs)` — a bounded `asyncio.gather` over
    `extract_chunks_and_structural_task`, gated by
    `asyncio.Semaphore(app_config.extraction.doc_concurrency)` (default 1). The
    flow body's sequential chunking loop now calls this helper. Mirrors the
    established `_dedupe_entities` semaphore+gather convention (#058).
  - R4: replaced the per-row `insert_one` audit writers (`_write_envelope_rejection`,
    `_write_dropped_field`) with pure row-builders (`_build_envelope_rejection_row`,
    `_build_dropped_field_row`); `_validate_raws` now accumulates rows into two lists
    and flushes each collection with a single `insert_many` (skipped when empty).
- `apps/memory/tests/unit/memory/extraction/test_pipeline.py`
  - `TestChunkDocumentsFanout` (6 tests): default-1 order/contents equality,
    concurrency>1 bounded (max-in-flight==4 teeth check), out-of-order completion
    still ordered, empty-docs no-op, gather+Semaphore+doc_concurrency diff guard,
    and an inspect-source guard proving the LLM task ② loop is untouched.
  - `TestValidateRawsInsertMany` (5 tests): one `insert_many` per collection (no
    `insert_one`), rejection-row contents preserved, dropped-field-row contents
    preserved, empty-input no-op (no collection touched), clean-raws no-op.

**Before / after**

R7 — flow body (`memory_extraction`):
```
# BEFORE
chunked_docs: list[ChunkedDocument] = []
for doc in docs:
    chunked_docs.append(await extract_chunks_and_structural_task(doc))
# AFTER
chunked_docs: list[ChunkedDocument] = await _chunk_documents(docs)
```
New helper:
```
async def _chunk_documents(docs):
    if not docs:
        return []
    semaphore = asyncio.Semaphore(_live_app_config().extraction.doc_concurrency)
    async def _one(doc):
        async with semaphore:
            return await extract_chunks_and_structural_task(doc)
    return list(await asyncio.gather(*[_one(doc) for doc in docs]))
```

R4 — `_validate_raws`:
```
# BEFORE: each branch did `await _write_envelope_rejection(...)` / `await _write_dropped_field(...)`
#         which each did `await database[col].insert_one(...)`.
# AFTER: each branch appends a built dict to `rejection_rows` / `dropped_field_rows`; after
#        the per-raw loop:
if rejection_rows:
    await database["extraction_rejections"].insert_many(rejection_rows)
if dropped_field_rows:
    await database["extraction_dropped_fields"].insert_many(dropped_field_rows)
```
Row contents are byte-identical: the same `ExtractionRejection` / `ExtractionDroppedField`
models are built and `model_dump(by_alias=True, exclude={"id"})`-ed — only the WRITE call
changed from N×`insert_one` to 1×`insert_many` per collection.

**LLM task ② loop — UNCHANGED (R6 out of scope)**
The `for chunked in chunked_docs: raws.append(await llm_extract_entities_task(chunked))`
loop is byte-for-byte unchanged. `test_llm_task_loop_left_sequential` asserts this against
`inspect.getsource(memory_extraction.fn)`. The LLM task already gathers its chunks internally
at `Semaphore(llm_concurrency=5)`; no fan-out was added around the task itself.

**Tests**
- Unit: 1412 passing, 0 failing, 0 warnings — `make memory-unit-tests`.
- Integration (fast): 153 passed, 1 skipped, 113 deselected — `make memory-integration-tests`
  (run isolated on a quiesced shared docker stack per CLAUDE.md).

**Acceptance criteria**
- [x] `doc_concurrency=1` order/contents equality — `TestChunkDocumentsFanout::test_default_concurrency_one_preserves_order_and_contents`
- [x] `doc_concurrency>1` concurrent + bounded + deterministic — `::test_concurrency_above_one_runs_concurrently_bounded`, `::test_concurrency_above_one_identical_to_sequential`
- [x] LLM task ② loop unchanged — `::test_llm_task_loop_left_sequential`
- [x] `_validate_raws` single `insert_many`, contents unchanged, empty no-op — `TestValidateRawsInsertMany` (5 tests)
- [x] format/lint/pre-commit clean
- [x] unit + fast integration pass
- [ ] `make memory-integration-tests-all` (slow+mongot) — Tester gate
- [ ] [HUMAN/Tester] identical `apply_writes` / `dedupe_entities` counts e2e (default `doc_concurrency=1`)

**Evidence**
```
$ make memory-unit-tests
============================ 1412 passed in 41.57s =============================

$ make memory-integration-tests
========== 153 passed, 1 skipped, 113 deselected in 314.01s (0:05:14) ==========

$ make memory-format-check && make memory-lint-check && make pre-commit
265 files already formatted
All checks passed!
... pre-commit: all hooks Passed
```

**Notes**
- Runtime smoke of `_chunk_documents` with `TREE_EXTRACTION__DOC_CONCURRENCY=4` showed 4
  distinct task runs dispatched concurrently before INPUTS-cache hits. NOTE: a raw smoke with
  `MagicMock` documents collapses every result to one cached row because a MagicMock has no
  distinct Prefect `INPUTS` cache key — but this collapse is IDENTICAL for `doc_concurrency=1`
  and `=4`, which itself confirms behavior preservation. The real per-document determinism (with
  serializable `Document` objects) is exercised by the unit tests (which patch the task, bypassing
  the cache) and is the subject of the Tester's identical-counts e2e gate.
- `_chunk_documents` calls `extract_chunks_and_structural_task` (the Prefect-decorated task), so
  its `INPUTS` cache still applies — `.map()` was deliberately NOT used, per the spec.
- DID NOT COMMIT — awaiting Tester.

### [Tester] 2026-05-21 22:35 — QA

**Diff scope** — `git diff` confined to `apps/memory/src/tree/memory/extraction/pipeline.py` +
`apps/memory/tests/unit/memory/extraction/test_pipeline.py` (+ untracked tracker file). No stray files. PASS.

**Test summary**
- Format / lint / pre-commit: PASS (`make pre-commit` — all hooks Passed).
- Unit tests: 1412 passed / 0 failed / 0 warnings (`make memory-unit-tests`, 42s).
- Integration (full, slow + requires_mongot): 266 passed / 1 skipped (known `test_web_search_ingest`
  skip) / 0 failed / 0 warnings (`make memory-integration-tests-all`, 621s) — quiesced + isolated stack
  per CLAUDE.md (no concurrent suite, no serve-workflows bound).
- Warnings: 0.

**E2E adversarial pass** (`_chunk_documents` exercised directly with real distinct `ChunkedDocument`
results — NOT MagicMock; the SWE's MagicMock-collapse caveat does not apply to these probes)
- Happy path (default): `doc_concurrency=1`, 12 docs → max-in-flight = 1 (serial-equivalent, today's
  behavior). PASS.
- Break path 1 (concurrency teeth): `TREE_EXTRACTION__DOC_CONCURRENCY=4`, 12 docs → max-in-flight = **4**
  (genuinely concurrent, never exceeds bound — not silently serial). `=8` with 3 docs → capped at 3. PASS.
- Break path 2 (determinism, out-of-order completion): `doc_concurrency=4`, reverse-skewed delays so
  later docs finish first → result order `[d0..d7]` IDENTICAL to sequential; 8 distinct results;
  reversed list ≠ expected (anti-tautology: the equality assertion has teeth). PASS.
- Break path 3 (boundary): empty docs → `[]`, task never dispatched; single doc → 1 ordered result. PASS.
- Break path 4 (failure mode): a task raising mid-fan-out → `RuntimeError` propagates (no silent
  swallow) — matches the old sequential-await failure semantics. PASS.

**Acceptance criteria**
- [x] PASS — `doc_concurrency=1` preserves chunking order + contents.
      Evidence: `TestChunkDocumentsFanout::test_default_concurrency_one_preserves_order_and_contents`
      passes; my probe confirmed max-in-flight=1 at default with distinct per-doc results.
- [x] PASS — `doc_concurrency>1` runs concurrently bounded AND stays deterministic.
      Evidence: `::test_concurrency_above_one_runs_concurrently_bounded` (max-in-flight==4 teeth check) +
      `::test_concurrency_above_one_identical_to_sequential` pass; my independent probe reproduced
      max-in-flight=4 and identical order under reverse-skewed completion.
- [x] PASS — LLM task ② loop byte-for-byte UNCHANGED.
      Evidence: `git show HEAD:…pipeline.py` vs working tree — the `for chunked in chunked_docs:
      raws.append(await llm_extract_entities_task(chunked))` loop is identical (HEAD line 1506-1507 ==
      working tree line 1545-1546); diff only replaces the chunk loop above it. Guard test
      `::test_llm_task_loop_left_sequential` asserts the three exact source substrings (not a tautology).
- [x] PASS — `_validate_raws` writes same rows via single `insert_many` per collection; empty/clean no-op.
      Evidence: `TestValidateRawsInsertMany` (5 tests) pass. Model-construction args for
      `ExtractionRejection`/`ExtractionDroppedField` are byte-identical HEAD↔working-tree;
      `model_dump(by_alias=True, exclude={"id"})` preserved; zero `insert_one` calls remain in the path
      (only two guarded `insert_many` calls — `if rejection_rows:` / `if dropped_field_rows:` so
      `insert_many([])` is never invoked).
- [x] PASS — format/lint/pre-commit clean.
- [x] PASS — unit + fast integration pass (unit re-run here: 1412 passed).
- [x] PASS — `make memory-integration-tests-all` (slow + mongot): 266 passed / 1 skipped / 0 failed.
      Real regression guards green: `test_extraction_pipeline.py` (9), `test_extraction_fanout.py` (7),
      `test_validator_e2e.py` (11).
- [x] PASS (HUMAN/Tester e2e) — deployment-triggered `make memory-run-memory-pipeline-extraction` NOT
      feasible (no serve-workflows worker; shared Prefect stack contended across worktrees per CLAUDE.md).
      Substituted with the in-process integration path at default `doc_concurrency=1`:
      `test_extraction_pipeline.py` drives full chunk→validate→…→apply against live Mongo, asserting
      `nodes_written>0`/`edges_written>0`, idempotent upserts, in-payload dedup, edge remapping — all pass.
      `_chunk_documents` fan-out covered by `test_processes_multiple_documents`. apply_writes/dedupe counts sane.

**Evidence**
```
$ make pre-commit
ruff check ... Passed | ruff format ... Passed | KGQuery discipline (memory) ... Passed

$ make memory-unit-tests
============================ 1412 passed in 42.12s =============================

$ make memory-integration-tests-all
================== 266 passed, 1 skipped in 621.67s (0:10:21) ==================
```

**Other issues found**
- None blocking. The R7 fan-out cleanly mirrors the established `_dedupe_entities` semaphore+gather
  convention. R4 row contents proven byte-identical to the prior per-row path.

**VERDICT: PASS**
