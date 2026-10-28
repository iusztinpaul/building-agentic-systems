---
id: 225-hf-dataset-uses-global-data-batching
status: done
feature: data-batching
---

# Drop the per-source `batch_size` / `concurrency` fields from `HuggingFaceDatasetSource`; the arXiv pipeline reads `app_config.data`

Tags: `memory`, `data`, `config`, `huggingface`
Depends on: 223
Blocks: 226
Implements: one place to tune data batching (owner decision 2026-10-28: 500 / 50 everywhere).

## Problem

HF is the only pipeline with its own batching knobs, authored per source in YAML
(`HuggingFaceDatasetSource.batch_size = 50`, `concurrency = 10`; `sources/backfill.yaml` sets
50 / 25). After task 223 that is a second, divergent place to tune the same thing. Bad: two
batch sizes for one pipeline layer. Good: `data.batch_size` / `data.concurrency` govern every
platform.

## Scope

1. Remove `batch_size` and `concurrency` from `HuggingFaceDatasetSource`
   (`apps/memory/src/tree/config/sources.py`) and from its docstring and the
   `build_uri_sources` error text ("tuning fields (max_samples/batch_size/num_workers/concurrency)").
   Keep `max_samples`, `fetch_content`, `num_workers`, `offset`.
2. `ingest_arxiv_dataset` (`data/huggingface/arxiv_dataset_pipeline.py`) uses
   `app_config.data.batch_size` for `_fetch_dataset_batches` and `app_config.data.concurrency`
   for `enrich_batch`'s semaphore; update its "Every knob … comes from the dispatched entry"
   docstring accordingly.
3. Delete `batch_size: 50` / `concurrency: 25` from `sources/backfill.yaml`.
4. Decide and test what a stale YAML still carrying those keys does: it must fail loudly
   (`extra="forbid"` on the model) rather than be silently ignored — check this does not break
   the coordinator's `model_dump()` → JSON → `TypeAdapter(list[SourceEntry])` round-trip.
5. Update `tests/unit/config/test_sources_config.py` (lines asserting `batch_size == 50` /
   `concurrency == 10`) and `test_app_config.py::test_discriminated_union_round_trip_preserves_window_fields`.

## Out of scope

- `num_workers` / `offset` window fan-out (unchanged).

## Acceptance criteria

- [x] `HuggingFaceDatasetSource` has no `batch_size` / `concurrency` fields; `sources/*.yaml` has none.
- [x] With `TREE_DATA__BATCH_SIZE=3`, `ingest_arxiv_dataset` over 7 rows calls `load_batch` with
      chunk sizes `[3, 3, 1]`; enrich runs under `app_config.data.concurrency` (unit tests, patched I/O).
- [x] A source YAML with `batch_size:` on an HF entry fails validation with a message naming the key.
- [x] Coordinator round-trip test still passes for both `offset=None` and a set offset.
- [x] Cadence green (memory targets, local env).

## Notes

- ADR-003 (source definitions as operator data) stays valid: what to ingest stays in `sources/`;
  how hard to hit the network moves to app config, like `concurrency:` already is.

## Log

### [Orchestrator] 2026-10-28 — Filed

### [SWE] 2026-10-28 15:05 — Implementation

**Files modified**
- `apps/memory/src/tree/config/sources.py` — dropped `batch_size` / `concurrency` from `HuggingFaceDatasetSource`; added `model_config = ConfigDict(extra="forbid")`; docstring + `build_uri_sources` docstring/error text now name `max_samples/fetch_content/num_workers`.
- `apps/memory/src/tree/data/huggingface/arxiv_dataset_pipeline.py` — `ingest_arxiv_dataset` reads `app_config.data.batch_size` (for `_fetch_dataset_batches`) and `app_config.data.concurrency` (for `enrich_batch`) inside the flow body, at call time; flow docstring + module comment updated. `enrich_batch(docs, concurrency)` signature unchanged.
- `sources/backfill.yaml` — removed `batch_size: 50` / `concurrency: 25` from the arxiv entry.
- `apps/memory/README.md` — HF tuning keys are now `max_samples`, `fetch_content`, `num_workers`; batching points at the `data:` block.
- `apps/memory/tests/unit/config/test_sources_config.py` — dropped old-default asserts; new `test_huggingface_dataset_rejects_removed_batching_key[batch_size|concurrency]` + `test_huggingface_dataset_rejects_misspelled_key`.
- `apps/memory/tests/unit/config/test_app_config.py` — round-trip test no longer sets/asserts the removed fields (both `offset=None` and `offset=250` cases kept).
- `apps/memory/tests/unit/data/test_arxiv_window_entries.py` — dropped the removed fields from the copy-preservation test.
- `apps/memory/tests/unit/config/test_sources.py` — new `TestLoadSources::test_stale_huggingface_batching_key_fails_naming_the_key` (a real stale YAML file through `load_sources`).
- `apps/memory/tests/unit/data/huggingface/test_arxiv_dataset_pipeline.py` — regression test renamed `test_takes_source_knobs_from_the_dispatched_entry` (max_samples/offset off the entry, batch_size off patched `app_config.data`); new `TestIngestArxivDatasetUsesDataConfig` (env `[3, 3, 1]` chunking through the real chunker + `enrich_batch` gets `data.concurrency`).

