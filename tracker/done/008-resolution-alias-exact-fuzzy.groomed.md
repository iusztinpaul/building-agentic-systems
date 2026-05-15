# Resolution module: types, base, alias, exact, fuzzy resolvers

Status: pending
Tags: `resolution`, `rapidfuzz`, `unit-tests`
Depends on: #007
Blocks: #009, #011, #012

## Scope

Introduce the new `tree.memory.resolution` package, the shared resolver types, and the three non-embedding resolvers (Alias → Exact → Fuzzy). This task is pure logic — no Mongo, no embeddings, no Prefect. The Semantic resolver and the `CompositeResolver` chain land in #009.

Reference: `notes/RESOLUTION_MODULE.md` §3–§6 (Neo4j source algorithm). Port mechanics, not Neo4j-isms.

### Files touched

- `apps/memory/src/tree/memory/resolution/__init__.py` — re-export public types.
- `apps/memory/src/tree/memory/resolution/types.py` — `ResolvedEntity`, `ResolutionMatch`, `_normalize`.
- `apps/memory/src/tree/memory/resolution/base.py` — `BaseResolver` protocol + ABC.
- `apps/memory/src/tree/memory/resolution/alias.py` — `AliasMatchResolver`.
- `apps/memory/src/tree/memory/resolution/exact.py` — `ExactMatchResolver`.
- `apps/memory/src/tree/memory/resolution/fuzzy.py` — `FuzzyMatchResolver` (lazy `rapidfuzz` import).
- `apps/memory/tests/unit/memory/resolution/__init__.py`
- `apps/memory/tests/unit/memory/resolution/test_normalize.py`
- `apps/memory/tests/unit/memory/resolution/test_alias.py`
- `apps/memory/tests/unit/memory/resolution/test_exact.py`
- `apps/memory/tests/unit/memory/resolution/test_fuzzy.py`
- `apps/memory/pyproject.toml` — add `rapidfuzz>=3` to `[project].dependencies`.

### Types (`types.py`)

```python
def _normalize(name: str) -> str:
    """Lowercase + whitespace-collapse. Used as canonical-form key."""
    # " Alice   Smith " -> "alice smith"
```

`ResolvedEntity` (Pydantic `BaseModel`):
- `original_name: str`
- `canonical_name: str`
- `entity_type: NodeType` (from `tree.entities.knowledge_graph`)
- `confidence: float`
- `match_type: Literal["alias","exact","fuzzy","semantic","batch","none"]`
- `merged_from: list[str] = []`

`ResolutionMatch` (Pydantic `BaseModel`):
- `candidate_name: str`
- `similarity_score: float`
- `match_type: Literal["alias","exact","fuzzy","semantic"]`

### Base (`base.py`)

- `class BaseResolver(Protocol)` for typing, plus a concrete `class AbstractResolver(ABC)` with `_normalize` as a `@staticmethod` calling `types._normalize`.
- Method signatures (abstract):
  - `resolve(self, name: str, entity_type: NodeType, candidate_names: Iterable[str], existing_aliases: Mapping[str, list[str]] | None = None) -> ResolvedEntity`
  - `resolve_batch(self, entities: Iterable[tuple[str, NodeType]], ...) -> list[ResolvedEntity]` (default impl loops `resolve`).
- All return values include `match_type="none"` and `confidence=0.0`, `canonical_name=name` when no match.

### Alias resolver (`alias.py`)

- Walks `existing_aliases: dict[canonical_name, list[str]]`.
- Returns first canonical whose alias list, normalized, contains `_normalize(name)`.
- `confidence=1.0`, `match_type="alias"`. `canonical_name=` the matched canonical (NOT the input — important).
- If `existing_aliases` is `None` or empty → `match_type="none"`.

### Exact resolver (`exact.py`)

- Case-insensitive equality post-normalization.
- Iterate `candidate_names`; first equality wins. `confidence=1.0`, `match_type="exact"`.
- Empty `candidate_names` → `match_type="none"`.

### Fuzzy resolver (`fuzzy.py`)

- Constructor: `FuzzyMatchResolver(*, threshold: float = 0.85, scorer_name: str = "token_sort_ratio")`.
- Lazy import in `__init__`:
  ```python
  try:
      from rapidfuzz import fuzz
      self._fuzz = fuzz
      self._scorer = getattr(fuzz, scorer_name)
      self._is_available = True
  except ImportError:
      self._fuzz = None
      self._scorer = None
      self._is_available = False
  ```
