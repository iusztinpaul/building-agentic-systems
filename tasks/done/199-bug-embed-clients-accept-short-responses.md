---
id: 199-bug-embed-clients-accept-short-responses
status: done
feature: bug-embedding-batching
---

# Bug: An embedding response with fewer (or reordered) vectors than inputs is accepted, so rows can silently lose or swap vectors

**Severity:** S3 — no live occurrence (order and count held on Voyage and Modal in every 2026-10-09 run), but the failure mode is silent data corruption, not an error.
**Affected component(s):** `apps/memory/src/tree/models/voyage_embedding.py`, `apps/memory/src/tree/models/voyage_multimodal_embedding.py`, `apps/memory/src/tree/models/modal_embedding.py`, `apps/memory/src/tree/memory/pipeline.py`, `apps/memory/src/tree/memory/rag/indexing.py`
**First observed:** 2026-10-09, code read during the embedding-batching e2e.

## Summary

The embedding clients return `[item["embedding"] for item in data]` without checking that the response carries exactly one vector per input, or putting them in the response's `index` order. Downstream, the ingestion path builds `dict(zip(texts, vectors))` and the backfill zips `docs` with `vectors` — both non-strict — so a short response drops the tail rows' vectors silently and a reordered one writes vectors onto the wrong rows. Only the resolver's `zip(..., strict=True)` would raise.

## Reproducer (deterministic)

Unit level: feed each client a mocked HTTP response for 3 inputs that carries (a) 2 items, (b) 3 items whose `index` fields are `[2, 0, 1]`.

Expected: (a) `ExtractionError` naming both counts; (b) vectors returned in input order (by `index`).
Actual: (a) a 2-vector list is returned; (b) the vectors come back in response order.

## Suspected localisation

- `apps/memory/src/tree/models/voyage_embedding.py:283-291` — `data` returned as-is.
- `apps/memory/src/tree/models/voyage_multimodal_embedding.py:244-251` — same.
- `apps/memory/src/tree/models/modal_embedding.py:323` — `[item.embedding for item in response.data]`; `_checked_vectors` checks width only.
- `apps/memory/src/tree/memory/pipeline.py:495`, `:1199` and `apps/memory/src/tree/memory/rag/indexing.py:224` — non-strict `zip`.

## Out of scope

- Gemini and sentence-transformers embedding providers.
- Retrying a short response (it is a provider bug, not a transient one: raise).

## Acceptance criteria

- [x] **Regression tests** in `tests/unit/models/test_voyage_embedding.py`, `test_voyage_multimodal_embedding.py` and `test_modal_embedding.py`: a count mismatch raises `ExtractionError` (no `status_code` 400, so the batcher never bisects it into skipped rows); an out-of-order response is returned in input order.
- [x] The three downstream `zip`s are `strict=True` (the batcher already returns one entry per text, `[]` for a skipped one), with a test that a length mismatch raises instead of dropping rows.
- [x] Unit suite green, format + lint + pre-commit clean.

## Notes for the SWE

- Voyage and OpenAI-compatible responses both carry `index` per item; order by it, then check the count.
- Do NOT turn the mismatch into a skipped `[]` placeholder — that would hide a provider bug as missing vectors.

## Log

### 2026-10-10 — SWE

