# Data pipeline: split into orchestrator + worker deployments

Status: pending
Tags: `infra`, `data`, `refactor`
Depends on: #066, #067
Blocks: #069

## Scope

Replace the single `data-pipeline-etl` deployment (current: one
`data_pipeline(user_id)` flow in `apps/memory/src/tree/data/pipeline.py` that reads
the configured `app_config.sources.sources` list and fans out over source-TYPES
in-process via `asyncio.gather` over per-type batch subflows; makes NO Voyage calls)
with TWO separate named deployments, mirroring the memory split (#067). The data
pipeline produces `documents` only — there is NO trailing index step. Implements the
data half of ADR-002 §3 as amended by #066. Parallelism is bounded by Bright
Data/HTTP, NOT the Voyage rate-limiter (the data pipeline doesn't embed).

### Worker flow — `data-etl-worker`

- Prefect `@flow(name="data-etl-worker", log_prints=True)`.
- Signature: `(user_id: PydanticObjectId, sources: list[SourceEntry]) -> list[Document]`.
  (`SourceEntry` is the discriminated-union from `tree.config.app_config`. If passing
  Pydantic models as Prefect deployment parameters proves awkward to serialize, the
  SWE may instead pass the shard as the serialized source dicts and re-parse to
  `SourceEntry` inside the worker — pick whichever round-trips cleanly through
  `run_deployment`; the contract is "the worker receives exactly the shard's source
  entries".)
- Body: ingest the SUBSET (shard) of sources it was handed, reusing the EXISTING
  per-source-type batch logic from today's `data_pipeline`. Concretely: group the
  shard's `sources` by variant (`SubstackRssSource`, `SubstackArticleSource`,
  `YouTubeRssSource`, `YouTubeVideoSource`, `HuggingFaceDatasetSource`, `WebSource`)
  and run the existing batch subflow for each variant PRESENT in the shard — the same
  `ingest_substack_rss_feed_batch` / `ingest_substack_article_batch` /
  `ingest_youtube_rss_feed_batch` / `ingest_youtube_video_batch` /
  `_HUGGINGFACE_DATASET_HANDLERS` (with unknown-dataset-id `ValueError`) / `ingest_url`
  dispatch that `data_pipeline` does today, just scoped to the shard's entries rather
  than the whole config list. A variant absent from the shard is skipped (same
  "skipped: no X entries" log lines, scoped to the shard).
- Keep the boot-time `assert_settings_match_live_vector_index` dim-check gate exactly
  as `data_pipeline` has it today (non-fatal on `vector_index not found`, hard-fail on
  a real mismatch) and the `init_mongodb` call.
- Registered as deployment `data-etl-worker` in `tree/orchestrator.py`.

### Orchestrator flow — `data-etl-orchestrator`

- Prefect `@flow(name="data-etl-orchestrator", log_prints=True)`.
- Signature: `(user_id: PydanticObjectId, num_shards: int = 1) -> FanOutStats`.
  (Reuse the `FanOutStats` report shape — `shards_total`/`succeeded`/`failed`/`failures`.
  The data orchestrator does NOT collect per-shard `Document` lists back; the worker
  persists documents directly, so the orchestrator only needs the fan-out accounting.
  If the SWE prefers a data-specific report dataclass over reusing the memory
  `FanOutStats`, that's acceptable as long as it carries the same shards_total/
  succeeded/failed/failures fields.)
- Body:
  1. Read the configured `app_config.sources.sources` list.
  2. `_resolve_num_shards(num_shards)` (clamps non-positive → 1).
  3. Empty sources list → clean no-op: zero dispatch, `shards_total=0`.
  4. Partition the sources into `min(num_shards, N)` balanced contiguous shards via the
     shared `_partition_into_shards` helper (from #066). Each shard is a disjoint
     subset of the configured source entries; the in-order union reconstructs the full
     list.
  5. Dispatch ONE `data-etl-worker` run per shard via `run_deployment` under
     `asyncio.gather(return_exceptions=True)`, each with `parameters={"user_id": str(user_id), "sources": <shard>}`.
     One shard's failure is caught, recorded in the report's `failures[str(idx)]`, and
     never aborts the others.
  6. NO trailing step — the data pipeline only produces `documents`; there is no index.
- Registered as deployment `data-etl-orchestrator` in `tree/orchestrator.py`.

### Data fan-out core

Mirror memory's `_fan_out_extraction` but for the data worker and with NO trailing
index. The SWE may add a `_fan_out_data(user_id, shards, run_deployment)` core (in a
data module, e.g. `tree/data/sharding.py` or alongside the data pipeline) that does the
`gather(return_exceptions=True)` + per-shard failure isolation and returns the report —
unit-testable with `run_deployment` injected/mocked, exactly like the memory core. Do
NOT fire any indexing run.

### Orchestrator registration (`tree/orchestrator.py`)

- REMOVE the `data_pipeline.to_deployment(name="data-pipeline-etl", …)` registration.
- ADD `data_etl_worker.to_deployment(name="data-etl-worker", tags=["data-pipeline", "worker"])`
  and `data_etl_orchestrator.to_deployment(name="data-etl-orchestrator", tags=["data-pipeline", "orchestrator"])`.
- KEEP `serve(..., limit=limit)` (#065) and every other registration, including the
  `memory-extract-etl-worker` / `memory-extract-etl-orchestrator` from #067, the
  `memory-indexing-etl`, the dream cron, and the file/conversation/youtube ingest
  deployments.

### Make target + script

- `apps/memory/Makefile` `run-data-pipeline`: re-point at the orchestrator and add an
  optional `NUM_SHARDS` flag (matching the memory target's `NUM_SHARDS` pattern).
  Keep the `USER_ID` guard. Update help text to reference `data-etl-orchestrator`.
- `apps/memory/scripts/run_data_pipeline.py`: change `DEPLOYMENT_NAME` from
  `"data-pipeline-etl/data-pipeline-etl"` to
  `"data-etl-orchestrator/data-etl-orchestrator"`. Add a `--num-shards` option
  (optional, `>= 1`, mirroring `run_memory_pipeline.py`'s guard + parameter
  forwarding). Keep `init_logger()` at module level. Update the docstring to describe
  the orchestrator/worker split (operator runs the orchestrator; it partitions sources
  into shards and dispatches one `data-etl-worker` per shard; no trailing step).

### Test rework

- `tests/unit/data/test_pipeline.py`: this suite currently exercises `data_pipeline`'s
  per-variant dispatch + grouping + skip-when-absent + unknown-HF-id `ValueError`.
  Re-target the variant-dispatch / grouping / skip / error assertions to
  `data_etl_worker` (the worker now owns the per-type batch logic) so the existing
  behavioral coverage (one batched call per variant, skip-when-absent, web per-entry
  dispatch, None-filtering, HF overrides, youtube branches, unknown-HF-id raise) is
  preserved against the worker.
- Add `data-etl-orchestrator` unit tests (new file or new class): partition the
  configured sources into N balanced shards, dispatch one `data-etl-worker` per shard
  (mock `run_deployment`), assert each child carries `{user_id, sources: <shard>}`, the
  shard union reconstructs the full source list, NO indexing/trailing run is ever
  issued, empty-sources is a no-op (`shards_total=0`), and one shard's failure is
  isolated + recorded while the others proceed.
- Add `_fan_out_data` (or equivalent core) unit tests mirroring the memory fan-out core
  tests, asserting NO trailing index call exists for the data path.
- The serve-registration name-set test (`tests/unit/test_orchestrator.py`) is reworked
  in #069 (after both splits land) — do NOT edit it here beyond what #069 specifies;
  #068's own unit tests cover the data flows directly.

### Stale-deployment cleanup (ops note — include in the task log / verification)

After deploying this task (serve-workflows re-run), the old `data-pipeline-etl`
server-side deployment is orphaned. Document — in the task log and as a verification
step — that the operator must run
`prefect deployment delete data-pipeline-etl/data-pipeline-etl`. Ops action, not code;
the AC verifies the note, the live deletion is part of #069's `[HUMAN]` pass.

## Acceptance Criteria

- [x] A `data-etl-worker` flow exists with signature
      `(user_id, sources) -> list[Document]` that ingests the shard's sources by
      grouping them by variant and running the EXISTING per-type batch subflows for
      each variant present (Substack RSS/article, YouTube RSS/video, HuggingFace
      dataset with unknown-id `ValueError`, web via `ingest_url`).
- [x] The worker keeps the boot-time `assert_settings_match_live_vector_index`
      dim-check gate (non-fatal on `vector_index not found`, hard-fail on a real
      mismatch) and the `init_mongodb` call.
- [x] A `data-etl-orchestrator` flow exists with signature `(user_id, num_shards=1)`
      that reads the configured sources, partitions into `min(num_shards, N)` balanced
      shards, and dispatches one `data-etl-worker` run per shard.
- [x] Each worker dispatch carries `parameters={"user_id": str(user_id), "sources": <shard>}`;
      the in-order union of all shards' sources equals the configured source list
      (asserted in unit tests).
- [x] The data orchestrator fires NO trailing/index run — verifiable by grep (no
      `indexing` reference in the data orchestrator/core) and by a unit test asserting
      zero non-worker `run_deployment` calls.
- [x] Empty configured sources list → clean no-op: zero worker dispatches,
      `shards_total=0`.
- [x] One shard's failure is isolated (`gather(return_exceptions=True)`), recorded in
      the report's `failures`, and the remaining shards still run (asserted in tests).
- [x] The existing per-variant behavioral coverage is preserved against `data-etl-worker`:
      one batched call per variant, skip-when-absent log lines, web per-entry dispatch
      via `ingest_url`, None-result filtering, HF `max_samples`/`fetch_content`
      forwarding, youtube RSS/video dispatch, and unknown-HF-dataset-id `ValueError`.
- [x] `tree/orchestrator.py` no longer registers `data-pipeline-etl`; it registers
      `data-etl-worker` and `data-etl-orchestrator`, keeps `serve(..., limit=limit)`,
      and keeps the #067 memory deployments + dream cron + all other registrations.
- [x] `python -c "import tree.orchestrator"` succeeds.
- [x] `scripts/run_data_pipeline.py` `DEPLOYMENT_NAME` targets
      `data-etl-orchestrator/data-etl-orchestrator`; a `--num-shards >= 1` option exists
      (exits 1 on `0`/negative); `init_logger()` is called at module level.
- [x] `make memory-run-data-pipeline` help text and behavior reference the
      orchestrator; `USER_ID` guard intact; optional `NUM_SHARDS` threaded.
- [x] The stale-`data-pipeline-etl` `prefect deployment delete` cleanup step is
      documented in the task log / verification section.
- [x] `make memory-format-fix && make memory-lint-fix && make memory-format-check && make memory-lint-check && make pre-commit` all clean.
- [x] `make memory-unit-tests` passes (reworked data-pipeline + new orchestrator/worker
      unit tests green, 0 warnings).
- [x] `make memory-integration-tests-all` passes on a quiesced + isolated mongot stack
      (run in isolation per CLAUDE.md).
- [ ] [HUMAN] Live e2e (deferred to #069's combined live pass, recorded here as the data
      half): with the stack up and `make memory-serve-workflows` running,
      `make memory-run-data-pipeline USER_ID=<oid> NUM_SHARDS=2` shows the parent run as
      `data-etl-orchestrator` and exactly 2 child runs as `data-etl-worker` in the
      Prefect UI, and NO index run fires.

## User Stories

### Story: Operator triggers a 2-way sharded data ingestion
1. Operator has the stack up and `make memory-serve-workflows` running, and the
   configured `default.yaml` sources list has several entries across variants.
2. Operator runs `make memory-run-data-pipeline USER_ID=507f1f77bcf86cd799439011 NUM_SHARDS=2`.
3. The script triggers the `data-etl-orchestrator` deployment.
4. The orchestrator reads the configured sources and partitions them into 2 balanced
   shards.
5. In the Prefect UI the operator sees ONE parent run named `data-etl-orchestrator` and
   TWO child runs named `data-etl-worker`.
6. No index run fires — the data pipeline only produces documents.

### Story: Operator runs a plain (default) data ingestion
1. Operator runs `make memory-run-data-pipeline USER_ID=<oid>` (no NUM_SHARDS).
2. The orchestrator runs with `num_shards=1`, partitions the sources into a single
   shard, and dispatches ONE `data-etl-worker` run with the full source list.
3. The worker ingests every configured source exactly as the old single
   `data-pipeline-etl` run did, persisting the same documents.

### Story: A worker shard contains mixed source variants
1. The orchestrator hands a `data-etl-worker` a shard containing 2 Substack RSS feeds,
   1 YouTube video, and 1 web URL.
2. The worker groups them by variant and runs `ingest_substack_rss_feed_batch` once
   with the 2 feeds, `ingest_youtube_video_batch` once with the 1 video, and
   `ingest_url` for the web URL.
3. Variants absent from the shard (e.g. HuggingFace) are skipped with the "no X
   entries configured" log line — scoped to this shard.

### Story: One data shard fails
1. Operator triggers `data-etl-orchestrator` with `NUM_SHARDS=2`.
2. One worker shard raises (e.g. a Bright Data fetch error).
3. The orchestrator isolates that failure (the other shard's documents are still
   ingested), records it in the report's `failures`, and completes — no index run, no
   abort of the surviving shard.

### Story: Operator cleans up the orphaned old deployment
1. After deploying #068 and re-serving workflows, the operator sees the old
   `data-pipeline-etl` deployment still listed in Prefect.
2. Following the documented ops note, they run
   `prefect deployment delete data-pipeline-etl/data-pipeline-etl`.
3. Only `data-etl-orchestrator` and `data-etl-worker` remain for the data pipeline.

---

Blocked by: #066, #067

## Log

### [PM] 2026-05-23 — Grooming

**Summary**
Splits the single `data-pipeline-etl` deployment into `data-etl-worker` (ingests one
shard of the configured sources, reusing today's per-source-type batch logic) and
`data-etl-orchestrator` (reads the configured sources, partitions into N balanced
shards, dispatches one worker per shard, NO trailing step). Mirrors the memory split
(#067) minus the index — the data pipeline only produces documents. Reuses the shared
`_partition_into_shards` helper (#066). Reworks the data-pipeline unit tests, adds
orchestrator/worker + fan-out-core tests, re-points the Make target + script at the
orchestrator with an optional NUM_SHARDS, and documents the stale-deployment cleanup.

**Key decisions**
- The new fan-out axis for data is SOURCE-shards (a balanced subset of the configured
  `sources:` list), distinct from memory's document-shards but the SAME partitioning
  math (reused helper). Today's data fan-out is over source TYPES in-process; the split
  moves the per-type batch logic into the worker and adds a coarse cross-shard fan-out
  over worker deployments on top.
- NO trailing index (explicit owner instruction) — the data pipeline produces
  `documents` only. The data fan-out core must never reference an indexing deployment;
  this is an AC (grep + a unit test).
- No Voyage rate-limiter participation — data doesn't embed; parallelism is bounded by
  Bright Data/HTTP. `num_shards` is a per-run knob with no YAML default (#062
  precedent), mirroring memory.
- The existing `tests/unit/data/test_pipeline.py` behavioral coverage is preserved by
  re-targeting it at `data-etl-worker` (which now owns the per-type dispatch), so we
  don't lose the variant/grouping/skip/error tests.
- Serialization caveat called out: `SourceEntry` is a Pydantic discriminated union;
  passing it through `run_deployment` parameters may need dict round-tripping. SWE
  picks the cleanest serialization; contract is "worker receives exactly the shard's
  entries".
- Depends on #067 too (not just #066) so the data flows mirror the memory
  orchestrator/worker conventions (report shape, fan-out core structure, registration
  pattern) the SWE just established — keeps the two splits consistent.

**Dependencies**
- #066 — shared `_partition_into_shards` / `_resolve_num_shards` helper + settled ADR.
- #067 — orchestrator/worker conventions (fan-out core shape, FanOutStats, registration
  + script/Make patterns) to mirror.

**User stories**
- 5 stories covering: 2-way sharded ingestion + UI distinct-name check (+ no index),
  default run parity with the old single deployment, mixed-variant shard grouping,
  shard-failure isolation, and orphaned-deployment cleanup.

Ready for implementation.

### [SWE] 2026-05-23 16:30 — Implementation

**Summary**
Split `data-pipeline-etl` into `data-etl-orchestrator` (operator entrypoint: reads the
configured `sources:` list, partitions into `min(num_shards, N)` balanced shards via
the shared `tree.sharding._partition_into_shards`/`_resolve_num_shards`, dispatches one
`data-etl-worker` per shard, NO trailing step) + `data-etl-worker` (ingests one shard,
reusing the existing per-source-type batch logic). Mirrors the #067 memory split minus
the index. Sources round-trip through `run_deployment` as `model_dump()` dicts;
the worker re-parses to `SourceEntry` via a discriminated-union `TypeAdapter`.

**Flow + deployment names**
- Worker: flow `@flow(name="data-etl-worker")` → deployment `data-etl-worker`
  (tags `["data-pipeline", "worker"]`).
- Orchestrator: flow `@flow(name="data-etl-orchestrator")` → deployment
  `data-etl-orchestrator` (tags `["data-pipeline", "orchestrator"]`).

**Sources-serialization approach**
`SourceEntry` is a Pydantic discriminated union; Prefect JSON-serializes flow-run
parameters. The orchestrator partitions the configured `list[SourceEntry]`, calls
`.model_dump()` on each entry (the `type` discriminator is preserved), and passes
`sources: list[dict]` to `run_deployment`. The worker's `_coerce_sources` re-parses
dicts through `TypeAdapter(list[SourceEntry])` (typed objects passed in-process pass
through unchanged). Verified the JSON round-trip preserves variant + fields
(`test_pipeline.py::test_reconstructs_sources_from_serialized_dicts` and the live
`uv run python` round-trip check).

**Files modified**
- `apps/memory/src/tree/data/sharding.py` (NEW) — `DataFanOutStats` + `_fan_out_data`
  (gather + per-shard failure isolation, NO trailing index); re-exports
  `_partition_into_shards`/`_resolve_num_shards` from `tree.sharding`.
- `apps/memory/src/tree/data/pipeline.py` — replaced `data_pipeline` with
  `data_etl_worker(user_id, sources)` + `data_etl_orchestrator(user_id, num_shards=1)`;
  factored the per-type dispatch into `_ingest_sources`; added `_coerce_sources` +
  `_SOURCES_ADAPTER`. Worker keeps `init_mongodb` + the
  `assert_settings_match_live_vector_index` boot gate.
- `apps/memory/src/tree/orchestrator.py` — removed `data-pipeline-etl`; registered
  `data-etl-orchestrator` + `data-etl-worker`; everything else (memory
  orchestrator/worker, `memory-indexing-etl`, `serve(..., limit=limit)`, dream cron,
  file/conversation/youtube ingest) UNCHANGED.
- `apps/memory/scripts/run_data_pipeline.py` — `DEPLOYMENT_NAME` →
  `data-etl-orchestrator/data-etl-orchestrator`; added `--num-shards` (`>= 1`, exits 1
  otherwise); rewrote docstring for the orchestrator/worker split; `init_logger()`
  stays at module level.
- `apps/memory/Makefile` — `run-data-pipeline` re-pointed at the orchestrator; optional
  `NUM_SHARDS` threaded; `USER_ID` guard + help text updated.
- `apps/memory/README.md` — fixed the #067 QA nit (old `memory-extraction-etl` name) →
  the deployment list now reflects both the memory and data orchestrator/worker
  splits; data-pipeline table description updated.
- `apps/memory/tests/unit/data/test_pipeline.py` — re-targeted the per-variant
  behavioral coverage at `data_etl_worker`; added a serialized-dict round-trip test.
- `apps/memory/tests/unit/data/test_fanout_data.py` (NEW) — `_fan_out_data` core:
  one run per shard, NO trailing index, worker-deployment dispatch, `{user_id, sources}`
  params, failure isolation, empty no-op.
- `apps/memory/tests/unit/data/test_orchestrator_data.py` (NEW) — `data_etl_orchestrator`
  flow: N dispatches, balanced partition (sizes 2,2,1,1 for 6→4), shard union ==
  configured list, default num_shards=1 → 1 worker w/ all sources, NO index run,
  empty→no-op, one-shard-failure isolation.
- `apps/memory/tests/unit/test_orchestrator.py` — minimal: swapped `data-pipeline-etl`
  for the two new data names in the registration name-set and asserted the old name is
  gone (the broader rework is #069; left green, not pre-empted).
- `apps/memory/tests/integration/data/test_pipeline.py` +
  `tests/integration/data/web/test_web_pipeline.py` — re-pointed the `data_pipeline`
  call sites at `data_etl_worker(user_id, <sources>)` (the worker now owns per-variant
  dispatch and takes sources as an argument).

**Tests**
- Unit: 1447 passing, 0 failing, 0 warnings (`make memory-unit-tests`). New data
  tests: 34 passing across `test_pipeline.py`/`test_fanout_data.py`/
  `test_orchestrator_data.py`/`test_orchestrator.py`.
- Integration (fast, `make memory-integration-tests`, isolated on a quiesced stack):
  153 passed, 1 skipped (BrightData-gated), 116 deselected (slow) — 4m19s.
- `make memory-integration-tests-all` (slow + mongot) NOT run by SWE — that's the
  Tester's acceptance gate (CLAUDE.md). Touched data integration files pass on the fast
  loop.

**Evidence**
```
$ make memory-unit-tests
... 1447 passed in 43.41s ...

$ make memory-integration-tests
... 153 passed, 1 skipped, 116 deselected in 259.50s (0:04:19) ...

$ uv run python -c "import tree.orchestrator" + serve_deployments spy
registered: ['data-etl-orchestrator', 'data-etl-worker', 'dream-consolidation-etl',
 'ingest-conversation-etl', 'ingest-file-etl', 'ingest-youtube-rss-feed-batch-etl',
 'ingest-youtube-video-batch-etl', 'memory-extract-etl-orchestrator',
 'memory-extract-etl-worker', 'memory-indexing-etl']
# data-etl-worker + data-etl-orchestrator present; data-pipeline-etl ABSENT;
# #067 memory names present. ALL ASSERTIONS PASS.

$ uv run python scripts/run_data_pipeline.py --user-id 507f... --num-shards 0  → exit 1
$ uv run python scripts/run_data_pipeline.py --num-shards 2 (no user)          → exit 1
$ make run-data-pipeline → USAGE: make run-data-pipeline USER_ID=<...> [NUM_SHARDS=<n>]

$ grep -in indexing src/tree/data/sharding.py        → only NO-index comments/log strings
$ grep -in indexing src/tree/data/pipeline.py        → only the worker boot dim-check
  (assert_settings_match_live_vector_index) + its comments; NO indexing run_deployment
```

**Stale-deployment ops note (cleanup — verification step)**
After this lands and `make memory-serve-workflows` is re-run, the old server-side
`data-pipeline-etl` deployment is orphaned. The operator must run (per ADR-002 §3
amendment #066 ops note):
```
prefect deployment delete data-pipeline-etl/data-pipeline-etl
```
This is an ops action, not code; the live deletion is part of #069's `[HUMAN]` pass.

**Notes for Tester / PM**
- Acceptance gate `make memory-integration-tests-all` (slow + mongot) NOT run by SWE —
  please run it locally with the full stack up, in isolation per CLAUDE.md (shared
  docker stack across worktrees).
- `[HUMAN]` live e2e (orchestrator parent + 2 worker children in the Prefect UI, no
  index run) is deferred to #069's combined live pass — `make memory-serve-workflows`
  intentionally NOT started here.
- The serve-registration full 4-name assertion is #069; `test_orchestrator.py` was only
  minimally updated to stay green (not pre-empted).

### [Tester] 2026-05-23 19:00 — QA

**Test summary**
- Format / lint / pre-commit: PASS (`ruff format --check` 270 files OK; `ruff check`
  all passed; pre-commit all hooks Passed incl. KGQuery discipline + biome).
- Unit tests: 1447 passed / 0 failed (`make memory-unit-tests`, 42.87s). New data
  suites: 49 passed across `test_pipeline.py` (17), `test_fanout_data.py` (7),
  `test_orchestrator_data.py` (7), `test_orchestrator.py` (3), `test_sharding.py` (15).
- Integration tests (ACCEPTANCE GATE, `make memory-integration-tests-all`, slow +
  requires_mongot, quiesced + isolated stack): 269 passed / 1 skipped (BrightData-gated
  `test_web_search_ingest`) / 0 failed in 655.58s (10:55), exit 0.
- Warnings: 0 (verified `grep -ci warning` = 0 in both unit and integration output).

**E2E adversarial pass** (run live against the local Prefect server; run_deployment
mocked for the dispatch-shape probes)
- Happy path: `data_etl_orchestrator(UID, num_shards=2)` over a 6-source config →
  2 `data-etl-worker` dispatches, `shards_total=2 succeeded=2 failed=0`, NO index run
  (PASS). Script guards: `make run-data-pipeline USER_ID=<oid>` resolves
  `DEPLOYMENT_NAME=data-etl-orchestrator/data-etl-orchestrator` (PASS).
- Break path 1 (boundary: `--num-shards 0`): `run_data_pipeline.py --num-shards 0` →
  `--num-shards must be >= 1 (got 0)`, exit 1 (PASS). `-3` → same, exit 1 (PASS).
- Break path 2 (state edge: `num_shards=0`/`-5` via DIRECT flow trigger bypassing the
  script guard): `_resolve_num_shards` clamps → 1 shard, `shards_total=1`, no zero-shard
  silent no-op (PASS).
- Break path 3 (boundary: `num_shards=4 > N=1`): collapses to 1 shard, no empty shards
  emitted (PASS). Empty sources + `num_shards=3` → 0 dispatch, `shards_total=0` (PASS).
- Break path 4 (malformed input: `--user-id not-an-oid`): exit 1 with ObjectId error
  (PASS). Missing user id → exit 1, no silent default-user fallback (PASS).
- Break path 5 (failure mode: one shard raises under `gather`): the failing shard is
  recorded in `failures`, the surviving shards complete, NO index run fires
  (`test_one_shard_failure_is_isolated` + live `_fan_out_data` probe) (PASS).

**Sources serialization round-trip (data-specific risk) — VERIFIED PER VARIANT**
Live `model_dump()` → `json.dumps`/`json.loads` (the exact `run_deployment` JSON path)
→ `TypeAdapter(list[SourceEntry]).validate_python` for all 6 variants; each re-parses to
the SAME concrete class with the `type` discriminator and fields intact:
`substack_rss`, `substack_article`, `youtube_rss`, `youtube_video`,
`huggingface_dataset` (incl. `max_samples`/`fetch_content`), `web`. The guard test
`test_pipeline.py::test_reconstructs_sources_from_serialized_dicts` is non-vacuous — it
feeds plain `model_dump()` dicts to `data_etl_worker` and asserts the batch sub-flows
receive correctly-typed args (not a dict-vs-dict tautology).

**Acceptance criteria**
- [x] PASS — `data-etl-worker (user_id, sources) -> list[Document]` groups by variant +
      runs existing batch subflows — `src/tree/data/pipeline.py:233` + 17 worker tests.
- [x] PASS — worker keeps `assert_settings_match_live_vector_index` (non-fatal on
      `vector_index not found`, hard-fail on mismatch) + `init_mongodb` —
      `pipeline.py:253-277`.
- [x] PASS — `data-etl-orchestrator (user_id, num_shards=1)` reads config, partitions
      `min(num_shards, N)`, dispatches one worker per shard — `pipeline.py:288` +
      `test_orchestrator_data.py`.
- [x] PASS — each dispatch carries `{user_id: str, sources: <shard>}`; in-order shard
      union == configured list — `test_shards_are_balanced_and_union_reconstructs_sources`,
      `sharding.py:149-161`.
- [x] PASS — NO trailing/index run: grep of `sharding.py`/`pipeline.py` shows no
      indexing `run_deployment`; `test_fires_no_trailing_index_run` +
      `test_fan_out_fires_no_trailing_index_run` assert zero non-worker dispatches.
- [x] PASS — empty sources → no dispatch, `shards_total=0` (`pipeline.py:312`, live probe
      + `test_empty_sources_is_a_clean_noop`).
- [x] PASS — one shard's failure isolated via `gather(return_exceptions=True)`, recorded
      in `failures`, others proceed (`sharding.py:149-181`, two failure-isolation tests).
- [x] PASS — per-variant behavioral coverage preserved on the worker (batched-per-variant,
      skip-when-absent log lines, web per-entry `ingest_url`, None-filtering, HF overrides,
      youtube RSS/video, unknown-HF-id `ValueError`) — `test_pipeline.py` 17 tests.
- [x] PASS — `orchestrator.py` drops `data-pipeline-etl`, adds `data-etl-worker` +
      `data-etl-orchestrator`, keeps `serve(..., limit=limit)`, #067 memory deployments,
      `memory-indexing-etl`, dream cron + ingest deployments — live `serve_deployments`
      spy: 10 names, all assertions pass; `limit` forwarded.
- [x] PASS — `python -c "import tree.orchestrator"` succeeds.
- [x] PASS — `run_data_pipeline.py` `DEPLOYMENT_NAME = data-etl-orchestrator/...`,
      `--num-shards >= 1` (exit 1 on 0/negative, verified live), `init_logger()` at module
      level (`scripts/run_data_pipeline.py:43,46,130`).
- [x] PASS — `make run-data-pipeline` help references the orchestrator + NO trailing
      index; `USER_ID` guard intact (exit 1, verified); `NUM_SHARDS` threaded.
- [x] PASS — stale `prefect deployment delete data-pipeline-etl/data-pipeline-etl`
      cleanup documented in the SWE log + ADR-002 §3 ops note.
- [x] PASS — format / lint / pre-commit clean.
- [x] PASS — `make memory-unit-tests` green, 0 warnings.
- [x] PASS — `make memory-integration-tests-all` green (269 passed / 1 skipped / 0 warn).
- [ ] [HUMAN] Live e2e (orchestrator parent + 2 worker children in the Prefect UI, no
      index run) — NOT RUN. Deferred to #069's combined live pass; `serve-workflows`
      intentionally NOT started here (per task instruction §8).

**Adversarial / regression checks**
- No live reference to `data-pipeline-etl` in `src/`, `scripts/`, or `Makefile`
  (`grep -rn` → none). No remaining importer of the old `data_pipeline` flow symbol;
  the only `data_pipeline` grep hits are docstrings (a pre-existing, unmodified
  `web_search_ingest.py` path string + a `test_pipeline.py` history note) — neither
  ImportErrors.
- README no longer lists the retired `data-pipeline-etl` / `memory-extraction-etl`
  names; it lists the new orchestrator/worker pairs + dream cron (folds the #067 nit).
- Implementation matches ADR-002 §3 "Data topology (target)" exactly (worker = subset
  ingestion, no fan-out/index; orchestrator = read → partition → dispatch worker, no
  trailing step).

**Other issues found**
- NIT (non-blocking, not in AC): the tracker file has a stray `</content>` line at
  ~L394 (an SWE markdown artifact), and `web_search_ingest.py` carries a pre-existing
  docstring reference to a non-existent `run_url_data_pipeline.py` — both predate / are
  orthogonal to #068. Orchestrator can decide whether to fold a cleanup.

**VERDICT: PASS**
