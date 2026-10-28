---
id: 225-hf-dataset-uses-global-data-batching
status: pending
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

- [ ] `HuggingFaceDatasetSource` has no `batch_size` / `concurrency` fields; `sources/*.yaml` has none.
- [ ] With `TREE_DATA__BATCH_SIZE=3`, `ingest_arxiv_dataset` over 7 rows calls `load_batch` with
      chunk sizes `[3, 3, 1]`; enrich runs under `app_config.data.concurrency` (unit tests, patched I/O).
- [ ] A source YAML with `batch_size:` on an HF entry fails validation with a message naming the key.
- [ ] Coordinator round-trip test still passes for both `offset=None` and a set offset.
- [ ] Cadence green (memory targets, local env).

## Notes

- ADR-003 (source definitions as operator data) stays valid: what to ingest stays in `sources/`;
  how hard to hit the network moves to app config, like `concurrency:` already is.

## Log

### [Orchestrator] 2026-10-28 — Filed
