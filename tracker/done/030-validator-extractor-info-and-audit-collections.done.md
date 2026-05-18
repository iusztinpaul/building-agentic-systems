# Field-level lenient validator + `ExtractorInfo` + audit collections (Phase 3, part 4 of 4)

Status: pending
Tags: `phase-3`, `validation`, `provenance`, `audit`, `extraction`
Depends on: #027, #028, #029
Blocks: #031, #032, #033

## Scope

Land the **two-tier validation policy** described at `plan.md:308–349`: envelope-level **strict** (drop the whole row on unmatched type / disallowed pair / missing name / unknown semantic) and field-level **lenient** (drop only invalid fields; keep the row even if every field fails). Land `ExtractorInfo` provenance metadata as a column on `KnowledgeGraphEntry`. Land the two audit collections `extraction_rejections` and `extraction_dropped_fields` that surface schema drift as structured signal. Land the four remaining "common fields" from `plan.md:189`: `description`, `valid_from`, `valid_until`, and (the `extractor` field itself). Tighten the strict "every POLE+O LLM-extractable node must have a `subtype`" rule introduced loosely in #028 — it becomes an **envelope-level** check inside the validator. Sweep every existing `*Properties` model to enforce `Field(description="…")` on every attribute per `plan.md:380–401`. **This is the validator-and-provenance task.**

### Files touched

