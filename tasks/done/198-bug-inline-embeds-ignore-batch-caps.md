---
id: 198-bug-inline-embeds-ignore-batch-caps
status: done
feature: bug-embedding-batching
---

# Bug: Ingestion embeds ignore `models.embedding_batch`, so one document goes out as one oversized request

**Severity:** S3 — a documented knob (`models.embedding_batch`, sized to the free-tier Voyage 10K TPM window) is silently ignored on the ingestion path; the indexing backfill honours it, so a workaround exists.
**Affected component(s):** `apps/memory/src/tree/memory/pipeline.py` (`_embed_children`, `_embed_entities`), `apps/memory/src/tree/memory/graph/resolution/semantic.py`, `apps/memory/src/tree/memory/embedding_text.py`
**First observed:** 2026-10-09, live e2e of embedding batching (rag + graphrag, Voyage + Modal).

## Summary

`models.embedding_batch` (`max_inputs` 1000, `max_total_tokens` 10000, `max_input_tokens` 32000) is meant to bound every embed request. The indexing backfill obeys it; the three inline ingestion embeds do not — they send the whole run's texts in as few requests as the Voyage HARD per-request caps allow (1000 inputs / 320K tokens). A document whose child chunks total more than ~10K tokens therefore goes out as ONE request, which on the free-tier key (`concurrency.voyage_tpm: 10000`) can never fit the TPM window: the 429 backoff exhausts and the ingestion fails, while the same rows would backfill fine.

## Reproducer (deterministic)

Live (2026-10-09, separate local DB, `TREE_MODELS__EMBEDDING_BATCH__MAX_INPUTS=8` exported in the serving shell):

1. `make memory-run-pipeline MODE=online SOURCE=$PWD/docs/adrs/010_document_natural_key.md …` → `embed_children: … n_texts=12`, ONE `voyage-embeddings` slot acquisition (= one POST).
2. `make memory-reset-embeddings CONFIRM=yes …` then `make memory-run-indexing-pipeline …` → 26 texts, FOUR slot acquisitions (= ⌈26/8⌉ POSTs).

As a unit test: patch `app_config.models.embedding_batch.max_inputs` to 2, run `_embed_children` / `_embed_entities` / `SemanticResolver` pre-embed over 5 texts with a recording fake embedding model.

Expected: 3 `.embed()` calls of ≤ 2 texts each (the configured cap), on all three paths.
Actual: 1 call with all 5 texts (the `embed_in_batches` defaults `max_inputs=1000`, `max_total_tokens=320_000`).

## Suspected localisation

- `apps/memory/src/tree/memory/pipeline.py:486` — `embed_in_batches(texts, get_search_embedding_model(), input_type="document")` passes no caps.
- `apps/memory/src/tree/memory/pipeline.py:1192` — same call for entities.
- `apps/memory/src/tree/memory/graph/resolution/semantic.py:89` — same call for resolution (no role).
- `apps/memory/src/tree/memory/embedding_text.py:98-105` — `embed_in_batches` defaults to the Voyage hard caps; only `embed_texts` (`:329`) resolves `app_config.models.embedding_batch` via `_resolve_batch_caps` (`:355`).

> Hypotheses, not conclusions.

## Out of scope

- `dispatch_concurrency` and `query.embedding_batch_size` (task 201).
- A token-weighted rate limiter (the TPM cap stays held by config, as `concurrency.voyage_tpm` says).
- Response-length checks (task 199) and Modal 400 handling (task 200).

## Acceptance criteria

- [x] **Regression test** in `apps/memory/tests/unit/memory/test_embedding_text.py` (and/or the pipeline / resolution tests) that fails on `main` and passes on the fix: with `max_inputs` (and separately `max_total_tokens`) lowered in `app_config`, each of the three ingestion paths splits into the configured number of `.embed()` calls. Test names describe the symptom.
- [x] ONE place resolves the caps: every caller that embeds a list goes through the config-resolving seam, so no call site can silently fall back to the hard Voyage caps again (removing the hard-coded defaults from `embed_in_batches`, or routing the three callers through `embed_texts`, are both acceptable shapes; pick the simpler).
- [x] Vectors stay positionally aligned and the Embedding role is unchanged on every path (`document` for children/entities, `None` for resolution).
- [x] Docstrings that say "as few requests as the Voyage caps allow" / name the 320K default are corrected.
- [x] Unit suite green (`make memory-tests`, local target), format + lint + pre-commit clean.

## Notes for the SWE

- The caps are read lazily from `app_config` so a `TREE_MODELS__EMBEDDING_BATCH__*` override applied after import still wins — keep that.
- The resolver's per-name `self._embedding_model.embed([name])` (`semantic.py:106`) is a single text; leave it.

