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

### [PM] 2026-05-22 — Rework groom (#061)

**Trigger:** the project owner reviewed the shipped #056 fan-out on open PR #24
(before merge) and requested a DESIGN CHANGE: do NOT introduce a second extraction
entrypoint (`memory-extraction-fanout-etl`). Instead the SAME `memory-extraction-etl`
deployment should gain a `num_shards` knob — `num_shards=1` (default) == today's
extraction; `num_shards>1` orchestrates the fan-out via recursive self-dispatch and a
single trailing index.

**#056's separate-entrypoint design is SUPERSEDED.** The standalone parent flow
`memory_extraction_sharded`, its `memory-extraction-fanout-etl` deployment,
`scripts/run_extraction_fanout.py`, and the `run-memory-pipeline-extraction-fanout`
Make target are all to be DELETED. The fan-out semantics #056 verified (document-shard
axis, balanced contiguous partition, `gather(return_exceptions=True)`
failure-isolation, exactly one trailing index) are PRESERVED — only the topology
changes from "two deployments" to "one deployment + a `num_shards` parameter using
one-level recursive self-dispatch".

**ADR-002 §3 amended (#061)** — recorded the in-flow `num_shards` recursive
self-dispatch topology, why (owner wants one entrypoint; `num_shards=1` == prior
behavior; trailing index only on the orchestrator path; children always
`num_shards=1` so recursion terminates after one level). Status stays `Accepted`
(topology refinement, same fan-out decision); the cross-flow GCL limiter (§1),
document-shard axis (§3), `serve(global_limit=…)` admission control (§4), and the
same-user write-interleaving tradeoff (Consequences) are UNCHANGED.

**Filed rework task:** `tracker/061-fold-fanout-into-extraction-deployment.groomed.md`
(depends on #054/#056; supersedes #056's separate-entrypoint design). 16 ACs (incl. 4
`[HUMAN]` live ACs now run via `make memory-run-memory-pipeline-extraction USER_ID=…
NUM_SHARDS=4`) + 6 user stories. The #056 unit + integration fan-out tests are to be
reworked to target `memory_extraction(num_shards=…)`.

Pipeline re-runs the inner loop on #061; on green, re-run acceptance on the feature.

### [PM] 2026-05-22 — Acceptance (post-#061 rework)

**VERDICT: ACCEPT**

Re-reviewed the feature after the #061 design change (commit `331a6fa`) landed, from
the project owner's verbatim intent: "I don't want another entrypoint such as
`memory-extraction-fanout-etl`; just tweak the code to have the SAME
`memory-extraction-etl` with a fanout option when fanout>1; fanout=1 == what we had
before." Spot-checked source + git, did NOT trust the Tester's PASS blindly. Did NOT
re-run the integration suite — the shared docker stack is contended across worktrees
(CLAUDE.md), and the Tester's `make memory-integration-tests-all` was green at 268
passed / 1 skipped / 0 warnings on a quiesced+isolated stack; relied on that per the
brief.

**1. Owner's intent delivered EXACTLY — verified in shipped code:**
- ONE deployment. `orchestrator.py` registers a single `memory-extraction-etl`
  (`memory_extraction.to_deployment`, no `_sharded`), keeps
  `serve(global_limit=app_config.concurrency.runner_global_limit)`. `import
  tree.orchestrator` → OK.
- `memory_extraction(user_id, document_ids=None, num_shards=1)` —
  `pipeline.py:1546-1550`, `num_shards` non-Optional int default 1, return
  `WriteSummary | FanOutStats`.
- `num_shards <= 1` == prior behavior. Worker path branch at `pipeline.py:1587`
  (`if num_shards > 1: return …`) returns BEFORE the worker body, so the worker body
  (1594+) is the unchanged extraction. No `run_deployment`, no `memory-indexing-etl`
  reference anywhere after line 1594 (grep clean).
- `num_shards > 1` shards via recursive self-dispatch + one trailing index.
  `_orchestrate_sharded_extraction` (1469) → resolve pending (or explicit verbatim) →
  `_partition_into_shards(min(num_shards,N))` → `_fan_out_extraction` dispatches each
  child `{user_id, document_ids: shard, num_shards: 1}` under
  `gather(return_exceptions=True)` → ONE trailing `memory-indexing-etl` after the
  gather, regardless of failures. Children carry `num_shards=1` → worker path →
  recursion terminates at one level (`sharding.py:235`).

**Independent confirmation of the "worker path byte-for-byte identical to HEAD" claim
(did not just trust the Tester):** `git show 331a6fa -- …/pipeline.py` removed
EXACTLY ONE line — the return annotation `) -> WriteSummary:`. Every other change is
pure addition (the `num_shards: int = 1` param, the `WriteSummary | FanOutStats`
return type, the orchestrator early-branch, `_orchestrate_sharded_extraction`,
docstring). Zero deletions inside the worker body. The parity is provable at the diff
level, not just by assertion.

**2. No second entrypoint remains:**
`grep -rn "memory_extraction_sharded|memory-extraction-fanout-etl|run_extraction_fanout|run-memory-pipeline-extraction-fanout"`
across `apps/memory/src apps/memory/scripts apps/memory/Makefile` (and the whole
`apps/memory/` incl. tests) → NOTHING (exit 1). `fanout.py` and
`run_extraction_fanout.py` deleted (renamed → `sharding.py`, which carries NO `@flow`
/ `to_deployment`). Single Make target `run-memory-pipeline-extraction` threads
`NUM_SHARDS` → `--num-shards`; script targets `memory-extraction-etl/memory-extraction-etl`
(no new deployment name), `init_logger()` at module level, `--num-shards < 1` guard
exits 1 (exercised: `0` and `-3` both → exit 1). KGQuery discipline allowlist
re-pointed `fanout.py` → `sharding.py`.

**3. No regression to the rest of the feature:** #061 (`331a6fa`) touched only the
fan-out topology files (extraction pipeline/sharding, orchestrator, fanout
script/Makefile/tests, discipline allowlist, ADR, tracker). It did NOT touch the
Voyage rate-limiter clients, `embedding_text.py`, `add_entity.py`, the
bulk_write/dedupe/chunking refactors, or `indexing/pipeline.py` — those #055/#057/#058/#059
changes are frozen. The Tester's acceptance gate (`integration-tests-all`, mongot up,
isolated) was 268 passed / 1 skipped / 0 warnings; trusted per the contention rule.
Independent spot-check: 37 unit fanout tests pass mongot-free.

**4. No silent descoping vs the plan's "Out of scope (intentional)" list:** R5, R6,
the `voyage-tokens` second limiter, apply-writes node-loop parallelization, and
data-pipeline fan-out remain intentionally deferred (verified in the 2026-05-21
acceptance; #061 touched none of those files). The fan-out semantics #056 verified
(document-shard axis, balanced contiguous partition, gather failure-isolation, single
trailing index) are all preserved — only the topology changed.

**Residual [HUMAN] live-verification — refreshed for the NEW entrypoint** (not a
reason to reject; surfaced for the user to run on a QUIESCED stack). The in-process
suite mocks `run_deployment` + fake embedding models, so the live 4-way fan-out, the
limiter negative-check, and deployment-triggered worker parity are genuinely
unverified. See the checklist handed to the orchestrator.

SWE may keep the commits; the reworked feature is accepted with no regression. The
orchestrator must surface the refreshed [HUMAN] checklist so the user has no false
confidence about the live path.
