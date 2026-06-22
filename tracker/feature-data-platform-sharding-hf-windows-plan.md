# Feature Plan: Platform-grouped data orchestration + HuggingFace dataset windowing

## Summary
The `data-etl-orchestrator` today shards the configured `sources:` list by COUNT
(`_partition_into_shards(sources, num_shards)`) and dispatches one mixed-variant
`data-etl-worker` per shard. That balancing is skewed: a single
`HuggingFaceDatasetSource` entry (millions of rows) is weighed as "1 item" against
a single Substack URL, so a count-based partition can drop the whole arXiv dataset
into one worker while three other workers split a handful of URLs. This feature
replaces count-based partitioning with **group-by-platform** fan-out and adds an
**offset-window sub-fan-out for HuggingFace** so the heavy dataset is split across
`num_workers` window-runs. We STAY at exactly two deployments (`data-etl-orchestrator`
+ `data-etl-worker`) — Prefect free-tier caps us at 5/5 — and add no recursion: every
dispatch happens depth-1 from the orchestrator process.

This is a topology refinement of ADR-002 §3 (the data fan-out axis), absorbed as an
AMENDMENT to ADR-002 (Status stays `Accepted`), exactly as #055/#061/#066 were.

## Locked design (decisions are final — do not relitigate)

1. **Two deployments only.** `data-etl-orchestrator` + `data-etl-worker`, unchanged
   names + registrations. NO new deployment (free-tier cap is 5/5 — see
   `apps/memory/src/tree/orchestrator.py` `_DEPLOYMENT_SPECS`). The multiple "worker"
   runs are RUNS of the single `data-etl-worker` deployment.

2. **Orchestrator: count-based → group-by-platform.** One `data-etl-worker` run per
   platform bucket. Platform map:
   - `SubstackRssSource`, `SubstackArticleSource` → `substack`
   - `YouTubeRssSource`, `YouTubeVideoSource` → `youtube`
   - `WebSource` → `custom`
   - `HuggingFaceDatasetSource` → `huggingface`
   Each non-HF platform present in the configured sources gets ONE worker run with a
   HOMOGENEOUS (single-platform) shard.

3. **HuggingFace fans out into `num_workers` window-runs** (one `data-etl-worker` run
   per window). Window via OFFSET arithmetic:
   `window_size = max_samples // num_workers`; worker `i` gets `offset = i*window_size`
   and `max_samples = window_size`; the LAST worker takes the remainder
   (`max_samples - offset`). This is NOT `split_dataset_by_node` — offset/`skip` is the
   chosen simple approach. (A documented upgrade path to `split_dataset_by_node` for a
   future uncapped whole-dataset run exists but is OUT OF SCOPE here.)

4. **Worker stays essentially unchanged.** It receives a homogeneous single-platform
   shard and the existing `_ingest_sources` `isinstance` routing fires the one matching
   branch. An HF window worker receives `sources=[one HuggingFaceDatasetSource entry
   with offset set]` → HF handler → windowed ingest. The ONLY worker-side change is
   threading the entry's `offset` into `ingest_arxiv_dataset`.

5. **HF payload contract.** `HuggingFaceDatasetSource` (in
   `apps/memory/src/tree/config/app_config.py`) gains:
   - `num_workers: int = 1` — config knob, sits next to `batch_size`/`concurrency`,
     authored in YAML.
   - `offset: int | None = None` — runtime coordinate, set ONLY at dispatch via
     `entry.model_copy(update={"offset": …})`, NEVER in YAML.
   `fetch_dataset_batches(offset, max_samples, batch_size)` in
   `apps/memory/src/tree/data/huggingface/arxiv_dataset.py` gains
   `if offset: ds = ds.skip(offset)` before the existing streaming loop.

