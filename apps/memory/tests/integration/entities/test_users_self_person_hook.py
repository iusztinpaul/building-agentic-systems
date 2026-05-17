"""Integration tests for ``User`` + the ``after_insert`` self-person hook.

These tests exercise the real Beanie/Motor write path against a local
MongoDB (the same instance used by ``make local-start``). They verify:

* A real ``User.insert()`` causes the self-person node to land in the
  ``knowledge_graph`` collection at the expected ``_id``.
* The hook is idempotent — re-running it does not duplicate the node.
* Two users get two distinct self-person nodes (no cross-tenant collision).
* The ``identifier`` uniqueness index raises ``DuplicateKeyError`` on
  collision.
* The ``canonical_name`` fallback to ``identifier`` works end-to-end.
"""

from __future__ import annotations

import pymongo.errors
import pytest

from tree.entities.knowledge_graph import KnowledgeGraphEntry, NodeType
from tree.entities.users import User


class TestUserSelfPersonHookIntegration:
    async def test_insert_creates_self_person_node(self):
        user = User(
            identifier="paul@example.com",
            attributes={"name": "Paul", "locale": "en-US"},
        )

        await user.insert()

        node = await KnowledgeGraphEntry.find_one({"_id": f"{user.id}:person:self"})

        assert node is not None
        assert node.id == f"{user.id}:person:self"
        # Per #018: the self-person row carries the tenant ``user_id``.
        assert node.user_id == user.id
        assert node.kind == "node"
        assert node.type == NodeType.PERSON
        assert node.name == "self"
        assert node.canonical_name == "Paul"
        assert node.properties["is_active_user"] is True
        assert node.properties["name"] == "Paul"
        assert node.properties["locale"] == "en-US"
        assert node.created_at.tzinfo is not None
        assert node.updated_at.tzinfo is not None

    async def test_rerunning_hook_does_not_duplicate_self_person(self):
        user = User(identifier="dev@example.com", attributes={"name": "Dev"})

        await user.insert()
        # Manually fire the hook a second time to simulate a re-run.
        await user.after_insert()

        col = KnowledgeGraphEntry.get_pymongo_collection()
        count = await col.count_documents({"_id": f"{user.id}:person:self"})

        assert count == 1

    async def test_canonical_name_falls_back_to_identifier(self):
        user = User(identifier="no-name@example.com", attributes={})

        await user.insert()

        node = await KnowledgeGraphEntry.find_one({"_id": f"{user.id}:person:self"})

        assert node is not None
        assert node.canonical_name == "no-name@example.com"

    async def test_two_users_get_two_distinct_self_person_nodes(self):
        user_a = User(identifier="a@example.com", attributes={"name": "Alice"})
        user_b = User(identifier="b@example.com", attributes={"name": "Bob"})

        await user_a.insert()
        await user_b.insert()

        node_a = await KnowledgeGraphEntry.find_one({"_id": f"{user_a.id}:person:self"})
        node_b = await KnowledgeGraphEntry.find_one({"_id": f"{user_b.id}:person:self"})

        assert node_a is not None and node_b is not None
        assert node_a.id != node_b.id
        assert node_a.canonical_name == "Alice"
        assert node_b.canonical_name == "Bob"

    async def test_duplicate_identifier_raises_duplicate_key_error(self):
        await User(identifier="dup@example.com", attributes={"name": "First"}).insert()

        with pytest.raises(pymongo.errors.DuplicateKeyError):
            await User(
                identifier="dup@example.com", attributes={"name": "Second"}
            ).insert()
