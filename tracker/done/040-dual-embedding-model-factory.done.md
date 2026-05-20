# Dual embedding-model factory (resolution + search getters)

Status: pending
Tags: `data`, `enhancement`, `P1`
Depends on: #039
Blocks: #041, #042, #043

## Scope

With the YAML/config split landed in #039, this task gives
`apps/memory/src/tree/models/get_model.py` two explicit factory entry
points so callers ask for the model that matches their role, instead of
a single ambiguous `get_embedding_model()`.

### New factory surface

Add two functions to `get_model.py`:

- `get_resolution_embedding_model() -> BaseEmbeddingModel` — builds from
  `app_config.models.resolution_embedding` (provider/model/dimensions).
- `get_search_embedding_model() -> BaseEmbeddingModel` — builds from
  `app_config.models.search_embedding`.

Both share the existing per-provider construction logic (mock / gemini /
sentence-transformers / modal / voyage). Refactor the existing
per-provider switch into a private helper
`_build_embedding_model(cfg: EmbeddingConfig) -> BaseEmbeddingModel` that
both getters call, so the provider dispatch lives in one place.

### Keep the old name working

`get_embedding_model(provider=None)` stays as a thin shim that returns
the **search** model (`get_search_embedding_model()`), preserving every
current call site's behavior. Mark it with a short docstring note that
new code should call the explicit getter for its role. Do not delete it
in this task — tasks #041/#043 migrate the call sites; this keeps the
diff reviewable.

### Provider-specific notes

- The voyage branch already takes `model=` and `output_dimension=` from
  the config block; just pass the per-role `cfg` through.
- `MockEmbeddingModel`, `GeminiEmbeddingModel`, `SentenceTransformer...`,
  `ModalEmbeddingModel` all take model/dimensions from the same `cfg` —
  no per-model special-casing.

## Acceptance Criteria

- [x] `get_resolution_embedding_model()` and
      `get_search_embedding_model()` exist in `get_model.py` and build
      from `models.resolution_embedding` / `models.search_embedding`
      respectively.
- [x] The per-provider dispatch is factored into a single private
      `_build_embedding_model(cfg)` helper used by both getters (no
      duplicated provider `if`-ladder).
- [x] `get_embedding_model()` (no args) still returns a search-model
      instance and all existing call sites compile and behave identically.
- [x] Unit test: with both YAML blocks set to `voyage` /
      `voyage-multimodal-3` / 1024, both getters return a
      `VoyageMultimodalEmbeddingModel` whose `.dimensions == 1024`.
- [x] Unit test: setting `models.resolution_embedding.provider: mock`
      (and keeping `search_embedding.provider: voyage`) makes
      `get_resolution_embedding_model()` return a `MockEmbeddingModel`
      while `get_search_embedding_model()` returns the voyage model —
      proving the two getters read independent config blocks.
- [x] Unit test: `get_embedding_model()` returns the same model type as
      `get_search_embedding_model()`.
- [x] `make memory-unit-tests` and `make memory-integration-tests` pass.
- [x] Format/lint/pre-commit clean.

## User Stories

### Story: A pipeline stage asks for the model that fits its job
1. Developer writing the resolution stage calls
   `get_resolution_embedding_model()`.
2. Developer writing the dedup/index stage calls
   `get_search_embedding_model()`.
3. Each receives a model built from the matching YAML block; swapping one
   block in YAML changes only that stage's model.

### Story: Operator points resolution at a cheap mock during local dev
1. Operator sets `models.resolution_embedding.provider: mock` in a local
   YAML override.
2. `get_resolution_embedding_model()` returns a `MockEmbeddingModel`
   (random vectors, no API cost) for resolution.
3. `get_search_embedding_model()` still returns the real voyage model, so
   dedup/query/persisted vectors are unaffected.

### Story: Legacy caller keeps working through the shim
1. A module that still calls `get_embedding_model()` runs unchanged.
2. It receives the search model — the same model it received before the
   split — so no behavior changes for un-migrated callers.

---

Blocked by: #039

## Log

### [PM] 2026-05-20 — Grooming

**Summary**
Adds `get_resolution_embedding_model()` and `get_search_embedding_model()`
to `get_model.py`, factoring the per-provider switch into one private
`_build_embedding_model(cfg)` helper. Legacy `get_embedding_model()`
aliases the search model.

**Key decisions**
- One provider-dispatch helper, two role-named getters — avoids drift.
- Keep `get_embedding_model()` as a shim this round; call-site migration
  happens in #041/#043 to keep diffs reviewable.

**Dependencies**
- #039 — needs the split `resolution_embedding`/`search_embedding` config
  blocks to read from.

**User stories**
- 3 stories: stage asks for the model that fits its job; operator points
  resolution at a mock; legacy caller keeps working through the shim.

Ready for implementation.

### [SWE] 2026-05-20 16:30 — Implementation

