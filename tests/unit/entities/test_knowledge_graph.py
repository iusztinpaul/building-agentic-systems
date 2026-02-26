from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from twin.entities.knowledge_graph import (
    EdgeLogEntry,
    EdgeType,
    KnowledgeGraphEntry,
    NodeLogEntry,
    NodeType,
)


class TestNodeLogEntry:
    async def test_valid_node(self):
        node = NodeLogEntry(
            name="alice",
            type=NodeType.PERSON,
            properties={"aliases": ["alice smith"]},
            source_document_id="507f1f77bcf86cd799439011",
            chunk_id="doc#chunk-0",
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )

        assert node.kind == "node"
        assert node.name == "alice"
        assert node.type == NodeType.PERSON

    async def test_defaults(self):
        node = NodeLogEntry(
            name="test",
            type=NodeType.TASK,
            source_document_id="507f1f77bcf86cd799439011",
            chunk_id="chunk-0",
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )

        assert node.properties == {}

    async def test_missing_required_fields(self):
        with pytest.raises(ValidationError):
            NodeLogEntry(kind="node")


class TestEdgeLogEntry:
    async def test_valid_edge(self):
        edge = EdgeLogEntry(
            source_node_id="alice",
            source_type=NodeType.PERSON,
            target_node_id="bob",
            target_type=NodeType.PERSON,
            type=EdgeType.RELATED_TO,
            source_document_id="507f1f77bcf86cd799439011",
            chunk_id="doc#chunk-0",
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )

        assert edge.kind == "edge"
        assert edge.source_node_id == "alice"
        assert edge.target_node_id == "bob"
        assert edge.type == EdgeType.RELATED_TO


class TestKnowledgeGraphEntry:
    async def test_node_entry(self):
        entry = KnowledgeGraphEntry(
            id="alice",
            kind="node",
            type=NodeType.PERSON,
            properties={"aliases": []},
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            updated_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
        )

        assert entry.id == "alice"
        assert entry.kind == "node"
        assert entry.embedding == []
        assert entry.source_node_id is None

    async def test_edge_entry(self):
        entry = KnowledgeGraphEntry(
            id={
                "source_node_id": "alice",
                "target_node_id": "bob",
                "type": "related_to",
            },
            kind="edge",
            type=EdgeType.RELATED_TO,
            source_node_id="alice",
            source_type=NodeType.PERSON,
            target_node_id="bob",
            target_type=NodeType.PERSON,
            created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            updated_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
        )

        assert entry.kind == "edge"
        assert entry.source_node_id == "alice"
        assert entry.target_node_id == "bob"
