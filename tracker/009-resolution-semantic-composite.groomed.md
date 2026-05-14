# Resolution module: semantic resolver + composite chain

Status: pending
Tags: `resolution`, `semantic`, `embeddings`, `composite`
Depends on: #008
Blocks: #011, #012

## Scope

Land the semantic (embedding) resolver and the `CompositeResolver` that chains Alias → Exact → Fuzzy → Semantic with short-circuit semantics and type strictness. This is the public entry point that #011 (`add_entity`) and #012 (the pipeline) consume.

Reference: `notes/RESOLUTION_MODULE.md` §6–§7 and `RESOLUTION_DEDUP_ALGORITHM.md` §3.

### Files touched

- `apps/memory/src/tree/memory/resolution/semantic.py` — `SemanticMatchResolver`.
- `apps/memory/src/tree/memory/resolution/composite.py` — `CompositeResolver`.
- `apps/memory/src/tree/memory/resolution/__init__.py` — extend re-exports.
- `apps/memory/tests/unit/memory/resolution/test_semantic.py`
- `apps/memory/tests/unit/memory/resolution/test_composite.py`
- (Uses `MockEmbeddingModel` from `tree.models.fake_model` for unit tests — already shipped.)

### Semantic resolver (`semantic.py`)

Constructor:
```python
SemanticMatchResolver(
    embedding_model: BaseEmbeddingModel,
    *,
    threshold: float = 0.80,
    cache_max_size: int = 10_000,
)
```

Behavior:
- For each candidate, compute cosine similarity between `embedding_model.embed(name)` and `embedding_model.embed(candidate)`.
- Clamp similarity to `[0.0, 1.0]` (defensive — small negative values from floating-point are common).
- Pick HIGHEST above `threshold`.
- `match_type="semantic"`, `confidence=best_score`, `canonical_name=matched_candidate`.

**Bounded LRU cache** (per-instance, per-name):
- `self._cache: OrderedDict[str, list[float]] = OrderedDict()`.
- On lookup hit: move to end (most recent). On miss: compute, insert at end, evict from front if `len > cache_max_size`.
- Cache keyed on `_normalize(name)` so case/whitespace variants share an entry.
- Public `clear_cache(self) -> None` method.

### Composite resolver (`composite.py`)

Constructor:
```python
CompositeResolver(
    embedding_model: BaseEmbeddingModel | None = None,
    *,
    fuzzy_threshold: float = 0.85,
    semantic_threshold: float = 0.80,
    type_strict: bool = True,
    embedding_cache_max_size: int = 10_000,
)
```

- Constructs `AliasMatchResolver`, `ExactMatchResolver`, `FuzzyMatchResolver(threshold=fuzzy_threshold)`.
- If `embedding_model is not None`, constructs `SemanticMatchResolver(embedding_model, threshold=semantic_threshold, cache_max_size=embedding_cache_max_size)`. Otherwise the chain stops after Fuzzy.
- On construction: if `fuzzy.is_available is False`, log INFO once ("rapidfuzz not installed; CompositeResolver will skip the fuzzy stage") and remove fuzzy from the active chain.

Methods:

- `async resolve(self, name: str, entity_type: NodeType, candidate_names: Iterable[str], existing_aliases: Mapping[str, list[str]] | None = None) -> ResolvedEntity`
  - Apply type filter (`type_strict=True` → candidates already pre-filtered by caller; resolver re-checks via the `existing_entities` map keyed on `(name, type)` passed via `resolve_with_types`).
  - Run resolvers in order: Alias → Exact → Fuzzy → Semantic. **First non-`none` result wins** (short-circuit).
  - If all return `none` → return `ResolvedEntity(original_name=name, canonical_name=name, entity_type=entity_type, confidence=0.0, match_type="none")`.

- `async resolve_batch(self, entities: Iterable[tuple[str, NodeType]], ...) -> list[ResolvedEntity]`
  - Default: loop `resolve`. Override the `match_type` to `"batch"` ONLY if the caller asks for it explicitly (see `resolve_with_types` below).

