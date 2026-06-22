# Group-by-platform data orchestrator + HuggingFace offset-window fan-out

Status: pending
Tags: `data`, `infra`, `refactor`
Depends on: #070, #071
Blocks: #073, #074

## Scope

Replace the `data-etl-orchestrator`'s COUNT-based partitioning
(`_partition_into_shards(serialized_sources, num_shards)` → mixed-variant shards) with a
GROUP-BY-PLATFORM partition + a HuggingFace OFFSET-WINDOW sub-fan-out. After this task,
the orchestrator emits:

- ONE `data-etl-worker` run per NON-HuggingFace platform bucket present in the configured
  sources (homogeneous, single-platform shard), AND
- `num_workers` `data-etl-worker` runs for HuggingFace (one per disjoint offset-window).

Stay at TWO deployments (`data-etl-orchestrator` + `data-etl-worker`). NO new deployment
(free-tier cap 5/5). All dispatch is depth-1 from the orchestrator process — a worker
NEVER calls `run_deployment` (ADR-002 amendment #066; recursion can deadlock the serve
admission limit). Reuse the existing `_fan_out_data` (gather + per-shard failure
isolation + Opik trace-header forwarding + NO trailing index) UNCHANGED as the dispatch
core. This is purely a change to HOW the orchestrator computes the shards it hands to
`_fan_out_data`.

### Platform map (the partition key)

| Source variant | Platform bucket |
|---|---|
| `SubstackRssSource`, `SubstackArticleSource` | `substack` |
| `YouTubeRssSource`, `YouTubeVideoSource` | `youtube` |
| `WebSource` | `custom` |
| `HuggingFaceDatasetSource` | `huggingface` |

A non-HF platform bucket present in the config ⇒ exactly ONE worker run carrying ALL
entries of that bucket (a HOMOGENEOUS shard). The worker's existing `_ingest_sources`
`isinstance` routing then fires the matching branch(es) for that single platform — e.g.
the `substack` bucket may contain both `substack_rss` and `substack_article` entries, and
the worker batches each variant as today. ("Platform" groups the two Substack variants
and the two YouTube variants together; the worker still batches per VARIANT inside the
homogeneous platform shard.)

### HuggingFace sub-fan-out (offset windows)

