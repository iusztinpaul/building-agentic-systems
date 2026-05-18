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
    RELATION_SEMANTICS,
    STRUCTURAL_EDGE_TYPES,
    SUBTYPE_EXTRAS,
    EdgeTypeSpec,
    EmployedByProperties,
    MentionsProperties,
    NodeTypeSpec,
    PersonProperties,
    RelationSemanticSpec,
    SameAsMatchType,
    SameAsProperties,
    SameAsStatus,
    SubtypeSpec,
    get_ontology_schema,
    register_edge_type,
    register_node_subtype,
    register_node_type,
    register_relation_semantic,
)


# Phase-3 #028 grew the schema with 4 new POLE+O canonical types; #029
# folded the LLM-extractable domain edges into ``related_to``; #030 added
# the ``common_fields`` block; #031 adds the ``fact`` LLM-extractable
# node type with ``FactProperties``. The v1–v4 snapshots are kept on
# disk for historical-diff review but no test reads them.
SNAPSHOT_PATH = Path(__file__).parent / "snapshots" / "ontology_schema_v5.json"


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
    semantics_snapshot = dict(RELATION_SEMANTICS)
    yield
    NODE_REGISTRY.clear()
    NODE_REGISTRY.update(node_snapshot)
    EDGE_REGISTRY.clear()
    EDGE_REGISTRY.update(edge_snapshot)
    SUBTYPE_EXTRAS.clear()
    SUBTYPE_EXTRAS.update(extras_snapshot)
    RELATION_SEMANTICS.clear()
    RELATION_SEMANTICS.update(semantics_snapshot)


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
        # Post-#031: ``fact`` joins as a 9th entry — the LLM-extractable
        # escape-hatch node type.
        assert set(NODE_REGISTRY) == {
            "document",
            "chunk",
            "person",
            "organization",
            "location",
            "event",
            "object",
            "preference",
            "fact",
        }

    def test_edge_registry_has_post_029_edge_types(self):
        # #029 collapsed ``todo`` / ``experienced`` into the
        # ``related_to`` umbrella, so the registry now holds 7 edges.
        assert set(EDGE_REGISTRY) == {
            "part_of",
            "next",
            "mentions",
            "referenced",
            "related_to",
            "has",
            "same_as",
        }

    def test_llm_extractable_node_types_pole_o(self):
        # Post-#028: 5 POLE+O canonical + preference (still freeform).
        # Post-#031: ``fact`` joins the LLM-extractable set as the
        # POLE+O escape-hatch for propositions that don't fit any
        # registered relation semantic.
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
            NodeType.FACT,
        }

    def test_llm_extractable_edge_types_post_029(self):
        # #029: ``todo`` / ``experienced`` retired into ``related_to``;
        # ``has`` is now structural (pipeline-emitted, never LLM).
        assert LLM_EXTRACTABLE_EDGE_TYPES == {EdgeType.RELATED_TO}

    def test_structural_edge_types_post_029(self):
        # ``has`` joins the structural set after #029.
        assert STRUCTURAL_EDGE_TYPES == {
            EdgeType.PART_OF,
            EdgeType.NEXT,
            EdgeType.MENTIONS,
            EdgeType.REFERENCED,
            EdgeType.HAS,
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

    def test_same_as_constraints_cover_post_029_pole_o_self_pairs(self):
        # #029: ``same_as`` broadened from the legacy 4-pair set to
        # every POLE+O LLM-extractable type (self-pair only). The
        # legacy task↔task / episode↔episode pairs are GONE — task and
        # episode now live as node subtypes and never carry their own
        # ``same_as`` edge.
        same_as = EDGE_CONSTRAINTS[EdgeType.SAME_AS]
        pairs = {(c.source_type.value, c.target_type.value) for c in same_as}
        assert pairs == {
            ("person", "person"),
            ("organization", "organization"),
            ("location", "location"),
            ("event", "event"),
            ("object", "object"),
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
        # Post-#031: ``FACT`` joins the enum + registry as the POLE+O
        # escape-hatch node type.
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
            "FACT",
        ]:
            assert hasattr(NodeType, name)

    def test_edge_type_exports_every_legacy_member(self):
        # Post-#029: ``TODO`` and ``EXPERIENCED`` are gone (collapsed
        # into ``RELATED_TO + semantic_type``).
        for name in [
            "PART_OF",
            "NEXT",
            "MENTIONS",
            "REFERENCED",
            "RELATED_TO",
            "HAS",
            "SAME_AS",
        ]:
            assert hasattr(EdgeType, name)
        for retired in ["TODO", "EXPERIENCED"]:
            assert not hasattr(EdgeType, retired), (
                f"EdgeType.{retired} must be retired per #029"
            )


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
        # Post-#029: the per-edge schema carries ``allowed_pairs``
        # (list of [src, tgt] pairs) and, for ``related_to``, a
        # nested ``semantic_types`` map. The legacy single
        # ``source_type``/``target_type`` is gone.
        schema = get_ontology_schema()

        for edge_type, info in schema["edge_types"].items():
            assert "allowed_pairs" in info, f"Missing allowed_pairs for {edge_type}"
            assert "description" in info, f"Missing description for {edge_type}"

    def test_related_to_schema_has_semantic_types_map(self):
        schema = get_ontology_schema()
        rt = schema["edge_types"]["related_to"]
        assert "semantic_types" in rt
        semantic_keys = set(rt["semantic_types"].keys())
        assert semantic_keys == {
            "knows",
            "member_of",
            "employed_by",
            "owns",
            "uses",
            "located_at",
            "resides_at",
            "headquarters_at",
            "participated_in",
            "occurred_at",
            "involved",
            "subsidiary_of",
            "partner_with",
            "alias_of",
            "has_task",
            "experienced_by",
        }
        # Per-semantic blocks carry their own description + allowed_pairs.
        for semantic, info in rt["semantic_types"].items():
            assert "description" in info, f"semantic {semantic} missing description"
            assert "allowed_pairs" in info, f"semantic {semantic} missing allowed_pairs"

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


# ---------------------------------------------------------------------------
# 10. #029 — RELATION_SEMANTICS catalogue + register_relation_semantic
# ---------------------------------------------------------------------------


# Canonical (allowed_pairs, properties_schema-is-None) for the 16 entries.
# Pinned here so a regression in the catalogue table fails loudly.
_EXPECTED_SEMANTICS: dict[str, tuple[set[tuple[str, str]], bool]] = {
    # name -> (allowed_pairs, has_properties_schema)
    "knows": ({("person", "person")}, False),
    "member_of": ({("person", "organization")}, True),
    "employed_by": ({("person", "organization")}, True),
    "owns": ({("person", "object"), ("organization", "object")}, True),
    "uses": ({("person", "object"), ("organization", "object")}, False),
    "located_at": ({("object", "location"), ("event", "location")}, True),
    "resides_at": ({("person", "location")}, True),
    "headquarters_at": ({("organization", "location")}, False),
    "participated_in": ({("person", "event"), ("organization", "event")}, True),
    "occurred_at": ({("event", "location")}, False),
    "involved": ({("object", "event")}, True),
    "subsidiary_of": ({("organization", "organization")}, False),
    "partner_with": ({("organization", "organization")}, False),
    "alias_of": (
        {
            ("person", "person"),
            ("organization", "organization"),
            ("location", "location"),
            ("event", "event"),
            ("object", "object"),
        },
        False,
    ),
    # Tree extensions
    "has_task": ({("person", "object")}, True),
    "experienced_by": ({("person", "event")}, True),
}


class TestRelationSemanticsCatalogue:
    """The 16-entry semantic catalogue + per-spec invariants."""

    def test_catalogue_has_16_entries(self):
        assert set(RELATION_SEMANTICS) == set(_EXPECTED_SEMANTICS)

    @pytest.mark.parametrize(
        "name,expected",
        list(_EXPECTED_SEMANTICS.items()),
        ids=list(_EXPECTED_SEMANTICS),
    )
    def test_each_semantic_has_expected_shape(self, name, expected):
        expected_pairs, has_props = expected
        spec = RELATION_SEMANTICS[name]
        assert set(spec.allowed_pairs) == expected_pairs
        assert (spec.properties_schema is not None) == has_props
        assert spec.description, f"semantic {name!r} missing description"

    def test_relation_semantic_spec_is_frozen(self):
        spec = RelationSemanticSpec(name="x", allowed_pairs=[("a", "b")])
        with pytest.raises(dataclasses.FrozenInstanceError):
            spec.name = "y"  # type: ignore[misc]

    def test_every_property_schema_has_field_descriptions(self):
        # Every per-semantic Pydantic model must carry Field(description=...)
        # on every attribute so the LLM has context.
        for name, spec in RELATION_SEMANTICS.items():
            if spec.properties_schema is None:
                continue
            schema = spec.properties_schema.model_json_schema()
            properties = schema.get("properties", {})
            assert properties, f"{name} properties_schema has no fields"
            for field_name, field_info in properties.items():
                assert field_info.get("description"), (
                    f"{name}.{field_name} is missing Field(description=...)"
                )


class TestRegisterRelationSemantic:
    def test_registers_new_semantic(self, registry_snapshot):
        spec = RelationSemanticSpec(
            name="testsemantic",
            allowed_pairs=[("person", "person")],
            description="Test.",
        )
        register_relation_semantic(spec)
        assert RELATION_SEMANTICS["testsemantic"] is spec

    def test_idempotent_on_identical_re_registration(self, registry_snapshot):
        spec = RelationSemanticSpec(
            name="testsemantic",
            allowed_pairs=[("person", "person")],
            description="Test.",
        )
        register_relation_semantic(spec)
        register_relation_semantic(
            RelationSemanticSpec(
                name="testsemantic",
                allowed_pairs=[("person", "person")],
                description="Test.",
            )
        )
        assert RELATION_SEMANTICS["testsemantic"].description == "Test."

    def test_conflicting_re_registration_raises(self, registry_snapshot):
        register_relation_semantic(
            RelationSemanticSpec(
                name="testsemantic",
                allowed_pairs=[("person", "person")],
                description="Test.",
            )
        )
        with pytest.raises(ValueError, match="conflicting re-registration"):
            register_relation_semantic(
                RelationSemanticSpec(
                    name="testsemantic",
                    allowed_pairs=[("organization", "organization")],
                    description="Test.",
                )
            )


class TestRelatedToUmbrellaEdge:
    """The umbrella ``related_to`` edge spec is derived from the union
    of every semantic's allowed_pairs (#029)."""

    def test_related_to_allowed_pairs_is_union_of_semantics(self):
        expected_union: set[tuple[str, str]] = set()
        for spec in RELATION_SEMANTICS.values():
            expected_union.update(spec.allowed_pairs)
        assert set(EDGE_REGISTRY["related_to"].allowed_pairs) == expected_union

    def test_related_to_remains_llm_extractable(self):
        assert EDGE_REGISTRY["related_to"].llm_extractable is True

    def test_only_related_to_is_llm_extractable(self):
        # Post-#029 the only LLM-extractable domain edge is ``related_to``.
        assert LLM_EXTRACTABLE_EDGE_TYPES == {EdgeType.RELATED_TO}


class TestStructuralEdgePropertyModels:
    """``MentionsProperties`` / ``SameAsProperties`` plus the
    ``SameAsMatchType`` / ``SameAsStatus`` enums (#029)."""

    def test_mentions_properties_defaults(self):
        m = MentionsProperties()
        assert m.confidence == 1.0
        assert m.start_pos is None
        assert m.end_pos is None

    def test_mentions_properties_descriptions_present(self):
        schema = MentionsProperties.model_json_schema()
        for field in ("confidence", "start_pos", "end_pos"):
            assert schema["properties"][field].get("description"), (
                f"MentionsProperties.{field} missing description"
            )

    def test_same_as_properties_defaults(self):
        s = SameAsProperties()
        assert s.confidence == 1.0
        assert s.match_type == SameAsMatchType.EMBEDDING
        assert s.status == SameAsStatus.PENDING

    def test_same_as_properties_round_trip(self):
        s = SameAsProperties(
            confidence=0.91,
            match_type=SameAsMatchType.BOTH,
            status=SameAsStatus.CONFIRMED,
        )
        dumped = s.model_dump()
        rehydrated = SameAsProperties.model_validate(dumped)
        assert rehydrated == s

    def test_same_as_status_values(self):
        assert SameAsStatus.PENDING == "pending"
        assert SameAsStatus.CONFIRMED == "confirmed"
        assert SameAsStatus.REJECTED == "rejected"

    def test_same_as_match_type_values(self):
        assert SameAsMatchType.EMBEDDING == "embedding"
        assert SameAsMatchType.FUZZY == "fuzzy"
        assert SameAsMatchType.BOTH == "both"

    def test_employed_by_properties_round_trip(self):
        # Pin the shared role+dates shape end-to-end.
        e = EmployedByProperties(
            role="staff engineer",
            start_date="2024-03-01",
            end_date=None,
        )
        rehydrated = EmployedByProperties.model_validate(e.model_dump())
        assert rehydrated == e


class TestMentionsBroadeningAndCarveOut:
    """``mentions`` accepts every POLE+O LLM-extractable entity EXCEPT
    preference (#029, ``plan.md:479``)."""

    def test_mentions_allows_chunk_to_extractable_entities(self):
        pairs = set(EDGE_REGISTRY["mentions"].allowed_pairs)
        for tgt in ("person", "organization", "location", "event", "object"):
            assert ("chunk", tgt) in pairs, f"missing (chunk, {tgt})"
            assert ("document", tgt) in pairs, f"missing (document, {tgt})"

    def test_mentions_carves_out_preference_target(self):
        pairs = set(EDGE_REGISTRY["mentions"].allowed_pairs)
        assert ("chunk", "preference") not in pairs
        assert ("document", "preference") not in pairs

    def test_mentions_properties_schema_attached(self):
        assert EDGE_REGISTRY["mentions"].properties_schema is MentionsProperties

    def test_mentions_is_structural_not_llm_extractable(self):
        assert EDGE_REGISTRY["mentions"].llm_extractable is False


class TestSameAsBroadening:
    def test_same_as_allowed_pairs(self):
        pairs = set(EDGE_REGISTRY["same_as"].allowed_pairs)
        # Self-pair on every LLM-extractable POLE+O type (except fact).
        assert pairs == {
            ("person", "person"),
            ("organization", "organization"),
            ("location", "location"),
            ("event", "event"),
            ("object", "object"),
            ("preference", "preference"),
        }

    def test_same_as_properties_schema_attached(self):
        assert EDGE_REGISTRY["same_as"].properties_schema is SameAsProperties


class TestHasEdgeStructural:
    """#029: ``has`` survives as a structural edge with broadened pairs."""

    def test_has_allowed_pairs_include_preference_and_object(self):
        pairs = set(EDGE_REGISTRY["has"].allowed_pairs)
        assert pairs == {("person", "preference"), ("person", "object")}

    def test_has_is_structural_not_llm_extractable(self):
        # Post-#029 ``has`` is pipeline-emitted, never LLM-extractable.
        assert EDGE_REGISTRY["has"].llm_extractable is False


class TestRetiredEdgeTypes:
    """The retired LLM-extractable domain edges must be gone from the
    registry."""

    @pytest.mark.parametrize("retired", ["todo", "experienced"])
    def test_retired_edge_not_in_registry(self, retired):
        assert retired not in EDGE_REGISTRY


# ---------------------------------------------------------------------------
# 12. #031 — ``fact`` escape-hatch node (island-style)
# ---------------------------------------------------------------------------


class TestFactNodeRegistration:
    """#031: ``fact`` is an LLM-extractable POLE+O escape-hatch node
    with ``FactProperties`` (subject / predicate / object). It has no
    closed subtype vocabulary and participates in zero edges — the
    envelope validator's forbidden-endpoint list pins the rule.
    """

    def test_fact_registered_with_expected_spec(self):
        from tree.entities.ontology import FactProperties

        spec = NODE_REGISTRY["fact"]
        assert spec.name == "fact"
        assert spec.properties_schema is FactProperties
        assert spec.subtypes is None
        assert spec.llm_extractable is True
        assert spec.description  # non-empty

    def test_fact_properties_round_trip_with_alias(self):
        """The wire-form key MUST be ``"object"`` (alias) even though the
        Python attribute is ``object_`` (to avoid shadowing the builtin).
        Pydantic ``model_validate`` with ``populate_by_name=True`` accepts
        either spelling; ``model_dump(by_alias=True)`` emits the alias.
        """

        from tree.entities.ontology import FactProperties

        # Construct from wire-form keys (the LLM's emission).
        fp = FactProperties.model_validate(
            {"subject": "earth", "predicate": "orbits", "object": "sun"}
        )
        assert fp.subject == "earth"
        assert fp.predicate == "orbits"
        assert fp.object_ == "sun"

        # Dump with aliases — the on-disk / wire form.
        dumped = fp.model_dump(by_alias=True)
        assert dumped == {
            "subject": "earth",
            "predicate": "orbits",
            "object": "sun",
        }
        # Round-trip via the alias-named dict.
        rehydrated = FactProperties.model_validate(dumped)
        assert rehydrated == fp

    def test_fact_properties_construct_from_python_name(self):
        """``populate_by_name=True`` accepts the Python attribute name
        ``object_`` as well — keeps existing call sites that use the
        attribute name working."""

        from tree.entities.ontology import FactProperties

        fp = FactProperties(subject="a", predicate="is", object_="b")
        assert fp.object_ == "b"

    def test_fact_properties_field_descriptions_non_empty(self):
        from tree.entities.ontology import FactProperties

        schema = FactProperties.model_json_schema()
        # by_alias=True is the default for model_json_schema, so the
        # wire-form key ``"object"`` appears in the properties map.
        properties = schema["properties"]
        for wire_key in ("subject", "predicate", "object"):
            assert wire_key in properties, (
                f"FactProperties wire-form key {wire_key!r} missing from schema"
            )
            assert properties[wire_key].get("description"), (
                f"FactProperties.{wire_key} is missing Field(description=...)"
            )

    def test_fact_in_llm_extractable_set(self):
        # Pin the public view: ``fact`` MUST be in the LLM-extractable
        # set so the prompt assembler surfaces it.
        assert NodeType.FACT in LLM_EXTRACTABLE_NODE_TYPES


class TestFactIslandRule:
    """#031: facts are an **island** — no edge type has ``fact`` in its
    ``allowed_pairs``, and no relation semantic does either. These tests
    pin the rule across every registered edge / semantic.
    """

    def test_no_edge_allowed_pair_has_fact_endpoint(self):
        offenders = []
        for name, spec in EDGE_REGISTRY.items():
            for src, tgt in spec.allowed_pairs:
                if src == "fact" or tgt == "fact":
                    offenders.append((name, src, tgt))
        assert offenders == [], (
            f"Fact endpoint found on edge allowed_pairs: {offenders!r}"
        )

    def test_no_relation_semantic_has_fact_endpoint(self):
        offenders = []
        for name, spec in RELATION_SEMANTICS.items():
            for src, tgt in spec.allowed_pairs:
                if src == "fact" or tgt == "fact":
                    offenders.append((name, src, tgt))
        assert offenders == [], (
            f"Fact endpoint found on relation_semantic allowed_pairs: {offenders!r}"
        )

    def test_mentions_does_not_allow_fact_target(self):
        # Explicit pin against the carve-out in ontology.py's
        # ``_pole_o_llm_extractable_for_mentions`` helper.
        pairs = set(EDGE_REGISTRY["mentions"].allowed_pairs)
        for src, tgt in pairs:
            assert tgt != "fact", (
                f"mentions edge unexpectedly allows fact target: {(src, tgt)}"
            )

    def test_same_as_does_not_allow_fact(self):
        pairs = set(EDGE_REGISTRY["same_as"].allowed_pairs)
        assert ("fact", "fact") not in pairs


class TestFactSchemaInPrompt:
    """The fact node MUST surface in ``get_ontology_schema()`` so the
    LLM extraction prompt includes its properties + the decision tree
    section lifted into the system prompt.
    """

    def test_fact_node_present_in_ontology_schema(self):
        schema = get_ontology_schema()
        assert "fact" in schema["node_types"]
        node_info = schema["node_types"]["fact"]
        # Freeform — no subtypes list.
        assert "subtypes" not in node_info
        # All three FactProperties fields surface under their wire-form keys.
        properties = node_info["properties"]
        for wire_key in ("subject", "predicate", "object"):
            assert wire_key in properties

    def test_fact_required_fields(self):
        schema = get_ontology_schema()
        required = set(schema["node_types"]["fact"]["required"])
        # All three properties are required (no defaults).
        assert required == {"subject", "predicate", "object"}


class TestKnowledgeGraphEntryAcceptsFactNode:
    """The Beanie ``KnowledgeGraphEntry`` model must accept a fact-typed
    node row constructed from the validator's surviving properties."""

    def test_construct_fact_node_entry(self):
        from datetime import UTC, datetime

        from beanie import PydanticObjectId

        from tree.entities.knowledge_graph import KnowledgeGraphEntry

        user_id = PydanticObjectId()
        now = datetime.now(tz=UTC)
        # Wire-form ``"object"`` key — what the validator stores after
        # alias-aware ``validate_properties``.
        entry = KnowledgeGraphEntry(
            id=f"{user_id}:fact:earth-orbits-sun",
            user_id=user_id,
            kind="node",
            type="fact",
            name="earth-orbits-sun",
            subtype=None,
            properties={
                "subject": "earth",
                "predicate": "orbits",
                "object": "sun",
            },
            created_at=now,
            updated_at=now,
        )
        assert entry.kind == "node"
        assert entry.type == "fact"
        assert entry.subtype is None
        assert entry.properties["object"] == "sun"
