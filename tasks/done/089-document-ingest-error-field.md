---
id: 089-document-ingest-error-field
feature: brightdata-youtube-transcripts
status: done
---

# Add `ingest_error` to Document + retryable errored rows in `load_video_document`

Tags: `data`
Depends on: None
Blocks: #092
Implements: ADR-004

## Scope

Add the persisted-failure primitive the feature needs, with NO producer yet (nothing
writes `ingest_error` until #092):

1. **Entity.** Add `ingest_error: str | None = None` to the shared `Document` ODM
   (`apps/memory/src/tree/entities/documents.py`), documented as: a normalized failure
   marker (`"<code>: <message>"`, e.g. `"no_transcript: …"`, `"invalid_url: …"`) written
   when ingestion could not produce content; failure rows carry `content=None`. Nullable
   field → no migration, no index. Only the YouTube path writes it in this feature.

2. **Retry semantics.** In `youtube_video.load_video_document`
   (`apps/memory/src/tree/data/youtube/youtube_video.py:145-168`), a row with
   `ingest_error is not None` becomes REPLACEABLE on a later run, exactly like
   `SourceType.LATENT`. Change the skip condition from
   `if existing and existing.source_type != SourceType.LATENT` to also allow replacement
   when `existing.ingest_error is not None`. When re-attempting a previously-errored row,
   log a WARNING naming the `source_uri` and the prior `ingest_error`. NO attempt cap,
   no counter field. A successful re-ingest replaces the row with a normal Document
   (`ingest_error=None`); a failed re-ingest (in #092) replaces it with a fresh failure
   row.

3. **Downstream verification (read-only).** Verify the extraction pipeline already
   excludes `content=None` rows via `{"content": {"$ne": None}}` at
   `memory/extraction/sharding.py:153` and `memory/extraction/pipeline.py:1801` — no
   downstream change expected or allowed in this task. Record both file:line references
   in the log.

Unit tests only (call the `/squid-testing-python` skill): the new skip/replace/WARNING
behavior of `load_video_document` (errored row replaced; non-errored non-LATENT row
still skipped; LATENT upgrade unchanged; WARNING emitted on re-attempt), plus a
`Document` round-trip asserting `ingest_error` defaults to `None` and persists a string.

## Acceptance criteria

- [x] `Document.ingest_error: str | None = None` exists on the shared ODM with a
      docstring/comment defining the normalized `"<code>: <message>"` format; no new
      index; `Settings.indexes` unchanged.
- [x] `load_video_document` replaces an existing row when `existing.ingest_error is not
      None` (same `doc.id` reuse + `replace()` path as the LATENT upgrade) and logs a
      WARNING naming the `source_uri` and the prior error string.
- [x] `load_video_document` still: skips an existing non-LATENT, non-errored duplicate
      (returns `None`); upgrades a LATENT row; treats concurrent `DuplicateKeyError` as
      a clean skip. Covered by unit tests.
- [x] Extraction exclusion verified read-only: `sharding.py:153` and `pipeline.py:1801`
      filter `{"content": {"$ne": None}}`; ZERO diff under `apps/memory/src/tree/memory/`.
- [x] No producer added: `git grep "ingest_error" apps/memory/src/tree` matches only
      `entities/documents.py` and `data/youtube/youtube_video.py`.
- [x] `make memory-format-check && make memory-lint-check && make pre-commit` clean;
      `make memory-unit-tests` green, 0 warnings.

## Out of scope

- Writing `ingest_error` anywhere (the producers land in #092). Building failure
  Documents. Any change to extraction, other sources' load paths, or indexes.

## Log

### [SWE] 2026-07-24 15:20 — Implementation

**Files modified**

- `apps/memory/src/tree/entities/documents.py` — adds `ingest_error: str | None = None`
  to the shared `Document` ODM with a comment defining the normalized
  `"<code>: <message>"` format (never a raw exception dump), the `content=None`
  pairing, and why it is unindexed. `Settings.indexes` untouched.
- `apps/memory/src/tree/data/youtube/youtube_video.py` — `load_video_document` now
  treats `existing.ingest_error is not None` as REPLACEABLE (same `doc.id` reuse +
  `replace()` as the LATENT upgrade), logs a WARNING naming `source_uri` + the prior
  error on re-attempt, and reports the replace outcome accurately
  (`Replaced errored document` vs `Upgraded latent document`). No attempt cap, no
  counter field.
- `apps/memory/tests/unit/entities/test_documents.py` — `TestDocumentIngestError`:
  default `None`, `"<code>: <message>"` round-trip with `content=None`, and that no
  declared index keys on `ingest_error`.
- `apps/memory/tests/unit/data/youtube/test_youtube_video.py` — `TestLoadVideoDocument`
  decision table: errored row replaced, re-attempt WARNING content, non-LATENT
  non-errored duplicate still skipped, LATENT upgrade unchanged + emits no WARNING.

**Tests**

- Unit: 1732 passing, 0 failing, 0 warnings — `make memory-unit-tests`.
- Integration: run for context (`make memory-integration-tests`) → 167 passed, 2 failed.
  Both failures (`test_indexing_pipeline.py::test_embeds_nodes`,
  `test_meta_state.py::TestRecordDreamRun::test_updated_at_is_recent`) are PRE-EXISTING:
  reproduced identically on a clean tree with my changes stashed (`git stash push --
  apps/memory/src apps/memory/tests`). Neither touches `data/youtube` or `entities`.
- Red/green confirmed: the 4 new assertions failed with
  `AttributeError: 'Document' object has no attribute 'ingest_error'` before the entity
  change (a real behavioral red, not a collection error).

**Acceptance criteria**

- [x] `Document.ingest_error` exists, documented, unindexed — verified by
      `tests/unit/entities/test_documents.py::TestDocumentIngestError` (3 tests,
      incl. `test_ingest_error_is_not_indexed`).
- [x] Errored row replaced + WARNING — verified by
      `tests/unit/data/youtube/test_youtube_video.py::TestLoadVideoDocument::test_replaces_existing_errored_row`
      and `::test_warns_with_source_uri_and_prior_error_on_reattempt`.
- [x] Existing skip/upgrade/DuplicateKeyError behavior unchanged — verified by
      `::test_skips_existing_non_latent_document_without_error`,
      `::test_upgrades_latent_document`,
      `::test_latent_upgrade_does_not_emit_reattempt_warning`,
      `::test_returns_none_on_duplicate_key_race`.
- [x] Extraction exclusion verified read-only — `memory/extraction/sharding.py:153`
      (`Document.find({"user_id": user_id, "content": {"$ne": None}})`) and
      `memory/extraction/pipeline.py:1801-1803`
      (`Document.find({"user_id": user_id, "content": {"$ne": None}})`, the
      no-`document_ids` branch). `git diff --stat -- apps/memory/src/tree/memory/`
      is EMPTY.
- [x] No producer added — `git grep -n "ingest_error" apps/memory/src/tree` matches only
      `entities/documents.py:50` and `data/youtube/youtube_video.py` (lines 148/160/166/170:
      docstring + the read-side condition and WARNING). Nothing writes the field.
- [x] Format/lint/pre-commit clean; unit tests green with 0 warnings.

**Evidence**

```
$ make memory-unit-tests
============================ 1732 passed in 42.58s =============================

$ make memory-format-check && make memory-lint-check && make pre-commit
289 files already formatted
All checks passed!
prettier.................................................................Passed
ruff check...............................................................Passed
ruff format..............................................................Passed
biome check (harness)....................................................Passed

$ git diff --stat -- apps/memory/src/tree/memory/
(empty)

$ git grep -n "ingest_error" apps/memory/src/tree
apps/memory/src/tree/data/youtube/youtube_video.py:148:    - a row carrying an `ingest_error` — a persisted ingest failure. Every
apps/memory/src/tree/data/youtube/youtube_video.py:160:        and existing.ingest_error is None
apps/memory/src/tree/data/youtube/youtube_video.py:166:        if existing.ingest_error is not None:
apps/memory/src/tree/data/youtube/youtube_video.py:170:                existing.ingest_error,
apps/memory/src/tree/entities/documents.py:50:    ingest_error: str | None = None
```

End-to-end against the real LOCAL MongoDB (throwaway db `e2e_089_ingest_error`,
dropped afterwards; scratchpad script, NOT committed; no Bright Data / Gemini call):

```
$ uv run --env-file ../../.env python .../e2e_089.py
Re-attempting previously failed ingest: https://www.youtube.com/watch?v=eYaWxljC4sA (prior error: no_transcript: no captions on either backend)
Replaced errored document: https://www.youtube.com/watch?v=eYaWxljC4sA
Skipping duplicate: https://www.youtube.com/watch?v=eYaWxljC4sA
[1] persisted failure row id=6a633b461503cb897ed86642 error=no_transcript: no captions on either backend
[2] re-attempt -> id=6a633b461503cb897ed86642 same_id=True stored.content='the transcript body' stored.ingest_error=None
[3] third run (now clean row) -> None; rows=1
```

**Notes**

- `make env-status` → `local` for every run. No live Bright Data or Gemini call in any
  test or in the e2e script.
- The worktree was missing the gitignored `.env` / `.env.prod` (every `make` target
  aborts without them); copied from the main checkout. Also ran
  `uv sync --extra local-models` in `apps/memory` — the worktree venv lacked
  `modal` / `sentence_transformers`, which broke unit-test collection.
- Deliberate small deviation: the post-replace INFO message is now
  `"Replaced errored document: %s"` on the errored branch instead of reusing
  `"Upgraded latent document: %s"`, which would have been factually wrong for a
  non-LATENT errored YouTube row. The latent wording is unchanged, so it still matches
  the sibling loaders (`substack_rss.py:131`, `arxiv_dataset.py:194`).
- No attempt cap by design (ADR-004 §6): a permanently transcript-less video re-runs
  both backends on every batch that includes it. The WARNING is the only visibility.
  Upgrade path if measured spend says so: an attempt counter field + cap — a new task,
  not a silent addition here.
- If a LATENT row ever carries an `ingest_error`, the errored branch wins (WARNING +
  "Replaced errored document") — the persisted failure is the more informative fact.
- `docs/glossary.md` shows as modified in `git status`: that is PA's pre-existing
  uncommitted grooming edit, untouched by me.

### [Tester] 2026-07-24 13:35 — QA

**Test summary**
- Format / lint / pre-commit: PASS (`make memory-format-check`, `make memory-lint-check`, `make pre-commit` all green)
- Unit tests: 1732 passed / 0 failed
- Integration tests: 167 passed / 2 failed, 1 skipped, 105 deselected — both failures independently reproduced as PRE-EXISTING (see evidence below)
- Warnings: 0

**E2E adversarial pass**
- Happy path: fresh `load_video_document(doc)` insert then a second call with the same `source_uri` → first call persists, second call returns `None` (clean skip). PASS.
- Break path 1 (pathological state: a `LATENT` row that ALSO carries `ingest_error`): manually inserted a `Document(source_type=LATENT, ingest_error="no_transcript: ...")`, then called `load_video_document` with a real YOUTUBE doc at the same `source_uri` → replaced (same `doc.id` reused), WARNING logged naming `source_uri` + prior error, outcome logged as "Replaced errored document", persisted row ends up `source_type=YOUTUBE, ingest_error=None`. Matches the SWE's documented design decision ("the errored branch wins"). PASS.
- Break path 2 (boundary: empty-string `ingest_error` vs `None`): inserted a row with `ingest_error=""` → `load_video_document` still replaced it (correctly uses `is not None`, not truthiness) — confirms the code does not accidentally skip an empty-string error as if it were `None`. PASS.
- Break path 3 (concurrency: two coroutines racing to replace the same already-existing errored row via `asyncio.gather`): both calls completed without exception, final DB state was exactly 1 row (no duplicate), one of the two writes won (last-writer-wins on `replace()`-by-`_id`, no `DuplicateKeyError` possible here since both share `doc.id`). No crash, no corruption, no duplicate rows. PASS — noted below as a design characteristic worth a comment, not a bug (see "Other issues found").
- Break path 4 (state edge: re-running against a row that was just successfully replaced): a 3rd call against the now-clean row returns `None` (skip), confirming the errored-row replaceability doesn't linger after a successful re-ingest. PASS.

All 4 e2e checks (script: `/private/tmp/.../scratchpad/e2e_089_qa.py`, run via `uv run --env-file ../../.env python ...` against throwaway local Mongo db `qa_089_adversarial`, dropped before and after run; no live Bright Data / Gemini call) printed `ALL ADVERSARIAL CHECKS PASSED`.

**Acceptance criteria**
- [x] PASS — `Document.ingest_error: str | None = None` exists on the shared ODM, documented, unindexed, `Settings.indexes` unchanged.
      Evidence: `apps/memory/src/tree/entities/documents.py:41-50` (comment + field); `Settings.indexes` diff is empty (only the field addition); `tests/unit/entities/test_documents.py::TestDocumentIngestError::test_ingest_error_is_not_indexed` passes.
- [x] PASS — `load_video_document` replaces an existing row when `existing.ingest_error is not None` (same `doc.id` reuse + `replace()`) and logs a WARNING naming `source_uri` + prior error.
      Evidence: `apps/memory/src/tree/data/youtube/youtube_video.py:157-177`; `tests/unit/data/youtube/test_youtube_video.py::TestLoadVideoDocument::test_replaces_existing_errored_row` and `::test_warns_with_source_uri_and_prior_error_on_reattempt` pass; manually reproduced (break path 1/2/3 above).
- [x] PASS — skip/upgrade/`DuplicateKeyError` behavior unchanged.
      Evidence: `::test_skips_existing_non_latent_document_without_error`, `::test_upgrades_latent_document`, `::test_latent_upgrade_does_not_emit_reattempt_warning`, `::test_returns_none_on_duplicate_key_race` all pass; manually reproduced happy-path skip and break-path-4 skip.
- [x] PASS — extraction exclusion verified read-only, zero diff under `apps/memory/src/tree/memory/`.
      Evidence: `apps/memory/src/tree/memory/extraction/sharding.py:153` and `apps/memory/src/tree/memory/extraction/pipeline.py:1801-1803` both filter `{"content": {"$ne": None}}`; `git diff --stat -- apps/memory/src/tree/memory/` is empty.
- [x] PASS — no producer added.
      Evidence: `git grep -n "ingest_error" apps/memory/src/tree` matches only `entities/documents.py:50` and `data/youtube/youtube_video.py:148,160,166,170` (docstring + read-side condition + WARNING arg) — nothing writes the field outside the entity default.
- [x] PASS — format/lint/pre-commit clean, unit tests green, 0 warnings.
      Evidence: see Test summary above; full output captured below.

**Evidence**
```
$ make memory-unit-tests
============================ 1732 passed in 42.37s =============================

$ make memory-format-check
289 files already formatted
$ make memory-lint-check
All checks passed!
$ make pre-commit
prettier.................................................................Passed
ruff check...............................................................Passed
ruff format..............................................................Passed
biome check (harness)....................................................Passed

$ make memory-integration-tests   # SWE's tree
===== 2 failed, 167 passed, 1 skipped, 105 deselected in 187.25s =====
FAILED tests/integration/memory/test_indexing_pipeline.py::TestMemoryIndexingPipeline::test_embeds_nodes
FAILED tests/integration/memory/test_meta_state.py::TestRecordDreamRun::test_updated_at_is_recent

$ git stash push -- apps/memory/src apps/memory/tests && make memory-integration-tests   # clean tree, independent repro
===== 2 failed, 167 passed, 1 skipped, 105 deselected in 197.45s =====
FAILED tests/integration/memory/test_indexing_pipeline.py::TestMemoryIndexingPipeline::test_embeds_nodes
FAILED tests/integration/memory/test_meta_state.py::TestRecordDreamRun::test_updated_at_is_recent
$ git stash pop   # restored SWE's changes
```
Identical failures, identical counts, on the clean stashed tree — confirms PRE-EXISTING and unrelated to this diff (neither failing test touches `data/youtube` or `entities`; failures are shared-DB/test-ordering flakiness in the full suite — both tests pass individually in isolation).

**Other issues found**
- Concurrent replace of an *already-existing* errored row (two workers re-attempting the same failed video in the same batch) is last-writer-wins with no ordering guarantee, unlike the `DuplicateKeyError`-protected fresh-insert path. Not a bug — the spec explicitly calls for no attempt cap/counter and this is an inherent property of reusing `doc.id` via `replace()` rather than a unique-index-protected insert — but worth a one-line code comment or a follow-up-task note if concurrent same-video re-attempts become a realistic batch pattern.
- The `LATENT` + `ingest_error` combination (should not occur via any current producer) is documented in the SWE's log and behaves as documented (errored branch wins) but has no dedicated unit test. Confirmed correct by manual e2e (break path 1). Suggest an explicit unit test for this combination in a follow-up if it's ever reachable in practice.
- Independently confirmed via file mtimes that `docs/glossary.md`, `docs/adrs/004_*.md`, `tasks/090-093`, and the YouTube fixtures directory were not touched by the SWE during this task (all pre-date the SWE's edit window by ~18-20 minutes).
- No `.env`/`.env.prod` leakage into `git status` or the diff; no `pyproject.toml`/`uv.lock` changes from the `uv sync --extra local-models` the SWE ran.

**VERDICT: PASS**
