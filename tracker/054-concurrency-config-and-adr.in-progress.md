# Concurrency config scaffolding + ADR-002 + sync-limits target

Status: pending
Tags: `infra`, `config`, `docs`
Depends on: None
Blocks: #055, #056, #057, #058, #059

## Scope

Land the configuration and documentation scaffolding the rest of the feature reads.
No runtime behavior change beyond the `max_total_tokens` cap drop (which only makes
embed requests smaller).

- **Typed config (`apps/memory/src/tree/config/app_config.py`):**
  - New `ConcurrencyConfig(BaseModel)` mirroring `DreamConfig`/`QueryConfig` shape with
    fields: `voyage_rpm: int = 3`, `voyage_tpm: int = 10000`,
    `runner_global_limit: int = 4`, `fanout_max_parallel: int = 4`.
  - Wire `concurrency: ConcurrencyConfig = ConcurrencyConfig()` onto the top-level
    `AppConfig`, loaded from the `concurrency:` YAML block.
  - On `ExtractionConfig`: add `doc_concurrency: int = 1` and `dedup_concurrency: int = 8`
    (both inherit `TREE_EXTRACTION__*` env overrides via `_apply_env_overrides`).
  - On `EmbeddingBatchConfig`: add `dispatch_concurrency: int = 1` (YAML-only) and
    change `max_total_tokens` default 320_000 → 10_000.
- **YAML (`apps/memory/configs/default.yaml`):**
  - New top-level `concurrency:` block with the four keys above and explanatory comments.
  - Under `extraction:`: `doc_concurrency: 1`, `dedup_concurrency: 8`.
  - Under `models.embedding_batch:`: `dispatch_concurrency: 1` and `max_total_tokens: 10000`.
- **Make target (`apps/memory/Makefile`):** `sync-concurrency-limits` that issues
  `prefect gcl create voyage-embeddings --limit <rpm> --slot-decay-per-second <rpm/60>`
  (or `update` if it exists) deriving `<rpm>` from `app_config.concurrency.voyage_rpm`.
  Run via a tiny script (e.g. `scripts/sync_concurrency_limits.py`) so the value is
  read from config, not hardcoded; the script must `init_logger()` at module level.
- **ADR (`docs/adrs/002_pipeline_concurrency_and_voyage_rate_limiting.md`):** author the
  Accepted ADR-002 per the grooming output (cross-flow limiter primitive, document-shard
  fan-out axis, accepted same-user write-interleaving). Follow the `ADR-001` header format.

## Acceptance Criteria

- [x] `ConcurrencyConfig` exists with the four typed fields and correct defaults;
      `app_config.concurrency.voyage_rpm == 3` and `.fanout_max_parallel == 4` load from YAML.
- [x] `app_config.extraction.doc_concurrency == 1` and `.dedup_concurrency == 8` load from YAML.
- [x] `app_config.models.embedding_batch.dispatch_concurrency == 1` and
      `.max_total_tokens == 10000` (NOT 320000) load from YAML.
- [x] `TREE_EXTRACTION__DEDUP_CONCURRENCY=4` (env override) is reflected in
      `app_config.extraction.dedup_concurrency` — proves the override hatch works for the new knobs.
- [x] `docs/adrs/002_pipeline_concurrency_and_voyage_rate_limiting.md` exists, Status `Accepted`,
      and topically covers the limiter, fan-out axis, and write-interleaving tradeoff.
- [x] `make memory-sync-concurrency-limits` runs without error against a started stack and
      `prefect gcl ls` (or `prefect gcl inspect voyage-embeddings`) shows the limit with
      `limit=3` and a non-zero slot-decay-per-second (~0.05).
- [x] `make memory-format-fix && make memory-lint-fix && make memory-format-check && make memory-lint-check && make pre-commit` clean.
- [x] `make memory-unit-tests` passes (new unit test asserts config defaults + env-override).
- [x] `make memory-integration-tests` (fast) shows no regression from the
      `max_total_tokens` drop — existing `test_embedding_batching.py` /
      `test_e2e_embedding_split_and_batching.py` still pass (more, smaller requests is expected).

## User Stories

### Story: Operator inspects the new concurrency knobs
1. Operator opens `apps/memory/configs/default.yaml`.
2. Sees a `concurrency:` block with `voyage_rpm: 3`, `voyage_tpm: 10000`,
   `runner_global_limit: 4`, `fanout_max_parallel: 4` and inline comments.
3. Runs `uv --directory apps/memory run python -c "from tree.config.app_config import app_config; print(app_config.concurrency)"`.
4. Sees the typed `ConcurrencyConfig` printed with those values.

### Story: Operator creates the server-side Voyage limit from config
1. Operator runs `make local-start`.
2. Operator runs `make memory-sync-concurrency-limits`.
3. Operator runs `prefect gcl ls` (from `apps/memory/`).
4. Sees `voyage-embeddings` with `limit 3` and a slot decay ~0.05/s.

