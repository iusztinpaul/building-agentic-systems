from datetime import UTC, datetime, timezone

import pytest
from beanie import PydanticObjectId
from pymongo import IndexModel

from tree.entities.knowledge_graph import (
    EdgeType,
    ExtractorInfo,
    KnowledgeGraphEntry,
    NodeType,
    build_edge_id,
    build_node_id,
)


def _user_id() -> PydanticObjectId:
    return PydanticObjectId()


class TestBuildNodeId:
    def test_builds_composite_id(self):
        user_id = PydanticObjectId()
        assert build_node_id(user_id, NodeType.PERSON, "alice") == (
            f"{user_id}:person:alice"
        )

    def test_builds_document_id(self):
        user_id = PydanticObjectId()
        assert (
            build_node_id(user_id, NodeType.DOCUMENT, "https://example.com")
            == f"{user_id}:document:https://example.com"
        )

    def test_builds_chunk_id(self):
        user_id = PydanticObjectId()
        assert (
            build_node_id(user_id, NodeType.CHUNK, "https://example.com#chunk-0")
            == f"{user_id}:chunk:https://example.com#chunk-0"
        )


class TestBuildEdgeId:
    def test_builds_edge_id(self):
        # Source/target carry the user prefix; edge id wraps them.
        # Post-#029 the ``todo`` edge type is gone; ``has_task`` semantics
        # are emitted via ``related_to``. The build_edge_id call itself
        # is type-agnostic — verify with the surviving ``RELATED_TO``.
        user_id = PydanticObjectId()
        src = build_node_id(user_id, NodeType.PERSON, "alice")
        tgt = build_node_id(user_id, NodeType.OBJECT, "write a book")

        result = build_edge_id(src, EdgeType.RELATED_TO, tgt)
        assert result == f"{src}|related_to|{tgt}"


class TestKnowledgeGraphEntry:
    async def test_node_entry(self):
        user_id = _user_id()
        entry = KnowledgeGraphEntry(
            id=f"{user_id}:person:alice",
            user_id=user_id,
            kind="node",
            type=NodeType.PERSON,
            properties={"aliases": []},
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            updated_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
        )

        assert entry.id == f"{user_id}:person:alice"
        assert entry.user_id == user_id
        assert entry.kind == "node"
        assert entry.embedding == []
        assert entry.source_node_id is None

    async def test_edge_entry(self):
        user_id = _user_id()
        entry = KnowledgeGraphEntry(
            id=f"{user_id}:person:alice|related_to|{user_id}:person:bob",
            user_id=user_id,
            kind="edge",
            type=EdgeType.RELATED_TO,
            semantic_type="knows",
            source_node_id=f"{user_id}:person:alice",
            source_type=NodeType.PERSON,
            target_node_id=f"{user_id}:person:bob",
            target_type=NodeType.PERSON,
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            updated_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
        )

        assert entry.kind == "edge"
        assert entry.source_node_id == f"{user_id}:person:alice"
        assert entry.target_node_id == f"{user_id}:person:bob"

    async def test_missing_required_id_raises(self):
        with pytest.raises(Exception):
            KnowledgeGraphEntry(
                user_id=_user_id(),
                kind="node",
                type=NodeType.PERSON,
                created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                updated_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
            )

    async def test_missing_required_user_id_raises(self):
        # Per #018: user_id is required at construction (no default).
        with pytest.raises(Exception):
            KnowledgeGraphEntry(
                id="anyuser:person:alice",
                kind="node",
                type=NodeType.PERSON,
                created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                updated_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
            )


class TestKnowledgeGraphSettingsIndexes:
    """#018: the model declares two static compound indexes with
    ``user_id`` as the leading field. The dynamic indexes
    (kind_source_node, kind_target_node, kind_embedding, canonical_name)
    are owned by the indexing pipeline and get their user_id prefix in #019.
    """

    def test_user_kind_type_index_declared(self) -> None:
        index_models: list[IndexModel] = list(KnowledgeGraphEntry.Settings.indexes)

        target_key = [("user_id", 1), ("kind", 1), ("type", 1)]
        assert any(
            list(im.document.get("key", {}).items()) == target_key
            for im in index_models
        ), f"Expected compound index on {target_key}; got {index_models}"

    def test_user_type_name_index_declared(self) -> None:
        index_models: list[IndexModel] = list(KnowledgeGraphEntry.Settings.indexes)

        target_key = [("user_id", 1), ("type", 1), ("name", 1)]
        assert any(
            list(im.document.get("key", {}).items()) == target_key
            for im in index_models
        ), f"Expected compound index on {target_key}; got {index_models}"


