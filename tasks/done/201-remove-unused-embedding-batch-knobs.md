---
id: 201-remove-unused-embedding-batch-knobs
status: done
feature: bug-embedding-batching
---

# Remove the two embedding-batching knobs nothing reads: `models.embedding_batch.dispatch_concurrency` and `query.embedding_batch_size`

**Severity:** S4 — config that claims to change behaviour and does not.
**Affected component(s):** `apps/memory/src/tree/memory/embedding_text.py`, `apps/memory/src/tree/config/app_config.py`, `apps/memory/src/tree/config/default.yaml`, `apps/memory/tests/unit/config/fixtures/frozen_config.yaml`, `apps/memory/tests/unit/config/test_app_config.py`, `apps/memory/tests/unit/memory/test_embedding_text.py`, `apps/memory/README.md`
**First observed:** 2026-10-09, embedding-batching e2e.

## Summary

- `models.embedding_batch.dispatch_concurrency` is read and LOGGED by `embed_in_batches`, but the dispatch loop is always sequential (`for start, end in chunks: … await …`), so setting it to 4 changes nothing. No ADR or doc commits to it (only code comments say "ADR-002 §1 … local fan-out seam").
- `query.embedding_batch_size` (64) has no reader at all: only `app_config.py`, `default.yaml`, `frozen_config.yaml` and the README mention it. Query embedding is one text per request.

AGENTS.md: prefer removing over adding. Concurrent dispatch is not needed today (free-tier Voyage at 3 RPM; the `voyage-embeddings` GCL is the throttle), so remove both rather than implement one.

## Reproducer (deterministic)

1. `grep -rn "embedding_batch_size" apps/memory/src` → only the config model.
2. Set `TREE_MODELS__EMBEDDING_BATCH__DISPATCH_CONCURRENCY=4`, run `embed_in_batches` over 3 chunks with a fake model that records overlapping calls → calls never overlap.

Expected: no config key without a behaviour.
Actual: two such keys.

## Out of scope

- Implementing concurrent dispatch.
- The caps themselves (task 198).

## Acceptance criteria

- [x] Both keys removed from `default.yaml`, the Pydantic models, `frozen_config.yaml`, the README config list and the tests that pinned them; the `embed_in_batches` log line and comment no longer mention dispatch concurrency.
- [x] A test pins that the config models no longer accept/define the two keys (or that the frozen-config snapshot matches without them), so they cannot creep back unread.
- [x] Stale `TREE_*` overrides of the removed keys do not crash startup (check how `_apply_env_overrides` treats an unknown key and keep that behaviour; note it in the log).
- [x] Unit suite green, format + lint + pre-commit clean.

## Log

### [SWE] 2026-10-10 00:40 — Implementation

**Files modified**
- `apps/memory/src/tree/config/app_config.py` — dropped `EmbeddingBatchConfig.dispatch_concurrency` (+ its comment) and `QueryConfig.embedding_batch_size`.
- `apps/memory/src/tree/config/default.yaml`, `apps/memory/tests/unit/config/fixtures/frozen_config.yaml` — dropped both keys.
- `apps/memory/src/tree/memory/embedding_text.py` — `embed_in_batches` no longer reads/logs `dispatch_concurrency`; removed the "ADR-002 §1 local fan-out seam" comment. Log line is now `embed_in_batches: N texts -> M request(s) (max_inputs=…, max_total_tokens=…)`.
- `apps/memory/README.md` — removed `embedding_batch_size` from the `query` config list.
- `apps/memory/tests/unit/config/test_app_config.py` — removed the `dispatch_concurrency` asserts in `test_loads_default_yaml` / `test_embedding_batch_defaults_when_absent`, deleted `test_dispatch_concurrency_loaded_from_default_yaml` (its `max_total_tokens` assert is already in `test_loads_default_yaml`), trimmed the `TestExtractionConcurrencyKnobs` docstring, added `TestRemovedEmbeddingBatchKnobs`.
- `apps/memory/tests/unit/memory/test_embedding_text.py` — deleted `TestDispatchConcurrencyDefault` (its `[3, 3, 1]` + global-order check duplicates the existing `max_inputs=3` test at ~:475).

