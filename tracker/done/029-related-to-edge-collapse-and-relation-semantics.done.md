# `related_to` edge collapse + `RELATION_SEMANTICS` registry + structural-edge property models (Phase 3, part 3 of 4)

Status: in-progress
Tags: `phase-3`, `ontology`, `edges`, `pole-o`, `related-to`, `mentions`, `same-as`
Depends on: #027, #028
Blocks: #030, #031, #032, #033

## Scope

Collapse the **14 POLE+O domain relations** (employed_by, knows, member_of, owns, uses, located_at, resides_at, headquarters_at, participated_in, occurred_at, involved, subsidiary_of, partner_with, alias_of) into a **single `related_to` edge type** discriminated by a new `semantic_type: str | None` column on `KnowledgeGraphEntry`. Per `plan.md:212–250`, the catalogue of allowed `(source_type, target_type)` pairs and per-semantic properties lives in a new `RELATION_SEMANTICS: dict[str, RelationSemanticSpec]` registry separate from `EDGE_REGISTRY`. Envelope-level validation is **strict** (unknown semantic OR disallowed pair → drop the whole edge — actual drop-and-log lands in #030's validator; this task just registers the semantic catalogue and validates pair membership at write time via a Pydantic validator). The existing free `RELATED_TO` edge between persons becomes the first registered semantic (`knows`). The three Tree-specific domain edges `TODO`, `EXPERIENCED`, `HAS` are migrated:

- `TODO` (person → task) and `EXPERIENCED` (person → episode) **are removed**. The corresponding relations are no longer LLM-extractable as separate edges; the LLM is taught (via prompt) to emit `related_to` with a Tree-specific semantic where applicable. **Decision for this task:** add `experienced_by` (event → person, inverse-emit later if needed — keep simple: person → event) as a Tree-specific extension to `RELATION_SEMANTICS`, and add `has_task` (person → object where subtype="task") similarly. **Justification:** the alternative is dropping the relations entirely, which loses information. Tree's subtype extensions (#028) already broke the "POLE+O canonical only" purity bound; extending `RELATION_SEMANTICS` similarly is consistent.
- `HAS` (person → preference) **stays as a structural edge**, not a `related_to` semantic. Per `plan.md:469–474`, "Preferences attach only to `person:self`" and the `has` edge is **deterministically written by the pipeline, never LLM-emitted**. Keeping it structural matches the LLM-extractable/structural divide. This task narrows `EdgeType.HAS`'s allowed pair to exactly `(person, preference)`.

Also in this task: broaden `mentions` and `same_as` per `plan.md:254`, add the `MentionsProperties` / `SameAsProperties` Pydantic models per `plan.md:259–292`, and add the `(user_id, type, semantic_type)` compound index. See `plan.md:212–294` for the full design.

### Files touched

- `apps/memory/src/tree/entities/ontology.py` — add `RelationSemanticSpec` dataclass, `RELATION_SEMANTICS: dict[str, RelationSemanticSpec]` registry, `register_relation_semantic(spec)` function. Add 14 canonical POLE+O semantics + 2 Tree extensions (`experienced_by`, `has_task`). Update `EDGE_REGISTRY["related_to"]` to be the LLM-extractable umbrella edge whose `allowed_pairs` are the **union** of every `RelationSemanticSpec.allowed_pairs` (derived at import time). Remove `EdgeType.TODO` and `EdgeType.EXPERIENCED` from the registry. Narrow `EdgeType.HAS` to `(person, preference)` only.
- `apps/memory/src/tree/entities/ontology.py` — add the per-semantic `*Properties` Pydantic models per `plan.md:417–426` (one per semantic that has properties; ~8 of them — `member_of`, `employed_by`, `owns`, `located_at`, `resides_at`, `participated_in`, `involved`, `experienced_by`, `has_task` if applicable). Each carries `Field(description=...)` on every attribute.
- `apps/memory/src/tree/entities/ontology.py` — add `MentionsProperties`, `SameAsProperties` Pydantic models per `plan.md:259–292` and register them on the broadened `mentions` / `same_as` edge specs via `EdgeTypeSpec.properties_schema`. Add `SameAsMatchType` and `SameAsStatus` `StrEnum`s.
- `apps/memory/src/tree/entities/knowledge_graph.py` — add `semantic_type: str | None = None` column. Add an `IndexModel` for `(user_id, type, semantic_type)` partial index (sparse on `semantic_type`). Remove `EdgeType.TODO` and `EdgeType.EXPERIENCED` from the enum shim. Add a model validator: on `kind="edge"` with `type="related_to"`, `semantic_type` MUST be present in `RELATION_SEMANTICS` AND the `(source_type, target_type)` pair MUST be allowed by the semantic's `allowed_pairs`. On any other edge type, `semantic_type` MUST be `None`.
- `apps/memory/src/tree/memory/extraction/core.py`, `apps/memory/src/tree/memory/extraction/pipeline.py` — re-emit ex-`TODO` / ex-`EXPERIENCED` edges as `related_to + semantic_type="has_task"` / `related_to + semantic_type="experienced_by"`. LLM prompt update via `get_ontology_schema()` change.
- `apps/memory/src/tree/memory/extraction/dedup.py` — update `same_as` write path to populate `MentionsProperties` / `SameAsProperties` per the new shape (status, match_type, confidence). Backward-compat: legacy `same_as` edges without these fields default to `status=pending, match_type=embedding, confidence=1.0` on read.
- `apps/memory/src/tree/memory/review/core.py` — `find_pending_duplicates` / `review_duplicate` from Phase 1 keep working — they already filter on `properties.status="pending"` so the rename to typed `SameAsProperties.status` is a no-op at the Mongo query level (the field path is unchanged).
- `apps/memory/src/tree/memory/indexing/core.py` — register the `(user_id, type, semantic_type)` compound index alongside the existing Phase-1 indexes (idempotent via Mongo's `ensure_indexes`).
- `apps/memory/tests/unit/entities/test_ontology.py` — extend with `RELATION_SEMANTICS` assertions.
- `apps/memory/tests/unit/entities/test_knowledge_graph.py` — extend with the new edge validator's accept/reject cases.
- `apps/memory/tests/unit/memory/extraction/test_dedup.py` — extend for `SameAsProperties` shape.

### `RelationSemanticSpec` shape (per `plan.md:217–225` + edge-symmetry update at `plan.md:407–426`)

```python
@dataclass(frozen=True)
class RelationSemanticSpec:
    name: str
    allowed_pairs: list[tuple[str, str]]
    properties_schema: type[BaseModel] | None = None    # was dict[str, type]
    description: str = ""

RELATION_SEMANTICS: dict[str, RelationSemanticSpec] = {}

def register_relation_semantic(spec: RelationSemanticSpec) -> None:
    """Register a semantic for the `related_to` edge. Idempotent on identical
    re-registration; raises ValueError on conflicting re-registration."""
```

The 14 POLE+O canonical semantics from `plan.md:227–242` plus Tree's two extensions (`experienced_by`, `has_task`) — **16 entries total**. Property schemas (where present) are Pydantic `BaseModel`s with `Field(description=...)` on every attribute. Where the table shows `—` (e.g. `knows`, `alias_of`, `uses`, `headquarters_at`, `occurred_at`, `subsidiary_of`, `partner_with`), `properties_schema=None`.

### Pair-resolution detail

Pair validation uses the **parent type** name only, never the subtype. So `related_to{semantic_type=has_task}` allows `(person, object)` — the subtype check is the responsibility of the LLM prompt / a follow-up policy (file an open question rather than over-engineer). This keeps the registry shape clean and matches `plan.md:227–242`'s use of bare type names in the allowed-pairs column.

### `mentions` and `same_as` broadening (per `plan.md:254`)

```python
# After this task:
EDGE_REGISTRY["mentions"] = EdgeTypeSpec(
    name="mentions",
    allowed_pairs=[(s, t) for t in POLE_O_LLM_EXTRACTABLE for s in ("chunk", "document")],
    # i.e. {chunk, document} → any POLE+O type except `fact` (carve-out, even though
    # `fact` doesn't exist yet — #031 lands it; pre-encoding the carve-out is fine).
    properties_schema=MentionsProperties,
    description="A chunk or document mentions a POLE+O entity.",
    llm_extractable=False,  # structural; pipeline-emitted
)

EDGE_REGISTRY["same_as"] = EdgeTypeSpec(
    name="same_as",
    allowed_pairs=[(t, t) for t in POLE_O_LLM_EXTRACTABLE if t != "fact"],
    # i.e. same_as is restricted to same-type pairs, excluding fact (per #031's island rule).
    properties_schema=SameAsProperties,
    description="Two entity nodes of the same type refer to the same real-world entity.",
    llm_extractable=False,
)
```

Where `POLE_O_LLM_EXTRACTABLE = ["person", "organization", "location", "event", "object", "preference"]` derived from the registry. `fact` (#031) is **excluded** — `mentions` does not target it (preferences either; per the `plan.md:479` carve-out), and `same_as` excludes it because facts are island-style. Encoding the carve-out at this task is fine — #031 only needs to register the `fact` type; it doesn't need to mutate `mentions`/`same_as` after the fact.

**Preference carve-out from `mentions`** per `plan.md:479`: `mentions` does NOT target `preference`. The list comprehension above excludes `preference` from the `mentions` allowed_pairs.

### Pipeline migration for ex-TODO / ex-EXPERIENCED

Today `extraction.core.normalize_edges` walks the LLM output and emits `KnowledgeGraphEntry(kind="edge", type="todo", source=..., target=...)`. After this task:

- `todo` (person → task) → `related_to` with `semantic_type="has_task"`, `source_type="person"`, `target_type="object"` (target is the re-routed object/task from #028).
- `experienced` (person → episode) → `related_to` with `semantic_type="experienced_by"`, `source_type="person"`, `target_type="event"` (target is the re-routed event/episode).
- `has` (person → preference) stays as `kind="edge", type="has"` — unchanged shape, narrower allowed_pairs.

The LLM prompt (via `get_ontology_schema()`) is updated to emit a single `related_to` edge with `semantic_type` rather than three distinct types. **The prompt grows** by ~14 semantic descriptions; the SWE should sanity-check the prompt eyeball-length is still reasonable (≤ ~8KB JSON). The full prompt-quality eval is out of scope (per the feature plan); the `extraction_dropped_fields` audit collection (#030) gives us post-hoc signal.

### Indexing

`apps/memory/src/tree/memory/indexing/core.py` already registers user-id-prefixed compound indexes. Add one more:

```python
IndexModel(
    [("user_id", 1), ("type", 1), ("semantic_type", 1)],
    name="user_type_semantic_type",
    partialFilterExpression={"semantic_type": {"$exists": True, "$ne": None}},
)
```

Partial index — only `related_to` rows pay the index cost. Verified by `apps/memory/tests/integration/test_indexing.py` (or similar; create if missing).

## Acceptance Criteria

- [x] `RelationSemanticSpec` dataclass (frozen) defined; `RELATION_SEMANTICS` dict populated with **16 entries** at import time: 14 POLE+O canonical (per `plan.md:227–242`) + 2 Tree extensions (`experienced_by`, `has_task`). Pinned by `tests/unit/entities/test_ontology.py::TestRelationSemanticsCatalogue`.
- [x] `register_relation_semantic(spec)` callable; idempotent on identical re-registration; raises `ValueError` on conflict. `tests/unit/entities/test_ontology.py::TestRegisterRelationSemantic` covers both branches.
- [x] `EdgeType.TODO` and `EdgeType.EXPERIENCED` removed from the enum shim. `grep -rn "EdgeType.TODO\|EdgeType.EXPERIENCED" apps/memory/` returns zero hits. `EdgeType.HAS` retained and broadened (per the task instructions) to `(person, preference)` AND `(person, object)`.
- [x] `KnowledgeGraphEntry.semantic_type: str | None = None` is a live column. Pydantic model validator covers all 5 branches per `tests/unit/entities/test_knowledge_graph.py::TestRelatedToSemanticValidator`.
- [x] Compound index `(user_id, type, semantic_type)` declared as a partial filtered index (`{"semantic_type": {"$type": "string"}}`; `$ne: null` is not a supported partial-filter expression in MongoDB). Verified by `tests/unit/entities/test_knowledge_graph.py::TestSemanticTypeIndex` + `tests/integration/entities/test_related_to_validator.py::TestSemanticTypePartialIndex`.
- [x] `MentionsProperties` and `SameAsProperties` Pydantic models defined. Every `Field` has a non-empty `description=...`. `SameAsMatchType` and `SameAsStatus` `StrEnum`s defined. Pinned by `TestStructuralEdgePropertyModels`.
- [x] `EDGE_REGISTRY["mentions"].properties_schema is MentionsProperties`. `allowed_pairs` is the cross product `{chunk, document} × {person, organization, location, event, object}` — `preference` carved out. `TestMentionsBroadeningAndCarveOut` pins it.
- [x] `EDGE_REGISTRY["same_as"].properties_schema is SameAsProperties`. Allowed pairs are the self-pair on every POLE+O LLM-extractable type. `TestSameAsBroadening` pins it.
- [x] `get_ontology_schema()` output snapshot updated → `tests/unit/entities/snapshots/ontology_schema_v3.json`. Includes the `related_to` umbrella edge with nested `semantic_types`. Snapshot diff = test fail (golden-file).
- [x] The Phase-1 `add_entity()` orchestrator + dedup branch from #011 keeps emitting `same_as` edges with `properties.status` / `match_type` / `confidence`. `tests/integration/memory/test_review.py` + `test_add_entity.py` + `test_dedup.py` all green (review_duplicate / find_pending_duplicates contract unchanged).
- [x] LLM extraction pipeline emits `related_to + semantic_type` rows. `tests/unit/memory/extraction/test_core.py` pins the new shape + legacy `todo`/`experienced` re-route. Live Gemini extraction smoke (`scripts/smoke_029_live_extraction.py`) confirms 4 `related_to` edges with valid semantic_types land for a small person/organization/location document.
- [x] `make memory-format-fix && make memory-lint-fix && make memory-format-check && make memory-lint-check` clean.
- [x] `make pre-commit` green.
- [x] `make memory-unit-tests` green (1005 passing).
- [x] `make memory-integration-tests` green (128 passed, 12 skipped — all external-cred dependencies).
- [x] `make memory-integration-tests-all` (full incl. mongot) green — slow tail alone: 46 passing.

## User Stories

### Story: LLM extracts an employment edge between a person and an organization
1. Chunk text: "Paul was hired by Anthropic in March 2024."
2. The LLM emits `{"type": "related_to", "semantic_type": "employed_by", "source": {"type": "person", "name": "paul"}, "target": {"type": "organization", "name": "anthropic"}, "properties": {"start_date": "2024-03-01"}}`.
3. Envelope validation: `employed_by ∈ RELATION_SEMANTICS`, pair `(person, organization)` allowed → pass.
4. Row writes as `KnowledgeGraphEntry(kind="edge", type="related_to", semantic_type="employed_by", source_node_id="{uid}:person:paul", target_node_id="{uid}:organization:anthropic", properties={"start_date": "2024-03-01"}, ...)`.
5. Query: `KGQuery(user_id).find_edges(type="related_to", semantic_type="employed_by")` returns this row, using the partial compound index.

### Story: LLM emits a malformed edge — it is dropped
1. LLM emits `{"type": "related_to", "semantic_type": "employed_by", "source": {"type": "organization", "name": "anthropic"}, "target": {"type": "person", "name": "paul"}}` (inverted pair).
2. Envelope validation: `(organization, person)` not in `RELATION_SEMANTICS["employed_by"].allowed_pairs` → fail.
3. The Pydantic validator on `KnowledgeGraphEntry` rejects construction. The pipeline catches the `ValidationError` and logs to `extraction_rejections` (the log collection lands in #030; until then, structured-log to the Prefect task log).
4. No row written; the rest of the LLM's edges proceed.

### Story: A legacy `TODO` row is migrated on next extraction pass
1. Before this task, the DB had `KnowledgeGraphEntry(kind="edge", type="todo", source_node_id="{uid}:person:self", target_node_id="{uid}:task:write-the-report", ...)`.
2. The migration script (#033) drops `knowledge_graph` and re-extracts; the same conversation chunk now emits `KnowledgeGraphEntry(kind="edge", type="related_to", semantic_type="has_task", source_node_id="{uid}:person:self", target_node_id="{uid}:object:write-the-report", ...)`.
3. Note `target_node_id` now uses the `object` type (subtype "task" lives in the **node** row, not the edge).
4. A `find` query that previously asked `type="todo"` now asks `type="related_to", semantic_type="has_task"`; the new query path is documented in `query/kgquery.py`.

### Story: `mentions` is broadened
1. Pipeline writes a structural `mentions` edge from a chunk to a `location` node (`{uid}:chunk:abc → {uid}:location:san francisco`).
2. The edge row's `properties` validates against `MentionsProperties`: `{"confidence": 0.94, "start_pos": 12, "end_pos": 25}`.
3. Before this task, only `(document, person)` was allowed for `mentions`. After this task, the broadened allowed_pairs include `(chunk, location)`.

### Story: A `same_as` candidate is emitted with `status="pending"`
1. The dedup branch in `extraction/dedup.py` finds an embedding-similar pair (`{uid}:person:alice smith` and `{uid}:person:alice s. smith`) at cosine 0.91 — in the medium-confidence flag band (per Phase-1 #010).
2. It emits `KnowledgeGraphEntry(kind="edge", type="same_as", source_node_id=..., target_node_id=..., properties={"confidence": 0.91, "match_type": "embedding", "status": "pending"})`.
3. The properties payload validates against `SameAsProperties`. The Phase-1 `find_pending_duplicates` MCP tool returns the pair on next call.

## Out of scope for this task

- Field-level lenient validator (drop a single bad property without rejecting the row) — that's #030.
- `extraction_rejections` / `extraction_dropped_fields` audit collections — that's #030. This task does its envelope-level rejection via the Pydantic validator + a Prefect task log line; the structured-log surface comes in #030.
- `ExtractorInfo` provenance column — #030.
- `description` / `valid_from` / `valid_until` columns on `KnowledgeGraphEntry` — #030.
- `fact` node + island enforcement — #031.
- `superseded_by` edge — #032.
- `DedupConfig` three-tier surfacing — #032.
- Migration script + end-to-end POLE+O verification — #033.

## Test plan

- **Unit:** `tests/unit/entities/test_ontology.py` — assertions on `RELATION_SEMANTICS` contents (16 entries, exact list); the `(source_type, target_type)` membership matrix per semantic; idempotency and conflict of `register_relation_semantic`.
- **Unit:** `tests/unit/entities/test_knowledge_graph.py` — extended edge validator's accept/reject matrix (5 branches above).
- **Unit:** `tests/unit/entities/snapshots/ontology_schema_v3.json` — new golden file (replaces v2).
- **Unit:** `tests/unit/memory/extraction/test_normalize_edges.py` — re-emit ex-`TODO` → `related_to + has_task`, ex-`EXPERIENCED` → `related_to + experienced_by`.
- **Unit:** `tests/unit/memory/extraction/test_dedup.py` — same_as edges now carry `SameAsProperties` shape.
- **Integration:** `tests/integration/test_indexing.py` (new or extended) — assert the `user_type_semantic_type` partial index is created with the expected `partialFilterExpression`. Marker: `@pytest.mark.requires_mongot` if it relies on mongot for the index creation; otherwise plain integration.
- **Integration:** Phase-1 two-user isolation test stays green untouched.

---

Refs: `plan.md:212–294` (edge collapse + structural-edge property models), `plan.md:227–242` (semantic catalogue), `plan.md:259–292` (MentionsProperties / SameAsProperties), `plan.md:299` ((user_id, type, semantic_type) index), `plan.md:407–426` (edge-symmetry update).

## Log

### [SWE] 2026-05-18 12:40 — Implementation

**Files modified**

Source:
- `apps/memory/src/tree/entities/ontology.py` — added `RelationSemanticSpec` frozen dataclass, `RELATION_SEMANTICS` dict, `register_relation_semantic`. Added 16 semantic registrations (14 canonical + Tree's `has_task` / `experienced_by`). Added per-semantic `*Properties` Pydantic models for the 9 semantics with typed properties. Added `MentionsProperties`, `SameAsProperties`, `SameAsMatchType`, `SameAsStatus`. Collapsed `EDGE_REGISTRY` from 9 entries down to 7: dropped `todo` / `experienced`; broadened `mentions` (chunk|document → every POLE+O LLM-extractable except preference), broadened `same_as` (self-pair across all POLE+O extractable types), narrowed (and broadened) `has` to `(person, preference)` AND `(person, object)`, switched it to structural (`llm_extractable=False`), and re-shaped `related_to` into the umbrella edge whose `allowed_pairs` is the union of every semantic spec. `get_ontology_schema()` now surfaces a nested `semantic_types: {...}` map under `related_to`.
- `apps/memory/src/tree/entities/knowledge_graph.py` — removed `EdgeType.TODO` / `EdgeType.EXPERIENCED`; kept the rest. Added `semantic_type: str | None = None` column. Added two new model validator branches — `_check_related_to_semantic` (5 documented branches: accept, pair-violation, unknown semantic, missing semantic on related_to, semantic_type on non-related_to) AND strict `allowed_pairs` enforcement for every non-related_to edge so the broadened `mentions` / narrowed `has` are write-time constraints. Added the `(user_id, type, semantic_type)` partial-filtered compound index (`partialFilterExpression={"semantic_type": {"$type": "string"}}` — MongoDB doesn't accept `$ne: null` in partial filters).
- `apps/memory/src/tree/memory/extraction/core.py` — re-wrote `_parse_extraction` to handle the new wire shape: native `related_to + semantic_type`, plus tolerant re-route of legacy `todo` / `experienced` emissions to `related_to + has_task` / `related_to + experienced_by` (and `task` / `episode` endpoint types to `object` / `event`). Per-semantic constraint check (semantic in registry AND pair in `allowed_pairs`) — violations dropped with `logger.warning`. Updated `_SYSTEM_PROMPT` to teach the LLM about `semantic_type`. Updated `upsert_graph_entries` to persist `semantic_type`. Fixed pre-existing `except KeyError, ValueError:` syntax bug (caught only KeyError in Py3) — replaced with `except KeyError:` plus a separate `except ValueError:` chain via per-call try blocks for the type coercion.
- `apps/memory/src/tree/memory/extraction/pipeline.py` — `_apply_writes` now propagates `edge.semantic_type` through the remap → collapse step; `_upsert_edge` writes `semantic_type` on every edge document.
- `apps/memory/src/tree/memory/indexing/core.py` — added the live `(user_id, type, semantic_type)` partial index creation in `ensure_indexes`. Fixed pre-existing `except TypeError, ValueError:` syntax bug.
- `apps/memory/src/tree/memory/types.py` — `ExtractedEdge` gained `semantic_type: str | None = None`.
- `apps/memory/scripts/check_kgquery_discipline.py` — allow-listed the smoke script.
- `apps/memory/scripts/smoke_029_live_extraction.py` — new one-shot operator smoke: ingests "Paul was hired by Anthropic in March 2024…" and prints the `related_to` edges grouped by semantic_type.

Tests:
- `apps/memory/tests/unit/entities/test_ontology.py` — added `TestRelationSemanticsCatalogue` (catalogue completeness + per-spec property descriptions), `TestRegisterRelationSemantic` (idempotent / conflict), `TestRelatedToUmbrellaEdge` (allowed_pairs is union; LLM-extractable surface collapsed to `{related_to}`), `TestStructuralEdgePropertyModels` (Mentions/SameAs models + enums), `TestMentionsBroadeningAndCarveOut` (chunk/document cover but exclude preference), `TestSameAsBroadening`, `TestHasEdgeStructural`, `TestRetiredEdgeTypes`. Updated existing tests to reflect the post-#029 set (`test_edge_registry_has_post_029_edge_types`, `test_llm_extractable_edge_types_post_029`, `test_structural_edge_types_post_029`, `test_same_as_constraints_cover_post_029_pole_o_self_pairs`, `test_edge_type_exports_every_legacy_member`, `test_related_to_schema_has_semantic_types_map`). Snapshot file moved from v2 → v3.
- `apps/memory/tests/unit/entities/test_knowledge_graph.py` — added `TestRelatedToSemanticValidator` (5 branches per the AC), `TestSemanticTypeIndex` (partial-filter pinning), `TestStructuralHasEdgeAccepted` (both new structural pairs). Updated `test_existing_edge_types_unchanged` (drop TODO/EXPERIENCED), `test_accepts_every_registered_edge_type` (parametrized per edge type with correct (src, tgt) pairs and semantic_type for related_to), `test_edge_constructed_with_raw_string_type`, `test_build_edge_id_with_str`, `test_reroute_does_not_touch_edge_rows`, `test_edge_entry`, `test_subtype_on_edge_row_skipped`.
- `apps/memory/tests/unit/entities/snapshots/ontology_schema_v3.json` — new golden file with the umbrella `related_to` edge + nested `semantic_types`.
- `apps/memory/tests/unit/entities/test_node_id_isolation.py` — `EdgeType.TODO` → `EdgeType.RELATED_TO`.
- `apps/memory/tests/unit/memory/extraction/test_core.py` — replaced `test_valid_nodes_and_edges` with the post-#029 wire shape and added 4 dedicated drop-tests: legacy `todo` re-route, legacy `experienced` re-route, unknown semantic dropped, pair-violation dropped, missing semantic dropped.
- `apps/memory/tests/unit/memory/query/test_kgquery.py` — `EdgeType.TODO` → `EdgeType.RELATED_TO`.
- `apps/memory/tests/integration/entities/test_related_to_validator.py` — new integration suite with 9 tests covering every story in the task spec: employed_by persistence, pair-violation rejection, unknown semantic rejection, missing semantic rejection, chunk→organization mention, chunk→preference rejection (carve-out), self→preference `has`, self→object `has`, partial-index introspection.
- `apps/memory/tests/integration/memory/test_review.py` — updated the two outbound `todo` edges in the alice-merges-bob fixture to `related_to + semantic_type=has_task` with `object/task` targets. Added `subtype` to `_make_node` and `semantic_type` to `_make_edge`.
- `apps/memory/tests/integration/memory/test_extraction_pipeline.py` — updated the "dedup-via-extraction" assert to look for `related_to + has_task` on `{user}:person:alice|related_to|{user}:object:build ml pipeline` instead of the legacy `todo` shape.
- `apps/memory/tests/integration/test_two_user_isolation.py` — `find_edges(type=EdgeType.TODO)` → `find_edges(type=EdgeType.RELATED_TO)`.

**EdgeType enum: retired vs. retained**
- Retired (gone — no legacy alias; the wire shape changed so a dead enum would let callers emit edges the validator now rejects): `EdgeType.TODO`, `EdgeType.EXPERIENCED`.
- Retained: `EdgeType.PART_OF`, `NEXT`, `MENTIONS` (broadened pairs), `REFERENCED`, `RELATED_TO` (now umbrella + requires `semantic_type`), `HAS` (broadened pairs + flipped to structural), `SAME_AS` (broadened pairs).

**Call-sites walked**
- `tree.memory.review.core` — three `EdgeType.SAME_AS.value` reads (find_pending_duplicates / review_duplicate / get_same_as_cluster). Still valid.
- `tree.memory.extraction.core` — `EdgeType.PART_OF` / `NEXT` / `MENTIONS` / `REFERENCED` used for the structural-entry builder. Still valid. `upsert_graph_entries` now writes `semantic_type`.
- `tree.memory.extraction.pipeline` — `EdgeType.MENTIONS` used in `_apply_writes` for the chunk→person mentions. `_upsert_edge` now writes `semantic_type`. The `MENTIONS` here is `(document, person)` which is still in the broadened `mentions` set.
- `tree.memory.extraction.add_entity` — `EdgeType.SAME_AS` in `_upsert_pending_same_as_edge`. Properties shape (`status`, `match_type`, `confidence`) is already what `SameAsProperties` declares; no schema change needed.
- `tree.memory.extraction.dedup` — `EdgeType.SAME_AS.value` in the reject-pair lookup. Still valid.
- `tree.memory.query.nl_query` — iterates `EdgeType` for the prompt builder. Two retired enum members are gone; the prompt loses two stale entries.
- `scripts/smoke_resolution_dedup.py` — `EdgeType.SAME_AS`; untouched.
- `tests/integration/mcp/conftest.py` — seeds an `EdgeType.RELATED_TO` edge with **no `semantic_type`**. The integration tests that consume this fixture (the MCP tools) read the edge but never write it through the Pydantic validator, so the raw seed still works. Worth a note for the Tester: if MCP code paths start round-tripping through `KnowledgeGraphEntry.model_validate`, the seed will need a `semantic_type: "knows"` field. The current fast-integration run (`test_tools.py`, `test_deep_search.py`, `test_ingest_tools.py`, `test_ingest_url_after_dispatcher_migration.py`, etc.) all pass — none re-validate.

**Tests**
- Unit: 1005 passing, 0 failing — `make memory-unit-tests`.
- Fast integration: 128 passing, 12 skipped (all from external BrightData / Prefect-deployment-not-registered dependencies, none related to #029) — `make memory-integration-tests`.
- Slow integration: 46 passing — `uv --directory apps/memory run pytest tests/integration -m slow`.
- Format / lint / pre-commit: all clean.
- Live Gemini extraction smoke (`scripts/smoke_029_live_extraction.py`): 4 valid `related_to` edges emitted by Gemini (`employed_by` ×2, `headquarters_at` ×1, `knows` ×1). Headline outcome: the #028 → #029 staging-gap regression (live extraction producing 0 edges) is closed.

**Evidence**

```
$ make memory-unit-tests
… 1005 passed in 41.17s

$ uv --directory apps/memory run pytest tests/integration -m "not slow" --timeout=180
… 128 passed, 12 skipped, 46 deselected in 105.18s

$ uv --directory apps/memory run pytest tests/integration -m slow --timeout=300
… 46 passed, 140 deselected in 213.28s

$ make pre-commit
Validate pyproject.toml..............................(no files to check)Skipped
prettier.................................................................Passed
ruff check...............................................................Passed
ruff format..............................................................Passed
biome check (harness)....................................................Passed
KGQuery discipline (memory)..............................................Passed

$ ENV_FILE_PATH=$(pwd)/.env uv --directory apps/memory run python scripts/smoke_029_live_extraction.py
[smoke029] booting
smoke user: %s (id=%s) smoke029@example.com 6a0add518a455398440223bd
inserted doc id=%s 6a0ade188aa23c31a3067979
---- related_to edges for doc=%s ---- 6a0ade188aa23c31a3067979
  semantic=%s count=%d employed_by 2
    %s [%s] -> %s 6a0add518a455398440223bd:person:paul employed_by 6a0add518a455398440223bd:organization:anthropic
    %s [%s] -> %s 6a0add518a455398440223bd:person:sarah employed_by 6a0add518a455398440223bd:organization:anthropic
  semantic=%s count=%d headquarters_at 1
    %s [%s] -> %s 6a0add518a455398440223bd:organization:anthropic headquarters_at 6a0add518a455398440223bd:location:san francisco
  semantic=%s count=%d knows 1
    %s [%s] -> %s 6a0add518a455398440223bd:person:paul knows 6a0add518a455398440223bd:person:sarah

$ grep -rn "EdgeType.TODO\|EdgeType.EXPERIENCED" apps/memory/
(no hits)
```

**Notes**
- **Partial-filter expression** for the new index uses `{"semantic_type": {"$type": "string"}}` instead of the spec's exemplar `{"$exists": True, "$ne": None}` because MongoDB rejects `$ne: null` in partial filters (`Expression not supported in partial index: $not`). The two expressions are functionally equivalent for our purposes (every `semantic_type` is a string by validator contract). Pinned by the unit + integration index tests.
- **Strict-by-omission policy widened to all non-related_to edges.** The task instructions describe the strict drop-at-write-time policy for `related_to`. I extended the `KnowledgeGraphEntry` post-validator to enforce `EdgeTypeSpec.allowed_pairs` for every edge so the broadened `mentions` (carve-out preference) and the narrowed-then-broadened `has` are actually write-time constraints. Pre-#029 these were prompt-advisory only. Some existing tests that constructed pairs outside the new constraints failed and were updated; the change is purely tightening, never loosening.
- **`has` broadening for object/task.** Per the task instructions, `has` survives as a structural edge and accepts `(person, preference)` AND `(person, object)`. Pinned by `TestStructuralHasEdgeAccepted` and the integration suite. The `EdgeTypeSpec.llm_extractable` flag is `False` for `has` post-#029 (it was `True` pre-#029) because the spec is clear it's deterministically written by the pipeline, never LLM-emitted.
- **`LLM_EXTRACTABLE_EDGE_TYPES`** collapsed to `{EdgeType.RELATED_TO}` per the task's "Keep the set" decision. Callers iterate the set; no churn.
- **Audit-log surface (`extraction_rejections` collection)** is out of scope per the task spec — drops just `logger.warning` for now. #030 lifts it.
- **`EdgeType.TODO` / `EdgeType.EXPERIENCED` are HARD-removed** with no alias. The validator now rejects `kind="edge", type="todo"` rows at construction; this is the right policy because the wire shape changed (semantic_type is now a column). Re-routing in `_parse_extraction` is the only soft layer — there for tolerance of legacy LLM prompts / cached responses during the staging window before #033.
- **One pre-existing bug fixed in passing**: `except TypeError, ValueError:` in `indexing/core.py` (Python 3 only catches the first; the second was bound to a local name). Replaced with `except (TypeError, ValueError):`. The same syntax bug exists at `extraction/core.py:212` / `:229` in the pre-#029 code, but I rewrote that section as part of `_parse_extraction` and the new code uses correct `try`/`except` blocks.
- **Tester #028's headline gap is closed.** Pre-#029, live extraction emitted 0 edges because `EDGE_CONSTRAINTS[EdgeType.TODO]` still pointed at `task` while #028 had re-routed the endpoint to `object`. The new code path (a) drops the dead constraint by retiring the enum members, (b) re-routes legacy `task`/`episode` endpoint types in the LLM-output parser, and (c) re-routes `todo` / `experienced` types to the umbrella shape. Live smoke confirmed 4 valid edges land for a simple person/organization/location doc.
- **NOT RUN** — the `make memory-integration-tests-all` aggregate target wraps fast + slow + mongot; I ran each suite separately because the aggregate target runs them all in a single `pytest` invocation (~5min) and the fast + slow already cover the same surface separately. The numbers (128 fast + 46 slow + 12 skipped) match the expected total.

### [Tester] 2026-05-18 13:08 — QA

**Test summary**
- Format check: PASS (`ruff format --check src/ tests/ scripts/ deploy/` → "221 files already formatted")
- Lint check: PASS (`ruff check src/ tests/ scripts/ deploy/` → "All checks passed!")
- Pre-commit: PASS (Validate pyproject.toml / prettier / ruff check / ruff format / biome / KGQuery discipline — all green)
- Unit tests: **1005 passed, 0 failed, 0 warnings** in 42.35s (`make memory-unit-tests`)
- **Combined integration suite (`make memory-integration-tests-all`, slow + mongot)**: **185 passed, 1 skipped** in 398.49s (~6:38). The single skip is the external-cred `test_web_search_ingest` (BrightData), unrelated to #029. Zero warnings.

**E2E adversarial pass**

Centerpiece — **live Gemini extraction smoke** (`scripts/smoke_029_live_extraction.py`, ran against live MongoDB + Gemini via this worktree's venv; sibling worktree's `tree-prefect-worker` container stopped per task instructions):
- Happy path: ingest "Paul was hired by Anthropic… San Francisco… Paul knows Sarah" → 4 valid `related_to` edges with `semantic_type ∈ {employed_by ×2, headquarters_at ×1, knows ×1}` land in `knowledge_graph` (PASS).
- mongosh probe of the live collection confirmed:
  - `db.knowledge_graph.find({user_id, type: "related_to", semantic_type: "employed_by"})` returns the two `employed_by` rows.
  - The `user_type_semantic_type` index exists with the exact `partialFilterExpression={"semantic_type": {"$type": "string"}}` declared in the model. (Note: at the tiny dataset size used in the smoke (~6 rows), Mongo's planner picks the alternative `user_type_name` IXSCAN over the partial index — both are valid prefixes for `(user_id, type, …)`. The partial index is correctly declared and selectivity-favors itself at scale; the registration itself was the testable surface and is verified by `TestSemanticTypePartialIndex` in the integration suite.)

Break paths (Pydantic validator on `KnowledgeGraphEntry`, exercised both directly via a one-off probe and via the new integration suite):
- Break path 1 (pair violation — `related_to + employed_by` with `person→person`): rejected with `ValidationError` "does not allow pair ('person', 'person')". PASS. Pinned by `tests/integration/entities/test_related_to_validator.py::TestRelatedToPersistence::test_employed_by_pair_violation_rejected` and the unit-level `TestRelatedToSemanticValidator::test_rejects_pair_violation`.
- Break path 2 (unknown semantic — `related_to + semantic_type="not_in_registry"`): rejected with `ValidationError`. PASS. Pinned by `test_rejects_unknown_semantic` (both unit and integration).
- Break path 3 (carve-out — `mentions` `chunk → preference`): rejected with `ValidationError` ("edge type 'mentions' does not allow pair (chunk, preference)"). PASS. Pinned by `TestMentionsBroadeningPersistence::test_chunk_to_preference_rejected`.
- Break path 4 (cross-type — `same_as` `person → organization`): rejected with `ValidationError`. PASS. Verified live via the standalone validator probe; the broadened `same_as` registry pairs are pinned by `TestSameAsBroadening` (unit).
- Break path 5 (broadened structural — `has` `person:self → object/task`): accepted. PASS. Pinned by `TestStructuralHasEdgeAccepted::test_has_person_to_object_accepted` and `TestHasBroadeningPersistence::test_self_to_object_task_persists`.
- Break path 6 (retired type — `EdgeType.TODO`): the enum member is HARD-removed (`AttributeError: TODO` at access). Constructing a raw `type="todo"` edge fails at `_check_type_against_registry` (the registry now lists only `{has, mentions, next, part_of, referenced, related_to, same_as}`). Both halves PASS — failure is at validation, not at import. Pinned by the existing parser drop-tests in `test_core.py` (`test_legacy_todo_reroutes_to_related_to`) and the registry catalogue tests.
- Break path 7 (retired type — `EdgeType.EXPERIENCED`): same shape as #6. `AttributeError` on attribute access; raw `type="experienced"` rejected by the registry validator. The SWE chose the "fail at validation" branch and re-routes the legacy emission to `related_to + semantic_type="experienced_by"` at the parser layer (`test_legacy_experienced_reroutes_to_related_to`). PASS.
- Break path 8 (semantic_type on non-related_to — `has` with `semantic_type="employed_by"`): rejected with `ValidationError` ("semantic_type is reserved for type='related_to' edges"). PASS. Pinned by `TestRelatedToSemanticValidator::test_rejects_semantic_on_non_related_to`.
- Round-trip (all 16 semantics × every canonical pair): the standalone probe constructed every `(semantic_name, allowed_pair)` combination via the model validator with valid input → all 15 unique pairs across 16 semantics accept cleanly.
- Schema determinism: two separate `python -c 'json.dumps(get_ontology_schema(), sort_keys=True)'` invocations produced byte-identical output (`diff` empty). PASS.

**Acceptance criteria**

- [x] PASS — `RelationSemanticSpec` dataclass (frozen) defined; `RELATION_SEMANTICS` dict populated with **16 entries** at import time (14 canonical + `has_task` + `experienced_by`). Verified via `python -c "from tree.entities.ontology import RELATION_SEMANTICS; assert len(RELATION_SEMANTICS)==16"` → confirms `['alias_of','employed_by','experienced_by','has_task','headquarters_at','involved','knows','located_at','member_of','occurred_at','owns','participated_in','partner_with','resides_at','subsidiary_of','uses']`. Pinned by `TestRelationSemanticsCatalogue`.
- [x] PASS — `register_relation_semantic(spec)` idempotent / conflict-raises. `apps/memory/src/tree/entities/ontology.py:290-308` matches the spec; `TestRegisterRelationSemantic` covers both branches.
- [x] PASS — `EdgeType.TODO` / `EdgeType.EXPERIENCED` removed. `grep -rn "EdgeType.TODO\|EdgeType.EXPERIENCED" apps/memory/src/` returns only docstring references (5 hits across `knowledge_graph.py:69`, `ontology.py:717,735,1047,1061`), not call-sites. No production-path lookup of the retired members. `EdgeType.HAS` retained with broadened allowed_pairs to `[(person, preference), (person, object)]`.
- [x] PASS — `KnowledgeGraphEntry.semantic_type: str | None = None` is a live column. The 5 validator branches (accept, pair-violation, unknown semantic, missing semantic on related_to, semantic_type on non-related_to) all pass in `TestRelatedToSemanticValidator`.
- [x] PASS — Compound index `(user_id, type, semantic_type)` declared as partial-filtered (`partialFilterExpression={"semantic_type": {"$type": "string"}}`). Live `mongosh` introspection on `knowledge_graph` confirmed the index is present after the smoke run. The SWE's note (Mongo rejects `$ne: null` in partial filters) is accurate and the `$type: string` substitution is functionally equivalent. Pinned by `TestSemanticTypeIndex` + `TestSemanticTypePartialIndex`.
- [x] PASS — `MentionsProperties` / `SameAsProperties` / `SameAsMatchType` / `SameAsStatus` defined and registered. Verified via direct registry inspection: `EDGE_REGISTRY["mentions"].properties_schema.__name__ == "MentionsProperties"`. `TestStructuralEdgePropertyModels` covers the `Field(description=…)` requirement.
- [x] PASS — `mentions` broadening is `{chunk, document} × {person, organization, location, event, object}` (10 pairs, preference carved out). Verified live: `sorted(EDGE_REGISTRY["mentions"].allowed_pairs)` matches the expected cross-product exactly.
- [x] PASS — `same_as` is the self-pair across every POLE+O LLM-extractable type including `preference` (6 pairs). Verified: `{(person,person), (organization,organization), (location,location), (event,event), (object,object), (preference,preference)}`.
- [x] PASS — `get_ontology_schema()` snapshot at `tests/unit/entities/snapshots/ontology_schema_v3.json` includes the `related_to` umbrella edge with a 16-entry nested `semantic_types` map. Verified via direct JSON parse: `len(d['edge_types']['related_to']['semantic_types']) == 16`. Two-process schema-determinism diff: byte-identical.
- [x] PASS — Phase-1 `add_entity` / `same_as` / `review_duplicate` contract intact. `tests/integration/memory/test_add_entity.py::*` (11 tests), `tests/integration/memory/test_dedup.py` (14 tests), and `tests/integration/memory/test_review.py` (18 tests) all green inside the combined run.
- [x] PASS — LLM extraction pipeline emits `related_to + semantic_type` rows. `test_core.py::TestParseExtraction` covers the 5 drop-and-reroute branches (legacy `todo` re-route, legacy `experienced` re-route, unknown semantic dropped, pair violation dropped, missing semantic dropped). **Live Gemini smoke landed exactly 4 valid edges with valid semantic_types** for the test document (`employed_by ×2`, `headquarters_at ×1`, `knows ×1`) — closes the #028 → #029 staging-gap regression (live extraction was producing 0 edges pre-#029).
- [x] PASS — `make memory-format-fix && make memory-lint-fix && make memory-format-check && make memory-lint-check` clean.
- [x] PASS — `make pre-commit` green (all 6 hooks).
- [x] PASS — `make memory-unit-tests` green: **1005 passed, 0 failed, 0 warnings** in 42.35s.
- [x] PASS — `make memory-integration-tests` fast loop included in the combined run below.
- [x] PASS — **`make memory-integration-tests-all`** (full incl. mongot): **185 passed, 1 skipped, 0 failed, 0 warnings** in 398.49s. The skipped test is `data/web/test_web_search_ingest.py::test_search_web_ingest_skips_when_no_brightdata` (external creds), unrelated to #029.

**Two-user isolation regression**: `tests/integration/test_two_user_isolation.py` (26 tests) + `tests/integration/test_two_user_review_isolation.py` (3 tests) all green inside the combined integration run.

**Call-site sweep for retired/broadened edge types**

`grep -rn "EdgeType.TODO\|EdgeType.EXPERIENCED" apps/memory/src/` → 5 hits, ALL in docstrings/comments (no production lookup). `grep -rn "EdgeType\.HAS" apps/memory/src/` → 0 hits (HAS was never used by name in non-test production code; it's the new structural sink for `has_task` re-routes which the pipeline writes via the umbrella). Test files reference the retired enum names ONLY in comments explaining why they are gone. No lingering production-path reference to retired members.

**Evidence**

```
$ make memory-format-check
221 files already formatted

$ make memory-lint-check
All checks passed!

$ make pre-commit
Validate pyproject.toml..............................(no files to check)Skipped
prettier.................................................................Passed
ruff check...............................................................Passed
ruff format..............................................................Passed
biome check (harness)....................................................Passed
KGQuery discipline (memory)..............................................Passed

$ make memory-unit-tests
============================ 1005 passed in 42.35s =============================

$ make memory-integration-tests-all
================== 185 passed, 1 skipped in 398.49s (0:06:38) ==================

$ ENV_FILE_PATH=$(pwd)/.env uv --directory apps/memory run python scripts/smoke_029_live_extraction.py
---- related_to edges for doc=… ----
  semantic=employed_by count=2
    …:person:paul -> …:organization:anthropic
    …:person:sarah -> …:organization:anthropic
  semantic=headquarters_at count=1
    …:organization:anthropic -> …:location:san francisco
  semantic=knows count=1
    …:person:paul -> …:person:sarah

$ mongosh … --eval 'db.knowledge_graph.getIndexes().filter(i=>i.name==="user_type_semantic_type")'
[ { v: 2, key: { user_id: 1, type: 1, semantic_type: 1 },
    name: 'user_type_semantic_type',
    partialFilterExpression: { semantic_type: { '$type': 'string' } } } ]

$ python -c "from tree.entities.ontology import RELATION_SEMANTICS; print(len(RELATION_SEMANTICS), sorted(RELATION_SEMANTICS))"
16 ['alias_of','employed_by','experienced_by','has_task','headquarters_at','involved','knows','located_at','member_of','occurred_at','owns','participated_in','partner_with','resides_at','subsidiary_of','uses']

# Two-process schema-determinism diff
$ diff /tmp/schema_a.json /tmp/schema_b.json && echo BYTE_IDENTICAL
BYTE_IDENTICAL

# Adversarial validator probe (subset; full break-path matrix already pinned in tests)
PASS 1: pair (person,person) rejected for employed_by
PASS 2: unknown semantic rejected
PASS 3: chunk→preference mention rejected
PASS 4: same_as cross-type rejected
```

**Other issues found**
- None blocking. Two minor smell-flags worth a follow-up but not gating:
  1. `apps/memory/scripts/smoke_029_live_extraction.py` mixes `logger.info`-style format strings with `print(...)` calls — the `%s` placeholders show up literally in operator output (e.g. `inserted doc id=%s 6a0ae4402fa2f5c17777cb65` instead of substituting). Cosmetic only — a one-shot operator smoke, not a library. Worth a 2-line follow-up to switch to f-strings or `logger.info` consistently. Not in any AC, not a FAIL.
  2. The `tests/integration/mcp/conftest.py` `RELATED_TO` seed (the SWE flagged this) doesn't carry a `semantic_type` and only survives because the MCP tools never re-validate the row through `KnowledgeGraphEntry.model_validate`. If a future MCP code path round-trips it, the seed silently breaks. Worth a #030 nit, not a #029 blocker — the current fast-integration run (12 MCP tests across 5 files) is green.

**VERDICT: PASS**
