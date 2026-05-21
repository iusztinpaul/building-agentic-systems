# Feature Plan: Pipeline Parallelism — 4-Way Memory-Extraction Runs Under the Voyage Free-Tier Cap

## Summary
Optimize the Prefect memory pipelines so up to 4 memory-extraction runs execute
concurrently while collectively staying under the shared Voyage free-tier cap
(3 RPM / 10K TPM), plus tier-independent intra-run batching/parallelism wins. Part
A adds a document-shard fan-out parent flow, a cross-flow Voyage rate-limiter
(Prefect global concurrency limit with slot decay), `serve()` admission control,
and a typed `concurrency:` config block. Part B collapses per-edge / per-node write
loops into `bulk_write`, parallelizes the read-only dedupe task, and adds an
off-by-default doc-level chunking fan-out. Embedding stays the throughput floor at
3 RPM until the cap is lifted; the win is overlapping the CPU/DB phases and keeping
4 runs legal.

## Tasks (in order)
1. **#054** — Concurrency config scaffolding + ADR-002 + sync-limits target —
   typed `ConcurrencyConfig`, `concurrency:` YAML block, new extraction/embedding
   knobs (`doc_concurrency=1`, `dedup_concurrency=8`, `dispatch_concurrency=1`),
   `embedding_batch.max_total_tokens` 320000→10000, `make memory-sync-concurrency-limits`,
   and `docs/adrs/002_*.md`. No runtime behavior change yet.
2. **#055** — Shared Voyage rate-limiter at the single embed chokepoint —
   `rate_limit("voyage-embeddings", occupy=1, strict=False)` inside
   `_embed_chunk_resilient`; route `add_entity:241` through that chokepoint;
   thread `dispatch_concurrency` (default 1). Depends on #054.
3. **#055→#056** — Parent fan-out flow + admission control —
   new `memory_extraction_sharded(user_id, document_ids=None, num_shards=None)`
   in `extraction/fanout.py`, `serve(global_limit=…)` + deployment registration in
   `orchestrator.py`, and `make memory-run-memory-pipeline-extraction-fanout`.
   Depends on #054 and #055.
4. **#057** — R1+R2: collapse edge & structural-node upsert loops to `bulk_write` —
   behavior-preserving refactor of `pipeline.py` `_upsert_edge` (1098-1115) and
   `_upsert_structural_node` (1004-1019) loops. Depends on #054.
5. **#058** — R3: parallelize the read-only dedupe task via bounded `asyncio.gather` —
   `dedupe_entities` (`pipeline.py:898-933`) under `Semaphore(dedup_concurrency)`.
   Behavior-preserving. Depends on #054.