**Scope item 4 — why `extra="forbid"`, not a targeted validator**
- Safe for the coordinator round-trip: `model_dump()` emits only declared fields, so `offline_pipeline.py` shards → JSON → `_SOURCES_ADAPTER` never carry extras (round-trip test green for `offset=None` and `offset=250`).
- Safe for untyped-entry inference: `_normalize_untyped_entry` only adds `type`, and inference never yields HF; `build_uri_sources` passes only `uri`/`type`. `arxiv_window_entries` uses `model_copy(update=...)` (no re-validation).
- The error names the key: `sources.0.huggingface_dataset.batch_size  Extra inputs are not permitted [type=extra_forbidden]`.
- It is one native line (the pattern `app_config.py` already uses 4x) versus a custom validator for two names that would silently accept the next stale key. Bonus: a typo like `max_sample: 10000` now fails instead of silently ingesting 10 rows (tested).
- Scoped to `HuggingFaceDatasetSource` only; the other variants carry only `type`/`uri` and are unchanged.

**Tests**
- Unit: 5246 passed, 0 failed — `make memory-tests` (local env).
- Red first: the 6 new/rewritten tests failed for the right reasons before the change (`(250, 50) == (250, 7)`, `[7] == [3, 3, 1]`, `10 == 2`, 3x `DID NOT RAISE`).
- Integration: N/A — no integration suite (AGENTS.md).

**Acceptance criteria**
- [x] No `batch_size`/`concurrency` on `HuggingFaceDatasetSource` (`test_huggingface_dataset_rejects_removed_batching_key`); none in `sources/*.yaml` (`grep -rn "batch_size\|concurrency" sources/` → no match; the committed backfill loads under `forbid` — Evidence below). The unit suite deliberately loads fixtures, never the committed files (`test_sources.py` header), so the committed-file check is the smoke, not a test.
- [x] `TREE_DATA__BATCH_SIZE=3` → `load_batch` chunk sizes `[3, 3, 1]` over 7 rows — `test_arxiv_dataset_pipeline.py::TestIngestArxivDatasetUsesDataConfig::test_env_batch_size_chunks_seven_rows_into_3_3_1`; enrich under `data.concurrency` — `::test_enrich_batch_runs_under_data_concurrency`.
- [x] Stale HF `batch_size:` fails validation naming the key — `test_sources.py::TestLoadSources::test_stale_huggingface_batching_key_fails_naming_the_key` (YAML file) + `test_sources_config.py::TestValidationErrors::test_huggingface_dataset_rejects_removed_batching_key` (both keys).
- [x] Coordinator round-trip (`offset=None` and a set offset) — `test_app_config.py::...::test_discriminated_union_round_trip_preserves_window_fields`.
- [x] Cadence green (local env).