**Tests**
- Unit: 5319 passed, 0 failed — `make memory-tests`.
- Integration: N/A — none exist (AGENTS.md).

**Acceptance criteria**
- [x] Both keys removed everywhere — repo grep (excluding `.git`, `tasks/done/`) hits only this task file and the new pinning test.
- [x] Pinned — `tests/unit/config/test_app_config.py::TestRemovedEmbeddingBatchKnobs::test_models_no_longer_define_the_removed_knobs` (asserts neither name is in `model_fields`). Red before the removal, green after.
- [x] Stale overrides do not crash — `TestRemovedEmbeddingBatchKnobs::test_stale_yaml_keys_and_env_overrides_are_ignored` (stale YAML keys + `TREE_MODELS__EMBEDDING_BATCH__DISPATCH_CONCURRENCY=4` + `TREE_QUERY__EMBEDDING_BATCH_SIZE=32` → load succeeds, neither attribute exists).
- [x] Unit suite green, format + lint + pre-commit clean.

**How a stale key behaves (unchanged mechanism, no code added)**
- `_apply_env_overrides` does no schema check: it writes any `TREE_A__B__C` into the raw dict (`raw["models"]["embedding_batch"]["dispatch_concurrency"] = 4`).
- `EmbeddingBatchConfig`, `QueryConfig`, `ModelsConfig` and `AppConfig` use pydantic's default `extra="ignore"`, so `AppConfig.model_validate` drops the unknown key silently — for both a stale env var and a stale YAML key. Only `ClusteringConfig` and the `modal.*` models set `extra="forbid"`; neither is touched here. `forbid` was deliberately NOT added: it would turn a leftover env var into a startup crash.

**Evidence**
```
$ make memory-format-fix && make memory-lint-fix && make memory-format-check && make memory-lint-check
341 files already formatted
All checks passed!
$ make pre-commit
prettier......Passed  ruff check......Passed  ruff format......Passed  biome check (harness)......Passed
$ make memory-tests
======================= 5319 passed in 61.02s (0:01:01) ========================
```

**Notes**
- e2e (`run-pipelines-e2e`) NOT RUN — config/log-line removal only; behaviour is unchanged (dispatch was already sequential). No `modal` command, no deploy, no commit.


### [Tester] 2026-10-10 00:50 — QA

**Test summary**
- Format / lint / pre-commit: PASS (`341 files already formatted`, `All checks passed!`, prettier / ruff check / ruff format / biome all `Passed`)
- Unit tests: 5319 passed / 0 failed (`make memory-tests`, env target local)
- Integration tests: N/A (none exist, per AGENTS.md)
- Warnings: 0 in the pytest summary line

**Pinning tests are red on the old config**
- `git stash push -- apps/memory/src/tree/config/app_config.py apps/memory/src/tree/config/default.yaml`, then run `TestRemovedEmbeddingBatchKnobs`: **2 failed** (`test_models_no_longer_define_the_removed_knobs`; `test_stale_yaml_keys_and_env_overrides_are_ignored` → `EmbeddingBatchConfig(..., dispatch_concurrency=4)`). After `git stash pop` the stash list is empty and the working tree is unchanged.

**The SWE's "covered elsewhere" claim holds**
- `test_dispatch_concurrency_loaded_from_default_yaml`'s other assert (`max_total_tokens == 10_000` on the frozen config) is still in `test_loads_default_yaml` (test_app_config.py:72).
- `TestDispatchConcurrencyDefault`'s `[3, 3, 1]` + global-order check on `max_inputs=3` is the same as `test_vectors_returned_in_input_order_across_chunks` (test_embedding_text.py:470-482).