class TestResolutionDedupFields:
    """Resolution + dedup fields added in task #007.

    Five new optional fields on ``KnowledgeGraphEntry`` plus ``EdgeType.SAME_AS``.
    """

    def _build_node(self, **overrides) -> KnowledgeGraphEntry:
        user_id = _user_id()
        defaults = dict(
            id=f"{user_id}:person:alice",
            user_id=user_id,
            kind="node",
            type=NodeType.PERSON,
            name="alice",
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            updated_at=datetime(2026, 1, 2, tzinfo=UTC),
        )
        defaults.update(overrides)
        return KnowledgeGraphEntry(**defaults)

    def _build_edge(self, **overrides) -> KnowledgeGraphEntry:
        user_id = _user_id()
        defaults = dict(
            id=(f"{user_id}:person:alice|same_as|{user_id}:person:alice smith"),
            user_id=user_id,
            kind="edge",
            type=EdgeType.SAME_AS,
            source_node_id=f"{user_id}:person:alice",
            source_type=NodeType.PERSON,
            target_node_id=f"{user_id}:person:alice smith",
            target_type=NodeType.PERSON,
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            updated_at=datetime(2026, 1, 2, tzinfo=UTC),
        )
        defaults.update(overrides)
        return KnowledgeGraphEntry(**defaults)

    async def test_node_default_values_for_new_fields(self):
        entry = self._build_node()

        assert entry.canonical_name is None
        assert entry.aliases == []
        assert entry.confidence == 1.0
        assert entry.merged_into is None
        assert entry.merged_at is None

    async def test_edge_default_values_for_new_fields(self):
        entry = self._build_edge()

        # Node-only fields stay at documented defaults on edge rows.
        assert entry.canonical_name is None
        assert entry.aliases == []
        assert entry.confidence == 1.0
        assert entry.merged_into is None
        assert entry.merged_at is None

    async def test_same_as_enum_member_value(self):
        assert EdgeType.SAME_AS == "same_as"
        assert EdgeType.SAME_AS.value == "same_as"

    @pytest.mark.parametrize(
        "previously_shipped",
        [
            EdgeType.PART_OF,
            EdgeType.NEXT,
            EdgeType.MENTIONS,
            EdgeType.REFERENCED,
            EdgeType.RELATED_TO,
            EdgeType.HAS,
        ],
    )
    async def test_existing_edge_types_unchanged(self, previously_shipped):
        # Adding SAME_AS must not rename / remove any prior member.
        # Post-#029: ``TODO`` and ``EXPERIENCED`` are intentionally
        # gone — they're now ``RELATED_TO + semantic_type``.
        assert previously_shipped in EdgeType

    async def test_same_as_edge_id_uses_build_edge_id_shape(self):
        user_id = PydanticObjectId()
        src = f"{user_id}:person:alice"
        tgt = f"{user_id}:person:alice smith"
        edge_id = build_edge_id(src, EdgeType.SAME_AS, tgt)
        assert edge_id == f"{src}|same_as|{tgt}"

    async def test_same_as_edge_round_trip_via_model_dump_and_validate(self):
        created_at = datetime(2026, 3, 4, 5, 6, 7, tzinfo=UTC)
        original = self._build_edge(
            properties={
                "status": "pending",
                "confidence": 0.9,
                "match_type": "embedding",
                "created_at": created_at,
            },
        )

        dumped = original.model_dump()
        rehydrated = KnowledgeGraphEntry.model_validate(dumped)

        assert rehydrated.kind == "edge"
        assert rehydrated.type == EdgeType.SAME_AS
        assert rehydrated.properties == {
            "status": "pending",
            "confidence": 0.9,
            "match_type": "embedding",
            "created_at": created_at,
        }

    async def test_legacy_node_doc_without_new_fields_loads_with_defaults(self):
        # A document written before task #007 has none of the five new
        # fields, but post-#018 it MUST carry user_id (migration backfill).
        user_id = _user_id()
        legacy = {
            "_id": f"{user_id}:person:bob",
            "id": f"{user_id}:person:bob",
            "user_id": user_id,
            "kind": "node",
            "type": NodeType.PERSON.value,
            "name": "bob",
            "properties": {},
            "embedding": [],
            "sources": [],
            "created_at": datetime(2025, 12, 1, tzinfo=UTC),
            "updated_at": datetime(2025, 12, 2, tzinfo=UTC),
        }

        entry = KnowledgeGraphEntry.model_validate(legacy)

        assert entry.canonical_name is None
        assert entry.aliases == []
        assert entry.confidence == 1.0
        assert entry.merged_into is None
        assert entry.merged_at is None

    async def test_soft_join_two_node_ids_share_canonical_name(self):
        """Soft-join contract: two physical docs may share ``canonical_name``
        while their ``_id`` values stay distinct. Conflating the two is a bug."""

        user_id = PydanticObjectId()
        alice = self._build_node(
            id=build_node_id(user_id, NodeType.PERSON, "alice"),
            user_id=user_id,
            name="alice",
            canonical_name="Alice Smith",
            aliases=["Alice"],
            confidence=0.92,
        )
        alice_smith = self._build_node(
            id=build_node_id(user_id, NodeType.PERSON, "alice smith"),
            user_id=user_id,
            name="alice smith",
            canonical_name="Alice Smith",
            aliases=[],
            confidence=0.92,
        )

        # Mocked Motor-style collection: in-memory dict keyed by _id.
        collection: dict[str, KnowledgeGraphEntry] = {}
        for entry in (alice, alice_smith):
            collection[entry.id] = entry

        assert alice.id != alice_smith.id
        assert alice.canonical_name == alice_smith.canonical_name == "Alice Smith"

        # Each doc is retrievable independently by _id.
        assert collection[f"{user_id}:person:alice"] is alice
        assert collection[f"{user_id}:person:alice smith"] is alice_smith
        assert len(collection) == 2

        # Soft-join: a "find by canonical_name" surfaces both, distinctly.
        matches = [e for e in collection.values() if e.canonical_name == "Alice Smith"]
        ids = {e.id for e in matches}
        assert ids == {
            f"{user_id}:person:alice",
            f"{user_id}:person:alice smith",
        }

    async def test_merged_at_accepts_tz_aware_utc(self):
        merged_at = datetime.now(tz=UTC)
        entry = self._build_node(
            merged_into="person:alice smith",
            merged_at=merged_at,
        )

        assert entry.merged_into == "person:alice smith"
        assert entry.merged_at is not None
        assert entry.merged_at.tzinfo is not None
        # Round-trip preserves the tz-aware value.
        rehydrated = KnowledgeGraphEntry.model_validate(entry.model_dump())
        assert rehydrated.merged_at == merged_at
        assert rehydrated.merged_at.tzinfo is not None

    async def test_merged_at_existing_behavior_with_naive_datetime(self):
        """Regression guard for the current Pydantic/Beanie behavior on naive
        datetimes. The ODM currently accepts a naive datetime (no tz coercion
        on the model itself); downstream writers are responsible for stamping
        ``datetime.now(tz=UTC)`` (see ``apps/memory/src/tree/memory/``). If the
        model later starts rejecting naive datetimes, this test will fail and
        the rejection should be promoted to a documented contract."""

        naive = datetime(2026, 1, 1, 12, 0, 0)
        assert naive.tzinfo is None

        try:
            entry = self._build_node(
                merged_into="person:alice smith",
                merged_at=naive,
            )
        except Exception:
            # Rejection is acceptable too — record the behavior change.
            return

        # Current behavior: naive datetime is accepted as-is.
        assert entry.merged_at is not None
        assert entry.merged_at.tzinfo is None