**Files modified**
- `apps/memory/src/tree/models/get_model.py` — extracted the per-provider
  embedding dispatch into `_build_embedding_model(cfg, provider=None)`;
  added `get_resolution_embedding_model()` (reads
  `models.resolution_embedding`) and `get_search_embedding_model()` (reads
  `models.search_embedding`); reduced `get_embedding_model()` to a thin
  shim that builds the search block (forwarding its optional `provider`
  override to the helper) so legacy call sites stay behavior-identical.
- `apps/memory/tests/unit/models/test_get_model.py` — added
  `TestDualEmbeddingGetters` (5 tests) plus the `_set_embedding_blocks`
  helper that points the two YAML blocks at real `EmbeddingConfig`
  instances; imported the two new getters and `EmbeddingConfig`.

**Tests**
- Unit: 1207 passing, 0 failing, 0 warnings — `make memory-unit-tests`
  (15 in `test_get_model.py`: 10 pre-existing + 5 new).
- Integration (fast loop): 142 passing, 1 skipped, 70 deselected —
  `make memory-integration-tests` (the deselected/skipped are the
  `slow` / `requires_mongot` tail, excluded from the fast loop by design;
  this task touches no infra so no integration behavior changed).

**Acceptance criteria**
- [x] Both getters exist and build from the matching YAML block — verified
      by `test_resolution_getter_builds_from_resolution_block` /
      `test_search_getter_builds_from_search_block`.
- [x] Single private `_build_embedding_model(cfg)` helper, no duplicated
      provider ladder — both getters and the shim call it.
- [x] `get_embedding_model()` still returns the search model; all call
      sites (`indexing/pipeline.py`, `extraction/pipeline.py`,
      `mcp/server.py`) use the unchanged no-arg signature — verified by
      `test_legacy_getter_aliases_search_model` + the 10 retained
      per-provider tests.
- [x] Both blocks voyage/voyage-multimodal-3/1024 → both getters return
      `VoyageMultimodalEmbeddingModel` at 1024 — `test_both_getters_return_voyage_at_1024`.
- [x] resolution=mock + search=voyage proves independent blocks —
      `test_getters_read_independent_config_blocks`.
- [x] `get_embedding_model()` aliases the search model type —
      `test_legacy_getter_aliases_search_model`.
- [x] `make memory-unit-tests` + `make memory-integration-tests` pass.
- [x] Format/lint/pre-commit clean.

**Evidence**

E2E (real `app_config` loaded from a YAML with resolution=mock/256 +
search=voyage/1024, fake VOYAGE_API_KEY for construction):
```
resolution YAML: mock 256
search YAML    : voyage 1024
resolution -> MockEmbeddingModel dims= 256
search     -> VoyageMultimodalEmbeddingModel dims= 1024
legacy     -> VoyageMultimodalEmbeddingModel dims= 1024
legacy aliases search type: True
independent blocks (resolution=mock, search=voyage): True
```

E2E (default YAML, both blocks voyage/1024, fake key):
```
DEFAULT YAML resolution -> VoyageMultimodalEmbeddingModel dims= 1024
DEFAULT YAML search     -> VoyageMultimodalEmbeddingModel dims= 1024
OK: both getters return VoyageMultimodalEmbeddingModel at 1024 on default YAML
```

```
$ make memory-unit-tests
... tests/unit/models/test_get_model.py ............... [ 95%]
============================ 1207 passed in 38.77s =============================

$ make memory-integration-tests
========== 142 passed, 1 skipped, 70 deselected in 167.05s (0:02:47) ===========

$ make memory-format-check && make memory-lint-check && make pre-commit
239 files already formatted
All checks passed!
... ruff check ... Passed / ruff format ... Passed / KGQuery discipline (memory) ... Passed
```

**Notes**
- `_build_embedding_model` keeps the optional `provider` override (defaults
  to `cfg.provider`) so the legacy shim and its 10 existing per-provider
  unit tests (which patch a `MagicMock` `app_config` and pass
  `provider="gemini"`/`"voyage"`/etc.) behave exactly as before. An
  earlier `model_copy(update=...)` approach broke those MagicMock tests
  (the copy returned another mock, not the override); the `provider`
  parameter path avoids touching the config object at all.
- No Voyage API key is present in the sandbox `.env`, so the live
  `VoyageMultimodalEmbeddingModel(...)` constructor raises
  `ModelError: Voyage API key is required` without one. The e2e above
  injected `VOYAGE_API_KEY=fake-key-for-construction` purely to exercise
  the construction path (the model is built, not called). Unit tests mock
  the settings boundary, so they need no key.
- `models.*` is NOT covered by the `TREE_<SECTION>__<KEY>` env-override
  escape hatch (`_apply_env_overrides` restricts to the `extraction.*`
  subtree). The e2e drove independent blocks via a temp YAML +
  `APP_CONFIG_PATH` instead. Flagging in case the Tester wants to swap
  embedding blocks at runtime — it must be done via YAML, not env.
- Code is uncommitted and on `feat/embedding-split-and-batching` per
  process. Tracker file renamed to `.in-progress.md` via `mv` (the
  tracker files are not yet under git in this worktree).

### [Tester] 2026-05-20 18:10 — QA

