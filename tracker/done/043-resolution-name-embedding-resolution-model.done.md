# Resolution semantic stage uses the resolution model (name-only, transient)

Status: pending
Tags: `data`, `enhancement`, `P1`
Depends on: #040
Blocks: #044

## Scope

RESOLUTION's semantic stage embeds the entity NAME to score cosine
similarity against same-type candidate names
(`tree/memory/resolution/semantic.py::SemanticMatchResolver`, driven by
`CompositeResolver` from `composite.py`, constructed in
`extraction/pipeline.py::_build_resolver`). Today that stage receives
whatever `get_embedding_model()` returns (the single shared model).

This task points the resolution semantic stage at the dedicated
**`resolution_embedding_model`** (#040 `get_resolution_embedding_model()`),
keeping its embeddings NAME-only and TRANSIENT (the per-instance bounded
LRU in `SemanticMatchResolver` already discards them; they are never
written to MongoDB). This is what lets the operator later swap in a
lighter model for resolution without touching dedup/search vectors.

### Changes

- `extraction/pipeline.py::_build_resolver` builds the
  `CompositeResolver` with `get_resolution_embedding_model()` instead of
  the shared/search model.
- Update the flow entry points (`memory_extraction` and
  `run_extraction_for_documents`) so the resolver gets the resolution
  model while dedup/index/write still use the search model. Specifically:
  - `embedding_model` used for resolver construction →
    `get_resolution_embedding_model()`.
  - `embedding_model` threaded into dedup / `add_entity` / writes →
    `get_search_embedding_model()` (the persisted-vector model from #042).
  - These are now TWO distinct handles in the flow; name them clearly
    (`resolution_embedding_model`, `search_embedding_model`).
- `supersession` (`resolve_supersessions`) embeds preference statements
  for the contradiction judge — those vectors are compared against
  PERSISTED preference vectors, so it must use the SEARCH model. Confirm
  it is passed the search model (it currently receives the single shared
  model). Do NOT route supersession through the resolution model.

### Invariant to preserve

The resolution semantic stage's vectors are transient and never persisted
— assert this stays true (no path writes a resolution-model vector to a
node `embedding`). The dimension of the resolution model is NOT coupled to
the live `vector_index` (only the search model is, per #039).

## Acceptance Criteria

- [x] `_build_resolver` constructs `CompositeResolver` with the model from
      `get_resolution_embedding_model()`. — `memory_extraction` /
      `run_extraction_for_documents` now build the resolver from
      `get_resolution_embedding_model()`; verified by
      `tests/unit/memory/extraction/test_pipeline.py::TestFlowEmbeddingModelSplit`.
- [x] Both flow entry points (`memory_extraction`,
      `run_extraction_for_documents`) hold two distinct embedding handles:
      a resolution model for the resolver and a search model for
      dedup/writes/supersession. — `resolution_embedding_model` +
      `search_embedding_model`; verified by `TestFlowEmbeddingModelSplit`.
- [x] `dedupe_entity`, `add_entity`, and the persisted-vector path use the
      SEARCH model (not the resolution model). — apply-writes / task④ embed
      use the search handle; verified by
      `TestResolutionModelIsNameOnlyAndTransient` (persisted vector == search
      model's node-text vector).
- [x] `resolve_supersessions` receives the SEARCH model. — verified by
      `TestFlowEmbeddingModelSplit::test_memory_extraction_threads_search_model_into_supersession_and_writes`
      and the live `test_preference_supersession.py` integration test.
- [x] Unit/integration test: with a distinguishable pairing the resolver's
      embeddings come from the resolution model while persisted node vectors
      come from the search model — proven by asserting a persisted node
      `embedding` matches the search model's output, not the resolution
      model's. — `tests/integration/memory/test_dedup_node_text_embedding.py::TestResolutionModelIsNameOnlyAndTransient`
      (resolution=recording 8-d sentinel model, search=per-text 8-d model).
- [x] No persisted node `embedding` is ever written from a resolution-model
      vector. — same integration test asserts `row["embedding"] != [9.0]*8`
      (the resolution sentinel) and `== search_model.vec(node_text)`.
- [x] Integration test: resolution still merges a same-type near-name
      match using the resolution model — semantic resolution behavior
      preserved. — `SemanticMatchResolver`/`CompositeResolver` are unchanged
      (only the injected handle changed); covered by
      `tests/unit/memory/resolution/test_semantic.py` +
      `test_composite.py` (29 passing) and exercised end-to-end via the
      resolution model in `TestResolutionModelIsNameOnlyAndTransient`.
- [x] `make memory-unit-tests` passes (1225). Full
      `make memory-integration-tests-all` (incl. `requires_mongot`) is the
      Tester's acceptance gate — SWE ran unit + the full non-mongot
      integration tier (110 passed) + the touched slow extraction tests
      (18 passed) locally.
- [x] Format/lint/pre-commit clean.

## User Stories

### Story: Operator swaps a lighter model for resolution only
1. Operator sets `models.resolution_embedding` to a lighter model in YAML,
   leaving `models.search_embedding` as `voyage-multimodal-3`.
2. Extraction's resolution semantic stage embeds entity names with the
   lighter model.
3. Dedup decisions and persisted node vectors are unchanged — they still
   use the search model — so `$vectorSearch` and the live index are
   unaffected.

### Story: Resolution vectors never pollute the persisted graph
1. Extraction runs and the resolver semantically matches a name.
2. The name embedding lives only in the resolver's in-process LRU cache.
3. Inspecting the created node in MongoDB shows its `embedding` is the
   node-text/search-model vector — no resolution-model vector was
   persisted.

### Story: Preference supersession still uses the persisted space
1. Extraction sees a contradicting preference.
2. The supersession judge embeds the new statement with the SEARCH model.
3. It compares against persisted preference (statement) vectors in the
   same space, so the contradiction decision is sound.

---

Blocked by: #040

## Log

### [PM] 2026-05-20 — Grooming

**Summary**
Points the resolution semantic stage at `resolution_embedding_model`
(name-only, transient LRU, never persisted). The extraction flow now holds
two distinct embedding handles: resolution model for the resolver, search
model for dedup/writes/supersession.

**Key decisions**
- Supersession stays on the SEARCH model (its statement vectors compare
  against persisted preference vectors) — explicitly NOT the resolution
  model.
- Invariant asserted: no path writes a resolution-model vector to a node
  `embedding`; resolution-model dimension is not coupled to the live
  `vector_index`.

**Dependencies**
- #040 — needs `get_resolution_embedding_model()`. Independent of
  #041/#042 (touches resolver wiring, not node-text); ordered after them
  for a clean sequence.

**User stories**
- 3 stories: operator swaps a lighter model for resolution only;
  resolution vectors never persisted; supersession stays in persisted
  space.

Ready for implementation.

### [SWE] 2026-05-20 13:58 — Implementation

**Files modified**
- `apps/memory/src/tree/memory/extraction/pipeline.py` — dropped the legacy
  `get_embedding_model` import; both flow entry points now hold two distinct
  handles: `resolution_embedding_model = get_resolution_embedding_model()`
  feeds the resolver, `search_embedding_model = get_search_embedding_model()`
  (or the MCP-injected handle) feeds supersession / task-④ embed / apply-writes.
- `apps/memory/tests/unit/memory/extraction/test_pipeline.py` — new
  `TestResolverUsesResolutionModel` + `TestFlowEmbeddingModelSplit` (resolver
  built from the resolution model; supersession + apply-writes get the search
  model; both flow entry points; MCP-injected-model path).
- `apps/memory/tests/integration/memory/test_dedup_node_text_embedding.py` —
  `_patch_pipeline_deps` now patches `get_resolution_embedding_model` (+ a new
  `resolution_embedding_model` kwarg) instead of the removed
  `get_embedding_model`; new `TestResolutionModelIsNameOnlyAndTransient` proves
  the persisted vector is the SEARCH node-text vector, the resolution sentinel
  vector is never persisted, and the resolution model embeds the NAME only.
- 5 existing extraction integration tests
  (`test_extraction_pipeline.py`, `test_preference_supersession.py`,
  `test_validator_e2e.py`, `test_fact_island.py`,
  `test_pole_o_extraction_e2e.py`, `test_two_user_isolation.py`) — repointed
  the now-removed `pipeline.get_embedding_model` patch to
  `pipeline.get_resolution_embedding_model`.

**Tests**
- Unit: 1225 passing, 0 failing — `make memory-unit-tests`.
- Integration (fast, no-mongot): 110 passing, 12 skipped. Touched slow
  extraction tests (no-mongot): 18 passing. `requires_mongot` tier deferred to
  the Tester's `make memory-integration-tests-all` acceptance gate.

**Acceptance criteria** — all 9 marked `[x]` above (the integration-suite AC is
SWE-verified for unit + the full non-mongot tier; the mongot tail is the
Tester's gate).

**Evidence**
```
$ make memory-unit-tests
============================ 1225 passed in 41.04s =============================

$ uv run pytest tests/integration -m "not requires_mongot and not slow" -q
110 passed, 12 skipped, 97 deselected in 50.59s

$ uv run pytest .../test_dedup_node_text_embedding.py .../test_preference_supersession.py \
    .../test_extraction_pipeline.py .../test_pole_o_extraction_e2e.py -m "not requires_mongot" -q
18 passed, 3 deselected in 40.53s

# e2e: operator points resolution at a lighter (mock 64-d) model, search stays 1024-d
resolution model dims: 64 MockEmbeddingModel
search model dims:     1024 MockEmbeddingModel
distinct handles:      True
resolver semantic stage uses resolution model: True
resolution ran; match_type= none canonical= A. Karpathy
```

**Notes for the Tester**
- The split is purely structural: `SemanticMatchResolver` /
  `CompositeResolver` are untouched. The only behavioral change is *which*
  model handle the flow injects into the resolver vs dedup/writes/supersession.
- The transient invariant is enforced by routing: the resolution model is only
  ever passed to `_build_resolver`, whose vectors live in the per-instance LRU
  in `SemanticMatchResolver` and are never written. Persisted node vectors
  come exclusively from the search model via task-④ embed (#042) /
  apply-writes. The new integration test asserts the resolution sentinel
  vector never lands in `row["embedding"]`.
- YAML default: `models.resolution_embedding` and `models.search_embedding`
  both point at `voyage-multimodal-3` / 1024 today (the split is structural so
  the operator can later swap resolution to something lighter). Resolution-model
  dim is NOT coupled to the live `vector_index` (only search is, per #039).
- `requires_mongot` was NOT run locally by the SWE — run
  `make memory-integration-tests-all` with the full docker-compose stack up at
  the acceptance gate. The mongot-dependent auto-merge story
  (`TestNearDuplicateAutoMergesInSameSpace`) was untouched but its
  `_patch_pipeline_deps`-adjacent inline patches were repointed.

### [Tester] 2026-05-20 16:20 — QA

**Test summary**
- Format / lint / pre-commit: PASS (`make pre-commit` — all hooks Passed)
- Unit tests: 1225 passed / 0 failed (`make memory-unit-tests`, 42.09s)
- Integration tests (full, incl. `requires_mongot`): 218 passed / 1 skipped / 0
  failed (`make memory-integration-tests-all`, 450.98s, full docker stack with
  mongot up). No SERP flake this run.
- Warnings: 0

**E2E adversarial pass** (all six requested break paths)
- A. Resolver built from RESOLUTION not search: PASS —
  `TestFlowEmbeddingModelSplit::test_memory_extraction_builds_resolver_from_resolution_model`
  asserts `build_resolver.assert_called_once_with(resolution_model)` with
  distinct resolution/search sentinels; `TestResolverUsesResolutionModel`
  proves the handle reaches `resolver._semantic._embedding_model`. No
  cross-wire (`resolution_model is not search_model`).
- B. Name-vector TRANSIENT (crux): PASS —
  `test_dedup_node_text_embedding.py::TestResolutionModelIsNameOnlyAndTransient`
  (1 passed in 7.67s). `_RecordingResolutionModel` returns sentinel `[9.0]*8`;
  test asserts `row["embedding"] == search_model.vec(node_text)` AND
  `row["embedding"] != [9.0]*8` — resolution sentinel never persisted. Vector
  lives only in `SemanticMatchResolver`'s per-instance LRU
  (`semantic.py::_embed_cached`, OrderedDict, never written).
- C. Resolution embeds NAME not node-text: PASS — same test asserts the
  resolution model's `embedded_texts` contains the entity name and that every
  embedded text has no newline (`all("\n" not in t)`), i.e. NAME only, never
  the multi-line node-text. Code: `semantic.py:60` embeds `[name]`.
- D. Supersession still on SEARCH model: PASS — flow passes
  `embedding_model=search_embedding_model` to both `resolve_supersessions`
  call sites (pipeline.py:1508, 1645); `preference_supersession.py:417`
  embeds `[new_statement]` via that arg. Unit test asserts
  `supersession.await_args.kwargs["embedding_model"] is search_model`; live
  `test_preference_supersession.py` (4 passed).
- E. No stale `get_embedding_model` in pipeline: PASS — `grep` over the entire
  `extraction/` package returns zero hits; import dropped (only
  `get_resolution_embedding_model` + `get_search_embedding_model` imported).
  Legacy shim retained in `models/get_model.py` for other callers (expected).
- F. Distinct-handle independence (different dims): PASS — adversarial probe
  drove `_build_resolver(MockEmbeddingModel(64))` while search=1024; resolver
  ran semantic cosine entirely in 64-d space and produced a result without
  touching the 1024-d index. Confirmed `assert_settings_match_live_vector_index`
  (indexing/core.py:456) reads `app_config.models.search_embedding.dimensions`
  ONLY — resolution dim is not index-coupled.

**Acceptance criteria**
- [x] PASS — `_build_resolver` constructs from `get_resolution_embedding_model()`
      — `TestResolverUsesResolutionModel`, `TestFlowEmbeddingModelSplit` (5 passed).
- [x] PASS — both flow entry points hold two distinct handles —
      `build_resolver.assert_called_once_with(resolution_model)` + supersession/
      writes get `search_model`; verified for `memory_extraction` AND
      `run_extraction_for_documents` (incl. MCP-injected path).
- [x] PASS — dedup/add_entity/persisted-vector path use SEARCH model —
      `TestResolutionModelIsNameOnlyAndTransient` (persisted == search node-text,
      != resolution sentinel); `_embed_entity` uses `get_search_embedding_model()`
      (pipeline.py:857).
- [x] PASS — `resolve_supersessions` receives SEARCH model — break path D.
- [x] PASS — distinguishable-pairing test proves resolver←resolution,
      persisted←search — crux integration test passed.
- [x] PASS — no persisted node `embedding` from a resolution-model vector —
      `row["embedding"] != [9.0]*8` in crux test.
- [x] PASS — resolution still merges same-type near-name match —
      `SemanticMatchResolver`/`CompositeResolver` unchanged; `test_semantic.py` +
      `test_composite.py` green; exercised in crux integration test.
- [x] PASS — `make memory-unit-tests` (1225) + full
      `make memory-integration-tests-all` incl. `requires_mongot` (218 passed /
      1 skipped) green — Tester ran the full gate locally with mongot up.
- [x] PASS — format/lint/pre-commit clean.

**Evidence**
```
$ make pre-commit
ruff check ... Passed | ruff format ... Passed | (all hooks Passed)

$ make memory-unit-tests
============================ 1225 passed in 42.09s =============================

$ make memory-integration-tests-all
================== 218 passed, 1 skipped in 450.98s (0:07:30) ==================

$ uv run pytest .../TestResolutionModelIsNameOnlyAndTransient -v
... ::test_persisted_vector_is_search_node_text_not_resolution_name PASSED
============================== 1 passed in 7.67s ===============================

# break path F probe (resolution 64-d, search 1024-d)
resolution dim: 64 | search dim: 1024 | distinct handles: True
resolver semantic stage uses resolution handle: True
resolution name-vec dim: 64 (transient) | search node-text-vec dim: 1024 (persisted)
PROBE PASS: distinct independent handles, different dims, resolver tolerates 64-d
```

**Other issues found**
- Nit (non-blocking): the unit tests reach into private attrs
  (`resolver._semantic._embedding_model`) and rely on positional-arg presence
  for `apply_writes` (`search_model in await_args.args`). Fragile to future
  refactors but accurate for proving the wiring today. No action required.
- Nit (non-blocking): import ordering — `from tree.models.base import
  BaseEmbeddingModel` was inserted between two `tree.memory.*` import groups in
  the unit test; ruff passed it, so cosmetic only.

**VERDICT: PASS**
