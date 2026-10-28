---
id: 223-data-batching-config-and-gather-concurrency-cap
status: done
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

- [x] `app_config.data.batch_size == 500` and `app_config.data.concurrency == 50` from `default.yaml`;
      `TREE_DATA__CONCURRENCY=7` overrides it; a value of `0` fails validation (unit tests).
- [x] `gather_isolated` with 200 items and `concurrency=50` never has more than 50 `work` calls
      in flight (unit test with an in-flight counter + peak assertion), and reaches 50 (the cap is
      used, not serialized).
- [x] Default comes from config: with `concurrency` omitted, the peak equals `app_config.data.concurrency`.
- [x] Existing `tests/unit/data/test_batch.py` and `test_offline_pipeline.py` pass unchanged
      (order, failure count, tally, `None`-drop).
- [x] `concurrency=0` raises `ValueError`.
- [x] Cadence green: `make memory-format-fix && make memory-lint-fix && make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` (local env).

## Notes

- Write tests with `/squid-testing-python`.
- Python ≥3.14 (`apps/memory/pyproject.toml`).

## Log

### [Orchestrator] 2026-10-28 — Filed
Planned with the owner in chat: standardize every data pipeline on batches of 500 with 50
in-flight requests per batch.

### [SWE] 2026-10-28 12:24 — Implementation

**Files modified**
- `apps/memory/src/tree/config/app_config.py` — new `DataConfig` (`batch_size: 500`, `concurrency: 50`, both `ge=1`) on `AppConfig.data`.
- `apps/memory/src/tree/config/default.yaml` — new `data:` block, one-line comment per key saying what it bounds.
- `apps/memory/src/tree/data/batch.py` — `gather_isolated(items, work, *, concurrency: int | None = None)`: per-call `asyncio.Semaphore` around each `work(item)`; `None` reads `app_config.data.concurrency` at call time; `< 1` raises `ValueError` before any coroutine is built. Still one gather, so order, `(successes, failure_count)`, `None`-drop and tally are unchanged. Module and function docstrings now state the cap.
- `apps/memory/tests/unit/config/test_app_config.py` — `TestDataConfig`: default.yaml agrees with typed default (500/50), `TREE_DATA__CONCURRENCY=7` override, `0` rejected for both fields.
- `apps/memory/tests/unit/data/test_batch.py` — `TestGatherIsolatedConcurrencyCap`: in-flight probe (peak == 50 for 200 items at `concurrency=50`), default cap read from config at call time (monkeypatched to 3, peak == `app_config.data.concurrency`), `concurrency` 0 / -1 raises `ValueError` with zero `work` calls.

**Tests**
- Unit: 5226 passing, 0 failing (`make memory-tests`, local env).
- Integration: N/A — no integration suite in this repo.

**Acceptance criteria**
- [x] 500/50 from default.yaml, env override, 0 rejected — `tests/unit/config/test_app_config.py::TestDataConfig`
- [x] ≤ 50 in flight and reaches 50 — `tests/unit/data/test_batch.py::TestGatherIsolatedConcurrencyCap::test_the_peak_in_flight_count_equals_the_cap`
- [x] Default from config — `::test_the_default_cap_is_read_from_config_at_call_time`
- [x] Existing `test_batch.py` and `test_offline_pipeline.py` pass with no edits to the existing tests (only new classes and imports were added).
- [x] `concurrency=0` raises `ValueError` — `::test_a_cap_below_one_raises_before_any_work`
- [x] Cadence green (local env).

**Evidence**
```
$ make env-status
Env target: local (.env)
$ make memory-format-fix && make memory-lint-fix && make memory-format-check && make memory-lint-check && make pre-commit
338 files already formatted / All checks passed! / prettier, ruff check, ruff format, biome: Passed
$ make memory-tests
======================= 5226 passed in 65.56s (0:01:05) ========================

$ uv --directory apps/memory run python -c "from tree.config.app_config import app_config; print(app_config.data)"
batch_size=500 concurrency=50
$ TREE_DATA__CONCURRENCY=7 uv --directory apps/memory run python -c "...print(app_config.data)"
batch_size=500 concurrency=7
$ TREE_DATA__CONCURRENCY=0 uv --directory apps/memory run python -c "from tree.config.app_config import app_config"
data.concurrency
  Input should be greater than or equal to 1 [type=greater_than_equal, input_value=False, input_type=bool]
$ uv --directory apps/memory run python - <<'PY'   # 100,000 items, 1 ms each, every 1000th raises, default cap
import asyncio
from tree.data.batch import gather_isolated
state = {"now": 0, "peak": 0}
async def fetch(i: int) -> int:
    state["now"] += 1; state["peak"] = max(state["peak"], state["now"])
    await asyncio.sleep(0.001); state["now"] -= 1
    if i % 1000 == 0: raise RuntimeError(f"bad {i}")
    return i
ok, failed = asyncio.run(gather_isolated(list(range(100_000)), fetch))
print(f"items=100000 ok={len(ok)} failed={failed} peak_in_flight={state['peak']} ordered={ok == sorted(ok)}")
PY
items=100000 ok=99900 failed=100 peak_in_flight=50 ordered=True
```

