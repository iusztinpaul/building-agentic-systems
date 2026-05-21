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
capped at **3 RPM / 10K TPM** (no payment method). Two call sites touch Voyage:
the batched chokepoint `_embed_chunk_resilient` (`embedding_text.py`) and an
inline dedup embed in `add_entity` that today bypasses the batcher. Running
4 runs in parallel does NOT speed embedding — it stays serialized at 3 RPM. The
win is overlapping the non-embedding phases; the risk is 4 runs collectively
429-storming Voyage.

## Decision

1. **Cross-flow rate limiting uses a Prefect global concurrency limit with
   slot decay**, named `voyage-embeddings` (limit = `concurrency.voyage_rpm`,
   slot-decay-per-second = `voyage_rpm / 60`). It is the only primitive that
   spans separate flow runs (a per-process `asyncio.Semaphore` cannot). Acquired
   via `rate_limit("voyage-embeddings", occupy=1, strict=False)` wrapped tightly
   around the single real POST inside `_embed_chunk_resilient`. `strict=False`
   makes a missing limit a no-op (graceful degradation for unit tests / fresh
   dev boxes). The `add_entity` inline embed is routed through
   `_embed_chunk_resilient` so there is exactly ONE guarded chokepoint.

2. **The TPM cap is held by config, not a second limiter (for now):**
   `models.embedding_batch.max_total_tokens` drops 320000 → 10000 so no single
   request can exceed the 10K TPM window. A token-weighted `voyage-tokens`
   limit is an explicit deferred follow-up, added only if 429s persist.

3. **The fan-out axis is document-shards of one user.** A parent flow
   `memory_extraction_sharded` partitions a user's pending documents into N
   contiguous shards and launches one `memory-extraction-etl` run per shard via
   `run_deployment` under `asyncio.gather(return_exceptions=True)`, then triggers
   ONE `memory-indexing-etl` run after all shards finish (indexing is a global
   backfill over unembedded nodes — running it per-shard would race 4 writers).

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
