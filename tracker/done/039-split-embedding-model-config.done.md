# Split embedding model config into resolution + search models

Status: pending
Tags: `config`, `data`, `enhancement`, `P1`
Depends on: None
Blocks: #040, #041, #042, #043

## Scope

Today there is a single `models.embedding` block in
`apps/memory/configs/default.yaml` (provider `voyage` / model
`voyage-multimodal-3` / dimensions `1024`) with one typed
`EmbeddingConfig` Pydantic model in
`apps/memory/src/tree/config/app_config.py`, and one factory
`get_embedding_model()` in `apps/memory/src/tree/models/get_model.py`.

This task splits that single embedding concept into **two distinct
configured models** without changing any pipeline behavior yet (later
tasks wire the consumers):

- **`resolution_embedding_model`** — a light, transient embedding used
  only during RESOLUTION's semantic stage, computed on the entity NAME
  and dropped afterward (never persisted). For now it points at the
  same Voyage model so behavior is unchanged; the split exists so a
  lighter model can be configured later.
- **`search_embedding_model`** — used for BOTH dedup and search/query.
  Its output is what gets persisted as the node `embedding` field and
  what the live mongot `vector_index` is dimension-coupled to.

### YAML shape

Replace the single `models.embedding` block with two sibling blocks under
`models`:

```yaml
models:
  llm:
    provider: gemini
    model: gemini-3.1-flash-lite
  resolution_embedding:
    provider: voyage
    model: voyage-multimodal-3
    dimensions: 1024
  search_embedding:
    provider: voyage
    model: voyage-multimodal-3
    dimensions: 1024
```

(Keep the same values on both so this task is behavior-preserving.)

### app_config.py

- Reuse the existing `EmbeddingConfig` Pydantic model for both blocks.
- Change `ModelsConfig` to expose `resolution_embedding: EmbeddingConfig`
  and `search_embedding: EmbeddingConfig` instead of `embedding:
  EmbeddingConfig`.
- **Backward-compat shim (required):** every existing reference to
  `app_config.models.embedding` MUST keep working OR be migrated. There
  are ~15 call sites across `get_model.py`, `indexing/core.py`
  (`assert_settings_match_live_vector_index`, `EmbeddingConfig`
  docstrings), `data/pipeline.py` (boot gate), `mcp/server.py` (boot
  gate), `gemini.py`, and `fake_model.py` (default-dimension fallback).
  The chosen approach: **migrate all consumers to the explicit new
  attribute** that matches their role —
  - Anything tied to the PERSISTED vector / live index dimension
    (`assert_settings_match_live_vector_index`, the `data/pipeline.py`
    and `mcp/server.py` boot gates, `fake_model.py`/`gemini.py` default
    dimensions) reads `app_config.models.search_embedding`.
  - Leave `get_embedding_model()` itself to task #040 (it grows two
    factory entry points there); for THIS task, make
    `get_embedding_model()` (no-arg) return the **search** model so
    existing call sites are behavior-identical.
- Do NOT introduce a `models.embedding` alias attribute — a stale alias
  is a future trap. Migrate the references instead.

### Decisions to record in the task log

- The dim-coupling guard `assert_settings_match_live_vector_index` is
  pinned to `search_embedding.dimensions` (search vectors are the
  persisted ones). Update its error message and the
  CLAUDE.md grep-anchor reference (the literal
  `Embedding dimension mismatch` string stays, but the
  `app_config.models.embedding.dimensions` mentions in the message
  become `app_config.models.search_embedding.dimensions`).
- `resolution_embedding.dimensions` is NOT coupled to any persisted
  index (resolution vectors are transient), so no boot gate reads it.

### Env-override escape hatch

`_apply_env_overrides` currently only walks the `extraction.*` subtree.
No change required for this task (model config is not overridable today).
Do not expand the override surface.

## Acceptance Criteria

- [x] `apps/memory/configs/default.yaml` has `models.resolution_embedding`
      and `models.search_embedding` blocks; the single `models.embedding`
      block is gone.
- [x] `ModelsConfig` exposes `resolution_embedding` and `search_embedding`
      (both `EmbeddingConfig`); `ModelsConfig.embedding` no longer exists.
- [x] `grep -rn "app_config.models.embedding\b" apps/memory/src` returns
      zero hits (every consumer migrated to `.search_embedding` or
      `.resolution_embedding`).
