---
id: 134-voyage-4-embedding-upgrade
status: done
feature: voyage-4-and-modal-embedding-catalog
---

# Voyage 4: `voyage-4` for both embedding blocks, 4-series dimension + price tables, embed-task cache keyed on embedding identity

Tags: `models`, `config`, `memory`
Depends on: None
Blocks: #136, #141
Implements: ADR-009 — Decision 1 (Voyage 4 pin) and Decision 6 (cache identity)

## Scope

Voyage's current text models are the 4 series (verified 2026-09-19, docs.voyageai.com/docs/embeddings
and /docs/pricing): `voyage-4-large` $0.12/1M tok, `voyage-4` $0.06, `voyage-4-lite` $0.02,
`voyage-code-4` $0.12 — all 32K context, 1024-d default, Matryoshka 256/512/1024/2048, ONE shared
embedding space. `voyage-3.x` is legacy but still served; `voyage-finance-2` / `voyage-law-2` and
`voyage-multimodal-3` / `-3.5` are unchanged.

- `apps/memory/configs/default.yaml`: `models.resolution_embedding.model` and
  `models.search_embedding.model` → `voyage-4`; `dimensions` stays `1024` on both (the mongot
  `vector_index` is untouched). `observability.embedding_price_per_1m_tokens` gains
  `voyage-4-large: 0.12`, `voyage-4: 0.06`, `voyage-4-lite: 0.02`, `voyage-code-4: 0.12`; every
  existing id stays. The "Verified against …" comment date → September 2026.
- `src/tree/config/app_config.py`: `EmbeddingConfig.model` default → `"voyage-4"`; the
  `ObservabilityConfig.embedding_price_per_1m_tokens` default map + its docstring gain the same four
  ids and prices.
- `src/tree/models/voyage_embedding.py`: constructor default `model="voyage-4"`;
  `_MODEL_NATIVE_DIMENSIONS` gains `voyage-4-large`, `voyage-4`, `voyage-4-lite`, `voyage-code-4`
  (all `1024`), legacy ids kept; module + class docstrings name the 4 series (payload example uses
  `voyage-4`).
- `src/tree/memory/embedding_text.py` / `get_model.py` comments that name `voyage-3` as "the text
  family" → "the `voyage-4` / legacy `voyage-3` families". No routing change: `voyage-multimodal*`
  → multimodal client, everything else → text client.
- **Embed-task cache identity.** `embed_children_task` and `embed_entities_task`
  (`src/tree/memory/pipeline.py`) cache on `INPUTS - "opik_trace_headers"` for 90 days, i.e. on the
  text list alone — after this swap a re-extraction of an already-seen document would return CACHED
  voyage-3.5 vectors. Add a required keyword task input `embedding_identity: str` to both task
  bodies (unused in the body, present in the cache key) and pass
  `f"{cfg.provider}:{cfg.model}:{cfg.dimensions}"` built from `app_config.models.search_embedding`
  by ONE helper (SWE picks the home; `tree.models.get_model` is fine). With the defaults the value
  is exactly `"voyage:voyage-4:1024"`. Every call site of the two tasks passes it.
- `tests/unit/config/fixtures/frozen_config.yaml` and the ~35 `voyage-3.5` references in
  `tests/unit/` (test_voyage_embedding, test_app_config, test_observability,
  test_observability_instrumentation, test_get_model, test_dimensions, test_add_entity,
  test_indexing_settings_vector_index_check): move DEFAULT-asserting references to `voyage-4`; keep
  at least one legacy-id assertion (`voyage-3-lite` → 512) so the "legacy kept" rule is pinned.
- `apps/memory/README.md` "`default.yaml` sections": the two `models.*_embedding` bullets say
  `voyage` / `voyage-4` / 1024 (today they wrongly say `voyage-multimodal-3`); `models.llm` default
  there is also stale (`gemini-2.5-flash-lite` vs YAML `gemini-3.1-flash-lite`) — fix in the same edit.

Use the `tech-docs` skill for the Voyage API; write tests with `/squid-testing-python`.

