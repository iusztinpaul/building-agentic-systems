from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from tree.entities.knowledge_graph import (
    EdgeType,
    NodeType,
    build_edge_id,
    build_node_id,
)

TEST_DATABASE = "integration_tests_twin"


@pytest.fixture()
def make_mcp_ctx(mongo_client):
    """Factory fixture: build a minimal MCP Context mock with lifespan_context."""

    def _factory(llm=None, embedding_model=None):
        ctx = MagicMock()
        ctx.lifespan_context = {
            "client": mongo_client,
            "database": TEST_DATABASE,
            "llm": llm,
            "embedding_model": embedding_model,
        }
        return ctx

    return _factory


@pytest.fixture()
async def seed_graph(mongo_client):
    """Insert a small graph: two person nodes + one edge, with text index."""

    now = datetime.now(tz=timezone.utc)
    col = mongo_client[TEST_DATABASE]["knowledge_graph"]

    await col.create_index(
        [
            ("name", "text"),
            ("properties.content", "text"),
            ("properties.aliases", "text"),
        ],
        name="text_index",
    )

    alice_id = build_node_id(NodeType.PERSON, "alice")
    bob_id = build_node_id(NodeType.PERSON, "bob")
    edge_id = build_edge_id(alice_id, EdgeType.RELATED_TO, bob_id)

    alice = {
        "_id": alice_id,
        "kind": "node",
        "type": NodeType.PERSON,
        "name": "alice",
        "properties": {"aliases": ["alice doe"], "content": "Alice is an ML engineer."},
        "embedding": [0.0] * 768,
        "sources": [],
        "created_at": now,
        "updated_at": now,
    }
    bob = {
        "_id": bob_id,
        "kind": "node",
        "type": NodeType.PERSON,
        "name": "bob",
        "properties": {"aliases": ["bob smith"], "content": "Bob is a data scientist."},
        "embedding": [0.0] * 768,
        "sources": [],
        "created_at": now,
        "updated_at": now,
    }
    edge = {
        "_id": edge_id,
        "kind": "edge",
        "type": EdgeType.RELATED_TO,
        "source_node_id": alice_id,
        "source_type": NodeType.PERSON,
        "target_node_id": bob_id,
        "target_type": NodeType.PERSON,
        "sources": [],
        "created_at": now,
        "updated_at": now,
    }

    for doc in [alice, bob, edge]:
        await col.replace_one({"_id": doc["_id"]}, doc, upsert=True)

    yield {"alice_id": alice_id, "bob_id": bob_id, "edge_id": edge_id}

    await col.delete_many({"_id": {"$in": [alice_id, bob_id, edge_id]}})
