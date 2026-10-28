---
id: 223-data-batching-config-and-gather-concurrency-cap
status: pending
feature: data-batching
---

# Add a `data:` config block (`batch_size: 500`, `concurrency: 50`) and cap `gather_isolated` at `data.concurrency` in-flight elements

Tags: `memory`, `data`, `config`, `concurrency`
Depends on: None
Blocks: 224, 225, 226
Implements: ADR-002 (pipeline concurrency) — bounded in-flight I/O inside one data worker.

## Problem

`tree.data.batch.gather_isolated` starts EVERY element of its list under one unbounded
`asyncio.gather`. Every non-HF data step routes through it (Substack feed resolve / article
scrape / load, Web scrape / load, YouTube feed resolve / oEmbed / load), so a shard of 100,000
Substack articles fires 100,000 concurrent HTTP requests — each on its own `httpx.AsyncClient`,
with no 429 handling in `substack.py`. Bad: in-flight requests = `len(items)`. Good: in-flight
requests ≤ 50 whatever the shard size.

## Scope

1. New `data:` section in `apps/memory/src/tree/config/default.yaml` + a Pydantic `DataConfig` on
   `AppConfig` (`apps/memory/src/tree/config/app_config.py`), both fields `int` with `ge=1`:
   - `batch_size: 500` — items per ETL batch (the unit of memory, retry and Prefect task run;
     consumed by tasks 224/225).
   - `concurrency: 50` — max in-flight requests inside one batch.
   Each key gets a one-line YAML comment saying what it bounds (good: "max in-flight requests
   inside one batch"; bad: "tuning knob"). The `TREE_DATA__BATCH_SIZE` / `TREE_DATA__CONCURRENCY`
   escape hatch comes free via `_apply_env_overrides`; do NOT add them to `.env.example`.
   Mirror the block in `tests/unit/config/fixtures/frozen_config.yaml` if that fixture requires it.
2. `gather_isolated(items, work, *, concurrency: int | None = None)`: wrap each `work(item)` in an
   `asyncio.Semaphore(concurrency)`; `None` → `app_config.data.concurrency` (read the same way
   `brightdata_transcript_fetcher.py` reads `app_config`). `concurrency < 1` raises `ValueError`
   (same shape as `embed_in_batches`). Still ONE gather over the list, so result order, the
   `(successes, failure_count)` return, `None`-drop semantics and the `ItemFailureTally` are
   unchanged. No call site changes in this task.
3. Rewrite the `batch.py` module docstring's contract ("every element runs under a SINGLE
   `asyncio.gather`") to state the cap.

## Out of scope

- Splitting flows into `batch_size` chunks (task 224) and the HF source fields (task 225).
- `GeminiTranscriptFetcher`'s own `Semaphore(3)` — an LLM-cost bound, not an I/O fan-out bound.

## Acceptance criteria

- [ ] `app_config.data.batch_size == 500` and `app_config.data.concurrency == 50` from `default.yaml`;
      `TREE_DATA__CONCURRENCY=7` overrides it; a value of `0` fails validation (unit tests).
- [ ] `gather_isolated` with 200 items and `concurrency=50` never has more than 50 `work` calls
      in flight (unit test with an in-flight counter + peak assertion), and reaches 50 (the cap is
      used, not serialized).
- [ ] Default comes from config: with `concurrency` omitted, the peak equals `app_config.data.concurrency`.
- [ ] Existing `tests/unit/data/test_batch.py` and `test_offline_pipeline.py` pass unchanged
      (order, failure count, tally, `None`-drop).
- [ ] `concurrency=0` raises `ValueError`.
- [ ] Cadence green: `make memory-format-fix && make memory-lint-fix && make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` (local env).

## Notes

- Write tests with `/squid-testing-python`.
- Python ≥3.14 (`apps/memory/pyproject.toml`).

## Log

### [Orchestrator] 2026-10-28 — Filed
Planned with the owner in chat: standardize every data pipeline on batches of 500 with 50
in-flight requests per batch.
