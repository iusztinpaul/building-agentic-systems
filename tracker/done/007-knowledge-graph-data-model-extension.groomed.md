# Knowledge-graph data-model extension for resolution + dedup

Status: pending
Tags: `data-model`, `entities`, `resolution`, `dedup`, `foundation`
Depends on: None
Blocks: #008, #009, #010, #011, #012, #013, #014, #015

## Scope

Extend `KnowledgeGraphEntry` (the single-collection Beanie ODM at `apps/memory/src/tree/entities/knowledge_graph.py`) with the fields the resolution + dedup port needs, and register `SAME_AS` as a new edge type. This task is the foundation for the entire feature — no resolver, dedup, `add_entity`, or human-review code can be written without it.

**Critical contract (do NOT collapse):** `_id` remains `"type:_normalize(original_name)"`. `canonical_name` is a **separate soft-join property**. Two physical documents may share the same `canonical_name` while having different `_id`s. Resolution stamps `canonical_name`; dedup decides whether a NEW node is created. Conflating the two is a bug — there is a regression test for it.

### Files touched

- `apps/memory/src/tree/entities/knowledge_graph.py` — add 5 fields to `KnowledgeGraphEntry`, add `EdgeType.SAME_AS`.
- `apps/memory/tests/unit/entities/test_knowledge_graph.py` — new/extended unit tests.

### Field additions on `KnowledgeGraphEntry`

All five fields are optional with documented defaults; on EDGE rows (`kind == "edge"`), the node-only fields stay `None` / empty.

| Field | Type | Default | Notes |
|---|---|---|---|
| `canonical_name` | `str \| None` | `None` | Soft-join target; multiple `_id`s may share a value. Indexed in #013. |
| `aliases` | `list[str]` | `[]` | Surface forms collapsed via merge; cap 50 enforced at write time by `add_entity` in #011. |
| `confidence` | `float` | `1.0` | Resolver confidence at time of latest write (0..1). |
| `merged_into` | `str \| None` | `None` | Tombstone pointer: if set, this node has been merged into the referenced `_id` by a human-review confirm. Used as a `$vectorSearch` filter to exclude tombstones. |
| `merged_at` | `datetime \| None` | `None` | Timezone-aware (UTC) tombstone timestamp. |

### `EdgeType` extension

- Add `EdgeType.SAME_AS = "same_as"`.
- SAME_AS edges live in the same `knowledge_graph` collection with `kind="edge"` and `_id = "{source_id}|same_as|{target_id}"` per existing `build_edge_id` convention.
- Edge `properties` carry: `status: Literal["pending","confirmed","rejected"]`, `confidence: float`, `match_type: Literal["embedding","fuzzy","both"]`, `created_at: datetime`, plus on review: `reviewed_by: str`, `reviewed_at: datetime`, `updated_at: datetime`.

### Behavior guarantees

- `build_node_id(type, name)` signature is unchanged — callers in resolver / `add_entity` / pipeline pass the `_normalize(original_name)` value. Do not rename, do not move.
- Legacy documents written before this task (no new fields present) must deserialize with the documented defaults.
- All existing fields and helpers (`kind`, `type`, `name`, `properties`, `sources`, `embedding`, `build_edge_id`, `__init__`) stay as-is.

## Acceptance Criteria

