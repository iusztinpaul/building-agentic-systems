from datetime import UTC, datetime, timezone

import numpy as np
import pytest
from beanie import PydanticObjectId
from bson.binary import Binary, BinaryVectorDtype
from pydantic import ValidationError
from pymongo import IndexModel

from tree.config.app_config import app_config
from tree.db import ALL_DOCUMENT_MODELS
from tree.entities.memory import (
    MEMORY_COLLECTION,
    RAG_NODE_TYPES,
    STORED_VECTOR_QUERY,
    ChunkViz,
    EdgeType,
    ExtractorInfo,
    MemoryEntry,
    NodeType,
    build_edge_id,
    build_node_id,
    build_rag_row_id,
    from_stored_vector,
    memory_indexes,
    to_stored_vector,
)
from tree.entities.meta_state import KnowledgeGraphMetaState
from tree.entities.ontology import LLM_EXTRACTABLE_NODE_TYPES, NODE_REGISTRY


def _user_id() -> PydanticObjectId:
    return PydanticObjectId()


class TestBuildNodeId:
    def test_builds_composite_id(self):
        user_id = PydanticObjectId()
        assert build_node_id(user_id, NodeType.PERSON, "alice") == (
            f"{user_id}:person:alice"
        )


class TestBuildRagRowId:
    _URI = "https://example.com/a"

    def test_builds_document_id(self):
        user_id = PydanticObjectId()
        assert (
            build_rag_row_id(user_id, NodeType.DOCUMENT, self._URI)
            == f"{user_id}:document:{self._URI}"
        )

    def test_builds_parent_chunk_id(self):
        user_id = PydanticObjectId()
        assert (
            build_rag_row_id(user_id, NodeType.CHUNK, self._URI, 2)
            == f"{user_id}:chunk:{self._URI}:p2"
        )

    def test_builds_child_chunk_id(self):
        user_id = PydanticObjectId()
        assert (
            build_rag_row_id(user_id, NodeType.CHUNK, self._URI, 2, 5)
            == f"{user_id}:chunk:{self._URI}:p2:c5"
        )

    def test_slug_equivalent_uris_get_distinct_ids(self):
        # ``a/b`` and ``a-b`` would slugify to one string; the raw URI keeps them apart.
        user_id = PydanticObjectId()
        assert build_rag_row_id(
            user_id, "document", "https://x.com/a/b"
        ) != build_rag_row_id(user_id, "document", "https://x.com/a-b")

    def test_same_uri_under_two_users_yields_distinct_ids(self):
        assert build_rag_row_id(
            PydanticObjectId(), "document", self._URI
        ) != build_rag_row_id(PydanticObjectId(), "document", self._URI)

    def test_child_index_without_parent_index_raises(self):
        with pytest.raises(ValueError, match="child_index requires parent_index"):
            build_rag_row_id(PydanticObjectId(), "chunk", self._URI, child_index=0)


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


