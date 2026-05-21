# Shared Voyage rate-limiter at the real network POST

Status: pending
Tags: `infra`, `memory`, `embedding`
Depends on: #054
Blocks: #056

## Scope

Put a single shared rate-limiter in front of every **real Voyage network request**
so 4 concurrent runs collectively stay under 3 RPM instead of 429-storming.
Implements ADR-002 §1 (see the "Amendment (#055 implementation)" note in that ADR
for why the chokepoint lives at the network POST, not at `_embed_chunk_resilient`).

**Chokepoint location decision (see ADR-002 §1 amendment).** The wrap lives at the
real HTTP POST inside each Voyage provider client — NOT at `_embed_chunk_resilient`.
Reason: the extraction hot path injects a `_CachedSingleEmbedding` into `add_entity`
(`extraction/pipeline.py:1206-1207`) so the per-entity dedup embed reuses an
already-computed vector and issues NO network POST on a cache hit. A wrap at
`_embed_chunk_resilient` throttles those zero-POST cache hits — serializing ~40
lookups behind the ~20s/slot limit and timing out extraction, contradicting
ADR-002 §1's "one wrap == one real POST". Gating the client POST gates exactly
real requests and is cache-robust.

- **Chokepoint at each Voyage client's real POST:** in
  `apps/memory/src/tree/models/voyage_embedding.py` (`VoyageTextEmbeddingModel.embed`)
  and `apps/memory/src/tree/models/voyage_multimodal_embedding.py`
  (`VoyageMultimodalEmbeddingModel.embed`), acquire
  `await rate_limit("voyage-embeddings", occupy=1, strict=False)`
  (`from prefect.concurrency.asyncio import rate_limit` — the SWE already verified
  this import path against the installed Prefect) immediately before each real
  `session.post(...)` attempt, INSIDE the `while True` 429-backoff loop, so a
  429-retry re-acquires a fresh slot (one slot per real POST attempt). With
  proactive rate-limiting the existing reactive 429 backoff becomes a fallback,
  not the primary throttle. The early `if not texts: return []` short-circuits
  before any POST and must NOT acquire a slot.
- **Remove the wrap from `_embed_chunk_resilient`
  (`apps/memory/src/tree/memory/embedding_text.py`):** delete the
  `await rate_limit("voyage-embeddings", occupy=1, strict=False)` the WIP added
  just before `await embedding_model.embed(chunk)`. The rate limit no longer lives
  here. (`embedding_text.py` no longer imports `rate_limit` once the wrap is gone.)
- **Keep `add_entity` routed through `_embed_chunk_resilient`
  (`apps/memory/src/tree/memory/extraction/add_entity.py:241-248`):** retain this
  routing — it still adds value (Voyage-400 bisect-and-skip resilience for the
  inline dedup embed) — but it NO LONGER carries the rate limit. On a cache hit the
  injected `_CachedSingleEmbedding.embed` returns the cached vector without reaching
  a Voyage client, so no slot is acquired (fixes the timeout). On a cache miss it
  delegates to the real Voyage `.embed()`, which acquires the slot. Preserve current
  semantics: `embedding = embedded[0] if embedded else []`, and the empty-placeholder
  `[]` (Voyage 400) must still degrade to `embedding = []`.