6. **All dispatch from the orchestrator process (depth-1, NO recursion).** A worker
   must NEVER call `run_deployment` (that contradicts ADR-002 amendment #066 and can
   deadlock the serve admission limit). All dispatches go under
   `asyncio.gather(return_exceptions=True)` for failure isolation + one uniform Opik
   trace (mirror the existing `_fan_out_data` in `pipeline.py`).

7. **Drop the global `num_shards` knob from the DATA orchestrator** (and its
   `--num-shards`/`NUM_SHARDS` plumbing in `scripts/run_data_pipeline.py` + the Makefile
   `run-data-pipeline` target). Parallelism is now declared per-source: automatic
   platform bucketing + HF `num_workers`. The MEMORY pipeline keeps its own
   `num_shards` — only the DATA orchestrator drops it.

8. **Raise `concurrency.runner_global_limit` 4 → 6** in
   `apps/memory/configs/default.yaml` (local serve admission limit). Data workers are
   NOT Voyage-bound and the `voyage-embeddings` GCL still caps embedding, so
   over-admitting is safe. The frozen test fixture
   `apps/memory/tests/unit/config/fixtures/frozen_config.yaml` is INDEPENDENT and STAYS
   at 4 — the config tests assert against the fixture, not `default.yaml`, so leaving it
   at 4 means zero config-test breakage. Do NOT change the fixture.

9. **Idempotency is already handled.** `load_document` (in `arxiv_dataset.py`) dedups on
   `(user_id, source_uri)`, `arxiv_id → source_uri` is deterministic, and offset windows
   are disjoint — so re-runs/overlap never double-insert. No new idempotency work; just
   note it.

## Caveats (state them — they are NOT blockers)

- **`skip(offset)` is O(offset)** on a streaming `IterableDataset` (it walks and discards
  the first `offset` rows). It is bounded by `max_samples` because this feature only
  windows CAPPED runs (`max_samples` set in YAML), so the cost is bounded and acceptable.
  The future uncapped whole-dataset path would use `split_dataset_by_node` instead.
- **`serve(limit=6)` is shared across data + memory.** So HF `num_workers` is a LOGICAL
  split that queues through the shared admission slots — declaring `num_workers=4`
  doesn't guarantee 4 simultaneous runs, it bounds the fan-out width and the runs queue.
- **One worker per non-HF platform is fine for hundreds of entries** via the existing
  in-worker async batching (the per-variant batch sub-flows + `asyncio.Semaphore(concurrency)`
  in the HF path). The platform bucket is homogeneous, so the worker's existing
  single-branch dispatch handles the whole bucket in one batched call.

## Tasks (in order)

1. **#070** — HF source fields + serve admission bump — add `num_workers: int = 1` and
   `offset: int | None = None` to `HuggingFaceDatasetSource`; raise
   `concurrency.runner_global_limit` 4 → 6 in `default.yaml` (fixture stays 4). Pure
   config; no fan-out wiring yet. Lands first so #071–#072 can reference the new fields.
   (file: `tracker/070-hf-source-window-fields-and-serve-limit.groomed.md`)
2. **#071** — Offset-aware arXiv windowed ingest — thread `offset` through
   `fetch_dataset_batches(offset, max_samples, batch_size)` (`if offset: ds = ds.skip(offset)`),
   `ingest_arxiv_dataset(..., offset=None)`, and `_ingest_arxiv_dataset_entry`
   (passes `entry.offset`). The leaf windowing; the worker now ingests exactly its
   window. Depends on #070. (file:
   `tracker/071-offset-aware-arxiv-windowed-ingest.groomed.md`)
3. **#072** — Group-by-platform orchestrator + HF offset-window fan-out — replace the
   orchestrator's count-based `_partition_into_shards(sources, num_shards)` with a
   group-by-platform partition (one homogeneous worker run per non-HF platform bucket)
   plus an HF sub-fan-out that emits `num_workers` window-runs via
   `entry.model_copy(update={"offset": …})`; drop the `num_shards` param from
   `data_etl_orchestrator`. Reuse `_fan_out_data` (gather + failure-isolation +
   trace-header forwarding) unchanged. Rework the orchestrator/fan-out unit tests for the
   new partition axis. Depends on #070, #071. (file:
   `tracker/072-group-by-platform-orchestrator-and-hf-window-fanout.groomed.md`)