**Notes**
- Trade-off: the single gather still creates `len(items)` task objects (O(n) memory for a 100k shard). Only the `work` calls are capped. Splitting into `batch_size` chunks is task 224.
- `frozen_config.yaml` is not mirrored. No test checks section parity against it, and the AC asks for the `default.yaml` values, so `TestDataConfig` loads `_DEFAULT_CONFIG_PATH`.
- `.env.example` is untouched, as the scope asks.
- The `test_batch.py` module docstring still says "under a SINGLE `asyncio.gather`". That is still true (one gather over the list), and the AC asks for existing tests unchanged, so I left it.
- Existing quirk, not changed: `_coerce_env_value` maps `"0"` to `False`, which pydantic reads as 0. A `0` override is still rejected, but the error shows `input_value=False`.
- The unit suite needs local Mongo on 27017. I started Docker Desktop and ran `make local-start` (tree-mongodb, mongot, prefect-server, prefect-worker), and they are still up.
- No call sites changed. Every existing `gather_isolated` caller now runs with at most 50 calls in flight by default.

### [Tester] 2026-10-28 — QA

**Test summary**
- Format / lint / pre-commit: PASS (`make memory-format-check`, `make memory-lint-check`, `make pre-commit`; env target local)
- Unit tests: 5226 passed / 0 failed (`make memory-tests`)
- Integration tests: N/A (no suite in this repo)
- Warnings: 0

**E2E adversarial pass** (scripts run via `uv --directory apps/memory run python`)
- Happy path: `TREE_DATA__CONCURRENCY=2`, 20 items, default cap → results in order, `failures=0`, peak in flight 2 (PASS)
- Break 1 (failure mode, deadlock): 1000 items that all raise, `concurrency=3`, under `wait_for(5s)` → `([], 1000)`, no hang; semaphore released via `async with` (PASS)
- Break 2 (mixed fail / None / order, cap 2): 30 items → `([2,5,...,29], 10)`; order kept, None dropped uncounted (PASS)
- Break 3 (cancellation): cancel outer task while 100 items queued, cap 5 → `CancelledError` propagates, only 5 `work` calls ever started, 0 leftover tasks (PASS)
- Break 4 (boundary): empty list → `([], 0)`; `concurrency=1` → peak 1; `0` / `-1` → `ValueError` before any work (PASS)
- Break 5 (config hostile): `TREE_DATA__CONCURRENCY` = `-5`, `abc`, `1.5` → pydantic ValidationError at load, clear message (PASS)
- Break 6 (import cycle): fresh interpreter `import tree.data.batch` first, then `tree.flow_runs`, `tree.config.app_config`, `tree.data.substack`, `.web`, `.youtube` → all import clean (PASS)
- Callers: 9 `gather_isolated(...)` call sites (substack, web, youtube, huggingface/arxiv) unchanged, full suite green.

**Acceptance criteria**
- [x] PASS — 500/50 from default.yaml, env override 7, 0 rejected — `TestDataConfig` (3 tests) pass; manual env override checked
- [x] PASS — 200 items, cap 50: peak == 50 — `TestGatherIsolatedConcurrencyCap::test_the_peak_in_flight_count_equals_the_cap`
- [x] PASS — default from config — `::test_the_default_cap_is_read_from_config_at_call_time`; manual `TREE_DATA__CONCURRENCY=2` → peak 2
- [x] PASS — existing `test_batch.py` / `test_offline_pipeline.py` unchanged and green (diff is additive only: new imports + new class)
- [x] PASS — `concurrency=0` raises `ValueError` — `::test_a_cap_below_one_raises_before_any_work` (0 and -1)
- [x] PASS — cadence green, local env

**Other issues found (non-blocking)**
- `apps/memory/src/tree/data/batch.py:13` is 119 chars (siblings wrap at ~88): the new docstring paragraph was not re-wrapped. Ruff does not flag it (no E501). Suggest re-wrapping.
- A work coroutine that itself raises `CancelledError` is counted as one failure (gather captures it). Pre-existing behaviour, unchanged.
- `concurrency=True` / `2.5` are not rejected by the `int` type hint (runtime only). Not worth a guard.
- `frozen_config.yaml` not mirrored; nothing requires it (typed default applies).

**VERDICT: PASS**
