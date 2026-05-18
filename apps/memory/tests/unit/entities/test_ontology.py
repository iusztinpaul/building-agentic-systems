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


# Phase-3 #028: the schema grew (4 new POLE+O canonical types; ``subtypes``
# fields added on every closed-vocab parent; ``task`` / ``episode`` no longer
# top-level), so the v1 snapshot from #027 is superseded by v2. The v1
# snapshot is kept on disk for historical-diff review but no test reads it.
SNAPSHOT_PATH = Path(__file__).parent / "snapshots" / "ontology_schema_v2.json"


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
        # Post-#028, ``person`` has a closed subtype set; ``preference``
        # is the remaining freeform LLM-extractable type, so it's the
        # parent we exercise the freeform branch against.
        with pytest.raises(ValueError, match="freeform"):
            register_node_subtype("preference", "scientist")

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
    def test_node_registry_has_exactly_the_pole_o_types(self):
        # Post-#028: 8 entries — 5 POLE+O LLM-extractable (person,
        # organization, location, event, object) + preference (still
        # freeform) + 2 structural (document, chunk). The legacy
        # ``task`` / ``episode`` top-level rows are GONE and live as
        # subtypes under ``object`` / ``event`` (see
        # ``test_tree_extensions_self_application``).
        assert set(NODE_REGISTRY) == {
            "document",
            "chunk",
            "person",
            "organization",
            "location",
            "event",
            "object",
            "preference",
        }

    def test_edge_registry_has_exactly_the_nine_legacy_types(self):
        # Untouched in #028 — the edge collapse lands in #029.
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

    def test_llm_extractable_node_types_pole_o(self):
        # Post-#028: 5 POLE+O canonical + preference (still freeform).
        # ``NodeType.TASK`` / ``NodeType.EPISODE`` enum members survive
        # as legacy aliases but are no longer registered as top-level
        # extractable types — they live as subtypes under object/event.
        assert LLM_EXTRACTABLE_NODE_TYPES == {
            NodeType.PERSON,
            NodeType.ORGANIZATION,
            NodeType.LOCATION,
            NodeType.EVENT,
            NodeType.OBJECT,
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
    def test_every_registered_node_type_has_properties(self):
        # Post-#028: ``NODE_PROPERTIES`` is built from
        # :data:`NODE_REGISTRY`, so ``NodeType.TASK`` / ``EPISODE``
        # (which survive only as legacy aliases) are intentionally
        # absent. Iterate the registry, not the enum.
        for type_name in NODE_REGISTRY:
            node_type = NodeType(type_name)
            assert node_type in NODE_PROPERTIES, f"Missing properties for {node_type}"

    def test_legacy_node_type_aliases_absent_from_properties(self):
        # The enum still exposes TASK/EPISODE for code-path compat,
        # but ``NODE_PROPERTIES`` mirrors the registry — which no
        # longer holds those entries.
        assert NodeType.TASK not in NODE_PROPERTIES
        assert NodeType.EPISODE not in NODE_PROPERTIES

    def test_every_edge_type_has_constraint(self):
        for edge_type in EdgeType:
            assert edge_type in EDGE_CONSTRAINTS, f"Missing constraint for {edge_type}"

    def test_same_as_constraints_cover_all_four_self_pairs(self):
        # ``same_as`` allowed_pairs are unchanged in #028 — the legacy
        # pre-POLE+O pair list still applies (task↔task / episode↔episode
        # carry over until #029 collapses these into ``related_to``).
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
    def test_node_type_members_match_node_registry_plus_legacy_aliases(self):
        # Post-#028: the enum has the 6 registry entries the LLM cares
        # about (+ DOCUMENT/CHUNK) PLUS two legacy aliases — ``TASK``
        # and ``EPISODE`` — that the :class:`KnowledgeGraphEntry`
        # mode=before validator silently re-routes to the new
        # (parent, subtype) shape. The aliases are intentionally NOT
        # in :data:`NODE_REGISTRY`.
        enum_values = {member.value for member in NodeType}
        legacy_aliases = {"task", "episode"}
        assert enum_values == set(NODE_REGISTRY) | legacy_aliases

    def test_edge_type_members_match_edge_registry(self):
        enum_values = {member.value for member in EdgeType}
        assert enum_values == set(EDGE_REGISTRY)

    def test_node_type_exports_every_legacy_member(self):
        # Pinned so a future refactor that drops a member fails loudly.
        # Post-#028 the enum also exposes the four new POLE+O canonicals.
        for name in [
            "DOCUMENT",
            "CHUNK",
            "PERSON",
            "ORGANIZATION",
            "LOCATION",
            "EVENT",
            "OBJECT",
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

    def test_iteration_order_is_deterministic(self):
        # Tester flagged on #027 that ``get_ontology_schema()`` iterated
        # a Python ``set`` which produced non-deterministic prompt order
        # across hash-randomized runs. #028's implementation sorts by
        # name; pin that contract here.
        first = list(get_ontology_schema()["node_types"].keys())
        second = list(get_ontology_schema()["node_types"].keys())
        assert first == second
        assert first == sorted(first)

    def test_subtypes_surface_for_closed_vocab_parents(self):
        schema = get_ontology_schema()

        # Each of the five POLE+O LLM-extractable types has a closed
        # subtype set and surfaces a sorted ``subtypes`` list in the
        # schema for the LLM prompt.
        for type_name in ("person", "organization", "location", "event", "object"):
            node_info = schema["node_types"][type_name]
            assert "subtypes" in node_info
            subtypes = node_info["subtypes"]
            assert isinstance(subtypes, list)
            assert subtypes == sorted(subtypes)
            assert len(subtypes) > 0

    def test_subtypes_omitted_for_freeform_parent(self):
        # ``preference`` keeps ``subtypes=None`` in the registry (the
        # typed-slots refactor lands in #032). The schema must omit the
        # ``subtypes`` key so the LLM treats subtype as freeform.
        schema = get_ontology_schema()
        assert "subtypes" not in schema["node_types"]["preference"]

    def test_object_schema_includes_tree_extension_subtypes(self):
        # Self-application of the extension API: Tree's
        # ``task`` / ``topic`` / ``project`` subtypes (registered in
        # :mod:`tree.entities.ontology_tree_extensions`) show up in
        # the LLM-facing schema for the canonical ``object`` parent —
        # the LLM doesn't see them as a separate "Tree thing".
        schema = get_ontology_schema()
        object_subtypes = set(schema["node_types"]["object"]["subtypes"])
        assert {"task", "topic", "project"}.issubset(object_subtypes)


# ---------------------------------------------------------------------------
# 7. POLE+O canonical type registration (#028)
# ---------------------------------------------------------------------------


class TestPoleOCanonicalTypes:
    """Phase-3 #028 pins the four new POLE+O canonical node types
    (organization, location, event, object), each with a closed
    subtype vocabulary and a ``*Properties`` Pydantic shell."""

    @pytest.mark.parametrize(
        "name,expected_subtypes",
        [
            ("person", {"individual", "alias", "persona"}),
            (
                "organization",
                {
                    "company",
                    "nonprofit",
                    "government",
                    "educational",
                    "political",
                    "religious",
                    "military",
                },
            ),
            (
                "location",
                {"address", "city", "region", "country", "landmark", "coordinates"},
            ),
            # Note: ``event`` and ``object`` here include the Tree
            # extension subtypes too — they're inseparable from the
            # registry-level view after import.
            (
                "event",
                {
                    "incident",
                    "meeting",
                    "transaction",
                    "communication",
                    "travel",
                    "employment",
                    "observation",
                    "episode",  # Tree extension
                },
            ),
            (
                "object",
                {
                    "vehicle",
                    "phone",
                    "email",
                    "document",
                    "device",
                    "software",
                    "task",  # Tree extension
                    "topic",  # Tree extension
                    "project",  # Tree extension
                },
            ),
        ],
    )
    def test_canonical_subtype_set(self, name, expected_subtypes):
        spec = NODE_REGISTRY[name]
        assert spec.subtypes == frozenset(expected_subtypes)
        assert spec.llm_extractable is True

    def test_organization_properties_has_field_descriptions(self):
        from tree.entities.ontology import OrganizationProperties

        schema = OrganizationProperties.model_json_schema()
        # Every field must carry a non-empty description — the LLM
        # reads these as its only context. ``aliases`` is the
        # list-typed field, so the description hangs off the parent.
        for field in ("aliases", "jurisdiction", "registration_number"):
            assert schema["properties"][field].get("description"), (
                f"OrganizationProperties.{field} is missing a Field(description=...)"
            )

    def test_location_properties_has_field_descriptions(self):
        from tree.entities.ontology import LocationProperties

        schema = LocationProperties.model_json_schema()
        for field in ("aliases", "address", "city", "country", "coordinates"):
            assert schema["properties"][field].get("description"), (
                f"LocationProperties.{field} is missing a Field(description=...)"
            )

    def test_event_properties_has_field_descriptions(self):
        from tree.entities.ontology import EventProperties

        schema = EventProperties.model_json_schema()
        for field in ("aliases", "date", "time", "duration", "outcome"):
            assert schema["properties"][field].get("description"), (
                f"EventProperties.{field} is missing a Field(description=...)"
            )

    def test_object_properties_has_field_descriptions(self):
        from tree.entities.ontology import ObjectProperties

        schema = ObjectProperties.model_json_schema()
        for field in ("aliases", "identifier", "make", "model", "serial_number"):
            assert schema["properties"][field].get("description"), (
                f"ObjectProperties.{field} is missing a Field(description=...)"
            )

    def test_person_properties_pole_o_extensions(self):
        # #028 also extends ``PersonProperties`` with three new POLE+O
        # fields (date_of_birth, nationality, occupation).
        from tree.entities.ontology import PersonProperties

        schema = PersonProperties.model_json_schema()
        for field in ("aliases", "email", "date_of_birth", "nationality", "occupation"):
            assert field in schema["properties"], (
                f"PersonProperties.{field} missing after #028"
            )
            assert schema["properties"][field].get("description"), (
                f"PersonProperties.{field} is missing a Field(description=...)"
            )

    def test_task_and_episode_not_top_level(self):
        # The legacy ``task`` / ``episode`` rows are GONE from
        # the top-level registry; they live as subtypes under
        # ``object`` / ``event`` now.
        assert "task" not in NODE_REGISTRY
        assert "episode" not in NODE_REGISTRY


# ---------------------------------------------------------------------------
# 8. Tree subtype extensions (self-application of the extension API)
# ---------------------------------------------------------------------------


class TestTreeExtensionsSelfApplication:
    """Pin the four Tree extensions registered in
    :mod:`tree.entities.ontology_tree_extensions`. The whole point of
    the extension API is that Tree's domain concepts (``task`` /
    ``episode`` / ``topic`` / ``project``) ride on top of the
    POLE+O canonical parents — they look indistinguishable from
    canonical subtypes to downstream consumers.
    """

    def test_object_task_extension(self):
        assert "task" in NODE_REGISTRY["object"].subtypes

    def test_event_episode_extension(self):
        assert "episode" in NODE_REGISTRY["event"].subtypes

    def test_object_topic_extension(self):
        # ``topic`` is Tree-only — NEVER a canonical POLE+O subtype.
        # Pinned so a misreading of the spec doesn't accidentally
        # promote it to canonical.
        assert "topic" in NODE_REGISTRY["object"].subtypes

    def test_object_project_extension_has_extras(self):
        from tree.entities.ontology_tree_extensions import ProjectExtras

        assert "project" in NODE_REGISTRY["object"].subtypes
        assert SUBTYPE_EXTRAS[("object", "project")] is ProjectExtras


class TestExternalRefAndProjectExtras:
    """The ``ProjectExtras`` schema is a thin wrapper around
    ``ExternalRef``, which is the public handle for "this object
    points at a richly-tracked project in Linear/Notion/etc.".
    """

    def test_external_ref_round_trip(self):
        from tree.entities.ontology_tree_extensions import ExternalRef

        original = ExternalRef(
            system="linear",
            id="PRJ-123",
            url="https://linear.app/teams/x/projects/PRJ-123",
        )

        dumped = original.model_dump()
        rehydrated = ExternalRef.model_validate(dumped)

        assert rehydrated == original

    def test_external_ref_url_is_optional(self):
        from tree.entities.ontology_tree_extensions import ExternalRef

        # ``url`` is the only optional field — without it the handle
        # still resolves.
        ref = ExternalRef(system="notion", id="page-abc")

        assert ref.url is None

    def test_project_extras_round_trip(self):
        from tree.entities.ontology_tree_extensions import (
            ExternalRef,
            ProjectExtras,
        )

        original = ProjectExtras(
            external_ref=ExternalRef(system="linear", id="PRJ-1"),
        )

        dumped = original.model_dump()
        rehydrated = ProjectExtras.model_validate(dumped)

        assert rehydrated == original
        assert rehydrated.external_ref is not None
        assert rehydrated.external_ref.system == "linear"
        assert rehydrated.external_ref.id == "PRJ-1"

    def test_project_extras_external_ref_is_optional(self):
        from tree.entities.ontology_tree_extensions import ProjectExtras

        # ``external_ref`` is optional — a Tree-only project with no
        # external mirror is a valid construction.
        extras = ProjectExtras()

        assert extras.external_ref is None

    def test_external_ref_descriptions_present(self):
        # Pin the LLM-facing descriptions so a refactor that drops them
        # fails loudly.
        from tree.entities.ontology_tree_extensions import ExternalRef

        schema = ExternalRef.model_json_schema()
        for field in ("system", "id", "url"):
            assert schema["properties"][field].get("description"), (
                f"ExternalRef.{field} is missing a Field(description=...)"
            )


# ---------------------------------------------------------------------------
# 9. register_node_subtype — failure modes ([#028] integration coverage)
# ---------------------------------------------------------------------------


class TestRegisterNodeSubtypeFailureModes:
    """The extension API has three documented failure modes; #027's
    primitive tests cover the parent-unknown + parent-freeform
    branches. #028 self-applies the API in production, so this block
    adds the "conflicting subtype-extras model" edge case the
    extension API supports today (last-write-wins on extras model).
    """

    def test_register_against_unknown_parent_raises(self, registry_snapshot):
        with pytest.raises(ValueError, match="not registered"):
            register_node_subtype("nope_unknown_parent", "subtype")

    def test_register_against_freeform_parent_raises(self, registry_snapshot):
        # Pre-#028 ``person`` was freeform; post-#028 ``preference`` is.
        # Use it to pin the freeform-rejection branch.
        with pytest.raises(ValueError, match="freeform"):
            register_node_subtype("preference", "scientific")

    def test_re_registering_same_subtype_is_idempotent(self, registry_snapshot):
        # The extension API is set-union-based; re-registering an
        # existing subtype on the same parent must not raise.
        before = NODE_REGISTRY["object"].subtypes
        assert before is not None
        assert "task" in before
        size_before = len(before)

        # No-op (subtype already in the set).
        register_node_subtype("object", "task", description="re-register")

        after = NODE_REGISTRY["object"].subtypes
        assert after is not None
        assert "task" in after
        assert len(after) == size_before

    def test_subtype_extras_last_write_wins(self, registry_snapshot):
        # The extension API stores ``extra_properties`` in a parallel
        # dict; re-registering a subtype with a new extras model
        # overwrites the previous one. Pinned so a future refactor
        # that wants stricter semantics gets a failing test to think
        # about.
        class _ExtrasV1(BaseModel):
            v: int = Field(default=1)

        class _ExtrasV2(BaseModel):
            v: int = Field(default=2)

        register_node_type(
            NodeTypeSpec(
                name="conflictparent",
                properties_schema=PersonProperties,
                description="Test parent for extras conflict.",
                subtypes=frozenset(),
            )
        )

        register_node_subtype("conflictparent", "child", extra_properties=_ExtrasV1)
        assert SUBTYPE_EXTRAS[("conflictparent", "child")] is _ExtrasV1

        register_node_subtype("conflictparent", "child", extra_properties=_ExtrasV2)
        assert SUBTYPE_EXTRAS[("conflictparent", "child")] is _ExtrasV2