- Public `@property is_available -> bool`.
- `resolve()`:
  - If not `is_available` → return `match_type="none"`.
  - Compute `_scorer(_normalize(name), _normalize(cand)) / 100` for each candidate. Pick the HIGHEST score above `threshold`.
  - `confidence = best_score`, `match_type="fuzzy"`, `canonical_name = matched_candidate` (original casing preserved).
- Threshold is `0.85` (score domain 0..1 after dividing by 100).

## Acceptance Criteria

- [x] `apps/memory/pyproject.toml` lists `rapidfuzz>=3` and `uv --directory apps/memory lock` is clean.
- [x] `from tree.memory.resolution import ResolvedEntity, ResolutionMatch, AliasMatchResolver, ExactMatchResolver, FuzzyMatchResolver, _normalize` succeeds.
- [x] `_normalize("  Alice   Smith ") == "alice smith"`; unit test covers leading/trailing whitespace, embedded multi-space, mixed-case, unicode pass-through (e.g. `"José"` → `"josé"`).
- [x] `AliasMatchResolver.resolve("alice", PERSON, candidate_names=["Alice Smith"], existing_aliases={"Alice Smith": ["alice", "as"]})` returns `canonical_name="Alice Smith"`, `confidence=1.0`, `match_type="alias"`.
- [x] **Alias precedence test:** when `name` exists both as an exact candidate AND as an alias under a DIFFERENT canonical, the alias result wins (because chain order will run alias first in #009 — this resolver returns the alias canonical regardless).
- [x] `ExactMatchResolver.resolve("ALICE", PERSON, candidate_names=[])` returns `match_type="none"`.
- [x] `ExactMatchResolver.resolve("Alice", PERSON, candidate_names=["alice"])` returns `canonical_name="alice"`, `confidence=1.0`, `match_type="exact"` (case-insensitive equality).
- [x] **Fuzzy "highest wins" test:** `FuzzyMatchResolver.resolve("alice smith", PERSON, candidate_names=["Alyce Smyth", "Alice Smyth", "Bob"])` returns `canonical_name="Alice Smyth"` (highest token_sort_ratio above 0.85), NOT `"Alyce Smyth"` (which appears first but scores lower).
- [x] **Fuzzy below threshold:** `FuzzyMatchResolver(threshold=0.85).resolve("alice", PERSON, candidate_names=["Robert"])` returns `match_type="none"`.
- [x] **Fuzzy missing rapidfuzz test:** with `monkeypatch.setitem(sys.modules, 'rapidfuzz', None)` BEFORE constructing the resolver, `FuzzyMatchResolver().is_available is False` and `.resolve(...)` returns `match_type="none"` without raising. (Use `pytest-mock`'s `mocker` per project conventions.)
- [x] `resolve_batch` default impl returns one `ResolvedEntity` per input tuple in order.
- [x] Every function/method has typed parameters and return type (including `-> None` where applicable) per CLAUDE.md.
- [x] Tests follow project conventions: AAA, `test_*` naming, `mocker` fixture for monkeypatching, `@pytest.mark.parametrize` for the threshold / scorer matrix.
- [x] `make memory-unit-tests` green; zero new warnings; `make memory-format-check && make memory-lint-check && make pre-commit` clean.

## User Stories

### Story: Pipeline maps an abbreviation to its canonical via alias lookup
1. The pipeline has `existing_aliases = {"International Business Machines": ["IBM", "ibm corp"]}`.
2. It calls `AliasMatchResolver().resolve("ibm", entity_type=ORGANIZATION, candidate_names=["IBM"], existing_aliases=existing_aliases)`.
3. The result is `canonical_name="International Business Machines"`, `confidence=1.0`, `match_type="alias"` — the alias wins over the exact match because the resolver knows about the alias table.

### Story: Fuzzy resolver picks the best near-match
1. The pipeline has three candidates with misspellings of "Alice Smith".
2. It calls `FuzzyMatchResolver(threshold=0.85).resolve("alice smith", PERSON, candidates)`.
3. The resolver scores each candidate via `token_sort_ratio`, returns the one with the highest score that clears 0.85.

### Story: rapidfuzz missing in CI image
1. CI runs in a constrained image where `rapidfuzz` is absent.
2. `FuzzyMatchResolver()` constructs without raising; `.is_available` reports `False`.
3. Calls to `.resolve(...)` return `match_type="none"` so the upstream composite chain in #009 can skip fuzzy gracefully.

### Story: Type-stable result for every caller
1. Every resolver returns a `ResolvedEntity` for every input — never `None`, never raises on "no match".
2. Callers branch on `result.match_type == "none"`, not on exceptions.

---

Blocked by: #007

## Log

### [PM] 2026-05-14 — Grooming

**Summary**
Ports the non-embedding half of the resolver chain from `neo4j_agent_memory`. Pure logic; sets up the resolver protocol used by #009 (semantic + composite) and #011 (`add_entity`).

**Key decisions**
- Lazy rapidfuzz import + `is_available` flag → graceful degradation in environments without the dep.
- `_normalize` lives in `types.py` and is reused everywhere; matches Neo4j-source semantics (lower + whitespace collapse).
- Fuzzy resolver picks HIGHEST above threshold, not first — explicit unit test guards against the regression.
- Alias resolver returns the canonical (not input) even when the input also matches an exact candidate; chain order in #009 will exploit this to short-circuit.

**Dependencies**
- #007 — uses `NodeType` from the data model.

**User stories**
- 4 stories covering: alias precedence, fuzzy "best wins", missing-dep fallback, type-stable API.

**Open questions**
- None.

Ready for implementation.

### [SWE] 2026-05-14 — Implementation

**Files modified**
- `apps/memory/pyproject.toml` — added `rapidfuzz>=3` to `[project].dependencies`.
- `apps/memory/uv.lock` — relock added `rapidfuzz==3.14.5`.
- `apps/memory/src/tree/memory/resolution/__init__.py` — public re-exports.
- `apps/memory/src/tree/memory/resolution/types.py` — `_normalize`, `ResolvedEntity`, `ResolutionMatch`.
- `apps/memory/src/tree/memory/resolution/base.py` — `BaseResolver` Protocol, `AbstractResolver` ABC with default `resolve_batch` + `_no_match` helper.
- `apps/memory/src/tree/memory/resolution/alias.py` — `AliasMatchResolver` (walks `existing_aliases`, returns canonical key, conf=1.0).
- `apps/memory/src/tree/memory/resolution/exact.py` — `ExactMatchResolver` (normalize-equality, first hit wins, preserves candidate casing).
- `apps/memory/src/tree/memory/resolution/fuzzy.py` — `FuzzyMatchResolver` with lazy `rapidfuzz` import, `is_available` property, HIGHEST-score-above-threshold semantics.
- `apps/memory/tests/unit/memory/resolution/__init__.py` — package marker.
- `apps/memory/tests/unit/memory/resolution/test_normalize.py` — 10 parametrized cases (case, whitespace, unicode, empty).
- `apps/memory/tests/unit/memory/resolution/test_alias.py` — 7 tests inc. alias-precedence-over-exact, None/empty alias map, batch ordering.
- `apps/memory/tests/unit/memory/resolution/test_exact.py` — 9 tests inc. parametrized normalization matrix, empty candidates, first-match wins, batch ordering.
- `apps/memory/tests/unit/memory/resolution/test_fuzzy.py` — 12 tests inc. `mocker.patch.dict('sys.modules', {'rapidfuzz': None})` ImportError fallback, highest-wins regression guard, scorer/threshold parametrize, batch ordering.

**Tests**
- Unit: 626 passing (38 new), 0 failing, 0 warnings — `make memory-unit-tests`.
- Integration: N/A — pure logic, no infra changes. Will be exercised end-to-end through #009/#011 once the composite chain is wired.

**Acceptance criteria** — see checkboxes above; all marked. Spot-mapping:
- AC "pyproject lists rapidfuzz>=3 + lock clean" — verified by `uv lock` resolving 261 packages with `rapidfuzz v3.14.5`.
- AC "from tree.memory.resolution import ... succeeds" — verified by smoke test below.
- AC "_normalize covers whitespace/case/unicode" — `tests/unit/memory/resolution/test_normalize.py::test_normalize_canonicalizes_whitespace_and_case` (10 parametrized cases).
- AC alias canonical contract — `test_alias.py::TestAliasMatchResolver::test_returns_canonical_when_alias_matches`.
- AC alias-precedence — `test_alias.py::TestAliasMatchResolver::test_alias_wins_over_exact_candidate_with_different_canonical`.
- AC exact empty candidates → none — `test_exact.py::TestExactMatchResolver::test_no_match_when_candidates_empty`.
- AC exact case-insensitive — `test_exact.py::TestExactMatchResolver::test_case_insensitive_match_preserves_candidate_casing`.
- AC fuzzy highest wins — `test_fuzzy.py::TestFuzzyMatchResolverScoring::test_highest_score_above_threshold_wins`.
- AC fuzzy below threshold → none — `test_fuzzy.py::TestFuzzyMatchResolverScoring::test_no_match_when_best_score_below_threshold`.
- AC fuzzy missing rapidfuzz → graceful — `test_fuzzy.py::TestFuzzyMatchResolverAvailability::test_falls_back_when_rapidfuzz_missing` (uses `mocker.patch.dict("sys.modules", {"rapidfuzz": None})` BEFORE construction).
- AC `resolve_batch` order — three `test_resolve_batch_returns_one_result_per_input_in_order` tests across alias/exact/fuzzy.
- AC typed signatures — all params + returns typed (`-> None`, `-> ResolvedEntity`, `-> list[ResolvedEntity]`, `-> bool`); `ruff check` passes.
- AC AAA + naming + parametrize — verified in test files; fuzzy uses `@pytest.mark.parametrize` for scorer and threshold matrices.
- AC QA green — output below.

**Evidence**

```
$ make memory-format-check && make memory-lint-check
uv run ruff format --check src/ tests/ scripts/ deploy/
175 files already formatted
uv run ruff check src/ tests/ scripts/ deploy/
All checks passed!

$ make pre-commit
uv run --project apps/memory pre-commit run --all-files
Validate pyproject.toml..............................(no files to check)Skipped
prettier.................................................................Passed
ruff check...............................................................Passed
ruff format..............................................................Passed
biome check (harness)....................................................Passed

$ make memory-unit-tests
... tests/unit/memory/resolution/test_alias.py .......                       [ 85%]
    tests/unit/memory/resolution/test_exact.py .........                     [ 87%]
    tests/unit/memory/resolution/test_fuzzy.py ............                  [ 89%]
    tests/unit/memory/resolution/test_normalize.py ..........                [ 90%]
    ...
============================= 626 passed in 24.32s =============================

$ uv run python -c "from tree.memory.resolution import (ResolvedEntity, ResolutionMatch, AliasMatchResolver, ExactMatchResolver, FuzzyMatchResolver, _normalize); print(_normalize('  Alice   Smith '))"
alice smith
```

**Notes**
- This is the pure-logic half of the resolver chain. No Mongo, no embeddings, no Prefect — all 38 new tests are unit tests with `pytest-mock`'s `mocker`. The composite/semantic resolver and the wiring into `add_entity` land in #009 / #011.
- Soft-join contract from #007 (two entries with different `_id`s may share a `canonical_name`) is respected: `AliasMatchResolver` returns the canonical-name *string* (mapping key), not a node `_id`. This is exactly the contract #009 needs.
- `FuzzyMatchResolver` uses HIGHEST score above threshold; explicit regression test (`test_highest_score_above_threshold_wins`) guards against the "first hit wins" trap. Threshold tested at both 0.5 and 0.99 to confirm gating works.
- Lazy import path uses `mocker.patch.dict("sys.modules", {"rapidfuzz": None})` BEFORE constructing the resolver (so the `from rapidfuzz import fuzz` raises `ImportError` at `__init__`). `is_available` flips to `False`; `.resolve` returns `match_type="none"` without raising. The `is_available` property is preserved for #009's composite to skip fuzzy gracefully.
- End-to-end runtime exercise: the only "runtime" surface this task adds is the public API. Verified with a one-liner that imports every symbol and exercises `_normalize` — output above.
- Did NOT commit. Tester goes next per `/night` workflow.

### [Tester] 2026-05-14 — QA

**Test summary**
- Format check: PASS (`make memory-format-check` → 175 files already formatted)
- Lint check: PASS (`make memory-lint-check` → All checks passed!)
- Pre-commit: PASS (prettier, ruff check, ruff format, biome — all passed)
- Unit tests: PASS — 626 passed in 22.09s, **0 warnings** (`make memory-unit-tests`)
- Integration tests: SKIPPED — pure-logic task, no infra. Exercised end-to-end via REPL instead.

**E2E adversarial pass**

1. **Happy path** — `_normalize("  Alice   Smith ")` → `'alice smith'`. Public re-exports all import successfully. AliasMatchResolver / ExactMatchResolver / FuzzyMatchResolver each construct, resolve correctly, and conform to `BaseResolver` Protocol (`isinstance(r, BaseResolver) is True` for all three). PASS.

2. **Break path 1 — `_normalize` with adversarial unicode whitespace.**
   Inputs tried: `'\t\nAlice\r\nSmith\t'`, `'Alice\xa0Smith'` (NBSP), `'Alice  Smith'` (EM+EN), `'Alice　Smith'` (CJK ideographic space), `'JOSÉ  GARCÍA'`, 1000-char string, `''`, `'   '`, `'Alice\x00Smith'` (NUL byte), `'一郎  Tanaka'`.
   All collapse to expected `'alice smith'` / `'josé garcía'` / `'一郎 tanaka'`; empty + all-whitespace → `''`; NUL byte passes through (not whitespace per Unicode) → `'alice\x00smith'`. Python's `str.split()` correctly handles all Unicode whitespace categories. **PASS** — `_normalize` is robust to every whitespace edge case I could construct.

3. **Break path 2 — `AliasMatchResolver` corner cases.**
   - Input that equals a dict KEY but is NOT in any alias list → returns `match_type="none"`. Per spec literal text ("walks `existing_aliases` ... contains `_normalize(name)`"), the resolver only looks inside the alias lists, not at the keys. This matches the spec and the SWE's hand-off note (`canonical_name = canonical_name value, not node _id`). Calling code that wants the key-as-implicit-alias behavior must include the canonical name in its own alias list. **PASS — spec-conformant, not a bug**, but documenting as an "Other issues found" note for the caller-facing pipeline (#009/#011) since this is a subtle contract.
   - Input matching alias lists under TWO different canonicals → first-key-wins deterministically (Python 3.7+ dict insertion order). Reproducible. PASS.
   - Empty alias list under one key, match under another → correctly skips the empty list and matches the populated one. PASS.
   - `existing_aliases={}` and `existing_aliases=None` → both return `match_type="none"`. PASS (covered by `test_no_match_when_aliases_empty_or_none`).
   - `candidate_names=None` → resolver does NOT iterate candidate_names in alias.py, so it succeeds and returns the alias match. PASS (mild inconsistency with fuzzy/exact, see below).

4. **Break path 3 — `FuzzyMatchResolver` rapidfuzz missing (lazy-import sentinel).**
   `sys.modules['rapidfuzz'] = None` BEFORE construction → `from rapidfuzz import fuzz` raises `ImportError` at `__init__` → `is_available` flips to `False` → `.resolve(...)` returns `match_type="none"` without raising. Verified `type(type(inst).is_available).__name__ == 'property'` — it is a real `@property`, not a method. PASS. Matches AC and concern 3.

5. **Break path 4 — "Best score above threshold wins, not first".**
   - AC scenario re-run live: `'alice smith'` vs `['Alyce Smyth', 'Alice Smyth', 'Bob']`, threshold=0.85. Live rapidfuzz scores: Alyce=0.82 (below threshold), Alice Smyth=0.91, Bob=0.00. Result: `canonical='Alice Smyth'`, confidence=0.9091, match_type='fuzzy'. PASS.
   - Stronger "best vs first" test: `'John Smith Jr.'` vs `['John Smith', 'Jon Smyth', 'John Smiths']`, threshold=0.7. Scores: John Smith=0.833, Jon Smyth=0.696, John Smiths=0.80. Result: `canonical='John Smith'` (the BEST at 0.833) — picked over `John Smiths` even though both clear threshold and `John Smiths` comes later. Confirms "highest wins, not last seen". PASS.

6. **Break path 5 — Fuzzy edge inputs.**
   - Empty `candidate_names` → `match_type="none"`. PASS.
   - Threshold=1.5 (impossible) → `match_type="none"`. PASS.
   - Bad `scorer_name="nonexistent_scorer"` → raises `AttributeError` at construction (`module 'rapidfuzz.fuzz' has no attribute ...`). This is **eager fail** — happens before any resolve call. Acceptable: it's a misconfig that should fail loudly, not silently degrade. NOTE: not covered by an explicit test, but spec doesn't require it.
   - Empty input vs `['']` → matches (token_sort_ratio(empty, empty)=100). Returns `canonical=''`, match_type='fuzzy'. Edge case but consistent.
   - Tie at threshold=0.5 with `['abd','abe']` for input `'abc'` → first one with strictly higher score wins; ties go to the first because the comparison is `score > best_score` (strict). Deterministic. PASS.

7. **Break path 6 — `candidate_names=None` parameter contract.**
   - `FuzzyMatchResolver.resolve(..., candidate_names=None)` → raises `TypeError: 'NoneType' object is not iterable`.
   - `ExactMatchResolver.resolve(..., candidate_names=None)` → same TypeError (untested, but same control flow).
   - `AliasMatchResolver.resolve(..., candidate_names=None)` → does NOT raise (never iterates `candidate_names`).
   - The Protocol types `candidate_names: Iterable[str]` (not `| None`), so passing `None` is a type contract violation by the caller. Acceptable — Python typically lets the iteration error speak for itself. Mild inconsistency between resolvers, but not a defect.

**Acceptance criteria**

- [x] PASS — `pyproject.toml` lists `rapidfuzz>=3` and lock is clean.
      Evidence: `apps/memory/pyproject.toml:34` adds `"rapidfuzz>=3"`; `uv.lock` resolves `rapidfuzz==3.14.5` with `cp314` wheels.
- [x] PASS — Public re-exports import successfully.
      Evidence: `uv run python -c "from tree.memory.resolution import ResolvedEntity, ResolutionMatch, AliasMatchResolver, ExactMatchResolver, FuzzyMatchResolver, _normalize; print(_normalize('  Alice   Smith '))"` → `alice smith`. Confirmed `AbstractResolver` and `BaseResolver` also exported.
- [x] PASS — `_normalize("  Alice   Smith ") == "alice smith"`; whitespace/case/unicode covered.
      Evidence: `tests/unit/memory/resolution/test_normalize.py::test_normalize_canonicalizes_whitespace_and_case` (10 parametrized cases including `\t\nAlice\nSmith\t`, `José`, unicode). Plus my live adversarial pass against NBSP/EM/EN/CJK ideographic spaces — all correctly normalized.
- [x] PASS — `AliasMatchResolver.resolve("alice", PERSON, candidate_names=["Alice Smith"], existing_aliases={"Alice Smith": ["alice","as"]})` returns the alias canonical.
      Evidence: `test_alias.py::TestAliasMatchResolver::test_returns_canonical_when_alias_matches`; live-confirmed.
- [x] PASS — Alias precedence: alias wins over exact-match candidate under a different canonical.
      Evidence: `test_alias.py::TestAliasMatchResolver::test_alias_wins_over_exact_candidate_with_different_canonical`.
- [x] PASS — `ExactMatchResolver` with empty candidates → `match_type="none"`.
      Evidence: `test_exact.py::TestExactMatchResolver::test_no_match_when_candidates_empty`.
- [x] PASS — `ExactMatchResolver` case-insensitive equality.
      Evidence: `test_exact.py::test_case_insensitive_match_preserves_candidate_casing` + `test_normalization_collapses_case_and_whitespace` (4 parametrized cases).
- [x] PASS — Fuzzy "highest wins" — picks `Alice Smyth` not `Alyce Smyth`.
      Evidence: `test_fuzzy.py::TestFuzzyMatchResolverScoring::test_highest_score_above_threshold_wins`; live-confirmed with rapidfuzz scoring (Alyce=0.82, Alice Smyth=0.91).
- [x] PASS — Fuzzy below threshold → `match_type="none"`.
      Evidence: `test_fuzzy.py::test_no_match_when_best_score_below_threshold` + `test_threshold_controls_match` (parametrized 0.5 vs 0.99).
- [x] PASS — Fuzzy missing rapidfuzz → `is_available is False`, `.resolve(...)` returns `match_type="none"` without raising.
      Evidence: `test_fuzzy.py::TestFuzzyMatchResolverAvailability::test_falls_back_when_rapidfuzz_missing` (uses `mocker.patch.dict("sys.modules", {"rapidfuzz": None})` BEFORE construction); live-confirmed.
- [x] PASS — `resolve_batch` default impl returns one `ResolvedEntity` per input in order.
      Evidence: `test_resolve_batch_returns_one_result_per_input_in_order` in all three test files; live-confirmed `[a,b,c]` ordering preserved.
- [x] PASS — All function/method signatures typed.
      Evidence: `make memory-lint-check` clean (ruff enforces); `src/tree/memory/resolution/{types,base,alias,exact,fuzzy}.py` all have full parameter + return-type annotations including `-> None` on `__init__`.
- [x] PASS — Tests follow AAA / `test_*` naming / `mocker` fixture / `@pytest.mark.parametrize`.
      Evidence: every test file uses Arrange/Act/Assert blocks with comments; fuzzy tests use `mocker: MockerFixture` (not `monkeypatch`); parametrize used in `test_normalize`, `test_alias::test_no_match_when_aliases_empty_or_none`, `test_exact::test_normalization_collapses_case_and_whitespace`, `test_fuzzy::test_custom_scorer_can_be_selected` and `test_threshold_controls_match`.
- [x] PASS — Unit tests green; **zero warnings**; format/lint/pre-commit clean.
      Evidence: 626 passed in 22.09s, grep for warning output returned empty.

**Evidence**

```
$ make memory-format-check
175 files already formatted

$ make memory-lint-check
All checks passed!

$ make pre-commit
Validate pyproject.toml..............................(no files to check)Skipped
prettier.................................................................Passed
ruff check...............................................................Passed
ruff format..............................................................Passed
biome check (harness)....................................................Passed

$ make memory-unit-tests
... tests/unit/memory/resolution/test_alias.py .......                       [ 85%]
    tests/unit/memory/resolution/test_exact.py .........                     [ 87%]
    tests/unit/memory/resolution/test_fuzzy.py ............                  [ 89%]
    tests/unit/memory/resolution/test_normalize.py ..........                [ 90%]
============================= 626 passed in 22.09s =============================

$ uv run python -c "<adversarial _normalize pass>"
'whitespace+case' -> 'alice smith'
'empty' -> ''
'all whitespace' -> ''
'tabs+newlines' -> 'alice smith'
'NBSP between words' -> 'alice smith'
'EM+EN spaces' -> 'alice smith'
'CJK ideographic space' -> 'alice smith'
'unicode upper' -> 'josé garcía'
'1000 chars' -> <1000 a's>
'NUL byte' -> 'alice\x00smith'
'two spaces only' -> ''
'mixed scripts' -> '一郎 tanaka'

$ uv run python -c "<fuzzy concern 4 with threshold=0.7>"
canonical='John Smith'  conf=0.8333
# (best at 0.833 picked over 'John Smiths' at 0.80 which came later)
```

**Other issues found (non-blocking, follow-up signals for #009/#011)**

1. **Alias dict-key-as-implicit-alias semantics.** The spec, the implementation, and the tests all agree: `AliasMatchResolver` only matches when the input is *in* an alias list, NOT when it equals a dict KEY. This is correct for #008's stated contract, but callers wiring the extraction pipeline (#011/#012) should ensure they include `canonical` in `existing_aliases[canonical]` if they want the canonical's own name to self-match through this resolver — or rely on `ExactMatchResolver` for that case. Worth a one-liner in #011's groomed task body. **Not a fix for #008.**
2. **`candidate_names=None` contract.** Fuzzy and exact will `TypeError` on `None`; alias will not (it never iterates). Type-annotation contract is `Iterable[str]` (no `| None`), so this is the caller's bug, but a one-line guard in `AbstractResolver._normalize_candidates` (materialize-or-empty) would make the surface symmetric. **Cosmetic / not a fix for #008.**
3. **Misconfigured `scorer_name`** raises `AttributeError` at `__init__`. Eager fail is the right design; no test covers it. Optional follow-up: add a parametrize entry asserting the AttributeError for grep-ability. **Cosmetic / not a fix.**

**VERDICT: PASS**

Every AC verified with evidence (test name + live re-run where applicable). Adversarial e2e pass exercised 6 break paths across `_normalize`, alias, and fuzzy — every one behaved correctly or surfaced contract-consistent failures. Zero warnings. Public surface, Protocol conformance, lazy import, threshold gating, best-above-threshold semantics, and rapidfuzz-missing fallback all live-confirmed. Hand off to PM for acceptance.
