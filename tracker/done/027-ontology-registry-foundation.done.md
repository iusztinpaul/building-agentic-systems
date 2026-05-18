# Ontology registry foundation (Phase 3, part 1 of 4)

Status: pending
Tags: `phase-3`, `ontology`, `registry`, `foundation`, `refactor`
Depends on: #026
Blocks: #028, #029, #030, #031, #032, #033

## Scope

Land the **extensible ontology registry** that replaces the closed `NodeType` and `EdgeType` `StrEnum`s. After this task, registering a new node/edge type is a `register_node_type(spec)` / `register_edge_type(spec)` call — adding a new line to an enum is no longer the way. **No behavior change ships in this task.** The existing six node types and nine edge types are retrofitted through the new API so the resulting registry holds *exactly* what's there today; downstream extraction, indexing, query, and MCP code keep using their current types via thin shims. This task is the foundation every Phase-3+ task builds on. See `plan.md:96–151` for the canonical design.

### Files touched

- `apps/memory/src/tree/entities/ontology.py` — replace the existing `NODE_PROPERTIES` / `EDGE_CONSTRAINTS` dicts with `NODE_REGISTRY: dict[str, NodeTypeSpec]` / `EDGE_REGISTRY: dict[str, EdgeTypeSpec]` and the four `register_*` functions. Re-register all six existing node types + nine existing edge types via the new API at import time.
- `apps/memory/src/tree/entities/knowledge_graph.py` — relax `KnowledgeGraphEntry.type` from `NodeType | EdgeType` to `str`; the field still serializes to the same strings on the wire. Add a model-level validator that on **node rows** (`kind == "node"`) the type must be present in `NODE_REGISTRY`, and on **edge rows** the type must be present in `EDGE_REGISTRY`. Keep `NodeType` and `EdgeType` enums as thin re-export shims that read their members from the registry (existing call sites importing `NodeType.PERSON` keep working).
- `apps/memory/src/tree/entities/ontology_tree_extensions.py` — NEW. Empty module with a docstring explaining it will hold Tree's `register_node_subtype("object", "task", ...)` etc. calls — those subtype registrations actually land in #028. This task only creates the file so the import path exists.
- `apps/memory/tests/unit/entities/test_ontology.py` — new tests for the registry API + retrofit equivalence.
- `apps/memory/tests/unit/entities/test_knowledge_graph.py` — extend to cover the relaxed-but-validated `type` field.

### Registry shape (canonical — copy-pasteable)

```python
# apps/memory/src/tree/entities/ontology.py

from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

@dataclass(frozen=True)
class NodeTypeSpec:
    name: str                                # snake_case identifier, e.g. "person"
    properties_schema: type[BaseModel]       # Pydantic model for type-specific fields
    description: str                         # human-readable; flows into LLM prompt
    subtypes: set[str] | None = None         # None = freeform; non-empty set = closed
    llm_extractable: bool = True             # set False for DOCUMENT, CHUNK

@dataclass(frozen=True)
class EdgeTypeSpec:
    name: str                                # snake_case identifier
    allowed_pairs: list[tuple[str, str]]     # [(source_type_name, target_type_name), ...]
    properties_schema: type[BaseModel] | None = None  # structural-edge property model; None for now
    description: str = ""
    llm_extractable: bool = False            # structural edges are False; will flip in #029 for related_to

NODE_REGISTRY: dict[str, NodeTypeSpec] = {}
EDGE_REGISTRY: dict[str, EdgeTypeSpec] = {}

def register_node_type(spec: NodeTypeSpec) -> None:
    """Register a node type. Idempotent on identical re-registration; raises
    ValueError on conflicting re-registration (different schema / subtypes)."""

def register_edge_type(spec: EdgeTypeSpec) -> None:
    """Register an edge type. Same idempotency contract."""

@dataclass(frozen=True)
class SubtypeSpec:
    name: str
    description: str
    extra_properties: type[BaseModel] | None = None

def register_node_subtype(
    parent_type: str,
    subtype: str,
    description: str = "",
    extra_properties: type[BaseModel] | None = None,
) -> None:
    """Add a subtype to an existing closed-subtype node type.
    Raises ValueError if parent_type is unknown or its subtypes are None
    (freeform — extension semantics don't apply)."""
```

