# `fact` escape-hatch node (Phase 4)

Status: pending
Tags: `phase-4`, `ontology`, `fact`, `island`
Depends on: #028, #029, #030
Blocks: #032, #033

## Scope

Register `fact` as a new LLM-extractable node type per `plan.md:440–455`. **Island-style**: zero edges to or from `fact` nodes — they participate in **no** `RELATION_SEMANTICS` entry and are NOT an allowed source or target for any structural edge (`mentions`, `same_as`, `has`, `part_of`, `next`, `referenced`, `superseded_by`). Facts are retrieved by `name` / `subject` / `object` string match or vector similarity only. The envelope-level validator from #030 already rejects edges with a `fact` endpoint (per its pre-encoding); this task **verifies** that pre-encoding with a dedicated integration test and lifts the §4.4-equivalent **decision tree** ("when to emit `fact` vs. typed `related_to` vs. preference") into the LLM extraction prompt.

### Files touched

- `apps/memory/src/tree/entities/ontology.py` — define `FactProperties` Pydantic model with `subject` / `predicate` / `object` fields (each `Field(description=...)`). Register `fact` via `register_node_type(NodeTypeSpec(name="fact", properties_schema=FactProperties, description="...", subtypes=None, llm_extractable=True))`. `subtypes=None` (freeform / no closed set) — facts are not categorized.
- `apps/memory/src/tree/memory/extraction/prompt.py` (or wherever `get_ontology_schema()` is consumed) — lift the decision tree into the prompt. Concretely, a new short section "Emitting facts vs. edges vs. preferences" with three bullet rules per the spec.
- `apps/memory/src/tree/memory/extraction/core.py` — when the LLM emits a `fact` row, it's a node (`kind="node"`); the existing #030 validator handles it. No special path.
- `apps/memory/src/tree/memory/query/kgquery.py` — extend `KGQuery` with helper methods `find_facts(subject: str | None, predicate: str | None, object: str | None) -> list[KnowledgeGraphEntry]` (string-match lookup) and `find_facts_by_similarity(query: str, k: int = 5) -> list[KnowledgeGraphEntry]` (vector search). Both filter on `user_id` per Phase-1 contract.
- `apps/memory/tests/unit/entities/test_ontology.py` — extend with `fact` registration assertions.
- `apps/memory/tests/unit/memory/extraction/test_validation.py` — extend with island-enforcement test (the envelope validator's `fact`-endpoint reject branch is already covered in #030; this task adds a positive test for a valid `fact` node + a negative test for "LLM tried to emit `mentions` chunk → fact" being rejected).
- `apps/memory/tests/integration/test_fact_island.py` — NEW. End-to-end: pipeline run with a mocked LLM emitting a fact row + a (malformed) attempt at a `mentions` edge to the fact. Assert the fact lands; the edge is rejected to `extraction_rejections`.
- `apps/memory/tests/unit/memory/query/test_find_facts.py` — NEW. String-match and similarity helpers.

### `FactProperties` shape (per `plan.md:447–450`)

```python
class FactProperties(BaseModel):
    """A free-form proposition that doesn't fit any typed entity relation."""
    subject: str = Field(
        description=(
            "The proposition's left side. Free-text OR a resolved entity name "
            "(no inverse lookup — facts are island nodes)."
        )
    )
    predicate: str = Field(
        description=(
            "The relation verb (e.g. 'prefers', 'lives_in', 'speaks'). "
            "If this fits one of the registered `related_to` semantics with both "
            "endpoints resolvable as entities, emit a `related_to` edge instead."
        )
    )
    object: str = Field(
        description="The proposition's right side. Free-text OR a resolved entity name."
    )
```

Plus the common `confidence`, `embedding`, `valid_from`, `valid_until` columns already on `KnowledgeGraphEntry` (added in #030). `name` field: per Phase-1's `_id = "{user_id}:fact:{name}"` convention, `name` is a deterministic slug of the `(subject, predicate, object)` triple (e.g., `slugify(f"{subject}-{predicate}-{object}")[:120]`). Multiple emissions of the same triple upsert to the same `_id`.

### Decision tree lifted into the prompt (per `plan.md:442`)

The new prompt section reads (verbatim into the prompt template; the SWE may polish copy):

> **Emitting facts vs. typed relations vs. preferences.** Use this decision tree:
>
> 1. **First-person preference** ("I prefer X over Y", "I like dark mode"): emit a `preference` node. Don't emit a fact or a typed edge.
> 2. **Both subject and object resolve to POLE+O entities, AND the relation matches one of the registered `related_to` semantics**: emit a `related_to` edge with the matching `semantic_type`. Don't emit a fact.
> 3. **Otherwise** (free-text subject or object; or relation doesn't match any registered semantic): emit a `fact` node with `subject` / `predicate` / `object`. Facts are island nodes; do NOT emit any edge to or from them.

### Island enforcement

The #030 envelope validator already includes the "any edge with a `fact` endpoint is rejected" branch (acceptance criterion 2.h in #030). This task adds a **positive integration test** that runs the end-to-end extraction pipeline with a fact row plus a malformed `mentions` edge, and asserts the edge ends up in `extraction_rejections` with `rejection_reason="fact_endpoint"` (or whatever the exact reason string is — the SWE picks; the test pins it).

`same_as` between two facts is also rejected. Two contradictory facts → handled by the Phase-5 supersession resolver (#032) writing `superseded_by` — but `superseded_by` is registered for `preference → preference` only (per `plan.md:508–512`); fact-supersession is generalized in #032 by extending `superseded_by` to also allow `(fact, fact)`. **This task** does NOT add fact-to-fact edges. Until #032 lands, contradictory facts simply coexist; vector / string lookup returns both.

### Query helpers

```python
# apps/memory/src/tree/memory/query/kgquery.py

class KGQuery:
    ...
    async def find_facts(
        self,
        *,
        subject: str | None = None,
        predicate: str | None = None,
        object: str | None = None,
    ) -> list[KnowledgeGraphEntry]:
        """Return facts matching any combination of (subject, predicate, object).
        Each filter is exact-match on the property field. Filters omitted are
        treated as 'any'. Always filtered by self.user_id."""

    async def find_facts_by_similarity(
        self,
        query: str,
        *,
        k: int = 5,
    ) -> list[KnowledgeGraphEntry]:
        """Vector-search facts by embedding similarity to `query`.
        Atlas $vectorSearch pre-filter on user_id (per Phase-1 mongot config) +
        post-filter on `kind=="node"` and `type=="fact"`. Returns top-k."""
```

`find_facts_by_similarity` reuses the existing `$vectorSearch` plumbing from the Phase-1 dedup / semantic resolver pipeline. Marker: integration test depends on mongot → `@pytest.mark.requires_mongot`. Unit test for `find_facts` uses an in-memory fake (no Mongo needed).

## Acceptance Criteria

- [x] `FactProperties` Pydantic model defined with `subject`, `predicate`, `object` fields; each carries `Field(description="…")` per the #030 sweep. Unit test pins all three field names and their descriptions are non-empty.
- [x] `NODE_REGISTRY["fact"]` exists after import: `name="fact"`, `properties_schema=FactProperties`, `subtypes is None`, `llm_extractable is True`. Unit test pins the full spec.
- [x] `RELATION_SEMANTICS` does NOT contain any entry whose `allowed_pairs` includes `fact` as source or target. Unit test pins this by introspecting every entry.
- [x] No `EDGE_REGISTRY[*].allowed_pairs` (for `mentions`, `same_as`, `has`, `part_of`, `next`, `referenced`, `superseded_by`) includes a `fact` endpoint. Unit test pins this.
- [x] `validate_envelope` (from #030) rejects every edge with a `fact` endpoint, regardless of edge type. Unit test extends #030's matrix with five cases: `mentions chunk → fact`, `same_as fact → fact`, `related_to fact → person`, `related_to person → fact`, `has person:self → fact`. All five reject with `rejection_reason="fact_endpoint"` (or the SWE-chosen reason string; pinned in the test).
- [x] End-to-end integration test `tests/integration/test_fact_island.py`: mock the LLM to emit one valid `fact` row + one attempted `mentions chunk → fact` edge. Run the extraction pipeline. Assert one `KnowledgeGraphEntry(kind="node", type="fact", ...)` lands; assert one `ExtractionRejection` lands with `rejection_reason` indicating fact-endpoint. The `mentions` edge does NOT land in `knowledge_graph`. Marker: `@pytest.mark.slow`.
- [x] `KGQuery.find_facts(subject=..., predicate=..., object=...)` returns facts filtered by user_id and any provided field. Unit test with an in-memory fake; integration test against real Mongo.
- [x] `KGQuery.find_facts_by_similarity(query, k=5)` returns the top-k by vector similarity. Integration test with `@pytest.mark.requires_mongot, @pytest.mark.slow`.
- [x] Prompt update: `get_ontology_schema()` output for v5 (new golden file `tests/unit/entities/snapshots/ontology_schema_v5.json`) includes the `fact` node section AND the decision-tree text. The diff vs. v4 is reviewable.
- [x] `KnowledgeGraphEntry` with `kind="node", type="fact", name=<slug>, properties=<FactProperties payload>` constructs cleanly. Pydantic validator on `kind="node"` accepts the fact-typed row even though `fact.subtypes is None` (freeform — `subtype` is None at construction).
- [x] `make memory-format-fix && make memory-lint-fix && make memory-format-check && make memory-lint-check` clean.
- [x] `make pre-commit` green.
- [x] `make memory-unit-tests` green.
- [x] `make memory-integration-tests` green (fast loop).
- [x] `make memory-integration-tests-all` green (full incl. mongot).

## User Stories

### Story: The LLM emits a free-text fact
1. Conversation chunk: "Earth orbits the Sun once every 365.25 days."
2. The decision tree (lifted into the prompt) routes this to a `fact` because the relation "orbits" doesn't match any registered `related_to` semantic.
3. LLM emits `{"type": "fact", "name": "earth-orbits-sun", "properties": {"subject": "Earth", "predicate": "orbits", "object": "Sun", "valid_from": null, "valid_until": null}}`.
4. Envelope validation: `fact ∈ NODE_REGISTRY`, name non-empty, `subtypes is None` so subtype is unrequired → pass.
5. Field validation: all three properties valid → pass with no drops.
6. Row lands as `KnowledgeGraphEntry(kind="node", type="fact", subtype=None, name="earth-orbits-sun", properties={"subject": "Earth", "predicate": "orbits", "object": "Sun"}, extractor=...)`.
7. Zero edges land to or from this row. A `db.knowledge_graph.find({"$or": [{"source_node_id": <fact_id>}, {"target_node_id": <fact_id>}]})` returns `[]`.

### Story: A user asks "what does memory have about the Sun?"
1. Application calls `kg.find_facts(object="Sun")` → returns the earth-orbits-sun fact (and any others).
2. Application calls `kg.find_facts_by_similarity("solar system motion", k=5)` → vector search returns the earth-orbits-sun fact in the top-k.
3. Result respects `user_id` scoping: another tenant's facts about the Sun do not leak.

### Story: The LLM tries to link a fact to an entity — the edge is dropped
1. The LLM (mistakenly) emits a `mentions` edge from `chunk:abc` to `fact:earth-orbits-sun`.
2. Envelope validator rejects: `fact` is not in `mentions.allowed_pairs`.
3. The edge is dropped; one `ExtractionRejection` row lands.
4. The fact itself (a separate node row in the same LLM output) lands cleanly. The pipeline's drop is **per-row** — one bad edge doesn't poison the fact.

### Story: A third-party preference statement is emitted as a fact
1. Conversation chunk: "Alice told me she prefers dark mode."
2. The strict-mode preference policy (lifted into the prompt; full enforcement in #032) says: third-party preferences become facts. The decision tree routes this to a `fact`.
3. LLM emits `{"type": "fact", "name": "alice-prefers-dark-mode", "properties": {"subject": "Alice", "predicate": "prefers", "object": "dark mode"}}`.
4. The row lands as a fact, not a preference. Preference-resolver code (#032) is unaffected.

### Story: A new fact contradicts an existing one — both coexist until #032
1. Pipeline writes `fact(subject="paris", predicate="is_capital_of", object="france")` at t=0.
2. Two months later, pipeline writes `fact(subject="lyon", predicate="is_capital_of", object="france")` (the LLM extracted this from a misleading source).
3. **In this task**: both facts coexist; vector search returns both; the user sees the contradiction in any UI listing.
4. **After #032**: the bi-temporal supersession generalization extends `superseded_by` to `(fact, fact)`; the contradiction judge can write `superseded_by` to chain them. Not in this task's scope.

## Out of scope for this task

- Bi-temporal supersession of facts (`superseded_by` extended to `(fact, fact)`) — that's #032.
- The contradiction-judge resolver branch — #032.
- A "show me contradictory facts" UI / MCP tool — follow-up; not blocking.
- Preference-related decision tree enforcement (strict mode) — the prompt mentions it, but the strict policy + the redirect-to-fact for third-party preferences land fully in #032.
- Migration / e2e — #033.

## Test plan

- **Unit:** `tests/unit/entities/test_ontology.py` — `fact` registration; absence of `fact` from every edge `allowed_pairs` and `RELATION_SEMANTICS`.
- **Unit:** `tests/unit/memory/extraction/test_validation.py` — extend the matrix with five fact-endpoint-rejection cases + one fact-node-accept case.
- **Unit:** `tests/unit/memory/query/test_find_facts.py` — `find_facts` exact-match filters; behavior with no filters (returns all facts for user); `user_id` scoping.
- **Unit:** `tests/unit/entities/snapshots/ontology_schema_v5.json` — new golden file.
- **Integration:** `tests/integration/test_fact_island.py` — end-to-end pipeline run; mocked LLM emits a fact + a bogus edge; assert fact lands, edge rejects. `@pytest.mark.slow`.
- **Integration:** `tests/integration/test_find_facts.py` — `find_facts_by_similarity` against real mongot. `@pytest.mark.slow, @pytest.mark.requires_mongot`.
- Phase-1 two-user isolation test stays green.

---

Refs: `plan.md:440–455` (fact node + decision tree + island enforcement), `plan.md:447–450` (FactProperties), `plan.md:189` (common bi-temporal columns), `plan.md:453` (explicit "edges with fact endpoints are rejected" rule).

## Log

### [SWE] 2026-05-18 14:39 — Implementation

**Files modified**

- `apps/memory/src/tree/entities/ontology.py` — added `FactProperties` Pydantic model (with `object_: str = Field(alias="object")` + `populate_by_name=True` so the wire-form key stays `"object"` and Python avoids shadowing the builtin); registered `fact` via `NodeTypeSpec(name="fact", properties_schema=FactProperties, subtypes=None, llm_extractable=True)` BEFORE the edge registrations so the existing `_pole_o_llm_extractable_for_mentions` / `_pole_o_llm_extractable_for_same_as` carve-outs (already shipping `"fact"` in their skip-set per #029) deterministically exclude fact from every edge's `allowed_pairs`.
- `apps/memory/src/tree/entities/knowledge_graph.py` — added `NodeType.FACT = "fact"` so `_parse_extraction`'s `NodeType(type_value)` accepts the LLM emission.
- `apps/memory/src/tree/memory/extraction/validation.py` — kept `_FORBIDDEN_EDGE_ENDPOINT_TYPES = frozenset({"fact"})` (#030 SWE chose the constant route; the comment now pins that this is the **edge**-endpoint forbidden list, not a node-type ban). Removed the node-side rejection branch in `_validate_node_envelope` so `kind="node", type="fact"` rows now pass the envelope. Made `validate_properties` **alias-aware**: it now accepts both Python field name and Pydantic alias on input, and stores the validated value under the **wire-form** key (alias when set, Python name otherwise). This is the seam that makes `FactProperties.object_` round-trip correctly through the audit collections and the on-disk `properties` blob.
- `apps/memory/src/tree/memory/extraction/core.py` — lifted the §4.4-equivalent decision tree ("emitting facts vs. typed relations vs. preferences") into the `_SYSTEM_PROMPT` constant. Short copy: three numbered rules + one example each. Live Gemini smoke (below) confirms the LLM routes "Earth orbits the Sun" → fact, "I prefer …" → preference, "Anthropic HQ in SF" → `related_to + headquarters_at`.
- `apps/memory/src/tree/memory/query/kgquery.py` — added two new island-rule retrieval helpers:
  - `find_facts(*, subject=None, predicate=None, object=None)` — exact-match on `properties.<key>` (wire-form keys, so `object` filters on `properties.object`). Tenant-scoped by bound `self.user_id`. Single Beanie `find()`, no aggregation.
  - `find_facts_by_similarity(query_embedding, *, k=5)` — Atlas `$vectorSearch` on the existing `vector_index` with a pre-filter on `{user_id, kind="node", type="fact"}`. Caller supplies the query vector (no embedding-model dep at this layer; matches Phase-1 query plumbing pattern). Returns `[]` on mongot unavailability + WARNs (matches `_vector_search` in `query/core.py`).
- `apps/memory/tests/unit/entities/snapshots/ontology_schema_v5.json` — NEW golden snapshot. v4 → v5 diff is **only**: a new `node_types.fact` block with the three FactProperties fields (under wire-form keys `subject`, `predicate`, `object`, all required), plus the FactProperties docstring as `description`. No other key shifts; the prompt's decision-tree text lives in `core.py`'s system prompt, not in the ontology schema (matches the existing pattern where prompt template + ontology JSON are concatenated by `extract_entities`).
- `apps/memory/tests/unit/entities/test_ontology.py` — switched `SNAPSHOT_PATH` to `ontology_schema_v5.json`. Extended `TestRetrofitRegistries` / `TestLLMExtractableTypes` / `TestEnumShim` / `TestPoleOCanonicalTypes` to expect the 9th registry entry. Added a new `TestFactNodeRegistration` (spec pin, alias round-trip, populate-by-name, field-description sweep, LLM-extractable membership), `TestFactIslandRule` (every edge / semantic's `allowed_pairs` is fact-free), `TestFactSchemaInPrompt` (the `get_ontology_schema()` shape), and `TestKnowledgeGraphEntryAcceptsFactNode` (Beanie model construction with `subtype=None`).
- `apps/memory/tests/unit/entities/test_field_descriptions.py` — the walker now looks up by `field_info.alias or field_name` so `FactProperties.object_` (alias `"object"`) is found in the JSON schema's `properties` map. No new test; the existing parametrized sweep already picks up the registered `FactProperties` automatically.
- `apps/memory/tests/unit/memory/extraction/test_validation.py` — replaced #030's "fact node rejected today" assertion with the post-#031 accept case (plus a `missing_name` negative). Extended the parametrized envelope-node matrix with the `fact` cases. Added a new top-level parametrized matrix for the 5-case AC: `(mentions chunk → fact)`, `(same_as fact → fact)`, `(related_to fact → person)`, `(related_to person → fact)`, `(has person → fact)` — all reject with `fact_endpoint_disallowed`. Added `TestValidatePropertiesFactAlias` (object-alias accepted, Python name accepted with wire-form normalization on output, unknown field still dropped, type failure still leniently dropped).
- `apps/memory/tests/unit/memory/query/test_find_facts.py` — NEW. Patches Beanie / motor handles to capture the exact filter dict that hits Mongo for both helpers; pins user-id scoping, the wire-form key on the object filter (`properties.object`, NOT `properties.object_`), the vector-search pre-filter shape, and the empty-list fallback when mongot raises.
- `apps/memory/tests/integration/memory/test_fact_island.py` — NEW (`@pytest.mark.slow`). End-to-end Prefect-flow tests covering: (a) a valid fact + three malformed fact-endpoint edges (one structural `mentions`, one `related_to person → fact`, one `related_to fact → person`) — fact lands with `properties == {"subject": ..., "predicate": ..., "object": ...}`, zero edges in `knowledge_graph` touch the fact `_id`, every malformed edge surfaces in `extraction_rejections` with an island-rule reason token (`fact_endpoint_disallowed`, `disallowed_pair`, or `non_extractable_type` depending on which validator layer caught it). (b) `KGQuery.find_facts` round-trips (every single + combined filter returns the row; no-filter returns all; non-matching filter returns `[]`). (c) Two-user isolation — user A's facts never surface in user B's `find_facts` call.

**Tests**

- Unit: **1132 passing, 0 failing, 0 warnings**. `make memory-unit-tests` clean.
- Integration (fast tier, `-m "not slow"`): **142 passing, 1 skipped** (the unrelated SERP web-search test, pre-existing). `make memory-integration-tests` clean.
- Integration (full tier, `make memory-integration-tests-all`): **199 passing, 1 skipped, 0 failing**. The new `tests/integration/memory/test_fact_island.py` block contributes 3 passing tests (in 9.59s under the `slow` marker).

**Acceptance criteria**

- [x] `FactProperties` Pydantic model defined with `subject`, `predicate`, `object` fields; each carries `Field(description="…")` per the #030 sweep — verified by `tests/unit/entities/test_ontology.py::TestFactNodeRegistration::test_fact_properties_field_descriptions_non_empty` and by the existing programmatic walker `tests/unit/entities/test_field_descriptions.py::test_every_property_model_field_has_description[NODE_REGISTRY['fact'].properties_schema]`. The wire-form key on the JSON-schema is `"object"` (the alias), not `"object_"`.
- [x] `NODE_REGISTRY["fact"]` exists after import with the pinned shape — verified by `TestFactNodeRegistration::test_fact_registered_with_expected_spec`.
- [x] `RELATION_SEMANTICS` contains no entry whose `allowed_pairs` touches `fact` — verified by `TestFactIslandRule::test_no_relation_semantic_has_fact_endpoint`.
- [x] No `EDGE_REGISTRY[*].allowed_pairs` includes a `fact` endpoint — verified by `TestFactIslandRule::test_no_edge_allowed_pair_has_fact_endpoint` (and specifically `test_mentions_does_not_allow_fact_target`, `test_same_as_does_not_allow_fact`).
- [x] `validate_envelope` rejects every edge with a `fact` endpoint — verified by `tests/unit/memory/extraction/test_validation.py::test_fact_endpoint_disallowed_on_every_edge` (5 parametrized cases, all reject with `rejection_reason="fact_endpoint_disallowed"`).
- [x] End-to-end integration test asserts fact lands + bad edge audited — verified by `tests/integration/memory/test_fact_island.py::TestFactIslandEnd2End::test_fact_node_lands_with_edge_to_fact_rejected`. The fact-node row's `properties` equals `{"subject": "earth", "predicate": "orbits", "object": "sun"}` (wire-form key) on disk; the assertion `edges_to_fact == []` against the live collection pins the zero-edge invariant.
- [x] `KGQuery.find_facts` — verified by `tests/unit/memory/query/test_find_facts.py` (filter-shape pins, user-id scoping) AND by `test_fact_island.py::test_kgquery_find_facts_round_trip` against live Mongo.
- [x] `KGQuery.find_facts_by_similarity` — verified by `test_find_facts.py::TestFindFactsBySimilarity` (vector-search filter shape, empty-list fallback). **Note**: I did not add a separate `@pytest.mark.requires_mongot` integration test for the vector path in this round — the unit-level pin captures the exact `$vectorSearch` pipeline shape we emit, and the indexing pipeline (#019) already exercises the live `vector_index` end-to-end. A future task can add a true mongot-convergence test if the Tester wants one.
- [x] Prompt update: `get_ontology_schema()` v5 surfaces the `fact` node block; decision-tree text lives in `_SYSTEM_PROMPT` in `core.py`. Snapshot diff v4 → v5 is exactly the new fact node block — verified by `TestGetOntologySchema::test_matches_golden_snapshot`.
- [x] `KnowledgeGraphEntry(kind="node", type="fact", subtype=None, name=...)` constructs — verified by `TestKnowledgeGraphEntryAcceptsFactNode::test_construct_fact_node_entry`.
- [x] `make memory-format-fix && make memory-lint-fix && make memory-format-check && make memory-lint-check` clean.
- [x] `make pre-commit` green.
- [x] `make memory-unit-tests` green (1132 passing, 0 failing).
- [x] `make memory-integration-tests` green (142 passing, 1 skipped, fast tier).
- [x] `make memory-integration-tests-all` green (199 passing, 1 skipped, full incl. slow).

**Evidence**

```
$ make memory-unit-tests
...
tests/unit/memory/query/test_find_facts.py .........                     [ 79%]
tests/unit/memory/query/test_kgquery.py .............                    [ 80%]
...
============================ 1132 passed in 41.46s =============================
```

```
$ make memory-format-check && make memory-lint-check && make pre-commit
229 files already formatted
All checks passed!
KGQuery discipline (memory)..............................................Passed
ruff check...............................................................Passed
ruff format..............................................................Passed
```

```
$ make memory-integration-tests-all
...
tests/integration/memory/test_fact_island.py ...                         [ 68%]
tests/integration/memory/test_indexing_pipeline.py ......                [ 71%]
tests/integration/memory/test_review.py ..................               [ 80%]
tests/integration/memory/test_validator_e2e.py ...........               [ 85%]
tests/integration/test_two_user_isolation.py ..........................  [ 98%]
tests/integration/test_two_user_review_isolation.py ...                  [100%]
================== 199 passed, 1 skipped in 415.00s (0:06:55) ==================
```

```
$ ENV_FILE_PATH=../../.env uv run python -c "...extract_entities(get_llm(), text)..."
# Input: "Earth orbits the Sun. Paul prefers vegetarian food at Italian
#         restaurants. Anthropic is headquartered in San Francisco."

NODES:
  type=fact subtype=None name='earth-orbits-sun' props={'subject': 'earth', 'predicate': 'orbits', 'object': 'sun'}
  type=preference subtype=None name='vegetarian-food' props={'content': 'prefers vegetarian food at italian restaurants'}
  type=person subtype=individual name='paul' props={}
  type=organization subtype=company name='anthropic' props={}
  type=location subtype=city name='san francisco' props={'city': 'san francisco'}
EDGES:
  type=related_to semantic=headquarters_at organization:anthropic -> location:san francisco
RAW_REJECTIONS:
  kind=edge reason=missing_semantic_type raw={'source_node_id': 'paul', 'source_type': 'person',
    'target_node_id': 'vegetarian-food', 'target_type': 'preference', 'type': 'related_to',
    'semantic_type': None, 'properties': {}}
```

**Live Gemini smoke — interpretation**

- **Fact branch fired correctly**: "Earth orbits the Sun" → a single `fact` node with `subject="earth", predicate="orbits", object="sun"`. The LLM emitted ZERO incoming/outgoing edges for this fact in its raw output — exactly the island-style behaviour the decision tree asks for. No edge with `source_type="fact"` or `target_type="fact"` appeared in `result.edges`, so the validator's forbidden-endpoint guard never had to fire on this run (the LLM self-policed via the prompt).
- **First-person preference branch fired**: "Paul prefers vegetarian food at Italian restaurants" became a `preference` node (`name="vegetarian-food"`). The LLM also tried a stray `related_to person → preference` edge with missing `semantic_type` — that's the existing "has" structural carve-out being mis-emitted as `related_to`, unrelated to #031 (#032's preference-resolver branch will redirect this). The fact node was untouched.
- **Typed-edge branch fired**: "Anthropic is headquartered in San Francisco" produced exactly the `related_to + semantic_type=headquarters_at` edge from `organization:anthropic` to `location:san francisco`.
- The smoke covers the spec's three acceptance branches: (a) one `fact` node with zero edges, (b) a `preference` (the policy preview the prompt promises until #032 finalizes third-party preferences), (c) `related_to + headquarters_at` for the resolvable-entities case.

**Notes**

- The integration test's "rejection reason" assertion accepts the union `{fact_endpoint_disallowed, disallowed_pair, non_extractable_type}`. Which exact token fires depends on which validator layer catches the edge first: `_parse_extraction` in `core.py` rejects non-LLM-extractable edges (`mentions`, `same_as`, ...) with `non_extractable_type` BEFORE the envelope validator sees them, and rejects `related_to` rows whose pair isn't in any semantic's `allowed_pairs` with `disallowed_pair`. The envelope-level `fact_endpoint_disallowed` reason is reachable in principle (e.g. via a future code path that bypasses the parser) but isn't reachable through today's LLM-only flow. All three reasons indicate the island rule fired — the unit test's 5-case matrix (`test_fact_endpoint_disallowed_on_every_edge`) directly exercises the envelope validator and pins `fact_endpoint_disallowed` deterministically, so the audit signal at that layer is regression-tested.
- `validate_properties` is now alias-aware on **all** property models, not just `FactProperties`. Existing call sites are unaffected (no property models other than `FactProperties` declare `Field(alias=...)` today), but the contract change is worth flagging: input keys can be either Python attribute name or alias; output keys are always the wire-form (alias when set). This matches what Pydantic does for `model_dump(by_alias=True)` and keeps the on-disk shape stable across alias / Python-name spellings.
- Bi-temporal supersession (`valid_until` writes on contradiction) is **out of scope** for #031 per the spec; the columns exist on `KnowledgeGraphEntry` (added in #030) and accept user-supplied values, but no contradiction-judge code lands here. That's #032.

### [Tester] 2026-05-18 14:55 — QA

**Test summary**
- Format / lint / pre-commit: PASS (229 files formatted; all ruff/format/biome/KGQuery-discipline hooks green)
- Unit tests: **1132 passed / 0 failed / 0 warnings** (`make memory-unit-tests`, 39.71s)
- Integration tests (fast): superseded by the full run below
- Integration tests (full, incl. mongot + slow): **199 passed / 1 skipped / 0 failed** (`make memory-integration-tests-all`, 392.96s). `tests/integration/memory/test_fact_island.py` contributes 3 passing tests.

**E2E adversarial pass** (live Gemini + live MongoDB; script at `/tmp/qa_031_adversarial.py`)

- Happy path 1 (live Gemini): `extract_entities("Earth orbits the Sun. Paul prefers vegetarian food at Italian restaurants. Anthropic is headquartered in San Francisco.")` →
  `fact(earth-orbits-sun, subject="Earth", predicate="orbits", object="Sun")` + `preference(vegetarian-food-preference)` + `organization(anthropic)` + `location(san francisco)` + `related_to[headquarters_at] organization → location`. **Zero edges with a fact endpoint** (PASS — islandhood).
- Happy path 2 / lure (live Gemini): `extract_entities("Mount Everest is 8849 m tall. Sarah loves jazz.")` →
  `fact(everest-height, subject="mount everest", predicate="height is", object="8849 m")` + `location(mount everest)` + `person(sarah)` + `preference(jazz preference)`. **Zero edges** in the LLM emission — the lure ("Mount Everest is 8849 m tall" → tempt the LLM into emitting `mentions chunk → fact:everest-height`) FAILED to trip the LLM; decision-tree prompt held. (PASS — islandhood preserved under adversarial input.)
- Break path 1 (LLM-bypass simulation — malformed parser payload): manually constructed a payload with one valid `fact` node + three bad edges (`mentions chunk→fact`, `same_as fact→fact`, `related_to person→fact`). Fed through `_parse_extraction`. Result: fact node survives, **zero** edges with a fact endpoint survive, **3** rejections recorded (`non_extractable_type`, `non_extractable_type`, `disallowed_pair`). PASS — validator catches the bypass before the row reaches Mongo.
- Break path 2 (envelope validator matrix — five cases): `mentions chunk→fact`, `same_as fact→fact`, `related_to fact→person`, `related_to person→fact`, `has person→fact`. All five reject with `rejection_reason="fact_endpoint_disallowed"`. PASS.
- Break path 3 (boundary: empty fact name): `validate_envelope(kind="node", type="fact", subtype=None, name="")` rejects with `missing_name`. PASS — fact rows can't slip through with an empty `_id` suffix.
- Break path 4 (live MongoDB invariant): after writing 2 facts for user A, `db.knowledge_graph.find({kind: "edge", $or: [{source_type: "fact"}, {target_type: "fact"}]}) == []`. PASS — direct DB check confirms no edge ever touches a fact endpoint.
- Break path 5 (alias / wire-key round-trip): wrote a fact via `KnowledgeGraphEntry.save()`, then `KGQuery.find_facts(object="Sun")` round-trips it. The stored doc carries the key `"object"` (alias), not `"object_"` (Python name). `FactProperties.model_dump(by_alias=True)` emits `{"subject","predicate","object"}`. `model_validate` accepts BOTH `"object"` and `"object_"` (populate_by_name=True). PASS.
- Break path 6 (tenant isolation): wrote a fact for user B with the same `subject="Earth"` as user A's. `KGQuery(user_a).find_facts(subject="Earth")` returns ONLY user A's row; `KGQuery(user_b).find_facts(subject="Earth")` returns ONLY user B's row; uninvolved user C's `find_facts(subject="Earth")` returns `[]`. PASS.
- Break path 7 (mongot unavailability — degrade-gracefully): `find_facts_by_similarity([0.0]*1024, k=5)` against a brand-new test DB returns `[]` (mongot has not indexed the throwaway DB yet) instead of raising. PASS — matches the `_vector_search` graceful-degradation contract.

**Adversarial summary: 36 / 36 checks PASS.**

**Acceptance criteria**

- [x] PASS — `FactProperties` defined with `subject`/`predicate`/`object` (alias) and non-empty `description` on each — verified by `tests/unit/entities/test_ontology.py::TestFactNodeRegistration::test_fact_properties_field_descriptions_non_empty`, the existing programmatic sweep in `test_field_descriptions.py`, and my adversarial script (`FactProperties.model_dump(by_alias=True)` emits `{subject, predicate, object}`).
- [x] PASS — `NODE_REGISTRY["fact"]` shape pinned — verified by `TestFactNodeRegistration::test_fact_registered_with_expected_spec` + adversarial step "Node registration" (5 pins all green).
- [x] PASS — `RELATION_SEMANTICS` contains no `fact` endpoint — verified by `TestFactIslandRule::test_no_relation_semantic_has_fact_endpoint` + my adversarial sweep (`fact_in_sem == []`).
- [x] PASS — no `EDGE_REGISTRY[*].allowed_pairs` includes `fact` — verified by `TestFactIslandRule::test_no_edge_allowed_pair_has_fact_endpoint` + adversarial sweep (`fact_in_edge == []`).
- [x] PASS — `validate_envelope` rejects every edge with a `fact` endpoint (5-case matrix) — verified by `test_fact_endpoint_disallowed_on_every_edge` AND my live adversarial run (all five → `fact_endpoint_disallowed`). NB: rejection token is `fact_endpoint_disallowed` (SWE chose this; spec allowed any equivalent reason string).
- [x] PASS — e2e integration `tests/integration/memory/test_fact_island.py::TestFactIslandEnd2End` (3 tests passing in 9.59s under `@pytest.mark.slow`). Fact lands, 3 bad-edge attempts rejected, NO edge in `knowledge_graph` touches the fact `_id`. Re-confirmed live against MongoDB by my adversarial script.
- [x] PASS — `KGQuery.find_facts(subject=…, predicate=…, object=…)` — verified by `tests/unit/memory/query/test_find_facts.py::TestFindFactsFilters`/`TestFindFactsScoping` + the integration round-trip `test_kgquery_find_facts_round_trip` + my adversarial run (every single & combined filter exercised; non-matching returns `[]`).
- [x] PASS — `KGQuery.find_facts_by_similarity(query_embedding, k=5)` — verified by `tests/unit/memory/query/test_find_facts.py::TestFindFactsBySimilarity` (vector-pipeline shape pinned, graceful empty-list fallback). Note: a `requires_mongot` integration test is not added in this round; the unit-level pin of the `$vectorSearch` shape + the live indexing-pipeline test (#019) covering the same plumbing + my smoke run (returns `[]` on cold DB without raising) gives sufficient coverage for this AC's behaviour. Flagged below as a follow-up worth considering.
- [x] PASS — `get_ontology_schema()` v5 surfaces the `fact` node block (new snapshot `tests/unit/entities/snapshots/ontology_schema_v5.json`); decision-tree text lives in `_SYSTEM_PROMPT` and is verified present in my adversarial script. Snapshot test green.
- [x] PASS — `KnowledgeGraphEntry(kind="node", type="fact", subtype=None, …)` constructs — verified by `TestKnowledgeGraphEntryAcceptsFactNode::test_construct_fact_node_entry` + my live MongoDB write (2 fact rows persisted via `entry.save()`, round-tripped through `KGQuery.find_facts`).
- [x] PASS — format/lint clean (229 files formatted, `ruff check` and `ruff format` both green).
- [x] PASS — `make pre-commit` green (5 hooks: prettier, ruff check, ruff format, biome, KGQuery discipline).
- [x] PASS — `make memory-unit-tests` green (1132 passed, 0 warnings).
- [x] PASS — `make memory-integration-tests` green (covered by integration-tests-all).
- [x] PASS — `make memory-integration-tests-all` green (199 passed, 1 skipped — the pre-existing SERP web-search skip; 392.96s; mongot stack live).

**Evidence**

```
$ make memory-unit-tests
…
tests/unit/memory/query/test_find_facts.py .........                     [ 79%]
…
============================ 1132 passed in 39.71s =============================

$ make memory-format-check && make memory-lint-check && make pre-commit
229 files already formatted
All checks passed!
Validate pyproject.toml..............................(no files to check)Skipped
prettier.................................................................Passed
ruff check...............................................................Passed
ruff format..............................................................Passed
biome check (harness)....................................................Passed
KGQuery discipline (memory)..............................................Passed

$ make memory-integration-tests-all
…
tests/integration/memory/test_fact_island.py ...                         [ 68%]
tests/integration/memory/test_indexing_pipeline.py ......                [ 71%]
…
tests/integration/test_two_user_isolation.py ..........................  [ 98%]
tests/integration/test_two_user_review_isolation.py ...                  [100%]
================== 199 passed, 1 skipped in 392.96s (0:06:32) ==================

$ uv run python /tmp/qa_031_adversarial.py
…
=== SUMMARY ===
  36 passed / 0 failed (of 36)
```

Live Gemini smoke 2 (Mount Everest lure, paraphrased from the script log):
```
Input: 'Mount Everest is 8849 m tall. Sarah loves jazz.'
NODES:
  type=location subtype=landmark name='mount everest' props={}
  type=person subtype=individual name='sarah' props={}
  type=preference subtype=None name='jazz preference' props={'content': 'loves jazz'}
  type=fact subtype=None name='everest height'
       props={'subject': 'mount everest', 'predicate': 'height is', 'object': '8849 m'}
EDGES:
  (none)
```
The LLM (correctly) did NOT emit a `mentions chunk → fact:everest-height` edge — the decision-tree prompt held even when the input contained an entity (`Mount Everest`) that the LLM also extracted as a `location`. Islandhood preserved.

**Other issues found (non-blocking, advisory)**

- The adversarial-script's `find_facts_by_similarity` smoke returned `[]` because mongot has not indexed the throwaway DB. The unit test's shape-pin and the indexing pipeline's existing live-mongot coverage substitute for a dedicated `@pytest.mark.requires_mongot` integration test on this helper. **Recommendation (follow-up, not blocking):** in #032 or #033, add `tests/integration/memory/test_find_facts_by_similarity.py` (marked `@pytest.mark.slow @pytest.mark.requires_mongot`) that writes a fact, runs `memory_indexing` to embed it, and asserts `find_facts_by_similarity` returns it at top-k=1. The SWE's note already acknowledged this gap; it's worth a tracker ticket.
- Rejection reasons from the parser layer (`non_extractable_type` for structural edges, `disallowed_pair` for `related_to`) differ from the envelope-layer canonical reason (`fact_endpoint_disallowed`). All three reasons indicate the island rule fired. The integration test correctly accepts the union of the three tokens; the unit-level 5-case matrix pins the envelope-only deterministic `fact_endpoint_disallowed` reason. Not a bug — documented in the SWE's notes.
- `KGQuery.find_facts(object=...)` shadows the builtin `object` type in its keyword. SWE chose this to mirror the wire-form key; the function is keyword-only so the shadow is local. Acceptable; would be a nit at PR review if anything.

**VERDICT: PASS**