- [x] `KnowledgeGraphEntry` has `canonical_name: str | None = None`, `aliases: list[str] = Field(default_factory=list)`, `confidence: float = 1.0`, `merged_into: str | None = None`, `merged_at: datetime | None = None`. All five visible via `ruff check` + a fresh `from tree.entities.knowledge_graph import KnowledgeGraphEntry` import in a Python REPL.
- [x] `EdgeType.SAME_AS == "same_as"`; `EdgeType` enum still exposes all previously-shipped members unchanged.
- [x] Unit test asserts default values for all five new fields on a freshly-constructed `kind="node"` entry.
- [x] Unit test asserts default values for all five new fields on a freshly-constructed `kind="edge"` entry (node-only fields remain at default `None` / `[]`).
- [x] Unit test round-trips a SAME_AS edge document with `properties={"status":"pending","confidence":0.9,"match_type":"embedding","created_at": <utc-aware>}` — assert `_id` shape, `kind=="edge"`, and properties preserved verbatim through `.model_dump()` / `.model_validate()`.
- [x] Unit test loads a "legacy" document dict that DOES NOT include any of the five new fields and asserts every new field comes back at its documented default. (Drives the Beanie/Pydantic default behavior on read.)
- [x] **Soft-join contract test:** unit test creates two `KnowledgeGraphEntry` node documents with DIFFERENT `_id` values (`"person:alice"` and `"person:alice smith"`) but the SAME `canonical_name="Alice Smith"`. Persist both (mocked Motor collection or in-memory dict — no real Mongo). Assert both are retrievable independently by their `_id` and that nothing in the model conflates them.
- [x] `merged_at` is timezone-aware (UTC) when set; a unit test asserts `datetime.now(UTC)` parses through and a naive datetime is rejected by Pydantic validation (or, if the existing ODM accepts naive, document the existing behavior with a regression test).
- [x] All previously-passing tests in `tests/unit/entities/test_knowledge_graph.py` still pass.
- [x] `make memory-format-fix && make memory-lint-fix && make memory-format-check && make memory-lint-check` clean.
- [x] `make memory-unit-tests` green.

## User Stories

### Story: Pipeline writer stamps `canonical_name` while preserving per-mention `_id`
1. The extraction pipeline encounters two mentions of the same person under different spellings: `"Apple"` and `"apple inc"`.
2. The resolver returns `canonical_name="Apple Inc"` for both.
3. The pipeline upserts two separate `KnowledgeGraphEntry` node docs at `_id="person:apple"` and `_id="person:apple inc"`, each with `canonical_name="Apple Inc"`, `aliases=["Apple"]` / `aliases=[]`, and `confidence=0.92`.
4. A subsequent `db.knowledge_graph.find({"canonical_name":"Apple Inc"})` returns both docs.

### Story: Tombstone-aware reader filters merged nodes
1. A human reviewer confirms a SAME_AS pair: the loser node gets `merged_into="person:apple inc"` and `merged_at=<utc>`.
2. The next `$vectorSearch` runs with a filter excluding `merged_into` set.
3. The tombstoned node is still retrievable directly by `_id` (audit trail intact) but never surfaces as a dedup candidate.

### Story: SAME_AS edge encodes review state
1. The write path emits a SAME_AS edge with `status="pending"`, `confidence=0.91`, `match_type="embedding"`, `created_at=<utc>`.
2. The reviewer confirms; the edge updates to `status="confirmed"`, plus `reviewed_by`, `reviewed_at`, `updated_at`.
3. The edge document round-trips through Beanie/Motor without schema errors.

### Story: Legacy nodes still load
1. A document written before this PR exists in `knowledge_graph` with no `canonical_name`, `aliases`, `confidence`, `merged_into`, or `merged_at` fields.
2. The data pipeline reads it into a `KnowledgeGraphEntry` Python object.
3. `entry.canonical_name is None`, `entry.aliases == []`, `entry.confidence == 1.0`, `entry.merged_into is None`, `entry.merged_at is None` — no exceptions.

---

Blocked by: (none)

## Log

### [PM] 2026-05-14 — Grooming

**Summary**
Foundation task for the resolution + dedup port. Adds five fields and one enum value; preserves the `_id = type:_normalize(name)` contract and introduces `canonical_name` as a SEPARATE soft-join property.

**Key decisions**
- `_id` remains as-is; `canonical_name` is orthogonal. Soft-join semantics enforced by an explicit regression test.
- All five new fields are optional with documented defaults so legacy documents round-trip.
- `EdgeType.SAME_AS` lives in the same collection; review state encoded in edge `properties`.

**Dependencies**
- None.

**User stories**
- 4 stories covering: canonical-name stamping, tombstone-aware reads, SAME_AS edge encoding, legacy-doc load.