## Log

### [SWE] 2026-10-09 23:47 — Implementation

**Files modified**
- `apps/memory/src/tree/memory/embedding_text.py` — `embed_in_batches` caps now `int | None = None`, resolved lazily from `app_config.models.embedding_batch` inside the function (the ONE resolving place); hard-coded 1000/320_000/32_000 defaults and `_resolve_batch_caps` removed; `embed_texts` is now a pass-through; docstrings/comments corrected.
- `apps/memory/src/tree/memory/pipeline.py`, `.../graph/resolution/semantic.py`, `.../rag/indexing.py`, `apps/memory/src/tree/config/app_config.py` — docstring/comment fixes only ("as few requests as the Voyage caps allow" / "1000 inputs / 320K"). The three call sites (`pipeline.py` `_embed_children` / `_embed_entities`, `semantic.py` `prewarm_cache`) need no code change: they already call `embed_in_batches` without caps, which now reads the config.
- `apps/memory/tests/unit/memory/test_embedding_text.py` — `TestInlineEmbedsHonourConfiguredBatchCaps` (regression, parametrised over embed-children / embed-entities / semantic-prewarm).

**Tests**
- Unit: 5298 passing, 0 failing (`make memory-tests`, env target local).
- Integration: N/A (no integration suite in this repo).

**Acceptance criteria**
- [x] Regression tests — `test_embedding_text.py::TestInlineEmbedsHonourConfiguredBatchCaps::test_ingestion_embed_splits_by_configured_max_inputs[*]` and `::test_ingestion_embed_splits_by_configured_max_total_tokens[*]` (3 paths each) plus `::test_embed_in_batches_without_caps_reads_the_config`. Confirmed red by `git stash push apps/memory/src/tree/memory/embedding_text.py` → 7 failed (`assert [5] == [2, 2, 1]`), green after `git stash pop`.
- [x] ONE place resolves caps — `embed_in_batches` (pinned by `test_embed_in_batches_without_caps_reads_the_config`).
- [x] Alignment + role — the two parametrised tests assert per-request roles (`document` ×3 for children/entities, `None` ×3 for resolution) and exact order; `::test_ingestion_vectors_stay_aligned_across_split_requests` checks the `text -> vector` map (a guard: it also passes pre-fix, as one request is trivially aligned).
- [x] Docstrings corrected (grep `src/` for "as few" / "320K" now clean apart from the hard-ceiling mentions in `app_config.py`).
- [x] Unit suite green; format + lint + pre-commit clean.

**Evidence**
```
$ make memory-format-check && make memory-lint-check
341 files already formatted
All checks passed!
$ make pre-commit
ruff check....Passed / ruff format....Passed / prettier....Passed / biome check (harness)....Passed
$ make memory-tests
======================= 5298 passed in 62.82s (0:01:02) ========================
```

**Notes**
- E2E (`run-pipelines-e2e`) NOT RUN by the SWE — left to the orchestrator; repro: with `TREE_MODELS__EMBEDDING_BATCH__MAX_INPUTS=8`, `embed-children` on ADR-010 should now log `embed_in_batches: 12 texts -> N request(s)` with N >= 2 (2 from the 8-input cap, more if the 10K-token cap also fires).
- `embed_texts` is now an identical pass-through to `embed_in_batches`; kept to avoid churn in `indexing.py` / `test_indexing.py`. Folding it away is a candidate follow-up.

### [Tester] 2026-10-09 23:56 — QA

**Test summary**
- Format / lint / pre-commit: PASS (`341 files already formatted`, `All checks passed!`, all pre-commit hooks Passed)
- Unit tests: 5298 passed / 0 failed (`make memory-tests`, `make env-status` → local)
- Integration tests: N/A (no integration suite in this repo)
- Warnings: 0
- Red without the fix: `git stash push apps/memory/src/tree/memory/embedding_text.py` → `TestInlineEmbedsHonourConfiguredBatchCaps` 7 failed / 1 passed (`assert [5] == [2, 2, 1]`, `test_embedding_text.py:632`); `git stash pop` → green; stash list empty afterwards.