**Evidence**
```
$ make env-status
Env target: local (.env)
$ make memory-format-fix && make memory-lint-fix && make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests
338 files left unchanged / All checks passed! / 338 files already formatted / All checks passed!
prettier / ruff check / ruff format / biome check (harness) ... Passed
======================= 5246 passed in 64.07s (0:01:04) ========================

# Committed backfill loads; a stale YAML fails loudly
backfill HF entry: {'type': 'huggingface_dataset', 'uri': 'librarian-bots/arxiv-metadata-snapshot', 'max_samples': 10000, 'fetch_content': False, 'num_workers': 2, 'offset': None}
ValidationError 1 validation error for SourcesConfig
sources.0.huggingface_dataset.batch_size
  Extra inputs are not permitted [type=extra_forbidden, input_value=50, input_type=int]

# Live arxiv flow, run in-process from this worktree (real HF stream, local Mongo, fetch_content=true)
$ TREE_DATA__BATCH_SIZE=3 TREE_DATA__CONCURRENCY=2 python -c "... ingest_arxiv_dataset(HuggingFaceDatasetSource(max_samples=7, fetch_content=True, offset=7), user) ..."
app_config.data batch_size=3 concurrency=2
Streaming arxiv dataset from librarian-bots/arxiv-metadata-snapshot (offset=7, max_samples=7, batch_size=3)
Yielding batch of 3 entries (total so far: 3)      -> Batch processed: 3 ingested so far
Yielding batch of 3 entries (total so far: 6)      -> Batch processed: 6 ingested so far
Yielding final batch of 1 entries (total: 7)       -> Batch processed: 7 ingested so far
Ingested 7 new arxiv documents
Prefect flow run c6972b86-1b9d-42d7-9809-3d952a742f82 (white-gerbil): Completed;
task runs: 3x enrich-arxiv-batch + 3x load-arxiv-batch, all Completed
```

