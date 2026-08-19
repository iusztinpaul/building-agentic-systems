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

§1–§5 below cover the **cross-flow** half (rate-limiter, fan-out topology,
admission control). §6 documents the orthogonal **intra-run** concurrency
shipped in #057/#058/#059 — bounded fan-out and write-batching *inside* a single
extraction run, which overlap the same non-embedding phases without touching the
shared Voyage budget.

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
   and the coordinator deployment (`memory-extraction-fanout-etl`) are otherwise the
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
   - `num_shards > 1` → the flow takes the "coordinator" path: resolve the user's
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
   - The trailing single index runs ONLY on the coordinator path (`num_shards > 1`).
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

   **Amendment (#066 — coordinator/worker two-deployment topology for both
   pipelines).** The fan-out is now realized as TWO SEPARATE NAMED deployments per
   pipeline — an `…-coordinator` (the operator entrypoint) and an `…-worker` (the
   coordinator's internal `run_deployment` dispatch target) — REPLACING #061's
   single-deployment + recursive-self-dispatch design. #061's in-flow `num_shards`
   recursive self-dispatch (one `memory-extraction-etl` deployment that re-dispatches
   to itself with `num_shards=1`) is hereby **SUPERSEDED** by this two-deployment
   topology.

   - **Why.** The owner wants the coordinator-vs-worker boundary to be explicit and
     visible in the Prefect UI: the parent run shows as `…-coordinator` and its
     children as `…-worker`, instead of every node being the same recursively
     self-dispatched `memory-extraction-etl` run. This is **NOT** the #061 "two
     operator entrypoints" problem that was rejected in the #061 amendment —
     operators still run exactly ONE entrypoint per pipeline (the coordinator). The
     worker is purely the coordinator's internal dispatch target, never a second
     operator command.

   - **Memory topology (target).**
     - Worker flow `memory-extract-etl-worker` `(user_id, document_ids=None)` — the
       actual six-task extraction body. NO `num_shards`, NO coordinator branch, NO
       indexing trigger. Registered as deployment `memory-extract-etl-worker`.
     - Coordinator flow `memory-extract-etl-coordinator`
       `(user_id, document_ids=None, num_shards=1)` — resolve pending docs (when
       `document_ids is None`), partition into `min(num_shards, N)` balanced shards,
       dispatch ONE `memory-extract-etl-worker` run per shard via `run_deployment`
       under `asyncio.gather(return_exceptions=True)`, then ONE trailing
       `memory-indexing-etl` run. Dispatches to the WORKER deployment — NO recursion.
       Registered as deployment `memory-extract-etl-coordinator`.
     - `memory-indexing-etl` is UNCHANGED (name + behavior); the coordinator still
       triggers it exactly once after the gather settles.

   - **Data topology (target).**
     - Worker flow `data-etl-worker` `(user_id, sources: list[...])` — ingest a SUBSET
       (shard) of the configured sources, reusing the existing per-source-type batch
       logic. Registered as deployment `data-etl-worker`.
     - Coordinator flow `data-etl-coordinator` `(user_id, num_shards=1)` — read the
       configured `sources:` list, partition into N balanced shards, dispatch one
       `data-etl-worker` per shard via `run_deployment` under
       `asyncio.gather(return_exceptions=True)`. NO trailing step (the data pipeline
       only produces `documents`; there is no index). Registered as deployment
       `data-etl-coordinator`.

   - **`num_shards=1` semantics CHANGE.** On the memory coordinator, `num_shards=1`
     now dispatches 1 worker run + 1 index run — it is NO LONGER a byte-identical
     in-process "plain" extraction run as it was under #061. A bare extraction (no
     index) is available by triggering `memory-extract-etl-worker` directly. This is
     an accepted consequence of making the worker a real, separately named deployment.

   - **What is UNCHANGED (so Status stays `Accepted`, not `Superseded`).** The
     document-shard axis (memory), balanced contiguous partitioning, the
     `asyncio.gather(return_exceptions=True)` failure-isolation, the
     single-trailing-index rule (memory only), the cross-flow `voyage-embeddings` GCL
     (§1), and the `serve(limit=runner_global_limit)` admission control (§4) are all
     unchanged. The data fan-out axis shifts from #056's in-process per-type to a
     source-shard fan-out across worker deployments, but the partitioning math and
     failure-isolation are the same primitive. This is therefore a topology
     refinement of the same fan-out decision, not a new decision.

   - **Shared partitioning helper.** The pure partitioning math
     (`_partition_into_shards`, generic over the element type, and
     `_resolve_num_shards` with its non-positive→1 clamp) is relocated to the neutral
     `tree.sharding` module (#066) so BOTH coordinators import the IDENTICAL helpers
     with no copy-paste. The memory-specific helpers (`_resolve_pending_document_ids`,
     `_fan_out_extraction`, `FanOutStats`) stay in `tree.memory.extraction.sharding`.

   - **Ops note — stale deployments.** After #067/#068 rename the deployments, the
     server-side definitions for the old names become orphaned and must be deleted
     with `prefect deployment delete <name>` on each environment:
     - `prefect deployment delete memory-extraction-etl`
     - `prefect deployment delete data-pipeline-etl`
     - `prefect deployment delete memory-extraction-fanout-etl` (the long-gone #056
       fan-out deployment).

   This amendment is DOC-ONLY: #066 changes zero deployment topology. The actual
   coordinator/worker split lands in #067 (memory) and #068 (data); the current
   `memory-extraction-etl` deployment and `memory_extraction(num_shards)` flow remain
   functional and behavior-identical until then.

   **Amendment (#070–#074 — platform-grouped data fan-out + HuggingFace
   offset-windowing).** The data fan-out axis (§3 / amendment #066's "Data
   topology") is refined again: the `data-etl-coordinator` STOPS partitioning the
   configured `sources:` list by COUNT (`_partition_into_shards(sources, num_shards)`
   → mixed-variant shards) and instead **groups sources by platform**, with a
   **HuggingFace offset-window sub-fan-out**. The dispatch core is unchanged
   (`asyncio.gather(return_exceptions=True)` over `data-etl-worker` runs, no trailing
   index), so Status stays `Accepted`. Context:
   `tracker/feature-data-platform-sharding-hf-windows-plan.md`.

   - **Motivation.** Count-based balancing is skewed: one `HuggingFaceDatasetSource`
     (millions of rows) was weighed as "1 item" against a single URL, so a count
     partition could drop the entire arXiv dataset into one worker while others split
     a handful of URLs. Parallelism is now declared per-source, not via a global
     shard count.

   - **Group-by-platform (non-HuggingFace).** One `data-etl-worker` run per platform
     bucket present in config, each a HOMOGENEOUS single-platform shard. Platform map:
     `{SubstackRssSource, SubstackArticleSource} → substack`;
     `{YouTubeRssSource, YouTubeVideoSource} → youtube`; `WebSource → custom`;
     `HuggingFaceDatasetSource → huggingface`. The worker's existing `_ingest_sources`
     `isinstance` routing still batches per VARIANT inside the homogeneous shard, so
     the worker body is unchanged.

   - **HuggingFace offset-window sub-fan-out.** Each `HuggingFaceDatasetSource` fans
     out into `num_workers` `data-etl-worker` runs, one per disjoint offset-window:
     `window_size = max_samples // num_workers`; window `i` =
     `(offset = i*window_size, max_samples = window_size)`, with the LAST window taking
     the remainder so windows tile `[0, max_samples)` exactly. Realized via
     `IterableDataset.skip(offset)` before the streaming loop. `num_workers=1` leaves
     `offset` unset → byte-identical to the prior single HF run. New fields on
     `HuggingFaceDatasetSource`: `num_workers: int = 1` (YAML-authored) and
     `offset: int | None = None` (a dispatch-time runtime coordinate set ONLY via
     `entry.model_copy(update={"offset": …})`, never in YAML). Caveat: `skip(n)` is
     O(n) on a streaming dataset but bounded by the `max_samples` cap — this windows
     CAPPED runs only; `split_dataset_by_node` is the documented (out-of-scope)
     successor for a future uncapped whole-dataset run.

   - **`num_shards` dropped for DATA only.** The `data_etl_coordinator` `num_shards`
     parameter, the `--num-shards` script flag, and the Makefile `NUM_SHARDS` thread
     are removed. The MEMORY coordinator keeps `num_shards` unchanged. The shared
     `tree.sharding._partition_into_shards` / `_resolve_num_shards` helpers are NOT
     deleted — the memory document-shard axis still uses them; the data coordinator
     simply stops importing them.

   - **`runner_global_limit` raised 4 → 6** in `apps/memory/configs/default.yaml`
     (§4 admission control). Data workers are NOT Voyage-bound and the
     `voyage-embeddings` GCL (§1) still throttles every real Voyage POST, so admitting
     more concurrent runs cannot exceed the embed budget; the bump accommodates the
     wider data fan-out (platform buckets + HF windows) queuing through the shared
     `serve(limit=…)` slots. The typed `ConcurrencyConfig.runner_global_limit` default
     stays `4` (the YAML is authoritative), and the frozen test fixture
     `frozen_config.yaml` stays at `4` by design (config tests assert against the
     fixture, not `default.yaml`).

   - **Unchanged invariants (so Status stays `Accepted`).** Exactly two deployments
     per pipeline; **depth-1 dispatch with NO recursion** — a worker never calls
     `run_deployment` (recursion can deadlock the serve admission limit);
     `asyncio.gather(return_exceptions=True)` per-shard failure-isolation; NO
     trailing/index run for the data pipeline; the cross-flow `voyage-embeddings` GCL
     (§1) and `serve(limit=runner_global_limit)` admission control (§4); and
     idempotency via `load_document`'s `(user_id, source_uri)` dedup (deterministic
     `arxiv_id → source_uri`), which makes disjoint windows — and any accidental
     overlap — safe to re-run (upsert, never double-insert).

   **Amendment (`free-tier-deployments` #098–#102 — end-to-end pipelines replace the
   coordinator DEPLOYMENTS).** Prefect Cloud's free tier caps a workspace at 5
   deployments. The previous core 5 spent two slots on the coordinators while gating
   `online-pipeline`/`offline-pipeline` behind `deploy_optional` (default false) — so on
   prod the async dispatchers ALWAYS fell through to the in-process fallbacks that this
   amendment goes on to delete. The
   deployment budget is re-spent:

   - **Coordinators remain FLOWS, cease to be DEPLOYMENTS.** `data_etl_coordinator` and
     `memory_extract_etl_coordinator` are no longer registered; they execute exclusively
     as inline subflows of `offline_pipeline` (which `tree/offline.py` already did).
     Their internal `run_deployment` fan-out to `data-etl-worker` /
     `memory-extract-etl-worker` and the single trailing `memory-indexing-etl` run are
     unchanged — all three targets stay registered.
   - **New core 5:** `data-etl-worker`, `memory-extract-etl-worker`,
     `memory-indexing-etl`, `online-pipeline`, `offline-pipeline`.
     `dream-consolidation-all-users` stays the ONLY `optional=True` spec, still gated by
     `prefect.deploy_optional`.
   - **The nightly cron moves** from `data-etl-coordinator` to `offline-pipeline`,
     keeping `0 3 * * *` UTC and `schedule_parameters={"source_files":
     ["sources/listen.yaml"]}`. Widened semantics, deliberate: the scheduled run now
     ingests the listen feeds AND extracts AND indexes across all active users — closing
     the gap where nightly documents sat PENDING until a manual memory run.
   - **`offline_pipeline` gains phase flags** `run_data: bool = True` /
     `run_extraction: bool = True` (mirroring `online_pipeline`'s `run_extraction`) and
     `document_ids: list[str] | None = None` (forwarded to each user's extraction
     coordinator; guarded single-tenant — `document_ids` without `user_id` raises). Both
     flags false is a logged no-op. The step CLIs (`run_data_pipeline` →
     `run_extraction=False`, `run_memory_pipeline` → `run_data=False`) funnel through
     `dispatch_offline_pipeline` like `run_pipeline` already did;
     `tree.cli.trigger_deployment` is RETAINED for the standalone indexing script (its
     one remaining caller).
   - **Accepted consequence — MCP ingest goes async on prod.** With `online-pipeline`
     registered, `ingest_url`/`ingest_file`/`ingest_conversation` return
     `{"status": <the new run's Prefect state, normally `scheduled`>, "flow_run_id": …}`
     instead of blocking in-process for a `document_id`. Intended, not a bug.
   - **The in-process fallbacks are DELETED, not merely bypassed.** Both dispatchers
     previously wrapped `run_deployment` in a bare `except Exception` and re-ran the
     SAME flow inline. That branch existed only because the pipelines were optional, and
     its "unreachable API" half never worked — the inline call is itself a `@flow`, so it
     died with `RuntimeError: Failed to reach API` after swallowing the original error,
     while the broad catch masked parameter-validation and auth failures by silently
     turning a fast submit into a long blocking run. Dispatch now REQUIRES a reachable
     Prefect API with the deployment registered; failures propagate. The vestigial
     `mode` key is gone from both return dicts (there is only one path), and `status` is
     derived from the created run's Prefect state via `tree.flow_runs.flow_run_status`
     rather than hardcoded — Prefect has no `submitted` state.
   - **Group overlap accepted.** `online-pipeline`/`offline-pipeline` carry both
     pipeline-identity tags, so they belong to BOTH `DEPLOYMENT_GROUPS` — a
     `down --groups <either>` deletes them; the next `up` restores them. The groups
     mechanism is unchanged.
   - **Ops note — stale deployments.** Per environment, delete the orphaned server-side
     definitions:
     - `prefect deployment delete data-etl-coordinator/data-etl-coordinator`
     - `prefect deployment delete memory-extract-etl-coordinator/memory-extract-etl-coordinator`

   **Unchanged invariants (so Status stays `Accepted`).** Depth-1 dispatch with NO
   recursion (a worker never calls `run_deployment`); per-shard/per-user
   `asyncio.gather`/try-isolated failure handling; the single-trailing-index rule (memory
   only; the e2e run still costs ONE admission slot, now held by the `offline-pipeline`
   run hosting the coordinator subflows); the cross-flow `voyage-embeddings` GCL (§1);
   `serve(limit=runner_global_limit)` admission control (§4); and the 5-deployment
   free-tier envelope itself — this amendment changes WHICH five, not how many.


   **Amendment (#078–#082 — batch-grain ETL task topology for the leaf pipelines).**
   The §3 fan-out so far governs the FLOW-level topology (coordinator → worker →
   per-source batch flow). This amendment refines the TASK grain *inside* a worker: each
   leaf batch ETL flow's Prefect `@task`s now run at **Batch** grain reflecting the ETL
   phases, NOT once per row. The flow-level topology, the fan-out axis, the deployment
   count, the gather-failure-isolation primitive, the GCL, and admission control are all
   unchanged — so Status stays `Accepted`; this is a finer-grained expression of the same
   topology decision, not a new decision and not a supersession. Context:
   `tracker/feature-batch-etl-task-topology-plan.md`.

   - **Task granularity = Batch, not Document.** Each leaf flow runs ETL-phase `@task`s
     over a **Batch** (HuggingFace `batch_size`-chunk / one feed's entries / the whole
     handed-in URL list), never per row. The per-row `extract_document` /
     `fetch_paper_content` / `load_document` `@task` calls (and the per-doc
     `_process_document`) are GONE. Concrete payoff: a 1000-doc arXiv **Window** worker
     drops from ~1000 task runs (`extract-arxiv-document` + `load-arxiv-document` per row,
     × `fetch_content`) to a few TENS — the explosion in the owner's Prefect screenshot.

   - **Pragmatic E/T/L (Load is ALWAYS its own task).** ETL vocabulary at batch grain;
     the task COUNT follows genuinely-separable phases:
     - **Load** is ALWAYS a separate `load_batch` task (`load-{source}-batch`, `retries=1`).
     - **Extract+Transform FUSE** into one `extract_batch` (`retries=2`) where a single
       scrape yields the Document: web (`extract-web-batch`), substack-article
       (`extract-substack-article-batch`), youtube-video metadata path.
     - A separate `transform_batch` (`retries=0`) exists ONLY where transform is a genuine
       pure map: arXiv dict→Document (`transform-arxiv-batch`) and substack-RSS
       feed-entry→Document (`transform-substack-rss-batch`). This asymmetry is intentional
       (the scrape pipelines have nothing to map; arXiv/substack-RSS do).
     - An optional network `enrich_batch` (`enrich-arxiv-batch`, `retries=2`) runs ONLY
       when arXiv `fetch_content` is set (paper-content fetch).
     - The streamed READ (arXiv `fetch_dataset_batches`) stays the FLOW LOOP, not a task —
       it's a generator the flow iterates per chunk.

   - **Per-item sub-flow collapse + thin MCP flow.** The three direct-link pipelines
     demote their bodies to plain async core functions `_ingest_web_url_one` /
     `_ingest_substack_article_one` / `_ingest_youtube_video_one`. The BATCH path calls
     the core directly per element — NO per-item sub-flow run. A 1-line `@flow` wrapper
     (`ingest_web_url` / `ingest_substack_article` / `ingest_youtube_video`, the
     `ingest-{x}-etl` flows) is RETAINED solely for the MCP `ingest_url` **URL router**'s
     single-URL path, so MCP single-URL ingest still gets its own Prefect flow run + Opik
     trace. So under a batch worker there are no `ingest-web-url-etl` /
     `ingest-substack-article-etl` / `ingest-youtube-video-etl` child runs.

   - **Per-element isolation inside the task + the idempotency invariant.** Per-element
     isolation lives INSIDE each batch task via the shared
     `tree.data.batch.gather_isolated` helper (one `asyncio.gather(return_exceptions=True)`
     — introduced once #079 crossed the 4+ call-site DRY threshold #078 named; #078 had
     inlined it). A bad-data element is logged at WARNING + SKIPPED; the task returns the
     successful subset + a per-batch failure COUNT, never sinking the batch on one element.
     The task hard-fails ONLY on a batch-WIDE infra failure (raised outside the gather) →
     Prefect retries the WHOLE batch, which is SAFE because every data-layer load dedups on
     `(user_id, source_uri)` (upsert, never double-insert).

   - **Retry relocation.** Batch-task retries gate batch-WIDE infra: fetch/extract
     `retries=2`, load `retries=1`, pure transform `retries=0`. Per-element transient FETCH
     retries live in the FETCH LAYER (existing httpx retry behavior / the transcript
     chain's per-slot fallback), not the batch task — so collapsing per-row tasks does NOT
     regress network-fetch retry behavior.
     **Counts superseded by amendment #096** (F/B/D tiers): free-replay units are `3 / 5 s`,
     billable ones stay capped at `2 / 5 s`, pure transforms stay `0`. The RELOCATION
     principle here — retries gate batch-WIDE infra, per-element transients belong to the
     fetch layer — is unchanged.

   - **RSS keeps feed-obtain + shares only the build/load tail (no re-fetch).**
     - **substack-RSS** builds from feed-EMBEDDED content (`fetch_feed_task` →
       `transform_batch` over feed entries → `load_batch`): 1 feed fetch → N docs, NO
       per-article re-scrape. It shares ONLY the LOAD with substack-article (which keeps
       its own scrape `extract_batch`); RSS is NOT unified onto the article scrape path.
     - **youtube-RSS + youtube-video** share the bulk-transcript-fetch + `build_batch` +
       `load_batch` tail (the `_batch_build_and_load` core in `youtube_pipeline.py`): ONE bulk
       `fetcher.fetch_many(all_urls)` per feed/batch (the #080 fix — previously per-video
       `fetch_many([url])` inside per-URL sub-flows). The metadata SOURCE stays distinct:
       oEmbed per video (direct) vs `feed_entry_to_metadata` (RSS).

   - **Result persistence stays off.** Prefect-3 result persistence is OFF by default (the
     repo sets no `persist_result` / `result_storage` / `cache_policy` /
     `PREFECT_RESULTS_PERSIST_BY_DEFAULT`), so the side-effecting load/extract tasks persist
     no results — and none was added (no cache policy is introduced).

   - **Unchanged invariants (so Status stays `Accepted`).** The flow-level topology
     (coordinator → worker → per-source batch flow), the two-deployments-per-pipeline +
     depth-1/no-recursion dispatch (§3 amendments #066/#070–#074), the group-by-platform
     data fan-out + HF offset-windowing (§3 amendment #070–#074), the
     `asyncio.gather(return_exceptions=True)` failure-isolation, NO trailing index for the
     data pipeline, the `voyage-embeddings` GCL (§1) + `serve(limit=runner_global_limit)`
     admission control (§4), and the batch-flow Opik trace structure (per-batch-PHASE spans
     via the existing `span(...)`, NOT per-doc; trace-header forwarding preserved) are ALL
     unchanged. The observability "win" is in Prefect TASK runs only — the batch flows
     already owned ONE Opik trace with no per-doc spans, so Opik structure does not change.

   **Amendment (#096 — retry placement + retry budget).** The #078–#082 amendment fixed
   the task GRAIN (Batch, not Document) and set retry counts per phase. It did not say
   WHERE a retry belongs when a flow has no tasks at all, nor HOW a count is chosen — so
   the leaf pipelines drifted: the thin single-item MCP flows carried no retry anywhere,
   and the substack batch lost the `extract-substack-article-batch` / `load-substack-batch`
   tasks this amendment's §"Pragmatic E/T/L" names, leaving its article scrape + load with
   ZERO retries while byte-identical web operations had two. This amendment makes the
   placement rule and the count rule explicit and normalizes every leaf pipeline onto them.
   Flow-level topology, deployment count, fan-out axis, GCL, and admission control are all
   unchanged — so Status stays `Accepted`; this is a finer-grained expression of the same
   topology decision, not a new decision and not a supersession.

   - **The placement rule.** Put the retry on the SMALLEST unit that (a) contains the
     failure, (b) is idempotent, and (c) is cheap to replay — then NEVER stack two levels.
     Applied as an ordered decision procedure:

     1. **Dispatcher flow** (body is a `run_deployment` fan-out) → NO retries at any level.
        A replay re-dispatches every shard; isolation is the per-shard gather.
        (`data-etl-coordinator`, `data-etl-worker`.)
     2. **Batch flow** (processes N items) → retries on batch-grain `@task`s, one per ETL
        phase; the FLOW carries none.
     3. **Single-item flow** → three sub-cases:
        - **3a. Body is a 1-line call to a plain async core**, steps cheap and symmetric →
          `@flow(retries=…)`, NO tasks. Two cheap steps (one HTTP GET ≈ 200 ms, one Mongo
          write ≈ 10 ms) do not justify two task objects and two Prefect state round-trips
          on an interactive MCP path. (`ingest-substack-article-etl`, `ingest-web-url-etl`.)
        - **3b. Body owns an Opik trace** (`configure_opik()` / `span(…)` / `flush_opik()`)
          → retries on the TASK. A flow retry re-runs the body, emitting ONE TRACE PER
          ATTEMPT and breaking the documented "owns ONE trace" contract.
          (`ingest-file-etl`, `ingest-conversation-etl`.)
        - **3c. Core delegates to shared batch tasks** → add NOTHING; those tasks already
          retry. (`ingest-youtube-video-etl` → `_batch_build_and_load`.)
     4. **Override — billable or asymmetric steps.** A step that is billable, or ≥10× the
        next step's cost, gets its OWN task so a later cheap failure never replays it. If
        even ONE replay is unacceptable, ABSORB the failure inside the task and return a
        partial result rather than raising (`fetch-youtube-transcripts-batch`, #095).
     5. **Never stack.** A flow whose tasks retry must not set `retries` itself — attempts
        MULTIPLY (flow 2 × task 3 = up to 12 executions).

   - **The count rule: `retries × retry_delay_seconds` = the transient window the unit must
     outlast.** Three tiers, chosen from what actually fails here:

     | Tier | Criterion | Budget |
     | --- | --- | --- |
     | **F — free replay** | plain HTTP read, or an idempotent Mongo write | **3 × 5 s = 15 s** |
     | **B — billable replay** | every attempt costs money | **2 × 5 s = 10 s, HARD CAP** |
     | **D — deterministic** | pure function, no I/O | **0** |

     15 s is sized to outlast a MongoDB primary election (~10–30 s typical) and a transient
     429/5xx — the two failures the data layer actually sees. Tier B is capped because past
     that point the budget stops being time and starts being invoice. Tier D is `0` because
     a pure map that raises on a bad row raises identically on attempt 2: retrying it buys
     nothing and delays the real error by 10 s. Every Tier-B unit carries an inline
     `# billable — capped at 2` comment so the cap is not raised without seeing the reason.

   - **Tier assignment (authoritative; the code is normalized onto this).**
     - **F, 3 / 5 s:** `fetch-substack-rss-feed`, `fetch-youtube-rss-feed`,
       `enrich-arxiv-batch`, `extract-substack-article-batch`, `load-substack-batch`,
       `load-web-batch`, `load-youtube-batch`, `load-arxiv-batch`, `load-file-document`,
       `load-conversation-document`, and the `ingest-substack-article-etl` FLOW (substack
       articles are fetched with plain `httpx` — replay is free).
     - **B, 2 / 5 s capped:** `extract-web-batch` and the `ingest-web-url-etl` FLOW (both
       scrape via **Bright Data Web Unlocker**, billable per request — one batch replay
       re-bills ALL N urls), and `fetch-youtube-transcripts-batch` (~173 s per collection
       plus per-record billing; 5 retries ≈ 15 min and 5 paid collections).
     - **D, 0:** `transform-arxiv-batch`, `build-youtube-batch`.

   - **Substack batch regains its ETL tasks.** `ingest-substack-batch-etl` wraps its
     existing `gather_isolated` calls in `extract-substack-article-batch` (`retries=3`) and
     `load-substack-batch` (`retries=3`), restoring the "Load is ALWAYS its own task" rule
     above and parity with web. The FLATTEN logic is unchanged — the tasks wrap the gathers,
     they do not re-introduce per-row tasks, and the flow stays at a small constant number
     of task runs regardless of shard size.

   - **Accepted exception (documented in code).** `arxiv`'s Extract
     (`_fetch_dataset_batches`) is a streamed generator the flow iterates, so it CANNOT be a
     task; it is the one unretried network read in the data layer. Retrying it would mean
     re-streaming from row 0. `ingest_arxiv_dataset`'s docstring names this.

   **Amendment (#097 — task-worthiness + task naming).** #096 fixed WHERE a retry lives
   and HOW its count is chosen, but left every step a task by default — even Tier D,
   where a pure map carried `@task(retries=0)`. This amendment inverts the default: a
   step is a PLAIN FUNCTION unless task-hood buys something concrete. A `@task` with no
   retries, no cache, and no persisted result still costs a task-run record, parameter
   serialization of its inputs through the Prefect engine, and one more box in the UI —
   pure overhead. Good: `build_batch` as a plain map called between two tasks. Bad:
   `@task(retries=0)` on a pure map — attempt 2 raises identically, and the task run
   bought nothing.

   - **The task-worthiness rule.** Annotate a step `@task` ONLY when at least one holds:
     1. **Billable or rate-limited external call** — it needs its own retry domain so a
        cheap downstream failure never replays it (`fetch-youtube-transcripts-batch`,
        `extract-web-batch`).
     2. **Long or expensive compute** worth isolating/caching (`enrich-arxiv-batch`).
     3. **Guard boundary for 1/2** — a cheap step whose task boundary protects an
        expensive sibling from replay: the `load-*-batch` tasks exist so a Mongo blip
        retries the load ALONE instead of re-running (and re-billing) the scrape above.
     4. **The flow cannot retry** (dispatcher, billable replay, streamed Extract) and
        the step is a network hop that still needs durability (`resolve-youtube-video`).

     Everything else is a plain function, and a pipeline with NO tasks at all is fine —
     its durability lives on the flow (`@flow(retries=…)`).

   - **Deltas applied.**
     - **Demoted to plain functions** (were Tier D tasks): `transform-arxiv-batch`,
       `build-youtube-batch`. Tier D now means "not a task at all", not "a task with
       `retries=0`".
     - **`ingest-file-etl` / `ingest-conversation-etl` move from rule 3b to the 3a
       treatment**: `@flow(retries=3, retry_delay_seconds=5)`, no tasks. The body is ONE
       idempotent Mongo write, so replay is free. Accepted cost, superseding 3b: a flow
       retry emits one Opik trace per attempt — a trace per REAL retry is signal, and
       the web/substack thin flows already behave this way. Rule 3b is retired.
     - **Promoted:** `resolve-youtube-video` (Tier F, 3 / 5 s) wraps the single-video
       oEmbed resolve — the one network hop before the billable core, inside a flow
       that must not retry (rule 3c). The BATCH path keeps calling the plain
       `_resolve_video_item` under `gather_isolated`, so its task-run count stays a
       small constant per shard.

   - **Task naming.** Task functions never carry a `_task` suffix — the decorator
     already says it, and the suffix leaks Prefect plumbing into every call site.
     Good: `fetch_rss_feed`, `load_batch`, `resolve_video`. Bad: `fetch_feed_task`,
     `load_file_document_task`. When a task wraps a same-named core function imported
     into the module, name the TASK for what it does at pipeline grain (core
     `fetch_feed` → task `fetch_rss_feed`) instead of suffixing. Batch-processing
     helpers say `batch`, not `bulk` (`_batch_build_and_load`) — one word for one
     concept.

4. **Admission control is `serve(global_limit=concurrency.runner_global_limit)`**
   kept close to `voyage_rpm` so we don't admit far more runs than the embed
   budget can feed.

5. **YAML is the single source of truth; the server limit is derived from it.**
   A `make memory-sync-concurrency-limits` target creates/updates the
   `voyage-embeddings` GCL from `app_config.concurrency`. Raising the cap after a
   payment method is added is: edit `voyage_rpm`, re-run the target — no code change.

## §6 — Intra-run concurrency

§1–§5 govern how runs relate to each other. This section documents the concurrency
shipped *within* a single `memory_extraction` run (#057/#058/#059). All three changes
preserve their stages' outputs exactly; they only overlap round-trips or batch them.
None of them touch the `voyage-embeddings` budget — the embedding stage (④) is
unchanged.

1. **`doc_concurrency` (default `1`) bounds the chunking stage.** The
   `extract-chunks-and-structural` task (① in `extraction/pipeline.py`) is fanned out
   over the run's documents under an `asyncio.Semaphore(doc_concurrency)` +
   `asyncio.gather` in `_chunk_documents`. The stage is purely CPU/DB-bound — no shared
   LLM/embed quota, no read-after-write — so the per-document calls parallelize safely.
   The default of `1` is serial = the prior behavior exactly. `gather` preserves input
   order, so the returned `chunked_docs` list is identical (order and contents) to the
   prior sequential loop and downstream per-document iteration stays deterministic.

2. **`dedup_concurrency` (default `8`) parallelizes the dedup stage.** The per-entity
   dedup decisions in `_dedupe_entities` (⑤) run concurrently under an
   `asyncio.Semaphore(dedup_concurrency)` + `asyncio.gather`. This is safe because
   `dedupe_entity` (`extraction/dedup.py`) is a READ-ONLY `$vectorSearch` over the
   PRECOMPUTED vectors from stage ④ — it issues no Voyage embed call, performs no writes,
   and is independent per entity. Results are rebuilt in the original key order, so the
   `decisions` mapping and the `n_merged` / `n_flagged` / `n_none` tallies are identical
   to the sequential version. This independence from Voyage is precisely why this stage
   parallelizes freely under the cross-flow limiter: it never acquires a
   `voyage-embeddings` slot (per §1, the slot is held only at the real Voyage network
   POST, which dedup never reaches).

3. **`bulk_write` batching (#057).** The two per-item `update_one`-in-a-loop write paths
   in `_apply_writes` (⑥) — the edge upserts and the structural-node upserts — were each
   collapsed into a single `bulk_write(ops, ordered=False)`. This is behavior-preserving:
   the `_id`s are deterministic, the `$set` payloads are identical, and the written counts
   are unchanged; only the Mongo round-trips are batched. It mirrors the existing
   `bulk_write` pattern in `indexing/core.py`.

The defaults (`doc_concurrency: 1`, `dedup_concurrency: 8`) live in
`apps/memory/configs/default.yaml` under `extraction:`, typed on `ExtractionConfig` in
`app_config.py`, and are overridable via `TREE_EXTRACTION__DOC_CONCURRENCY` /
`TREE_EXTRACTION__DEDUP_CONCURRENCY`.

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
