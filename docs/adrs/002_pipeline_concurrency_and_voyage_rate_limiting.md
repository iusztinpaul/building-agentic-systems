# ADR-002: Pipeline Concurrency and Cross-Flow Voyage Rate-Limiting

- **Status:** Accepted
- **Date:** 2026-05-21
- **Deciders:** Paul (project owner)
- **Context references:**
  - `tracker/feature-pipeline-parallelism-plan.md` (this feature's task plan)
  - `apps/memory/src/tree/memory/consolidation/dream.py` (the fan-out shape this mirrors, #052)
  - `ADR-001` (data-model/ontology — unchanged by this ADR)

## Context

The memory-extraction pipeline runs essentially serially: deployments are
served with no admission control, and the work inside a run (chunking,
dedup, Mongo writes) runs in `for` loops. We want up to **4 memory-extraction
runs to execute concurrently** to overlap the CPU/DB-bound phases.

The gating constraint is the Voyage embedding API: ONE shared free-tier key
capped at **3 RPM / 10K TPM** (no payment method). Two code paths reach Voyage:
the batched embed path through `_embed_chunk_resilient` (`embedding_text.py`)
and an inline dedup embed in `add_entity`. The single point every real Voyage
request funnels through is the HTTP POST inside the Voyage provider clients'
`embed` method (`VoyageTextEmbeddingModel` / `VoyageMultimodalEmbeddingModel`);
non-Voyage models (mock, sentence-transformers, the `_CachedSingleEmbedding`
cache-hit shim) never issue a network request. Running 4 runs in parallel does
NOT speed embedding — it stays serialized at 3 RPM. The win is overlapping the
non-embedding phases; the risk is 4 runs collectively 429-storming Voyage.

## Decision

1. **Cross-flow rate limiting uses a Prefect global concurrency limit with
   slot decay**, named `voyage-embeddings` (limit = `concurrency.voyage_rpm`,
   slot-decay-per-second = `voyage_rpm / 60`). It is the only primitive that
   spans separate flow runs (a per-process `asyncio.Semaphore` cannot). Acquired
   via `rate_limit("voyage-embeddings", occupy=1, strict=False)` immediately
   before each real Voyage network POST, inside the Voyage provider clients
   (`VoyageTextEmbeddingModel.embed` in `models/voyage_embedding.py` and
   `VoyageMultimodalEmbeddingModel.embed` in `models/voyage_multimodal_embedding.py`).
   `strict=False` makes a missing limit a no-op (graceful degradation for unit
   tests / fresh dev boxes). The slot is acquired per real POST *attempt*, so a
   429-retry inside the client's backoff loop re-acquires a fresh slot. The
   `add_entity` inline dedup embed still routes through `_embed_chunk_resilient`
   for the Voyage-400 bisect-and-skip resilience, but no longer holds the rate
   limit there — the limit lives at the network boundary instead.

   **Amendment (#055 implementation).** The chokepoint was originally specified
   at `_embed_chunk_resilient` (`embedding_text.py`) on the premise "one wrap ==
   one real POST". Implementation surfaced a counterexample: the extraction hot
   path injects a `_CachedSingleEmbedding` into `add_entity`
   (`extraction/pipeline.py`) so the per-entity dedup embed REUSES the vector
   already computed upstream and issues NO network POST on a cache hit. With the
   rate limit at `_embed_chunk_resilient`, every such cache-hit dedup acquired a
   `voyage-embeddings` slot — serializing ~40 zero-POST lookups behind the
   ~20s/slot throttle and timing out a normal extraction. That directly
   contradicts this section's own principle ("one wrap == one real POST"; do not
   throttle non-embedding phases). Relocating the wrap down to the real HTTP POST
   inside each Voyage client gates **exactly** real Voyage requests at the same
   granularity (one slot per `.embed(chunk)` POST as before), is robust to
   caching (a `_CachedSingleEmbedding` hit never reaches a Voyage client, so it
   is never throttled), and is provider-correct (mock / sentence-transformers /
   local models carry no wrap and are not throttled). A cache *miss* still
   reaches the real Voyage `.embed()` and is throttled. This is a refinement of
   the same cross-flow-GCL-with-slot-decay decision — the limiter, its name, its
   decay, and the YAML-derived limit are unchanged — so the ADR stays `Accepted`
   rather than being superseded; only the documented location of the wrap moves.

2. **The TPM cap is held by config, not a second limiter (for now):**
   `models.embedding_batch.max_total_tokens` drops 320000 → 10000 so no single
   request can exceed the 10K TPM window. A token-weighted `voyage-tokens`
   limit is an explicit deferred follow-up, added only if 429s persist.

3. **The fan-out axis is document-shards of one user.** A user's pending
   documents are partitioned into N contiguous shards; one `memory-extraction-etl`
   run is launched per shard via `run_deployment` under
   `asyncio.gather(return_exceptions=True)`, then ONE `memory-indexing-etl` run is
   triggered after all shards finish (indexing is a global backfill over unembedded
   nodes — running it per-shard would race 4 writers).

   **Amendment (#061 — fold fan-out into the single extraction deployment).** The
   #056 implementation realised the fan-out as a SEPARATE parent flow
   `memory_extraction_sharded` registered as a SECOND deployment
   `memory-extraction-fanout-etl`. On review (open PR #24, before merge) the project
   owner rejected the two-entrypoint shape: it forces operators to know which of two
   extraction commands to run, and the worker deployment (`memory-extraction-etl`)
   and the orchestrator deployment (`memory-extraction-fanout-etl`) are otherwise the
   same logical pipeline. The fan-out axis, the document-shard partitioning, the
   `asyncio.gather(return_exceptions=True)` failure-isolation, and the
   single-trailing-index rule are all **unchanged** — only the deployment topology is
   amended:

   - The fan-out is folded INTO the existing `memory_extraction` flow (the single
     `memory-extraction-etl` deployment) via a new `num_shards: int = 1` parameter,
     using **recursive self-dispatch**. There is exactly ONE extraction deployment.
   - `num_shards <= 1` (the default) → the flow runs TODAY'S extraction logic
     directly, unchanged — the "worker" path. It does NOT self-dispatch and does NOT
     trigger indexing (exactly the prior `memory_extraction` behaviour). `num_shards=1`
     is therefore byte-for-byte equivalent to extraction before this feature.
   - `num_shards > 1` → the flow takes the "orchestrator" path: resolve the user's
     pending documents (when `document_ids is None`), partition into
     `min(num_shards, N)` contiguous balanced shards, then `run_deployment(
     "memory-extraction-etl/memory-extraction-etl", parameters={"user_id": …,
     "document_ids": shard, "num_shards": 1})` once per shard under
     `asyncio.gather(return_exceptions=True)`, and finally ONE trailing
     `run_deployment("memory-indexing-etl/memory-indexing-etl", parameters={"user_id":
     …})`.
   - Children are ALWAYS dispatched with `num_shards=1`, so each child takes the
     worker path — recursion terminates after exactly one level (no infinite
     self-dispatch).
   - The trailing single index runs ONLY on the orchestrator path (`num_shards > 1`).
     The worker path (`num_shards <= 1`) is pure extraction with no indexing trigger,
     matching the prior `memory_extraction` contract.

   The reusable pure helpers (`_partition_into_shards`, pending-doc resolution,
   `_resolve_num_shards` with its non-positive→1 clamp, the `FanOutStats`-shaped
   report) are RETAINED — they move so `memory_extraction` consumes them — but the
   standalone parent FLOW `memory_extraction_sharded` and its
   `memory-extraction-fanout-etl` deployment registration are DELETED. There is no
   second extraction entrypoint. This is a topology refinement of the same
   document-shard fan-out decision (axis, partitioning, failure-isolation,
   single-trailing-index all unchanged), so the ADR stays `Accepted` rather than being
   superseded; #056's separate-entrypoint design is superseded by this in-flow
   `num_shards` design.

4. **Admission control is `serve(global_limit=concurrency.runner_global_limit)`**
   kept close to `voyage_rpm` so we don't admit far more runs than the embed
   budget can feed.

5. **YAML is the single source of truth; the server limit is derived from it.**
   A `make memory-sync-concurrency-limits` target creates/updates the
   `voyage-embeddings` GCL from `app_config.concurrency`. Raising the cap after a
   payment method is added is: edit `voyage_rpm`, re-run the target — no code change.

## Consequences

- **Accepted tradeoff — same-user write interleaving.** All 4 shards upsert into
  the one `knowledge_graph` collection. Node/edge `_id`s are deterministic, so
  upserts collapse correctly (idempotent on identity, last-writer-wins on mutable
  props). The soft cost: each shard's dedup `$vectorSearch` reads a partially
  written graph, so two shards can create variant nodes for the same real-world
  entity. These are reconciled by the nightly dream-consolidation pass (#051/#052)
  — accepted, not serialized.
- **Lease-leak risk:** `rate_limit` leases default to 300s; a SIGKILL'd run can
  hold a slot up to 5 min under a 3-slot limit. We prefer `rate_limit`
  (fire-and-decay) over `concurrency` (held context) and keep the wrap tight.
- **`strict=False` masks misconfiguration:** a missing limit silently disables
  throttling. Mitigated by making `sync-concurrency-limits` part of the documented
  serve workflow and asserting limit existence in the throttling integration test.
- **Honest expectation:** with 3 RPM shared across 4 runs, embedding is the
  throughput floor. Parallelism speeds CPU/DB phases and keeps runs legal; it does
  not make embedding faster until the cap is lifted.