- [x] `assert_settings_match_live_vector_index` reads
      `app_config.models.search_embedding.dimensions`; its mismatch error
      still contains the literal substring `Embedding dimension mismatch`.
- [x] The `data/pipeline.py` and `mcp/server.py` boot gates reference the
      search model's dimensions.
- [x] `get_embedding_model()` called with no provider returns a model
      built from `models.search_embedding` (behavior-identical to today).
- [x] Unit test: loading `default.yaml` yields a config where
      `models.resolution_embedding.model == "voyage-multimodal-3"` and
      `models.search_embedding.model == "voyage-multimodal-3"` and both
      `dimensions == 1024`.
- [x] Unit test: a YAML that sets only `models.search_embedding` (omitting
      `resolution_embedding`) loads with `resolution_embedding` defaulting
      to the `EmbeddingConfig` defaults (no crash).
- [x] `make memory-unit-tests` and `make memory-integration-tests` pass
      (no behavior change expected — both models are identical here).
- [x] `make memory-format-fix && make memory-lint-fix &&
      make memory-format-check && make memory-lint-check &&
      make pre-commit` clean.

## User Stories

### Story: Operator configures a lighter resolution model in YAML
1. Operator opens `apps/memory/configs/default.yaml`.
2. Operator sees two embedding blocks under `models`:
   `resolution_embedding` and `search_embedding`, each with
   provider/model/dimensions.
3. Operator changes `models.resolution_embedding.model` to a different
   value (e.g. a hypothetical lighter model) and leaves
   `search_embedding` untouched.
4. Config loads without error; `app_config.models.resolution_embedding.model`
   reflects the new value and `app_config.models.search_embedding.model`
   is unchanged.

### Story: Developer reads the config and knows which model is persisted
1. Developer greps for `search_embedding` in the codebase.
2. They find it is the model whose `dimensions` the live `vector_index`
   is asserted against at boot, and the model used for dedup + query.