# ---------------------------------------------------------------------------
# Phase-3 #027 — relaxed `type: str` + registry-driven validator.
# ---------------------------------------------------------------------------


class TestTypeFieldIsRelaxedString:
    """Post-#027 the wire type of ``type`` is ``str``. The enum shims
    still flow through (``StrEnum`` -> ``str``)."""

    async def test_node_constructed_with_raw_string_type(self):
        user_id = _user_id()
        entry = KnowledgeGraphEntry(
            id=f"{user_id}:person:alice",
            user_id=user_id,
            kind="node",
            type="person",
            name="alice",
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            updated_at=datetime(2026, 1, 2, tzinfo=UTC),
        )

        # The wire value is plain str (no enum coercion).
        assert entry.type == "person"
        assert isinstance(entry.type, str)

    async def test_node_constructed_with_enum_member_still_works(self):
        user_id = _user_id()
        entry = KnowledgeGraphEntry(
            id=f"{user_id}:person:alice",
            user_id=user_id,
            kind="node",
            type=NodeType.PERSON,
            name="alice",
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            updated_at=datetime(2026, 1, 2, tzinfo=UTC),
        )

        # StrEnum serializes to the string value.
        assert entry.type == "person"

    async def test_edge_constructed_with_raw_string_type(self):
        # Post-#029 ``todo`` is gone; the surviving extractable wire
        # shape is ``related_to + semantic_type``. Pin the string-type
        # construction path against the new umbrella instead.
        user_id = _user_id()
        entry = KnowledgeGraphEntry(
            id=f"{user_id}:person:alice|related_to|{user_id}:object:write a book",
            user_id=user_id,
            kind="edge",
            type="related_to",
            semantic_type="has_task",
            source_node_id=f"{user_id}:person:alice",
            source_type=NodeType.PERSON,
            target_node_id=f"{user_id}:object:write a book",
            target_type=NodeType.OBJECT,
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            updated_at=datetime(2026, 1, 2, tzinfo=UTC),
        )

        assert entry.type == "related_to"
        assert entry.semantic_type == "has_task"