4. **#073** — Drop data `num_shards` plumbing (script + Make + README) — remove
   `--num-shards`/`NUM_SHARDS` from `scripts/run_data_pipeline.py` and the Makefile
   `run-data-pipeline` target, and rewrite their docstrings/help + the README data-pipeline
   row to describe the per-source parallelism model (platform bucketing + HF `num_workers`).
   The memory `num_shards` is untouched. Depends on #072. (file:
   `tracker/073-drop-data-num-shards-plumbing.groomed.md`)
5. **#074** — ADR-002 amendment + full acceptance + live e2e — land the proposed ADR-002
   §3 amendment (platform-grouping data fan-out axis, HF offset-windowing sub-fan-out,
   dropped data `num_shards`, `runner_global_limit` 4→6 justification), run the full
   `make memory-integration-tests-all` acceptance gate, and the `[HUMAN]` live e2e
   (Prefect UI shows one orchestrator parent + N platform/window worker children, no
   index run, arXiv windows disjoint). Depends on #070–#073. (file:
   `tracker/074-adr-amendment-and-live-e2e-acceptance.groomed.md`)

## Out of scope (intentional)

- **`split_dataset_by_node` whole-dataset sharding.** Offset/`skip` is the chosen
  approach for capped windows. The `split_dataset_by_node` upgrade path (for a future
  uncapped whole-dataset run) is documented in the ADR amendment but NOT implemented here.
- **A second HF dataset / a generic per-platform `num_workers`.** Only HuggingFace
  sub-fans-out; the non-HF platforms each get exactly one worker. `num_workers` lives on
  `HuggingFaceDatasetSource` only.
- **Memory pipeline `num_shards`.** The memory orchestrator keeps its `num_shards` knob,
  script flag, and Make plumbing entirely unchanged. Only the DATA orchestrator drops it.
- **The shared `tree.sharding` helpers.** `_partition_into_shards` / `_resolve_num_shards`
  stay — the MEMORY orchestrator still uses them. The data orchestrator merely STOPS
  importing/using them; do NOT delete them.
- **New idempotency / dedup work.** Disjoint offset windows + the existing
  `(user_id, source_uri)` dedup already make re-runs and window overlap safe (caveat 9).
- **The frozen config fixture.** `frozen_config.yaml` stays at `runner_global_limit: 4`
  and its source counts unchanged; the config-test assertions read the fixture, so they
  stay green by construction.

## Documentation updates (this grooming round)
- **Glossary:** no glossary in this project (`docs/glossary.md` absent); not applicable.
  No new domain terms introduced — "platform", "window", "worker", and "orchestrator"
  are existing vocabulary across ADR-002 §3, the config models, and the flow docstrings.
- **ADRs:** ADR-002 (`docs/adrs/002_pipeline_concurrency_and_voyage_rate_limiting.md`)
  §3 is AMENDED (not superseded; Status stays `Accepted`) to record: the
  platform-grouping data fan-out axis, the HuggingFace offset-windowing sub-fan-out, the
  dropped data `num_shards` knob, and the `runner_global_limit` 4→6 bump with its
  justification. Per the owner's brief the amendment text is DRAFTED in grooming (handed
  back as a proposal for the human gate) and AUTHORED to disk in task **#074** — it is
  NOT pre-written in the grooming commit. ADR-001 is unchanged (no contradiction).

## Open questions
- None blocking. Every decision (platform map, window math + last-worker-remainder rule,
  `num_workers`/`offset` field defaults + their authored-vs-runtime split, depth-1
  no-recursion dispatch, dropped data `num_shards`, `runner_global_limit` 4→6 with the
  fixture held at 4, idempotency already-handled) is pinned in the locked design above.
