# Real-time request batching for embeddings (resolution + dedup + indexing)

Status: pending
Tags: `data`, `infra`, `enhancement`, `P1`
Depends on: #042, #043
Blocks: #045

## Scope

Speed up embedding (and cut the 429s the operator hit) by packing many
texts into FEWER synchronous embed requests across all three stages:
RESOLUTION, DEDUP, and INDEXING.

### CRITICAL — this is REAL-TIME REQUEST batching, NOT Voyage's async Batch API

The operator linked https://docs.voyageai.com/docs/batch-inference
(Voyage's async Batch API). **That is the wrong tool here** and must NOT
be used. Two hard, verified reasons (record them in the task and the
"Rejected alternatives" note):

- **(a) 12-hour completion window.** The async Batch API is submit-a-JSONL
  → poll → retrieve-later, with up to a 12h turnaround. Our
  extraction/dedup pipeline needs embeddings synchronously mid-flow to
  make dedup decisions, so a 12h-latency job cannot drive it.
- **(b) Endpoint incompatibility.** The async Batch API only supports
  `/v1/embeddings`, `/v1/contextualizedembeddings`, and `/v1/rerank` —
  NOT `/v1/multimodalembeddings`. Our pinned model `voyage-multimodal-3`
  lives on `/v1/multimodalembeddings`, so the async Batch API cannot
  embed with our model at all.

Therefore "batch the embedding operation to speed up" = pack many texts
into a single SYNCHRONOUS `/v1/multimodalembeddings` request, bounded by
the per-request limits below, preserving the existing 429 backoff.

### Authoritative Voyage limits for `voyage-multimodal-3` on `/v1/multimodalembeddings`

(Ground the batcher on these; do NOT let the implementation re-derive
them.)

- **Max 1,000 inputs per request.**
- **Each input ≤ 32,000 tokens; total across all inputs ≤ 320,000 tokens
  per request.**
- Rate limits (paid tier 1): 2M TPM, 2000 RPM. Free tier (no payment
  method): 3 RPM / 10K TPM — this is the source of the operator's earlier
  `429 rate-limit retries exhausted` during indexing.
- `VoyageMultimodalEmbeddingModel.embed(texts)` ALREADY accepts a list and
  ALREADY sends `inputs=[{"content":[{"type":"text","text": t}]} ...]`
  with the 429 exponential-backoff loop. The batching work is upstream of
  `.embed()`: group the pipeline's many texts into request-sized chunks
  bounded by the 1000-input / 320,000-token caps, then call `.embed()`
  once per chunk. **Do not touch the 429 backoff loop** inside
  `VoyageMultimodalEmbeddingModel` — it stays as the inner retry.

### Implementation shape

Add a batching helper in `tree/memory/embedding_text.py` (or a sibling
`tree/memory/embedding_batch.py` — implementer's call; keep it next to the
shared embed function from #041) of the form:

```
async def embed_in_batches(
    texts: list[str],
    embedding_model: BaseEmbeddingModel,
    *,
    max_inputs: int = 1000,
    max_total_tokens: int = 320_000,
    max_input_tokens: int = 32_000,
) -> list[list[float]]
```

- Greedily packs `texts` into chunks that respect BOTH the 1000-input cap
  AND the 320K total-token cap; never lets a single input exceed 32K
  tokens (truncate or split — the model already sends `truncation=True`,
  so rely on that and just bound the COUNT/TOTAL; document the reliance).
- Token counting: a cheap heuristic is acceptable (e.g. chars/4 estimate
  with a safety margin) — we do not need exact Voyage tokenization, just a
  conservative bound that keeps requests under the API caps. Document the
  heuristic and its safety margin. (You MAY confirm SDK ergonomics via the
  `context7` MCP server for `voyageai`, but the REST caps above are
  authoritative.)
- Returns vectors in the SAME order as the input `texts` (concatenate
  per-chunk results).
- Preserves the existing per-request 429 backoff (it lives inside
  `.embed()`; the batcher just calls `.embed()` per chunk).
- Make `max_inputs` / `max_total_tokens` configurable, ideally surfaced as
  YAML knobs under a new `embedding` (or reuse `query.embedding_batch_size`
  semantics) — implementer proposes the exact config shape; if adding YAML
  knobs, add the typed Pydantic fields too. Keep defaults at the Voyage
  caps so out-of-the-box behavior is correct.

### Wire the three stages through the batcher

1. **INDEXING** (`indexing/core.py::embed_nodes`/`_embed_batch`): replace
   the manual `range(0, len(docs), query.embedding_batch_size)` chunking
   with `embed_in_batches` over node-texts. (This is the stage that hit
   the 429s — fewer, larger requests directly reduce RPM pressure.)
2. **DEDUP / extraction task ④** (`pipeline.py`): task ④ currently maps
   one text at a time (`embed_entities_task` per canonical/node-text).
   Replace the one-at-a-time map with a SINGLE batched embed of all the
   node-texts for the run via `embed_in_batches`, then distribute vectors
   back into the `EmbeddingMap`. Preserve cache benefits where reasonable
   (a per-text cache can still wrap the batch; if the Prefect `INPUTS`
   cache on the mapped task is lost, document the tradeoff — fewer
   requests is the win the operator asked for).
3. **RESOLUTION** (`resolution/semantic.py`): the semantic resolver embeds
   the input name and each candidate name one-at-a-time inside
   `_embed_cached`. Add a path to pre-warm the cache by batching: embed all
   uncached candidate names (and the input names) in one
   `embed_in_batches` call, populate the LRU, then run the cosine loop
   against the cache. Keep the LRU and its eviction semantics. The
   resolution model (from #043) is the one used here.

### Out of scope

- The async Batch API discount path (see Rejected alternatives).
- Changing the 429 backoff schedule inside `VoyageMultimodalEmbeddingModel`.
- Switching models or endpoints.

## Acceptance Criteria

- [x] A batching helper exists next to the shared embed function and packs
      texts into chunks bounded by max 1000 inputs AND max 320,000 total
      tokens per request, never exceeding 32,000 tokens for a single
      input, returning vectors in input order.
- [x] Unit test: 2,500 short texts produce exactly 3 chunks by the
      1000-input cap (1000 + 1000 + 500) and the concatenated output has
      2,500 vectors in original order.
- [x] Unit test: a set of long texts that would blow the 320K total-token
      cap is split into multiple chunks by the TOKEN cap even when under
      1000 inputs.
- [x] Unit test: vectors come back in the same order as inputs (seed a
      mock model that returns an index-encoding vector and assert order).
- [x] INDEXING (`embed_nodes`) routes node-text embedding through the
      batcher; integration test confirms backfilling N nodes issues
      ceil(N / effective-chunk-size) requests, not N requests (assert via
      a counting mock embedding model).
- [x] Extraction task ④ embeds all run node-texts via the batcher in
      fewer requests than the one-per-name baseline; integration test
      asserts the request count drops (counting mock).
- [x] RESOLUTION pre-warms its embedding cache with a batched call;
      integration test asserts uncached candidate names are embedded in
      one batched request rather than one-per-name.
- [x] The 429 exponential-backoff inside `VoyageMultimodalEmbeddingModel`
      is untouched (diff shows no edits to that loop); a unit test still
      exercises the 429-retry path.
- [x] If YAML knobs are added, `default.yaml` + `app_config.py` carry the
      typed fields with defaults at the Voyage caps; a unit test loads
      them.
- [x] `make memory-unit-tests` pass; the new `make memory-integration-tests-all`
      batching tests pass. Full integration suite is GREEN via the canonical
      `make` target (221 passed / 1 skipped / 0 failed). The 10
      `Voyage API key is required` failures noted earlier were an env-loading
      artifact of running pytest outside `make` (bare `uv run pytest` does not
      load `.env`) — NOT pre-existing breakage and NOT a real missing key.
      See SWE log → Notes (CORRECTION).
- [x] Format/lint/pre-commit clean.

## User Stories

### Story: Indexing a large backfill no longer exhausts the rate limit
1. Operator has ~500 nodes lacking embeddings and runs
   `make memory-run-memory-pipeline-indexing USER_ID=<oid>`.
2. The batcher packs them into a handful of multi-input requests (bounded
   by 1000 inputs / 320K tokens) instead of dozens of small ones.
3. On a paid tier the run finishes in far fewer requests; on free tier the
   request count is low enough that the existing 429 backoff rides out the
   3-RPM window instead of exhausting retries.

### Story: Extraction embeds a document's entities in one shot
1. Extraction resolves 40 entities for a document.
2. Task ④ embeds all 40 node-texts in a single batched request (well under
   the 1000-input cap).
3. Dedup proceeds with the per-entity vectors distributed from that one
   response — same decisions, fewer API calls.

### Story: A developer is stopped from reaching for the async Batch API
1. A developer reads the task and the "Rejected alternatives" note.
2. They see the async Batch API is incompatible (12h window;
   `/v1/multimodalembeddings` unsupported) and that batching here means
   real-time multi-input requests.
3. They implement request packing against the 1000-input / 320K-token caps
   and leave the synchronous endpoint in place.

### Story: A single oversized input is bounded safely
1. A node-text happens to be very long (near or over 32K tokens).
2. The batcher relies on the model's `truncation=True` and its own
   per-input token bound so the request stays under the API cap.
3. The request succeeds (truncated) rather than 400-ing on token overflow.

---

Blocked by: #042, #043

## Log

### [PM] 2026-05-20 — Grooming

**Summary**
Adds real-time request batching across resolution, dedup, and indexing:
pack texts into multi-input synchronous `/v1/multimodalembeddings`
requests bounded by 1000 inputs / 320K total tokens (≤32K per input),
preserving the existing 429 backoff inside `VoyageMultimodalEmbeddingModel`.

**Key decisions**
- **Async Batch API is rejected** and must NOT be used. Verified reasons:
  (a) 12h completion window can't drive synchronous mid-flow dedup;
  (b) it doesn't support `/v1/multimodalembeddings`, the endpoint our
  pinned `voyage-multimodal-3` model uses. Captured as a "Rejected
  alternatives" AC so a future implementer doesn't reach for it.
- Batching is UPSTREAM of `.embed()`; the model's 429 retry loop is the
  inner retry and stays untouched.
- Token counting may use a conservative chars/4 heuristic with a safety
  margin (the model already sends `truncation=True`); exact Voyage
  tokenization not required.

**Dependencies**
- #042 (dedup/extraction embed node-text) and #043 (resolution model) —
  both stages must be on their final embedding before batching wraps them.

**User stories**
- 4 stories: large backfill no longer exhausts rate limit; document
  entities embedded in one shot; developer steered away from async Batch
  API; oversized input bounded safely.

**Open questions**
- (Surfaced in plan) Whether `max_inputs`/`max_total_tokens` should be YAML
  knobs or code constants — implementer's call unless operator prefers.

Ready for implementation.

### [SWE] 2026-05-20 15:05 — Implementation

**Decision on the open question**
- Added YAML knobs (the operator approved exposing them). New typed
  `EmbeddingBatchConfig` under `models.embedding_batch` with
  `max_inputs` / `max_total_tokens` / `max_input_tokens`, defaults at the
  Voyage caps (1000 / 320000 / 32000).

**Files modified**
- `apps/memory/src/tree/memory/embedding_text.py` — added the batcher:
  `estimate_tokens` (conservative chars/3 heuristic — OVER-counts vs the
  ~4 chars/token typical, so requests stay safely under caps),
  `_chunk_indices_by_caps` (greedy packing by input-count AND total-token
  caps; clamps a single oversized input to `max_input_tokens` and relies on
  the model's `truncation=True`), and the public `embed_in_batches(...)`.
  Wired `embed_node_texts` (the #041 shared fn — the dedup+indexing choke
  point) through `embed_in_batches`, resolving caps from
  `app_config.models.embedding_batch`. Added the "Rejected alternatives"
  note (async Batch API: 12h window + no `/v1/multimodalembeddings`) as a
  module-level comment so a future maintainer can't reach for it.
- `apps/memory/src/tree/memory/indexing/core.py` — INDEXING: `embed_nodes`
  no longer slices by `query.embedding_batch_size`; `_embed_batch` now
  delegates to `embed_node_texts` (which batches internally). Fewer, larger
  requests = direct RPM relief on the stage that hit the 429s.
- `apps/memory/src/tree/memory/extraction/pipeline.py` — DEDUP/task ④:
  replaced the per-text `.map()` (`_embed_entity(text) -> (text, vector)`,
  task name `embed-entity`) with a single batched `_embed_entities(texts)
  -> {text: vector}` (task name `embed-entities`) over `embed_in_batches`.
  Flow + MCP shim (`run_extraction_for_documents`) both route through the
  batcher.
- `apps/memory/src/tree/memory/resolution/semantic.py` — RESOLUTION:
  added `SemanticMatchResolver.prewarm_cache(names)` — one batched embed of
  all uncached, normalized-dedup'd names, seeding the LRU (eviction
  semantics preserved).
- `apps/memory/src/tree/memory/resolution/composite.py` — `resolve_with_types`
  pre-warms the semantic cache (input names + all candidate names) in one
  batched call before the cosine loop; no-op when no embedding model.
- `apps/memory/src/tree/config/app_config.py` + `apps/memory/configs/default.yaml`
  — typed `EmbeddingBatchConfig` + YAML block.
- Tests: `tests/unit/memory/test_embedding_text.py` (batcher + estimate),
  `tests/unit/memory/resolution/test_semantic.py` (prewarm),
  `tests/unit/memory/resolution/test_composite.py` (prewarm wiring),
  `tests/unit/config/test_app_config.py` (YAML knobs),
  `tests/unit/memory/extraction/test_pipeline.py` (task ④ rewrite),
  `tests/integration/memory/test_embedding_batching.py` (NEW — indexing /
  task ④ / resolution request-count drops via counting mocks).

**429 backoff — UNTOUCHED**
- `git diff src/tree/models/voyage_multimodal_embedding.py` is EMPTY. The
  batcher is strictly upstream of `.embed()`; the retry loop stays the inner
  retry. The 4 rate-limit tests in `test_voyage_multimodal_embedding.py`
  still pass (15/15).

**Chunking logic**
- Greedy left-to-right. Roll to a new chunk when adding the next text would
  exceed `max_inputs` OR (chunk non-empty AND total est-tokens + this text
  > `max_total_tokens`). A single text is clamped to `max_input_tokens` for
  accounting so one huge input still forms its own valid request.

**Order preservation**
- Chunks are contiguous slices covering `texts` left-to-right; results are
  concatenated in chunk order. Unit + e2e use an index-encoding mock that
  resets per request — a reorder/drop would change the output, so vector
  equality proves global order. E2E: 7 texts / cap 3 →
  `[[0.0],[1.0],...,[6.0]]`.

**Tests**
- Unit: 1246 passing, 0 failing — `make memory-unit-tests`.
- Integration (batching, NEW): 3 passing — indexing 250 nodes/cap 100 → 3
  requests; task ④ 40 entities → 1 batched request; resolution 30
  candidates → 1 batched request (names only).

**Evidence**
```
$ uv run pytest tests/unit -q
1246 passed in 43.86s

$ uv run pytest tests/integration/memory/test_embedding_batching.py -q
3 passed in 9.87s

$ uv run pytest tests/unit/models/test_voyage_multimodal_embedding.py -q
15 passed in 0.24s

$ git diff src/tree/models/voyage_multimodal_embedding.py   # (429 loop)
(empty — no edits)

$ uv run python  # e2e batcher smoke
count-cap req sizes: [1000, 1000, 500] -> total vectors 2500
token-cap req sizes: [2, 2, 1]
order-preservation: [[0.0], [1.0], [2.0], [3.0], [4.0], [5.0], [6.0]]
oversized single req sizes: [1]
estimate_tokens("x"*300)= 101
ALL E2E ASSERTIONS PASSED
```

**Notes / caveats for the Tester**
- CORRECTION (this note originally claimed 10 PRE-EXISTING / missing-key
  integration failures unrelated to #044 — that diagnosis was WRONG and the
  Tester proved it). The 10 `ModelError: Voyage API key is required` failures
  I saw were neither pre-existing breakage nor a real missing key: they were a
  HARNESS / env-loading artifact of invoking pytest OUTSIDE `make`. A bare
  `uv run pytest` does NOT load `.env`, so the live Voyage client had no key
  and raised. The canonical target `make memory-integration-tests-all`
  (`include .env` + `export` in the Makefile) supplies the key and the full
  integration suite is GREEN — 221 passed / 1 skipped / 0 failed, including
  `tests/integration/mcp/test_ingest_tools.py` and
  `tests/integration/scripts/test_migrate_pole_o_ontology.py`. There is no
  pre-existing breakage and no real missing key; always run integration tests
  via `make`, not bare `uv run pytest`.
- **Free-tier rate limits for any LIVE e2e:** Voyage free tier is 3 RPM /
  10K TPM. Batching REDUCES request count (the point of the feature), but a
  live indexing/extraction e2e on the free tier can still pause inside the
  429 backoff. That's expected and correct — the backoff rides it out. Use a
  paid-tier key for a fast live e2e, or expect the run to take minutes on
  free tier.
- Tradeoff recorded in code: task ④ gives up the per-text Prefect `INPUTS`
  cache (one key per node-text) in exchange for far fewer requests. The task
  still caches on the whole text list, so identical re-runs hit cache;
  partial-overlap re-runs lose the finer reuse. This is the win the operator
  asked for.
- `query.embedding_batch_size` (YAML) is now unused by indexing (the batcher
  owns chunking). Left in place to avoid scope creep / breaking config
  tests; a follow-up could remove it.
- No commit yet — awaiting Tester PASS + PM ACCEPT per process.

### [Tester] 2026-05-20 17:40 — QA

**Test summary**
- Format / lint / pre-commit: PASS (`make pre-commit` — prettier, ruff check,
  ruff format, biome, KGQuery discipline all Passed).
- Unit tests: 1246 passed / 0 failed (`make memory-unit-tests`), 0 warnings.
- Integration tests (FULL, mongot up): **221 passed / 1 skipped / 0 failed**
  (`make memory-integration-tests-all`, 556s) — including the exact files the
  SWE flagged (`mcp/test_ingest_tools.py`, `scripts/test_migrate_pole_o_ontology.py`).
- Warnings: 0.

**SWE "10 pre-existing failures" claim — INVESTIGATED, claim is INACCURATE but
gate is GREEN.** I verified `VOYAGE_API_KEY` IS present and real in the
worktree `.env` (`pa-` prefix, 46 chars). Run via the canonical
`make memory-integration-tests-all` target (which `include .env / export`s the
key), there are **ZERO failures** — all 221 pass. The 10 failures the SWE saw
were a HARNESS artifact, not a missing key and not pre-existing breakage: a
bare `uv run pytest` does NOT load `.env`, so the live Voyage client raises
`ModelError: Voyage API key is required`. I reproduced both halves: bare
`uv run python` → ModelError; `set -a && . ./.env` then `uv run` → live embed
succeeds. Net: the SWE's conclusion ("unrelated to #044") holds, but the
diagnosis ("missing key in this env / pre-existing") is wrong — it was a
`.env`-not-exported harness slip. AC #149-152's note text should be corrected
by PM/SWE, but nothing blocks the gate.

**E2E adversarial pass** — independent content-encoded embedder (vector =
hash of TEXT CONTENT, not call position, so reorder/mis-slice is caught):
- Happy path (LIVE Voyage): `embed_in_batches(5 texts, model, max_inputs=2)`
  against real `/v1/multimodalembeddings` → 3 real requests, 5×1024-d vectors,
  distinct + order-aligned. PASS.
- A (count cap): 2500 tiny texts, cap 1000 → `[1000,1000,500]`, content-order
  exact. PASS.
- B (token cap, the crux): 5 texts ~1001 est-tokens each, count cap 1000,
  total cap 2500 → split `[2,2,1]` though count << cap; content-order exact.
  PASS. **B2 (estimator truly gates):** SAME 5 inputs with huge token cap
  collapse to ONE request `[5]` — proves the split was the token estimator,
  not a count artifact. PASS.
- C (order preservation, the crux): 23 content-unique texts, cap 4 → 6 chunks
  `[4,4,4,4,4,3]`; asserted EVERY output index == content-hash of input at the
  same index. PASS.
- D (429 untouched): `git diff main..HEAD voyage_multimodal_embedding.py`
  EMPTY; 4 rate-limit/429 retry tests pass; batcher calls `.embed()` once per
  chunk so a 429 retries that chunk in-place without losing the batch. PASS.
- E (per-input 32K clamp): single 200K-char text → 1 request (clamped, not
  dropped, not crashing). E2: oversized + 3 neighbors share one request with
  clamped accounting, order kept. PASS.
- F (empty/single): `embed_in_batches([])` → `[]`, 0 model calls; single text
  → 1 vector, 1 call. PASS.
- G (all THREE stages wired): indexing `embed_nodes→_embed_batch→embed_node_texts→embed_in_batches`;
  task ④ per-text `.map()` GONE → single `_embed_entities`/`embed_in_batches`
  (+ MCP shim `run_extraction_for_documents` routes through batcher); resolution
  `resolve_with_types→prewarm_cache→embed_in_batches`. Integration tests assert
  the request-count drop in all three. PASS.
- H (YAML knob honored): set `app_config.models.embedding_batch.max_inputs=2`,
  `embed_node_texts(5 nodes)` → `[2,2,1]` — knob is live, not dead config. PASS.
- Boundary: 6 texts est=100 tokens each, total cap=200 → exact `[2,2,2]`. PASS.

**Acceptance criteria**
- [x] PASS — batching helper next to shared embed fn, bounded by 1000 inputs
      AND 320K tokens, never >32K/input, input-order output.
      Evidence: `embedding_text.py:140 embed_in_batches`; adversarial A/E/F + live e2e.
- [x] PASS — 2500 short texts → 3 chunks (1000+1000+500), 2500 vectors in order.
      Evidence: `test_embedding_text.py::test_splits_2500_short_texts...`; my A check.
- [x] PASS — long-text set blows 320K cap → split by TOKEN cap under 1000 inputs.
      Evidence: `test_splits_by_token_cap_even_under_input_count_cap`; my B + B2 checks.
- [x] PASS — vectors in input order (index-encoding mock).
      Evidence: `test_vectors_returned_in_input_order_across_chunks`; my C content-encoded check (harder).
- [x] PASS — INDEXING routes through batcher; ceil(N/chunk) requests not N.
      Evidence: `test_embedding_batching.py::TestIndexingBackfillBatches` (250/cap100→[100,100,50]).
- [x] PASS — task ④ batches all run node-texts in fewer requests.
      Evidence: `TestExtractionTaskFourBatches` + `test_pipeline.py::TestEmbedEntitiesTask`.
- [x] PASS — RESOLUTION pre-warms cache with one batched call.
      Evidence: `TestResolutionPrewarmBatches` + `test_composite.py::TestCompositeResolverPrewarmsSemanticCache`.
- [x] PASS — 429 backoff untouched; 429-retry unit test still exercised.
      Evidence: empty voyage diff; 4 `TestVoyageMultimodalRateLimitRetry` tests pass.
- [x] PASS — YAML knobs typed in app_config + default.yaml at Voyage caps; unit test loads them.
      Evidence: `EmbeddingBatchConfig`; `test_app_config.py` (defaults + caps-from-yaml); my H check.
- [x] PASS — unit tests pass + new batching integration tests pass.
      Evidence: 1246 unit / 221 integration (incl. 3 batching) green.
- [x] PASS — format/lint/pre-commit clean. Evidence: `make pre-commit` all Passed.
- [x] PASS — (oversized-input story) single >32K-token input bounded safely.
      Evidence: my E/E2 checks; `test_single_oversized_input_still_forms_a_request`.

**Evidence**
```
$ make memory-unit-tests
============================ 1246 passed in 41.26s =============================

$ make memory-integration-tests-all   # mongot up, .env exported by Makefile
================== 221 passed, 1 skipped in 556.07s (0:09:16) ==================

$ git diff main..HEAD -- .../voyage_multimodal_embedding.py
(empty)

$ ALL ADVERSARIAL CHECKS (content-encoded embedder): PASS
  A[1000,1000,500] B[2,2,1] B2[5] C[4,4,4,4,4,3] E[1] E2[4] F[] boundary[2,2,2]

$ LIVE Voyage embed_in_batches(5, max_inputs=2): 3 real requests, 5×1024-d, distinct: PASS
```

**Other issues found (non-blocking, for PM/orchestrator)**
- AC #149-152 note is factually wrong (claims missing `VOYAGE_API_KEY` /
  pre-existing failures). The key is present and the suite is fully green via
  the canonical target. The failures the SWE saw were a `.env`-not-exported
  harness slip on bare `uv run pytest`. Recommend the SWE/PM correct the note
  before squash; it does not affect code or the gate.
- `query.embedding_batch_size` is now dead config for indexing (SWE already
  flagged this for a follow-up). Nit, not blocking.

**VERDICT: PASS**