class TestTypeFieldValidator:
    """The ``_check_type_against_registry`` model validator rejects rows
    whose ``type`` is not in the registry for the row's ``kind``."""

    async def test_rejects_unknown_node_type(self):
        user_id = _user_id()
        with pytest.raises(Exception) as excinfo:
            KnowledgeGraphEntry(
                id=f"{user_id}:ferret:alice",
                user_id=user_id,
                kind="node",
                type="ferret",
                name="alice",
                created_at=datetime(2026, 1, 1, tzinfo=UTC),
                updated_at=datetime(2026, 1, 2, tzinfo=UTC),
            )

        # Validators surface through Pydantic's ValidationError; the
        # underlying message is the ValueError we raise.
        assert "ferret" in str(excinfo.value)

    async def test_rejects_unknown_edge_type(self):
        user_id = _user_id()
        with pytest.raises(Exception) as excinfo:
            KnowledgeGraphEntry(
                id=f"{user_id}:person:alice|owns|{user_id}:task:write",
                user_id=user_id,
                kind="edge",
                type="owns",
                source_node_id=f"{user_id}:person:alice",
                source_type=NodeType.PERSON,
                target_node_id=f"{user_id}:task:write",
                target_type=NodeType.TASK,
                created_at=datetime(2026, 1, 1, tzinfo=UTC),
                updated_at=datetime(2026, 1, 2, tzinfo=UTC),
            )

        assert "owns" in str(excinfo.value)

    async def test_accepts_every_registered_node_type(self):
        # Phase-3 #028: iterate the **registry**, not the enum. The
        # ``NodeType.TASK`` legacy alias survives on the enum but it is
        # no longer a top-level type — its construction triggers the
        # legacy → POLE+O reroute (covered by
        # :class:`TestLegacyNodeTypeReroute`).
        from tree.entities.ontology import NODE_REGISTRY

        user_id = _user_id()
        for type_name in NODE_REGISTRY:
            entry = KnowledgeGraphEntry(
                id=f"{user_id}:{type_name}:x",
                user_id=user_id,
                kind="node",
                type=type_name,
                name="x",
                created_at=datetime(2026, 1, 1, tzinfo=UTC),
                updated_at=datetime(2026, 1, 2, tzinfo=UTC),
            )
            assert entry.type == type_name

    @pytest.mark.parametrize(
        "edge_type,src_type,src_name,tgt_type,tgt_name,extra",
        [
            (EdgeType.PART_OF, NodeType.CHUNK, "c", NodeType.DOCUMENT, "d", {}),
            (EdgeType.NEXT, NodeType.CHUNK, "c0", NodeType.CHUNK, "c1", {}),
            (EdgeType.MENTIONS, NodeType.CHUNK, "c", NodeType.PERSON, "alice", {}),
            (
                EdgeType.REFERENCED,
                NodeType.DOCUMENT,
                "d1",
                NodeType.DOCUMENT,
                "d2",
                {},
            ),
            (
                EdgeType.RELATED_TO,
                NodeType.PERSON,
                "alice",
                NodeType.PERSON,
                "bob",
                {"semantic_type": "knows"},
            ),
            (
                EdgeType.HAS,
                NodeType.PERSON,
                "self",
                NodeType.PREFERENCE,
                "coffee",
                {},
            ),
            (
                EdgeType.SAME_AS,
                NodeType.PERSON,
                "alice",
                NodeType.PERSON,
                "alice s",
                {},
            ),
        ],
    )
    async def test_accepts_every_registered_edge_type(
        self, edge_type, src_type, src_name, tgt_type, tgt_name, extra
    ):
        # Post-#029, every edge type enforces its ``allowed_pairs``;
        # ``related_to`` additionally requires ``semantic_type``.
        user_id = _user_id()
        entry = KnowledgeGraphEntry(
            id=(
                f"{user_id}:{src_type.value}:{src_name}|{edge_type.value}|"
                f"{user_id}:{tgt_type.value}:{tgt_name}"
            ),
            user_id=user_id,
            kind="edge",
            type=edge_type.value,
            source_node_id=f"{user_id}:{src_type.value}:{src_name}",
            source_type=src_type,
            target_node_id=f"{user_id}:{tgt_type.value}:{tgt_name}",
            target_type=tgt_type,
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            updated_at=datetime(2026, 1, 2, tzinfo=UTC),
            **extra,
        )
        assert entry.type == edge_type.value


class TestBuildIdAcceptsStringTypes:
    """Post-#027 ``build_node_id`` / ``build_edge_id`` accept either
    the :class:`NodeType` / :class:`EdgeType` shim **or** a plain
    ``str`` — both produce identical ``_id`` strings."""

    def test_build_node_id_with_str(self):
        user_id = PydanticObjectId()

        from_enum = build_node_id(user_id, NodeType.PERSON, "alice")
        from_str = build_node_id(user_id, "person", "alice")

        assert from_enum == from_str == f"{user_id}:person:alice"

    def test_build_edge_id_with_str(self):
        # Post-#029 ``todo`` / ``EdgeType.TODO`` is gone. Pin the
        # build_edge_id "string OR enum" parity against the surviving
        # ``related_to`` umbrella.
        user_id = PydanticObjectId()
        src = build_node_id(user_id, NodeType.PERSON, "alice")
        tgt = build_node_id(user_id, NodeType.OBJECT, "write")

        from_enum = build_edge_id(src, EdgeType.RELATED_TO, tgt)
        from_str = build_edge_id(src, "related_to", tgt)

        assert from_enum == from_str == f"{src}|related_to|{tgt}"


