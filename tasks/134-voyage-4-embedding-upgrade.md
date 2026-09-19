---
id: 134-voyage-4-embedding-upgrade
status: pending
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

- [ ] `app_config.models.search_embedding` and `.resolution_embedding` are both `provider="voyage", model="voyage-4", dimensions=1024` from YAML AND from `EmbeddingConfig()` defaults — `tests/unit/config/test_app_config.py::TestEmbeddingDefaults::test_both_blocks_default_to_voyage_4_1024`.
- [ ] `VoyageTextEmbeddingModel(api_key="k").dimensions == 1024` with no explicit model; each of `voyage-4-large`, `voyage-4`, `voyage-4-lite`, `voyage-code-4` → `1024`; `voyage-3-lite` still → `512`; an unknown id with no `output_dimension` still raises `ModelError` naming `_MODEL_NATIVE_DIMENSIONS` — `tests/unit/models/test_voyage_embedding.py::TestNativeDimensions`.
- [ ] `app_config.observability.cost_for("voyage-4", 1_000_000) == 0.06`, `voyage-4-large` → `0.12`, `voyage-4-lite` → `0.02`, `voyage-code-4` → `0.12`, `voyage-3.5` still → `0.06`, unknown model → `0.0` — `tests/unit/test_observability.py::TestVoyage4Prices`.
- [ ] The YAML price map and the Pydantic default price map have identical keys and values — `tests/unit/config/test_app_config.py::test_yaml_price_map_matches_code_default`.
- [ ] `_embed_children` and `_embed_entities` take `embedding_identity: str`; the helper returns `"voyage:voyage-4:1024"` for the default config; both tasks' computed cache keys differ between `embedding_identity="voyage:voyage-3.5:1024"` and `"voyage:voyage-4:1024"` for the same `texts`, and are equal when only `opik_trace_headers` differs — `tests/unit/memory/test_pipeline.py::TestEmbedTaskCacheIdentity`.
- [ ] `grep -n "voyage-multimodal-3\` / 1024" apps/memory/README.md` → no match; the two bullets read `voyage` / `voyage-4` / 1024.
- [ ] `grep -rn "voyage-3.5" apps/memory/src apps/memory/configs` matches only the legacy rows of the dimension + price tables and their docstrings.
- [ ] `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green (`make env-status` → local).

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