- `apps/memory/src/tree/memory/extraction/validation.py` — NEW. `validate_properties(raw, schema, extras=None) -> tuple[dict[str, Any], list[ValidationError]]` per `plan.md:354–376`. `validate_envelope(row) -> tuple[bool, str | None]` for the envelope-level checks (returns `(ok, rejection_reason)`).
- `apps/memory/src/tree/entities/knowledge_graph.py` — add columns `description: str | None`, `valid_from: datetime | None`, `valid_until: datetime | None`, `extractor: ExtractorInfo | None`. `ExtractorInfo` is a Pydantic `BaseModel` (NOT a Beanie Document — it's an embedded field). All datetimes tz-aware UTC.
- `apps/memory/src/tree/entities/extraction_audit.py` — NEW. Two Beanie Documents: `ExtractionRejection` and `ExtractionDroppedField`. Per-row provenance: `user_id`, `chunk_id`, `timestamp`, plus per-collection specifics (see "Audit-collection schema" below).
- `apps/memory/src/tree/db.py` — register the two new Beanie Documents in the init list.
- `apps/memory/src/tree/memory/extraction/pipeline.py` — wire `validate_properties` and `validate_envelope` between the LLM extract task and the resolve task. Populate `ExtractorInfo` on every emitted row from the extraction pipeline (Document and Chunk rows skip `extractor` per `plan.md:210`).
- `apps/memory/src/tree/memory/extraction/core.py` — invoke envelope + field validation; on envelope reject, write to `ExtractionRejection`; on field reject, write batched to `ExtractionDroppedField`.
- `apps/memory/src/tree/entities/ontology.py` — sweep every `*Properties` model (node + edge + structural) and add `Field(description="…")` to any field that's missing one. Includes the POLE+O canonical types from #028 and the per-semantic edge property models from #029.
- `apps/memory/tests/unit/memory/extraction/test_validation.py` — NEW. Comprehensive test matrix.
- `apps/memory/tests/unit/entities/test_extraction_audit.py` — NEW. Schema round-trip.
- `apps/memory/tests/unit/entities/test_knowledge_graph.py` — extend for new columns.
- `apps/memory/tests/integration/test_validator_e2e.py` — NEW. End-to-end pipeline run with a mocked LLM emitting a mix of valid and invalid rows; assert what lands in `knowledge_graph` vs. what lands in the audit collections.

### Validator shape (per `plan.md:354–376`)

```python
# apps/memory/src/tree/memory/extraction/validation.py

from pydantic import BaseModel, TypeAdapter, ValidationError

def validate_properties(
    raw: dict[str, Any],
    schema: type[BaseModel],
    extras: type[BaseModel] | None = None,
) -> tuple[dict[str, Any], list[FieldDrop]]:
    """Per-field validation: keep valid, drop invalid, never raise.

    Combines the parent's properties_schema fields with optional subtype
    extras. Unknown fields are dropped (recorded in the returned list).
    """
    validated: dict[str, Any] = {}
    drops: list[FieldDrop] = []
    combined_fields = {**schema.model_fields, **(extras.model_fields if extras else {})}
    for key, value in raw.items():
        if key not in combined_fields:
            drops.append(FieldDrop(field=key, value=value, reason="unknown_field"))
            continue
        field = combined_fields[key]
        try:
            adapter = TypeAdapter(field.annotation)
            validated[key] = adapter.validate_python(value)
        except ValidationError as e:
            drops.append(FieldDrop(field=key, value=value, reason=str(e)))
    return validated, drops


@dataclass(frozen=True)
class FieldDrop:
    field: str
    value: Any            # raw value the LLM emitted; PII-aware truncation at write time
    reason: str           # short structured reason; for audit


def validate_envelope(
    *,
    kind: str,                                      # "node" | "edge"
    type: str,                                      # node type or edge type name
    subtype: str | None,
    name: str | None,
    source_type: str | None,
    target_type: str | None,
    semantic_type: str | None,
) -> tuple[bool, str | None]:
    """Envelope-level strict validation. Returns (ok, reason_if_not_ok)."""
    # Implementation per the bullet list at plan.md:316-324:
    #   - type is registered (NODE_REGISTRY or EDGE_REGISTRY by kind)
    #   - if node and LLM-extractable: name non-empty
    #   - if node and parent.subtypes is a closed set: subtype required and a member
    #   - if edge type=="related_to": semantic_type registered AND
    #     (source_type, target_type) pair allowed by the semantic's allowed_pairs
    #   - if edge type!="related_to": semantic_type is None
    #   - if any endpoint type is "fact": REJECT (island enforcement preview;
    #     fact lands in #031, but encoding the carve-out here is safe)
```

### `ExtractorInfo` shape (per `plan.md:201–210`)

```python
# apps/memory/src/tree/entities/knowledge_graph.py (or a sibling module)

class ExtractorInfo(BaseModel):
    name: str = Field(description="Extractor identifier (e.g. 'gemini-2.5-pro').")
    version: str = Field(description="Model version or pipeline release tag.")
    extraction_time_ms: int | None = Field(
        default=None,
        description="Optional perf metric: total time the LLM call took for this row.",
    )
```

The extraction pipeline reads `settings.gemini_model` (or whatever the active LLM identifier is) and a pipeline-release tag (e.g. from `pyproject.toml` version OR from a git sha at deploy time). For now: `version` = `tree-memory-{__version__}` from `pyproject.toml`. `extraction_time_ms` is opt-in; pipeline measures and populates per-row if cheap. Document and Chunk rows have `extractor=None`.

### Audit-collection schema

```python
# apps/memory/src/tree/entities/extraction_audit.py

class ExtractionRejection(BeanieDocument):
    """Whole-row rejections from the envelope validator."""
    user_id: PydanticObjectId
    chunk_id: PydanticObjectId | None = None       # the chunk whose extraction surfaced this row
    timestamp: datetime                            # tz-aware UTC
    rejected_at_stage: str                          # "envelope" | "field" | ... (mostly "envelope")
    rejection_reason: str                           # short structured: "unknown_type" / "disallowed_pair" / "missing_name" / ...
    raw_row: dict[str, Any]                         # what the LLM emitted (truncated to 4KB)
    extractor: ExtractorInfo | None = None

    class Settings:
        name = "extraction_rejections"
        indexes = [
            IndexModel([("user_id", 1), ("timestamp", -1)], name="user_timestamp_desc"),
            IndexModel([("user_id", 1), ("rejection_reason", 1)], name="user_reason"),
        ]


class ExtractionDroppedField(BeanieDocument):
    """Per-field drops from the lenient field-level validator."""
    user_id: PydanticObjectId
    chunk_id: PydanticObjectId | None = None
    timestamp: datetime
    row_type: str                                   # e.g. "person", "related_to"
    row_subtype: str | None = None
    semantic_type: str | None = None
    dropped_field: str
    raw_value: Any                                  # truncated to 1KB
    reason: str
    extractor: ExtractorInfo | None = None

    class Settings:
        name = "extraction_dropped_fields"
        indexes = [
            IndexModel([("user_id", 1), ("row_type", 1), ("dropped_field", 1)], name="user_type_field"),
            IndexModel([("user_id", 1), ("timestamp", -1)], name="user_timestamp_desc"),
        ]
```

Both collections are **wiped by the #033 migration** (no production data; dev seeded). The `user_type_field` compound on `ExtractionDroppedField` is the key query: "for user X, which fields on `person` have been dropped repeatedly?" → drives prompt iteration.

### Field-description sweep (per `plan.md:380–401`)

Every Pydantic field on every `*Properties` model in `tree.entities.ontology` and `tree.entities.ontology_tree_extensions` MUST carry `Field(description="…")` after this task. Audit method: a unit test introspects `model_fields` for every model registered in `NODE_REGISTRY`, `EDGE_REGISTRY`, `RELATION_SEMANTICS`, and `SUBTYPE_EXTRAS`; asserts each `FieldInfo.description` is a non-empty string. Description style: action-oriented, ≤15 words, examples for ambiguous fields. Existing models with present descriptions (`DocumentProperties`, `ChunkProperties`, parts of `PersonProperties`) are left alone unless they fail the introspection check.

### Pipeline integration

The extraction pipeline at `apps/memory/src/tree/memory/extraction/pipeline.py` today has 6 Prefect tasks (per Phase-1 #012): chunk + structural → LLM extract → resolve → embed → dedupe → write. Per `plan.md:378`, the new validator lives **between Task 2 (LLM extract) and Task 3 (resolve)**:

```
1. chunk + structural
2. llm_extract            → raw LLM JSON output
2.5 envelope+field validate (NEW)  → (validated_rows, rejections, field_drops)
3. resolve                → on validated_rows only
4. embed
5. dedupe
6. apply_writes           → also writes rejections + field_drops in batches
```

`apply_writes` writes the rejection / dropped-field rows in **the same Mongo session** as the validated `knowledge_graph` rows when possible, so a pipeline crash mid-write doesn't leak audit-only data.

### Tightening pass — strict subtype on POLE+O LLM-extractable

Per #028's "Tightening pass" note: the strict rule **"every POLE+O LLM-extractable node MUST have a `subtype`"** lands here as an envelope-level check. `validate_envelope` rejects a `kind="node"` row where `NODE_REGISTRY[type].llm_extractable is True` AND `NODE_REGISTRY[type].subtypes is not None` AND `subtype is None`. The seed `person:self` node is unaffected (it has `subtype="individual"`).

## Acceptance Criteria

- [x] `tree.memory.extraction.validation.validate_properties` exists with the exact signature in the Scope section. Unit tests cover:
  - All fields valid → returns `(raw, [])`.
  - One unknown field → returns `({valid_fields}, [FieldDrop(field=<unknown>, reason="unknown_field")])`.
  - One invalid-typed field (e.g. `email=12345`) → returns `({valid_fields}, [FieldDrop(field="email", reason=<pydantic-msg>)])`.
  - All fields invalid → returns `({}, [...])` and **does not raise**.
  - Subtype `extras` schema is honored when passed.
- [x] `tree.memory.extraction.validation.validate_envelope` exists. Unit tests cover (at minimum) seven branches:
  - Unknown node type → reject.
  - Unknown edge type → reject.
  - `related_to` with unknown `semantic_type` → reject.
  - `related_to` with disallowed `(source_type, target_type)` pair → reject.
  - Non-`related_to` edge with `semantic_type` set → reject.
  - LLM-extractable node with closed subtype set and `subtype=None` → reject.
  - Any edge with a `fact` endpoint → reject (island-enforcement pre-encoding; consistent with #031).
  - Plus at least three accept paths.
- [x] `KnowledgeGraphEntry.description: str | None = None`, `valid_from: datetime | None = None`, `valid_until: datetime | None = None`, `extractor: ExtractorInfo | None = None`. Unit tests:
  - Naive `valid_from` / `valid_until` rejected by Pydantic (per `CLAUDE.md` tz-aware policy).
  - Round-trip with tz-aware UTC values.
  - `ExtractorInfo` embeds and unembeds cleanly.
  - Legacy row (none of the four fields present in the BSON document) loads with all four at default `None`.
- [x] `ExtractorInfo` Pydantic model with the three fields and `Field(description=...)` on each. Unit test.
- [x] `ExtractionRejection` and `ExtractionDroppedField` Beanie Documents exist; registered in `tree.db.init_db()`. Integration test: write one of each, query back by `(user_id, timestamp)`. The two collections have the indexes listed in the Scope section — verified by `db.<coll>.indexes` introspection.
- [x] Extraction pipeline writes to `extraction_rejections` when the envelope validator returns `(False, reason)`. Integration test: mock a Gemini call to return a row with `type="dragon"` (unknown type); run the pipeline; assert one `ExtractionRejection` row lands with `rejection_reason="unknown_type"`.
- [x] Extraction pipeline writes to `extraction_dropped_fields` when field validation drops fields. Integration test: mock a Gemini call to return a `person` row with `email=12345` (int instead of str); run the pipeline; assert one `ExtractionDroppedField` row lands with `dropped_field="email"`, AND a `KnowledgeGraphEntry` for the person lands (the row is NOT dropped — lenient policy).
- [x] Every row written by the extraction pipeline carries `extractor` populated (verified by a count check: extracted-row count == count of rows with non-null `extractor`). Document and Chunk rows skip `extractor` (verified: count of rows with `kind in {document, chunk}` AND `extractor is null` == total document+chunk count).
- [x] Field-description sweep: unit test introspects `model_fields` for every Pydantic model in `NODE_REGISTRY[*].properties_schema`, `EDGE_REGISTRY[*].properties_schema`, `RELATION_SEMANTICS[*].properties_schema`, and `SUBTYPE_EXTRAS[*]`. For every field, asserts `FieldInfo.description is not None and len(description) > 0`. Test fails if a single field is missing a description.
- [x] `get_ontology_schema()` output snapshot updated → `tests/unit/entities/snapshots/ontology_schema_v4.json` (replaces v3). The schema now includes the four new common fields under the prompt instructions.
- [x] `make memory-format-fix && make memory-lint-fix && make memory-format-check && make memory-lint-check` clean.
- [x] `make pre-commit` green.
- [x] `make memory-unit-tests` green.
- [x] `make memory-integration-tests` green (fast loop).
- [x] `make memory-integration-tests-all` green (full incl. mongot).

## User Stories

### Story: The LLM emits a perfect row
1. LLM emits `{"type": "person", "subtype": "individual", "name": "alice", "properties": {"email": "alice@example.com", "occupation": "engineer"}}`.
2. Envelope validation: `person` is registered, `individual` is in `person.subtypes`, `name` non-empty → pass.
3. Field validation: both fields valid → returns the dict unchanged, no drops.
4. Row lands as `KnowledgeGraphEntry(kind="node", type="person", subtype="individual", name="alice", properties={...}, extractor=ExtractorInfo(name="gemini-2.5-pro", version="tree-memory-0.1.0", extraction_time_ms=812), ...)`.
5. Zero rows written to `extraction_rejections` or `extraction_dropped_fields`.

### Story: The LLM emits a partially-bad row — the row survives, the bad field is dropped
1. LLM emits `{"type": "person", "subtype": "individual", "name": "alice", "properties": {"email": 12345, "occupation": "engineer"}}` (email is an int).
2. Envelope validation: pass.
3. Field validation: `occupation` valid, `email` invalid (int not str) → returns `({"occupation": "engineer"}, [FieldDrop(field="email", value=12345, reason="...")])`.
4. Row lands as `KnowledgeGraphEntry(properties={"occupation": "engineer"}, ...)`. The bad email is **not** stored on the row.
5. One `ExtractionDroppedField(user_id=..., row_type="person", dropped_field="email", raw_value=12345, reason="...")` written.
6. A user running `db.extraction_dropped_fields.aggregate([{$match: {user_id: <X>}}, {$group: {_id: "$dropped_field", count: {$sum: 1}}}])` sees that `email` drops are high → signal that the prompt needs work.

### Story: The LLM emits a structurally-malformed row — the whole row is dropped
1. LLM emits `{"type": "dragon", "name": "smaug", "properties": {"breath": "fire"}}`.
2. Envelope validation: `dragon` not in `NODE_REGISTRY` → `(False, "unknown_type")`.
3. No `KnowledgeGraphEntry` lands.
4. One `ExtractionRejection(user_id=..., rejected_at_stage="envelope", rejection_reason="unknown_type", raw_row={"type": "dragon", ...}, extractor=...)` written.

### Story: The LLM emits a `related_to` edge with the wrong direction
1. LLM emits `{"type": "related_to", "semantic_type": "employed_by", "source": {"type": "organization", "name": "anthropic"}, "target": {"type": "person", "name": "paul"}}`.
2. Envelope validation: `employed_by ∈ RELATION_SEMANTICS`, but `(organization, person)` not in its `allowed_pairs` → reject.
3. No edge lands; `ExtractionRejection(rejection_reason="disallowed_pair", raw_row=...)` written.

### Story: A POLE+O LLM-extractable node missing its subtype is dropped
1. LLM emits `{"type": "organization", "name": "anthropic", "properties": {"jurisdiction": "delaware"}}` — no `subtype`.
2. Envelope validation: `organization.llm_extractable is True` AND `organization.subtypes is not None` AND `subtype is None` → reject.
3. `ExtractionRejection(rejection_reason="missing_subtype", raw_row=...)` written.

### Story: A pre-fact row writes its provenance
1. The extraction pipeline runs the existing #029 `related_to` flow.
2. Every node and edge row written carries `extractor=ExtractorInfo(name="gemini-2.5-pro", version="tree-memory-0.1.0")`.
3. A query `db.knowledge_graph.find({"user_id": X, "extractor.name": "gemini-2.5-pro"})` returns every LLM-extracted row for the user.

## Out of scope for this task

- `fact` node — that's #031 (this task pre-encodes only the "edges with fact endpoints are rejected" rule).
- `superseded_by` edge — #032.
- Preference typed slots — #032.
- `DedupConfig` — #032.
- Migration / e2e — #033.
- A *quality* eval of the prompt change — `extraction_dropped_fields` gives the audit signal we need; a formal eval is a follow-up not blocking acceptance.

## Test plan

- **Unit:** `tests/unit/memory/extraction/test_validation.py` — exhaustive matrix for `validate_properties` and `validate_envelope`.
- **Unit:** `tests/unit/entities/test_extraction_audit.py` — round-trip both audit Documents; index introspection.
- **Unit:** `tests/unit/entities/test_knowledge_graph.py` — new-column round-trip; tz-aware enforcement.
- **Unit:** `tests/unit/entities/test_field_descriptions.py` (or extend `test_ontology.py`) — the description-sweep introspection check.
- **Unit:** `tests/unit/entities/snapshots/ontology_schema_v4.json` — new golden file.
- **Integration:** `tests/integration/test_validator_e2e.py` — end-to-end pipeline run with a mocked Gemini client emitting six rows (one good, one with bad field, one with unknown type, one with disallowed pair, one with missing subtype, one with extractor metadata) and assert the resulting state of `knowledge_graph` + `extraction_rejections` + `extraction_dropped_fields`. Marker: `@pytest.mark.slow` (real Prefect pipeline run); excluded from CI if `@pytest.mark.requires_mongot` (it likely isn't — extraction itself doesn't need mongot).
- **Integration:** Phase-1 two-user isolation test stays green.

---

Refs: `plan.md:308–349` (validator asymmetry), `plan.md:354–376` (validate_properties shape), `plan.md:380–401` (Field-description discipline), `plan.md:431–434` (audit collections), `plan.md:189` (common fields on KnowledgeGraphEntry), `plan.md:201–210` (ExtractorInfo).

## Log

### [SWE] 2026-05-18 13:50 — Implementation

**Files modified**
- `apps/memory/src/tree/memory/extraction/validation.py` — NEW. `validate_envelope`, `validate_properties`, `FieldDrop`, `EnvelopeResult`, plus `get_node_property_schemas` / `get_edge_property_schema` helpers.
- `apps/memory/src/tree/entities/extraction_audit.py` — NEW. `ExtractionRejection` + `ExtractionDroppedField` Beanie Documents, indexes, raw-payload truncation helpers.
- `apps/memory/src/tree/entities/knowledge_graph.py` — added `ExtractorInfo` Pydantic model and the four common columns (`description`, `valid_from`, `valid_until`, `extractor`) with `_require_tz_aware_temporal` field validator.
- `apps/memory/src/tree/db.py` — registered the two new ODMs in `ALL_DOCUMENT_MODELS`.
- `apps/memory/src/tree/memory/extraction/pipeline.py` — wired validator task ②.5 BEFORE the first-person resolver, made `_apply_writes` / `_upsert_edge` / `_dispatch_entity_write` plumb `ExtractorInfo`, populated extractor on every LLM-extracted node + every `related_to` edge row.
- `apps/memory/src/tree/memory/extraction/add_entity.py` — added `extractor` kwarg on `add_entity` + `_upsert_node` and stamped it on writes.
- `apps/memory/src/tree/memory/extraction/core.py` — `_parse_extraction` now carries dropped emissions out as `RawRejection` entries on `ExtractionResult` so audit signal isn't lost.
- `apps/memory/src/tree/memory/types.py` — added `RawRejection` model + `raw_rejections` list on `ExtractionResult` (merged across chunks).
- `apps/memory/src/tree/entities/ontology.py` — `get_ontology_schema()` now surfaces a `common_fields` section so the LLM knows about `description` / `valid_from` / `valid_until`.
- `apps/memory/tests/unit/entities/snapshots/ontology_schema_v4.json` — NEW golden file (supersedes v3).
- `apps/memory/tests/unit/entities/test_ontology.py` — snapshot path updated to v4.
- `apps/memory/tests/unit/memory/extraction/test_validation.py` — NEW. Exhaustive matrix for the validator: happy paths, every unknown/missing/disallowed branch, parametrized node table, edge pair tests, lenient field drops, schema lookup helpers.
- `apps/memory/tests/unit/entities/test_extraction_audit.py` — NEW. Round-trips for both ODMs + index introspection + truncation helpers.
- `apps/memory/tests/unit/entities/test_knowledge_graph.py` — extended for `ExtractorInfo` round-trip, the four new common-column defaults, naive-datetime rejection, legacy doc loading with defaults.
- `apps/memory/tests/unit/entities/test_field_descriptions.py` — NEW. Programmatic sweep over every model registered in `NODE_REGISTRY` / `EDGE_REGISTRY` / `RELATION_SEMANTICS` / `SUBTYPE_EXTRAS`, asserting every field has a non-empty description.
- `apps/memory/tests/integration/memory/test_validator_e2e.py` — NEW. 8 slow-marked Prefect e2e tests covering happy path, unknown type, disallowed pair, missing subtype, single-bad-field lenient survival, all-bad-fields lenient survival, extractor stamping, tenant isolation, plus 3 non-slow Beanie round-trip + index introspection tests.
- `apps/memory/tests/integration/memory/test_extraction_pipeline.py` — updated `_ALICE_TODO_RESPONSE` and other LLM fixtures to emit `subtype` on every closed-vocab POLE+O person/object node (strict subtype gate lands here).
- `apps/memory/tests/integration/test_two_user_isolation.py` — same fixture update so Phase-1 isolation tests keep passing.

**Tests**
- Unit: 1098 passing, 0 failing — `make memory-unit-tests` (+93 over the 1005 baseline; new validator + audit + field-description + extra knowledge_graph tests).
- Integration (fast loop): 142 passing, 1 skipped, 54 deselected — `make memory-integration-tests`.
- Integration (full incl. slow + mongot): 196 passing, 1 skipped — `make memory-integration-tests-all`.

**Acceptance criteria**
- [x] `validate_properties` exists with the documented signature; unit tests cover happy / unknown-field / invalid-type / all-invalid / extras-honored paths — `tests/unit/memory/extraction/test_validation.py::TestValidateProperties*`.
- [x] `validate_envelope` exists with seven-plus branches: unknown node type, unknown edge type, related_to-with-unknown-semantic, related_to-disallowed-pair, semantic-on-non-related_to, llm-extractable-missing-subtype, fact-endpoint, plus three accept paths — `tests/unit/memory/extraction/test_validation.py::TestEnvelope*` + parametrized matrix.
- [x] `KnowledgeGraphEntry.description / valid_from / valid_until / extractor` columns added; naive datetimes rejected; legacy rows load with defaults — `tests/unit/entities/test_knowledge_graph.py::TestKnowledgeGraphCommonColumns`.
- [x] `ExtractorInfo` Pydantic model with `Field(description=...)` on each attribute; round-trip pinned — `tests/unit/entities/test_knowledge_graph.py::TestExtractorInfo`.
- [x] Both audit ODMs exist + are registered in `tree.db.ALL_DOCUMENT_MODELS`; live round-trip + index introspection pinned — `tests/integration/memory/test_validator_e2e.py::TestAuditOdmLiveRoundTrip`.
- [x] Pipeline writes `extraction_rejections` on envelope drop — `tests/integration/memory/test_validator_e2e.py::test_unknown_type_drops_row_and_writes_rejection` + `test_disallowed_pair_drops_edge_and_writes_rejection` + `test_missing_subtype_drops_row`.
- [x] Pipeline writes `extraction_dropped_fields` on per-field drop, row survives — `tests/integration/memory/test_validator_e2e.py::test_invalid_field_dropped_row_kept` + `test_all_fields_invalid_row_still_lands`.
- [x] Every LLM-extracted row carries `extractor`; document / chunk rows skip it — `tests/integration/memory/test_validator_e2e.py::test_extractor_stamped_on_llm_rows_absent_on_structural`.
- [x] Field-description sweep is a parametrized test over every registered model — `tests/unit/entities/test_field_descriptions.py::test_every_property_model_field_has_description`.
- [x] `get_ontology_schema()` v4 snapshot pinned — `tests/unit/entities/snapshots/ontology_schema_v4.json`; v3 reference left on disk.
- [x] format-fix / lint-fix / format-check / lint-check clean.
- [x] `make pre-commit` green.
- [x] `make memory-unit-tests` green.
- [x] `make memory-integration-tests` green (fast loop).
- [x] `make memory-integration-tests-all` green (full incl. mongot).

**Evidence**

```
$ make memory-unit-tests
...
============================ 1098 passed in 42.39s =============================

$ make memory-integration-tests
...
========== 142 passed, 1 skipped, 54 deselected in 184.68s (0:03:04) ===========

$ make memory-integration-tests-all
...
tests/integration/memory/test_add_entity.py ...........                  [ 55%]
tests/integration/memory/test_dedup.py ..............                    [ 62%]
tests/integration/memory/test_extraction_pipeline.py .........           [ 67%]
tests/integration/memory/test_indexing_pipeline.py ......                [ 70%]
tests/integration/memory/test_review.py ..................               [ 79%]
tests/integration/memory/test_validator_e2e.py ...........               [ 85%]
tests/integration/test_two_user_isolation.py ..........................  [ 98%]
tests/integration/test_two_user_review_isolation.py ...                  [100%]
================== 196 passed, 1 skipped in 434.47s (0:07:14) ==================

$ make memory-format-check && make memory-lint-check
uv run ruff format --check src/ tests/ scripts/ deploy/
227 files already formatted
uv run ruff check src/ tests/ scripts/ deploy/
All checks passed!

$ make pre-commit
uv run --project apps/memory pre-commit run --all-files
Validate pyproject.toml..............................(no files to check)Skipped
prettier.................................................................Passed
ruff check...............................................................Passed
ruff format..............................................................Passed
biome check (harness)....................................................Passed
KGQuery discipline (memory)..............................................Passed
```

**Notes**
- Validator task ②.5 runs BEFORE the first-person resolver. Originally placed AFTER per the strict reading of `plan.md:378`, but the first-person resolver iterates `node.properties.get("aliases", [])` which blows up on bad payloads (e.g. `aliases: 5` int). Validating first keeps the resolver fed only with known-good Pydantic-validated property dicts and is semantically equivalent — the resolver never re-introduces envelope drift.
- `_parse_extraction` was extended to surface its own rejections (unknown types, invalid endpoints, edge-constraint violations) via a new `RawRejection` model carried on `ExtractionResult.raw_rejections`. The validator task drains that list into `extraction_rejections` before running its own envelope pass. This is the only place the audit signal isn't redundant with my own envelope check — without it, things like `type="dragon"` would be lost to `logger.warning` because `_parse_extraction` filters them before the row ever reaches `validate_envelope`.
- `ExtractorInfo.version` resolves from `importlib.metadata.version("tree-memory")` with a `"0.0.0+local"` fallback when the package isn't installed (test runners that exercise the module without `uv pip install -e .`).
- `person:self` rows (created by `User.after_insert`, not by the LLM) intentionally carry `extractor=None`. The pipeline-end check filters them out by `name != "self"` in the test assertion.
- The `common_fields` section added to `get_ontology_schema()` only documents `description` / `valid_from` / `valid_until`. `extractor` is **server-stamped** and the LLM is explicitly NOT asked to emit it.
- Existing LLM-emission test fixtures (`_ALICE_TODO_RESPONSE` etc.) were updated to emit `subtype` on every closed-vocab person/object row. This is required by the new strict envelope check (`missing_subtype` is a hard reject). Three test files touched.
- DO NOT COMMIT YET — Tester gate next.

### [Tester] 2026-05-18 14:10 — QA

**Test summary**
- Format / lint / pre-commit: PASS
- Unit tests: 1098 passed / 0 failed / 0 warnings (`make memory-unit-tests`, 45.97s)
- Integration tests (full incl. slow + mongot): 196 passed / 1 skipped / 0 failed / 0 warnings (`make memory-integration-tests-all`, 437.59s)
- New #030 surface area: 170 passed across `test_validation.py` + `test_extraction_audit.py` + `test_field_descriptions.py` + `test_knowledge_graph.py`.

**E2E adversarial pass** — drove `run_extraction_for_documents` against live MongoDB with a synthetic `FakeLLM` emitting a curated 5-node + 1-edge payload + dedupe patched out. The mocked LLM emission exercises every audit branch in one pipeline run:

- Happy path (`paul_happy` person, all valid props): `kg.find({user_id, type: "person", name: "paul_happy"})` → row landed with `extractor={'name': 'gemini-3.1-flash-lite', 'version': 'tree-memory-0.1.0'}` and `properties={'email': 'paul@example.com', 'occupation': 'engineer'}`. **PASS**
- Break path 1 (boundary: empty `properties={}` on org with missing subtype): `acme_no_subtype` → row dropped, ONE `extraction_rejections` row inserted with `rejection_reason='missing_subtype'`, raw_row preserved. **PASS**
- Break path 2 (malformed: `type='dragon'`): row dropped, ONE `extraction_rejections` with `rejection_reason='unknown_type'`. Surfaces through `_parse_extraction`'s `RawRejection` → validator drain → audit insert. **PASS**
- Break path 3 (disallowed pair: reversed `employed_by(organization, person)`): row dropped, ONE `extraction_rejections` with `rejection_reason='disallowed_pair'`, raw_row complete. **PASS**
- Break path 4 (lenient field drop: `alice_partial` with `garbage: 42` unknown key): row LANDS with `properties={'email': 'alice@example.com'}` (garbage stripped); ONE `extraction_dropped_fields` row with `dropped_field='garbage'`, `reason='unknown_field'`. **PASS**
- Break path 5 (all-fields-invalid lenient: `bob_allbad` with `email: 12345` int and `occupation: ['not','a','string']` list): row LANDS with `properties={}` (no surviving fields); TWO `extraction_dropped_fields` rows with `reason` carrying the compacted Pydantic message `<root>: Input should be a valid string`. **PASS**
- Break path 6 (naive datetime on `valid_from`): `KnowledgeGraphEntry(valid_from=datetime(2025,1,1))` raises `pydantic.ValidationError` with message naming `valid_from`. Same for `valid_until`. **PASS**
- Break path 7 (fact-endpoint envelope): `validate_envelope(kind='edge', type='related_to', semantic_type='employed_by', source_type='fact', target_type='organization')` → `EnvelopeResult(ok=False, reason='fact_endpoint_disallowed')`. **PASS**
- Break path 8 (missing name): `validate_envelope(kind='node', type='person', subtype='individual', name=None)` and `name='   '` both → `missing_name`. **PASS**
- Break path 9 (unknown subtype): `subtype='alien'` on `person` → `unknown_subtype`. **PASS**
- Break path 10 (semantic on non-related-to): `mentions` edge with `semantic_type='employed_by'` → `semantic_on_non_related_to`. **PASS**
- Break path 11 (unknown kind): `kind='ufo'` → `unknown_kind`. **PASS**
- Break path 12 (`validate_properties` pathological): `{'email': b'raw bytes', 'occupation': {'nested': 'dict'}}` → no exception, both fields dropped with structured Pydantic messages. **PASS**
- Break path 13 (`validate_properties` schema=None): every key → `unknown_field` drop; never raises. **PASS**
- Break path 14 (extras-only schema): `validate_properties({'priority':'high','other':1}, schema=None, extras=Extras)` → kept `priority`, dropped `other`. **PASS**
- Break path 15 (field-description discipline bites): in-process mutation that clobbers `PersonProperties.aliases.description = None` causes the discipline test loop to detect one missing description; restoring re-passes. **PASS**

Audit-row provenance check (key sanity, since the audit collections are the headline behavioral surface):
- Every rejection row carries non-null `extractor`, non-empty `raw_row`, `document_id` (`ObjectId`), `chunk_id` (`str` UUID), and `rejected_at_stage='envelope'`. **PASS**
- Every dropped-field row carries `chunk_id`, `document_id`, `extractor`, `row_type`, `dropped_field`, `raw_value`, `reason`. **PASS**

Provenance + structural-row exclusions on the live `knowledge_graph` collection for the probe user:
- 3 LLM-extracted person rows, all with non-null `extractor.name` and `extractor.version`. **PASS** (AC: "extracted-row count == count of rows with non-null extractor")
- 2 structural (document + chunk) rows, both with `extractor=None`. **PASS**
- 0 `person:self` rows in this probe (user freshly seeded; the User after_insert hook writes `person:self` with `extractor=None` per the SWE's architectural call).
- 0 `organization` rows landed (the only org emitted got rejected for missing subtype — correct).
- 0 `dragon` rows landed.

**Acceptance criteria**
- [x] PASS — `validate_properties` exists with documented signature; all 5 sub-bullet behaviors exercised. Evidence: `tests/unit/memory/extraction/test_validation.py::TestValidateProperties*` (50 cases) + adversarial break paths 4, 5, 12–14.
- [x] PASS — `validate_envelope` with 7+ branches and 3+ accept paths. Evidence: `tests/unit/memory/extraction/test_validation.py::TestEnvelope*` + adversarial break paths 1–3, 7–11.
- [x] PASS — `KnowledgeGraphEntry.description / valid_from / valid_until / extractor` columns; naive-datetime rejection; tz-aware round-trip; legacy-row default-None loading. Evidence: `tests/unit/entities/test_knowledge_graph.py::TestKnowledgeGraphCommonColumns` + adversarial probe 6.
- [x] PASS — `ExtractorInfo` model with 3 fields and `Field(description=...)` on each. Evidence: `apps/memory/src/tree/entities/knowledge_graph.py:88-110` + `tests/unit/entities/test_knowledge_graph.py::TestExtractorInfo`.
- [x] PASS — `ExtractionRejection` + `ExtractionDroppedField` Beanie Documents exist, registered in `tree.db.ALL_DOCUMENT_MODELS`, with the documented compound indexes. Evidence: `apps/memory/src/tree/db.py:5-17`, `tests/integration/memory/test_validator_e2e.py::TestAuditOdmLiveRoundTrip`.
- [x] PASS — Pipeline writes to `extraction_rejections` on envelope drop. Evidence: adversarial probes 1–3 wrote 3 rejection rows live; `tests/integration/memory/test_validator_e2e.py::test_unknown_type_drops_row_and_writes_rejection` + sibling tests.
- [x] PASS — Pipeline writes to `extraction_dropped_fields` on per-field drop; the lenient row is NOT dropped. Evidence: adversarial probes 4–5 wrote 3 drop rows + kept `alice_partial` and `bob_allbad`; integration tests `test_invalid_field_dropped_row_kept`, `test_all_fields_invalid_row_still_lands`.
- [x] PASS — Every LLM-extracted row carries `extractor`; document / chunk rows skip it. Evidence: adversarial probe direct counts (3 person → 3 with extractor; 2 doc/chunk → 0 with extractor) + `test_extractor_stamped_on_llm_rows_absent_on_structural`.
- [x] PASS — Field-description sweep test parametrizes over every registered model. Evidence: `tests/unit/entities/test_field_descriptions.py::test_every_property_model_field_has_description` + adversarial probe 15 confirms it bites when a description is removed.
- [x] PASS — `get_ontology_schema()` v4 snapshot pinned; includes a `common_fields` section with `description`, `valid_from`, `valid_until` (extractor intentionally omitted — server-stamped). Evidence: `apps/memory/tests/unit/entities/snapshots/ontology_schema_v4.json`.
- [x] PASS — `make memory-format-check` + `make memory-lint-check` clean.
- [x] PASS — `make pre-commit` green.
- [x] PASS — `make memory-unit-tests` green (1098 passing, 0 warnings).
- [x] PASS — `make memory-integration-tests` green — verified by the SWE's prior fast-loop run; the broader `-all` target (which is a strict superset) ran clean below.
- [x] PASS — `make memory-integration-tests-all` green: 196 passed, 1 skipped, 0 warnings, 437.59s.

**Evidence**

```
$ make memory-unit-tests
============================ 1098 passed in 45.97s =============================

$ make memory-integration-tests-all
tests/integration/memory/test_validator_e2e.py ...........               [ 85%]
tests/integration/test_two_user_isolation.py ..........................  [ 98%]
tests/integration/test_two_user_review_isolation.py ...                  [100%]
================== 196 passed, 1 skipped in 437.59s (0:07:17) ==================

$ make memory-format-check && make memory-lint-check && make pre-commit
... All checks passed!
... pre-commit: Validate pyproject.toml / prettier / ruff check / ruff format / biome check (harness) / KGQuery discipline (memory) — Passed
```

Adversarial probe live MongoDB output (synthetic FakeLLM, dedupe patched to no-op):

```
[adv030] summary=nodes_written=5 edges_written=4 nodes_merged=0 nodes_flagged=0 same_as_edges_emitted=0 documents_processed=1
PERSON rows (3):
  name='paul_happy' subtype='individual' props={'email': 'paul@example.com', 'occupation': 'engineer'} extractor={'name': 'gemini-3.1-flash-lite', 'version': 'tree-memory-0.1.0', 'extraction_time_ms': None}
  name='alice_partial' subtype='individual' props={'email': 'alice@example.com'} extractor={...}
  name='bob_allbad' subtype='individual' props={} extractor={...}
DOCUMENT+CHUNK rows (2): both extractor=None
extraction_rejections (3): reasons={'missing_subtype':1, 'disallowed_pair':1, 'unknown_type':1}
extraction_dropped_fields (3): one 'garbage'/unknown_field, two Pydantic-string drops on bob's email+occupation
VERDICTS: V1-V11 all pass
```

**Other issues found**
- Documentation drift (informational, not a blocker): the SWE hand-off mentioned a `forbids_edges: bool = False` field on `NodeTypeSpec` as a "placeholder for fact (#031)". The actual implementation uses a hard-coded `_FORBIDDEN_EDGE_ENDPOINT_TYPES = frozenset({"fact"})` constant in `validation.py`. Behaviorally equivalent (and consistent with the spec, which calls it a "carve-out" not a registry flag), but the hand-off note diverges from the code. Worth a one-line note in the next SWE handoff if #031 wants to formalize this into a registry field.
- Hand-off also said `chunk_id: PydanticObjectId | None` in the audit schema text; the actual field is `chunk_id: str | None` (chunk ids are UUID strings, not Mongo ObjectIds, since chunks aren't a Beanie collection — they live as nested rows in `knowledge_graph`). The implementation is correct; the spec doc just used the wrong type hint in the example. The audit rows do carry `chunk_id` populated with the per-chunk UUID, confirmed live.
- `ExtractorInfo.extraction_time_ms` is documented as "Optional perf metric: total time the LLM call took for this row" but the pipeline never actually populates it (`_make_extractor_info` builds a single `ExtractorInfo` per flow and reuses it for every row). All audit + KG rows show `extraction_time_ms=None`. Spec only says "extraction pipeline measures and populates per-row if cheap" — explicitly opt-in — so this is in-scope, but a future enhancement worth filing.

**VERDICT: PASS**

QA passed for #030. The two-tier validator behaves as specified end-to-end against the live stack, the audit collections capture both envelope drops and per-field drops with actionable provenance (chunk_id, document_id, raw_row, extractor), `ExtractorInfo` is correctly stamped on every LLM-extracted row and skipped on structural rows, the field-description discipline test bites under mutation, and the full local CI mirror is green with zero warnings. Hand off to PM for acceptance review.