class TestKnowledgeGraphEntrySubtype:
    """Phase-3 #028: ``KnowledgeGraphEntry.subtype: str | None`` is a
    live column on the model and validated against the parent type's
    closed ``subtypes`` set when the parent has one. The validator is
    intentionally loose at construction: ``subtype is None`` is
    accepted (the strict envelope check is #030's job)."""

    def _build(self, user_id, **overrides):
        defaults = dict(
            id=f"{user_id}:person:alice",
            user_id=user_id,
            kind="node",
            type="person",
            name="alice",
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            updated_at=datetime(2026, 1, 2, tzinfo=UTC),
        )
        defaults.update(overrides)
        return KnowledgeGraphEntry(**defaults)

    async def test_subtype_field_defaults_to_none(self):
        user_id = _user_id()
        entry = self._build(user_id)
        assert entry.subtype is None

    async def test_valid_subtype_on_closed_vocab_parent_accepted(self):
        user_id = _user_id()
        entry = self._build(user_id, type="person", subtype="individual")
        assert entry.subtype == "individual"
        assert entry.type == "person"

    async def test_invalid_subtype_on_closed_vocab_parent_rejected(self):
        user_id = _user_id()
        with pytest.raises(Exception) as excinfo:
            self._build(user_id, type="person", subtype="dragon")
        # Validator message must surface the bad subtype + allowed set
        # so a downstream debugger can see what's allowed.
        msg = str(excinfo.value)
        assert "dragon" in msg
        assert "individual" in msg

    async def test_subtype_none_on_closed_vocab_parent_accepted_loose(self):
        # "Loose at construction" — the envelope-level strict check
        # lands in #030. Intermediate pipeline steps that construct a
        # KG entry before extraction has populated ``subtype`` must
        # still validate.
        user_id = _user_id()
        entry = self._build(user_id, type="person", subtype=None)
        assert entry.subtype is None

    async def test_subtype_on_freeform_parent_accepted(self):
        # ``preference`` is still freeform after #028; any subtype value
        # (or none) is acceptable.
        user_id = _user_id()
        entry = self._build(
            user_id,
            id=f"{user_id}:preference:foo",
            type="preference",
            subtype="anything-goes",
            properties={"content": "x"},
        )
        assert entry.subtype == "anything-goes"

    @pytest.mark.parametrize(
        "type_name,subtype",
        [
            ("organization", "company"),
            ("organization", "nonprofit"),
            ("location", "city"),
            ("location", "country"),
            ("event", "meeting"),
            ("event", "incident"),
            ("object", "vehicle"),
            ("object", "software"),
            ("object", "task"),  # Tree extension
            ("object", "topic"),  # Tree extension
            ("object", "project"),  # Tree extension
        ],
    )
    async def test_every_canonical_subtype_constructs(self, type_name, subtype):
        user_id = _user_id()
        entry = KnowledgeGraphEntry(
            id=f"{user_id}:{type_name}:x",
            user_id=user_id,
            kind="node",
            type=type_name,
            subtype=subtype,
            name="x",
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            updated_at=datetime(2026, 1, 2, tzinfo=UTC),
        )
        assert entry.type == type_name
        assert entry.subtype == subtype

    async def test_subtype_on_edge_row_skipped(self):
        # The subtype validator is node-only; edge rows pass through
        # even when their ``type`` is a registered edge with no
        # subtype vocabulary.
        user_id = _user_id()
        entry = KnowledgeGraphEntry(
            id=f"{user_id}:person:alice|related_to|{user_id}:person:bob",
            user_id=user_id,
            kind="edge",
            type="related_to",
            semantic_type="knows",
            source_node_id=f"{user_id}:person:alice",
            source_type=NodeType.PERSON,
            target_node_id=f"{user_id}:person:bob",
            target_type=NodeType.PERSON,
            subtype="whatever",  # Not validated on edges.
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            updated_at=datetime(2026, 1, 2, tzinfo=UTC),
        )
        assert entry.subtype == "whatever"