For EACH `HuggingFaceDatasetSource` entry in the configured sources, emit `num_workers`
worker runs (`num_workers` from #070, default 1), one per disjoint offset-window:

- `window_size = max_samples // num_workers`
- worker `i` (for `i` in `range(num_workers)`): `offset = i * window_size`, and
  `max_samples_for_window = window_size` — EXCEPT the LAST worker
  (`i == num_workers - 1`), which takes the remainder: `max_samples - offset` (so the
  union of windows is exactly `[0, max_samples)` with no rows dropped when `max_samples`
  isn't divisible by `num_workers`).
- Stamp each window onto a COPY of the entry via
  `entry.model_copy(update={"offset": offset, "max_samples": max_samples_for_window})`
  (NEVER mutate the configured entry; `offset` is a dispatch-time coordinate per #070).
  Each window-entry is its OWN single-entry homogeneous shard `[windowed_entry]` ⇒ the
  worker's HF branch ingests exactly that window (#071 honors `offset`).
- `num_workers == 1` ⇒ one window with `offset=0`/`None`-equivalent (prefer leaving
  `offset` unset, i.e. `None`, when `num_workers == 1` so the single-window run is
  byte-identical to today's HF ingest) covering the full `max_samples`.
- Edge cases the helper MUST handle: `num_workers > max_samples` (clamp so no window has
  `max_samples_for_window <= 0` — collapse to at most `max_samples` windows of size 1, or
  to a single full window; pick the simplest correct rule and TEST it); `max_samples == 0`
  (no HF windows / a clean no-op for that entry). Multiple HF entries each fan out
  independently.

### Partition helper (new pure function)

Add a PURE, unit-testable helper — e.g.
`_partition_sources_by_platform(sources: list[SourceEntry]) -> list[list[SourceEntry]]`
(or a pair: one that buckets non-HF platforms, one that expands HF windows) — that takes
the typed configured sources and returns the FULL list of shards to dispatch
(non-HF platform buckets + HF window-entries), each shard a `list[SourceEntry]`. Keep it
in a place both the flow and tests import cleanly (e.g. alongside the existing data
fan-out code in `tree/data/pipeline.py`, mirroring how `_partition_into_shards` was
imported there). It must be deterministic and order-stable so tests can assert exact
shard contents. The orchestrator then `.model_dump()`s each shard's entries (as today)
before handing the serialized shards to `_fan_out_data`.

### Orchestrator flow changes

In `data_etl_orchestrator` (`tree/data/pipeline.py`):

- REMOVE the `num_shards: int = 1` parameter. New signature:
  `data_etl_orchestrator(user_id: PydanticObjectId) -> DataFanOutStats`.
- REMOVE the `_resolve_num_shards(num_shards)` + `_partition_into_shards(serialized,
  effective_num_shards)` calls. Do NOT delete the shared helpers from `tree.sharding` —
  the MEMORY orchestrator still imports them. Just stop importing/using them in the data
  pipeline (drop the now-unused `_partition_into_shards`/`_resolve_num_shards` import from
  `pipeline.py` so lint stays clean).
- Build shards via the new platform/window partition helper, serialize each shard's
  entries (`model_dump()`), and dispatch via the UNCHANGED `_fan_out_data(...)` (it
  already does gather + failure isolation + trace forwarding + NO index).
- Empty configured sources ⇒ clean no-op (`DataFanOutStats(shards_total=0)`) — preserved.
- Update the flow docstring + the partition log line to describe platform buckets + HF
  windows instead of count-based shards.

The WORKER (`data_etl_worker`, `_ingest_sources`) is UNCHANGED in this task — it already
groups by variant and the HF offset threading landed in #071. Each worker now happens to
receive a homogeneous (single-platform) shard, but the routing code is identical.

### Test rework

- `tests/unit/data/test_orchestrator_data.py` — REWORK for the new axis:
  - One worker dispatch per non-HF platform bucket present; entries grouped by platform
    (a `substack_rss` + a `substack_article` land in the SAME `substack` worker shard; a
    `youtube_rss` + `youtube_video` land in the SAME `youtube` shard; web → one `custom`
    shard).
  - A HF entry with `num_workers=N` produces exactly N worker dispatches, each carrying a
    single windowed HF entry with the correct `offset` + `max_samples` (assert the window
    arithmetic incl. the last-worker remainder).
  - The in-order union of all NON-HF shards' entries equals the configured non-HF sources;
    the HF windows tile `[0, max_samples)` with no gap/overlap.
  - `num_workers=1` HF ⇒ one HF dispatch with the full `max_samples` and `offset`
    unset/`None` (byte-identical to today's single HF run).
  - NO `num_shards` param accepted (calling `data_etl_orchestrator(user_id, num_shards=2)`
    is a `TypeError` — assert the param is gone).
  - Still: NO trailing/index run; empty sources → no-op `shards_total=0`; one shard's
    failure isolated + recorded while the others proceed (these are `_fan_out_data`
    behaviors — keep their assertions).
- Add a focused PURE unit test file (or class) for the new partition helper —
  `_partition_sources_by_platform` (e.g. `tests/unit/data/test_platform_partition.py`):
  platform bucketing, HF window math (divisible + remainder + `num_workers >
  max_samples` clamp + `max_samples=0`), multiple HF entries, order-stability, and the
  homogeneous-shard invariant (every shard's entries share one platform — except each HF
  shard is a single windowed entry).
- `tests/unit/data/test_fanout_data.py` — UNCHANGED (the `_fan_out_data` core is reused
  as-is). If any of its fixtures assumed count-based shards, they only feed `_fan_out_data`
  arbitrary `list[list[dict]]` shards, so they keep passing; do NOT weaken them.
- `tests/unit/test_orchestrator.py` (serve-registration name set) — UNCHANGED: the
  deployment names + topology are identical (still `data-etl-orchestrator` +
  `data-etl-worker`). Do not edit beyond keeping it green.
- Integration: `tests/integration/data/test_pipeline.py` already drives
  `data_etl_worker(user_id, <sources>)` directly (worker unchanged), so it should stay
  green. If any integration test triggered `data_etl_orchestrator` with `num_shards`,
  re-point it to the no-arg orchestrator.

### Files touched

- `apps/memory/src/tree/data/pipeline.py` — new platform/window partition helper(s);
  `data_etl_orchestrator` drops `num_shards`, builds shards via the new helper, dispatches
  via the unchanged `_fan_out_data`; drop the unused `tree.sharding` import; update
  docstrings + log lines.
- `apps/memory/tests/unit/data/test_orchestrator_data.py` — reworked for the platform/window axis.
- `apps/memory/tests/unit/data/test_platform_partition.py` (NEW) — pure partition-helper tests.
- (verify-green, likely untouched) `apps/memory/tests/unit/data/test_fanout_data.py`,
  `apps/memory/tests/unit/data/test_pipeline.py`, `apps/memory/tests/unit/test_orchestrator.py`,
  `apps/memory/tests/integration/data/test_pipeline.py`.

## Acceptance Criteria

- [x] `data_etl_orchestrator` signature is `(user_id) -> DataFanOutStats` — the
      `num_shards` parameter is REMOVED (passing it raises `TypeError`).
- [x] The orchestrator emits exactly ONE `data-etl-worker` dispatch per non-HF platform
      bucket present in the configured sources, each carrying a HOMOGENEOUS shard (all
      entries share one platform: substack / youtube / custom).
- [x] Substack RSS + Substack article entries land in the SAME `substack` worker shard;
      YouTube RSS + YouTube video land in the SAME `youtube` shard; web entries land in the
      `custom` shard.
- [x] Each `HuggingFaceDatasetSource` with `num_workers=N` produces exactly N
      `data-etl-worker` dispatches, each carrying a single windowed HF entry; window `i`
      has `offset = i * (max_samples // N)` and `max_samples = max_samples // N`, except
      the LAST window which takes the remainder `max_samples - offset`. The windows tile
      `[0, max_samples)` with no gap or overlap.
- [x] `num_workers=1` HF ⇒ exactly ONE HF dispatch with the full `max_samples` and
      `offset` unset/`None` — byte-identical to today's single HF ingest.
- [x] HF window edge cases handled + tested: `num_workers > max_samples` clamps so no
      window has `max_samples <= 0`; `max_samples == 0` emits no HF window for that entry;
      multiple HF entries fan out independently.
- [x] All dispatch happens depth-1 from the orchestrator via the UNCHANGED `_fan_out_data`
      under `asyncio.gather(return_exceptions=True)`; the worker issues NO `run_deployment`
      (verifiable by grep: the worker/`_ingest_sources` path contains no `run_deployment`).
- [x] NO trailing/index run is ever fired (grep: no `indexing` dispatch in the data
      orchestrator/core; a unit test asserts zero non-worker dispatches).
- [x] Empty configured sources ⇒ clean no-op (`shards_total=0`, zero dispatch).
- [x] One shard's failure is isolated (`gather(return_exceptions=True)`), recorded in
      `DataFanOutStats.failures`, and the remaining shards still run.
- [x] The shared `tree.sharding._partition_into_shards` / `_resolve_num_shards` are NOT
      deleted (the memory orchestrator still imports them); the data pipeline simply no
      longer imports/uses them, and lint is clean (no unused import).
- [x] Each dispatched shard is serialized as `list[dict]` carrying the `type`
      discriminator (+ `offset`/`max_samples` for HF windows) so it round-trips through
      `run_deployment` params; the worker re-parses via the existing `TypeAdapter`.
- [x] `python -c "import tree.orchestrator"` succeeds; deployment names/topology unchanged.
- [x] `make memory-format-fix && make memory-lint-fix && make memory-format-check && make memory-lint-check && make pre-commit` all clean.
- [x] `make memory-unit-tests` passes, 0 warnings (reworked orchestrator + new
      partition-helper tests green).
- [x] `make memory-integration-tests` (fast tail) passes for the touched data path.
- [ ] [HUMAN] Live e2e deferred to #074 (recorded here): the Prefect UI shows one
      `data-etl-orchestrator` parent + one worker per non-HF platform + `num_workers` HF
      window workers, no index run, and the arXiv windows are disjoint.

## BDD scenarios

### Scenario: one homogeneous worker per non-HF platform
- **Given** configured sources with 3 Substack feeds, 2 Substack articles, 1 YouTube RSS,
  1 YouTube video, and 2 web URLs (and no HF entry)
- **When** `data_etl_orchestrator(user_id)` runs (`run_deployment` mocked)
- **Then** exactly 3 worker dispatches fire: a `substack` shard with the 5 Substack
  entries, a `youtube` shard with the 2 YouTube entries, and a `custom` shard with the 2
  web URLs — each shard homogeneous to one platform.

### Scenario: HuggingFace fans out into disjoint offset windows
- **Given** one `HuggingFaceDatasetSource(max_samples=1000, num_workers=4)` configured
- **When** the orchestrator partitions
- **Then** 4 worker dispatches fire with windows `(offset=0, max_samples=250)`,
  `(250, 250)`, `(500, 250)`, `(750, 250)` — tiling `[0, 1000)` exactly.

### Scenario: HuggingFace remainder goes to the last window
- **Given** `HuggingFaceDatasetSource(max_samples=1000, num_workers=3)`
- **When** the orchestrator partitions
- **Then** 3 windows fire: `(0, 333)`, `(333, 333)`, `(666, 334)` — the last window takes
  the remainder so the union covers `[0, 1000)` with no dropped rows.

### Scenario: single-worker HuggingFace is byte-identical to today
- **Given** `HuggingFaceDatasetSource(max_samples=1000, num_workers=1)`
- **When** the orchestrator partitions
- **Then** exactly ONE HF dispatch fires with the full `max_samples=1000` and `offset`
  unset/`None` — the same as the pre-feature single HF run.

### Scenario: the num_shards knob is gone
- **Given** the new orchestrator
- **When** a caller invokes `data_etl_orchestrator(user_id, num_shards=2)`
- **Then** it raises `TypeError` (the parameter no longer exists); parallelism is declared
  per-source (platform bucketing + HF `num_workers`), not via a global shard count.

### Scenario: a windowed shard round-trips through run_deployment params
- **Given** a HF window shard `[HuggingFaceDatasetSource(..., offset=250, max_samples=250)]`
- **When** the orchestrator serializes it (`model_dump()`) and the worker re-parses it via
  `TypeAdapter(list[SourceEntry])`
- **Then** the worker receives a `HuggingFaceDatasetSource` with `offset == 250` and
  `max_samples == 250`, and #071's HF branch ingests exactly that window.

### Scenario: one platform/window shard fails, the rest proceed
- **Given** a config that fans out into several platform + HF-window shards, where one
  shard raises (e.g. a Bright Data fetch error or a window ingest error)
- **When** the orchestrator dispatches under `_fan_out_data`
- **Then** the failing shard is recorded in `DataFanOutStats.failures`, the surviving
  shards still complete, and NO index run fires.

## User Stories

### Story: Operator runs platform-grouped ingestion with a windowed HuggingFace dataset
1. Operator has `default.yaml` with several Substack/YouTube/web sources plus
   `librarian-bots/arxiv-metadata-snapshot` at `max_samples: 1000, num_workers: 4`, the
   stack up, and `make memory-serve-workflows` running.
2. Operator runs `make memory-run-data-pipeline USER_ID=507f1f77bcf86cd799439011`.
3. The orchestrator groups the non-HF sources by platform (one worker each for substack,
   youtube, custom) and fans the HF dataset into 4 disjoint offset-windows (4 workers).
4. In the Prefect UI the operator sees ONE `data-etl-orchestrator` parent and several
   `data-etl-worker` children — one per non-HF platform plus 4 HF window workers.
5. No index run fires; the arXiv windows ingest disjoint `[0,250)`, `[250,500)`,
   `[500,750)`, `[750,1000)` row ranges.

### Story: The heavy dataset no longer skews the fan-out
1. Previously a count-based `num_shards` could drop the whole arXiv dataset (millions of
   rows) into one worker while three workers split a few URLs.
2. Now arXiv is split across `num_workers` windows regardless of how many URL sources
   exist, and the URL platforms each get their own dedicated worker.
3. The operator no longer has to guess a global `num_shards`; parallelism is declared
   per-source (platform bucketing is automatic; HF width is `num_workers`).

## Test guidance

- The platform/window partition helper is PURE decision logic → UNIT, no Mongo, no
  Prefect, no markers. Drive it with constructed `SourceEntry` lists and assert exact
  shard contents + window arithmetic.
- The orchestrator flow tests mock `tree.data.pipeline.run_deployment` and patch
  `tree.data.pipeline.app_config` (mirror the existing `test_orchestrator_data.py`
  `_patch_config` / `_capture_run_deployment` helpers) — UNIT, no real Prefect server.
- The `_fan_out_data` core (gather + failure isolation + no-index) is already covered by
  `test_fanout_data.py`; reuse it, don't duplicate. The new partition tests own the new
  axis; the orchestrator tests own the integration of partition + fan-out.
- Per CLAUDE.md, call the `/testing-python` skill when writing the tests, and run via
  `make memory-*` targets on the LOCAL env.

---

Blocked by: #070, #071

## Log

### [PA] 2026-06-22 14:10 — Grooming

**Summary**
Replace the data orchestrator's count-based partitioning with a group-by-platform
partition (one homogeneous worker per non-HF platform bucket) plus a HuggingFace
offset-window sub-fan-out (`num_workers` window-runs via `model_copy(update={"offset",
"max_samples"})`). Drop the `num_shards` param. Reuse the existing `_fan_out_data` core
(gather + failure isolation + trace forwarding + no index) unchanged — only the shard
COMPUTATION changes. Stay at two deployments; all dispatch is depth-1 (no recursion).

**Key decisions**
- Platform map groups the two Substack variants and the two YouTube variants into single
  platform buckets; the worker still batches per VARIANT inside the homogeneous shard, so
  no worker change is needed.
- HF window math: `window_size = max_samples // num_workers`; last worker takes the
  remainder so windows tile `[0, max_samples)` exactly. `num_workers=1` leaves `offset`
  unset for byte-identical parity with today.
- `offset`/`max_samples` are stamped on a `model_copy` — the configured entry is never
  mutated (`offset` is a dispatch-time coordinate per #070).
- `tree.sharding` helpers are NOT deleted (memory still uses them); the data pipeline just
  stops importing them. A new PURE partition helper owns the new axis and is unit-tested
  directly, mirroring how `_partition_into_shards` was unit-tested in #068.
- `_fan_out_data` is untouched, so failure-isolation / no-trailing-index / trace
  forwarding are inherited and their existing tests keep passing.

**Dependencies**
- #070 — `num_workers` + `offset` fields on `HuggingFaceDatasetSource`.
- #071 — the worker's HF branch honors `entry.offset` (windowed ingest).

**User stories**
- 2 stories: platform-grouped run with a windowed HF dataset (+ UI distinct-name check
  deferred to #074); the heavy dataset no longer skews the fan-out.

Ready for implementation.

### [SWE] 2026-06-22 21:34 — Implementation

**Files modified**
- `apps/memory/src/tree/data/huggingface/arxiv_dataset_pipeline.py` — added the PURE
  `arxiv_window_entries(entry) -> list[HuggingFaceDatasetSource]` helper (HF offset-Window
  math; no DB/Prefect).
- `apps/memory/src/tree/data/pipeline.py` — added `_NON_HF_PLATFORMS` map +
  `_partition_sources_by_platform(sources)` group-by-Platform shard builder; rewrote
  `data_etl_orchestrator` to drop `num_shards` (signature now `(user_id) -> DataFanOutStats`),
  build shards via the new helper, `model_dump()` each shard, dispatch via the UNCHANGED
  `_fan_out_data`; dropped the now-unused `tree.sharding` import; updated module +
  flow docstrings + the partition log line.
- `apps/memory/tests/unit/data/test_arxiv_window_entries.py` (NEW) — pure window-math tests.
- `apps/memory/tests/unit/data/test_platform_partition.py` (NEW) — pure partition-helper tests.
- `apps/memory/tests/unit/data/test_orchestrator_data.py` — reworked for the
  Platform/Window axis (replaced the count-based-partition assertions).
- `apps/memory/tests/unit/data/test_fanout_data.py` — only re-pointed the
  `_partition_into_shards` import to `tree.sharding` (its canonical home) since the data
  pipeline no longer re-exports it; the `_fan_out_data` core assertions are unchanged.

**Window-math implementation** (`arxiv_window_entries`, m = `max_samples`, n = `num_workers`):
- `m <= 0` ⇒ `[]` (no window for that entry).
- `n <= 1` ⇒ `[entry.model_copy()]` — `offset` stays `None`, `max_samples` unchanged
  (byte-identical to today's single HF run).
- else: `effective_workers = min(n, m)` (clamp so no window collapses to `<= 0` rows;
  `n > m` ⇒ `m` size-1 windows), `window_size = m // effective_workers`; window `i` ⇒
  `offset = i*window_size`, `max_samples = window_size`, EXCEPT the last window which
  takes the remainder `m - offset` so the windows tile `[0, m)` exactly. Each window is a
  `model_copy(update={offset, max_samples})` — the configured entry is never mutated.

`_partition_sources_by_platform` emits non-HF Platform buckets first (stable order
substack → youtube → custom, one homogeneous shard each, per-Platform internal order
preserved), then each HF entry's window shards (one single-entry shard per window) in
configured-entry order. The orchestrator serializes each shard's entries with
`model_dump()` and hands `list[list[dict]]` to the unchanged `_fan_out_data`.

**Tests**
- Unit: 1597 passing, 0 failing — `make memory-unit-tests`. New: 8 window-math + 11
  partition-helper tests; reworked 18 orchestrator tests (incl. `num_shards`-gone →
  `TypeError`, byte-identical `num_workers=1`, remainder-to-last-window, tiling, mixed
  config 3+4=7, failure isolation).
- Integration (fast tail): 178 passing, 1 skipped, 104 deselected (`@pytest.mark.slow`) —
  `make memory-integration-tests`. The worker is unchanged; the data-pipeline integration
  tests (arxiv/substack/youtube/web + `test_pipeline.py`) confirm it still ingests a
  homogeneous Platform shard and a single windowed-HF-entry shard.

**Acceptance criteria** — all non-HUMAN criteria verified (see checkboxes). Key mappings:
- signature/`num_shards`-gone → `test_orchestrator_signature_has_no_num_shards`,
  `test_passing_num_shards_raises_type_error`.
- one homogeneous worker per non-HF Platform + variant grouping →
  `test_one_homogeneous_worker_per_non_hf_platform`,
  `test_substack_and_youtube_variants_share_one_shard`.
- HF window arithmetic / tiling / remainder / `num_workers=1` →
  `test_hf_fans_out_into_disjoint_offset_windows`, `test_hf_remainder_goes_to_the_last_window`,
  `test_hf_single_worker_is_byte_identical_to_today`, plus the pure
  `test_arxiv_window_entries.py` suite.
- edge cases (`n > m` clamp, `m == 0`, multiple HF entries) →
  `test_num_workers_greater_than_max_samples_clamps_to_size_one_windows`,
  `test_hf_max_samples_zero_emits_no_shard`, `test_multiple_hf_entries_fan_out_independently`.
- depth-1 / no worker `run_deployment` / no index → grep (only `_fan_out_data` dispatches;
  the two `indexing` hits are a boot-time dim-check import + a log string) +
  `test_fires_no_trailing_index_run`.
- shared `tree.sharding` helpers NOT deleted → `test_sharding.py` +
  `memory/extraction/test_fanout.py` still green and importing them.
- `import tree.orchestrator` succeeds; topology unchanged → `test_orchestrator.py` green.
- [ ] [HUMAN] Live e2e deferred to #074 — Prefect UI distinct-worker check.

**Evidence**
```
$ make memory-unit-tests
============================ 1597 passed in 47.40s =============================

$ make memory-integration-tests
========== 178 passed, 1 skipped, 104 deselected in 176.51s (0:02:56) ==========

$ make memory-format-fix && make memory-lint-fix && make memory-format-check && make memory-lint-check
All checks passed!  /  282 files already formatted  /  All checks passed!

$ make pre-commit
ruff check ... Passed / ruff format ... Passed / prettier ... Passed / biome check ... Passed

$ uv run python -c "import tree.orchestrator; import tree.data.pipeline; print('imports OK')"
imports OK
```

**Notes**
- Deviation: the only edit to `test_fanout_data.py` (the task expected it UNCHANGED) was
  re-pointing the `_partition_into_shards` import from `tree.data.pipeline` to its
  canonical home `tree.sharding` — required because the data pipeline no longer re-exports
  it (the locked design drops that import). The `_fan_out_data` assertions are untouched;
  the helper is used there only to build arbitrary `list[list[dict]]` shards.
- The `--num-shards` script flag (`run_data_pipeline.py`) + Makefile `NUM_SHARDS` thread
  are NOT touched here — per the locked design they are deferred to #073/#074. The script
  only adds `num_shards` to deployment params when the flag is passed (default `None`), so
  the default no-arg invocation is unaffected at import/runtime.
- NOT RUN — full slow integration tail (`make memory-integration-tests-all`) and live e2e
  (`make memory-serve-workflows` + `make memory-run-data-pipeline`): the latter is the
  [HUMAN] criterion explicitly deferred to #074; the task scopes verification to the fast
  tail for this data-orchestrator change.

### [Tester] 2026-06-22 21:48 — QA

**Test summary**
- Format / lint / pre-commit: PASS (`282 files already formatted`; `All checks passed!`;
  pre-commit prettier/ruff-check/ruff-format/biome all Passed)
- Unit tests: 1597 passed / 0 failed (0 warnings in the pytest summary)
- Integration tests (fast tail): 178 passed, 1 skipped, 104 deselected (`@slow`) / 0 failed
- Targeted touched files: 43 passed (8 window-math + 11 partition-helper + 15 orchestrator
  + 9 fan-out)
- Env: LOCAL (`.env`), docker stack up (mongodb/mongot/prefect healthy)

**E2E adversarial pass**
- Happy path (orchestrator with mocked `run_deployment`): substack+youtube+web+HF(n=4) →
  7 dispatches (3 homogeneous non-HF + 4 HF windows), `shards_total=7 succeeded=7`, flow
  logs `NO trailing index` (PASS)
- Break path 1 (HF tiling — property sweep, 1500 combos m∈[0,59]×n∈[1,24]): every output
  tiles `[0,m)` exactly — disjoint, no gap/overlap, sizes sum to m, last window takes the
  remainder, no `<=0`-size window, offsets strictly increasing from 0, n==1 leaves
  `offset=None`. Spot tuples: m1000n3→(0,333)(333,333)(666,334); m10n3→(0,3)(3,3)(6,4);
  m7n4→(0,1)(1,1)(2,1)(3,4) (PASS)
- Break path 2 (boundary clamp n>m): m=3,n=10→(0,1)(1,1)(2,1); m=1,n=5→(0,1); m=5,n=10 via
  orchestrator → 5 size-1 windows, no `<=0` window (PASS)
- Break path 3 (absent-platform / empty-bucket): only-web config → exactly ONE `custom`
  worker, no substack/youtube/hf; only-substack_article → one substack shard; m=0 HF mixed
  with web → only the web shard, HF emits nothing (PASS)
- Break path 4 (state edge — mutation): configured HF entry unchanged after orchestration
  (`offset=None, max_samples=1000` post-run) — `model_copy` never mutates input (PASS)
- Break path 5 (round-trip): serialized window re-parses via `TypeAdapter` to a
  `HuggingFaceDatasetSource(offset=250, max_samples=250)` (PASS)
- Break path 6 (num_shards gone): `inspect.signature` params == `["user_id"]`;
  `data_etl_orchestrator(user_id, num_shards=2)` raises `TypeError` (PASS)

**Acceptance criteria**
- [x] PASS — signature `(user_id)`, `num_shards` removed (raises `TypeError`) —
      `test_orchestrator_signature_has_no_num_shards`, `test_passing_num_shards_raises_type_error`;
      independent `inspect.signature` + live `TypeError` repro
- [x] PASS — one homogeneous worker per non-HF platform bucket present —
      `test_one_homogeneous_worker_per_non_hf_platform`; adversarial only-web → 1 custom shard
- [x] PASS — substack rss+article share one shard; youtube rss+video share one; web → custom —
      `test_substack_and_youtube_variants_share_one_shard`, `_partition_sources_by_platform` `_NON_HF_PLATFORMS` map
- [x] PASS — HF num_workers=N → N windowed dispatches, `offset=i*(m//N)`, last takes remainder, tiles `[0,m)` —
      `test_hf_fans_out_into_disjoint_offset_windows`, `test_hf_remainder_goes_to_the_last_window`; 1500-combo property sweep
- [x] PASS — num_workers=1 → ONE dispatch, full max_samples, `offset` None (byte-identical) —
      `test_hf_single_worker_is_byte_identical_to_today`; default-config HF (m=10,n=1) → (None,10)
- [x] PASS — edge cases: n>m clamp (no `<=0` window), m==0 emits no shard, multiple HF independent —
      `test_num_workers_greater_than_max_samples_clamps_to_size_one_windows`, `test_hf_max_samples_zero_emits_no_shard`, `test_multiple_hf_entries_fan_out_independently`
- [x] PASS — depth-1 dispatch via unchanged `_fan_out_data`; worker issues NO `run_deployment` —
      grep: only `_fan_out_data` (pipeline.py:518) dispatches; zero `run_deployment` in worker/`_ingest_sources` path
- [x] PASS — NO trailing/index run — `test_fires_no_trailing_index_run`; grep: 2 `indexing` hits = boot import + log string, not a dispatch; flow logs `NO trailing index`
- [x] PASS — empty sources → no-op `shards_total=0`, zero dispatch — `test_empty_sources_is_a_clean_noop`
- [x] PASS — one shard's failure isolated/recorded, others proceed — `test_one_shard_failure_is_isolated`
- [x] PASS — `tree.sharding._partition_into_shards`/`_resolve_num_shards` NOT deleted, lint clean —
      both present in `tree/sharding.py`; memory still imports them; `test_sharding.py` (15) + `test_orchestrator.py` (6) green; no unused import in pipeline.py
- [x] PASS — shard serialized `list[dict]` with `type` discriminator (+ HF offset/max_samples) round-trips —
      `test_hf_window_shard_roundtrips_offset_through_run_deployment`; independent `TypeAdapter` re-parse
- [x] PASS — `import tree.orchestrator` succeeds; topology unchanged (5 deployments, both data deployments present)
- [x] PASS — format/lint/pre-commit all clean
- [x] PASS — `make memory-unit-tests` 1597 passed, 0 warnings
- [x] PASS — `make memory-integration-tests` (fast tail) 178 passed for the touched data path
- [ ] [HUMAN] — Live Prefect-UI e2e deferred to #074 — Awaiting human verification

**Evidence**
```
$ make memory-unit-tests
============================ 1597 passed in 49.06s =============================

$ make memory-integration-tests
========== 178 passed, 1 skipped, 104 deselected in 186.04s (0:03:06) ==========

$ uv run pytest tests/unit/data/test_arxiv_window_entries.py tests/unit/data/test_platform_partition.py \
    tests/unit/data/test_orchestrator_data.py tests/unit/data/test_fanout_data.py -v
============================== 43 passed in 1.97s ==============================

# adversarial property sweep
ALL INVARIANTS HOLD across m in [0,59], n in [1,24] (1500 combos)
ALL ADVERSARIAL ORCHESTRATOR CHECKS PASSED
```

**Other issues found**
- (out-of-scope, already documented by SWE — NOT a 072 FAIL) `scripts/run_data_pipeline.py`
  still threads `--num-shards`/`NUM_SHARDS` and injects `parameters["num_shards"]` (lines
  66–67) when the flag is passed. Since the orchestrator flow no longer accepts that param,
  an operator running `make memory-run-data-pipeline NUM_SHARDS=2` would now hit a Prefect
  flow-run parameter-validation error. The DEFAULT no-arg invocation is unaffected
  (`num_shards=None` ⇒ not injected). This script plumbing is explicitly the scope of task
  #073 (`drop-data-num-shards-plumbing`) and is listed out-of-scope for 072 — flagged here
  for the orchestrator's awareness so #073 is not skipped.
- `_fan_out_data` is genuinely UNCHANGED (diff touches only docstrings/the partition
  computation); the gather + per-shard failure-isolation + Opik-trace-header forwarding +
  no-index contract is intact and still covered by `test_fanout_data.py` (9 tests).
- `test_fanout_data.py` import re-point (`_partition_into_shards` ← `tree.sharding`) is the
  documented, minimal deviation; the `_fan_out_data` assertions are untouched.

**VERDICT: PASS**