- `async resolve_with_types(self, entities: Iterable[tuple[str, NodeType]], existing_entities: Mapping[NodeType, list[str]], existing_aliases: Mapping[str, list[str]]) -> list[ResolvedEntity]`
  - **Preferred entry point** for #012's resolve task: candidate lists pre-grouped by type.
  - For each `(name, type)`, looks up `existing_entities[type]` and calls `resolve`. Applies the type filter strictly (`type_strict=True`): a PERSON named "Alice" must not match a TASK named "Alice".

- `find_matches(self, name: str, entity_type: NodeType, top_k: int = 5) -> list[ResolutionMatch]`:
  - Stub that raises `NotImplementedError("Reserved for future review tooling — see RESOLUTION_MODULE.md §7.4")`.
  - Reserves the API for #014's human-review extensions without committing to an implementation now.

## Acceptance Criteria

- [x] `SemanticMatchResolver(MockEmbeddingModel(), threshold=0.80).resolve("alice", PERSON, ["Alice Smith", "Bob"])` returns the highest-similarity candidate above 0.80 with `match_type="semantic"`.
- [x] **Clamping test:** with a mock model that returns embeddings producing cosine `-1e-9` (floating-point artifact), the resolver clamps to `0.0` and does not raise.
- [x] **LRU cache size test:** after `cache_max_size + 1000` distinct lookups, `len(resolver._cache) == cache_max_size`.
- [x] **LRU eviction order test:** insert keys `k0..k_{N}` with `N = cache_max_size`. Access `k0`. Insert one more. Assert `k1` (oldest non-accessed) was evicted, NOT `k0`.
- [x] `SemanticMatchResolver.clear_cache()` empties the cache; subsequent lookups recompute.
- [x] `CompositeResolver` constructed with `MockEmbeddingModel()` runs all 4 stages.
- [x] `CompositeResolver` constructed with `embedding_model=None` runs only Alias → Exact → Fuzzy.
- [x] **Chain order test:** input that matches both an alias (canonical `"X"`) AND a fuzzy candidate (canonical `"Y"`) returns `canonical_name="X"`, `match_type="alias"` — alias wins by chain order.
- [x] **Type-strict test:** with `type_strict=True`, `resolve_with_types([("Alice", PERSON)], existing_entities={PERSON: [], TASK: ["Alice"]}, ...)` returns `match_type="none"` for the PERSON entry — the TASK candidate is ignored.
- [x] **Batch idempotency test:** `resolve_batch` called twice with the same input returns identical canonicals for repeated `(name, type)` tuples.
- [x] **Missing rapidfuzz INFO log:** with `monkeypatch.setitem(sys.modules, 'rapidfuzz', None)` BEFORE construction, `CompositeResolver(...)` constructs without raising, emits exactly ONE INFO log naming `rapidfuzz`, and the chain runs Alias → Exact → Semantic (fuzzy stage absent). Assert via `caplog`.
- [x] `CompositeResolver(...).find_matches("alice", PERSON)` raises `NotImplementedError` with a message referencing `"RESOLUTION_MODULE.md §7.4"`.
- [x] **Empty-chain "none" result:** with empty `existing_entities[PERSON]` and `embedding_model=None`, every resolve returns `match_type="none"`, `canonical_name=original_name`, `confidence=0.0`.
- [x] `make memory-unit-tests` green; zero new warnings; format/lint/pre-commit clean.

## User Stories