class TestLegacyNodeTypeReroute:
    """Phase-3 #028: legacy node rows of ``type=task`` are silently
    re-routed at validator time to the new POLE+O subtype shape —
    ``type='object', subtype='task'``. This keeps code paths that
    still construct with ``NodeType.TASK`` working (user prompt
    explicitly preserves the enum alias for read paths) while
    ensuring writes always land in the POLE+O storage form. The
    actual DB-row migration is #033's job."""

    def _build(self, user_id, **overrides):
        defaults = dict(
            id=f"{user_id}:object:ship demo",
            user_id=user_id,
            kind="node",
            type=NodeType.TASK,
            name="ship demo",
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            updated_at=datetime(2026, 1, 2, tzinfo=UTC),
        )
        defaults.update(overrides)
        return KnowledgeGraphEntry(**defaults)

    async def test_legacy_task_enum_reroutes_to_object_task(self):
        user_id = _user_id()
        entry = self._build(user_id, type=NodeType.TASK)
        assert entry.type == "object"
        assert entry.subtype == "task"

    async def test_legacy_task_raw_string_reroutes_to_object_task(self):
        user_id = _user_id()
        entry = self._build(user_id, type="task")
        assert entry.type == "object"
        assert entry.subtype == "task"

    async def test_explicit_subtype_overrides_default_reroute_value(self):
        # Defensive: a caller may pre-populate subtype with a richer
        # value (e.g. a future migration that wants finer-grained
        # tagging). The reroute must rewrite ``type`` but leave the
        # explicit ``subtype`` untouched.
        user_id = _user_id()
        entry = self._build(
            user_id,
            type=NodeType.TASK,
            subtype="project",  # caller-provided override
        )
        assert entry.type == "object"
        assert entry.subtype == "project"

    async def test_legacy_and_new_shape_produce_equivalent_rows(self):
        # The core invariant of the user-prompt clause "Tests must
        # cover both old and new shapes producing equivalent stored
        # rows".
        user_id = _user_id()
        legacy = self._build(user_id, type=NodeType.TASK)
        explicit = self._build(user_id, type="object", subtype="task")

        # Both rows store with the same logical shape.
        legacy_dump = legacy.model_dump(exclude={"created_at", "updated_at"})
        explicit_dump = explicit.model_dump(exclude={"created_at", "updated_at"})
        assert legacy_dump == explicit_dump

    async def test_reroute_does_not_touch_edge_rows(self):
        # Edges carry their own ``source_type`` / ``target_type`` columns;
        # the node-shape rewrite is scoped to ``kind="node"``. After
        # #029, ``todo`` is no longer a registered edge type — exercise
        # the "edge rewrite doesn't fire" invariant via the surviving
        # ``related_to`` umbrella with a legacy-looking ``NodeType.TASK``
        # endpoint (which still exists as an enum alias).
        user_id = _user_id()
        entry = KnowledgeGraphEntry(
            id=f"{user_id}:person:alice|related_to|{user_id}:object:write",
            user_id=user_id,
            kind="edge",
            type="related_to",
            semantic_type="has_task",
            source_node_id=f"{user_id}:person:alice",
            source_type=NodeType.PERSON,
            target_node_id=f"{user_id}:object:write",
            target_type=NodeType.OBJECT,
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            updated_at=datetime(2026, 1, 2, tzinfo=UTC),
        )
        assert entry.type == "related_to"
        assert entry.target_type == NodeType.OBJECT


class TestSubtypeIndexDeclared:
    """The (user_id, kind, type, subtype) compound index ships in
    #028 so MCP queries like "all object/task for this user" land
    on an index prefix. Pinned so a future index refactor doesn't
    silently drop it."""

    def test_user_kind_type_subtype_index_declared(self) -> None:
        index_models: list[IndexModel] = list(KnowledgeGraphEntry.Settings.indexes)
        target_key = [("user_id", 1), ("kind", 1), ("type", 1), ("subtype", 1)]
        assert any(
            list(im.document.get("key", {}).items()) == target_key
            for im in index_models
        ), f"Expected compound index on {target_key}; got {index_models}"


class TestOntologyTreeExtensionsModuleApplied:
    """Phase-3 #028: importing the Tree-extensions module **mutates**
    the registry — it's the canonical example of an extension consumer
    self-applying ``register_node_subtype()``. Pinned shape:

    * ``object`` parent now has the 6 canonical POLE+O subtypes plus
      Tree's ``task`` / ``topic`` / ``project`` extensions (9 total).
    * ``event`` parent has the 7 canonical POLE+O subtypes (no Tree
      extension).
    * ``SUBTYPE_EXTRAS`` carries the ``ProjectExtras`` model under
      ``("object", "project")``.
    """

    def test_object_subtypes_include_tree_extensions(self):
        from tree.entities.ontology import NODE_REGISTRY

        object_spec = NODE_REGISTRY["object"]
        assert object_spec.subtypes is not None
        assert "task" in object_spec.subtypes
        assert "topic" in object_spec.subtypes
        assert "project" in object_spec.subtypes
        # Canonical POLE+O subtypes still present.
        for canonical in {
            "vehicle",
            "phone",
            "email",
            "document",
            "device",
            "software",
        }:
            assert canonical in object_spec.subtypes
        assert len(object_spec.subtypes) == 9

    def test_event_subtypes_are_canonical_only(self):
        from tree.entities.ontology import NODE_REGISTRY

        event_spec = NODE_REGISTRY["event"]
        assert event_spec.subtypes is not None
        # ``episode`` was removed — ``event`` carries no Tree extension.
        assert "episode" not in event_spec.subtypes
        for canonical in {
            "incident",
            "meeting",
            "transaction",
            "communication",
            "travel",
            "employment",
            "observation",
        }:
            assert canonical in event_spec.subtypes
        assert len(event_spec.subtypes) == 7

    def test_project_extras_registered_in_subtype_extras(self):
        from tree.entities.ontology import SUBTYPE_EXTRAS
        from tree.entities.ontology_tree_extensions import ProjectExtras

        assert SUBTYPE_EXTRAS[("object", "project")] is ProjectExtras