### Story: Operator overrides a knob for a one-off run
1. Operator exports `TREE_EXTRACTION__DEDUP_CONCURRENCY=4`.
2. Loads `app_config`.
3. `app_config.extraction.dedup_concurrency` reads `4`, not the YAML default `8`.

### Story: Engineer reads ADR-002 before implementing the limiter
1. Engineer opens `docs/adrs/002_pipeline_concurrency_and_voyage_rate_limiting.md`.
2. Reads that the limiter is a Prefect GCL with slot decay, wrapped at
   `_embed_chunk_resilient`, with `strict=False`.
3. Reads the accepted same-user write-interleaving tradeoff and the lease-leak risk.

---

Blocked by: (none)

## Log

### [SWE] 2026-05-21 16:30 — Implementation

**Files modified**
- `apps/memory/src/tree/config/app_config.py` — added `ConcurrencyConfig` (4 typed fields, wired onto `AppConfig.concurrency`); added `doc_concurrency`/`dedup_concurrency` to `ExtractionConfig`; added `dispatch_concurrency` and dropped `max_total_tokens` 320_000 → 10_000 on `EmbeddingBatchConfig`.
- `apps/memory/configs/default.yaml` — new top-level `concurrency:` block (4 commented keys); `extraction.doc_concurrency: 1` + `dedup_concurrency: 8`; `models.embedding_batch.dispatch_concurrency: 1` + `max_total_tokens: 10000`.
- `apps/memory/scripts/sync_concurrency_limits.py` — NEW. Reads `app_config.concurrency.voyage_rpm`, issues `prefect gcl create`/`update voyage-embeddings --limit <rpm> --slot-decay-per-second <rpm/60>` (create-if-absent else update). Calls `init_logger()` at module level.
- `apps/memory/Makefile` — new `sync-concurrency-limits` target (reachable as `make memory-sync-concurrency-limits` via the root auto-delegation).
- `apps/memory/tests/unit/config/test_app_config.py` — new `TestConcurrencyConfig` + `TestExtractionConcurrencyKnobs` (YAML defaults, missing-block fallback, custom YAML, `TREE_EXTRACTION__DEDUP_CONCURRENCY=4` + `__DOC_CONCURRENCY` overrides); updated two pre-existing assertions for the `max_total_tokens` drop + new fields.
- `apps/memory/tests/unit/test_sync_concurrency_limits.py` — NEW. Command construction (`_build_command`), create-vs-update branch, non-zero-exit → `SystemExit(1)`, Click entrypoint.

**ADR**
- `docs/adrs/002_pipeline_concurrency_and_voyage_rate_limiting.md` — pre-written during grooming; verified Status `Accepted` and that it covers the cross-flow GCL limiter (slot decay, `_embed_chunk_resilient`, `strict=False`), the document-shard fan-out axis, and the accepted same-user write-interleaving tradeoff + lease-leak risk. NOT rewritten (read-only for SWE).

**Tests**
- Unit: 1340 passing, 0 failing (10 new across the two test files) — output below.
- Integration (fast): 152 passed, 1 skipped, 107 deselected. One unrelated failure: `test_web_serp.py::test_empty_query_returns_empty_list` (live Bright Data SERP returned unrelated YouTube results for a nonsense query — network-flaky, no relation to config/embedding changes).
- Named batching tests (slow + 1 requires_mongot, run in isolation on the quiesced stack): `test_embedding_batching.py` + `test_e2e_embedding_split_and_batching.py` → 4 passed with `max_total_tokens=10000`.

**Acceptance criteria**
- [x] `ConcurrencyConfig` 4 fields + YAML load — `tests/unit/config/test_app_config.py::TestConcurrencyConfig`
- [x] `extraction.doc_concurrency==1` / `dedup_concurrency==8` from YAML — `TestExtractionConcurrencyKnobs::test_extraction_fanout_knobs_loaded_from_default_yaml`
- [x] `embedding_batch.dispatch_concurrency==1` / `max_total_tokens==10000` — `TestExtractionConcurrencyKnobs::test_dispatch_concurrency_loaded_from_default_yaml`
- [x] `TREE_EXTRACTION__DEDUP_CONCURRENCY=4` override — `TestExtractionConcurrencyKnobs::test_dedup_concurrency_env_override` + verified at runtime (printed `dedup_concurrency = 4`)
- [x] ADR-002 exists, Accepted, covers limiter/fan-out/write-interleaving — verified
- [x] `make memory-sync-concurrency-limits` against the running stack → GCL `limit=3`, `slot_decay_per_second=0.05` (verified via `prefect gcl inspect`); re-run is idempotent (update branch)
- [x] format/lint/pre-commit clean
- [x] `make memory-unit-tests` passes
- [x] fast integration: no regression from the `max_total_tokens` drop