**Test summary**
- Format / lint / pre-commit: PASS (`make memory-format-check && make memory-lint-check && make pre-commit` — 239 files formatted, all ruff checks passed, KGQuery discipline passed)
- Unit tests: 1207 passed / 0 failed / 0 warnings (`make memory-unit-tests`, 41.84s; 15 in `test_get_model.py`)
- Integration tests (fast loop): 142 passed / 1 skipped / 70 deselected / 0 failed (`make memory-integration-tests`, 184.15s, exit 0)
- `-all` (slow + requires_mongot tail): NOT RUN — pure-factory change touches no infra/index/pipeline path; spec authorizes the fast loop as sufficient. The 70 deselected + 1 skipped are the slow/mongot tail, unaffected by a `get_model.py` dispatch refactor.
- Warnings: 0

**E2E adversarial pass** (drove the real `tree.models.get_model` factory functions with patched `app_config`/`settings`; cleaned up the harness after)
- Happy path: real `default.yaml` + fake VOYAGE key → `get_search_embedding_model()` / `get_resolution_embedding_model()` / `get_embedding_model()` all return `VoyageMultimodalEmbeddingModel` dims=1024 (PASS)
- Break path A (routing distinct): app_config with resolution=mock/8-dim, search=voyage/1024 → `get_resolution_embedding_model()` → MockEmbeddingModel/8, `get_search_embedding_model()` → VoyageMultimodalEmbeddingModel/1024 (PASS). Reversed the two blocks (res=voyage/1024, search=mock/8) → results flip accordingly, proving no hardcoded cross-wire (PASS)
- Break path B (legacy = search): resolution=mock, search=voyage/1024 → `get_embedding_model()` returns VoyageMultimodalEmbeddingModel/1024, identical type+dims to `get_search_embedding_model()`, and NOT the resolution (mock) block (PASS)
- Break path C (all providers route, no dup ladder): mock→MockEmbeddingModel, gemini→GeminiEmbeddingModel, sentence-transformers→SentenceTransformerEmbeddingModel, modal→ModalEmbeddingModel, voyage→VoyageMultimodalEmbeddingModel (all PASS); unknown provider `"wormhole"` → `ValueError: Unknown embedding provider: wormhole` (PASS). Single `_build_embedding_model` helper holds the only `if`-ladder; both getters + shim delegate to it (verified by reading `get_model.py:28-126`)
- Break path D (no stale callers): grep found 10 `get_embedding_model(` call sites (indexing/pipeline.py x2, extraction/pipeline.py x3, mcp/server.py, query_graph.py, smoke_resolution_dedup.py, migrate_multi_tenancy.py, def site) — all use the unchanged no-arg signature; exercised the no-arg path against real default.yaml end-to-end (returns search model). No `models.embedding` (singular) in any code — the only match is a docstring history note at `get_model.py:111`. (PASS)
- Break path E (cold-start missing key): voyage block + empty VOYAGE key → `ModelError: Voyage API key is required. Set the VOYAGE_API_KEY environment variable.` raised at construction; no silent garbage embedder (PASS)

**Acceptance criteria**
- [x] PASS — Both getters exist and build from matching YAML block — `get_model.py:84-105`; break path A; `test_resolution_getter_builds_from_resolution_block` / `test_search_getter_builds_from_search_block`
- [x] PASS — Single `_build_embedding_model(cfg)` helper, no duplicated ladder — `get_model.py:28-81`; both getters + shim delegate; break path C
- [x] PASS — `get_embedding_model()` returns search model, all 10 call sites unchanged signature — break paths B+D; end-to-end run against default.yaml
- [x] PASS — Both blocks voyage/voyage-multimodal-3/1024 → both getters VoyageMultimodalEmbeddingModel dims==1024 — `test_both_getters_return_voyage_at_1024`; e2e default.yaml run
- [x] PASS — resolution=mock + search=voyage proves independent blocks — `test_getters_read_independent_config_blocks`; break path A
- [x] PASS — `get_embedding_model()` aliases search model type — `test_legacy_getter_aliases_search_model`; break path B
- [x] PASS — `make memory-unit-tests` + `make memory-integration-tests` pass — 1207 / 142+1skip
- [x] PASS — Format/lint/pre-commit clean

**Evidence**
```
$ make memory-unit-tests
tests/unit/models/test_get_model.py ...............                      [ 95%]
============================ 1207 passed in 41.84s =============================

$ make memory-integration-tests
========== 142 passed, 1 skipped, 70 deselected in 184.15s (0:03:04) ===========

$ make memory-format-check && make memory-lint-check && make pre-commit
239 files already formatted
All checks passed!
ruff check ... Passed / ruff format ... Passed / KGQuery discipline (memory) ... Passed

adversarial harness: 10/10 passed (A routing distinct + reversed, B legacy=search,
  C five providers + unknown raises, E missing-key ModelError)
```

**Other issues found**
- None blocking. Note (Nit, not in AC): `_build_embedding_model` retains the optional `provider=` override solely to keep the 10 pre-existing per-provider unit tests green; once #041/#043 migrate call sites the override and the legacy shim can be removed together. Documented by SWE; no action this task.

**VERDICT: PASS**
