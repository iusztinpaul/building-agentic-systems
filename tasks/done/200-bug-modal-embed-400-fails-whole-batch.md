---
id: 200-bug-modal-embed-400-fails-whole-batch
status: done
feature: bug-embedding-batching
---

# Bug: On Modal, one un-embeddable input fails its whole embed request instead of being skipped

**Severity:** S4 — low practical risk (child chunks are size-bounded and `strip_invalid_chars` runs upstream), but the Modal path lacks the skip-and-continue behaviour the Voyage path has.
**Affected component(s):** `apps/memory/src/tree/models/modal_embedding.py`, `apps/memory/src/tree/memory/embedding_text.py` (`_embed_chunk_resilient`)
**First observed:** 2026-10-09, live probe against `voyageai/voyage-4-nano` on the vLLM App (`ep-tree-voyage-4-nano`).

## Summary

`_embed_chunk_resilient` bisects a request that fails with a structured HTTP 400 and skips the offending input with an aligned `[]`. `ModalEmbeddingModel.embed` wraps every non-timeout failure as `ExtractionError(f"Embedding call failed: {exc}")` with NO `status_code`, so a vLLM 400 is re-raised and the whole request (up to `max_inputs` texts) fails. Two observed inputs:

- an input over the model's `max_model_len` (32,768 for voyage-4-nano): vLLM answers HTTP 400 (`This model's maximum context length is 32768 tokens…`); Voyage instead truncates server-side (`truncation=True`);
- a lone surrogate (`"\ud800"`): the OpenAI SDK fails client-side with `UnicodeEncodeError` before sending; Voyage answers 400 and the batcher skips it.

## Reproducer (deterministic)

Live (2026-10-09): `embed_in_batches([ok_text, "graph memory retrieval " * 12000], modal_model, input_type="document", max_inputs=3)` → `ExtractionError: Embedding call failed: Error code: 400 - {... maximum context length is 32768 tokens ...}`; and `[ok, "bad \ud800 surrogate", ok]` → `ExtractionError: Embedding call failed: 'utf-8' codec can't encode character '\ud800' …`.

Unit level: make the mocked `embeddings.create` raise `openai.BadRequestError` (status 400) for any batch containing a marker text, and `UnicodeEncodeError` for another.

Expected: the batcher bisects, returns `[]` at the bad positions and real vectors elsewhere (same as Voyage).
Actual: `ExtractionError` with `status_code=None` escapes; no vector is returned for the request.

## Suspected localisation

- `apps/memory/src/tree/models/modal_embedding.py:319-320` — `except Exception` drops the status of `openai.APIStatusError`.
- `apps/memory/src/tree/memory/embedding_text.py` `_embed_chunk_resilient` — keys the skip off `ExtractionError.status_code == 400` (correct; the client must supply it).
- `apps/memory/src/tree/config/app_config.py:84-87` / `embedding_text.py` docstrings — say an oversized input "is truncated server-side rather than 400-ing", which is Voyage-only.

## Out of scope

- Client- or server-side truncation for Modal (`truncate_prompt_tokens` etc.) — a design change; this task only makes the failure local to the bad input.
- Mapping 401/403/404/5xx differently than today: only a 400 (content rejection) becomes skippable; everything else keeps re-raising.

## Acceptance criteria

- [x] **Regression tests** in `tests/unit/models/test_modal_embedding.py` (+ a batcher-level test in `tests/unit/memory/test_embedding_text.py`): a vLLM 400 carries `status_code=400` on the `ExtractionError`; a client-side `UnicodeEncodeError` is classified as the same content rejection; through `embed_in_batches`, only the bad input gets `[]` and the others keep their vectors in order.
- [x] A 5xx / 429 / timeout from Modal still re-raises (not skipped) — existing tests stay green, add one for a 5xx if missing.
- [x] Docstrings stating server-side truncation are scoped to Voyage.
- [x] Unit suite green, format + lint + pre-commit clean. No `modal` command is run (mocked tests only).

