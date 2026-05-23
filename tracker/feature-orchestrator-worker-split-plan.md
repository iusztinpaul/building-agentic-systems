# Feature Plan: Explicit Orchestrator/Worker Deployment Split (Memory + Data Pipelines)

## Summary
Make the orchestrator-vs-worker distinction explicit as SEPARATE named Prefect
deployments for BOTH the memory-extraction and data pipelines. Today each pipeline
is a single deployment that branches internally (memory uses a `num_shards` branch
+ recursive self-dispatch into its own `memory-extraction-etl` deployment; data
fans out over source-types in-process). After this feature, each pipeline is two
deployments — an `…-orchestrator` (the operator entrypoint: resolve/partition work
into N balanced shards, dispatch one worker run per shard under
`asyncio.gather(return_exceptions=True)`, plus — memory only — one trailing index)
and an `…-worker` (the actual unit of work, the orchestrator's internal dispatch
target). Operators run only the orchestrator; there is still ONE operator entrypoint
per pipeline (this is NOT the #061 "two operator entrypoints" problem the owner
rejected — the worker is internal). This SUPERSEDES #061's single-deployment +
recursive-self-dispatch design. Owner-approved re-architecture.

## Tasks (in order)
1. **#066** — ADR-002 §3 amendment + shared shard-partitioning helper —
   author the ADR amendment recording the orchestrator/worker two-deployment
   topology for BOTH pipelines (superseding #061's recursive self-dispatch; Status
   stays `Accepted`, num_shards=1 semantics-change recorded), and generalize the
   reusable shard helper (`_partition_into_shards` / `_resolve_num_shards`) so both
   the memory and data orchestrators consume it. Doc + pure-helper only; no
   deployment topology change yet (memory keeps working unchanged). Lands first so
   #067–#069 reference the settled ADR and the shared helper.
2. **#067** — Memory pipeline orchestrator/worker split — `memory-extract-etl-worker`
   (`(user_id, document_ids=None)`, the six-task extraction body, NO num_shards / NO
   orchestrator branch / NO indexing) + `memory-extract-etl-orchestrator`
   (`(user_id, document_ids=None, num_shards=1)`, resolve→partition→dispatch N
   workers→ONE trailing `memory-indexing-etl`). Replaces the `memory-extraction-etl`
   registration with the two new ones; reworks the memory fan-out unit + integration
   tests; re-points the Make target + `run_memory_pipeline.py` at the orchestrator;
   includes the stale-`memory-extraction-etl` cleanup ops note. `memory-indexing-etl`
   UNCHANGED. Depends on #066.
3. **#068** — Data pipeline orchestrator/worker split — `data-etl-worker`
   (`(user_id, sources: list[SourceEntry])`, ingest one shard of the configured
   sources, reusing the existing per-source-type batch logic) + `data-etl-orchestrator`
   (`(user_id, num_shards=1)`, read the configured `sources:` list, partition into N
   balanced shards, dispatch one worker per shard under
   `gather(return_exceptions=True)`, NO trailing step). Replaces the
   `data-pipeline-etl` registration with the two new ones; reworks the data-pipeline
   unit tests + adds orchestrator/worker tests; re-points the Make target +
   `run_data_pipeline.py` at the orchestrator; includes the stale-`data-pipeline-etl`
   cleanup ops note. No Voyage involvement. Depends on #066 (helper) and #067
   (orchestrator/worker conventions to mirror).
4. **#069** — Cross-cutting serve-registration test rework + full [HUMAN] live e2e —
   rework `tests/unit/test_orchestrator.py` so the registered-deployment-name set
   asserts the FOUR new names (`memory-extract-etl-worker`,
   `memory-extract-etl-orchestrator`, `data-etl-worker`, `data-etl-orchestrator`) and
   the absence of the two retired names, while preserving the #065
   `serve(limit=…)`-not-`global_limit` and dream-cron guards. Then run the full
   acceptance suite and the `[HUMAN]` live e2e for BOTH pipelines (trigger each
   orchestrator with `NUM_SHARDS=2`, confirm in the Prefect UI the parent shows as
   `…-orchestrator` and children as `…-worker`). Depends on #067 and #068.

## Out of scope (intentional)
- **Changing the fan-out axis or partitioning math.** The document-shard axis
  (memory), the new source-shard axis (data), balanced-contiguous partitioning,
  `gather(return_exceptions=True)` failure-isolation, and the single-trailing-index
  rule (memory) are all carried over UNCHANGED from #056/#061 — only the deployment
  topology changes. No new sharding semantics.
- **A YAML default for `num_shards`.** Per the #062 precedent, `num_shards` stays a
  per-run-only knob (no `default.yaml` entry, no `app_config` field). Omitted ⇒ 1.
- **Voyage rate-limiter / admission-control changes.** The `voyage-embeddings` GCL
  (§1), `serve(limit=runner_global_limit)` admission control (§4, the #065 fix), and
  the intra-run concurrency knobs (§6) are untouched. The data pipeline issues no
  Voyage calls, so no limiter participation there.
- **A byte-identical "plain" single extraction run.** Accepted by the owner:
  `num_shards=1` on the memory orchestrator now dispatches 1 worker + 1 index rather
  than running extraction in-process. A bare extraction (no index) is available by
  triggering `memory-extract-etl-worker` directly if ever needed.
- **The MCP `run_extraction_for_documents` shim.** It already runs extraction
  in-process without Prefect deployments; it is unaffected by the deployment split
  and stays as-is.
- **`memory-indexing-etl` and the dream-consolidation deployment.** Names and
  behavior unchanged; the memory orchestrator still triggers indexing once.

## Documentation updates (this grooming round)
- **Glossary:** no glossary in this project (`docs/glossary.md` absent); not
  applicable. No new domain terms introduced — "orchestrator" and "worker" are
  already the vocabulary used across ADR-002 §3 and the existing flow docstrings.
- **ADRs:** ADR-002 (`docs/adrs/002_pipeline_concurrency_and_voyage_rate_limiting.md`)
  §3 is AMENDED (not superseded) by task **#066** to record the orchestrator/worker
  two-deployment topology for BOTH pipelines. The amendment supersedes #061's
  in-flow `num_shards` recursive-self-dispatch design (it documents that #061's
  single-deployment topology is itself now superseded by the two-deployment split),
  records the `num_shards=1` semantics change (no longer a byte-identical plain run),
  and keeps Status `Accepted` (same fan-out decision — axis, partitioning,
  failure-isolation, single-trailing-index all unchanged; only topology refined).
  ADR-001 is unchanged (no contradiction). Per the owner's brief, the ADR amendment
  is the deliverable of task #066 (specced here, authored there), not pre-written in
  the grooming commit.

## Open questions
- None blocking. The owner's decision is confirmed and fully specified (target
  signatures, deployment names, dispatch shape, trailing-index rule per pipeline,
  num_shards=1 semantics, stale-deployment cleanup, test rework targets, and the
  per-pipeline `[HUMAN]` distinct-name UI check are all pinned in the brief).
</content>
</invoke>