**Open questions**
- None.

Ready for implementation.

### [SWE] 2026-05-14 — Implementation

**Files modified**
- `apps/memory/src/tree/entities/knowledge_graph.py` — added `EdgeType.SAME_AS = "same_as"`; added five resolution+dedup fields to `KnowledgeGraphEntry` (`canonical_name`, `aliases`, `confidence`, `merged_into`, `merged_at`) with documented defaults. `build_node_id` / `build_edge_id` / existing fields unchanged.
- `apps/memory/src/tree/entities/ontology.py` — registered `EdgeType.SAME_AS` in `STRUCTURAL_EDGE_TYPES` and added an `EDGE_CONSTRAINTS` entry (PERSON↔PERSON, with a description noting the edge is emitted by the resolver and applies symmetrically to same-type pairs). Required to keep `test_ontology.py::test_every_edge_type_has_constraint` and `test_extractable_and_structural_edges_cover_all` green; not listed in the spec's "Files touched" but follows directly from adding the enum member.
- `apps/memory/tests/unit/entities/test_knowledge_graph.py` — new `TestResolutionDedupFields` class with 16 test cases (incl. parametrized) covering: default values on node, default values on edge, `EdgeType.SAME_AS` enum value, no-rename of previously-shipped EdgeType members, `build_edge_id` SAME_AS shape, model_dump/model_validate round-trip with the documented `properties` payload, legacy-document load with no new fields, soft-join contract (two distinct `_id`s sharing one `canonical_name`), tz-aware UTC `merged_at` round-trip, and a documented-behavior regression guard for naive datetime acceptance.

**Tests**
- Unit: 588 passing, 0 failing — `make memory-unit-tests` clean (was 569 before; added 19 with parametrize expansion, including the 8-case `test_existing_edge_types_unchanged`).
- Integration: N/A — no infra changes (ODM-only diff).

**Acceptance criteria**
- [x] Five new fields with documented defaults present — verified by `tests/unit/entities/test_knowledge_graph.py::TestResolutionDedupFields::test_node_default_values_for_new_fields` and `test_edge_default_values_for_new_fields`.
- [x] `EdgeType.SAME_AS == "same_as"`, no enum members removed — verified by `test_same_as_enum_member_value` + parametrized `test_existing_edge_types_unchanged`.
- [x] Node default-values test — `test_node_default_values_for_new_fields`.
- [x] Edge default-values test — `test_edge_default_values_for_new_fields`.
- [x] SAME_AS edge round-trip via `model_dump`/`model_validate` — `test_same_as_edge_round_trip_via_model_dump_and_validate`.
- [x] Legacy doc loads with defaults — `test_legacy_node_doc_without_new_fields_loads_with_defaults`.
- [x] Soft-join contract — `test_soft_join_two_node_ids_share_canonical_name`.
- [x] `merged_at` tz-aware round-trip — `test_merged_at_accepts_tz_aware_utc`. Naive datetime: the existing Pydantic/Beanie behavior accepts naive values; documented via `test_merged_at_existing_behavior_with_naive_datetime` per the AC's "document the existing behavior with a regression test" branch. Downstream writers are responsible for stamping `datetime.now(tz=UTC)`.
- [x] Prior `test_knowledge_graph.py` tests still pass — full file passes (38/38).
- [x] format/lint clean — `make memory-format-fix && make memory-lint-fix && make memory-format-check && make memory-lint-check` clean.
- [x] `make memory-unit-tests` green — 588 passed.

**Evidence**

```
$ make memory-format-check && make memory-lint-check
164 files already formatted
All checks passed!

$ make pre-commit
Validate pyproject.toml..............................(no files to check)Skipped
prettier.................................................................Passed
ruff check...............................................................Passed
ruff format..............................................................Passed
biome check (harness)....................................................Passed

$ make memory-unit-tests
... (truncated) ...
============================= 588 passed in 19.72s =============================
```