**Fix.** One shared guard, `vectors_in_input_order(indexed, n_inputs, *, provider)` in `apps/memory/src/tree/models/base.py`: sorts `(index, vector)` pairs by `index` and requires the indices to be exactly `0..n_inputs-1` (covers short, long, duplicate and out-of-range responses in one comparison), else `ExtractionError("<provider> returned N vectors for M inputs …")` with NO `status_code`, so `_embed_chunk_resilient` re-raises instead of bisecting into `[]` rows. Called from the response path of `voyage_embedding.py`, `voyage_multimodal_embedding.py` (`item.get("index", position)`) and `modal_embedding.py` (`getattr(item, "index", position)`, before `_checked_vectors`; the `except` ladder is untouched — task 200's territory). A missing `index` falls back to the item's position (both providers always send it; the fallback keeps the existing index-less test doubles valid). The three downstream zips (`pipeline.py` `_embed_children` / `_embed_entities`, `rag/indexing.py` `_embed_batch`) are `strict=True`.

**Tests.** Count-mismatch-raises (asserts `status_code is None`) + reordered-response-in-input-order for each client: `test_voyage_embedding.py:435,459` (`TestResponseShape`), `test_voyage_multimodal_embedding.py:213,237`, `test_modal_embedding.py:606,617`. Strict zips: `test_pipeline.py:524` (parametrized children/entities), `rag/test_indexing.py:1366` (raises, no `bulk_write`). Fixture updates: the Modal `_response` double now carries `index` per item; the three Modal `MagicMock` items in `test_observability_instrumentation.py` get `index=0` (an auto-attribute `MagicMock` index is not an int).

**Evidence.**
- Red without fix: `git stash push -- <the 6 src files>` then the 6 test files via `PYTEST_ADDOPTS=… make memory-tests` → `9 failed, 378 passed` (all `DID NOT RAISE` / order assertion); `git stash pop`.
- `make memory-tests` → `5307 passed`.
- format-fix/lint-fix/format-check/lint-check → `341 files already formatted`, `All checks passed!`; `make pre-commit` → all Passed.
- No e2e run: guard-only change (live 2026-10-09 runs always returned full, ordered responses); `run-pipelines-e2e` is left for the feature-level gate.

### [Tester] 2026-10-10 00:20 — QA

**Test summary**
- Env: `make env-status` → `local (.env)`.
- Red without fix: `git stash push -- <the 6 src files>` + the 6 touched test files via `PYTEST_ADDOPTS` → `9 failed, 378 passed` (exactly the 9 new regression tests); `git stash pop` → `git stash list` empty, tree back to 12 modified + 3 untracked.
- Format / lint / pre-commit: PASS (`341 files already formatted`, `All checks passed!`, every pre-commit hook Passed/Skipped).
- Unit tests: `make memory-tests` → `5307 passed in 60.80s`, 0 failed, 0 warnings. (No integration suite, per AGENTS.md.)

**E2E adversarial pass**
Unit-level probes (scratch `scratchpad/test_qa199_adversarial.py`, not committed; 32/33 pass, the 1 "fail" is the probe in break path 5):
- Break path 1 (malformed: duplicate index `[0,0,2]`, out of range `[0,1,3]`, negative `[-1,0,1]`, empty `[]`) on the guard + all 3 clients → `ExtractionError`, `status_code=None` (PASS).
- Break path 2 (boundary: LONGER response, 4 vectors for 3 inputs) on the 2 Voyage clients + Modal → `ExtractionError "... returned 4 vectors for 3 inputs ..."`, `status_code=None` (PASS).
- Break path 3 (malformed: mixed present/missing `index`) Voyage `[1, missing→1, 2]` → raises (PASS); `[0, missing→1, 2]` → accepted in order (consistent, PASS); all-missing → positional (PASS); string indices `"0","1","2"` → raises (PASS); `index: null` → Voyage wraps as `ExtractionError "... call failed: '<' not supported ..."` (PASS).
- Break path 4 (failure mode through `embed_in_batches`, real `VoyageTextEmbeddingModel` over a scripted aiohttp): short 200 response → raises, ONE request sent, no bisect (PASS); short in the 2nd chunk → raises after 2 requests (PASS); real Voyage 400 on `"POISON"` → still bisected, output `[[1.0], [], [3.0], [4.0]]` (PASS); 400 then a SHORT answer inside a bisected half → raises, not swallowed (PASS); reversed order in every chunk across 3 chunks → realigned (PASS).
- Break path 5 (Modal re-warm, `self._gate.call`): cold 503 → re-warm → retry answers SHORT → `ExtractionError "1 vectors for 2 inputs"`, exactly 2 calls, `status_code=None` (PASS — the guard runs AFTER the gate, so it never triggers a re-warm or a 2nd retry); cold → reordered retry → input order (PASS); real `AsyncOpenAI` over `httpx.MockTransport`, short body → raises after 1 request (PASS). Modal `index=None` mixed with ints → bare `TypeError` escapes `embed()` (not an `ExtractionError`; see note 2).
Live e2e (Voyage only, DB `tree_e2e_199`, user `e2e-199@example.com`, `TREE_MODELS__EMBEDDING_BATCH__MAX_INPUTS=4`, docker worker stopped):
- Happy path: `TREE_MEMORY__MODE=rag make memory-run-pipeline MODE=online SOURCE="$PWD/docs/adrs/010_document_natural_key.md" …` → `Flow completed successfully`; 1 document + 1 parent + 12 children, 12/12 children carry a 1024-d float32 vector (binData 4098 B), 12 distinct (PASS).
- `make memory-reset-embeddings CONFIRM=yes …` → `rows=12 (children=12)`, 0 vectors left; `make memory-run-indexing-pipeline …` → `indexing: … embedded=12`, 12/12 1024-d, 12 distinct (PASS).
- `make memory-search QUERY="how is the document natural key computed?" …` → vector leg 12 candidates (top 0.775), returns `010_document_natural_key.md — ADR-010 …`, 12 matched children (PASS).
- Cleanup: orchestrator killed (pgrep empty), `tree_e2e_199` dropped (`dropped: 'tree_e2e_199'`), `tree-prefect-worker` restarted; database `tree` unchanged at 854 memory rows with an embedding / 221 documents (same as the pre-run baseline).

**Acceptance criteria**
- [x] PASS — Regression tests in the 3 client test files: count mismatch → `ExtractionError` with `status_code is None`; out-of-order → input order — `test_voyage_embedding.py:435,459` (`TestResponseShape`), `test_voyage_multimodal_embedding.py:213,237`, `test_modal_embedding.py:606,617`; all 6 red without the fix, green with it; impl `models/base.py:57` `vectors_in_input_order`.
- [x] PASS — The three downstream zips are `strict=True` (`pipeline.py:496`, `:1199`, `rag/indexing.py:224`), with tests `test_pipeline.py:524` (children + entities) and `rag/test_indexing.py:1366` (raises, `bulk_write` not awaited); all 3 red without the fix.
- [x] PASS — Unit suite green (`5307 passed`), format + lint + pre-commit clean.

**Other issues found (non-blocking)**
1. Error wording misleads when the count matches but the indices are wrong: `[0,0,2]` gives `"... returned 3 vectors for 3 inputs (expected one per input index 0..2) — no vector was returned."`. "3 vectors for 3 inputs" reads as a success, and "— no vector was returned" sounds like the PROVIDER returned nothing (it means this call returns none). Suggest naming the indices received, e.g. `"returned indices [0, 0, 2] for 3 inputs (expected 0..2); this batch's vectors were discarded"`.
2. Modal: a non-int `index` (e.g. `None` mixed with ints) escapes `embed()` as a bare `TypeError` from `sorted`, because the guard sits outside the client's `except` ladder (Voyage wraps the same case as `ExtractionError`). Not silent corruption (it still raises and `_embed_chunk_resilient` does not bisect it), and the OpenAI SDK types `index` as `int`, so it is unlikely on the wire.
3. Nit: the Voyage-text regression tests live in `class TestInputType`, which is about the Embedding role, not response shape.
4. The live run cannot show the per-request split: `embed_in_batches` module-logger lines reach neither `serve.log` nor `prefect flow-run logs`. Task 198 already verified the split; this run only checks for no regression.
5. `/code-review low` (plugin enabled in `.claude/settings.json`) on the working-tree diff → no correctness findings.
6. `TREE_MODELS__EMBEDDING_BATCH__MAX_INPUTS=4` did reach the served flow (`.env` has no `EMBEDDING_BATCH` key, so the Makefile `include`/`export` cannot override the shell value). The multi-chunk realignment is also covered by the scratch probe `test_batcher_reordered_across_chunks_aligned`.

**VERDICT: PASS**

### 2026-10-10 — SWE (post-Tester polish)

- Error now names the indices, not a count (a count match with wrong indices read as a contradiction): `"<provider> returned indices [0, 1] for 3 inputs (expected 0..2); this batch's vectors were discarded."` — the list is truncated to the first 10 + `…`. The three count-mismatch tests match `indices \[0, 1\] for 3 inputs`.
- The two Voyage-text tests moved out of `TestInputType` into `TestResponseShape` (`test_voyage_embedding.py:435,459`), mirroring Modal's `TestResponseLength`.
- **Known limitation:** a Modal response item whose `index` is not an `int` makes the sort or comparison raise a bare `TypeError`/mismatch rather than a named error — accepted, since the OpenAI SDK types `Embedding.index` as `int`.
- Gates: format/lint clean (`341 files already formatted`, `All checks passed!`), `make memory-tests` → `5307 passed`.