**Evidence**

```
$ make memory-unit-tests
... tests/unit/test_sync_concurrency_limits.py ......                      [100%]
============================ 1340 passed in 46.53s =============================

$ make memory-format-fix && make memory-lint-fix && make memory-format-check && make memory-lint-check
261 files left unchanged
All checks passed!
261 files already formatted
All checks passed!

$ make pre-commit
prettier................Passed
ruff check..............Passed
ruff format.............Passed
biome check (harness)...Passed
KGQuery discipline......Passed

$ make memory-integration-tests   # fast loop
===== 1 failed, 152 passed, 1 skipped, 107 deselected in 190.64s =====
# the 1 failure is the unrelated live SERP test (see Notes)

$ uv run pytest tests/integration/memory/test_embedding_batching.py tests/integration/memory/test_e2e_embedding_split_and_batching.py
============================== 4 passed in 13.66s ==============================

$ make memory-sync-concurrency-limits
Syncing GCL 'voyage-embeddings': limit=3 slot-decay-per-second=0.050000 (action=create)
Running: prefect gcl create voyage-embeddings --limit 3 --slot-decay-per-second 0.05
GCL 'voyage-embeddings' synced successfully.

$ prefect gcl inspect voyage-embeddings
GlobalConcurrencyLimitResponse(name='voyage-embeddings', limit=3, active_slots=0, slot_decay_per_second=0.05)

# re-run is idempotent (update branch, not error):
$ make memory-sync-concurrency-limits
Syncing GCL 'voyage-embeddings': ... (action=update)

$ uv run python -c "from tree.config.app_config import app_config; print(app_config.concurrency)"
voyage_rpm=3 voyage_tpm=10000 runner_global_limit=4 fanout_max_parallel=4
```

**Notes**
- The worktree had no `.env` (required by every `make memory-*` target). Copied it from the sibling main checkout (`../building-agentic-systems/.env`); it is gitignored, so it will NOT be committed.
- The shared docker stack (`tree-prefect-server`, `tree-mongodb`, `tree-mongot`) was already up, so the live `make memory-sync-concurrency-limits` AC was run for real — NOT deferred. The created `voyage-embeddings` GCL persists on the shared Prefect server.
- Unrelated fast-integration failure: `test_web_serp.py::test_empty_query_returns_empty_list` — a live Google SERP query returned tangential YouTube results. No relation to this task's config/embedding changes; network-flaky.
- `make memory-integration-tests-all` (full, incl. mongot) NOT RUN by SWE — that is the Tester's acceptance-gate target. The two named batching tests (the only ones the `max_total_tokens` drop could regress) were run directly and pass.
- DID NOT COMMIT — awaiting Tester review.

### [Tester] 2026-05-21 16:45 — QA

**Test summary**
- Format / lint / pre-commit: PASS (`261 files already formatted`; `All checks passed!`; all 5 pre-commit hooks Passed)
- Unit tests: 1340 passed / 0 failed, **0 warnings** (`1340 passed in 41.96s`)
- Integration (full, `make memory-integration-tests-all`, incl. slow + requires_mongot): 259 passed / 1 failed / 1 skipped in 598.29s. The 1 failure is **pre-existing and unrelated** (see below) — NOT a regression from this diff.
- Warnings: 0

**E2E adversarial pass (config task)**
- Happy path — defaults load from YAML: `uv run python -c "...print(app_config.concurrency, ...doc_concurrency, ...dedup_concurrency, ...dispatch_concurrency, ...max_total_tokens)"`
  → `voyage_rpm=3 voyage_tpm=10000 runner_global_limit=4 fanout_max_parallel=4`, `doc=1`, `dedup=8`, `dispatch=1`, `max_total_tokens=10000` (NOT 320000). PASS
- Break path 1 (env override, valid): `TREE_EXTRACTION__DEDUP_CONCURRENCY=4` → `dedup_concurrency = 4`. PASS
- Break path 2 (malformed env override): `TREE_EXTRACTION__DEDUP_CONCURRENCY=abc` → fails loud with `pydantic_core.ValidationError ... extraction.dedup_concurrency Input should be a valid integer, unable to parse string as an integer [input_value='abc']`. No silent corruption — sane failure naming the exact field. PASS
- Break path 3 (empty env override): `TREE_EXTRACTION__DEDUP_CONCURRENCY=""` → same loud ValidationError (`input_value=''`). PASS
- Break path 4 (boundary: `voyage_rpm=0` in sync script): script does NOT validate; it would issue `prefect gcl create voyage-embeddings --limit 0 --slot-decay-per-second 0.0`. A limit of 0 with zero decay would block all embed acquisitions (deadlock-class misconfig). Operator-controlled config, no AC requires the guard — recorded as a note, not a FAIL (see Other issues).
- Live GCL state (read-only `prefect gcl inspect voyage-embeddings` with `.env` `PREFECT_API_URL` loaded against the shared Docker Prefect server): `GlobalConcurrencyLimitResponse(name='voyage-embeddings', limit=3, active_slots=0, slot_decay_per_second=0.05)`. Confirms the SWE's live `make memory-sync-concurrency-limits` run. PASS. (Did NOT re-run the mutating target — auto-mode blocked writing shared infra; verified via the persisted server-side state instead.)
- SERP flake re-run: `test_web_serp.py::test_empty_query_returns_empty_list` re-run in isolation → PASSED. Confirms the SWE's flag: network-flaky, unrelated to this diff.