**E2E adversarial pass** (isolated DB `tree_e2e_198`, user `e2e-198@example.com`, rag, Voyage voyage-4, docker `tree-prefect-worker` stopped for the run)
- Happy path: serve with `TREE_MODELS__EMBEDDING_BATCH__MAX_INPUTS=4`, `make memory-run-pipeline MODE=online SOURCE=$PWD/docs/adrs/010_document_natural_key.md …` → `embed_children: … n_texts=12`, 3 × `Concurrency limits ['voyage-embeddings'] do not exist - skipping acquisition.` on `embed-children-d4a` = 3 POSTs (= ⌈12/4⌉; the bug gave 1). 12/12 children carry a `binData` vector. `make memory-search QUERY="how is the document natural key computed?" …` → `vector leg: 12 kept (top=0.775)`, `text leg: 7 candidate(s)`, 1 parent (ADR-010) returned = found; no degraded caveat line printed (`scripts/search_memory.py:75` prints it whenever `search_mode != "hybrid"`) = hybrid. (PASS)
- Break path 1 (boundary: `TREE_MODELS__EMBEDDING_BATCH__MAX_TOTAL_TOKENS=1` in the serving shell): ingest `docs/adrs/003_source_definitions_as_operator_data.md` → `n_texts=14`, 14 POSTs (one text per request), 0 errors / 429s, 26/26 children in the DB carry a vector. (PASS)
- Break path 2 (state: same source twice): re-ingest ADR-010 → `Duplicate at submit time`, no new `embed-children` task run. (PASS)
- Break path 3 (precedence, scratch unit script over `embed_in_batches` / `embed_texts` with a recording fake, config `max_inputs=2`): explicit `max_inputs=5` → `[5]`; explicit `max_inputs=1` via `embed_texts` → `[1,1,1,1,1]`; no caller cap → `[2,2,1]`; `max_total_tokens=1` → `[1,1,1,1,1]`; empty list → no call; vectors aligned in every case. (PASS)
- Cleanup: serve killed, `tree_e2e_198` dropped (`ok: 1`), `tree-prefect-worker` restarted; `tree` still 854 memory rows with an embedding / 221 documents.

**Acceptance criteria**
- [x] PASS — Regression test fails on main, passes on fix, per path, for `max_inputs` and `max_total_tokens` — `TestInlineEmbedsHonourConfiguredBatchCaps` (`test_embedding_text.py:546`), parametrised `embed-children` / `embed-entities` / `semantic-prewarm`; 7 red with the fix stashed, green with it.
- [x] PASS — ONE place resolves the caps — `embed_in_batches` (`embedding_text.py:127-137`), `_resolve_batch_caps` deleted, `embed_texts` forwards. Grep: the only list callers are `pipeline.py:487`, `pipeline.py:1192`, `semantic.py:89`, `indexing.py:212`, all through that seam; the remaining `.embed(` sites (`preference_supersession.py:435`, `nl_query.py:448`, `rag/search.py:238`, `semantic.py:106`) pass a single-element list; no callers in `scripts/` / `deploy/`.
- [x] PASS — Vectors aligned, role unchanged — the parametrised tests assert exact per-request order and roles (`document` ×3 children/entities, `None` ×3 resolution); `test_ingestion_vectors_stay_aligned_across_split_requests`; live search finds the right parent.
- [x] PASS — Docstrings corrected — diff fixes `pipeline.py` (`_embed_children`, `_embed_entities`, task ⑥ comment), `semantic.py` `prewarm_cache`, `indexing.py` `_embed_batch`, `embedding_text.py` module + function docstrings, `app_config.py` `EmbeddingBatchConfig`; no remaining "as few … as the Voyage caps allow" in `src/`.
- [x] PASS — Unit suite green, format + lint + pre-commit clean — see Test summary.

**Other issues found** (none blocking)
- Test names: `test_ingestion_embed_splits_by_configured_max_inputs` / `..._max_total_tokens` assert the FIXED split, so the name reads as the opposite of what the test checks; siblings in the same class (`..._stay_aligned_...`, `..._reads_the_config`) and the class name (`...HonourConfiguredBatchCaps`) are named for correct behaviour. The AC's "Test names describe the symptom" invited this; recommend `test_ingestion_embed_splits_by_configured_max_inputs` / `..._max_total_tokens`.
- `max_inputs=0` (or negative) yields an empty leading chunk (`sizes=[0,1,1,1,1,1]`) from `_chunk_indices_by_caps`. Harmless today (both Voyage models return `[]` on empty input, `voyage_embedding.py:221`, `voyage_multimodal_embedding.py:178`) and pre-existing, but `EmbeddingBatchConfig` has no `ge=1` bounds, so a bad `TREE_MODELS__EMBEDDING_BATCH__*` override is accepted silently. Follow-up candidate.
- The `embed_in_batches: N texts -> M request(s)` INFO line never reaches the serve log (module logger not forwarded by Prefect), so the SWE's repro hint in the Notes doesn't work as written; the request count had to come from the GCL warning.
- E2E ran rag only: `_embed_entities` and `prewarm_cache` are covered by the unit tests, not live.
- `embed_texts` is now a pure pass-through (SWE already flagged it).

**VERDICT: PASS**