## Out of scope
- Resetting / re-embedding persisted vectors (#137) and the live threshold re-pin (#141). Until
  #137 + #141 run, an existing local DB holds voyage-3.5 vectors queried with voyage-4 — expected.
- `input_type` (#135, #136). Editing `docs/adrs/001` (Accepted ADR; ADR-009 records the pin).
- Dimensions, the vector index, `voyage-context-4`, the multimodal client's model table.

## Acceptance Criteria

- [x] `app_config.models.search_embedding` and `.resolution_embedding` are both `provider="voyage", model="voyage-4", dimensions=1024` from YAML AND from `EmbeddingConfig()` defaults — `tests/unit/config/test_app_config.py::TestEmbeddingDefaults::test_both_blocks_default_to_voyage_4_1024`.
- [x] `VoyageTextEmbeddingModel(api_key="k").dimensions == 1024` with no explicit model; each of `voyage-4-large`, `voyage-4`, `voyage-4-lite`, `voyage-code-4` → `1024`; `voyage-3-lite` still → `512`; an unknown id with no `output_dimension` still raises `ModelError` naming `_MODEL_NATIVE_DIMENSIONS` — `tests/unit/models/test_voyage_embedding.py::TestNativeDimensions`.
- [x] `app_config.observability.cost_for("voyage-4", 1_000_000) == 0.06`, `voyage-4-large` → `0.12`, `voyage-4-lite` → `0.02`, `voyage-code-4` → `0.12`, `voyage-3.5` still → `0.06`, unknown model → `0.0` — `tests/unit/test_observability.py::TestVoyage4Prices`.
- [x] The YAML price map and the Pydantic default price map have identical keys and values — `tests/unit/config/test_app_config.py::test_yaml_price_map_matches_code_default`.
- [x] `_embed_children` and `_embed_entities` take `embedding_identity: str`; the helper returns `"voyage:voyage-4:1024"` for the default config; both tasks' computed cache keys differ between `embedding_identity="voyage:voyage-3.5:1024"` and `"voyage:voyage-4:1024"` for the same `texts`, and are equal when only `opik_trace_headers` differs — `tests/unit/memory/test_pipeline.py::TestEmbedTaskCacheIdentity`.
- [x] `grep -n "voyage-multimodal-3\` / 1024" apps/memory/README.md` → no match; the two bullets read `voyage` / `voyage-4` / 1024.
- [x] `grep -rn "voyage-3.5" apps/memory/src apps/memory/configs` matches only the legacy rows of the dimension + price tables and their docstrings.
- [x] `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green (`make env-status` → local).

## User Stories

### Story: Operator boots the memory app after the upgrade
1. Operator pulls the branch and runs `make memory-serve-mcp USER_ID=<oid>` with an untouched `.env`.
2. Boot passes `assert_settings_match_live_vector_index` (1024 == 1024) — no index rebuild.
3. A `search_memory` call's Opik span `voyage-text-embed` shows `model: voyage-4` and a non-zero `total_cost` (tokens × $0.06 / 1M).

### Story: Operator re-runs the memory pipeline on a document processed last week
1. `make memory-run-memory-pipeline SOURCE_URIS="https://example.substack.com/p/prefect"` — the same document text as a run made on voyage-3.5.
2. In the Prefect UI `embed-children` is `Completed`, NOT `Cached`: the cache key now carries `voyage:voyage-4:1024`.
3. A second identical run right after shows `Cached`.

### Story: Operator pins a legacy model through the escape hatch
1. `TREE_MODELS__SEARCH_EMBEDDING__MODEL=voyage-3.5 make memory-run-indexing-pipeline`.
2. The run embeds with `voyage-3.5`, reports 1024-d, and costs at $0.06 / 1M — legacy ids still resolve.

---

Blocked by: (none)

## Log

### [PA] 2026-09-19 17:43 — Grooming

**Summary**
Swap both embedding blocks to `voyage-4` @ 1024-d, extend (never shrink) the dimension and price tables, and stop the 90-day Prefect embed cache from serving old-model vectors.

**Key decisions**
- `embedding_identity` as a plain task INPUT rather than a custom `cache_key_fn`: INPUTS already hashes it; zero new mechanism.
- Legacy ids stay in both tables — `voyage-3.x` is still served and the env escape hatch must keep working.
- ADR-001's `voyage-3` example is not edited (Accepted ADR); ADR-009 carries the pin.

**Dependencies**
- None.

**User stories**
- 3 stories: boot after upgrade, cache miss after the swap, legacy model via env override.

Ready for implementation.

### [SWE] 2026-09-19 18:30 — Implementation

**Files modified**
- `apps/memory/configs/default.yaml` — both embedding blocks → `voyage-4` @ 1024; price map gains the 4 series; "Verified against …" date → September 2026.
- `apps/memory/src/tree/config/app_config.py` — `EmbeddingConfig.model` default → `voyage-4`; `ObservabilityConfig` default price map + docstring gain the 4 series, legacy rows kept.
- `apps/memory/src/tree/models/voyage_embedding.py` — ctor default → `voyage-4`; `_MODEL_NATIVE_DIMENSIONS` gains `voyage-4-large` / `voyage-4` / `voyage-4-lite` / `voyage-code-4` (1024 each), legacy ids kept; module + class docstrings name the 4 series.
- `apps/memory/src/tree/models/get_model.py` — new `search_embedding_identity()` helper (`provider:model:dimensions`, read at call time); text-vs-multimodal routing comment now names the `voyage-4` / legacy `voyage-3` families. No routing change.
- `apps/memory/src/tree/memory/pipeline.py` — `_embed_children` / `_embed_entities` take a REQUIRED keyword-only `embedding_identity: str` (logged, otherwise unused; it rides in the `INPUTS` cache key); the worker body resolves it once per run and passes it at both call sites.
- `apps/memory/README.md` — the two `models.*_embedding` bullets → `voyage` / `voyage-4` / 1024; stale `models.llm` default → `gemini-3.1-flash-lite`.
- `apps/memory/tests/unit/config/fixtures/frozen_config.yaml` + 7 unit-test modules — default-asserting references moved to `voyage-4`; legacy assertions kept (`voyage-3-lite` → 512, `voyage-3.5` → $0.06).

**Tests**
- Unit: 3134 passing, 0 failing (`make memory-tests`, env target `local`).
- Integration: N/A — the repo has no integration suite by design (AGENTS.md); e2e verification is by running the real code, below.
- New/changed tests: `TestEmbeddingDefaults::test_both_blocks_default_to_voyage_4_1024`, `test_yaml_price_map_matches_code_default` (both `tests/unit/config/test_app_config.py`), `TestNativeDimensions` (`tests/unit/models/test_voyage_embedding.py`), `TestVoyage4Prices` (`tests/unit/test_observability.py`), `TestEmbedTaskCacheIdentity` (`tests/unit/memory/test_pipeline.py`), `TestSearchEmbeddingIdentity` + 4-series routing params (`tests/unit/models/test_get_model.py`), `TestFlowEmbeddingModelSplit::test_both_embed_tasks_receive_the_embedding_identity` (call-site coverage).

**Acceptance criteria**
- [x] Both blocks `voyage/voyage-4/1024` from YAML and from `EmbeddingConfig()` — `tests/unit/config/test_app_config.py::TestEmbeddingDefaults::test_both_blocks_default_to_voyage_4_1024` (asserts the REAL `configs/default.yaml`, not the frozen fixture).
- [x] Dimension table — `tests/unit/models/test_voyage_embedding.py::TestNativeDimensions`.
- [x] Price table — `tests/unit/test_observability.py::TestVoyage4Prices`.
- [x] YAML price map == Pydantic default map — `tests/unit/config/test_app_config.py::test_yaml_price_map_matches_code_default`.
- [x] `embedding_identity` on both tasks + cache-key behaviour — `tests/unit/memory/test_pipeline.py::TestEmbedTaskCacheIdentity`.
- [x] README bullets fixed (grep for ``voyage-multimodal-3` / 1024`` → no match).
- [x] `grep -rn "voyage-3.5" apps/memory/src apps/memory/configs` → only legacy table rows, their docstrings, and the escape-hatch example in `search_embedding_identity`'s docstring.
- [x] format-check / lint-check / pre-commit / tests green on `make env-status` → local.

**Evidence**
```
$ make env-status
Env target: local (.env)

$ make memory-tests
3134 passed in 53.97s

$ make memory-format-check && make memory-lint-check
293 files already formatted
All checks passed!

$ make pre-commit
prettier / ruff check / ruff format / biome check (harness) ......... Passed
```

End-to-end (real services, LOCAL env, no writes to persisted vectors):
```
# Story 1 — live voyage-4 embed through the shipped config + boot gate
search_embedding: voyage voyage-4 1024
identity: voyage:voyage-4:1024
price voyage-4 /1M: 0.06
client: VoyageTextEmbeddingModel dims: 1024
live embed OK — n= 1 dim= 1024 head= [0.021762313, -0.031853836, 0.048230849, 0.000185564]
boot dimension assertion PASSED (search_embedding 1024 == live vector_index)
# (the call emitted a live Opik span batch — HTTP 204; the span's model id and the
#  $0.06/1M cost math are pinned by test_observability_instrumentation.py::TestVoyageTextCostRecording,
#  not by an inspection of that payload)

# Story 2 — real Prefect run of embed-children against the local Prefect server
18:23:32.061 | Task run 'embed-children-951' - embed_children: identity=voyage:voyage-4:1024 n_texts=1 dim=1024
18:23:32.065 | Task run 'embed-children-951' - Finished in state Completed()
18:23:32.067 | Task run 'embed-children-ce3' - Finished in state Cached(type=COMPLETED)
18:23:34.450 | Task run 'embed-children-969' - embed_children: identity=voyage:voyage-3.5:1024 n_texts=1 dim=1024
18:23:34.453 | Task run 'embed-children-969' - Finished in state Completed()
run1 (fresh identity):      Completed
run2 (same identity):       Cached
run3 (legacy identity):     Completed

# Story 3 — legacy pin through the escape hatch
$ TREE_MODELS__SEARCH_EMBEDDING__MODEL=voyage-3.5 ...
model: voyage-3.5 | client: VoyageTextEmbeddingModel | dims: 1024
identity: voyage:voyage-3.5:1024
cost/1M: 0.06
```

**Notes**
- **3-part identity, deliberately.** ADR-009 §6 AND `docs/glossary.md` (**Embedding role**: "Part of the embed-task cache identity (`voyage:voyage-4:1024:document`)") specify a 4-part `provider:model:dimensions:role`. This task ships the 3-part prefix because `input_type` / the Embedding role is explicitly out of scope here (task 134 "Out of scope", and AC #5 pins the literal `"voyage:voyage-4:1024"`) and lands in #135/#136. `search_embedding_identity()` is the single extension point for appending the role then — it is NOT an undocumented deviation. Glossary and ADR were read, not edited (SWE is read-only on both).
- **AC #7 grep, verbatim results.** Beyond the legacy table rows and their docstrings, three `voyage-3.5` matches are prose: `app_config.py:51` (why `voyage-4` replaces it), `get_model.py:131` (the `TREE_MODELS__SEARCH_EMBEDDING__MODEL=voyage-3.5` escape-hatch example in the helper docstring) and `configs/default.yaml:177` (the same example above the price map). Left concrete on purpose — CLAUDE.md requires concrete examples over vague ones, so a literal legacy id beats a `<legacy id>` placeholder.
- Helper home: `tree.models.get_model.search_embedding_identity()` (the task left the choice to SWE). It reads `app_config` at CALL time, never as a module constant — Prefect re-imports the module in flow-run subprocesses and the `TREE_MODELS__…` override must move the identity with it (verified in Story 3 above).
- `embedding_identity` is keyword-only and required: `opik_trace_headers` already has a default, and a default on the identity would let a call site silently fall back to a model-blind cache key.
- The identity is folded into each task's existing `log.info` line (`embed_children: identity=… n_texts=… dim=…`) so the value is visible in the Prefect logs instead of being a lint-invisible unused arg.
- `embed_nodes_task` (indexing backfill) was checked for the same stale-cache bug: it is `NO_CACHE`, so nothing to do.
- `src/tree/memory/embedding_text.py` needed no edit — its only `voyage-3`-looking strings are `voyage-multimodal-3` per-request batch caps, which this task does not touch. The scope bullet naming that file is stale.
- **NOT RUN — Voyage doc re-verification via the `tech-docs` skill**: no context7 / web-fetch tool is available in this sub-agent session. Relied on the 2026-09-19 verification recorded in this task and ADR-009 (which agree) — and, more usefully, on the live call above, which proves `voyage-4` is served by `ai.mongodb.com/v1/embeddings` at 1024-d.
- Persisted vectors were NOT touched: the local DB still holds voyage-3.5 vectors until #137 (reset) and #141 (threshold re-pin) run — the expected, documented state.
- NOT committed — waiting on Tester review.

### [Tester] 2026-09-19 18:36 — QA

**Test summary**
- Format / lint / pre-commit: PASS (`make memory-format-check`, `make memory-lint-check`, `make pre-commit` all green; `make env-status` → local)
- Unit tests: 3134 passed / 0 failed (`make memory-tests`, 48.25s)
- Integration tests: N/A by design (AGENTS.md — no integration suite; e2e verification below)
- Warnings: 1 (`opik/rest_api/core/pydantic_utilities.py:13: UserWarning: Core Pydantic V1 functionality isn't compatible with Python 3.14`) — confirmed PRE-EXISTING by `git stash` + re-running `make memory-tests` on the unmodified branch tip (61122e4): same single warning, 3105 passed. Not introduced by this diff.

**E2E adversarial pass**
- Happy path: live `get_search_embedding_model().embed(["adversarial QA test string"])` against the shipped `configs/default.yaml` (no overrides) → `model: voyage-4`, `n=1 dim=1024` (real call to `ai.mongodb.com/v1/embeddings`). PASS.
- Break path 1 (other call sites of the two embed tasks): `grep -n "embed_children_task\|embed_entities_task" apps/memory/src apps/memory/scripts apps/memory/deploy` → only one call site each, both inside `_run_extraction_worker_body` (`pipeline.py:2008`, `pipeline.py:2127`), which is itself called from exactly one place (`pipeline.py:1918`). `orchestrator.py` does not call either task directly. No dangling caller would crash on the new required kwarg. PASS.
- Break path 2 (state edge — omitted required kwarg): `await _embed_children([])` (no `embedding_identity`) → `TypeError: _embed_children() missing 1 required keyword-only argument: 'embedding_identity'`. Fails loud, not silent. PASS.
- Break path 3 (cache key really changes with identity, stable across trace headers): called the REAL `embed_children_task.cache_policy.compute_key` / `embed_entities_task.cache_policy.compute_key` directly (not the SWE's mocked test) with `texts=["hello","world"]`: `embedding_identity="voyage:voyage-3.5:1024"` vs `"voyage:voyage-4:1024"` → keys differ for both tasks; two calls with the same identity but different `opik_trace_headers` → identical keys for both tasks. PASS.
- Break path 4 (env-override propagation, hostile-ish config path): `TREE_MODELS__SEARCH_EMBEDDING__MODEL=voyage-3.5 uv run python -c "..."` → `app_config.models.search_embedding.model == 'voyage-3.5'`, `search_embedding_identity() == 'voyage:voyage-3.5:1024'`. Without the override, same process → `'voyage:voyage-4:1024'`. The escape hatch really moves the cache identity, matching Story 3. PASS.
- Break path 5 (external doc verification — prices and dimensions): fetched `https://docs.voyageai.com/docs/pricing` and `/docs/embeddings` directly (curl). Pricing table matches exactly: `voyage-4-large` $0.12, `voyage-4` $0.06, `voyage-4-lite` $0.02, `voyage-code-4` $0.12. Embeddings table confirms 32K context, 1024-d default, Matryoshka 256/512/1024/2048 for all four, and legacy `voyage-3-lite` still lists 512-d. PASS — the SWE's un-run `tech-docs` verification is independently confirmed correct.

**Acceptance criteria**
- [x] PASS — Both embedding blocks `voyage/voyage-4/1024` from YAML and `EmbeddingConfig()` defaults — `tests/unit/config/test_app_config.py::TestEmbeddingDefaults::test_both_blocks_default_to_voyage_4_1024` passes; asserts against the real `configs/default.yaml` via `_DEFAULT_CONFIG_PATH`, not the frozen fixture. Also reproduced live: `python -c "from tree.models.get_model import search_embedding_identity; print(search_embedding_identity())"` → `voyage:voyage-4:1024`.
- [x] PASS — Dimension table (4-series 1024, legacy `voyage-3-lite` 512, unknown id raises `ModelError` naming `_MODEL_NATIVE_DIMENSIONS`) — `tests/unit/models/test_voyage_embedding.py::TestNativeDimensions` (7 params) passes; confirmed live `VoyageTextEmbeddingModel(api_key='k').dimensions == 1024` with no explicit model.
- [x] PASS — Price table (`voyage-4-large` 0.12, `voyage-4` 0.06, `voyage-4-lite` 0.02, `voyage-code-4` 0.12, legacy `voyage-3.5` 0.06, unknown → 0.0) — `tests/unit/test_observability.py::TestVoyage4Prices` passes; prices independently verified against live docs.voyageai.com/docs/pricing (see break path 5).
- [x] PASS — YAML price map == Pydantic default map — `tests/unit/config/test_app_config.py::test_yaml_price_map_matches_code_default` passes, reads the real `default.yaml` via `yaml.safe_load(_DEFAULT_CONFIG_PATH...)`.
- [x] PASS — `embedding_identity` required kwarg on both task bodies + cache-key behaviour — `tests/unit/memory/test_pipeline.py::TestEmbedTaskCacheIdentity` passes; independently reproduced against the real (unmocked) `cache_policy.compute_key` of both tasks (break path 3) and the required-kwarg TypeError (break path 2).
- [x] PASS — README: `grep -n "voyage-multimodal-3\` / 1024" apps/memory/README.md` → no match (exit 1); both `models.*_embedding` bullets now read `voyage` / `voyage-4` / 1024; `models.llm` bullet also fixed to `gemini-3.1-flash-lite`.
- [x] PASS — `grep -rn "voyage-3.5" apps/memory/src apps/memory/configs` → 9 matches, all either legacy price/dimension table rows (`app_config.py:346`, `voyage_embedding.py:128-129`, `default.yaml:184,186`) or docstring/comment prose naming the legacy id as an example (`app_config.py:51,330-331`, `get_model.py:131`, `default.yaml:177`, `voyage_embedding.py:9`) — no runtime default left at `voyage-3.5`.
- [x] PASS — `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green, `make env-status` → local (evidence below).

**Evidence**
```
$ make env-status
Env target: local (.env)

$ make memory-format-check && make memory-lint-check
293 files already formatted
All checks passed!

$ make pre-commit
prettier / ruff check / ruff format / biome check (harness) ... Passed

$ make memory-tests
============================ 3134 passed in 48.25s =============================
(base-branch stash re-run for warning provenance: 3105 passed, same single opik UserWarning)

$ python -c "from tree.models.get_model import get_search_embedding_model; ..."
model: voyage-4
n= 1 dim= 1024

$ TREE_MODELS__SEARCH_EMBEDDING__MODEL=voyage-3.5 python -c "..."
search_embedding.model = voyage-3.5
identity = voyage:voyage-3.5:1024

$ python -c "embed_children_task.cache_policy.compute_key(...)"
embed-children old!=new: True trace1==trace2: True
embed-entities old!=new: True trace1==trace2: True

$ python -c "await _embed_children([])"
TypeError: _embed_children() missing 1 required keyword-only argument: 'embedding_identity'

$ grep -n "embed_children_task\|embed_entities_task" apps/memory/src/tree/orchestrator.py
(no matches)
```

**Other issues found**
- None blocking. `search_embedding_identity()`'s 3-part scope (vs. the 4-part `provider:model:dimensions:role` the glossary/ADR-009 eventually specify) is a deliberate, documented, in-scope deferral to #135/#136 — checked against `docs/glossary.md` and `docs/adrs/009_embedding_roles_and_modal_embedding_catalog.md`, consistent.
- `deploy/modal_vllm_embedding.py` references `voyageai/voyage-4-nano` — unrelated Modal-catalog work explicitly out of scope for #134, not a regression.

**VERDICT: PASS**