**Acceptance criteria**
- [x] PASS — `ConcurrencyConfig` 4 typed fields + YAML load (`voyage_rpm==3`, `fanout_max_parallel==4`) — runtime print above; `tests/unit/config/test_app_config.py::TestConcurrencyConfig`; `app_config.py:221-244`
- [x] PASS — `extraction.doc_concurrency==1` / `dedup_concurrency==8` from YAML — runtime print; `TestExtractionConcurrencyKnobs::test_extraction_fanout_knobs_loaded_from_default_yaml`; `app_config.py:155-164`
- [x] PASS — `embedding_batch.dispatch_concurrency==1` / `max_total_tokens==10000` (NOT 320000) — runtime print; `TestExtractionConcurrencyKnobs::test_dispatch_concurrency_loaded_from_default_yaml`; `app_config.py:77,85`
- [x] PASS — `TREE_EXTRACTION__DEDUP_CONCURRENCY=4` env override reflected — runtime run above (`dedup_concurrency = 4`); `test_dedup_concurrency_env_override`
- [x] PASS — `docs/adrs/002_pipeline_concurrency_and_voyage_rate_limiting.md` exists, Status `Accepted`, covers limiter (Decision 1: GCL + slot decay + `_embed_chunk_resilient` + `strict=False`), fan-out axis (Decision 3: document-shards), write-interleaving tradeoff + lease-leak (Consequences). Verified by read.
- [x] PASS — `make memory-sync-concurrency-limits` GCL `limit=3` + non-zero slot-decay (~0.05) — confirmed via live `prefect gcl inspect` against the running Prefect server (output above)
- [x] PASS — format/lint/pre-commit clean
- [x] PASS — `make memory-unit-tests` passes (1340, incl. 10 new across the two test files)
- [x] PASS — fast integration shows no regression from `max_total_tokens` drop — `test_embedding_batching.py` (3) + `test_e2e_embedding_split_and_batching.py` (1) re-run in isolation: 4 passed with `max_total_tokens=10000`

**Evidence**
```
$ make memory-unit-tests
============================ 1340 passed in 41.96s =============================

$ make memory-integration-tests-all   (full: slow + requires_mongot, quiesced+isolated stack)
============= 1 failed, 259 passed, 1 skipped in 598.29s (0:09:58) =============
FAILED tests/integration/memory/test_dream_e2e_acceptance.py::test_vector_space_swap_runbook_is_discoverable
  → asserts "vector space" in repo-root CLAUDE.md; string was removed on origin/main
    (commit d9c9b7b "feat: Update CLAUDE.md", which is main's HEAD / this branch's merge-base ancestry).
    PRE-EXISTING failure inherited from main — task #054 touches no CLAUDE.md, no test, no dream/vector code.

$ uv run pytest test_embedding_batching.py test_e2e_embedding_split_and_batching.py test_web_serp.py
============================== 7 passed in 24.17s ==============================

$ prefect gcl inspect voyage-embeddings   (PREFECT_API_URL from .env)
GlobalConcurrencyLimitResponse(name='voyage-embeddings', limit=3, active_slots=0, slot_decay_per_second=0.05)
```

**Other issues found**
- (Note, not a blocker) `scripts/sync_concurrency_limits.py` does not validate `voyage_rpm > 0`. A misconfigured `voyage_rpm=0` would create a GCL with `--limit 0 --slot-decay-per-second 0.0`, which blocks all embed slot acquisitions (effective deadlock). It's operator-controlled config and no AC requires the guard, but a one-line `if voyage_rpm <= 0: sys.exit(...)` guard in `_run()` would be cheap insurance — worth a follow-up nit for the PR Reviewer / a later task.
- (Pre-existing red, NOT this task) `test_dream_e2e_acceptance.py::test_vector_space_swap_runbook_is_discoverable` fails because the "vector space" runbook line was removed from CLAUDE.md on main. This will turn CI/full-suite red for the whole feature branch regardless of #054. Recommend the orchestrator file a separate task to either restore the runbook line in CLAUDE.md or update/retire the test — but it must NOT block #054.

**VERDICT: PASS**
