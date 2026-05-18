"""Unit tests for the #030 envelope + field validators.

The two functions are deliberately split:

* :func:`validate_envelope` is strict — failures drop the whole row.
* :func:`validate_properties` is lenient — invalid fields are dropped
  off the row, the row itself survives, and the function NEVER raises.

The matrix below covers every documented branch in
:doc:`tracker/030-validator-extractor-info-and-audit-collections.in-progress.md`.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel, Field

from tree.entities.ontology import (
    EmployedByProperties,
    HasTaskProperties,
    PersonProperties,
)
from tree.memory.extraction.validation import (
    FieldDrop,
    get_edge_property_schema,
    get_node_property_schemas,
    validate_envelope,
    validate_properties,
)


# ---------------------------------------------------------------------------
# validate_properties — lenient, never raises
# ---------------------------------------------------------------------------


class TestValidatePropertiesHappyPath:
    def test_all_valid_returns_input_with_no_drops(self) -> None:
        raw = {"email": "alice@example.com", "occupation": "engineer"}
        validated, drops = validate_properties(raw, PersonProperties)
        assert validated == raw
        assert drops == []

    def test_empty_dict_returns_empty(self) -> None:
        validated, drops = validate_properties({}, PersonProperties)
        assert validated == {}
        assert drops == []


class TestValidatePropertiesUnknownField:
    def test_unknown_field_dropped_with_reason(self) -> None:
        raw = {"email": "alice@example.com", "favourite_colour": "blue"}
        validated, drops = validate_properties(raw, PersonProperties)

        assert validated == {"email": "alice@example.com"}
        assert len(drops) == 1
        assert drops[0] == FieldDrop(
            field="favourite_colour", value="blue", reason="unknown_field"
        )

    def test_all_fields_unknown_returns_empty(self) -> None:
        validated, drops = validate_properties(
            {"a": 1, "b": 2, "c": 3}, PersonProperties
        )
        assert validated == {}
        assert {d.field for d in drops} == {"a", "b", "c"}
        assert all(d.reason == "unknown_field" for d in drops)


class TestValidatePropertiesTypeFailures:
    def test_int_for_str_field_dropped(self) -> None:
        # ``email`` is ``str | None`` — passing an int triggers a
        # Pydantic ValidationError that this validator must NOT raise.
        raw = {"email": 12345, "occupation": "engineer"}
        validated, drops = validate_properties(raw, PersonProperties)

        assert validated == {"occupation": "engineer"}
        assert len(drops) == 1
        assert drops[0].field == "email"
        assert drops[0].value == 12345
        # Reason carries the structured Pydantic message.
        assert drops[0].reason  # non-empty
        assert drops[0].reason != "unknown_field"

    def test_all_invalid_returns_empty_and_does_not_raise(self) -> None:
        class _StrictModel(BaseModel):
            x: int = Field(default=0)
            y: int = Field(default=0)

        # Both fields receive non-int / non-coercible strings.
        raw = {"x": "not-a-number", "y": [1, 2, 3]}
        validated, drops = validate_properties(raw, _StrictModel)

        assert validated == {}
        assert {d.field for d in drops} == {"x", "y"}
        # Lenient: NEVER raises.
        assert all(d.reason for d in drops)

    def test_list_of_strs_for_aliases_validates(self) -> None:
        raw = {"aliases": ["alice", "alice smith"], "email": "a@b.com"}
        validated, drops = validate_properties(raw, PersonProperties)
        assert validated == raw
        assert drops == []


class TestValidatePropertiesExtras:
    """The extras model layers additional fields on top of the parent
    (for closed-vocab subtypes with SUBTYPE_EXTRAS registered)."""

    def test_extras_fields_accepted(self) -> None:
        class _ParentProps(BaseModel):
            a: str | None = Field(default=None)

        class _Extras(BaseModel):
            b: int | None = Field(default=None)

        raw = {"a": "yes", "b": 7}
        validated, drops = validate_properties(raw, _ParentProps, extras=_Extras)
        assert validated == raw
        assert drops == []

    def test_extras_field_validates_independently(self) -> None:
        class _ParentProps(BaseModel):
            a: str | None = Field(default=None)

        class _Extras(BaseModel):
            b: int | None = Field(default=None)

        # ``b`` should be dropped — strings don't coerce to int here.
        raw = {"a": "yes", "b": "not-a-number"}
        validated, drops = validate_properties(raw, _ParentProps, extras=_Extras)
        assert validated == {"a": "yes"}
        assert len(drops) == 1
        assert drops[0].field == "b"

    def test_no_schema_no_extras_drops_everything(self) -> None:
        raw = {"a": 1, "b": "x"}
        validated, drops = validate_properties(raw, None, None)
        assert validated == {}
        assert {d.field for d in drops} == {"a", "b"}
        assert all(d.reason == "unknown_field" for d in drops)


# ---------------------------------------------------------------------------
# validate_envelope — strict
# ---------------------------------------------------------------------------


class TestEnvelopeNodeBranches:
    def test_unknown_node_type_rejected(self) -> None:
        result = validate_envelope(
            kind="node", type="dragon", subtype=None, name="smaug"
        )
        assert result.ok is False
        assert result.reason == "unknown_type"

    def test_missing_name_on_llm_extractable_rejected(self) -> None:
        result = validate_envelope(
            kind="node", type="person", subtype="individual", name=""
        )
        assert result.ok is False
        assert result.reason == "missing_name"

    def test_whitespace_only_name_rejected(self) -> None:
        result = validate_envelope(
            kind="node", type="person", subtype="individual", name="   "
        )
        assert result.ok is False
        assert result.reason == "missing_name"

    def test_missing_subtype_on_closed_vocab_rejected(self) -> None:
        # ``person`` has a closed subtype set and is LLM-extractable;
        # omitting subtype must drop the row.
        result = validate_envelope(
            kind="node", type="person", subtype=None, name="alice"
        )
        assert result.ok is False
        assert result.reason == "missing_subtype"

    def test_unknown_subtype_on_closed_vocab_rejected(self) -> None:
        result = validate_envelope(
            kind="node", type="person", subtype="dragon", name="alice"
        )
        assert result.ok is False
        assert result.reason == "unknown_subtype"

    def test_valid_person_individual_accepted(self) -> None:
        result = validate_envelope(
            kind="node", type="person", subtype="individual", name="alice"
        )
        assert result.ok is True
        assert result.reason is None

    def test_valid_object_task_tree_extension_accepted(self) -> None:
        result = validate_envelope(
            kind="node", type="object", subtype="task", name="ship demo"
        )
        assert result.ok is True

    def test_freeform_parent_accepts_none_subtype(self) -> None:
        # ``preference`` is freeform (subtypes=None); the strict
        # subtype check is skipped.
        result = validate_envelope(
            kind="node", type="preference", subtype=None, name="coffee"
        )
        assert result.ok is True

    def test_structural_node_with_no_name_accepted(self) -> None:
        # ``document`` is llm_extractable=False; the strict name check
        # only applies to LLM-extractable rows.
        result = validate_envelope(
            kind="node", type="document", subtype=None, name=None
        )
        assert result.ok is True

    def test_fact_node_endpoint_rejected_today(self) -> None:
        # ``fact`` lands in #031; encoding the carve-out as a node-level
        # rejection now keeps the rule consistent.
        result = validate_envelope(kind="node", type="fact", name="x")
        # ``fact`` isn't in NODE_REGISTRY yet so the registry check
        # fires first — either ``unknown_type`` or the future
        # ``fact_endpoint_disallowed`` is acceptable.
        assert result.ok is False
        assert result.reason in {"unknown_type", "fact_endpoint_disallowed"}


class TestEnvelopeEdgeBranches:
    def test_unknown_edge_type_rejected(self) -> None:
        result = validate_envelope(
            kind="edge",
            type="dragon_breath",
            source_type="person",
            target_type="person",
            semantic_type=None,
        )
        assert result.ok is False
        assert result.reason == "unknown_type"

    def test_missing_semantic_on_related_to_rejected(self) -> None:
        result = validate_envelope(
            kind="edge",
            type="related_to",
            source_type="person",
            target_type="organization",
            semantic_type=None,
        )
        assert result.ok is False
        assert result.reason == "missing_semantic_type"

    def test_unknown_semantic_on_related_to_rejected(self) -> None:
        result = validate_envelope(
            kind="edge",
            type="related_to",
            source_type="person",
            target_type="organization",
            semantic_type="dragon_loves",
        )
        assert result.ok is False
        assert result.reason == "unknown_semantic"

    def test_related_to_disallowed_pair_rejected(self) -> None:
        # employed_by is (person, organization); reverse direction is
        # rejected.
        result = validate_envelope(
            kind="edge",
            type="related_to",
            source_type="organization",
            target_type="person",
            semantic_type="employed_by",
        )
        assert result.ok is False
        assert result.reason == "disallowed_pair"

    def test_related_to_valid_pair_accepted(self) -> None:
        result = validate_envelope(
            kind="edge",
            type="related_to",
            source_type="person",
            target_type="organization",
            semantic_type="employed_by",
        )
        assert result.ok is True

    def test_non_related_to_with_semantic_rejected(self) -> None:
        # ``has`` is structural, never carries semantic_type.
        result = validate_envelope(
            kind="edge",
            type="has",
            source_type="person",
            target_type="preference",
            semantic_type="employed_by",
        )
        assert result.ok is False
        assert result.reason == "semantic_on_non_related_to"

    def test_has_edge_valid_accepted(self) -> None:
        result = validate_envelope(
            kind="edge",
            type="has",
            source_type="person",
            target_type="preference",
            semantic_type=None,
        )
        assert result.ok is True

    def test_disallowed_pair_on_structural_edge_rejected(self) -> None:
        # ``part_of`` is (chunk, document) only.
        result = validate_envelope(
            kind="edge",
            type="part_of",
            source_type="person",
            target_type="document",
            semantic_type=None,
        )
        assert result.ok is False
        assert result.reason == "disallowed_pair"

    def test_fact_endpoint_rejected_on_edge(self) -> None:
        # A ``related_to`` edge with one endpoint = ``fact`` is dropped.
        result = validate_envelope(
            kind="edge",
            type="related_to",
            source_type="person",
            target_type="fact",
            semantic_type="knows",
        )
        assert result.ok is False
        assert result.reason == "fact_endpoint_disallowed"

    def test_missing_endpoint_type_rejected(self) -> None:
        result = validate_envelope(
            kind="edge",
            type="related_to",
            source_type=None,
            target_type="person",
            semantic_type="knows",
        )
        assert result.ok is False
        assert result.reason == "missing_endpoint_type"


class TestEnvelopeKindBranches:
    def test_unknown_kind_rejected(self) -> None:
        result = validate_envelope(kind="banana", type="anything")
        assert result.ok is False
        assert result.reason == "unknown_kind"


# ---------------------------------------------------------------------------
# Schema-lookup helpers
# ---------------------------------------------------------------------------


class TestGetNodePropertySchemas:
    def test_returns_parent_schema_for_known_type(self) -> None:
        parent, extras = get_node_property_schemas(type="person", subtype="individual")
        assert parent is PersonProperties
        # ``person`` has no SUBTYPE_EXTRAS today.
        assert extras is None

    def test_extras_returned_for_object_project(self) -> None:
        from tree.entities.ontology_tree_extensions import ProjectExtras

        parent, extras = get_node_property_schemas(type="object", subtype="project")
        assert parent is not None
        assert extras is ProjectExtras

    def test_unknown_type_returns_none(self) -> None:
        parent, extras = get_node_property_schemas(type="dragon", subtype=None)
        assert parent is None
        assert extras is None


class TestGetEdgePropertySchema:
    def test_returns_per_semantic_for_related_to(self) -> None:
        schema = get_edge_property_schema(
            type="related_to", semantic_type="employed_by"
        )
        assert schema is EmployedByProperties

    def test_returns_none_when_semantic_has_no_properties(self) -> None:
        schema = get_edge_property_schema(type="related_to", semantic_type="knows")
        assert schema is None

    def test_returns_none_for_unknown_semantic(self) -> None:
        assert (
            get_edge_property_schema(type="related_to", semantic_type="dragon_loves")
            is None
        )

    def test_returns_spec_for_non_related_to_edge(self) -> None:
        from tree.entities.ontology import MentionsProperties

        schema = get_edge_property_schema(type="mentions", semantic_type=None)
        assert schema is MentionsProperties


# ---------------------------------------------------------------------------
# End-to-end: per-semantic-edge property validation through validate_properties
# ---------------------------------------------------------------------------


class TestValidatePropertiesOnEdgeSemantic:
    def test_employed_by_properties_drop_bad_role(self) -> None:
        # ``role`` is ``str | None``; passing a list drops it.
        raw = {"role": ["invalid"], "start_date": "2025-01-01"}
        validated, drops = validate_properties(raw, EmployedByProperties)
        assert validated == {"start_date": "2025-01-01"}
        assert len(drops) == 1
        assert drops[0].field == "role"

    def test_has_task_properties_round_trip(self) -> None:
        raw = {"status": "pending"}
        validated, drops = validate_properties(raw, HasTaskProperties)
        assert validated == raw
        assert drops == []


@pytest.mark.parametrize(
    "type_name,subtype,name,reason",
    [
        ("person", "individual", "alice", None),
        ("organization", "company", "anthropic", None),
        ("location", "city", "san francisco", None),
        ("event", "meeting", "demo day", None),
        ("object", "task", "ship-demo", None),
        ("preference", None, "coffee", None),
        # Negative cases
        ("person", None, "alice", "missing_subtype"),
        ("person", "dragon", "alice", "unknown_subtype"),
        ("organization", "company", "", "missing_name"),
        ("dragon", None, "smaug", "unknown_type"),
    ],
)
def test_envelope_node_matrix(
    type_name: str, subtype: str | None, name: str, reason: str | None
) -> None:
    result = validate_envelope(kind="node", type=type_name, subtype=subtype, name=name)
    if reason is None:
        assert result.ok is True
    else:
        assert result.ok is False
        assert result.reason == reason