6. **#059** — R7+R4: doc-level chunking fan-out (default off) + validate-raws insert_many —
   `extract_chunks_and_structural_task` fan-out via `Semaphore(doc_concurrency)`
   (default 1 = today's behavior) and `_validate_raws` `insert_many` accumulation.
   Depends on #054.

## Out of scope (intentional)
- **R6 — doc-level fan-out of the LLM task ②.** It already gathers chunks at
  `Semaphore(llm_concurrency=5)`; stacking doc fan-out gives docs×5 concurrent
  Gemini calls with no quota benefit. The correct lever (if Gemini has headroom)
  is raising `extraction.llm_concurrency` — not in this round.
- **R5 — parallel embed-request dispatch.** At 3 RPM, K concurrent requests share
  one quota and all 429 together. The `dispatch_concurrency` knob ships in #054
  defaulting to **1** (today's behavior); it is the seam to flip on only after a
  payment method lifts the cap. Not enabled now.
- **Token-weighted `voyage-tokens` second limiter.** Deferred follow-up; ship the
  `max_total_tokens=10000` config cap first, add the token limit only if 429s persist.
- **apply-writes node loop parallelization.** Stays sequential — read-after-write
  through dedup + `name_to_target_id` feeds the edge-remap pass.
- **Data-pipeline fan-out changes.** `data_pipeline` already fans out over sources
  in-process and makes no Voyage calls; out of scope.

## Documentation updates (this grooming round)
- **Glossary:** no glossary in this project; not applicable. No new domain terms.
- **ADRs:** new `docs/adrs/002_pipeline_concurrency_and_voyage_rate_limiting.md`
  (Accepted) records the cross-flow Voyage rate-limiter primitive, the
  document-shard fan-out axis, and the accepted same-user write-interleaving
  tradeoff. ADR-001 is unchanged (no contradiction). The ADR is committed in the
  grooming commit (task #054), not as a separate implementation task.

## [HUMAN]-only acceptance criteria (cannot be fully automated)
These require the live Docker stack + `make memory-serve-workflows` running and a
real (rate-paced) Voyage key, so they are marked `[HUMAN]` in the task specs:
- **#056** — Live 4-way fan-out observed in the Prefect UI: parent spawns 4 child
  `memory-extraction-etl` runs over disjoint shards, ≤4 execute at once
  (`global_limit`), Voyage embeds serialize to ~3/min with NO
  "rate-limit retries exhausted" / 429 warnings, and a SINGLE `memory-indexing-etl`
  run fires after the shards complete (plan Verification §3).
- **#056** — Negative check: temporarily delete/raise the `voyage-embeddings` limit
  and confirm 429 warnings reappear under 4-way fan-out, proving the limiter holds
  the line (plan Verification §7).
- **#055/#057/#058/#059** — Behavior-preservation e2e: a single
  `make memory-run-memory-pipeline-extraction USER_ID=<oid>` logs IDENTICAL
  `apply_writes: nodes_written/edges_written` and `dedupe_entities:
  n_merged/n_flagged/n_none` counts pre/post change (plan Verification §4). The
  Tester can run this; signing off requires the full mongot stack up.

## Open questions
- None blocking. The plan is fully specified and pre-researched; all ambiguities
  (fan-out axis, TPM mitigation order, what NOT to do) are resolved in the source plan.

## Log

### [PM] 2026-05-21 — Acceptance Review (whole feature, user POV)

**VERDICT: ACCEPT**

Reviewed all 7 task tracker logs (#054–#060) against the diff, the amended ADR-002,
and the plan's intentional-deferral list. Spot-checked source rather than trusting
the Tester's PASS. Acceptance gate (`make memory-integration-tests-all`) was run
green per-task by the Tester on a quiesced+isolated stack — latest tip (#059):
266 passed / 1 skipped / 0 failed / 0 warnings in 621s. Did NOT re-run: the shared
docker stack is currently contended (two stale cross-worktree serve-workflows
processes live — `building-agentic-systems` @11:45AM, `…-dream-consolidation`
@1:31AM), and CLAUDE.md forbids running an integration suite against a contended
shared stack. A re-run was optional per the review brief; relying on the Tester's
isolated-stack evidence instead.

**Amended ADR-002 §1 conformance — VERIFIED in shipped code:**
- `rate_limit(_VOYAGE_EMBED_LIMIT, occupy=1, strict=False)` lives ONLY in the two
  Voyage clients (`voyage_embedding.py:213`, `voyage_multimodal_embedding.py:188`),
  as the first statement inside the `while True` 429-backoff loop, immediately
  before `session.post(...)`, after the `if not texts: return []` short-circuit.
- `embedding_text.py` has ZERO `rate_limit` references (grep -c = 0); `add_entity.py`
  has none. Cache hits via `_CachedSingleEmbedding` never reach a client → not
  throttled. The unit conftest no-op patches BOTH client modules, matching the
  relocation. This is exactly the amended decision (limiter at the network POST, not
  `_embed_chunk_resilient`).

**Intentional-deferral list — all VERIFIED deliberate, none silently missed:**
- R5 (parallel embed dispatch): `dispatch_concurrency: 1` in YAML; `embed_in_batches`
  loop stays a sequential `for` (embedding_text.py:142). Seam present, off.
- R6 (LLM task ② fan-out): `for chunked in chunked_docs: …llm_extract_entities_task`
  loop (pipeline.py:1545-1546) byte-for-byte unchanged.
- voyage-tokens second limiter: absent (grep none); TPM held by
  `max_total_tokens: 10000` (was 320000), comment notes the deferral.
- apply-writes node loop: still sequential read-after-write via `add_entity`
  (pipeline.py:1290) feeding `name_to_target_id`.
- data-pipeline fan-out: untouched (diff does not modify `data_pipeline`).

**User-facing surface confirmed:** `make memory-sync-concurrency-limits` and
`make memory-run-memory-pipeline-extraction-fanout USER_ID=<oid> [NUM_SHARDS=<n>]`
both present with USER_ID guards; fan-out deployment `memory-extraction-fanout-etl`
registered with `serve(global_limit=runner_global_limit)` admission control;
`concurrency:` YAML block discoverable with explanatory comments. Both new entry
scripts call `init_logger()` at module level per CLAUDE.md.

**Residual [HUMAN] live-verification (NOT a reason to reject — surfaced for the user
to run on a QUIESCED stack before/after merge):** see the checklist handed to the
orchestrator. The in-process integration suite proves the no-timeout fix, the
fan-out/single-index contract, and behavior-preservation, but exercises fake
embedding models (no real Voyage POST) and a mocked `run_deployment` — the live
4-way fan-out observation, the limiter negative-check, and the deployment-triggered
behavior-preservation e2e remain genuinely unverified.

SWE may keep the commits; the feature is accepted. The orchestrator must surface the
[HUMAN] checklist to the user so they have no false confidence about the live path.