## Log

### 2026-10-10 — SWE

**Fix.** `ModalEmbeddingModel.embed` (`apps/memory/src/tree/models/modal_embedding.py`) gets two handlers between the `APITimeoutError` one and the final `except Exception`: `UnicodeEncodeError` → `ExtractionError("Embedding call failed: …", status_code=400)` (the SDK raises it in `_build_request`, outside its own try, so nothing is sent; verified on openai 2.28.0), and `openai.APIStatusError` → `ExtractionError(…, status_code=exc.status_code)`. Reading taken: EVERY `APIStatusError` carries its real status (as `modal_llm.py` `_status_code` and the Voyage client already do), not only 400 — `_embed_chunk_resilient` skips only `== 400`, so 401/403/404/429 still re-raise; they just stop arriving as `status_code=None`. Timeouts (`None`) and the Warm gate's `ModelError`/`ExtractionError` (5xx after one re-warm → its 5xx) are unchanged. Warm gate: no change needed — `BadRequestError` is neither `InternalServerError` nor `APIConnectionError`, and a `UnicodeEncodeError` is not an SDK error, so both propagate unchanged with no re-warm and no retry (already pinned by `test_modal_warmup.py` `test_a_4xx_passes_through_unchanged[BadRequestError-400]` and `test_a_non_sdk_exception_passes_through_unchanged`; re-pinned at the client and batcher level via `poll.await_count == 1`). Docstrings: "truncated server-side rather than 400-ing" scoped to Voyage in `app_config.py` `EmbeddingBatchConfig`, `embedding_text.py` (`_CHARS_PER_TOKEN` comment, `_chunk_indices_by_caps`, `_embed_chunk_resilient`); the skip warning reads `(HTTP 400)` instead of `(Voyage 400)`, and the same wording in the `rag/indexing.py` skipped-count comment.

**Trade-off (named, not fixed).** A 400 that is NOT about one input (e.g. a misconfigured request every input would get) now bisects a Modal chunk down to singletons — ~2N calls — and skips every input with `[]`; the same is already true on Voyage. The skipped count is logged by `rag/indexing.py`; a config-level 400 would show as "N skipped".

**Tests.**
- `apps/memory/tests/unit/models/test_modal_embedding.py` `TestContentRejection` (:739): `test_a_vllm_400_carries_status_400_and_does_not_re_warm` (:744, real SDK over `httpx.MockTransport`, vLLM 400 body), `test_a_lone_surrogate_is_a_client_side_content_rejection` (:762, real SDK, 0 requests sent), `test_a_non_400_status_is_carried_unchanged[401/403/404/429]` (:787), `test_a_5xx_after_one_re_warm_is_never_a_400` (:805); plus `status_code == 400` added to `TestWarmAtUse::test_embed_wraps_non_cold_errors` (:1135). `_WireStub` gains a `rejects` predicate.
- `apps/memory/tests/unit/memory/test_embedding_text.py` `TestModalContentRejectionsAreSkipped` (:924), through `embed_in_batches` with the REAL `ModalEmbeddingModel` + real `AsyncOpenAI` over a vLLM stub: `test_only_the_bad_input_is_skipped[over-max-model-len-400 / lone-surrogate]` (:933, caps wide enough for ONE request so the 400 forces the bisect; `[vec, [], vec]` fingerprinted per input, one warm), `test_a_transient_status_still_fails_the_request[429/500/503]` (:958).