### Story: Pipeline resolves a batch of mentions in one call
1. Task ③ in the extraction pipeline (#012) calls `composite.resolve_with_types(entities, existing_entities, existing_aliases)`.
2. Each `(name, type)` pair returns a `ResolvedEntity` whose `canonical_name` is either an existing canonical (when matched) or the input name (when no match).
3. Downstream `embed.map` (task ④) and `dedupe` (task ⑤) consume the `canonical_name` field uniformly.

### Story: Alias short-circuit avoids needless embedding lookups
1. Composite gets `name="ibm"`, type=ORGANIZATION, `existing_aliases={"IBM": ["ibm"]}`.
2. Alias resolver matches with `confidence=1.0`.
3. The chain returns immediately — the semantic resolver is never invoked. (Asserted by `MockEmbeddingModel.embed_call_count == 0`.)

### Story: Semantic resolver eats memory budget gracefully
1. The pipeline resolves 50,000 distinct names over its lifetime.
2. The semantic resolver's bounded LRU caps memory at `cache_max_size=10_000` entries.
3. Eviction is least-recently-used: hot names stay in cache.

### Story: PERSON named "Alice" never matches TASK named "Alice"
1. `type_strict=True` is the default.
2. Even with identical surface form and identical embedding, the type filter blocks the cross-type match.

---

Blocked by: #008

## Log

### [PM] 2026-05-14 — Grooming

**Summary**
Adds the semantic resolver (with bounded LRU cache) and the composite chain. Chain order is fixed (Alias → Exact → Fuzzy → Semantic) and short-circuits on first hit. `find_matches` is stubbed for future review tooling.

**Key decisions**
- Composite re-checks type strictness via `resolve_with_types` even though callers pre-filter, to prevent the rare case where a misconfigured candidate list bleeds across types.
- Semantic cache is per-instance bounded LRU (`OrderedDict`), not a global LRU; isolates flow runs from each other.
- `find_matches` is intentionally unimplemented — reserves the API surface without committing semantics that #014 hasn't designed yet.

**Dependencies**
- #008 (alias/exact/fuzzy + types).

**User stories**
- 4 stories covering: batched resolve, alias short-circuit, bounded-cache memory, type strictness.

**Open questions**
- None.

Ready for implementation.

### [SWE] 2026-05-14 — Implementation

**Files modified**
- `apps/memory/src/tree/memory/resolution/semantic.py` — new `SemanticMatchResolver` with bounded LRU embedding cache, cosine-similarity clamp, and async `resolve` / `resolve_batch`.
- `apps/memory/src/tree/memory/resolution/composite.py` — new `CompositeResolver`; runs Alias → Exact → Fuzzy → Semantic, short-circuits on first hit, drops Fuzzy with one INFO log if `rapidfuzz` is missing, applies `type_strict` in `resolve_with_types`, exposes `find_matches` stub raising `NotImplementedError` with §7.4 ref.
- `apps/memory/src/tree/memory/resolution/__init__.py` — re-exports `CompositeResolver` and `SemanticMatchResolver`.
- `apps/memory/tests/unit/memory/resolution/test_semantic.py` — new (14 tests): scripted-embedding model for deterministic similarity, clamp test, LRU size + LRU-eviction-order test, `clear_cache`, normalized cache key, `MockEmbeddingModel` smoke test, cosine edge cases.
- `apps/memory/tests/unit/memory/resolution/test_composite.py` — new (15 tests): construction with/without embedding model and with rapidfuzz absent (caplog asserts ONE INFO log), chain-order short-circuit (alias beats fuzzy; alias skips semantic — `embed_call_count == 0`), fall-through Alias → Exact → Fuzzy → Semantic, no-match cases, type-strict cross-type block + non-strict union, batch idempotency, `find_matches` stub raises with §7.4 string.

**Tests**
- Unit: 655 passing, 0 failing (29 new tests added; 0 new warnings) — `make memory-unit-tests` output below.
- Integration: N/A — pure in-memory algorithms; no infra changes.

**Acceptance criteria** — all checkboxes ticked (see body). Each AC mapped to a concrete test:
- Highest-above-threshold → `test_semantic.TestSemanticMatchResolverBasic::test_returns_highest_above_threshold` (+ `test_with_mock_embedding_model_returns_well_formed_result` exercises `MockEmbeddingModel`).
- Clamping → `test_semantic.TestSemanticMatchResolverClamping::test_clamps_negative_floating_point_artifact_to_zero` (resolve-level) and `test_clamp_via_static_helper` (helper-level).
- LRU size → `test_lru.test_cache_size_capped_after_overflow` (cache_max=50, +1000 inserts).
- LRU eviction order → `test_lru_eviction_keeps_recently_accessed`.
- `clear_cache` → `test_clear_cache_empties_and_forces_recompute`.
- All-4-stages composite construction → `test_constructs_with_all_four_stages_when_embedding_model_passed`.
- 3-stage composite (no embedding) → `test_constructs_without_semantic_when_no_embedding_model`.
- Chain order (alias beats fuzzy) → `test_alias_wins_over_fuzzy_candidate` and short-circuit via `test_alias_short_circuit_skips_semantic` (asserts `embed_call_count == 0`).
- Type-strict → `test_type_strict_blocks_cross_type_match`.
- Batch idempotency → `test_repeated_inputs_yield_identical_canonicals`.
- Missing rapidfuzz INFO → `test_skips_fuzzy_when_rapidfuzz_unavailable_and_logs_once` (via `caplog`).
- `find_matches` stub → `test_find_matches_raises_with_section_ref`.
- Empty-chain "none" → `test_empty_chain_with_no_candidates_returns_none` + `test_returns_none_when_no_chain_member_matches`.

**Evidence**
```
$ make memory-format-fix && make memory-lint-fix
... 1 file reformatted (composite.py — line break), 178 unchanged; lint: All checks passed!

$ make memory-format-check && make memory-lint-check
179 files already formatted
All checks passed!

$ make pre-commit
Validate pyproject.toml..............................(no files to check)Skipped
prettier.................................................................Passed
ruff check...............................................................Passed
ruff format..............................................................Passed
biome check (harness)....................................................Passed

$ make memory-unit-tests
...
tests/unit/memory/resolution/test_composite.py ...............           [ 84%]
tests/unit/memory/resolution/test_semantic.py ..............             [ 91%]
...
============================= 655 passed in 22.50s =============================
```

**Notes**
- The composite's `resolve` is async because the semantic stage requires `await`. The three leaf resolvers (alias/exact/fuzzy) remain synchronous and are called directly inside the async method — no asyncio wrapping required.
- `_ScriptedEmbeddingModel` is duplicated across `test_semantic.py` and `test_composite.py` rather than moved to a `conftest.py` — keeps each file self-contained, matching the pattern in `test_alias.py` / `test_fuzzy.py`. Happy to extract if the Tester prefers.
- `_no_match` for the composite is inlined as a `ResolvedEntity(...)` literal — `CompositeResolver` doesn't extend `AbstractResolver` (it composes them), so reaching into the ABC felt wrong.
- The "MockEmbeddingModel runs all 4 stages" AC is exercised structurally (`test_constructs_with_all_four_stages_when_embedding_model_passed` checks the internal stages are wired) plus the per-stage fall-through tests (`test_falls_through_to_semantic_when_fuzzy_misses` uses a scripted model end-to-end). No combined "MockEmbeddingModel + all four leaf assertions in one test" — chose narrower tests for clearer failure attribution.
- NOT RUN: integration tests — task is pure in-memory algorithms, no DB / Prefect / Modal touched. End-to-end run via the pipeline lands in #012.
- DID NOT COMMIT — waiting for Tester.

### [Tester] 2026-05-14 — QA

**Test summary**
- Format check: PASS (`179 files already formatted`)
- Lint check: PASS (`All checks passed!`)
- Pre-commit: PASS (`prettier / ruff check / ruff format / biome` all Passed)
- Unit tests: 655 passed, 0 failed, **0 warnings** (`make memory-unit-tests`, 21.96s)
- Integration tests: N/A — pure in-memory algorithms, no DB / Prefect / Modal touched. Will be exercised end-to-end in #012.

**E2E adversarial pass**

*9 specific concern scenarios (from hand-off):*
- Concern 1 — chain order (alias > exact > semantic when input matches multiple stages): `resolve("apple", PERSON, ["apple","apple computer"], existing_aliases={"Apple Inc":["apple"]})` → `canonical="Apple Inc"`, `match_type="alias"`, `embed_calls=0`. **PASS** — alias wins over exact/semantic.
- Concern 2 — alias short-circuit avoids embed: `resolve("ibm", PERSON, ["acme"], {"IBM":["ibm"]})` → `match="alias"`, `embed_call_count=0`. **PASS**.
- Concern 3 — type_strict filtering: `resolve_with_types([("Alice", PERSON)], {PERSON: [], TASK: ["Alice"]}, {})` → `match="none"` (TASK candidate ignored). Verified leaf `ExactMatchResolver.resolve("Alice", PERSON, ["Alice"], None)` returns `match="exact"` — confirms leaves don't filter types, composite does. **PASS**.
- Concern 4 — bounded LRU (`cache_max_size=3`, 5 inserts): cache size = 3, evicted = `{a, b}`, retained = `{c, d, e}`. Touch `c` (MRU), insert `f` → cache = `[e, c, f]`. `d` evicted (oldest non-touched), `c` retained. LRU semantics, not FIFO. `clear_cache()` → size 0. **PASS**.
- Concern 5 — missing rapidfuzz INFO log: with `sys.modules['rapidfuzz']=None`, construction succeeds, `_fuzzy is None`, exactly 1 INFO log on `tree.memory.resolution.composite` across construction + 5 resolves. Chain runs Alias → Exact → Semantic correctly (verified end-to-end with scripted model). **PASS**. *Note:* a second `CompositeResolver(...)` construction emits another log (i.e. one log **per construction**, not globally one-shot). The AC scenario tests a single construction with `caplog`, so it asserts the correct behavior — consider this a "PASS with note" if a global one-shot was actually desired (the spec text "exactly ONE INFO log" naturally reads as per-construction here).
- Concern 6 — `find_matches` stub: raises `NotImplementedError("Reserved for future review tooling — see RESOLUTION_MODULE.md §7.4")`. **PASS**.
- Concern 7 — `resolve_batch` with repeated inputs: `[("Alice", PERSON), ("alice", PERSON), ("Alice Smith", PERSON)]` against `["Alice Smith"]` at `fuzzy_threshold=0.5` → all three canonicalize to `"Alice Smith"`. `match_type`s are `fuzzy/fuzzy/exact` (per-resolve), no `"batch"` synthetic match_type — matches the spec ("override to `batch` ONLY if caller asks explicitly"; no such caller knob exists yet, which is consistent). **PASS**.
- Concern 8 — composite inline `no-match` byte-equal to leaf's `_no_match`: both produce `{original_name, canonical_name=name, entity_type, confidence=0.0, match_type='none', merged_from=[]}` — verified with `model_dump()` equality. **PASS**.
- Concern 9 — 0 warnings on `make memory-unit-tests`. **PASS**.

*Additional adversarial break paths (Tester-chosen):*
- **Empty name input**: `resolve("", PERSON, ["Alice"], None)` → `match='none'`, no crash. `resolve("   ", PERSON, ["Alice"], None)` → `match='none'`. PASS.
- **Empty name + empty candidate** (normalized equality): both normalize to `""` → `match='exact'`. Defensible — empty matches empty.
- **Unicode** (`"Élise"` vs `"élise"`): normalizes to same → `match='exact'`. PASS.
- **Large input** (100k-char name): exact match works, no truncation/OOM. PASS.
- **Generator candidate_names**: composite materializes via `list(candidate_names)` before running stages — generator works correctly across chain. PASS.
- **Embedding model raises**: `RuntimeError("API down")` propagates to caller (acceptable; retries are the caller's job). PASS.
- **Concurrent resolves** (3 coroutines via `asyncio.gather`): all complete correctly; LRU cache OrderedDict isn't thread-shared, async cooperative scheduling fine. PASS.
- **Dimension mismatch in scripted embeddings** (`[1.0,0.0]` vs `[0.0,1.0,0.0]`): raises `ValueError: zip() argument 2 is longer than argument 1` — defensible (loud failure rather than silent wrong score). PASS (not specified by spec; defensive default is reasonable).
- **`None` vs `{}` aliases**: behaviorally equivalent — both fall through to next stage. PASS.
- **`resolve_with_types` with missing type bucket** (entity is PERSON, only TASK bucket exists): returns `match='none'`. No KeyError. PASS.
- **Alias-across-types** (alias map is type-agnostic; TASK input matching a PERSON alias): does match (alias wins, type strictness is composite-level over `existing_entities`, not over `existing_aliases`). This is a **known limitation carried over from #008** — `AliasMatchResolver`'s structure has no per-type alias maps. Worth flagging as a follow-up but **not a #009 FAIL** — the spec scopes `type_strict` to `resolve_with_types`'s `existing_entities` mapping, not to aliases.

**Acceptance criteria**
- [x] PASS — Semantic returns highest-above-threshold — `tests/unit/memory/resolution/test_semantic.py::TestSemanticMatchResolverBasic::test_returns_highest_above_threshold` and concern 1.
- [x] PASS — Clamping (cosine ≈ -1e-9 → 0.0) — `test_semantic.py::test_clamps_negative_floating_point_artifact_to_zero` + `test_clamp_via_static_helper`; `semantic.py:88-91` clamps explicitly.
- [x] PASS — LRU cache size cap — `test_semantic.py::test_cache_size_capped_after_overflow` (cache_max=50, +1000 inserts → len=50) + concern 4a (size 3 after 5 inserts).
- [x] PASS — LRU eviction order (touched entry survives) — `test_semantic.py::test_lru_eviction_keeps_recently_accessed` + concern 4b; `semantic.py:57` calls `move_to_end` on hit; `semantic.py:64-65` `popitem(last=False)` evicts head.
- [x] PASS — `clear_cache()` empties + forces recompute — `test_semantic.py::test_clear_cache_empties_and_forces_recompute` + concern 4c.
- [x] PASS — CompositeResolver(MockEmbeddingModel()) wires all 4 stages — `test_composite.py::test_constructs_with_all_four_stages_when_embedding_model_passed` (verifies `_alias/_exact/_fuzzy/_semantic` all set).
- [x] PASS — CompositeResolver(embedding_model=None) runs only Alias→Exact→Fuzzy — `test_composite.py::test_constructs_without_semantic_when_no_embedding_model` (verifies `_semantic is None`, `_fuzzy` set).
- [x] PASS — Chain order (alias canonical wins over fuzzy candidate's canonical) — `test_composite.py::test_alias_wins_over_fuzzy_candidate` + concern 1 (3-way collision: alias beats exact and semantic).
- [x] PASS — Type-strict blocks cross-type — `test_composite.py::test_type_strict_blocks_cross_type_match` + concern 3 (PERSON vs TASK with identical "Alice"). Non-strict path also verified (`test_type_strict_off_unions_across_types`).
- [x] PASS — Batch idempotency — `test_composite.py::test_repeated_inputs_yield_identical_canonicals` + concern 7.
- [x] PASS — Missing rapidfuzz INFO log (exactly once per construction) — `test_composite.py::test_skips_fuzzy_when_rapidfuzz_unavailable_and_logs_once` (asserts `len(rapidfuzz_logs)==1`) + concern 5 (5 subsequent resolves do NOT re-log).
- [x] PASS — `find_matches` stub raises with §7.4 ref — `test_composite.py::test_find_matches_raises_with_section_ref` + concern 6.
- [x] PASS — Empty-chain none result — `test_composite.py::test_empty_chain_with_no_candidates_returns_none` + concern 8 (byte-equal to leaf's `_no_match`).
- [x] PASS — Suite green, 0 warnings, format/lint/pre-commit clean — see Evidence block.

**Evidence**
```
$ make memory-format-check
179 files already formatted

$ make memory-lint-check
All checks passed!

$ make pre-commit
Validate pyproject.toml..............................(no files to check)Skipped
prettier.................................................................Passed
ruff check...............................................................Passed
ruff format..............................................................Passed
biome check (harness)....................................................Passed

$ make memory-unit-tests
... tests/unit/memory/resolution/test_composite.py ............... [ 84%]
    tests/unit/memory/resolution/test_semantic.py  ..............  [ 91%]
============================= 655 passed in 21.96s =============================
```

E2E concern run (output):
```
CONCERN 1: canonical='Apple Inc', match_type='alias', confidence=1.0, embed_calls=0
CONCERN 2: match='alias', canonical='IBM', embed_calls=0
CONCERN 3a (resolve_with_types strict): match='none', canonical='Alice'
CONCERN 3b (leaf exact, no type filter): match='exact'
CONCERN 4a (5 inserts cache_max=3): size=3 keys=['c', 'd', 'e']
CONCERN 4b (touch c, add f): keys=['e', 'c', 'f']
CONCERN 4c (clear_cache): size=0
CONCERN 5: rapidfuzz INFO logs (1 construction + 5 resolves) = 1
CONCERN 5b: 2nd CompositeResolver construction emits 1 log(s)
CONCERN 5c (fuzzy skipped, semantic active): match='semantic'
CONCERN 6: NotImplementedError msg='Reserved for future review tooling — see RESOLUTION_MODULE.md §7.4'
CONCERN 7: results=Alice->Alice Smith(fuzzy), alice->Alice Smith(fuzzy), Alice Smith->Alice Smith(exact)
CONCERN 8 leaf:      {'original_name': 'xyzzy', 'canonical_name': 'xyzzy', 'entity_type': <NodeType.PERSON: 'person'>, 'confidence': 0.0, 'match_type': 'none', 'merged_from': []}
CONCERN 8 composite: {'original_name': 'xyzzy', 'canonical_name': 'xyzzy', 'entity_type': <NodeType.PERSON: 'person'>, 'confidence': 0.0, 'match_type': 'none', 'merged_from': []}
```

**Other issues found** (non-blocking; orchestrator decides whether to spin off)
- `_ScriptedEmbeddingModel` is duplicated between `test_semantic.py` and `test_composite.py` (SWE already flagged this; matches existing pattern in `test_alias.py` / `test_fuzzy.py`). Cosmetic — no FAIL.
- `AliasMatchResolver` is type-agnostic: an alias listed under one type's canonical can match an input of a different type. Not a #009 FAIL (type_strict per spec only governs `existing_entities`, not the alias map), but the limitation may bite #012's pipeline if alias maps span types. Worth a follow-up task to plumb type into the alias map.
- The "rapidfuzz missing" INFO log fires once per `CompositeResolver` construction, not once globally. The current test asserts the per-construction case (as the AC scenario describes), so this matches the contract. If a long-lived process re-constructs the resolver many times, the INFO will repeat — likely fine, but worth knowing.
- `resolve_batch` is purely sequential; no in-batch dedup or `match_type="batch"` synthesis is implemented. The spec explicitly says "ONLY if the caller asks for it explicitly" and there's no such knob — so the current minimal loop matches the spec. Hand-off note 7 framed this as "verify match_type='batch' for in-batch cache hits" — that synthetic value is *not* produced by this implementation, and per-spec it shouldn't be. Not a FAIL.
- Dim-mismatch embeddings raise `ValueError` from `zip(strict=True)` — defensible loud failure but unspecified in the spec; consider a friendlier error message if real embedding backends ever ship inconsistent dims.

**VERDICT: PASS**

All 14 acceptance criteria verified with concrete evidence (test + e2e run). Full suite green: 655 passed / 0 failed / 0 warnings. Format, lint, pre-commit all clean. E2E adversarial pass covered the 9 hand-off concerns plus 10 additional break paths (empty input, unicode, large input, generator candidates, embed-raise, concurrent resolves, dim mismatch, None vs {} aliases, missing type bucket, alias-across-types) — all behave as specified or defensibly. No security or convention regressions. Ready for SWE to commit and hand off to PM for acceptance review.