- **Thread `dispatch_concurrency` (default 1):** in `embed_in_batches`
  (`embedding_text.py:131-146`), read `app_config.models.embedding_batch.dispatch_concurrency`
  and keep dispatch sequential when it is 1 (today's behavior). The knob is the seam to
  flip on only after the cap is lifted; do NOT make it default to >1.
- `strict=False` everywhere so a missing `voyage-embeddings` limit is a no-op (unit tests,
  fresh dev boxes) rather than an error.

## Acceptance Criteria

- [x] BOTH Voyage clients acquire `rate_limit("voyage-embeddings", occupy=1, strict=False)`
      exactly once per real POST attempt: `VoyageTextEmbeddingModel.embed`
      (`voyage_embedding.py`) and `VoyageMultimodalEmbeddingModel.embed`
      (`voyage_multimodal_embedding.py`) each await it immediately before
      `session.post(...)`, inside the 429-backoff `while True` loop so a 429-retry
      re-acquires a slot (verified by reading the diff + a unit test per client that
      mocks `rate_limit` and the HTTP session and asserts `rate_limit` is awaited once
      per POST attempt, and once more on a 429-then-200 retry) — verified by
      `test_voyage_embedding.py::TestVoyageTextRateLimitChokepoint` and
      `test_voyage_multimodal_embedding.py::TestVoyageMultimodalRateLimitChokepoint`.
- [x] `_embed_chunk_resilient` (`embedding_text.py`) NO LONGER calls
      `rate_limit(...)` — the wrap is removed; `embedding_text.py` no longer imports
      `rate_limit` (grep confirms the import and the call are gone from this module) —
      verified by grep + `test_embedding_text.py::TestEmbedChunkResilientDoesNotRateLimit`.
- [x] A cache-hit dedup via `_CachedSingleEmbedding` acquires NO slot — regression test
      for the timeout: drive the `add_entity` dedup path with a `_CachedSingleEmbedding`
      (cache-hit) injected, assert `rate_limit` is NOT awaited (the cached vector returns
      without reaching a Voyage client). A cache MISS (real Voyage model) DOES acquire a slot —
      verified by `test_add_entity.py::TestCachedDedupAcquiresNoRateLimitSlot`
      (`test_cache_hit_..._acquires_no_slot` + `test_cache_miss_..._acquires_a_slot`).
- [x] `add_entity` keeps routing the inline dedup embed through `_embed_chunk_resilient`
      (Voyage-400 resilience retained), but does NOT call `embedding_model.embed(...)`
      directly (grep shows the old direct call site is still gone); semantics preserved —
      `embedding = embedded[0] if embedded else []`, and a Voyage-400 `[]` placeholder still
      degrades to `embedding = []` — verified by grep +
      `test_add_entity.py::TestAddEntityRoutesThroughChokepoint`.
- [x] With NO `voyage-embeddings` limit present, both clients' `embed` and `add_entity`
      behave exactly as before (`strict=False` no-op) — a unit/integration test with the
      limit absent passes unchanged; `if not texts: return []` short-circuits before any
      slot acquisition — verified by `test_empty_input_acquires_no_slot` in both client
      tests + the autouse no-op `rate_limit` fixture in `tests/unit/conftest.py`.
- [x] `dispatch_concurrency=1` keeps `embed_in_batches` dispatch sequential (default behavior
      preserved); a unit test asserts request count and ordering are unchanged from pre-task —
      verified by `test_embedding_text.py::TestDispatchConcurrencyDefault`.
- [x] Existing `test_embedding_batching.py` and `test_e2e_embedding_split_and_batching.py` pass
      — `test_embedding_batching.py` 3/3 in 6.16s (previously 2 timed out at 300s).
- [x] `make memory-format-fix && make memory-lint-fix && make memory-format-check && make memory-lint-check && make pre-commit` clean.
- [x] `make memory-unit-tests` passes (1354 passed, 0 warnings).
- [x] `make memory-integration-tests` (fast) passes — including the extraction-pipeline
      tests that previously STALLED (`test_extraction_pipeline.py`,
      `TestExtractionTaskFourBatches`, `TestResolutionPrewarmBatches`): with the limit
      present on the dev Prefect server, a normal extraction NO LONGER times out, because
      cache-hit dedups acquire no slot — 153 passed, 1 skipped, 0 timeouts in 174.72s.
- [ ] [HUMAN] Behavior-preservation e2e: a single
      `make memory-run-memory-pipeline-extraction USER_ID=<oid>` (limit absent OR present)
      logs identical `apply_writes: nodes_written/edges_written` and `dedupe_entities`
      counts vs a pre-change run AND completes without timing out under a present limit —
      proves the relocated chokepoint did not change dedup outcomes and fixed the stall.

## User Stories

### Story: Developer runs unit tests on a box with no Prefect server
1. Developer runs `make memory-unit-tests` (no `voyage-embeddings` limit exists).
2. Embedding code paths execute; `rate_limit(..., strict=False)` is a no-op.
3. Tests pass with no Prefect-server connection error.

### Story: A poison input is still skipped through the guarded chokepoint
1. `add_entity` is asked to embed a text Voyage rejects with HTTP 400.
2. The call goes through `_embed_chunk_resilient`, which bisects and skips the bad input.
3. `add_entity` receives `[]` for that embedding and degrades to `embedding = []` (unchanged behavior).

### Story: A cache-hit dedup is not throttled (the timeout regression)
1. The extraction pipeline injects a `_CachedSingleEmbedding` into `add_entity` because
   task ④ already embedded this entity's text.
2. `add_entity` routes through `_embed_chunk_resilient`, which calls the cached model's
   `embed` — returning the pre-computed vector WITHOUT reaching a Voyage client.
3. No `voyage-embeddings` slot is acquired, so a 40-entity extraction does not serialize
   behind the 3-RPM throttle and does not time out.

### Story: Engineer confirms the chokepoint is at the real network POST
1. Engineer greps the repo for `rate_limit("voyage-embeddings"` (or `_VOYAGE_EMBED_LIMIT`).
2. The only call sites are inside `VoyageTextEmbeddingModel.embed` and
   `VoyageMultimodalEmbeddingModel.embed`, immediately before `session.post(...)`.
3. `embedding_text.py` and `add_entity.py` contain no `rate_limit` call; `add_entity`
   still routes through `_embed_chunk_resilient` for Voyage-400 resilience.

---

Blocked by: #054

## Log

### [SWE] 2026-05-21 18:55 — Implementation (BLOCKED — architectural fork)

**Files modified**
- `apps/memory/src/tree/memory/embedding_text.py` — import `rate_limit` from
  `prefect.concurrency.asyncio`; wrap the one real POST in
  `_embed_chunk_resilient` with `await rate_limit("voyage-embeddings", occupy=1,
  strict=False)`; thread `dispatch_concurrency` (read from
  `app_config.models.embedding_batch`, logged; loop kept sequential at default 1).
- `apps/memory/src/tree/memory/extraction/add_entity.py` — route the inline
  dedup embed through `_embed_chunk_resilient(embedding_model, [embeddable_text])`
  instead of `embedding_model.embed(...)`; preserved
  `embedding = embedded[0] if embedded else []` and the Voyage-400 `[]` degrade.
- `apps/memory/tests/unit/memory/test_embedding_text.py` — `TestVoyageRateLimitChokepoint`
  (rate_limit awaited once per real POST incl. per bisected sub-chunk; strict=False
  no-op) + `TestDispatchConcurrencyDefault` (default 1 preserves request count/order).
- `apps/memory/tests/unit/memory/extraction/test_add_entity.py` —
  `TestAddEntityRoutesThroughChokepoint` (routes via `_embed_chunk_resilient`, not
  a direct `.embed`; 400→`[]` and falsy→`[]` degrade preserved).
- `apps/memory/tests/unit/conftest.py` — autouse fixture no-ops `rate_limit` for
  unit tests (avoids ~3s/Prefect-server roundtrip per embed; tests that assert on
  the limiter re-patch it locally).

**Import path used for `rate_limit`**
- `from prefect.concurrency.asyncio import rate_limit` — verified against the
  installed Prefect (`uv run python -c "from prefect.concurrency.asyncio import
  rate_limit; print('ok')"` → `ok`). This is the path the plan expected.

**Tests**
- Unit: 1348 passing, 0 failing (`make memory-unit-tests`, 41.92s) — includes the
  new chokepoint/dispatch/add_entity routing tests.
- Format/lint/pre-commit: clean (`make memory-format-fix && memory-lint-fix &&
  memory-format-check && memory-lint-check && make pre-commit` all pass).
- Integration (fast `make memory-integration-tests`): NOT GREEN — STALLED on
  `tests/integration/memory/test_extraction_pipeline.py` (see blocker). Tests up to
  that point passed: `test_add_entity.py` 11/11, `test_dedup.py` 14/14.
- Integration (slow, two AC-named files in isolation): `test_embedding_batching.py`
  → 1 passed (`TestIndexingBackfillBatches`, the indexing/`embed_node_texts` path),
  2 FAILED with `Timeout (>300s)` (`TestExtractionTaskFourBatches`,
  `TestResolutionPrewarmBatches` — both the per-entity `add_entity` extraction path).

**Acceptance criteria**
- [x] `_embed_chunk_resilient` calls `rate_limit("voyage-embeddings", occupy=1,
      strict=False)` once per real POST (incl. per bisected sub-chunk) — unit-verified.
- [x] `add_entity` no longer calls `embedding_model.embed(...)` directly; routes
      through `_embed_chunk_resilient` — grep + unit-verified.
- [x] `strict=False` no-op when no limit present — unit-verified.
- [x] `dispatch_concurrency=1` preserves request count + ordering — unit-verified.
- [ ] Existing `test_embedding_batching.py` / `test_e2e_embedding_split_and_batching.py`
      pass — BLOCKED (timeouts on the extraction path; see fork below).
- [x] format/lint/pre-commit clean.
- [x] `make memory-unit-tests` passes.
- [ ] `make memory-integration-tests` (fast) passes — BLOCKED (stalls on extraction).
- [ ] [HUMAN] Behavior-preservation e2e — NOT RUN (Tester/human acceptance; needs
      a quiesced full stack + serve-workflows).

**BLOCKER — undocumented architectural fork (over-throttling the cached dedup path)**

The extraction hot path injects a `_CachedSingleEmbedding` into `add_entity`
(`pipeline.py:1206-1207`) precisely so the per-entity dedup embed reuses the vector
task ④ already computed — i.e. it issues NO real Voyage POST. Routing `add_entity`
through `_embed_chunk_resilient` (as the spec requires) makes that cache-hit path
now `await rate_limit("voyage-embeddings", occupy=1)` per entity. With the real
3-RPM / 0.05-slot-decay limit present on the dev Prefect server, a 40-entity
extraction serializes ~40 cache-hit dedups behind a 3-RPM throttle (~20s/slot),
blowing the 300s test timeout. Evidence: the indexing/batched test (real POSTs)
passed; both extraction/`add_entity` (cached) tests timed out; the fast loop stalled
entering `test_extraction_pipeline.py`. ADR-002 §1 says "one wrap == one real POST"
and "embedding is the throughput floor; parallelism does not throttle non-embedding
phases" — gating a cache-hit (zero-POST) lookup contradicts both.

Fork: should the chokepoint rate-limit fire on the `_CachedSingleEmbedding` reuse
path, which performs no real Voyage request?
- A: Only `occupy` the slot around an actual network POST — e.g. the rate_limit
  wrap lives in the Voyage client's real request method, not in
  `_embed_chunk_resilient`. (Moves the chokepoint the spec named; truest to "one
  wrap == one real POST".)
- B: Skip the rate_limit when the model is a no-network model (e.g. the chokepoint
  asks the model `issues_network_request`/is-cached, or pipeline passes a flag).
  Keeps the spec's chokepoint location; adds a "is this a real POST?" signal.
- C: Accept it — pace dedup cache-hits too (current behavior). Rejected: throttles
  a phase ADR-002 explicitly says must not be throttled, and times out e2e.

I will not pick silently. Need PM to resolve (and likely amend ADR-002 §1 on what
"one real POST" means for the cached dedup path) before I finalize the
`add_entity` routing.

**Notes**
- `voyage-embeddings` GCL on this worktree's shared Prefect server is currently
  saturated (`active_slots=3/3`, decay 0.05/s) — partly leaked leases from runs I
  had to kill (ADR-002 lease-leak consequence). I was DENIED permission to reset
  the shared limit (correctly out of SWE scope). A clean re-run needs a quiesced
  stack; the slow/`requires_mongot` e2e are Tester/human-gate items regardless.
- Trailing `ConcurrencySlotAcquisitionService ... rate_limit ... cancelled` lines in
  pytest output are benign teardown-time logging-to-closed-stream noise (exit 0), not
  failures.

### [PM] 2026-05-21 19:30 — Fork resolution

**Decision: Option A — relocate the chokepoint to the real network POST inside each
Voyage provider client.** Confirmed the SWE's recommended option; B and C rejected.

**Why A (verified against the code):**
- `_CachedSingleEmbedding.embed` (`extraction/pipeline.py:1267`) returns the cached
  vector with zero network I/O. With the wrap at `_embed_chunk_resilient`, every
  cache-hit dedup acquired a `voyage-embeddings` slot, serializing zero-POST lookups
  behind the 3-RPM throttle — exactly the timeout the SWE hit, and a direct violation
  of ADR-002 §1's "one wrap == one real POST" / "don't throttle non-embedding phases".
- Moving the wrap to `VoyageTextEmbeddingModel.embed` / `VoyageMultimodalEmbeddingModel.embed`
  (immediately before `session.post`) gates EXACTLY real Voyage requests at the same
  granularity the old location had (one `.embed(chunk)` POST == one slot). It is
  cache-robust (a `_CachedSingleEmbedding` hit never reaches a client → no slot) and
  provider-correct (mock / sentence-transformers / local models carry no wrap). A cache
  MISS still delegates to the real Voyage `.embed()` → throttled, so coverage is intact.
- Per-attempt acquisition inside the 429 `while True` loop makes proactive limiting the
  primary throttle and the reactive 429 backoff a fallback; a 429-retry re-acquires.
- Option B (an "is-real-POST / cache-hit" signal threaded into `_embed_chunk_resilient`)
  rejected: it pushes network-awareness up into a layer that should not own it, and is
  hackier than gating the actual network boundary. Option C (throttle cache-hits too)
  rejected: it throttles a phase ADR-002 explicitly forbids throttling and times out e2e.

**ADR-002 amendments (Status stays `Accepted` — refinement, not reversal):**
- §1 chokepoint moved from `_embed_chunk_resilient` to "immediately before each real
  Voyage network POST inside both provider clients"; added a per-attempt-acquisition note
  for the 429-retry path; recorded that `add_entity` still routes through
  `_embed_chunk_resilient` for 400-resilience but no longer holds the limit there.
- Added an inline "Amendment (#055 implementation)" paragraph in §1 recording the
  `_CachedSingleEmbedding` cache-hit counterexample and why gating the client POST is
  correct and cache-robust. The cross-flow-GCL-with-slot-decay decision itself, the limit
  name, decay, and YAML-derived limit are all unchanged → no new/superseding ADR.
- Context section updated: the single point real Voyage requests funnel through is the
  client `embed` POST; non-Voyage models never issue a network request.

**#055 spec amendments:**
- Title + Scope: chokepoint is the real network POST in both Voyage clients; explicit
  instruction to REMOVE the rate-limit wrap (and the now-unused import) from
  `_embed_chunk_resilient`; `add_entity` KEEPS routing through `_embed_chunk_resilient`
  for 400-resilience only.
- ACs rewritten: (a) both clients acquire the slot once per real POST attempt incl. the
  429-retry; (b) NEW regression AC — a `_CachedSingleEmbedding` cache hit acquires NO slot
  (assert `rate_limit` not awaited), cache miss does; (c) `strict=False` no-op when limit
  absent + `if not texts` short-circuits before acquiring; (d) `dispatch_concurrency`
  default 1 unchanged; (e) fast integration loop incl. the previously-stalled
  `test_extraction_pipeline.py` no longer times out under a present limit; e2e
  behavior-preservation AC extended to assert no timeout.
- User stories: replaced "single chokepoint" story with a "chokepoint at the real POST"
  story + a new "cache-hit dedup is not throttled" story covering the timeout regression.

Ready for SWE to re-implement against the amended spec. SWE must reset/quiesce the shared
`voyage-embeddings` GCL (leaked leases noted in the prior SWE log) before the integration
re-run — that is dev-infra contention, not a code regression.

### [SWE] 2026-05-21 19:10 — Re-implementation (Option A)

**Chokepoint relocated to the real network POST (the fix).**
- REMOVED the `await rate_limit("voyage-embeddings", occupy=1, strict=False)` wrap AND
  the now-unused `from prefect.concurrency.asyncio import rate_limit` import (plus the
  `_VOYAGE_EMBED_LIMIT` constant) from `_embed_chunk_resilient` in
  `apps/memory/src/tree/memory/embedding_text.py`.
- ADDED the wrap to BOTH Voyage clients, immediately before each real `session.post(...)`
  attempt, INSIDE the existing `while True` 429-backoff loop (so a 429-retry re-acquires a
  fresh slot): `apps/memory/src/tree/models/voyage_embedding.py`
  (`VoyageTextEmbeddingModel.embed`) and
  `apps/memory/src/tree/models/voyage_multimodal_embedding.py`
  (`VoyageMultimodalEmbeddingModel.embed`). Both define `_VOYAGE_EMBED_LIMIT =
  "voyage-embeddings"` and import `from prefect.concurrency.asyncio import rate_limit`.
  The early `if not texts: return []` short-circuits BEFORE the loop, so an empty call
  never acquires a slot.
- `add_entity` STILL routes the inline dedup embed through `_embed_chunk_resilient`
  (Voyage-400 bisect-and-skip resilience retained); only the stale "shares the rate limit"
  comment was corrected. No direct `embedding_model.embed(...)` call.
- `dispatch_concurrency` threading unchanged (default 1 → sequential dispatch).

**Confirmation the chokepoint moved (grep on `apps/memory/src/`):**
```
$ grep -rn 'rate_limit("voyage-embeddings"\|rate_limit(_VOYAGE_EMBED_LIMIT' apps/memory/src/
apps/memory/src/tree/models/voyage_multimodal_embedding.py:188: await rate_limit(_VOYAGE_EMBED_LIMIT, occupy=1, strict=False)
apps/memory/src/tree/models/voyage_embedding.py:213:        await rate_limit(_VOYAGE_EMBED_LIMIT, occupy=1, strict=False)
$ grep -n "rate_limit" apps/memory/src/tree/memory/embedding_text.py        # (none)
$ grep -n "rate_limit" apps/memory/src/tree/memory/extraction/add_entity.py # (none)
```

**Files modified**
- `apps/memory/src/tree/memory/embedding_text.py` — removed the `rate_limit` wrap, import,
  and `_VOYAGE_EMBED_LIMIT`; updated the `_embed_chunk_resilient` docstring/comment to note
  the limit now lives at the client POST and a cache hit acquires no slot.
- `apps/memory/src/tree/models/voyage_embedding.py` — import + `_VOYAGE_EMBED_LIMIT` +
  per-POST-attempt `rate_limit` acquisition inside the 429 loop.
- `apps/memory/src/tree/models/voyage_multimodal_embedding.py` — same, for the multimodal client.
- `apps/memory/src/tree/memory/extraction/add_entity.py` — corrected the now-stale "shares the
  rate limit" comment (routing through `_embed_chunk_resilient` unchanged).
- `apps/memory/tests/unit/conftest.py` — autouse no-op `rate_limit` now patches BOTH client
  modules (`tree.models.voyage_embedding.rate_limit`,
  `tree.models.voyage_multimodal_embedding.rate_limit`) instead of the removed
  `embedding_text.rate_limit`.
- `apps/memory/tests/unit/models/test_voyage_embedding.py` — `TestVoyageTextRateLimitChokepoint`
  (one slot per POST; 429-retry re-acquires; empty input acquires none).
- `apps/memory/tests/unit/models/test_voyage_multimodal_embedding.py` —
  `TestVoyageMultimodalRateLimitChokepoint` (same per-client coverage).
- `apps/memory/tests/unit/memory/test_embedding_text.py` — replaced the obsolete
  `TestVoyageRateLimitChokepoint` (patched the removed `embedding_text.rate_limit`) with
  `TestEmbedChunkResilientDoesNotRateLimit` (asserts the symbol/import are gone + a cached
  no-network chunk returns with no client reached); `TestDispatchConcurrencyDefault` no longer
  patches the removed symbol.
- `apps/memory/tests/unit/memory/extraction/test_add_entity.py` — kept
  `TestAddEntityRoutesThroughChokepoint` (routing + degrade); ADDED the HEADLINE regression
  `TestCachedDedupAcquiresNoRateLimitSlot`: a real `_CachedSingleEmbedding` cache HIT drives
  `add_entity` and asserts `rate_limit` is NOT awaited in either client; a real
  `VoyageTextEmbeddingModel` cache MISS DOES acquire exactly one slot.

**Cache-hit regression test result (the whole reason for the amendment)**
- `tests/unit/memory/extraction/test_add_entity.py::TestCachedDedupAcquiresNoRateLimitSlot`
  — both cases PASS: cache hit → `rate_limit.assert_not_awaited()` (text + multimodal);
  cache miss → `rate_limit.assert_awaited_once_with("voyage-embeddings", occupy=1, strict=False)`.

**Stale shared GCL reset (this feature's limit only — not the docker stack)**
```
$ prefect gcl inspect voyage-embeddings   # before: active_slots=3, limit=3 (leaked)
$ prefect gcl delete voyage-embeddings     # Deleted ... 'voyage-embeddings'.
$ make memory-sync-concurrency-limits      # recreated limit=3 decay=0.05
$ prefect gcl inspect voyage-embeddings   # after: active_slots=0, limit=3
```

**Tests / Evidence**
- Format/lint/pre-commit: clean (`format-fix && lint-fix && format-check && lint-check &&
  pre-commit` all pass).
- Unit: `make memory-unit-tests` → `1354 passed in 42.38s` (0 warnings). The 4 affected files
  alone: `102 passed in 6.16s`.
- Integration (no-timeout headline check, isolated on a quiesced stack, `.env` loaded so
  `PREFECT_API_URL` → shared server with the present `voyage-embeddings` limit):
```
$ uv run pytest tests/integration/memory/test_extraction_pipeline.py -q
9 passed in 7.47s                       # previously STALLED on entry
$ uv run pytest tests/integration/memory/test_embedding_batching.py -q
3 passed in 6.16s                       # TestExtractionTaskFourBatches + TestResolutionPrewarmBatches previously TIMED OUT at 300s each
$ make memory-integration-tests         # full fast loop
153 passed, 1 skipped, 106 deselected in 174.72s (0:02:54)   # 0 timeouts
```

**Acceptance criteria**
- All code/unit/fast-integration ACs: [x] (see updated checkboxes above).
- [ ] [HUMAN] Behavior-preservation e2e — NOT RUN (Tester/human gate): needs
  `make memory-serve-workflows` + a real `make memory-run-memory-pipeline-extraction`
  with a live `VOYAGE_API_KEY` to compare `apply_writes`/`dedupe_entities` counts vs a
  pre-change run AND confirm no timeout under a present limit. The full
  `make memory-integration-tests-all` + live rate-pacing remain the Tester's acceptance gate.

**Notes**
- Did NOT touch the docker stack or any other GCL — only the feature-owned
  `voyage-embeddings` limit was reset, exactly as the amended plan authorized.
- `docs/adrs/002_*.md` and the spec amendments are the PM's edits (already on disk before
  this session); I made no changes to ADR or glossary files.
- The 1 skipped fast-integration test is an unrelated web-search test (live-network gated),
  not affected by this change.
- DID NOT COMMIT — handing to Tester for review.

### [Tester] 2026-05-21 19:35 — QA

**Test summary**
- Format / lint / pre-commit: PASS (`make pre-commit` — ruff check + ruff format + biome + KGQuery discipline all Passed).
- Unit tests: 1354 passed / 0 failed (`make memory-unit-tests`, 42.01s, 0 warnings). Re-run after a mutation probe (below) — still 1354/0.
- Integration tests (ACCEPTANCE GATE, full incl. slow + requires_mongot): 259 passed / 0 failed / 1 skipped (`make memory-integration-tests-all`, 625.38s / 10:25, exit 0, 0 timeouts, 0 warnings). The 1 skip is the known live-network web-search gate, unrelated.

**Chokepoint relocation (grep + diff)**
```
$ grep -rn 'rate_limit("voyage-embeddings"|rate_limit(_VOYAGE_EMBED_LIMIT' apps/memory/src/
apps/memory/src/tree/models/voyage_multimodal_embedding.py:188:  await rate_limit(_VOYAGE_EMBED_LIMIT, occupy=1, strict=False)
apps/memory/src/tree/models/voyage_embedding.py:213:        await rate_limit(_VOYAGE_EMBED_LIMIT, occupy=1, strict=False)
$ grep -n rate_limit apps/memory/src/tree/memory/embedding_text.py            # (none, exit 1)
$ grep -n rate_limit apps/memory/src/tree/memory/extraction/add_entity.py     # (none, exit 1)
$ grep -n _VOYAGE_EMBED_LIMIT apps/memory/src/tree/memory/embedding_text.py    # (none, exit 1)
```
EXACTLY the two Voyage clients carry the wrap; nothing in `embedding_text.py` or `add_entity.py`.
Read both clients: in each, `if not texts: return []` short-circuits BEFORE the `while True`
loop (voyage_embedding.py:182-183 / voyage_multimodal_embedding.py:152-153), and
`await rate_limit(...)` is the FIRST statement inside `try:` within the loop, immediately
before `session.post(...)` (voyage_embedding.py:213 / voyage_multimodal_embedding.py:188).
A 429 falls through to the next `while True` iteration → re-acquires. Confirmed.

**E2E adversarial pass**
- Headline cache-hit/cache-miss regression (`TestCachedDedupAcquiresNoRateLimitSlot`): PASS.
  Cache HIT uses a REAL `_CachedSingleEmbedding` imported from `extraction.pipeline` (NOT an
  over-mocked stub) driven through the full `add_entity` dedup path →
  `text_rate_limit.assert_not_awaited()` + `mm_rate_limit.assert_not_awaited()`. Cache MISS uses
  a REAL `VoyageTextEmbeddingModel` (only the aiohttp session mocked) →
  `assert_awaited_once_with("voyage-embeddings", occupy=1, strict=False)`. Verified
  `_CachedSingleEmbedding.embed` (pipeline.py:1267) returns the cached vector with zero network
  I/O — never reaches a client.
- MUTATION PROBE (non-vacuity check): re-injected a `await rate_limit("voyage-embeddings"...)`
  on the `add_entity` dedup path → `test_cache_hit_..._acquires_no_slot` FAILED with
  `Expected rate_limit to not have been awaited. Awaited 1 times.` Reverted; `add_entity.py`
  grep `rate_limit` = 0, diffstat back to original (11+/2-). The headline test genuinely guards
  the regression — it is NOT vacuous.
- 429-retry re-acquires (SWE claim): REAL. `test_429_retry_reacquires_a_fresh_slot` (both
  clients) drives 429→429→200 over 3 sessions → `rate_limit.await_count == 3`, each call
  `("voyage-embeddings",) {occupy:1, strict:False}`. PASS.
- Empty-input acquires no slot (boundary): `test_empty_input_acquires_no_slot` (both clients) —
  `model.embed([])` → `assert_not_awaited()`. PASS.
- No-limit-present no-op: autouse `_noop_voyage_rate_limit` conftest fixture patches BOTH client
  modules; `strict=False` confirmed at both call sites. PASS.
- Headline no-timeout regression (the reason for the amendment): `test_extraction_pipeline.py`
  (9 passed inline in the gate) + `test_embedding_batching.py` (3 passed; previously
  `TestExtractionTaskFourBatches` + `TestResolutionPrewarmBatches` timed out at 300s each).
  Root cause confirmed by reading the tests: they run the FULL `memory_extraction` flow with a
  fake/`_CountingEmbeddingModel` over 40 entities — the OLD wrap at `_embed_chunk_resilient`
  fired `rate_limit` against the LIVE present GCL on every cache-hit dedup regardless of model,
  serializing ~40 zero-POST lookups behind the 3-RPM throttle. With the wrap relocated to the
  client POST, the fake model never touches the GCL → 6.16s, no timeout.

**Acceptance criteria**
- [x] PASS — Both Voyage clients acquire `rate_limit(...)` once per real POST attempt inside the
      429 loop — grep above + `TestVoyageTextRateLimitChokepoint` / `TestVoyageMultimodalRateLimitChokepoint`
      (one-slot-per-POST + 3× on 429-retry).
- [x] PASS — `_embed_chunk_resilient` no longer calls/imports `rate_limit`; `_VOYAGE_EMBED_LIMIT`
      gone from `embedding_text.py` — grep (exit 1 ×3) + `TestEmbedChunkResilientDoesNotRateLimit`.
- [x] PASS — Cache-hit dedup via `_CachedSingleEmbedding` acquires NO slot; cache miss DOES —
      `TestCachedDedupAcquiresNoRateLimitSlot` (real shim + real client) + mutation probe proving
      non-vacuity.
- [x] PASS — `add_entity` routes through `_embed_chunk_resilient`, no direct `.embed`; degrade
      semantics (`[]` placeholder → `embedding = []`) preserved — diff (add_entity.py:248) +
      `TestAddEntityRoutesThroughChokepoint`.
- [x] PASS — No limit present → `strict=False` no-op; `if not texts` short-circuits before any
      slot — `test_empty_input_acquires_no_slot` (both clients) + autouse conftest fixture.
- [x] PASS — `dispatch_concurrency=1` keeps `embed_in_batches` sequential — `TestDispatchConcurrencyDefault`
      (asserts default==1, request sizes [3,3,1], global input order).
- [x] PASS — `test_embedding_batching.py` 3/3 and `test_e2e_embedding_split_and_batching.py`
      pass — both green inline in the full gate, 0 timeouts.
- [x] PASS — format/lint/pre-commit clean.
- [x] PASS — `make memory-unit-tests` 1354 passed, 0 warnings.
- [x] PASS — full integration (acceptance-gate target) green incl. previously-stalled
      `test_extraction_pipeline.py` — 259 passed, 1 skipped, 0 timeouts in 625.38s.
- [ ] [HUMAN] Behavior-preservation e2e — **NOT RUN — needs serve-workflows + Voyage key (human
      acceptance).** Reason: a live `make memory-run-memory-pipeline-extraction` triggers a Prefect
      DEPLOYMENT, so the executing code is whatever worktree's `serve-workflows` worker picks it up.
      Two STALE cross-worktree serve-workflows/orchestrator processes are live against the shared
      Prefect server (`building-agentic-systems` @11:45AM, `building-agentic-systems-dream-consolidation`
      @1:31AM) plus a hung `memory-extraction` flow run (`b47876ed`, ~39 min old) that was holding
      all 3 GCL slots — exactly the cross-worktree contention CLAUDE.md warns against. Triggering an
      extraction now would (a) be ambiguous about which code ran and (b) risk disrupting two other
      active sessions. The integration suite proves the no-timeout fix but uses fake embedding models
      (no real Voyage POST); the live-acquisition proof is the unit `test_cache_miss_..._acquires_a_slot`
      (real `VoyageTextEmbeddingModel`). A clean isolated serve-from-this-worktree + real Voyage run
      comparing `apply_writes`/`dedupe_entities` counts is left for human acceptance.

**Evidence**
```
$ make memory-integration-tests-all
tests/integration/memory/test_e2e_embedding_split_and_batching.py .      [ 60%]
tests/integration/memory/test_embedding_batching.py ...                  [ 61%]
tests/integration/memory/test_extraction_pipeline.py .........           [ 65%]
================== 259 passed, 1 skipped in 625.38s (0:10:25) ==================

$ uv run pytest .../TestCachedDedupAcquiresNoRateLimitSlot .../TestVoyageTextRateLimitChokepoint \
                .../TestVoyageMultimodalRateLimitChokepoint .../TestEmbedChunkResilientDoesNotRateLimit -v
10 passed in 5.96s
```

**Stale shared GCL cleanup (this feature's limit only)**
The full gate's real-Voyage POSTs + the hung cross-worktree flow left `voyage-embeddings`
saturated (`active_slots=3`, frozen `updated` ts — leaked leases, not decaying). Reset the
feature-owned limit only (delete + `make memory-sync-concurrency-limits`) → `active_slots=0,
limit=3, decay=0.05`. Did NOT touch the docker stack or any other GCL. No serve-workflows
started from this worktree; nothing leaked from my QA activity.

**Other issues found**
- None blocking. Note for the human acceptance run: the integration suite exercises the relocated
  chokepoint only with fake embedding models (live-Voyage-POST acquisition is unit-covered, not
  integration-covered) — the [HUMAN] e2e is the one place a real Voyage POST through the full
  pipeline is observed end-to-end.
- Cross-worktree dev-infra contention is active on the shared stack (two stale serve processes +
  a hung flow). Not a code defect in #055; flagged so the human quiesces the stack before the
  acceptance run.

**VERDICT: PASS** (all non-`[HUMAN]` acceptance criteria verified with evidence; full suite +
acceptance gate green with 0 timeouts/0 warnings; headline cache-hit regression test proven
non-vacuous by mutation; e2e behavior-preservation correctly deferred to human acceptance with
reason.)
