from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from beanie import PydanticObjectId

from tree.entities.knowledge_graph import (
    EdgeType,
    NodeType,
    build_edge_id,
    build_node_id,
)
from tree.entities.users import User

TEST_DATABASE = "integration_tests_twin"


@pytest.fixture()
async def test_user() -> User:
    """Create a real User row for tests that exercise the extraction pipeline.

    The extraction pipeline calls ``User.get(user_id)`` so the active user
    must exist in Mongo. Yields the user; cleanup happens via the autouse
    ``_clean_collections`` fixture in the parent conftest.
    """

    user = User(identifier=f"mcp-test-{PydanticObjectId()}")
    await user.insert()
    return user


@pytest.fixture()
def make_mcp_ctx(mongo_client):
    """Factory fixture: build a minimal MCP Context mock with lifespan_context.

    ``user_id`` defaults to a stable :class:`PydanticObjectId` so callers
    can override when the test needs to match a seed-time tenant. Tests
    that drive the extraction pipeline should pass the id of a real
    :class:`User` row (e.g. the ``test_user`` fixture).
    """

    def _factory(llm=None, embedding_model=None, user_id=None):
        ctx = MagicMock()
        ctx.lifespan_context = {
            "client": mongo_client,
            "database": TEST_DATABASE,
            "llm": llm,
            "embedding_model": embedding_model,
            "user_id": user_id or PydanticObjectId(),
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

    # Per #018, all rows carry a tenant ``user_id``. MCP integration tests
    # exercise read paths that #020 will plumb user_id through; for #018
    # we seed a single fixture user so the data is well-formed.
    seed_user_id = PydanticObjectId()
    alice_id = build_node_id(seed_user_id, NodeType.PERSON, "alice")
    bob_id = build_node_id(seed_user_id, NodeType.PERSON, "bob")
    edge_id = build_edge_id(alice_id, EdgeType.RELATED_TO, bob_id)

    alice = {
        "_id": alice_id,
        "user_id": seed_user_id,
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
        "user_id": seed_user_id,
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
        "user_id": seed_user_id,
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

    yield {
        "alice_id": alice_id,
        "bob_id": bob_id,
        "edge_id": edge_id,
        "user_id": seed_user_id,
    }

    await col.delete_many({"_id": {"$in": [alice_id, bob_id, edge_id]}})