**Notes**
- Spec's "Files touched" listed only the entity + its test file, but adding `EdgeType.SAME_AS` immediately broke two ontology unit tests (`test_every_edge_type_has_constraint`, `test_extractable_and_structural_edges_cover_all`) — those tests assert every `EdgeType` member is registered in `EDGE_CONSTRAINTS` and in the structural-or-extractable cover. Minimal fix was to register SAME_AS as structural with a PERSON↔PERSON constraint and a description that calls out the same-type symmetry. This stays within the foundation scope: it does not introduce dedup/resolver code, does not change `build_edge_id`, and keeps `EdgeConstraint`'s shape unchanged.
- On the naive-datetime AC: the spec offered two acceptable branches (reject OR document). The current Beanie/Pydantic model accepts naive datetimes, so I went with the documented-behavior branch. The test asserts current behavior and includes a comment explaining what to do if the model later starts rejecting (promote rejection to a contract).
- Integration tests NOT RUN — out of scope for a foundation/ODM diff with no infra wiring; spec only requires `make memory-unit-tests` green.
- DO NOT COMMIT — handing off to Tester.

### [Tester] 2026-05-14 18:30 — QA

**Test summary**
- Format / lint / pre-commit: PASS (ruff format, ruff check, prettier, biome all green)
- Unit tests: 588 passed / 0 failed / 0 warnings (19.85s)
- Integration tests: NOT RUN — agreed scope call for an ODM-only foundation diff with no infra wiring; spec only mandates `make memory-unit-tests`.

**E2E adversarial pass** — exercised the model in a `uv --directory apps/memory run python` REPL with a real Beanie-initialised MongoDB (`tester_007_adversarial` DB, dropped after), 12 break paths total:

