# Embedding-path resilience: sanitize + skip-and-continue

Status: in-progress
Tags: `memory`, `infra`, `enhancement`, `P1`
Depends on: #044 (real-time request batching)

> Came in as a direct `/day` ops change (no PM groom). This tracker file was
> created by the Tester to record scope + AC + the QA verdict. The change was
> written and exercised live during a 5-doc reset+reindex on the free Voyage
> tier.

## Scope

Make the embedding path resilient to scraped-content quirks so one poison
chunk can't fail an entire indexing/dedup run, WITHOUT silently dropping data
on transient failures.

1. `apps/memory/src/tree/memory/embedding_text.py`
   - `_sanitize_for_embedding(text)` — strips C0 controls (except
     tab/newline/CR), DEL+C1 (0x7f-0x9f), and surrogates (0xd800-0xdfff).
     Applied at the tail of `node_to_embedding_text`. Voyage's
     `/v1/multimodalembeddings` 400s on these (common in HTML->markdown scraped
     content).
   - `_embed_chunk_resilient(model, chunk)` — wraps `embedding_model.embed`
     inside `embed_in_batches`. On an `ExtractionError` containing "400"
     (content rejection) it bisects to isolate and skip the offending text(s),
     emitting an aligned empty-vector `[]` placeholder. Errors WITHOUT "400"
     (429/5xx) are RE-RAISED — transient errors must never be silently skipped.
2. `apps/memory/src/tree/memory/indexing/core.py` — `_embed_batch` skips
   writing the empty `[]` placeholder (`if vector`), leaving such nodes
   unembedded so a later backfill run retries them.
3. Unit tests for sanitization + skip-and-continue (incl. 429-propagates).

## Acceptance Criteria

- [x] Sanitization strips control/surrogate chars but preserves ordinary text,
      tab, newline, CR, and legitimate Unicode (smart quotes, emoji, accents).
- [x] `node_to_embedding_text` byte-identical regressions still pass
      (sanitization is a no-op on clean text).
- [x] A 400 content rejection mid-batch is isolated by bisection and skipped
      with a positionally-aligned `[]`; good vectors stay in their slots.
- [x] Multi-poison bisection preserves EXACT positional alignment (no drift).
- [x] 429/5xx (transient) errors propagate out of `embed_in_batches` and are
      NEVER skipped. **FIXED (SWE 2026-05-20): discriminator now keys off
      structured `ExtractionError.status_code == 400`, not a message substring.
      A 429/5xx body containing "400" propagates. Verified by
      `test_429_message_containing_400_still_propagates` (xfail removed) +
      `test_status_less_400_message_still_propagates`.**
- [x] `_embed_batch` does NOT persist an `embedding: []` for skipped nodes;
      they remain missing/`[]` so the backfill retries them.
- [x] `make pre-commit` clean, `make memory-unit-tests` clean,
      `make memory-integration-tests-all` clean (acceptance gate).
      **RE-VERIFIED (Tester 2026-05-20): pre-commit clean; unit 1258 passed /
      0 xfailed; integration 221 passed, 1 skipped, 1 failed — the single
      failure is `test_web_serp.py::test_empty_query_returns_empty_list`, a
      live Bright Data SERP flake (Google near-match expansion) unrelated to
      this change and excluded from CI. Not a regression.**

## Log

### [Tester] 2026-05-20 — QA

**Test summary**
- Format / lint / pre-commit: PASS (ruff check + format + biome + KGQuery discipline all green)
- Unit tests (`make memory-unit-tests`): 1255 passed, 1 xfailed (was 1249; +6 new adversarial tests, +1 strict-xfail documenting the defect below)
- Integration tests (`make memory-integration-tests-all`): 222 passed, 1 skipped, 0 failed (548s, full docker+mongot stack)
- Warnings: 0

**Infra note (classification of the skip + stack state)**
- The session's docker stack was running under compose project `building-agentic-systems` (mongodb/mongot/prefect-server up & healthy); `make local-start` from this worktree collided on the fixed container names. I did NOT tear it down — ran the acceptance gate against the already-healthy session stack (MongoDB ping ok on :27017, mongot on :27028).
- The 1 integration skip is `test_web_search_ingest.py:65` — "Deployment 'ingest-web-url-batch-etl' not registered (serve workflows first?)". Pre-existing environmental skip (Prefect deployment not served), unrelated to embedding resilience. NOT a regression. No Bright Data SERP live failures observed.

