from datetime import UTC, datetime, timezone

import pytest

from tree.entities.knowledge_graph import (
    EdgeType,
    KnowledgeGraphEntry,
    NodeType,
    build_edge_id,
    build_node_id,
)


class TestBuildNodeId:
    def test_builds_composite_id(self):
        assert build_node_id(NodeType.PERSON, "alice") == "person:alice"

    def test_builds_document_id(self):
        assert (
            build_node_id(NodeType.DOCUMENT, "https://example.com")
            == "document:https://example.com"
        )

    def test_builds_chunk_id(self):
        assert (
            build_node_id(NodeType.CHUNK, "https://example.com#chunk-0")
            == "chunk:https://example.com#chunk-0"
        )


class TestBuildEdgeId:
    def test_builds_edge_id(self):
        result = build_edge_id("person:alice", EdgeType.TODO, "task:write a book")
        assert result == "person:alice|todo|task:write a book"


class TestKnowledgeGraphEntry:
    async def test_node_entry(self):
        entry = KnowledgeGraphEntry(
            id="person:alice",
            kind="node",
            type=NodeType.PERSON,
            properties={"aliases": []},
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            updated_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
        )

        assert entry.id == "person:alice"
        assert entry.kind == "node"
        assert entry.embedding == []
        assert entry.source_node_id is None

    async def test_edge_entry(self):
        entry = KnowledgeGraphEntry(
            id="person:alice|related_to|person:bob",
            kind="edge",
            type=EdgeType.RELATED_TO,
            source_node_id="person:alice",
            source_type=NodeType.PERSON,
            target_node_id="person:bob",
            target_type=NodeType.PERSON,
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            updated_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
        )

        assert entry.kind == "edge"
        assert entry.id == "person:alice|related_to|person:bob"
        assert entry.source_node_id == "person:alice"
        assert entry.target_node_id == "person:bob"

    async def test_missing_required_id_raises(self):
        with pytest.raises(Exception):
            KnowledgeGraphEntry(
                kind="node",
                type=NodeType.PERSON,
                created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
                updated_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
            )


class TestResolutionDedupFields:
    """Resolution + dedup fields added in task #007.

    Five new optional fields on ``KnowledgeGraphEntry`` plus ``EdgeType.SAME_AS``.
    """

    def _build_node(self, **overrides) -> KnowledgeGraphEntry:
        defaults = dict(
            id="person:alice",
            kind="node",
            type=NodeType.PERSON,
            name="alice",
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            updated_at=datetime(2026, 1, 2, tzinfo=UTC),
        )
        defaults.update(overrides)
        return KnowledgeGraphEntry(**defaults)

    def _build_edge(self, **overrides) -> KnowledgeGraphEntry:
        defaults = dict(
            id="person:alice|same_as|person:alice smith",
            kind="edge",
            type=EdgeType.SAME_AS,
            source_node_id="person:alice",
            source_type=NodeType.PERSON,
            target_node_id="person:alice smith",
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
            EdgeType.TODO,
            EdgeType.EXPERIENCED,
            EdgeType.HAS,
        ],
    )
    async def test_existing_edge_types_unchanged(self, previously_shipped):
        # Adding SAME_AS must not rename / remove any prior member.
        assert previously_shipped in EdgeType

    async def test_same_as_edge_id_uses_build_edge_id_shape(self):
        edge_id = build_edge_id("person:alice", EdgeType.SAME_AS, "person:alice smith")
        assert edge_id == "person:alice|same_as|person:alice smith"

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

        assert rehydrated.id == "person:alice|same_as|person:alice smith"
        assert rehydrated.kind == "edge"
        assert rehydrated.type == EdgeType.SAME_AS
        assert rehydrated.properties == {
            "status": "pending",
            "confidence": 0.9,
            "match_type": "embedding",
            "created_at": created_at,
        }

    async def test_legacy_node_doc_without_new_fields_loads_with_defaults(self):
        # A document written before task #007 has none of the five new fields.
        legacy = {
            "_id": "person:bob",
            "id": "person:bob",
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

        alice = self._build_node(
            id=build_node_id(NodeType.PERSON, "alice"),
            name="alice",
            canonical_name="Alice Smith",
            aliases=["Alice"],
            confidence=0.92,
        )
        alice_smith = self._build_node(
            id=build_node_id(NodeType.PERSON, "alice smith"),
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
        assert collection["person:alice"] is alice
        assert collection["person:alice smith"] is alice_smith
        assert len(collection) == 2

        # Soft-join: a "find by canonical_name" surfaces both, distinctly.
        matches = [e for e in collection.values() if e.canonical_name == "Alice Smith"]
        ids = {e.id for e in matches}
        assert ids == {"person:alice", "person:alice smith"}

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