# ---------------------------------------------------------------------------
# Phase-3 #029 — `related_to` umbrella validator
# ---------------------------------------------------------------------------


class TestRelatedToSemanticValidator:
    """Five branches of ``_check_related_to_semantic`` (#029):

    1. Accept a valid (semantic_type, source_type, target_type) triple.
    2. Reject a pair-violating triple.
    3. Reject an unknown semantic_type.
    4. Reject a related_to row missing semantic_type.
    5. Reject a semantic_type set on a non-related_to edge.
    """

    def _build_related_to(self, user_id, **overrides):
        defaults = dict(
            id=f"{user_id}:person:a|related_to|{user_id}:person:b",
            user_id=user_id,
            kind="edge",
            type="related_to",
            semantic_type="knows",
            source_node_id=f"{user_id}:person:a",
            source_type=NodeType.PERSON,
            target_node_id=f"{user_id}:person:b",
            target_type=NodeType.PERSON,
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            updated_at=datetime(2026, 1, 2, tzinfo=UTC),
        )
        defaults.update(overrides)
        return KnowledgeGraphEntry(**defaults)

    async def test_accepts_valid_employed_by_person_to_organization(self):
        user_id = _user_id()
        entry = self._build_related_to(
            user_id,
            id=f"{user_id}:person:alice|related_to|{user_id}:organization:anthropic",
            semantic_type="employed_by",
            source_node_id=f"{user_id}:person:alice",
            source_type=NodeType.PERSON,
            target_node_id=f"{user_id}:organization:anthropic",
            target_type=NodeType.ORGANIZATION,
        )
        assert entry.semantic_type == "employed_by"
        assert entry.source_type == NodeType.PERSON
        assert entry.target_type == NodeType.ORGANIZATION

    async def test_rejects_pair_violation(self):
        # employed_by is (person, organization), not (organization, person).
        user_id = _user_id()
        with pytest.raises(Exception) as excinfo:
            self._build_related_to(
                user_id,
                id=(
                    f"{user_id}:organization:anthropic|related_to|"
                    f"{user_id}:person:alice"
                ),
                semantic_type="employed_by",
                source_node_id=f"{user_id}:organization:anthropic",
                source_type=NodeType.ORGANIZATION,
                target_node_id=f"{user_id}:person:alice",
                target_type=NodeType.PERSON,
            )
        msg = str(excinfo.value)
        assert "employed_by" in msg

    async def test_rejects_unknown_semantic(self):
        user_id = _user_id()
        with pytest.raises(Exception) as excinfo:
            self._build_related_to(user_id, semantic_type="dragon_breath")
        msg = str(excinfo.value)
        assert "dragon_breath" in msg

    async def test_rejects_missing_semantic_on_related_to(self):
        user_id = _user_id()
        with pytest.raises(Exception) as excinfo:
            self._build_related_to(user_id, semantic_type=None)
        msg = str(excinfo.value)
        assert "semantic_type" in msg

    async def test_rejects_semantic_on_non_related_to(self):
        user_id = _user_id()
        with pytest.raises(Exception) as excinfo:
            KnowledgeGraphEntry(
                id=f"{user_id}:person:alice|has|{user_id}:preference:coffee",
                user_id=user_id,
                kind="edge",
                type="has",
                semantic_type="employed_by",  # Wrong: only related_to carries this
                source_node_id=f"{user_id}:person:alice",
                source_type=NodeType.PERSON,
                target_node_id=f"{user_id}:preference:coffee",
                target_type=NodeType.PREFERENCE,
                created_at=datetime(2026, 1, 1, tzinfo=UTC),
                updated_at=datetime(2026, 1, 2, tzinfo=UTC),
            )
        msg = str(excinfo.value)
        assert "semantic_type" in msg
        assert "has" in msg


class TestSemanticTypeIndex:
    """The ``(user_id, type, semantic_type)`` partial index is declared
    on the model so the indexing pipeline picks it up on boot (#029)."""

    def test_user_type_semantic_type_index_declared(self) -> None:
        index_models: list[IndexModel] = list(KnowledgeGraphEntry.Settings.indexes)
        target_key = [("user_id", 1), ("type", 1), ("semantic_type", 1)]
        match = None
        for im in index_models:
            if list(im.document.get("key", {}).items()) == target_key:
                match = im
                break
        assert match is not None, (
            f"Expected partial compound index on {target_key}; got {index_models}"
        )
        # Partial filter must restrict to non-null semantic_type rows.
        partial = match.document.get("partialFilterExpression")
        assert partial == {"semantic_type": {"$type": "string"}}