**Evidence.**
- Red without fix: `git stash push -- apps/memory/src/tree/models/modal_embedding.py` (the only behavioural file; the other src edits are docstrings/comments/log text), then `uv run pytest tests/unit/memory/test_embedding_text.py tests/unit/models/test_modal_embedding.py -q -rf --tb=no` → `10 failed, 118 passed` (both batcher skip cases re-raise `ExtractionError: Embedding call failed: Error code: 400 …` / `… surrogates not allowed`; the status asserts read `None`; the batcher 429 reads `None`); 500/503 pass before and after (guards). `git stash pop`.
- `make env-status` → `local (.env)`; format-fix/lint-fix/format-check/lint-check → `341 files already formatted`, `All checks passed!`; `make pre-commit` → all Passed/Skipped; `make memory-tests` → `5319 passed in 62.72s`.
- No e2e / no `modal` command (orchestrator hard rule: mocked tests only). Task 201 files untouched; nothing committed.

### [Tester] 2026-10-10 00:32 — QA

**Test summary**
- `make env-status` → `local (.env)`.
- Red without the fix: `git stash push -- apps/memory/src/tree/models/modal_embedding.py`, then `uv run --env-file ../../.env pytest tests/unit/memory/test_embedding_text.py tests/unit/models/test_modal_embedding.py -q` → `10 failed, 118 passed` (the same 10 the SWE listed). `git stash pop`; `git stash list` empty afterwards.
- Format / lint / pre-commit: PASS (`341 files already formatted`, `All checks passed!`, every pre-commit hook Passed/Skipped).
- Unit tests: `make memory-tests` → `5319 passed in 62.84s`, 0 failed, no warnings summary.
- Integration tests: none (deleted on purpose, per AGENTS.md).