**E2E adversarial pass**
- Happy path: `TREE_MODELS__EMBEDDING_BATCH__DISPATCH_CONCURRENCY=4 TREE_QUERY__EMBEDDING_BATCH_SIZE=32 make memory-search QUERY="context engineering" TOP_K=1` → exit 0, `search_mode=hybrid` (so the query embed ran), `No results.`. This matches the run without the stale vars exactly. Read-only on local `tree`. (PASS)
- Startup with stale vars: `make memory-serve-workflows` with the same two vars → `Your deployments are being served and polling for scheduled runs!` with all 5 deployments listed and no traceback. Stopped with `pkill -f tree.orchestrator`. Before serving I checked that no orchestrator was running and that all SCHEDULED runs were 5+ hours away, so the serve could not pick up a run. `tree-prefect-worker` was `Up 28 minutes` before and after. (PASS)
- Override path is live and stale keys are dropped: with the stale vars plus `TREE_MODELS__EMBEDDING_BATCH__MAX_INPUTS=7`, `app_config.models.embedding_batch.model_dump()` → `{'max_inputs': 7, 'max_total_tokens': 10000, 'max_input_tokens': 32000}`. The sibling override applies and the stale key does not appear. (PASS)
- Malformed stale value: `TREE_MODELS__EMBEDDING_BATCH__DISPATCH_CONCURRENCY=not-an-int TREE_QUERY__EMBEDDING_BATCH_SIZE=` → config loads (`ok`), because the key is dropped before validation. (PASS)
- Log line format: ran `embed_in_batches` over 7 texts (`max_inputs=3`) with `logging.raiseExceptions=True` → `embed_in_batches: 7 texts -> 3 request(s) (max_inputs=3, max_total_tokens=10000)`. That is 4 placeholders for 4 args, with no `Logging error`. Calls `[3, 3, 1]`, vectors in global order. (PASS)
- Other places that could set the keys: a repo-wide grep (excluding `.git`/`.venv`/`node_modules`, dotfiles included) hits only the pinning test, this task file and `tasks/done/140`/`198` (history). Nothing in the Makefile, `.github/workflows/{ci,cd}.yml`, `apps/memory/deploy/*`, `.env`, `.env.prod`, `.env.example` or `.envrc`, and no such var is in the shell env. (PASS)

**Acceptance criteria**
- [x] PASS — Both keys removed from yaml/models/frozen fixture/README/tests; the log line and comment no longer mention dispatch concurrency. Evidence: diff + grep above; embedding_text.py:148-155.
- [x] PASS — A test pins the removal. Evidence: test_app_config.py:566 `TestRemovedEmbeddingBatchKnobs`, red on the stashed old config and green now.
- [x] PASS — Stale `TREE_*` overrides do not crash startup. Evidence: `_apply_env_overrides` (app_config.py:1135) has no schema check, the models use pydantic's default `extra="ignore"`, and the live search and serve both ran with the stale vars.
- [x] PASS — Unit suite green; format, lint and pre-commit clean (see above).

**Other issues found**
- None blocking. Note: a stale key is dropped silently, with no warning log. This is the intended behaviour and matches every other non-`forbid` model.
- ADR check: `docs/adrs/002_pipeline_concurrency_and_voyage_rate_limiting.md` has no section on a local dispatch-concurrency knob. Searching it for "concurren", "fan", "seam", "local" and "serial" finds only cross-flow GCL, `doc_concurrency`/`dedup_concurrency` and an unrelated "no local semaphore" line. No doc is left stale.
- Side effects of the live checks (expected, no `tree` writes): the serve re-registered the 5 deployments on the LOCAL Prefect server, and the search run's Opik client rewrote `~/.opik.config`. The second is existing behaviour, not caused by this change.
- `code-review` plugin gate: the sibling `code-review` agent in this session is running it, so this PASS depends on its findings.

**VERDICT: PASS**