**Notes**
- Deployment-path e2e caveat: `make memory-run-data-pipeline SOURCE_FILE=<7-row HF yaml>` (offline run 652f1eef…) completed, but its `data-etl-worker` child (7c95f412…) was claimed by the Docker `tree-prefect-worker` runner, which executes main-branch code (the child's `ingest-arxiv-dataset-etl` params still showed `batch_size: 50, concurrency: 10`, 1 enrich + 1 load). That run proves nothing about this branch, so I ran the flow in-process from the worktree instead (above) rather than stopping shared infra. The full deployment-path chunking e2e belongs to task 226.
- Deploy-skew: once this ships, a shard dumped by an OLD coordinator (still carrying `batch_size`/`concurrency`) and re-parsed by a NEW worker fails loudly (`extra_forbidden`). CD updates every deployment together, so only runs created before the deploy and executed after it are affected. The reverse case (new coordinator → old worker) works because the old model just applies its defaults.
- Read-only docs still naming the HF per-source knobs (not edited by me, for PA / task 226): `docs/glossary.md:11` (**Batch**: "a streaming chunk of `batch_size` rows (HuggingFace) … under `asyncio.Semaphore(concurrency)`") and `docs/adrs/003_source_definitions_as_operator_data.md:142` ("needs `max_samples`/`batch_size`/`num_workers`/`concurrency`"). Task 226 covers ADR-002:425 but does not obviously cover these two.
- Seven arxiv docs (rows 7–13, `fetch_content=true`) plus seven from the main-code run (rows 0–6) now sit in local Mongo for user `6ad34350d22e8f7ce231b861`. They are local only and deduped on re-run.

### [Tester] 2026-10-28 — QA

**Test summary**
- Format / lint / pre-commit: PASS (`make memory-format-check`, `memory-lint-check`, `pre-commit` all exit 0; 338 files formatted)
- Unit tests: 5246 passed / 0 failed (`make memory-tests`, env target local)
- Integration tests: N/A (no suite, per AGENTS.md)
- Warnings: 0

**E2E adversarial pass** (script: scratchpad `scratch225/adv.py`, run via `uv run`)
- Happy path: `load_sources(["sources/backfill.yaml"/"listen"/"light"])` → 23/3/10 entries, HF entry = `{max_samples: 10000, fetch_content: False, num_workers: 2, offset: None}`; backfill partitioned → `model_dump()` → `json.dumps` → `_SOURCES_ADAPTER` round-trips (shards of 10/3/9/1/1). PASS. `TREE_DATA__BATCH_SIZE=3` loads `batch_size=3, concurrency=50`. PASS.
- Break 1 (stale keys): HF YAML with `batch_size: 50`, `concurrency: 25`, `batch_size: null`, `Batch_Size: 5` → `ValidationError ... sources.0.huggingface_dataset.<key> Extra inputs are not permitted [extra_forbidden]` each time, key named. PASS.
- Break 2 (typos / malformed): `max_sample: 5` rejected naming the key; `type: huggingface_datasets` → union_tag_invalid; `num_workers: 0` → ge=1 error. PASS.
- Break 3 (deploy skew): old-coordinator shard dict (with `batch_size`/`concurrency`) via `_coerce_sources` → fails loudly with extra_forbidden; same shard minus the keys parses. Behaves as the SWE described. PASS (see exposure assessment below).
- Break 4 (hostile env values): `TREE_DATA__BATCH_SIZE=0|-1|abc`, `TREE_DATA__CONCURRENCY=0` → `AppConfig` ValidationError at load (ge=1 / int parsing), so no zero-size chunking or `Semaphore(0)` hang. PASS.
- Break 5 (--uri): `build_uri_sources([(hf_id, "huggingface_dataset")])` → ValueError with updated text `(max_samples/fetch_content/num_workers)`. PASS.

**Acceptance criteria**
- [x] PASS — no `batch_size`/`concurrency` on HF model; none in `sources/*.yaml` — `grep -rn "batch_size\|concurrency" sources/` empty; `sources.py` diff; all three committed YAMLs load under `forbid`.
- [x] PASS — `TREE_DATA__BATCH_SIZE=3` over 7 rows → `[3, 3, 1]`; enrich gets `data.concurrency` — `TestIngestArxivDatasetUsesDataConfig` (2 tests) pass in the 5246.
- [x] PASS — stale `batch_size:` on HF entry fails naming the key — `test_sources.py::test_stale_huggingface_batching_key_fails_naming_the_key`, `test_sources_config.py::test_huggingface_dataset_rejects_removed_batching_key[batch_size|concurrency]`, plus my manual runs above.
- [x] PASS — coordinator round-trip for `offset=None` and `offset=250` — `test_app_config.py::TestHuggingFaceWindowFields`; plus my backfill JSON round-trip.
- [x] PASS — cadence green, local env.

**Blast radius (all paths audited)**
- `HuggingFaceDatasetSource` / `SourceEntry` are built/validated only in: `load_sources` (YAML), `_SOURCES_ADAPTER` in `offline_pipeline.py` (`_coerce_sources`, `_resolve_source_set`), `build_uri_sources` (rejects HF), `arxiv_window_entries` (`model_copy`, no re-validation). `scripts/run_data_pipeline.py` / `run_pipeline.py` only `model_dump()` non-HF entries from `--uri`. No other reader of `entry.batch_size/concurrency` in `src/`, `scripts/`, `deploy/`.
- `orchestrator.py` schedules carry only `source_files=["sources/listen.yaml"]` (paths, re-loaded each run; listen has no HF). No deployment carries HF entry params. Online/MCP path uses a separate `_ONLINE_SOURCE_ADAPTER` with no HF.
- `data_etl_worker` has no `retries`, so a Prefect auto-retry cannot replay old params.

**Deploy-skew exposure: acceptable, no mitigation needed.** Shards are produced at coordinator runtime and consumed seconds later by `run_deployment`; they are never persisted in config. Only a worker run created by the old coordinator and executed after the new code lands (or a manual re-run of an old failed worker run from the UI) hits `extra_forbidden`, and it fails loudly on one shard, not silently. The coordinator is not auto-retried. Optional note for the release: avoid deploying during a nightly/backfill run.

**Stale references (confirmed, task 226 / PA, not blocking)**: `docs/glossary.md:11` and `docs/adrs/003_source_definitions_as_operator_data.md:142`, plus `docs/adrs/002_pipeline_concurrency_and_voyage_rate_limiting.md:425` ("HuggingFace `batch_size`-chunk", already covered by task 226). No other leftovers in code, tests, `sources/`, `.agents/`, `tutorials/`, README.

**Other issues found (notes, non-blocking)**
- Pre-existing: the other variants (`WebSource` etc.) have no `extra="forbid"`, so `type: web` + `batch_size: 5` is silently ignored, and an untyped entry with stray keys silently becomes `WebSource`. Out of scope; inconsistent with the new HF strictness, could be a follow-up.
- A stale source file is rejected inside the remote coordinator run (not client-side in `run_data_pipeline.py`); loud but late. Pre-existing for any validation error.
- New/edited docstring and comment lines are all ≤88 chars; the 89-char lines in `arxiv_dataset_pipeline.py` (164, 197, 198) and `sources.py:189` are pre-existing and untouched.

**VERDICT: PASS**