**E2E adversarial pass** (scratch script `scratchpad/adv/adv.py`: the real `ModalEmbeddingModel` and real `AsyncOpenAI` over `httpx.MockTransport`; warm coroutines are AsyncMocks; no Modal and no DB. The baseline is `git show e17c4de:…/modal_embedding.py` loaded side by side.)
- A (boundary: 400 on a 1-input chunk): `embed_in_batches(["t#1"])` → `[[]]`, 1 request, 1 poll. Not raised. PASS.
- B (config-level 400, "model `bogus` does not exist", N=8 in one chunk): all 8 → `[]`, 15 requests (= 2N-1), 1 poll. This matches the cost the SWE documented. PASS (documented trade-off).
- B2/B3 (indexing): `indexing._embed_batch` with every input getting 400 → `embedded_count=0` and `bulk_write` is never called, so no empty vector is written and the rows stay pending for the next backfill. With one bad row among good ones, `written_ids=['u:1','u:3']`. `rag/load.py` maps `[]` to `$$REMOVE`, and `to_stored_vector([])` raises. PASS.
- C (401, new vs old): NEW → `ExtractionError status=401`, re-raised by the batcher. OLD → `ExtractionError status=None`, re-raised. Same exception type and the batcher outcome is unchanged; only the status field differs. PASS.
- D (400 on the Warm gate's single retry after a cold 503): direct embed → `ExtractionError status=400` (2 requests, 2 polls). Through the batcher: `[1.0, [], 3.0]`, then the bisect runs warm with no further polls. PASS.
- E (422): `ExtractionError status=422` re-raised, 1 request, not skipped. PASS.
- F (task 199 interaction): bisected halves answered with reversed `index` → `[1.0, [], 3.0, 4.0, 5.0]`, correct order. A SHORT response on a bisected half → `ExtractionError status=None` re-raised, not skipped. PASS.
- G (except ordering): a `ModelError` from the warm (bad token) propagates as `ModelError`. The gate's "still answered HTTP 503 after one re-warm" passes through unwrapped (`status=503`). A `ModelError` from the re-warm surfaces through the batcher as `ModelError`. `except ModelError: raise` comes before the new handlers, so nothing from the gate gets re-wrapped. PASS.
- H (config error that raises `UnicodeEncodeError`: a non-ASCII Proxy token): httpx fails to encode the header client-side. NEW → every input `[]`, 0 requests sent, logged as "skipping un-embeddable input (HTTP 400)". OLD → raised `status=None`. In practice this can't happen: a local aiohttp server showed `poll_health` sends that header and the 401 fails fast with `ModelError` during the warm, before any embeddings call. Note only.

**Acceptance criteria**
- [x] PASS — Regression tests. Evidence: `TestContentRejection` (test_modal_embedding.py:739) asserts `status_code == 400`, the `BadRequestError` cause, 1 request and 1 poll. The surrogate case asserts a `UnicodeEncodeError` cause and 0 requests. `TestModalContentRejectionsAreSkipped` (test_embedding_text.py:924) asserts `[vec, [], vec]` fingerprinted per input. All are red without the fix.
- [x] PASS — 5xx / 429 / timeout still re-raise. Evidence: `test_a_transient_status_still_fails_the_request[429/500/503]`, `test_a_5xx_after_one_re_warm_is_never_a_400`, and the existing `TestRequestTimeout` (green), plus probes C, E, G2.
- [x] PASS — Truncation docstrings scoped to Voyage. Evidence: the diff in `app_config.py` `EmbeddingBatchConfig`, `embedding_text.py` (`_CHARS_PER_TOKEN`, `_chunk_indices_by_caps`, `_embed_chunk_resilient`). `grep -rni "server-side\|truncation=True" src` finds no unscoped claim left.
- [x] PASS — Suite green, format + lint + pre-commit clean, no `modal` command run. Evidence above.

**Decision judged: every `APIStatusError` now carries its real status.** `grep -rn status_code apps/memory/src`: the only reader of `ExtractionError.status_code` is `embedding_text.py:215` (`!= 400`). No code branches on `is None`, and `modal_warmup._rewarm` only forwards the gate's own status. `modal_llm._status_code` and the Voyage clients already do the same. Behaviour is unchanged except for 400. Accepted.

**Other issues found** (non-blocking)
- Probe H above: `except UnicodeEncodeError` also catches non-input encode failures (headers) as a 400. The warm's 401 makes this unreachable today. Narrowing it would mean checking `exc.object` against the inputs, which isn't worth it now.
- Stale names: the comments at `embedding_text.py:205` and `graph/add_entity.py:247` still say "Voyage-400 bisect-and-skip"; they're now provider-generic. Cosmetic.
- B, a candidate follow-up: on Modal a 400 that has nothing to do with input content used to fail loudly. It had `status=None`, so it re-raised, the task retries ran out and the flow failed with nothing cached. Now it fails quietly: every input gets `[]`, and `_embed_children` / `_embed_entities` return a dict of empty vectors. Prefect caches that dict for 90 days (`_INPUTS_NO_HEADERS`, `cache_expiration=timedelta(days=90)`), so re-running the inline path on the same texts replays the empty vectors. No data is lost, because the rows are written without a vector and the indexing backfill refills them. Voyage already behaved this way. The SWE named the bisect cost and the "N skipped" log but not this cache replay. It doesn't block this task, since mapping non-content statuses is out of scope.
- Code-review plugin: not run from this QA pass, because a `code-review` agent runs in parallel this session.

**VERDICT: PASS**

### 2026-10-10 — SWE (post-Tester fixes)

- Made the two remaining "Voyage-400 bisect-and-skip" comments provider-neutral ("HTTP-400"): `embedding_text.py` (`_embed_chunk_resilient` body) and `graph/add_entity.py` (dedup embed routing).
- **Follow-up (named, not fixed here):** a NON-content 400 (e.g. a bad served model id or a malformed request every input gets) now bisects every input of a Modal chunk to `[]`. `_embed_children` / `_embed_entities` return that dict, and Prefect caches it for 90 days keyed on the task inputs — so a re-run of the inline path replays the empties (rows load without a vector; the indexing backfill refills them later). Voyage already behaves this way.
- **Note:** `except UnicodeEncodeError` in `ModalEmbeddingModel.embed` would also catch a non-ASCII header encode failure (e.g. a Proxy token with non-ASCII characters) and classify it as a skippable 400; in practice that fails earlier, in the Warm gate's `poll_health`, as a `ModelError`, so the embed call is never reached.