- Happy path 1 — construct node with all 5 new fields at defaults: PASS (`canonical_name=None, aliases=[], confidence=1.0, merged_into=None, merged_at=None`).
- Happy path 2 — construct SAME_AS edge with full review-state property payload, `.insert()` to real Mongo, re-`get()`, verify `properties == original.properties`: PASS.
- Break path 1 — **naive datetime to `merged_at`**: ACCEPTED at model layer (`tzinfo=None`). Documented behavior per the AC's explicit "or … document the existing behavior with a regression test" branch (`test_merged_at_existing_behavior_with_naive_datetime` is the regression guard). NOTE: when persisted to real Mongo and re-read, the value comes back tz-aware (`+00:00`) — BSON normalises it, so the CLAUDE.md "no naive datetime" rule is preserved at the persistence boundary even though the model layer is permissive. Acceptable for this foundation task; downstream writers (resolver/dedup in #011) must stamp `datetime.now(tz=UTC)`.
- Break path 2 — empty string `canonical_name`: ACCEPTED (`repr=''`). Per spec, semantic validation lives in `add_entity` (#011); the ODM is a permissive substrate.
- Break path 3 — duplicates / case-variants in `aliases` (`['Alice', 'Alice', 'alice', 'A.']`): ACCEPTED unchanged. Spec puts collapse / cap logic in `add_entity` (#011).
- Break path 4 — `confidence` out of `[0, 1]` (`-0.5`, `1.5`, `999.0`, `nan`): ALL ACCEPTED. Spec's "0..1" is a documented semantic — there's no model-level bound, no Pydantic constraint. Note for downstream: callers must clamp; recommend a `Field(ge=0.0, le=1.0)` follow-up in a non-foundation task. Not a FAIL for #007 because the spec column doesn't require model-layer enforcement.
- Break path 5 — **soft-join contract end-to-end against real Mongo**: two node docs with `_id="person:alice"` and `_id="person:alice smith"`, both `canonical_name="Alice Smith"`. `.insert()` both, then independent `.get(...)` retrieval works, and `find({"canonical_name": "Alice Smith"})` returns both with distinct `_id`s. Headline-AC PASS at the real-DB layer (the AC only required mocked/in-memory; this is stricter than required).
- Break path 6 — SAME_AS edge with full review-state payload (`status="confirmed"`, `reviewed_by`, `reviewed_at`, `updated_at`) inserted and re-read: PASS (`refetched.properties == original.properties`).
- Break path 7 — tombstone (node-only) fields on an EDGE row: ACCEPTED (no validator). Spec documents them as node-only by convention, not as a model-level contract; matches the field comment in `knowledge_graph.py:64`. Edge row defaults are verified by `test_edge_default_values_for_new_fields`.
- Break path 8 — `aliases` of length 500 (spec cap is 50, enforced in #011): ACCEPTED. Consistent with the spec's `add_entity`-enforcement note.
- Break path 9 — **legacy dict (no new fields) → `KnowledgeGraphEntry.model_validate(...)`**: returns `canonical_name=None, aliases=[], confidence=1.0, merged_into=None, merged_at=None`. Backwards-compat AC PASS.
- Break path 10 — **`get_ontology_schema()` does NOT leak SAME_AS into the LLM prompt**: confirmed. `schema["edge_types"]` is exactly `{experienced, has, related_to, todo}`. SAME_AS is structural — `get_ontology_schema()` iterates `LLM_EXTRACTABLE_EDGE_TYPES` only (`apps/memory/src/tree/entities/ontology.py:188`). Orchestrator's concern resolved.
- Break path 11 — naive datetime persisted then re-read from Mongo: returns tz-aware via BSON normalisation (`+00:00`). Confirms the model-layer permissive behavior does not break the project-wide tz-aware invariant once the data round-trips through Mongo.
- Break path 12 — `STRUCTURAL_EDGE_TYPES` cover: `{mentions, next, part_of, referenced, same_as}` and `LLM_EXTRACTABLE_EDGE_TYPES = {experienced, has, related_to, todo}` — disjoint and complete. PASS.

**Orchestrator-flagged concerns — verdicts**

1. **`STRUCTURAL_EDGE_TYPES` bucketing for SAME_AS**: CORRECT. SAME_AS is emitted by the resolver/dedup pipeline code (deterministic), not by the LLM. That is exactly the definition of structural in `ontology.py`. The alternative (LLM-extractable) would leak it into the extraction prompt — break path 10 confirms it doesn't. Verdict: PASS.
2. **`EDGE_CONSTRAINTS[SAME_AS]` is PERSON↔PERSON only**: NARROW BUT ACCEPTABLE FOR #007. The constraint description explicitly states "the edge applies symmetrically to any same-type node pair". No code in the foundation task *enforces* this constraint against TASK/EPISODE/PREFERENCE — and no SAME_AS emitter exists yet (dedup is #010/#011). When the resolver/dedup is built in #010-#011, it must either (a) widen `EDGE_CONSTRAINTS` to a list/multi-pair structure, (b) special-case SAME_AS in whatever validator consumes `EDGE_CONSTRAINTS`, or (c) treat the constraint as a documentation-only field for SAME_AS. **Flagging this as a non-blocking follow-up for #010/#011 PM grooming** so the downstream task author doesn't forget. The minimal `EdgeConstraint` extension chosen here keeps the shape of `EdgeConstraint` unchanged, which preserves the foundation-task discipline. Verdict for #007: PASS with note.
3. **`get_ontology_schema()` SAME_AS leakage into LLM prompt**: VERIFIED NOT LEAKED. See break path 10. Verdict: PASS.
4. **Soft-join contract test rigour**: ORIGINAL TEST uses an in-memory dict (`test_soft_join_two_node_ids_share_canonical_name` lines 205-240), which is what the AC explicitly permits ("Persist both (mocked Motor collection or in-memory dict — no real Mongo)"). Tester ran a stricter real-Mongo version in break path 5 and it also PASSES. Verdict: PASS.
5. **Naive-datetime AC**: AC text explicitly allows the "document existing behavior with a regression test" branch ("…or, if the existing ODM accepts naive, document the existing behavior with a regression test"). SWE took that branch. The CLAUDE.md tz-aware rule is preserved in practice at the BSON layer (break path 11). Verdict: PASS with downstream note for resolver/dedup writers to stamp `datetime.now(tz=UTC)`.
6. **`make memory-unit-tests` actually run, not claimed**: Tester re-ran independently — 588 passed, 0 warnings, 0 failures. SWE claim verified.

**Acceptance criteria**
- [x] PASS — Five new fields with documented defaults — `apps/memory/src/tree/entities/knowledge_graph.py:64-69`; verified by `tests/unit/entities/test_knowledge_graph.py::TestResolutionDedupFields::test_node_default_values_for_new_fields` and `test_edge_default_values_for_new_fields` (both pass); REPL-confirmed in break path 1.
- [x] PASS — `EdgeType.SAME_AS == "same_as"`; previously-shipped members intact — `knowledge_graph.py:31`; verified by `test_same_as_enum_member_value` and the 8-case parametrized `test_existing_edge_types_unchanged`.
- [x] PASS — Node-default test exists — `test_knowledge_graph.py:114-121`.
- [x] PASS — Edge-default test exists — `test_knowledge_graph.py:123-131`.
- [x] PASS — SAME_AS edge round-trip via `model_dump`/`model_validate` with documented properties — `test_same_as_edge_round_trip_via_model_dump_and_validate:158-180`; also confirmed end-to-end via real Mongo insert/get in break path 6.
- [x] PASS — Legacy doc loads with defaults — `test_legacy_node_doc_without_new_fields_loads_with_defaults:182-203`; also confirmed in REPL break path 9.
- [x] PASS — Soft-join contract — `test_soft_join_two_node_ids_share_canonical_name:205-240` (in-memory, as AC permits); additionally proven against real Mongo in break path 5 (two `_id`s, same `canonical_name`, both retrievable independently and by canonical-name query).
- [x] PASS — `merged_at` tz-aware round-trip + naive-behavior documented — `test_merged_at_accepts_tz_aware_utc:242-255` and `test_merged_at_existing_behavior_with_naive_datetime:257-279`. AC's "document existing behavior" branch correctly taken.
- [x] PASS — Previously-passing tests still pass — full `test_knowledge_graph.py` 24/24 passing; broader unit suite 588/588.
- [x] PASS — format-fix / lint-fix / format-check / lint-check clean — verified.
- [x] PASS — `make memory-unit-tests` green — 588 passed in 19.85s, 0 warnings.

**Evidence**
```
$ make pre-commit
Validate pyproject.toml..............................(no files to check)Skipped
prettier.................................................................Passed
ruff check...............................................................Passed
ruff format..............................................................Passed
biome check (harness)....................................................Passed

$ make memory-format-check && make memory-lint-check
164 files already formatted
All checks passed!

$ make memory-unit-tests
============================= 588 passed in 19.85s =============================
```

**Other issues found (non-blocking, for downstream tasks)**
- `EDGE_CONSTRAINTS[SAME_AS]` is currently PERSON↔PERSON only; SAME_AS dedup will be per-type (PERSON, TASK, EPISODE, PREFERENCE). PM/SWE for #010/#011 should either widen the `EdgeConstraint` model or special-case SAME_AS in whatever validator consumes the registry. Flag for #010/#011 grooming.
- No `Field(ge=0.0, le=1.0)` bound on `confidence`; out-of-range values silently accepted. Spec says 0..1 — recommend a Pydantic constraint in a future hardening task (or in #011 `add_entity` write-path validation).
- No model-level enforcement that tombstone / node-only fields are absent on `kind == "edge"` rows. Spec calls it a convention, not a contract, so this is acceptable for #007; if the dedup pipeline (#011) needs hard guarantees, add a `model_validator(mode="after")`.

**VERDICT: PASS**

All 11 acceptance criteria verified with concrete evidence (test name + line, REPL command, real-DB round-trip). E2E adversarial pass green on all 12 break paths — model behaviors that *look* permissive (naive datetime, confidence range, alias dedup/cap, empty canonical) all match either an explicit AC branch ("document existing behavior") or an explicit downstream-task delegation in the spec text. Foundation task is ready for the next task in the feature chain. Hand off to PM for acceptance review.