`register_node_subtype` rewrites the parent's `NodeTypeSpec` in place (since it's `frozen`, technically a `NODE_REGISTRY[parent] = dataclasses.replace(...)` swap with `subtypes = parent.subtypes | {new_subtype}`). The `extra_properties` model, if given, is held in a parallel dict `SUBTYPE_EXTRAS: dict[tuple[str, str], type[BaseModel]]` keyed by `(parent_type, subtype)`. Lookups during validation (in #030) combine `parent.properties_schema` and `SUBTYPE_EXTRAS.get((parent, subtype))`.

### Retrofit list (exact mapping — no behavior change)

Re-register each existing type with `register_node_type` / `register_edge_type` at the bottom of `ontology.py`:

| Existing | Re-registered as |
|---|---|
| `NodeType.DOCUMENT` | `NodeTypeSpec(name="document", properties_schema=DocumentProperties, description=DocumentProperties.__doc__ or "...", subtypes=None, llm_extractable=False)` |
| `NodeType.CHUNK` | `NodeTypeSpec(name="chunk", properties_schema=ChunkProperties, description=..., subtypes=None, llm_extractable=False)` |
| `NodeType.PERSON` | `NodeTypeSpec(name="person", properties_schema=PersonProperties, description=..., subtypes=None, llm_extractable=True)` — closed-subtype set lands in #028 |
| `NodeType.TASK` | `NodeTypeSpec(name="task", properties_schema=TaskProperties, ..., llm_extractable=True)` — kept temporarily; #028 re-routes to `(object, task)` |
| `NodeType.EPISODE` | `NodeTypeSpec(name="episode", properties_schema=EpisodeProperties, ..., llm_extractable=True)` — kept temporarily; #028 re-routes to `(event, episode)` |
| `NodeType.PREFERENCE` | `NodeTypeSpec(name="preference", properties_schema=PreferenceProperties, ..., llm_extractable=True)` — typed slots land in #032 |
| Each `EdgeType` member | `EdgeTypeSpec(name=<str>, allowed_pairs=[<existing constraint pairs>], properties_schema=None, description=<existing constraint description>, llm_extractable=<True for RELATED_TO/TODO/EXPERIENCED/HAS, else False>)` |

After retrofit, `NODE_REGISTRY.keys() == {"document", "chunk", "person", "task", "episode", "preference"}` and `EDGE_REGISTRY.keys() == {"part_of", "next", "mentions", "referenced", "related_to", "todo", "experienced", "has", "same_as"}`. **Exact same prompt schema** must be emitted by `get_ontology_schema()` after this refactor — a snapshot/golden-file test pins this.

### Backward-compat shims

```python
# apps/memory/src/tree/entities/knowledge_graph.py
class NodeType(StrEnum):
    """Backward-compat shim. Built from NODE_REGISTRY at import time.
    New code should reference type names as strings or pull from NODE_REGISTRY.
    Deletion target: once #028–#032 land and call sites migrate."""
    DOCUMENT = "document"
    CHUNK = "chunk"
    PERSON = "person"
    TASK = "task"
    EPISODE = "episode"
    PREFERENCE = "preference"

class EdgeType(StrEnum):
    """Backward-compat shim (see NodeType)."""
    PART_OF = "part_of"
    NEXT = "next"
    MENTIONS = "mentions"
    REFERENCED = "referenced"
    RELATED_TO = "related_to"
    TODO = "todo"
    EXPERIENCED = "experienced"
    HAS = "has"
    SAME_AS = "same_as"
```

The two enums **stay in `knowledge_graph.py`** (not moved). They keep their `_id` builder usages working unchanged (`build_node_id`, `build_edge_id` continue to accept the enum *or* a plain str — add a unit test for the str path). Downstream module migration off the enums happens task-by-task in #028–#032 as each is touched.

### What does NOT change

- `KnowledgeGraphEntry.kind` validator (Phase 1).
- `build_node_id` / `build_edge_id` signatures (Phase 1) — accept `str` as an additional input type (one-line annotation change).
- `LLM_EXTRACTABLE_NODE_TYPES` / `LLM_EXTRACTABLE_EDGE_TYPES` / `STRUCTURAL_EDGE_TYPES` modules-level constants — rebuilt from the registry (derive at import time) so existing imports keep working.
- The Phase-1 indexes on `KnowledgeGraphEntry` — untouched in this task. `(user_id, type, semantic_type)` lands in #029; the validator-driven indexes land in #030.
- The extraction pipeline, resolver, dedup, MCP tools — none touched in this task (they only see the existing types, which are still present via the shim).

## Acceptance Criteria

- [x] `NodeTypeSpec`, `EdgeTypeSpec`, `SubtypeSpec` dataclasses defined as `frozen=True` in `tree.entities.ontology`. Unit test asserts the frozen-ness (assignment raises).
- [x] `register_node_type(spec)` / `register_edge_type(spec)` / `register_node_subtype(parent, subtype, ...)` callable; idempotent on identical re-registration; raise `ValueError` on conflicting re-registration (different `properties_schema` for the same `name`).
- [x] `register_node_subtype` raises `ValueError` if `parent_type not in NODE_REGISTRY`; raises `ValueError` if the parent's `subtypes is None` (freeform → no extension semantics); succeeds and appends to `subtypes` set otherwise. Unit tests cover all three branches.
- [x] After `tree.entities.ontology` is imported, `set(NODE_REGISTRY.keys()) == {"document", "chunk", "person", "task", "episode", "preference"}` and `set(EDGE_REGISTRY.keys()) == {"part_of", "next", "mentions", "referenced", "related_to", "todo", "experienced", "has", "same_as"}`. Pinned by unit test.
- [x] `get_ontology_schema()` output is **byte-identical** (after a deterministic sort of dict keys) before and after the refactor. Pinned by a golden-file test: snapshot of `get_ontology_schema()` checked into `tests/unit/entities/snapshots/ontology_schema_v1.json`. Diff in CI = test fail.
- [x] `NodeType` and `EdgeType` enums still importable from `tree.entities.knowledge_graph` and still expose every member they exposed before this task. Existing call sites in `tree.memory.*` and `tree.mcp.*` compile without edits.
- [x] `KnowledgeGraphEntry.type` annotation relaxed to `str`; a Pydantic model validator rejects construction of a `kind="node"` entry with `type` not in `NODE_REGISTRY`, and a `kind="edge"` entry with `type` not in `EDGE_REGISTRY`. Unit tests for both rejection paths.
- [x] `build_node_id` / `build_edge_id` accept both the existing `NodeType` / `EdgeType` shim AND a plain `str` (e.g., `"person"`) — both produce identical `_id` strings. Unit test pins this.
- [x] `tree.entities.ontology_tree_extensions` module exists (empty body + module docstring); importing it has no side effect on `NODE_REGISTRY` / `EDGE_REGISTRY` in this task.
- [x] `make memory-format-fix && make memory-lint-fix && make memory-format-check && make memory-lint-check` clean.
- [x] `make pre-commit` green.
- [x] `make memory-unit-tests` green. **Critical regression: every test under `tests/unit/entities/` and `tests/unit/memory/` continues to pass with zero edits.** A diff that requires editing existing unit tests outside `tests/unit/entities/test_ontology.py` and `tests/unit/entities/test_knowledge_graph.py` is a SCOPE VIOLATION in this task — push it back into #028 / #029 / #030.
- [x] `make memory-integration-tests` green (fast loop). The Phase-1 two-user isolation integration test is the standing regression.

## User Stories

### Story: A developer reads the new ontology module to add a custom type
1. Developer opens `apps/memory/src/tree/entities/ontology.py`.
2. Top of the module: `NodeTypeSpec` / `EdgeTypeSpec` / `SubtypeSpec` dataclasses with docstrings.
3. Middle: `register_node_type` / `register_edge_type` / `register_node_subtype` functions with brief usage examples in the docstrings.
4. Bottom of the module under a clear `# --- Built-in registrations ---` banner: six `register_node_type(...)` calls and nine `register_edge_type(...)` calls.
5. Developer copies one of the existing `register_node_type(...)` lines into their downstream module, swaps in their own `name` / `properties_schema`, and the type appears in `NODE_REGISTRY` at the next import.

### Story: The extraction pipeline keeps working unchanged
1. The extraction pipeline at `apps/memory/src/tree/memory/extraction/pipeline.py` runs unchanged after this task.
2. The LLM prompt built from `get_ontology_schema()` is **byte-identical** to the prompt from before the task (the golden-file test guarantees this).
3. Memory pipeline output (the rows written to `knowledge_graph`) is **byte-identical** to before — same `_id`s, same `type` strings, same `properties` payloads. No subtype slot is populated yet (that's #028).

### Story: A registry conflict is caught at import time
1. A developer accidentally types `register_node_type(NodeTypeSpec(name="person", properties_schema=OtherPersonProperties, ...))` after the built-in registration.
2. At import time, `register_node_type` notices the existing `NODE_REGISTRY["person"]` and the schemas differ; raises `ValueError("conflicting re-registration for node type 'person': existing schema PersonProperties, new schema OtherPersonProperties")`.
3. The application fails to start. The developer fixes the conflict.

### Story: An ill-typed KG row is caught at construction time
1. Pipeline code accidentally tries to write `KnowledgeGraphEntry(kind="node", type="ferret", name="alice", ...)`.
2. The Pydantic model validator notices `"ferret" not in NODE_REGISTRY` and raises `ValidationError("type 'ferret' is not a registered node type")`.
3. The bad row never reaches MongoDB.

### Story: A node-subtype registration succeeds (smoke for #028)
1. A test calls `register_node_subtype("person", "scientist", description="Researcher.")`.
2. The test asserts `NODE_REGISTRY["person"].subtypes == {"scientist"}` (the only subtype since the canonical set hasn't been added yet in this task).
3. The test undoes the registration (or uses a registry-snapshot fixture that auto-resets) so it doesn't leak into the rest of the suite.

## Out of scope for this task

- Adding the POLE+O canonical node types (`organization`, `location`, `event`, `object`) — that's #028.
- Registering Tree's subtype extensions (`object/task`, `event/episode`, etc.) — that's #028.
- Collapsing domain edges into `related_to + semantic_type` — that's #029.
- Field-level validator, `ExtractorInfo`, audit collections, `subtype` / `description` / `valid_from` / `valid_until` columns on `KnowledgeGraphEntry` — that's #030.
- `fact` node — that's #031.
- Preference typed slots, `superseded_by`, `DedupConfig` — that's #032.
- Migration script + e2e verification — that's #033.

## Test plan

- **Unit:** `tests/unit/entities/test_ontology.py` — registry idempotency, conflict raises, subtype registration's three branches, retrofit equivalence (every existing type present), `get_ontology_schema()` golden-file diff.
- **Unit:** `tests/unit/entities/test_knowledge_graph.py` — extended with the `type` validator's accept/reject cases.
- **Integration (fast loop):** existing tests stay green untouched. No new integration tests in this task.
- **No slow markers, no mongot.**

---

Refs: `plan.md:96–151` (registry design), `plan.md:438` (NodeType enum-as-shim migration note).

## Log

### [SWE] 2026-05-18 11:00 — Implementation

**Files modified**
- `apps/memory/src/tree/entities/ontology.py` — replaced ad-hoc dicts with the `NODE_REGISTRY` / `EDGE_REGISTRY` registry, the three `register_*` functions, and the `NodeTypeSpec` / `EdgeTypeSpec` / `SubtypeSpec` frozen dataclasses. Re-registered the six legacy node types and nine legacy edge types at module import time so behavior is byte-neutral. Kept `NODE_PROPERTIES`, `EDGE_CONSTRAINTS`, `LLM_EXTRACTABLE_*`, `STRUCTURAL_EDGE_TYPES`, and `EdgeConstraint` as derived views so all existing call sites compile unchanged. `get_ontology_schema()` reads from the registry but emits an identical payload (pinned by golden file).
- `apps/memory/src/tree/entities/knowledge_graph.py` — relaxed `KnowledgeGraphEntry.type` from `NodeType | EdgeType` to `str`. Added a `model_validator(mode="after")` that, on a node row, requires `type in NODE_REGISTRY`, and on an edge row requires `type in EDGE_REGISTRY`. Updated `build_node_id` / `build_edge_id` signatures to accept `NodeType | str` / `EdgeType | str`. Kept the `NodeType` and `EdgeType` `StrEnum` shims with their full set of members (drift-checked by a unit test).
- `apps/memory/src/tree/entities/ontology_tree_extensions.py` — NEW. Empty module with a docstring explaining that #028 will populate it with `register_node_subtype(...)` calls; importing it today is a no-op on the registries.
- `apps/memory/tests/unit/entities/test_ontology.py` — rewritten. New test classes for frozen-ness, registration idempotency / conflict, subtype registration (all three branches), retrofit equivalence, back-compat views, enum-shim agreement, and a golden-file diff for `get_ontology_schema()`.
- `apps/memory/tests/unit/entities/test_knowledge_graph.py` — appended four new test classes covering the relaxed-but-validated `type` field, the validator rejection paths, `build_node_id` / `build_edge_id` accepting plain `str`, and a smoke that importing `ontology_tree_extensions` does not mutate the built-in registries.
- `apps/memory/tests/unit/entities/snapshots/ontology_schema_v1.json` — NEW. Golden-file snapshot of `get_ontology_schema()` (deterministically sorted keys); diff = test fail.

**Tests**
- Unit: 888 passing, 0 failing — full `make memory-unit-tests` output attached below.
- Integration: 130 passing, 1 skipped, 46 deselected (fast loop, `make memory-integration-tests`) — output attached below.

**Acceptance criteria**
- [x] Frozen dataclasses — `TestSpecDataclassesAreFrozen` in `tests/unit/entities/test_ontology.py`.
- [x] `register_*` idempotency + conflict — `TestRegisterNodeType`, `TestRegisterEdgeType`.
- [x] `register_node_subtype` three branches — `TestRegisterNodeSubtype` (`test_raises_when_parent_unknown`, `test_raises_when_parent_is_freeform`, `test_appends_subtype_to_closed_parent`, `test_appends_extra_properties_to_parallel_dict`).
- [x] Exact registry keysets — `TestRetrofitRegistries::test_node_registry_has_exactly_the_six_legacy_types` / `test_edge_registry_has_exactly_the_nine_legacy_types`.
- [x] `get_ontology_schema()` byte-identical via golden file — `TestGetOntologySchema::test_matches_golden_snapshot` (file at `tests/unit/entities/snapshots/ontology_schema_v1.json`).
- [x] `NodeType` / `EdgeType` still importable + full membership — `TestEnumShim` (plus every pre-existing test in `test_knowledge_graph.py` still passing untouched).
- [x] Relaxed `type: str` with validator — `TestTypeFieldValidator` (rejection + accept paths).
- [x] `build_node_id` / `build_edge_id` accept `str` — `TestBuildIdAcceptsStringTypes`.
- [x] `ontology_tree_extensions` module — `TestOntologyTreeExtensionsModuleExists::test_module_imports_with_no_side_effects`.
- [x] Format + lint clean — output below.
- [x] `make pre-commit` green — output below.
- [x] `make memory-unit-tests` green; no edits to existing tests outside `test_ontology.py` / `test_knowledge_graph.py` — confirmed by `git status` (existing tests under `tests/unit/memory/`, `tests/unit/mcp/`, `tests/unit/models/`, etc. are untouched and all pass).
- [x] `make memory-integration-tests` green (fast loop) — output below.

**Evidence**

```
$ make memory-format-fix && make memory-lint-fix
... ruff format src/ tests/ scripts/ deploy/
3 files reformatted, 216 files left unchanged
... ruff check --fix src/ tests/ scripts/ deploy/
All checks passed!

$ make memory-format-check && make memory-lint-check
... ruff format --check src/ tests/ scripts/ deploy/
219 files already formatted
... ruff check src/ tests/ scripts/ deploy/
All checks passed!

$ make pre-commit
Validate pyproject.toml..............................(no files to check)Skipped
prettier.................................................................Passed
ruff check...............................................................Passed
ruff format..............................................................Passed
biome check (harness)....................................................Passed
KGQuery discipline (memory)..............................................Passed

$ make memory-unit-tests
... (888 tests collected) ...
tests/unit/entities/test_ontology.py .................................   [ 51%]
tests/unit/entities/test_users.py ...............                        [ 53%]
... (full suite) ...
============================= 888 passed in 41.59s =============================

$ make memory-integration-tests
... (177 collected / 46 deselected / 131 selected) ...
tests/integration/memory/test_extraction_pipeline.py .........           [ 83%]
tests/integration/memory/test_indexing_pipeline.py ....                  [ 86%]
tests/integration/memory/test_review.py ..................               [100%]
========== 130 passed, 1 skipped, 46 deselected in 177.93s (0:02:57) ===========
```

End-to-end runtime smoke (registry is the source of truth that drives the LLM-extraction prompt and the nl-to-Mongo prompt):

```
$ uv run python -c "from tree.entities.ontology import ..."
imports OK
NODE_REGISTRY: ['chunk', 'document', 'episode', 'person', 'preference', 'task']
EDGE_REGISTRY: ['experienced', 'has', 'mentions', 'next', 'part_of', 'referenced', 'related_to', 'same_as', 'todo']
LLM extractable nodes: ['episode', 'person', 'preference', 'task']
LLM extractable edges: ['experienced', 'has', 'related_to', 'todo']
structural edges: ['mentions', 'next', 'part_of', 'referenced', 'same_as']
NodeType: ['document', 'chunk', 'person', 'task', 'episode', 'preference']
EdgeType: ['part_of', 'next', 'mentions', 'referenced', 'related_to', 'todo', 'experienced', 'has', 'same_as']
build_node_id str+enum equivalence: OK
rejected ferret: ValidationError

$ uv run python -c "from tree.memory.query.nl_query import build_nl_query_system_prompt; ..."
prompt length: 6624
nl_query prompt covers all 15 types: OK
extraction schema node_types: ['episode', 'person', 'preference', 'task']
extraction schema edge_types: ['experienced', 'has', 'related_to', 'todo']
```

**Notes**
- **Behavior-neutrality preserved as a hard constraint.** Every public surface that downstream modules import (`NODE_PROPERTIES`, `EDGE_CONSTRAINTS`, `LLM_EXTRACTABLE_NODE_TYPES`, `LLM_EXTRACTABLE_EDGE_TYPES`, `STRUCTURAL_EDGE_TYPES`, `EdgeConstraint`, plus the `NodeType` / `EdgeType` enums) is preserved verbatim. They are now derived from the registry rather than hand-rolled. The golden-file test pins `get_ontology_schema()` to its pre-refactor JSON.
- **Subtype `description` is currently swallowed.** `register_node_subtype` accepts a `description` parameter but does not yet attach it to a per-subtype description map — the spec says the subtype-aware prompt / validator wiring lands in #028 / #030. The `SubtypeSpec` dataclass exists for forward-compat and is consumed via the `description` arg today; full plumbing arrives downstream. Marked with a comment in `ontology.py`.
- **`subtypes` is stored as `frozenset[str]`** (not `set[str]` as the groomed spec wrote informally). Required for the dataclass to be `frozen=True`-compatible. The semantics are identical — the four subtype-related ACs reference `set(...)` membership and `frozenset` satisfies that contract; the unit test asserts `spec.subtypes == frozenset({"scientist"})` explicitly.
- **NodeType / EdgeType enums remain hand-declared in `knowledge_graph.py`.** They are not dynamically rebuilt from the registry: `ontology.py` imports them from `knowledge_graph.py`, so a registry-driven definition would require an import inversion. A `TestEnumShim` unit test guards against drift (enum members must equal registry keys). Deletion of these enums (per #028–#032 migration) is unaffected.
- **`type: NodeType | EdgeType` → `type: str` migration is wire-neutral.** `StrEnum` subclasses inherit from `str`, so existing call sites passing `NodeType.PERSON` continue to work — Pydantic accepts the enum and stores `"person"`. The new model validator additionally rejects unregistered strings with a `ValidationError`.
- **Local mongot infra is up but the fast integration loop excludes `requires_mongot` tests anyway.** No slow / mongot tests were touched in this task.
- **DO NOT COMMIT YET** — handing off to Tester per role rules.

### [Tester] 2026-05-18 14:20 — QA

**Test summary**
- Format / lint / pre-commit: **PASS** (`make memory-format-check && make memory-lint-check && make pre-commit` all green)
- Unit tests: **888 passed, 0 failed, 0 warnings** (`make memory-unit-tests`, 42.00s)
- Integration tests (full — slow + mongot included): **176 passed, 1 skipped, 0 warnings** (`make memory-integration-tests-all`, 6:46)

**E2E adversarial pass**

- **Happy path — KG round-trip through live MongoDB:**
  - `KnowledgeGraphEntry.save()` + `.get()` for a `person` node and a `related_to` edge (db `qa_027_e2e_rt`).
  - Result: node `type='person'` (str), edge `type='related_to'` round-trip cleanly; `properties` preserved; `_id` formed via `build_node_id` / `build_edge_id` works with both `str` and `NodeType.PERSON` enum. **PASS.**

- **Behavior-neutrality — full LLM prompt diff vs `main`:**
  - Compared `json.dumps(get_ontology_schema(), sort_keys=True)` and the full nl-query system prompt between feature-branch and main worktrees: **byte-identical.**
  - `NODE_PROPERTIES` / `EDGE_CONSTRAINTS` views (incl. all four `same_as` self-pair `EdgeConstraint`s with their pair-specific descriptions): **byte-identical** between main and feat.
  - The checked-in golden snapshot `tests/unit/entities/snapshots/ontology_schema_v1.json` equals the schema generated from `main`. **PASS.**

- **Break path 1 (boundary: empty string `type`):** `KnowledgeGraphEntry.model_validate(... type="" ...)` → `ValidationError: type '' is not a registered node type`. **PASS.**
- **Break path 2 (boundary: 10 000-char type):** `type="a" * 10000` → `ValidationError`. **PASS.**
- **Break path 3 (malformed: unregistered node type):** `type="not_registered"` → `ValidationError: type 'not_registered' is not a registered node type`. **PASS.**
- **Break path 4 (malformed: unregistered edge type):** `kind="edge", type="owns"` → `ValidationError: type 'owns' is not a registered edge type`. **PASS.**
- **Break path 5 (state edge: registry conflict):** `register_node_type` with same name but different `properties_schema` → `ValueError("conflicting re-registration for node type '_tmp_ix': ...")`. Identical re-registration (same spec, equal-but-distinct instance) → no-op. **PASS.**
- **Break path 6 (state edge: subtype invariants):** `register_node_subtype("person", "individual")` on a freeform parent → `ValueError("freeform")`; `register_node_subtype("nonexistent_parent", "x")` → `ValueError("not registered")`. Adding a subtype to a closed-vocabulary parent via `register_node_subtype("_tmp_obj_parent", "task")` succeeds; re-adding "task" is a set-union no-op; adding a second "project" yields `frozenset({'task', 'project'})`. **PASS.**
- **Break path 7 (shim compat: legacy enum still works):** `KnowledgeGraphEntry(..., type=NodeType.PERSON)` → `entry.type == 'person'` (str). `build_node_id(uid, NodeType.PERSON, 'alice') == build_node_id(uid, 'person', 'alice')` and same for `build_edge_id` / `EdgeType.TODO`. **PASS.**
- **Break path 8 (frozen specs):** assignment to `NodeTypeSpec.name`, `EdgeTypeSpec.name`, `SubtypeSpec.name` all raise `FrozenInstanceError`. **PASS.**
- **Break path 9 (side-effect leak: ontology_tree_extensions import):** importing `tree.entities.ontology_tree_extensions` does not mutate `NODE_REGISTRY` or `EDGE_REGISTRY` (7 nodes / 9 edges before == after, where 7 includes the test-injected `_tmp_obj_parent` from break 6). **PASS.**
- **Break path 10 (retrofit equivalence):** `set(NODE_REGISTRY) ⊇ {document, chunk, person, task, episode, preference}` and `set(EDGE_REGISTRY) == {part_of, next, mentions, referenced, related_to, todo, experienced, has, same_as}` after `tree.entities.ontology` import. **PASS.**
- **Break path 11 (integration regression: two-user isolation):** `test_two_user_isolation` (26 tests) + `test_two_user_review_isolation` (3 tests) all pass against live MongoDB + mongot. **PASS.**
- **Break path 12 (end-to-end pipeline via integration suite):** `tests/integration/memory/test_extraction_pipeline.py` (9 tests) drives the real `memory_extraction` Prefect flow with patched LLM, and `test_indexing_pipeline.py` (6 tests) drives `memory_indexing` end-to-end against live Mongo + mongot. All pass. **PASS.**

**Pipeline e2e via real Prefect deployment — not exercised.** Reason: the live `tree-prefect-worker` runs out of a Docker image built from `main` (pre-#027 code), so triggering deployments would run the **old** code path rather than the SWE's diff. Per the spec, "If the sibling-worktree worker can't be relocated cleanly, document why and exercise via direct-function-call as #026 did — this is acceptable for a behavior-neutral refactor". The `memory_extraction` / `memory_indexing` flows are exercised end-to-end (live Mongo, real flow code, patched LLM only) via the integration suite above, which covers the same code path the Prefect worker would execute.

**Acceptance criteria**
- [x] PASS — Frozen dataclasses (`NodeTypeSpec` / `EdgeTypeSpec` / `SubtypeSpec`). Evidence: `tests/unit/entities/test_ontology.py::TestSpecDataclassesAreFrozen` (3 tests) + adversarial break 8.
- [x] PASS — `register_node_type` / `register_edge_type` idempotency + conflict. Evidence: `TestRegisterNodeType`, `TestRegisterEdgeType` + adversarial break 5.
- [x] PASS — `register_node_subtype` all three branches. Evidence: `TestRegisterNodeSubtype` (4 tests) + adversarial break 6.
- [x] PASS — Registry keysets exactly match the six legacy node types + nine legacy edge types. Evidence: `TestRetrofitRegistries::test_node_registry_has_exactly_the_six_legacy_types` / `test_edge_registry_has_exactly_the_nine_legacy_types` + adversarial break 10.
- [x] PASS — `get_ontology_schema()` byte-identical to pre-refactor. Evidence: `TestGetOntologySchema::test_matches_golden_snapshot`; out-of-band `diff` against `main` worktree's `get_ontology_schema()` (sort_keys=True): **identical** (`IDENTICAL` printed); golden snapshot equals main's schema.
- [x] PASS — `NodeType` / `EdgeType` enums importable from `tree.entities.knowledge_graph` with all 6+9 members. Evidence: `TestEnumShim` (4 tests); all 130+ downstream call sites compile (every unit + integration test passes untouched outside test_ontology / test_knowledge_graph).
- [x] PASS — `KnowledgeGraphEntry.type` relaxed to `str` with registry validator. Evidence: `TestTypeFieldIsRelaxedString` + `TestTypeFieldValidator` (6 tests) + adversarial breaks 1, 3, 4.
- [x] PASS — `build_node_id` / `build_edge_id` accept `str` and enum. Evidence: `TestBuildIdAcceptsStringTypes` + adversarial break 7.
- [x] PASS — `ontology_tree_extensions` empty module exists with no import side effects. Evidence: `TestOntologyTreeExtensionsModuleExists` + adversarial break 9.
- [x] PASS — Format / lint clean. Evidence: command output above.
- [x] PASS — `make pre-commit` green. Evidence: command output above.
- [x] PASS — `make memory-unit-tests` green; no edits outside `test_ontology.py` / `test_knowledge_graph.py`. Evidence: `git diff --stat HEAD` shows only those two test files modified; 888 passed.
- [x] PASS — `make memory-integration-tests` green. Evidence: full `memory-integration-tests-all` (slow + mongot) shows 176 passed / 1 skipped / 0 warnings.

**Evidence**

```
$ make memory-format-check && make memory-lint-check
219 files already formatted
All checks passed!

$ make pre-commit
Validate pyproject.toml..............................(no files to check)Skipped
prettier.................................................................Passed
ruff check...............................................................Passed
ruff format..............................................................Passed
biome check (harness)....................................................Passed
KGQuery discipline (memory)..............................................Passed

$ make memory-unit-tests
... (888 collected) ...
============================= 888 passed in 42.00s =============================

$ make memory-integration-tests-all
... (177 collected) ...
tests/integration/memory/test_extraction_pipeline.py .........           [ 70%]
tests/integration/memory/test_indexing_pipeline.py ......                [ 73%]
tests/integration/memory/test_review.py ..................               [ 83%]
tests/integration/test_two_user_isolation.py ..........................  [ 98%]
tests/integration/test_two_user_review_isolation.py ...                  [100%]
================== 176 passed, 1 skipped in 406.37s (0:06:46) ==================
```

```
$ diff <(uv run --directory <main_worktree> ... get_ontology_schema sort_keys=True) \
       <(uv run --directory <feat_worktree> ... get_ontology_schema sort_keys=True)
IDENTICAL

$ diff /tmp/views_main.json /tmp/views_feat.json
IDENTICAL  # NODE_PROPERTIES + EDGE_CONSTRAINTS views byte-identical

$ diff /tmp/prompts_main.txt /tmp/prompts_feat.txt
PROMPTS IDENTICAL  # get_ontology_schema (sorted) + nl_query system prompt
```

**Other issues found (not blockers — flagged for #028+ or future polish)**
- `get_ontology_schema()` iterates `LLM_EXTRACTABLE_NODE_TYPES` / `LLM_EXTRACTABLE_EDGE_TYPES`, which are `set[NodeType]` / `set[EdgeType]`. The resulting dict key order is **non-deterministic across Python processes** (verified: three back-to-back runs each emit a different order). The extraction prompt is built via `json.dumps(get_ontology_schema(), indent=2)` in `tree/memory/extraction/core.py:135` **without `sort_keys=True`**, so the prompt fed to the LLM varies per process boot. This is a **pre-existing issue on `main`** (same set-driven iteration there) — #027 is behavior-neutral with respect to it — but worth fixing in a follow-up: it would defeat any LLM prompt-caching layer and makes evals less reproducible. Quick fix: sort node/edge keys in `get_ontology_schema()` (e.g. `for node_type in sorted(LLM_EXTRACTABLE_NODE_TYPES, key=lambda x: x.value):`), or change the constants to ordered structures. Flag for #028.
- The model-validator comment in `knowledge_graph.py:187` reads "Unknown ``kind`` values fall through; the ``kind`` validator (Phase 1) is the gate that rejects those." Verified: there is **no kind validator** on the entity (neither in main nor in this diff). `KnowledgeGraphEntry.model_validate(... kind="nodeXX" ...)` is accepted. This is a **pre-existing latent gap on main** that #027 inherits — not a regression — but the comment is misleading and should be tightened (either add a `kind` validator or remove the claim). Flag for a future task.
- `register_node_subtype` accepts a `description` arg and discards it (line 251: `_ = description`). The SWE flagged this in their Notes — full wiring lands in #028 / #030. Acceptable for #027 scope.

**VERDICT: PASS**

Refactor is behavior-neutral as claimed. Every acceptance criterion verified with evidence; full local suite (slow + mongot) green with zero warnings; e2e round-trip through live MongoDB succeeds; LLM-prompt input (`get_ontology_schema()` schema, nl-query prompt, `NODE_PROPERTIES`/`EDGE_CONSTRAINTS` views) is byte-identical to `main` after deterministic key sort. The two pre-existing nits noted above were inherited from `main`, not introduced by this diff, and do not block #027.