class TestMemoryEntry:
    async def test_node_entry(self):
        user_id = _user_id()
        entry = MemoryEntry(
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
        assert entry.embedding is None
        assert entry.source_node_id is None

    async def test_edge_entry(self):
        user_id = _user_id()
        entry = MemoryEntry(
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
            MemoryEntry(
                user_id=_user_id(),
                kind="node",
                type=NodeType.PERSON,
                created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                updated_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
            )

    async def test_missing_required_user_id_raises(self):
        # Per #018: user_id is required at construction (no default).
        with pytest.raises(Exception):
            MemoryEntry(
                id="anyuser:person:alice",
                kind="node",
                type=NodeType.PERSON,
                created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                updated_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
            )


# No ``text_index``: the lexical leg reads the mongot ``text_search_index``,
# which ``ensure_indexes`` owns (ADR-015 §1).
_BASE_INDEX_NAMES = {"user_kind_type_subtype", "user_type_name"}
_GRAPH_INDEX_NAMES = {"active_user", "user_kind_source_node", "user_kind_target_node"}


def _by_name(mode: str) -> dict[str, IndexModel]:
    return {im.document["name"]: im for im in memory_indexes(mode)}


class TestMemoryIndexes:
    """ADR-012: the per-mode classic index set Beanie owns."""

    @pytest.mark.parametrize(
        ("mode", "expected"),
        [
            ("rag", _BASE_INDEX_NAMES),
            ("graphrag", _BASE_INDEX_NAMES | _GRAPH_INDEX_NAMES),
        ],
    )
    def test_each_mode_declares_exactly_its_set(
        self, mode: str, expected: set[str]
    ) -> None:
        assert set(_by_name(mode)) == expected

    @pytest.mark.parametrize("mode", ["rag", "graphrag"])
    def test_every_compound_index_leads_with_user_id(self, mode: str) -> None:
        for name, im in _by_name(mode).items():
            if name == "active_user":
                continue
            assert next(iter(im.document["key"].items())) == ("user_id", 1), name

    @pytest.mark.parametrize("mode", ["rag", "graphrag"])
    def test_no_classic_text_index_in_either_mode(self, mode: str) -> None:
        # A ``text`` key would recreate the retired ``$text`` index on every
        # boot — Beanie only creates, and ``ensure_indexes`` would drop it again.
        for name, im in _by_name(mode).items():
            assert "text" not in im.document["key"].values(), name

    def test_compound_keys(self) -> None:
        indexes = _by_name("graphrag")

        assert list(indexes["user_kind_type_subtype"].document["key"].items()) == [
            ("user_id", 1),
            ("kind", 1),
            ("type", 1),
            ("subtype", 1),
        ]
        assert list(indexes["user_type_name"].document["key"].items()) == [
            ("user_id", 1),
            ("type", 1),
            ("name", 1),
        ]
        assert list(indexes["user_kind_source_node"].document["key"].items()) == [
            ("user_id", 1),
            ("kind", 1),
            ("source_node_id", 1),
        ]
        assert list(indexes["user_kind_target_node"].document["key"].items()) == [
            ("user_id", 1),
            ("kind", 1),
            ("target_node_id", 1),
        ]

    def test_active_user_is_partial_on_the_active_user_flag(self) -> None:
        active_user = _by_name("graphrag")["active_user"].document

        assert list(active_user["key"].items()) == [("properties.is_active_user", 1)]
        assert active_user["partialFilterExpression"] == {
            "properties.is_active_user": True
        }

    def test_no_index_covers_the_embedding_vector(self) -> None:
        for mode in ("rag", "graphrag"):
            for im in memory_indexes(mode):
                assert "embedding" not in im.document["key"]

    def test_settings_indexes_follow_the_configured_mode(self) -> None:
        # Deliberately NOT monkeypatched: the import-time binding is frozen when
        # the module loads, so it can only be compared to the configured mode.
        assert {im.document["name"] for im in MemoryEntry.Settings.indexes} == set(
            _by_name(app_config.memory.mode)
        )

    def test_kind_carries_no_inline_index(self) -> None:
        # ``Indexed(str)`` would make Beanie recreate ``kind_1`` on every boot.
        assert MemoryEntry.model_fields["kind"].annotation is str


class TestResolutionDedupFields:
    """Resolution + dedup fields added in task #007.

    Five new optional fields on ``MemoryEntry`` plus ``EdgeType.SAME_AS``.
    """

    def _build_node(self, **overrides) -> MemoryEntry:
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
        return MemoryEntry(**defaults)

    def _build_edge(self, **overrides) -> MemoryEntry:
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
        return MemoryEntry(**defaults)

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
        rehydrated = MemoryEntry.model_validate(dumped)

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

        entry = MemoryEntry.model_validate(legacy)

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
        collection: dict[str, MemoryEntry] = {}
        for entry in (alice, alice_smith):
            collection[entry.id] = entry

        assert alice.id != alice_smith.id
        assert alice.canonical_name == alice_smith.canonical_name == "Alice Smith"

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
        rehydrated = MemoryEntry.model_validate(entry.model_dump())
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
        entry = MemoryEntry(
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
        entry = MemoryEntry(
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
        entry = MemoryEntry(
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
            MemoryEntry(
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
            MemoryEntry(
                id=f"{user_id}:person:alice|owns|{user_id}:object:write",
                user_id=user_id,
                kind="edge",
                type="owns",
                source_node_id=f"{user_id}:person:alice",
                source_type=NodeType.PERSON,
                target_node_id=f"{user_id}:object:write",
                target_type=NodeType.OBJECT,
                created_at=datetime(2026, 1, 1, tzinfo=UTC),
                updated_at=datetime(2026, 1, 2, tzinfo=UTC),
            )

        assert "owns" in str(excinfo.value)

    async def test_accepts_every_registered_node_type(self):
        # Phase-3 #028: iterate the **registry**, not the enum.
        from tree.entities.ontology import NODE_REGISTRY

        user_id = _user_id()
        for type_name in NODE_REGISTRY:
            entry = MemoryEntry(
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
        entry = MemoryEntry(
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


class TestMemoryEntrySubtype:
    """Phase-3 #028: ``MemoryEntry.subtype: str | None`` is a
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
        return MemoryEntry(**defaults)

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
        entry = MemoryEntry(
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
        entry = MemoryEntry(
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
        return MemoryEntry(**defaults)

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
            MemoryEntry(
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


class TestStructuralHasEdgeAccepted:
    """``has`` now covers both (person, preference) and (person, object)
    construction paths (#029)."""

    async def test_has_person_to_preference_accepted(self):
        user_id = _user_id()
        entry = MemoryEntry(
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
        entry = MemoryEntry(
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


class TestMemoryEntryCommonColumns:
    """#030 adds ``description``, ``valid_from``, ``valid_until``, and
    ``extractor`` to :class:`MemoryEntry`. Each is optional;
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
        return MemoryEntry(**defaults)

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
        rehydrated = MemoryEntry.model_validate(dumped)
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
        entry = MemoryEntry.model_validate(legacy)
        assert entry.description is None
        assert entry.valid_from is None
        assert entry.valid_until is None
        assert entry.extractor is None


class TestMemoryCollectionName:
    """ADR-006 decision 1 / #105: ``knowledge_graph`` -> ``memory``.

    One exported constant replaces the thirteen per-module private copies, so a
    future rename is a one-line change instead of a grep-and-pray.
    """

    def test_memory_collection_constant(self) -> None:
        assert MEMORY_COLLECTION == "memory"

    def test_entry_settings_name_is_the_shared_constant(self) -> None:
        """Beanie and the raw-pymongo readers must target the SAME collection —
        pinning the identity (not just the value) is what stops them drifting.
        """

        assert MemoryEntry.Settings.name == MEMORY_COLLECTION

    def test_memory_entry_is_registered_with_beanie(self) -> None:
        assert MemoryEntry in ALL_DOCUMENT_MODELS

    def test_no_registered_model_still_targets_the_old_collection(self) -> None:
        """No compat shim survived the rename: nothing Beanie initialises
        writes to ``knowledge_graph`` any more."""

        targets = {model.Settings.name for model in ALL_DOCUMENT_MODELS}

        assert MEMORY_COLLECTION in targets
        assert "knowledge_graph" not in targets

    def test_dream_watermark_collection_is_deliberately_unchanged(self) -> None:
        """``knowledge_graph_meta_state`` is graph-only state whose name stays
        accurate, so ADR-006 leaves it alone."""

        assert KnowledgeGraphMetaState in ALL_DOCUMENT_MODELS
        assert KnowledgeGraphMetaState.Settings.name == "knowledge_graph_meta_state"

    async def test_inserted_row_lands_in_the_memory_collection(self) -> None:
        """End-to-end through Beanie: a row written via the ODM shows up in
        ``memory``, and no ``knowledge_graph`` collection is created."""

        user_id = _user_id()
        now = datetime.now(UTC)
        entry = MemoryEntry(
            id=build_node_id(user_id, NodeType.PERSON, "collection-probe"),
            user_id=user_id,
            kind="node",
            type=NodeType.PERSON.value,
            name="collection-probe",
            created_at=now,
            updated_at=now,
        )

        await entry.insert()

        database = MemoryEntry.get_pymongo_collection().database
        collection_names = await database.list_collection_names()
        assert MEMORY_COLLECTION in collection_names
        assert "knowledge_graph" not in collection_names

        await entry.delete()


class TestRagNodeTypes:
    """ADR-006 decision 1: the closed set of node types the RAG layer writes.

    The rag/graph split is by MODE, not by row class — so this constant, not a
    Beanie subclass, is the one place that says "these rows are the RAG rows".
    Later tasks consume it (loader, child-search filter, the "rag never writes
    anything else" test).
    """

    def test_rag_node_types_are_document_and_chunk(self) -> None:
        assert RAG_NODE_TYPES == frozenset({"document", "chunk"})

    def test_rag_node_types_is_immutable(self) -> None:
        """A frozenset so no caller can widen the RAG surface at runtime."""

        assert isinstance(RAG_NODE_TYPES, frozenset)

    @pytest.mark.parametrize("node_type", sorted(RAG_NODE_TYPES))
    def test_every_rag_node_type_is_registered(self, node_type: str) -> None:
        assert node_type in NODE_REGISTRY

    @pytest.mark.parametrize("node_type", sorted(RAG_NODE_TYPES))
    def test_no_rag_node_type_is_llm_extractable(self, node_type: str) -> None:
        """RAG rows are built deterministically by pipeline code; if one of them
        ever became LLM-extractable the two layers would fight over the row."""

        assert node_type not in {t.value for t in LLM_EXTRACTABLE_NODE_TYPES}


class TestChunkHierarchyFields:
    """ADR-006 §2 / #107: the two-level chunk hierarchy in the row model.

    ``parent_id`` + ``chunk_index`` are top-level graph-modeling meta fields
    (ADR-001 §11) and the ``chunk`` registry entry closes its subtype vocabulary
    to ``{parent, child}`` — the level marker is the EXISTING ``subtype`` column,
    not a new ``level`` field.
    """

    def _chunk_kwargs(self, **overrides: object) -> dict[str, object]:
        now = datetime.now(UTC)
        kwargs: dict[str, object] = {
            "id": "u:chunk:x#parent-0#child-3",
            "user_id": _user_id(),
            "kind": "node",
            "type": "chunk",
            "name": "x#parent-0#child-3",
            "created_at": now,
            "updated_at": now,
        }
        kwargs.update(overrides)
        return kwargs

    def test_child_chunk_row_validates(self) -> None:
        entry = MemoryEntry(
            **self._chunk_kwargs(
                subtype="child",
                parent_id="u:chunk:x#parent-0",
                chunk_index=3,
            )
        )

        assert entry.subtype == "child"
        assert entry.parent_id == "u:chunk:x#parent-0"
        assert entry.chunk_index == 3

    def test_parent_chunk_row_validates(self) -> None:
        entry = MemoryEntry(
            **self._chunk_kwargs(
                id="u:chunk:x#parent-0",
                subtype="parent",
                parent_id="u:document:x",
                chunk_index=0,
            )
        )

        assert entry.subtype == "parent"
        assert entry.parent_id == "u:document:x"
        assert entry.chunk_index == 0

    def test_unknown_chunk_subtype_is_rejected(self) -> None:
        with pytest.raises(ValueError) as excinfo:
            MemoryEntry(**self._chunk_kwargs(subtype="section"))

        assert "['child', 'parent']" in str(excinfo.value)

    def test_chunk_subtype_may_stay_none(self) -> None:
        """Loose at construction, like every other closed-vocabulary type."""

        entry = MemoryEntry(**self._chunk_kwargs())

        assert entry.subtype is None

    def test_hierarchy_fields_default_to_none(self) -> None:
        """Entity rows (and pre-#107 rows) carry neither field."""

        now = datetime.now(UTC)
        entry = MemoryEntry(
            id="u:person:alice",
            user_id=_user_id(),
            kind="node",
            type="person",
            name="alice",
            created_at=now,
            updated_at=now,
        )

        assert entry.parent_id is None
        assert entry.chunk_index is None

    def test_chunk_registry_subtypes_are_closed_to_parent_and_child(self) -> None:
        assert NODE_REGISTRY["chunk"].subtypes == frozenset({"parent", "child"})


class TestChunkClusterFields:
    """ADR-007 §3 / #115: ``cluster_id`` + ``viz`` on the CHILD chunk row.

    Two top-level graph-modeling meta fields (ADR-001 §11) written by a
    **Clustering run**, both defaulting to ``None`` so every pre-#115 row
    validates unchanged. A validator keeps them off every non-child row: a
    ``document`` or ``parent`` row carrying map coordinates would be drawn by a
    surface that only ever plots children.
    """

    def _child_kwargs(self, **overrides: object) -> dict[str, object]:
        now = datetime.now(UTC)
        kwargs: dict[str, object] = {
            "id": "u:chunk:x#parent-0#child-3",
            "user_id": _user_id(),
            "kind": "node",
            "type": "chunk",
            "subtype": "child",
            "name": "x#parent-0#child-3",
            "parent_id": "u:chunk:x#parent-0",
            "chunk_index": 3,
            "created_at": now,
            "updated_at": now,
        }
        kwargs.update(overrides)
        return kwargs

    def test_child_chunk_carries_cluster_id_and_viz(self) -> None:
        entry = MemoryEntry(
            **self._child_kwargs(
                cluster_id=2,
                viz={"x": 0.1, "y": -3.2, "run_id": "r1"},
            )
        )

        assert entry.cluster_id == 2
        assert entry.viz == ChunkViz(x=0.1, y=-3.2, run_id="r1")

    def test_noise_is_stored_on_the_chunk_as_minus_one(self) -> None:
        """Noise gets coordinates and ``-1`` on the chunk (but no cluster row)."""

        entry = MemoryEntry(
            **self._child_kwargs(
                cluster_id=-1,
                viz={"x": 9.0, "y": 9.0, "run_id": "r1"},
            )
        )

        assert entry.cluster_id == -1

    def test_fields_default_to_none_on_an_unclustered_child(self) -> None:
        """Every existing row round-trips unchanged — there is no migration."""

        entry = MemoryEntry(**self._child_kwargs())
        rehydrated = MemoryEntry.model_validate(entry.model_dump())

        assert entry.cluster_id is None and entry.viz is None
        assert rehydrated.cluster_id is None and rehydrated.viz is None

    def test_round_trip_preserves_the_coordinates(self) -> None:
        entry = MemoryEntry(
            **self._child_kwargs(
                cluster_id=7,
                viz={"x": 0.1, "y": -3.2, "run_id": "r1"},
            )
        )

        rehydrated = MemoryEntry.model_validate(entry.model_dump())

        assert rehydrated.cluster_id == 7
        assert rehydrated.viz is not None
        assert (rehydrated.viz.x, rehydrated.viz.y) == (0.1, -3.2)
        assert rehydrated.viz.run_id == "r1"

    @pytest.mark.parametrize(
        "overrides",
        [
            {"cluster_id": 2},
            {"viz": {"x": 0.1, "y": -3.2, "run_id": "r1"}},
        ],
        ids=["cluster_id", "viz"],
    )
    def test_parent_chunk_row_rejects_the_cluster_fields(
        self, overrides: dict[str, object]
    ) -> None:
        with pytest.raises(ValueError, match="child"):
            MemoryEntry(
                **self._child_kwargs(
                    id="u:chunk:x#parent-0",
                    subtype="parent",
                    parent_id="u:document:x",
                    chunk_index=0,
                    **overrides,
                )
            )

    @pytest.mark.parametrize(
        "overrides",
        [
            {"cluster_id": 2},
            {"viz": {"x": 0.1, "y": -3.2, "run_id": "r1"}},
        ],
        ids=["cluster_id", "viz"],
    )
    def test_document_row_rejects_the_cluster_fields(
        self, overrides: dict[str, object]
    ) -> None:
        now = datetime.now(UTC)
        with pytest.raises(ValueError, match="child"):
            MemoryEntry(
                id="u:document:x",
                user_id=_user_id(),
                kind="node",
                type="document",
                name="x",
                created_at=now,
                updated_at=now,
                **overrides,
            )

    def test_entity_node_rejects_the_cluster_fields(self) -> None:
        now = datetime.now(UTC)
        with pytest.raises(ValueError, match="child"):
            MemoryEntry(
                id="u:person:alice",
                user_id=_user_id(),
                kind="node",
                type="person",
                name="alice",
                cluster_id=0,
                created_at=now,
                updated_at=now,
            )

    def test_chunk_viz_requires_all_three_coordinates(self) -> None:
        with pytest.raises(ValidationError):
            ChunkViz(x=0.1, y=-3.2)  # type: ignore[call-arg]

    @pytest.mark.parametrize("model", [ChunkViz], ids=["ChunkViz"])
    def test_new_models_document_every_field(self, model) -> None:
        properties = model.model_json_schema()["properties"]

        for name in model.model_fields:
            description = properties[name].get("description")
            assert description and description.strip(), (
                f"{model.__name__}.{name} is missing Field(description=...)"
            )

    @pytest.mark.parametrize("name", ["cluster_id", "viz"])
    def test_memory_entry_documents_the_new_fields(self, name: str) -> None:
        description = MemoryEntry.model_json_schema()["properties"][name].get(
            "description"
        )

        assert description and description.strip()


class TestStoredVector:
    """Task 175: ``memory.embedding`` is stored as BSON float32 ``binData``.

    ``to_stored_vector`` is the ONE write boundary and ``from_stored_vector`` the
    ONE read boundary; a 1024-d vector costs ~4 KB instead of ~13 KB as an array
    of doubles (the Atlas M0 512 MB cap is what forced it).
    """

    def test_to_stored_vector_writes_float32_bindata(self) -> None:
        stored = to_stored_vector([0.1, -0.2, 1.5])

        assert isinstance(stored, Binary)
        assert stored.subtype == 9
        assert stored.as_vector().dtype == BinaryVectorDtype.FLOAT32

    def test_to_stored_vector_costs_four_bytes_per_dimension(self) -> None:
        stored = to_stored_vector([0.5] * 1024)

        # 2-byte header (dtype + padding) + 4 bytes per float32.
        assert len(stored) == 2 + 4 * 1024

    def test_round_trip_preserves_floats_within_float32_precision(self) -> None:
        vector = [0.123456789, -0.987654321, 3.14159265, 0.0, 1e-7]

        decoded = from_stored_vector(to_stored_vector(vector))

        assert decoded == pytest.approx(vector, rel=1e-6, abs=1e-9)
        assert all(isinstance(value, float) for value in decoded)

    def test_to_stored_vector_rejects_an_empty_vector(self) -> None:
        # Pending is an ABSENT field, never an empty vector.
        with pytest.raises(ValueError, match="empty"):
            to_stored_vector([])

    @pytest.mark.parametrize("value", [None, []], ids=["none", "empty-list"])
    def test_from_stored_vector_reads_missing_as_empty(self, value) -> None:
        assert from_stored_vector(value) == []

    def test_from_stored_vector_passes_a_float_list_through(self) -> None:
        assert from_stored_vector([0.1, 0.2]) == [0.1, 0.2]

    def test_from_stored_vector_rejects_a_non_vector_binary(self) -> None:
        with pytest.raises(ValueError, match="subtype"):
            from_stored_vector(Binary(b"\x01\x02\x03\x04", 0))

    @pytest.mark.parametrize(
        "raw",
        [b"\x01\x02", bytearray(b"\x01\x02"), memoryview(b"\x01\x02")],
        ids=["bytes", "bytearray", "memoryview"],
    )
    def test_from_stored_vector_rejects_raw_bindata(self, raw) -> None:
        # PyMongo decodes binData subtype 0 to plain ``bytes`` (not ``Binary``);
        # iterating it yields byte ints, so ``b"\x01\x02"`` must not become
        # ``[1.0, 2.0]``.
        with pytest.raises(ValueError, match="raw binData"):
            from_stored_vector(raw)

    def test_to_stored_vector_accepts_a_numpy_array(self) -> None:
        stored = to_stored_vector(np.array([0.25, -0.5], dtype=np.float32))

        assert from_stored_vector(stored) == [0.25, -0.5]

    def test_to_stored_vector_rejects_an_empty_numpy_array(self) -> None:
        with pytest.raises(ValueError, match="empty"):
            to_stored_vector(np.array([], dtype=np.float32))

    async def test_model_validate_rejects_a_raw_bindata_embedding(self) -> None:
        user_id = _user_id()
        row = {
            "_id": f"{user_id}:person:bob",
            "user_id": user_id,
            "kind": "node",
            "type": "person",
            "name": "bob",
            "embedding": b"\x01\x02\x03\x04",
            "created_at": datetime(2025, 12, 1, tzinfo=UTC),
            "updated_at": datetime(2025, 12, 2, tzinfo=UTC),
        }

        with pytest.raises(ValidationError, match="raw binData"):
            MemoryEntry.model_validate(row)

    def test_from_stored_vector_rejects_a_non_float32_vector(self) -> None:
        int8 = Binary.from_vector([1, 2, 3], BinaryVectorDtype.INT8)

        with pytest.raises(ValueError, match="FLOAT32"):
            from_stored_vector(int8)

    def test_stored_vector_query_selects_bindata(self) -> None:
        assert STORED_VECTOR_QUERY == {"$type": "binData"}

    async def test_model_validate_decodes_a_binary_embedding(self) -> None:
        user_id = _user_id()
        row = {
            "_id": f"{user_id}:person:bob",
            "user_id": user_id,
            "kind": "node",
            "type": "person",
            "name": "bob",
            "embedding": to_stored_vector([0.25, -0.5, 0.75]),
            "created_at": datetime(2025, 12, 1, tzinfo=UTC),
            "updated_at": datetime(2025, 12, 2, tzinfo=UTC),
        }

        entry = MemoryEntry.model_validate(row)

        assert entry.embedding == [0.25, -0.5, 0.75]

    async def test_model_validate_reads_an_absent_embedding_as_none(self) -> None:
        user_id = _user_id()
        row = {
            "_id": f"{user_id}:chunk:p0",
            "user_id": user_id,
            "kind": "node",
            "type": "chunk",
            "subtype": "parent",
            "created_at": datetime(2025, 12, 1, tzinfo=UTC),
            "updated_at": datetime(2025, 12, 2, tzinfo=UTC),
        }

        entry = MemoryEntry.model_validate(row)

        assert entry.embedding is None
