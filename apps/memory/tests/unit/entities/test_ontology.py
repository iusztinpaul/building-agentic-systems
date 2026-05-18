"""Unit tests for the ontology registry (#027).

The registry is the foundation for Phase-3 POLE+O ontology work — every
downstream task (#028–#032) reads/writes through ``register_*`` calls.
These tests pin:

1. The registry primitives (``NodeTypeSpec`` / ``EdgeTypeSpec`` /
   ``SubtypeSpec``) are frozen dataclasses.
2. The ``register_*`` functions are idempotent on identical
   re-registration and raise ``ValueError`` on conflicts.
3. ``register_node_subtype`` has the three documented branches.
4. The retrofit from closed enums onto the registry produced exactly
   the six node types and nine edge types we shipped pre-#027.
5. ``get_ontology_schema()`` is byte-identical to the pre-refactor
   output (golden file).
6. ``NodeType`` / ``EdgeType`` enum shims still expose every member
   they did before, and their members agree with the registry.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest
from pydantic import BaseModel, Field

from tree.entities.knowledge_graph import EdgeType, NodeType
from tree.entities.ontology import (
    EDGE_CONSTRAINTS,
    EDGE_REGISTRY,
    LLM_EXTRACTABLE_EDGE_TYPES,
    LLM_EXTRACTABLE_NODE_TYPES,
    NODE_PROPERTIES,
    NODE_REGISTRY,
    STRUCTURAL_EDGE_TYPES,
    SUBTYPE_EXTRAS,
    EdgeTypeSpec,
    NodeTypeSpec,
    PersonProperties,
    SubtypeSpec,
    get_ontology_schema,
    register_edge_type,
    register_node_subtype,
    register_node_type,
)


SNAPSHOT_PATH = Path(__file__).parent / "snapshots" / "ontology_schema_v1.json"


# ---------------------------------------------------------------------------
# Helpers — registry snapshot fixture so per-test mutations don't leak.
# ---------------------------------------------------------------------------


@pytest.fixture
def registry_snapshot():
    """Yield a fixture that snapshots the registry mutable state and
    restores it on teardown. Any test that mutates the registry MUST
    request this fixture so the rest of the suite stays deterministic."""

    node_snapshot = dict(NODE_REGISTRY)
    edge_snapshot = dict(EDGE_REGISTRY)
    extras_snapshot = dict(SUBTYPE_EXTRAS)
    yield
    NODE_REGISTRY.clear()
    NODE_REGISTRY.update(node_snapshot)
    EDGE_REGISTRY.clear()
    EDGE_REGISTRY.update(edge_snapshot)
    SUBTYPE_EXTRAS.clear()
    SUBTYPE_EXTRAS.update(extras_snapshot)


# ---------------------------------------------------------------------------
# 1. Primitive shape
# ---------------------------------------------------------------------------


class TestSpecDataclassesAreFrozen:
    def test_node_type_spec_is_frozen(self):
        spec = NodeTypeSpec(
            name="x", properties_schema=PersonProperties, description="x"
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            spec.name = "y"  # type: ignore[misc]

    def test_edge_type_spec_is_frozen(self):
        spec = EdgeTypeSpec(name="e", allowed_pairs=[("a", "b")])
        with pytest.raises(dataclasses.FrozenInstanceError):
            spec.name = "f"  # type: ignore[misc]

    def test_subtype_spec_is_frozen(self):
        spec = SubtypeSpec(name="s", description="d")
        with pytest.raises(dataclasses.FrozenInstanceError):
            spec.name = "t"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# 2. Registration contract — idempotency + conflict
# ---------------------------------------------------------------------------


class _NewNodeProps(BaseModel):
    """Test-only property schema for register_node_type tests."""

    field_x: str = Field(default="")


class _OtherNodeProps(BaseModel):
    """Different test-only schema (used to force a conflict)."""

    field_y: int = Field(default=0)


class TestRegisterNodeType:
    def test_registers_new_node_type(self, registry_snapshot):
        spec = NodeTypeSpec(
            name="testnode",
            properties_schema=_NewNodeProps,
            description="A test node.",
        )

        register_node_type(spec)

        assert "testnode" in NODE_REGISTRY
        assert NODE_REGISTRY["testnode"] is spec

    def test_idempotent_on_identical_re_registration(self, registry_snapshot):
        spec = NodeTypeSpec(
            name="testnode",
            properties_schema=_NewNodeProps,
            description="A test node.",
        )

        register_node_type(spec)
        # Same spec again — must not raise.
        register_node_type(spec)
        # An equal but distinct spec object — must also not raise.
        register_node_type(
            NodeTypeSpec(
                name="testnode",
                properties_schema=_NewNodeProps,
                description="A test node.",
            )
        )

        assert NODE_REGISTRY["testnode"].properties_schema is _NewNodeProps

    def test_conflicting_re_registration_raises(self, registry_snapshot):
        register_node_type(
            NodeTypeSpec(
                name="testnode",
                properties_schema=_NewNodeProps,
                description="A test node.",
            )
        )

        with pytest.raises(ValueError, match="conflicting re-registration"):
            register_node_type(
                NodeTypeSpec(
                    name="testnode",
                    properties_schema=_OtherNodeProps,
                    description="A test node.",
                )
            )


class TestRegisterEdgeType:
    def test_registers_new_edge_type(self, registry_snapshot):
        spec = EdgeTypeSpec(
            name="testedge",
            allowed_pairs=[("person", "person")],
            description="Test edge.",
            llm_extractable=True,
        )

        register_edge_type(spec)

        assert "testedge" in EDGE_REGISTRY
        assert EDGE_REGISTRY["testedge"] is spec

    def test_idempotent_on_identical_re_registration(self, registry_snapshot):
        spec = EdgeTypeSpec(
            name="testedge",
            allowed_pairs=[("person", "person")],
        )
        register_edge_type(spec)
        register_edge_type(spec)

        assert EDGE_REGISTRY["testedge"] is spec

    def test_conflicting_re_registration_raises(self, registry_snapshot):
        register_edge_type(
            EdgeTypeSpec(
                name="testedge",
                allowed_pairs=[("person", "person")],
            )
        )

        with pytest.raises(ValueError, match="conflicting re-registration"):
            register_edge_type(
                EdgeTypeSpec(
                    name="testedge",
                    allowed_pairs=[("person", "task")],
                )
            )


class TestRegisterNodeSubtype:
    def test_raises_when_parent_unknown(self, registry_snapshot):
        with pytest.raises(ValueError, match="not registered"):
            register_node_subtype("nonexistent_parent", "scientist")

    def test_raises_when_parent_is_freeform(self, registry_snapshot):
        # "person" is registered with subtypes=None (freeform) in
        # the built-in registrations; extension semantics don't apply.
        with pytest.raises(ValueError, match="freeform"):
            register_node_subtype("person", "scientist")

    def test_appends_subtype_to_closed_parent(self, registry_snapshot):
        # Re-register "person" with an empty closed set so we can
        # exercise the success branch.
        register_node_type(
            NodeTypeSpec(
                name="closedparent",
                properties_schema=PersonProperties,
                description="A closed-subtype parent for testing.",
                subtypes=frozenset(),
                llm_extractable=True,
            )
        )

        register_node_subtype(
            "closedparent",
            "scientist",
            description="Researcher.",
        )

        spec = NODE_REGISTRY["closedparent"]
        assert spec.subtypes == frozenset({"scientist"})

    def test_appends_extra_properties_to_parallel_dict(self, registry_snapshot):
        class _Extra(BaseModel):
            domain: str = Field(default="")

        register_node_type(
            NodeTypeSpec(
                name="closedparent",
                properties_schema=PersonProperties,
                description="A closed-subtype parent for testing.",
                subtypes=frozenset(),
            )
        )

        register_node_subtype(
            "closedparent",
            "scientist",
            extra_properties=_Extra,
        )

        assert SUBTYPE_EXTRAS[("closedparent", "scientist")] is _Extra


# ---------------------------------------------------------------------------
# 3. Retrofit equivalence — exact set of registered types
# ---------------------------------------------------------------------------


class TestRetrofitRegistries:
    def test_node_registry_has_exactly_the_six_legacy_types(self):
        assert set(NODE_REGISTRY) == {
            "document",
            "chunk",
            "person",
            "task",
            "episode",
            "preference",
        }

    def test_edge_registry_has_exactly_the_nine_legacy_types(self):
        assert set(EDGE_REGISTRY) == {
            "part_of",
            "next",
            "mentions",
            "referenced",
            "related_to",
            "todo",
            "experienced",
            "has",
            "same_as",
        }

    def test_llm_extractable_node_types_unchanged(self):
        assert LLM_EXTRACTABLE_NODE_TYPES == {
            NodeType.PERSON,
            NodeType.TASK,
            NodeType.EPISODE,
            NodeType.PREFERENCE,
        }

    def test_llm_extractable_edge_types_unchanged(self):
        assert LLM_EXTRACTABLE_EDGE_TYPES == {
            EdgeType.RELATED_TO,
            EdgeType.TODO,
            EdgeType.EXPERIENCED,
            EdgeType.HAS,
        }

    def test_structural_edge_types_unchanged(self):
        assert STRUCTURAL_EDGE_TYPES == {
            EdgeType.PART_OF,
            EdgeType.NEXT,
            EdgeType.MENTIONS,
            EdgeType.REFERENCED,
            EdgeType.SAME_AS,
        }

    def test_extractable_and_structural_edges_are_disjoint(self):
        overlap = LLM_EXTRACTABLE_EDGE_TYPES & STRUCTURAL_EDGE_TYPES
        assert overlap == set(), f"Overlap: {overlap}"

    def test_extractable_and_structural_edges_cover_all(self):
        combined = LLM_EXTRACTABLE_EDGE_TYPES | STRUCTURAL_EDGE_TYPES
        assert combined == set(EdgeType)


# ---------------------------------------------------------------------------
# 4. NODE_PROPERTIES / EDGE_CONSTRAINTS — back-compat views
# ---------------------------------------------------------------------------


class TestBackwardCompatViews:
    def test_every_node_type_has_properties(self):
        for node_type in NodeType:
            assert node_type in NODE_PROPERTIES, f"Missing properties for {node_type}"

    def test_every_edge_type_has_constraint(self):
        for edge_type in EdgeType:
            assert edge_type in EDGE_CONSTRAINTS, f"Missing constraint for {edge_type}"

    def test_same_as_constraints_cover_all_four_self_pairs(self):
        same_as = EDGE_CONSTRAINTS[EdgeType.SAME_AS]
        pairs = {(c.source_type.value, c.target_type.value) for c in same_as}
        assert pairs == {
            ("person", "person"),
            ("task", "task"),
            ("episode", "episode"),
            ("preference", "preference"),
        }


# ---------------------------------------------------------------------------
# 5. NodeType / EdgeType enum shim agreement
# ---------------------------------------------------------------------------


class TestEnumShim:
    def test_node_type_members_match_node_registry(self):
        enum_values = {member.value for member in NodeType}
        assert enum_values == set(NODE_REGISTRY)

    def test_edge_type_members_match_edge_registry(self):
        enum_values = {member.value for member in EdgeType}
        assert enum_values == set(EDGE_REGISTRY)

    def test_node_type_exports_every_legacy_member(self):
        # Pinned so a future refactor that drops a member fails loudly.
        for name in [
            "DOCUMENT",
            "CHUNK",
            "PERSON",
            "TASK",
            "EPISODE",
            "PREFERENCE",
        ]:
            assert hasattr(NodeType, name)

    def test_edge_type_exports_every_legacy_member(self):
        for name in [
            "PART_OF",
            "NEXT",
            "MENTIONS",
            "REFERENCED",
            "RELATED_TO",
            "TODO",
            "EXPERIENCED",
            "HAS",
            "SAME_AS",
        ]:
            assert hasattr(EdgeType, name)


# ---------------------------------------------------------------------------
# 6. get_ontology_schema() — basic shape + golden-file diff
# ---------------------------------------------------------------------------


class TestGetOntologySchema:
    def test_returns_node_and_edge_types(self):
        schema = get_ontology_schema()

        assert "node_types" in schema
        assert "edge_types" in schema

    def test_only_extractable_nodes(self):
        schema = get_ontology_schema()
        node_keys = set(schema["node_types"].keys())
        expected = {nt.value for nt in LLM_EXTRACTABLE_NODE_TYPES}

        assert node_keys == expected

    def test_only_extractable_edges(self):
        schema = get_ontology_schema()
        edge_keys = set(schema["edge_types"].keys())
        expected = {et.value for et in LLM_EXTRACTABLE_EDGE_TYPES}

        assert edge_keys == expected

    def test_node_schema_has_properties(self):
        schema = get_ontology_schema()

        for node_type, info in schema["node_types"].items():
            assert "properties" in info, f"Missing properties for {node_type}"
            assert "description" in info

    def test_edge_schema_has_constraints(self):
        schema = get_ontology_schema()

        for edge_type, info in schema["edge_types"].items():
            assert "source_type" in info, f"Missing source_type for {edge_type}"
            assert "target_type" in info, f"Missing target_type for {edge_type}"

    def test_matches_golden_snapshot(self):
        """Pinning test: the current ``get_ontology_schema()`` output
        must equal the checked-in snapshot. A diff means the LLM
        extraction prompt would shift — call that out explicitly
        (regenerate the snapshot only when the behavior change is
        intentional)."""

        assert SNAPSHOT_PATH.exists(), (
            f"Missing snapshot at {SNAPSHOT_PATH}. Regenerate via "
            "scripts/regen_ontology_snapshot.py (or by re-running the "
            "snippet in #027's log)."
        )

        with SNAPSHOT_PATH.open() as f:
            snapshot = json.load(f)

        # Compare via JSON-normalized roundtrip so dict-key ordering
        # doesn't matter; we already sort_keys when we write the file.
        actual = json.loads(json.dumps(get_ontology_schema(), sort_keys=True))
        assert actual == snapshot