**E2E adversarial pass**
- Live DB (production evidence from the session's 5-doc run): `knowledge_graph` has 356 nodes; exactly 3 unembedded. The 3 skipped nodes are all `type=chunk` from ONE article (`maximelabonne.substack.com/.../direct-preference-optimization`, chunks #2/#3/#4), each with `embedding=[]` (NOT a written placeholder — left retriable). Embedded nodes carry 1024-d voyage-multimodal-3 vectors. Matches the spec's "353/356, 3 chunks from one article" exactly and confirms point D in production.
- Break path A (boundary: multi-poison bisection alignment): added `test_multi_poison_preserves_exact_alignment` with an identity-encoding model (vector = `[ord(text[0])]`, so each good slot is fingerprinted, unlike the original `[1.0,1.0]`-uniform model that could not detect a swap). Poison at idx 1 & 4 of 6 → `[[a],[],[c],[d],[],[f]]`. PASS — exact alignment held. Also probed n=8/16/64/256 single-poison live: alignment correct in every case.
- Break path B (failure mode: transient 429/5xx must not be skipped): added `test_429_message_containing_400_still_propagates` with a 429-exhaustion message whose body contains the substring "400" → expected propagate, ACTUAL **skipped** (two "skipping un-embeddable input" WARNINGs, no raise). **FAIL — defect, see below.** The existing `test_rate_limit_propagates_not_skipped` (message has no "400") correctly passes — but that only proves the happy 429 case.
- Break path C (boundary: sanitization correctness): added `test_preserves_legitimate_unicode_tab_and_newline` (café / smart quotes / emoji / accents / tab / newline / CR all survive), `test_strips_each_invalid_class` (NUL/BEL/VT/FF/DEL/C1/unpaired-surrogate all stripped), `test_no_op_on_plain_ascii`. All PASS. Byte-identical `node_to_embedding_text` regressions still green.
- Break path D (state edge: all-poison chunk): added `test_all_poison_chunk_yields_all_placeholders` → `[[],[],[]]`. PASS — full bisection to singletons, every slot a placeholder.
- Break path E (cost note, not a bug): single-poison chunk of size n costs O(log n) extra `.embed()` calls (measured 7/9/13/17 for n=8/16/64/256) and re-sends ~3n total inputs. Logarithmic in request count — acceptable. The O(n)-extra-calls pathology only materializes if a LARGE FRACTION of inputs are poison (each forces its own bisection branch); a lone poison among many is cheap. Worth a note, not a blocker.

**Acceptance criteria**
- [x] PASS — Sanitization strips invalid classes, preserves rich Unicode/tab/newline. Evidence: `test_embedding_text.py::TestSanitizeForEmbedding` (3 tests) + `test_strips_control_chars_and_surrogates`.
- [x] PASS — Byte-identical regressions intact. Evidence: `TestNodeToEmbeddingText` (6 tests) green; sanitize is a no-op on clean text (`test_no_op_on_plain_ascii`).
- [x] PASS — Single-poison isolation keeps order. Evidence: `test_skips_poison_input_keeps_order` → `[[1,1],[],[1,1]]`.
- [x] PASS — Multi-poison exact alignment. Evidence: `test_multi_poison_preserves_exact_alignment` (idx 1 & 4 of 6).
- [ ] FAIL — 429/5xx must propagate, never be skipped.
      Expected: an `ExtractionError` with HTTP 429/5xx semantics raises out of `embed_in_batches`.
      Actual: when the 429/5xx error MESSAGE contains the substring "400", `_embed_chunk_resilient` mis-classifies it as a content rejection, bisects down to singletons, and SILENTLY SKIPS every input (returns `[]` placeholders) — the exact data-drop the change forbids. Reproduced by `test_429_message_containing_400_still_propagates` (currently `@pytest.mark.xfail(strict=True)` so the suite stays green and the defect is documented; remove the xfail when fixed).
      Reachability (not theoretical): `VoyageMultimodalEmbeddingModel.embed` builds the 429-exhaustion message as `f"...429: rate-limit retries exhausted ({detail})"` and the 5xx message as `f"...error {status}: {detail}"`, where `detail = body.get("detail", body)` interpolates the server response body VERBATIM. Any 429/5xx body containing the digit-run "400" — token/quota counts, a Retry-After of 400, a request ID/timestamp/hash, or the raw fallback dict — flips the discriminator. (`apps/memory/src/tree/models/voyage_multimodal_embedding.py:184-201`)
      Fix: key the skip decision off the structured HTTP status (e.g. raise a typed `VoyageContentRejection` / carry a `status_code` attribute on the exception and branch on `exc.status_code == 400`), not a substring of the human-readable message. Then delete the strict-xfail so the test becomes a green regression guard. File:line: `apps/memory/src/tree/memory/embedding_text.py:153`.
- [x] PASS — Skip placeholder not persisted. Evidence: unit `test_core.py::test_skipped_placeholder_vector_is_not_persisted` (asserts only the good doc reaches `bulk_write`, count=1) + live DB (3 skipped nodes carry `embedding=[]`, retriable; query filter `embedding in [[],null]` re-selects them).
- [x] PASS — All gates clean (counts above).

**Evidence**
```
$ make memory-unit-tests
======================= 1255 passed, 1 xfailed in 44.08s =======================

$ make memory-integration-tests-all
================== 222 passed, 1 skipped in 548.64s (0:09:08) ==================

$ uv run pytest tests/unit/memory/test_embedding_text.py -v
...TestEmbedInBatchesAlignmentAdversarial::test_429_message_containing_400_still_propagates XFAIL
======================== 25 passed, 1 xfailed in 0.20s =========================

$ mongosh ... --eval 'unembedded count'
total nodes: 356
unembedded (embedding in [[],null]): 3
skipped chunks: maximelabonne.substack.com/.../direct-preference-optimization #chunk-2/3/4 (embedding=[])
```

**Other issues found (not blocking; for SWE/PR-Reviewer awareness)**
- `_embed_batch` now returns `len(ops)` (persisted count), so `embed_nodes`'s "Embedded N nodes" log under-reports when chunks are skipped (353 logged for 356 fetched). Correct as a persisted-count, but a one-line log noting "(K skipped, will retry)" would aid ops visibility. Nit.
- Cost note E above (logarithmic re-embed on bisection) — acceptable, documented for the record.

**VERDICT: FAIL**

One Blocker: the 429/5xx-vs-400 discriminator is a substring match that can silently drop data on transient errors (AC #5). This is the single data-loss failure mode the change explicitly set out to prevent, and it is reachable in production via the verbatim-interpolated Voyage error body. Everything else (sanitization, alignment through bisection incl. multi-poison, skip-not-persisted, full suite, live e2e) PASSES. SWE: replace the substring discriminator with a structured HTTP-status check and remove the strict-xfail on `test_429_message_containing_400_still_propagates`.

### [SWE] 2026-05-20 14:50 — Blocker fix (structured status discriminator)

**Discriminator approach chosen**
Structured `status_code` on `ExtractionError` (option 1 — smaller blast radius than a
new `VoyageContentRejection` type, since `ExtractionError` is already the shared
embedding/extraction error and no new symbol needs importing across files). Added an
optional `status_code: int | None = None` to `ExtractionError.__init__`; the Voyage
multimodal client passes `status_code=resp.status` on BOTH non-200 raise paths;
`_embed_chunk_resilient` now decides skip via `getattr(exc, "status_code", None) != 400`
(re-raise unless a structured HTTP 400). No more substring match on the message.

**Files modified**
- `apps/memory/src/tree/models/exceptions.py` — `ExtractionError` carries optional `status_code`; default `None` keeps every existing call site working (subclass `PipelineValidationError` and all other `ExtractionError(...)` raises unaffected).
- `apps/memory/src/tree/models/voyage_multimodal_embedding.py` — both non-200 raises (429-exhausted AND generic non-200) now pass `status_code=resp.status`.
- `apps/memory/src/tree/memory/embedding_text.py` — `_embed_chunk_resilient` branches on `exc.status_code == 400`, not `"400" in str(exc)`; docstring updated to explain why message-substring discrimination is unsafe (verbatim server body).
- `apps/memory/src/tree/memory/indexing/core.py` — Nit: `embed_nodes` summary log now appends `" (K skipped, will retry)"` when fetched > persisted, so the "Embedded N nodes" count is not misread as "all done" when poison chunks were skipped. Single log line.
- `apps/memory/tests/unit/memory/test_embedding_text.py` — poison models now raise with `status_code=400`; rate-limited model with `status_code=429`. **Removed `@pytest.mark.xfail(strict=True)`** on `test_429_message_containing_400_still_propagates`; it now asserts propagation (the 429 model carries `status_code=429`). Added `test_status_less_400_message_still_propagates` (a status-less `ExtractionError` whose message contains "400" must re-raise, never skip).
- `apps/memory/tests/unit/models/test_voyage_multimodal_embedding.py` — added `test_embed_400_carries_status_code`; asserted `status_code` on the 429-exhausted (==429) and 5xx (==500) raises.
- `apps/memory/tests/unit/memory/indexing/test_core.py` — extended `test_skipped_placeholder_vector_is_not_persisted` with a `caplog` assertion that the summary log contains `"(1 skipped, will retry)"`.

**Tests**
- Unit: 1258 passing, 0 failing, 0 xfailed (was 1255 passed + 1 xfailed) — `make memory-unit-tests`. 0 warnings.
- Integration: NOT RUN — Tester owns `make memory-integration-tests-all` on re-verify (per the hand-off instruction).

**xfail removed + now-green confirmation**
`test_429_message_containing_400_still_propagates` no longer carries `@pytest.mark.xfail`; it
now passes asserting that a 429 whose message contains the digit-run "400" propagates
(`pytest.raises(ExtractionError, match="429")`). Confirmed green in both the focused run and
the full suite.

**Grep evidence — no sibling relied on the old message-substring behavior**
`grep -rn '"400"|status_code|in str(exc)|rate-limit retries exhausted|Voyage multimodal API error' apps/memory/src apps/memory/tests`
- The ONLY control-flow consumer of the `"400"` substring was `embedding_text.py:153` (the line fixed).
- `data/pipeline.py:103` matches `"vector_index not found"` — unrelated.
- Voyage error-message anchors (`rate-limit retries exhausted`, `Voyage multimodal API error`) are matched only by *test assertions* (`pytest.raises(..., match=...)`) and the module docstring's grep anchor — no production code branches on them. Those test matches still hold (the messages are unchanged).
- `grep -rn ExtractionError` across src/tests: every other raise/catch uses the single-positional-arg form; the new optional kwarg is backward-compatible.

**Evidence**
```
$ make memory-format-fix && make memory-lint-fix && make memory-format-check && make memory-lint-check
All checks passed!   (244 files, ruff format + check clean)

$ make pre-commit
prettier...Passed  ruff check...Passed  ruff format...Passed  biome check (harness)...Passed  KGQuery discipline (memory)...Passed

$ make memory-unit-tests
============================ 1258 passed in 40.59s =============================

$ uv run python -c "...exercise discriminator e2e..."
skipping un-embeddable input (Voyage 400): 'bad'
400-skip -> [[1.0, 1.0], [], [1.0, 1.0]]
429-propagates -> 429 | error 429: exhausted after 400 retries
E2E OK
```

**Caveat for the Tester's re-verify**
- I did NOT run `make memory-integration-tests-all` (per hand-off — Tester owns it on re-verify). No infra/schema change in this fix, so no integration regression expected; the change is confined to in-process error classification + a log-string tweak.
- The fix is data-format-neutral: it does NOT alter what gets persisted (a real 400 still produces an aligned `[]` placeholder, still skipped from `bulk_write`). The live-DB evidence from the original run (3 chunks `embedding=[]`, retriable) remains valid — re-running indexing/backfill should behave identically for genuine 400s.
- The new ops log note is informational only; on a clean run with zero skips the note is absent (empty suffix), so existing "Embedded N nodes in knowledge_graph" log greps still match.
- Other models that raise `ExtractionError` (Gemini, Modal, sentence-transformer) do NOT pass `status_code`, so it defaults to `None` and they re-raise out of any resilient path — correct (only Voyage HTTP 400 is a known-skippable content rejection).

### [Tester] 2026-05-20 16:20 — QA re-verify (Blocker fix)

**Test summary**
- Format / lint / pre-commit (`make pre-commit`): PASS (prettier, ruff check, ruff format, biome harness, KGQuery discipline all Passed)
- Unit tests (`make memory-unit-tests`): **1258 passed, 0 xfailed** (was 1255 passed + 1 xfailed last round; the strict-xfail I planted is now a real green pass). 0 warnings.
- Integration tests (`make memory-integration-tests-all`): 221 passed, 1 skipped, **1 failed** (525s, full session docker+mongot stack). The 1 failure is classified NON-REGRESSION below.

**Infra note (stack state + failure classification)**
- Ran against the already-up session stack (compose project `building-agentic-systems`): `tree-mongodb` healthy on :27017, `tree-mongot` up on :27028, `tree-prefect-server` healthy on :4200 (all up ~28h). Did NOT tear it down, per instruction.
- **The 1 integration FAILURE is NOT a regression.** `tests/integration/data/web/test_web_serp.py::TestLiveSerpSearch::test_empty_query_returns_empty_list` asserts a nonsense query returns `[]` against the **live** Bright Data SERP API → live Google HTML. Google returned a near-match YouTube result ("ASDFGHJKL QWERTYUIOP | UNO w/ Friends") with its own "Missing: qzxcvbnm…" annotation — i.e. fuzzy expansion the test's quoting trick failed to suppress this run. Evidence it is orthogonal to #047: (a) `git diff --stat` touches NO `data/web` files; (b) the test file has 0 references to `embed`/`ExtractionError`/`status_code`/`sanitize`; (c) the module is `skipif`-gated on live BrightData secrets and CI runs `-m "not requires_mongot"` so it never gates the pipeline — it only ran here because this worktree's `.env` has live SERP creds. A flaky assert-on-emptiness against a live search engine. Last round's 1 skip (`test_web_search_ingest.py`) did not recur in this run's tail; every embedding-relevant integration test (extraction, indexing, review, two-user isolation, torch, migration) passed.

**E2E adversarial pass — discriminator (the Blocker)**
- Independent probe (models I wrote, NOT the SWE's tests), via `uv run python`:
  - Genuine HTTP-400 whose body ALSO contains literal "400" (`status_code=400`): `embed_in_batches(["a","poison","c","d"])` → `[[97.0],[],[99.0],[100.0]]` — bisects + skips poison, aligned, order preserved. **PASS** (Point B).
  - 5xx whose body contains "400" (`"…503: …retry-after 400ms, req-400abc"`, `status_code=503`): → **PROPAGATES** (raised, no silent drop). **PASS** (Point A — the exact original Blocker, confirmed for 5xx).
  - status-less `ExtractionError` with "400" in body (`status_code` defaulting to `None`): → **PROPAGATES**. **PASS** (Point C/D — wrapped/non-Voyage failures never skipped).
- SWE's regression guards (re-ran verbosely): `test_429_message_containing_400_still_propagates` PASSED (xfail removed → real pass), `test_status_less_400_message_still_propagates` PASSED, `test_embed_400_carries_status_code` PASSED, `test_multi_poison_preserves_exact_alignment` PASSED, `test_all_poison_chunk_yields_all_placeholders` PASSED.

**Focused re-verification A–E**
- **A (Blocker closed)** PASS — Discriminator keys on structured `ExtractionError.status_code`, not the message. `embedding_text.py:163` `if getattr(exc, "status_code", None) != 400: raise`. Voyage sets `status_code=resp.status` on BOTH non-200 raises: 429-exhausted (`voyage_multimodal_embedding.py:187`) AND generic non-200 (`:202`). 429-with-"400"-body and 5xx-with-"400"-body both propagate (my probe + the now-green xfail). status_code asserted ==429 / ==500 by `test_voyage_multimodal_embedding.py` unit tests.
- **B (genuine 400 still skips, aligned)** PASS — my probe (genuine 400, "400" in body) bisects to aligned `[]`, order preserved; `test_skips_poison_input_keeps_order` + `test_multi_poison_preserves_exact_alignment` green.
- **C (non-Voyage models safe)** PASS — Gemini/Modal/sentence-transformer all raise `ExtractionError(<msg>)` single-positional (verified by grep: `gemini.py:39/43/48/87`, `modal_embedding.py:162/168/195`, `sentence_transformer.py:53`) → `status_code=None` → `None != 400` → re-raise. Probe C confirms a status-less error propagates.
- **D (no signature regression)** PASS — `status_code` is keyword-only with default `None`; every other `ExtractionError(...)` raiser uses the single-positional form, so all are backward-compatible. Full unit suite (1258) green, not just embedding tests; integration green except the unrelated live-SERP flake. Voyage `data is None` path (`:207`) deliberately omits status_code → `None` → re-raise (correct: a 200 with a malformed body is not a skippable 400).
- **E (skip-not-persisted + log note)** PASS — `_embed_batch` (`core.py:124`) keeps `if vector` so `[]` placeholders never reach `bulk_write`; `test_skipped_placeholder_vector_is_not_persisted` asserts only the good doc is written (count=1) AND the summary log contains `"(1 skipped, will retry)"`. New `embed_nodes` note (`core.py:91-97`) computes `skipped = len(docs) - embedded_count` and only appends the suffix when `skipped` is truthy (absent on a clean run). Live DB confirms the contract: 356 nodes, exactly 3 unembedded `kind=node` (all `type=chunk`, one article, `embedding=[]` retriable); embedded nodes carry 1024-d vectors. (The 182 unembedded `kind=edge` rows are by-design — edges are never embedded — and unrelated.)

**Acceptance criteria**
- [x] PASS — Sanitization strips invalid classes, preserves rich Unicode/tab/newline. (`TestSanitizeForEmbedding` + `test_strips_control_chars_and_surrogates`)
- [x] PASS — Byte-identical regressions intact. (`TestNodeToEmbeddingText` green)
- [x] PASS — Single-poison isolation keeps order. (`test_skips_poison_input_keeps_order`)
- [x] PASS — Multi-poison exact alignment. (`test_multi_poison_preserves_exact_alignment` + my probe A with ord-fingerprinted slots)
- [x] PASS — **429/5xx must propagate, never be skipped (was the Blocker).** Discriminator now keys on structured `status_code == 400`; 429/5xx/status-less bodies containing "400" all propagate. Evidence: now-green `test_429_message_containing_400_still_propagates` (xfail removed) + `test_status_less_400_message_still_propagates` + my independent 5xx/status-less probe.
- [x] PASS — Skip placeholder not persisted. (`test_skipped_placeholder_vector_is_not_persisted` + live DB: 3 chunk nodes `embedding=[]`)
- [x] PASS — All gates: pre-commit clean, unit 1258/0-xfailed, integration green except one unrelated live-SERP flake (classified non-regression).

**Evidence**
```
$ make pre-commit
prettier...Passed  ruff check...Passed  ruff format...Passed  biome (harness)...Passed  KGQuery discipline...Passed

$ make memory-unit-tests
============================ 1258 passed in 42.20s =============================

$ uv run pytest .../test_embedding_text.py::TestEmbedInBatchesAlignmentAdversarial \
                .../TestEmbedInBatchesSkipsContentRejections .../test_voyage_multimodal_embedding.py -v
============================== 22 passed in 0.25s ==============================

$ uv run python  # independent adversarial discriminator probe
A genuine-400 (body has '400') -> SKIP + aligned: [[97.0], [], [99.0], [100.0]] OK
B 5xx-with-400-body -> PROPAGATES: Voyage multimodal API error 503: ... OK
C status-None-with-400-body -> PROPAGATES: Voyage multimodal embedding call failed: ... OK
INDEPENDENT E2E DISCRIMINATOR PROBE: ALL OK

$ make memory-integration-tests-all
============= 1 failed, 221 passed, 1 skipped in 525.43s (0:08:45) =============
# FAILED: test_web_serp.py::test_empty_query_returns_empty_list — live Bright Data SERP flake, NOT a regression (not in diff, not embedding-related, skipif-gated, excluded from CI)

$ mongosh ... # live DB
total: 538 | kind=node: 356 | kind=edge: 182
unembedded kind=node: 3  (all type=chunk, one article, embedding=[])  | embedded node vector len: 1024
```

**Other issues found**
- None new. The prior nit (under-reported "Embedded N" count) is now fixed — `embed_nodes` appends "(K skipped, will retry)". Verified by a `caplog` assertion in the unit test and by reading `core.py:91-97`.
- The live-SERP flake (`test_empty_query_returns_empty_list`) is a pre-existing assert-on-emptiness-against-a-live-search-engine fragility; worth a follow-up task to make it shape-based (the sibling tests already assert on shape, not exact content), but it is out of scope for #047 and does not gate this fix.

**VERDICT: PASS**

The Blocker is closed: the 429/5xx-vs-400 discriminator now keys off the structured `ExtractionError.status_code` set on BOTH Voyage non-200 raise paths, not a message substring. A 429/5xx/status-less error whose body contains "400" propagates (verified by the now-green xfail, the new status-less test, AND my own independent 5xx + status-less probe); a genuine HTTP-400 still bisects + skips with aligned `[]` placeholders (order preserved); non-Voyage models default to `status_code=None` and re-raise; the optional kwarg is backward-compatible across all existing `ExtractionError` call sites; skipped placeholders are excluded from `bulk_write` and surfaced in the summary log. Full unit suite green (1258/0-xfailed), full integration green except one unrelated live-SERP flake. Ready for the human to commit (lean /day — handing back to the orchestrator for commit + PR).