3. They grep for `resolution_embedding` and find it is only consumed by
   the resolution semantic stage (after task #043), confirming its
   vectors are never written to MongoDB.

### Story: Existing pipeline boots unchanged after the split
1. Developer runs `make memory-serve-workflows &` then
   `make memory-run-memory-pipeline-indexing USER_ID=<oid>` on a DB whose
   live `vector_index` is 1024-d.
2. The boot gate compares the live index dimension against
   `models.search_embedding.dimensions` (1024) and passes — no
   `Embedding dimension mismatch` error.
3. Pipeline completes exactly as before the split.

---

Blocked by: (none)

## Log

### [PM] 2026-05-20 — Grooming

**Summary**
Splits the single `models.embedding` config into a transient
`resolution_embedding` and a persisted `search_embedding`, behavior-
preserving (both point at `voyage-multimodal-3` / 1024).

**Key decisions**
- Reuse the existing `EmbeddingConfig` model for both blocks; no new type.
- Migrate all ~15 `app_config.models.embedding` consumers rather than add
  a stale alias attribute. Persisted/index/boot-gate consumers →
  `search_embedding`; `get_embedding_model()` (no-arg) returns the search
  model so callers are behavior-identical.
- The dim-coupling guard (`assert_settings_match_live_vector_index`) is
  pinned to `search_embedding.dimensions` — search vectors are the
  persisted ones. The literal `Embedding dimension mismatch` grep anchor
  stays.

**Dependencies**
- None (first task in the feature).

**User stories**
- 3 stories: operator configures a lighter resolution model; developer
  reads which model is persisted; existing pipeline boots unchanged.

Ready for implementation.

### [SWE] 2026-05-20 16:30 — Implementation

**Files modified**
- `apps/memory/configs/default.yaml` — replaced the single `models.embedding`
  block with sibling `models.resolution_embedding` + `models.search_embedding`
  blocks (same values: voyage / voyage-multimodal-3 / 1024).
- `apps/memory/src/tree/config/app_config.py` — `ModelsConfig` now exposes
  `resolution_embedding: EmbeddingConfig` and `search_embedding: EmbeddingConfig`;
  `embedding` removed. Reused the existing `EmbeddingConfig` (no new type).
  Docstrings explain which model is persisted/index-coupled.
- `apps/memory/src/tree/models/get_model.py` — no-arg `get_embedding_model()`
  now reads `app_config.models.search_embedding` (behavior-identical).
- `apps/memory/src/tree/models/fake_model.py` — `Fake`/`Mock` default-dim
  fallback reads `search_embedding.dimensions`.
- `apps/memory/src/tree/models/gemini.py` — `GeminiEmbeddingModel` default
  model/dim reads `search_embedding`.
- `apps/memory/src/tree/memory/indexing/core.py` —
  `assert_settings_match_live_vector_index` reads
  `search_embedding.dimensions`; docstring + error messages updated
  (literal `Embedding dimension mismatch` anchor preserved).
- `apps/memory/src/tree/memory/indexing/pipeline.py`,
  `apps/memory/src/tree/data/pipeline.py`,
  `apps/memory/src/tree/mcp/server.py` — boot-gate comments point at
  `search_embedding.dimensions`.
- `CLAUDE.md` — diagnosis tip + runbook error-string + rebuild step now
  reference `models.search_embedding.dimensions` (PM-territory ADR/glossary
  untouched; this is the operator runbook only, kept in sync with the live
  error string).
- `apps/memory/README.md` — `default.yaml sections` documents both new
  embedding blocks (also corrected a stale pre-#034 sentence-transformers/384
  default); Modal-deploy step points at `search_embedding`.
- Tests migrated: `tests/unit/config/test_app_config.py` (new AC tests),
  `tests/unit/models/test_get_model.py`,
  `tests/unit/memory/indexing/test_settings_vector_index_check.py`,
  `tests/unit/data/test_pipeline.py` (comment),
  `tests/integration/memory/test_dedup.py` (runtime `patch.object`),
  `tests/integration/memory/test_indexing_pipeline.py` (comment),
  `tests/integration/test_two_user_isolation.py` (comment).

**Tests**
- Unit: 1202 passing, 0 failing, 0 warnings — `make memory-unit-tests`.
- Integration (fast loop): 142 passing, 1 skipped, 70 deselected (slow) —
  `make memory-integration-tests`. Includes the dedup test whose
  `patch.object(app_config.models.search_embedding, "dimensions", ...)` was
  migrated.
- TDD: added two new red→green AC tests in `test_app_config.py`
  (default.yaml yields both models == voyage-multimodal-3/1024; search-only
  YAML defaults resolution_embedding) — confirmed red with
  `AttributeError: 'ModelsConfig' object has no attribute 'search_embedding'`
  before implementing.

**Acceptance criteria**
- [x] default.yaml has both blocks; single `embedding` block gone.
- [x] `ModelsConfig` exposes `resolution_embedding` + `search_embedding`;
      `.embedding` no longer exists (verified at runtime: `hasattr(...,'embedding')==False`).
- [x] `grep -rn "app_config.models.embedding\b" apps/memory/src` → zero hits.
- [x] `assert_settings_match_live_vector_index` reads
      `search_embedding.dimensions`; `Embedding dimension mismatch` anchor kept
      (verified by `test_mismatch_raises_runtime_error_with_both_numbers`).
- [x] `data/pipeline.py` + `mcp/server.py` boot gates reference search dims.
- [x] no-arg `get_embedding_model()` builds from `search_embedding`
      (verified by `test_returns_mock_by_default` + manual e2e).
- [x] Unit test: default.yaml → both models voyage-multimodal-3/1024
      (`test_loads_default_yaml`).
- [x] Unit test: search-only YAML defaults resolution_embedding
      (`test_search_embedding_only_defaults_resolution_embedding`).
- [x] `make memory-unit-tests` + `make memory-integration-tests` pass.
- [x] format/lint/pre-commit clean.

**Evidence**
```
$ make memory-unit-tests
============================ 1202 passed in 45.50s =============================

$ make memory-integration-tests
========== 142 passed, 1 skipped, 70 deselected in 166.37s (0:02:46) ===========

$ make memory-format-check && make memory-lint-check
239 files already formatted
All checks passed!

$ make pre-commit
prettier ... Passed | ruff check ... Passed | ruff format ... Passed
biome check (harness) ... Passed | KGQuery discipline (memory) ... Passed

$ grep -rn "app_config.models.embedding\b" apps/memory/src
(zero hits)

$ uv run python -c "from tree.config.app_config import app_config as a; \
print(a.models.resolution_embedding.model, a.models.search_embedding.model, \
hasattr(a.models,'embedding'))"
voyage-multimodal-3 voyage-multimodal-3 False

$ uv run python -c "from tree.models.get_model import get_embedding_model as g; \
m=g(provider='mock'); print(type(m).__name__, m.dimensions)"
MockEmbeddingModel 1024
```

**Notes**
- Behavior-preserving: both embedding blocks point at the same
  voyage-multimodal-3 / 1024 config, so no functional change. The split is
  config scaffold for #040–#043.
- `get_embedding_model()` was NOT given dual entry points here (deferred to
  #040 per spec); no-arg returns the search model.
- `_apply_env_overrides` left as-is (only walks `extraction.*`; model config
  is not overridable today) — per spec, did not expand the override surface.
- The full `make memory-integration-tests-all` (slow + mongot) was NOT run by
  me — that is the Tester's acceptance-gate target. Fast loop is green and the
  change is behavior-preserving, so I do not expect slow-tier regressions, but
  flagging it for the Tester to run with the full docker stack.
- Touched `CLAUDE.md` (runbook prose) and `README.md`: these are operator docs
  that quote the live error string verbatim, so they had to track the rename.
  No `docs/adr/` or `docs/glossary.md` edits (PM territory; none needed — no
  new domain term, no new architectural fork).
- DID NOT COMMIT — handing to Tester first per process.

### [Tester] 2026-05-20 18:40 — QA

**Test summary**
- Format / lint / pre-commit: PASS (`memory-format-check` 239 files formatted;
  `memory-lint-check` All checks passed; `pre-commit` all hooks Passed incl.
  prettier / ruff check / ruff format / biome / KGQuery discipline).
- Unit tests: 1202 passed / 0 failed (`make memory-unit-tests`, 44.98s, 0 warnings).
- Integration tests (ACCEPTANCE GATE, slow + mongot): 212 passed / 1 skipped
  (`make memory-integration-tests-all`, 574.64s, full docker stack up incl.
  tree-mongot). The 1 skip is `test_web_search_ingest.py` — environmental
  (skips unless `serve-workflows` has registered the deployment); skips
  identically on main, NOT a regression.
- Warnings: 0.
- `code-review` plugin: the enabled plugin is GitHub-PR-scoped (operates via
  `gh pr diff` / comments on a PR). No PR exists at the Tester stage (work is
  uncommitted; PR opens at squash time per PROCESS.md), so the plugin's
  workflow is N/A here. Substituted a full manual line-by-line diff review of
  all 18 changed files — no defects found.

**E2E adversarial pass**
- Happy path (D — YAML round-trip): `app_config` loads; both
  `models.resolution_embedding` and `models.search_embedding` parse to typed
  `EmbeddingConfig(provider=voyage, model=voyage-multimodal-3, dimensions=1024)`;
  `hasattr(app_config.models, 'embedding') == False`. (PASS)
- Break path A (import-time safety — stale singular refs): `grep -rn
  "app_config.models.embedding\b"` → 0 hits in src AND tests AND scripts. Only
  surviving `models.embedding` token is a prose mention in a `get_model.py`
  docstring (no attribute access). Boot-imported all 6 config/orchestrator
  modules + 11 flow modules + FastMCP server + all 16 `scripts/*.py`: every
  one imports clean, zero `AttributeError: ... no attribute 'embedding'`.
  (`smoke_resolution_dedup.py` first appeared to FAIL — traced to a CPython
  3.14 `@dataclass` introspection quirk from my throwaway-module-name import
  method, NOT a code defect; re-imported with proper `sys.modules` registration
  → OK.) (PASS)
- Break path B (dim-guard pinned to SEARCH): with resolution=999 / search=1024 /
  live=1024 the guard returns None (ignores resolution). With resolution=1024
  (matches live) / search=512 / live=1024 the guard RAISES keyed on SEARCH:
  `Embedding dimension mismatch: app_config.models.search_embedding.dimensions=512
  but live vector_index numDimensions=1024. ...models.search_embedding.dimensions
  to 1024.` — byte-identical to the CLAUDE.md runbook quote (line 384); the word
  "resolution" never leaks into the message. (PASS)
- Break path C (behavior-preserving factory routing): no-arg
  `get_embedding_model()` resolves provider AND dimensions from
  `search_embedding` only — proven by patching search→mock/777 and
  resolution→gemini/111: returned `MockEmbeddingModel` dim 777 (the search
  values, not resolution). `provider='voyage'` selects the
  `VoyageMultimodalEmbeddingModel` branch (confirmed at source; instantiation
  only blocked by absent VOYAGE_API_KEY in the QA env). (PASS)
- Break path E (TREE_*__* override scope): `TREE_MODELS__SEARCH_EMBEDDING__DIMENSIONS=512`
  is IGNORED (search dims stay 1024) while the control `TREE_EXTRACTION__CHUNK_SIZE=999`
  DOES apply (chunk_size→999). Model-config env overrides are out of the override
  surface BY DESIGN (`_apply_env_overrides` hard-gates `path[0] != "extraction"`),
  matching the spec ("Do not expand the override surface") and the #037 Tester
  note. Documented behavior, not a defect. (PASS)

**Acceptance criteria**
- [x] PASS — default.yaml has `models.resolution_embedding` + `models.search_embedding`,
      single `models.embedding` gone — `configs/default.yaml:63-77`; verified the
      `embedding:` key is replaced by the two sibling blocks in the diff.
- [x] PASS — `ModelsConfig` exposes `resolution_embedding` + `search_embedding`
      (both `EmbeddingConfig`); `.embedding` gone — `app_config.py:61-79`; runtime
      `hasattr(app_config.models,'embedding') == False`.
- [x] PASS — `grep -rn "app_config.models.embedding\b" apps/memory/src` → 0 hits
      (also 0 in tests + scripts).
- [x] PASS — `assert_settings_match_live_vector_index` reads
      `app_config.models.search_embedding.dimensions` (`indexing/core.py:479`);
      mismatch error contains literal `Embedding dimension mismatch` (break path B).
- [x] PASS — `data/pipeline.py:89` + `mcp/server.py:158` boot-gate comments
      reference search dims; the gate they invoke reads `search_embedding.dimensions`.
- [x] PASS — no-arg `get_embedding_model()` builds from `search_embedding`
      (`get_model.py:39-40`; break path C proves search-vs-resolution selection).
- [x] PASS — Unit test: `test_app_config.py::TestLoadAppConfig::test_loads_default_yaml`
      asserts both models == voyage-multimodal-3 / 1024.
- [x] PASS — Unit test:
      `test_app_config.py::test_search_embedding_only_defaults_resolution_embedding`
      — search-only YAML defaults `resolution_embedding`, no crash.
- [x] PASS — `make memory-unit-tests` (1202) + `make memory-integration-tests-all`
      (212/1 skip) pass.
- [x] PASS — format/lint/pre-commit clean.

**Evidence**
```
$ make memory-unit-tests
============================ 1202 passed in 44.98s =============================

$ make memory-integration-tests-all   # full docker stack (mongot up)
================== 212 passed, 1 skipped in 574.64s (0:09:34) ==================

$ make pre-commit
prettier ... Passed | ruff check ... Passed | ruff format ... Passed
biome check (harness) ... Passed | KGQuery discipline (memory) ... Passed

$ grep -rn "app_config.models.embedding\b" apps/memory/src apps/memory/tests apps/memory/scripts
(zero hits)
```

**Other issues found**
- (Nit / scope flag — not blocking) The uncommitted `CLAUDE.md` diff carries
  changes BEYOND this task's runbook-string rename: it removes the entire
  "Developing New Features and Bug Fixes Workflow" section, swaps
  "Firecrawl"→"Bright Data", and adds "Configuration" + "Populating the Users
  Collection" sections. These are unrelated to the embedding split and aren't
  mentioned in the SWE log (which only claims "runbook prose"). Whether intended
  branch-level doc cleanup or accidental drift, the SWE must stage files
  explicitly (no `git add -A`) and the orchestrator/PR Reviewer should confirm
  these doc edits are intended before the squash. The embedding-relevant
  CLAUDE.md edits (runbook error-string + diagnosis tip → `search_embedding`)
  are correct and byte-match the live error.
- (Note) `code-review` plugin is GitHub-PR-scoped and N/A pre-commit; flagging
  so the PR Reviewer / On-Call runs it against the diff once the PR exists.

**VERDICT: PASS**