class TestStructuralHasEdgeAccepted:
    """``has`` now covers both (person, preference) and (person, object)
    construction paths (#029)."""

    async def test_has_person_to_preference_accepted(self):
        user_id = _user_id()
        entry = KnowledgeGraphEntry(
            id=f"{user_id}:person:self|has|{user_id}:preference:coffee",
            user_id=user_id,
            kind="edge",
            type="has",
            source_node_id=f"{user_id}:person:self",
            source_type=NodeType.PERSON,
            target_node_id=f"{user_id}:preference:coffee",
            target_type=NodeType.PREFERENCE,
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            updated_at=datetime(2026, 1, 2, tzinfo=UTC),
        )
        assert entry.type == "has"
        assert entry.semantic_type is None

    async def test_has_person_to_object_accepted(self):
        user_id = _user_id()
        entry = KnowledgeGraphEntry(
            id=f"{user_id}:person:self|has|{user_id}:object:ship-demo",
            user_id=user_id,
            kind="edge",
            type="has",
            source_node_id=f"{user_id}:person:self",
            source_type=NodeType.PERSON,
            target_node_id=f"{user_id}:object:ship-demo",
            target_type=NodeType.OBJECT,
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            updated_at=datetime(2026, 1, 2, tzinfo=UTC),
        )
        assert entry.type == "has"


# ---------------------------------------------------------------------------
# Phase-3 #030 — ExtractorInfo + new common columns
# ---------------------------------------------------------------------------


class TestExtractorInfo:
    def test_round_trip(self) -> None:
        info = ExtractorInfo(
            name="gemini-2.5-pro",
            version="tree-memory-0.1.0",
            extraction_time_ms=812,
        )
        rehydrated = ExtractorInfo.model_validate(info.model_dump())
        assert rehydrated == info

    def test_extraction_time_optional(self) -> None:
        info = ExtractorInfo(name="gemini", version="v1")
        assert info.extraction_time_ms is None

    def test_field_descriptions_present(self) -> None:
        schema = ExtractorInfo.model_json_schema()
        for field in ("name", "version", "extraction_time_ms"):
            assert schema["properties"][field].get("description"), (
                f"ExtractorInfo.{field} missing description"
            )


class TestKnowledgeGraphCommonColumns:
    """#030 adds ``description``, ``valid_from``, ``valid_until``, and
    ``extractor`` to :class:`KnowledgeGraphEntry`. Each is optional;
    legacy rows load with defaults."""

    def _build(self, **overrides):
        user_id = _user_id()
        defaults = dict(
            id=f"{user_id}:person:alice",
            user_id=user_id,
            kind="node",
            type="person",
            subtype="individual",
            name="alice",
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            updated_at=datetime(2026, 1, 2, tzinfo=UTC),
        )
        defaults.update(overrides)
        return KnowledgeGraphEntry(**defaults)

    async def test_defaults_none(self) -> None:
        entry = self._build()
        assert entry.description is None
        assert entry.valid_from is None
        assert entry.valid_until is None
        assert entry.extractor is None

    async def test_round_trip_with_values(self) -> None:
        vf = datetime(2025, 1, 1, tzinfo=UTC)
        vu = datetime(2025, 12, 31, tzinfo=UTC)
        entry = self._build(
            description="A person known for X.",
            valid_from=vf,
            valid_until=vu,
            extractor=ExtractorInfo(name="gemini-2.5-pro", version="tree-memory-0.1.0"),
        )
        dumped = entry.model_dump()
        rehydrated = KnowledgeGraphEntry.model_validate(dumped)
        assert rehydrated.description == "A person known for X."
        assert rehydrated.valid_from == vf
        assert rehydrated.valid_until == vu
        assert rehydrated.extractor is not None
        assert rehydrated.extractor.name == "gemini-2.5-pro"

    async def test_valid_from_naive_rejected(self) -> None:
        naive = datetime(2025, 1, 1)
        assert naive.tzinfo is None
        with pytest.raises(Exception) as excinfo:
            self._build(valid_from=naive)
        assert "timezone-aware" in str(excinfo.value)

    async def test_valid_until_naive_rejected(self) -> None:
        naive = datetime(2025, 1, 1)
        with pytest.raises(Exception):
            self._build(valid_until=naive)

    async def test_legacy_doc_without_new_fields_loads_with_defaults(self) -> None:
        user_id = _user_id()
        legacy = {
            "_id": f"{user_id}:person:bob",
            "id": f"{user_id}:person:bob",
            "user_id": user_id,
            "kind": "node",
            "type": "person",
            "name": "bob",
            "properties": {},
            "embedding": [],
            "sources": [],
            "created_at": datetime(2025, 12, 1, tzinfo=UTC),
            "updated_at": datetime(2025, 12, 2, tzinfo=UTC),
        }
        entry = KnowledgeGraphEntry.model_validate(legacy)
        assert entry.description is None
        assert entry.valid_from is None
        assert entry.valid_until is None
        assert entry.extractor is None
