from datetime import datetime, timezone

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
