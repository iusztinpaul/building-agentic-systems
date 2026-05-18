"""Integration tests for the ``related_to`` umbrella validator (#029).

The tests round-trip ``KnowledgeGraphEntry`` rows through the
Beanie/MongoDB stack so the Pydantic model validator AND the partial
``user_type_semantic_type`` index are both exercised. The unit-level
validator branches are pinned in
``tests/unit/entities/test_knowledge_graph.py``; this file's job is
the persistence story.

All tests are written as the red side of a TDD pair — they would have
failed against the pre-#029 ``KnowledgeGraphEntry`` because:

* ``related_to`` had no ``semantic_type`` discriminator.
* ``mentions`` only accepted ``(document, person)``.
* ``has`` only accepted ``(person, preference)``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from beanie import PydanticObjectId
from pydantic import ValidationError

from tree.entities.knowledge_graph import (
    EdgeType,
    KnowledgeGraphEntry,
    NodeType,
    build_edge_id,
    build_node_id,
)


_NOW = datetime(2026, 5, 17, tzinfo=UTC)


def _user_id() -> PydanticObjectId:
    return PydanticObjectId()


def _node(user_id, node_type: NodeType, name: str, **overrides) -> KnowledgeGraphEntry:
    defaults: dict[str, Any] = dict(
        id=build_node_id(user_id, node_type, name),
        user_id=user_id,
        kind="node",
        type=node_type.value,
        name=name,
        properties={},
        created_at=_NOW,
        updated_at=_NOW,
    )
    defaults.update(overrides)
    return KnowledgeGraphEntry(**defaults)


def _edge(
    user_id,
    *,
    edge_type: EdgeType,
    src_type: NodeType,
    src_name: str,
    tgt_type: NodeType,
    tgt_name: str,
    semantic_type: str | None = None,
    properties: dict[str, Any] | None = None,
) -> KnowledgeGraphEntry:
    src_id = build_node_id(user_id, src_type, src_name)
    tgt_id = build_node_id(user_id, tgt_type, tgt_name)
    edge_id = build_edge_id(src_id, edge_type, tgt_id)
    return KnowledgeGraphEntry(
        id=edge_id,
        user_id=user_id,
        kind="edge",
        type=edge_type.value,
        semantic_type=semantic_type,
        source_node_id=src_id,
        source_type=src_type,
        target_node_id=tgt_id,
        target_type=tgt_type,
        properties=properties or {},
        created_at=_NOW,
        updated_at=_NOW,
    )


@pytest.fixture()
async def kg_collection(mongo_client):
    # The session-scoped ``mongo_client`` fixture binds Beanie to a
    # specific test database; use the same database for raw collection
    # access so the per-test ``_clean_collections`` autouse fixture
    # also wipes our writes here.
    from tests.integration.conftest import TEST_DATABASE

    db = mongo_client[TEST_DATABASE]
    yield db["knowledge_graph"]


# ---------------------------------------------------------------------------
# related_to umbrella
# ---------------------------------------------------------------------------


class TestRelatedToPersistence:
    async def test_employed_by_person_to_organization_persists(self) -> None:
        user_id = _user_id()
        # Endpoint nodes (the validator doesn't require them to exist
        # in the DB; we still insert so the row is self-consistent).
        await _node(user_id, NodeType.PERSON, "alice").insert()
        await _node(
            user_id,
            NodeType.ORGANIZATION,
            "anthropic",
            subtype="company",
        ).insert()
        edge = _edge(
            user_id,
            edge_type=EdgeType.RELATED_TO,
            src_type=NodeType.PERSON,
            src_name="alice",
            tgt_type=NodeType.ORGANIZATION,
            tgt_name="anthropic",
            semantic_type="employed_by",
            properties={"start_date": "2024-03-01"},
        )
        await edge.insert()

        rehydrated = await KnowledgeGraphEntry.get(edge.id)
        assert rehydrated is not None
        assert rehydrated.semantic_type == "employed_by"
        assert rehydrated.properties.get("start_date") == "2024-03-01"

    async def test_employed_by_pair_violation_rejected(self) -> None:
        user_id = _user_id()
        # employed_by is (person, organization). Reverse direction
        # should fail Pydantic construction before any write.
        with pytest.raises(ValidationError) as excinfo:
            _edge(
                user_id,
                edge_type=EdgeType.RELATED_TO,
                src_type=NodeType.ORGANIZATION,
                src_name="anthropic",
                tgt_type=NodeType.PERSON,
                tgt_name="alice",
                semantic_type="employed_by",
            )
        assert "employed_by" in str(excinfo.value)

    async def test_unknown_semantic_rejected(self) -> None:
        user_id = _user_id()
        with pytest.raises(ValidationError) as excinfo:
            _edge(
                user_id,
                edge_type=EdgeType.RELATED_TO,
                src_type=NodeType.PERSON,
                src_name="alice",
                tgt_type=NodeType.PERSON,
                tgt_name="bob",
                semantic_type="not_in_registry",
            )
        assert "not_in_registry" in str(excinfo.value)

    async def test_missing_semantic_rejected(self) -> None:
        user_id = _user_id()
        with pytest.raises(ValidationError) as excinfo:
            _edge(
                user_id,
                edge_type=EdgeType.RELATED_TO,
                src_type=NodeType.PERSON,
                src_name="alice",
                tgt_type=NodeType.PERSON,
                tgt_name="bob",
                semantic_type=None,
            )
        assert "semantic_type" in str(excinfo.value)


# ---------------------------------------------------------------------------
# mentions broadening
# ---------------------------------------------------------------------------


class TestMentionsBroadeningPersistence:
    async def test_chunk_to_organization_persists(self) -> None:
        user_id = _user_id()
        await _node(
            user_id,
            NodeType.CHUNK,
            "abc",
            properties={
                "source_type": "conversation",
                "source_uri": "u",
                "content": "Anthropic was mentioned.",
            },
        ).insert()
        await _node(
            user_id, NodeType.ORGANIZATION, "anthropic", subtype="company"
        ).insert()
        edge = _edge(
            user_id,
            edge_type=EdgeType.MENTIONS,
            src_type=NodeType.CHUNK,
            src_name="abc",
            tgt_type=NodeType.ORGANIZATION,
            tgt_name="anthropic",
        )
        await edge.insert()

        rehydrated = await KnowledgeGraphEntry.get(edge.id)
        assert rehydrated is not None
        assert rehydrated.target_type == NodeType.ORGANIZATION

    async def test_chunk_to_preference_rejected(self) -> None:
        # ``mentions`` carves preference OUT per ``plan.md:479``.
        user_id = _user_id()
        with pytest.raises(ValidationError) as excinfo:
            _edge(
                user_id,
                edge_type=EdgeType.MENTIONS,
                src_type=NodeType.CHUNK,
                src_name="abc",
                tgt_type=NodeType.PREFERENCE,
                tgt_name="coffee",
            )
        assert "mentions" in str(excinfo.value)


# ---------------------------------------------------------------------------
# `has` broadening
# ---------------------------------------------------------------------------


class TestHasBroadeningPersistence:
    async def test_self_to_preference_persists(self) -> None:
        user_id = _user_id()
        await _node(
            user_id,
            NodeType.PERSON,
            "self",
            properties={"is_active_user": True},
        ).insert()
        await _node(
            user_id, NodeType.PREFERENCE, "coffee", properties={"content": "coffee"}
        ).insert()
        edge = _edge(
            user_id,
            edge_type=EdgeType.HAS,
            src_type=NodeType.PERSON,
            src_name="self",
            tgt_type=NodeType.PREFERENCE,
            tgt_name="coffee",
        )
        await edge.insert()
        rehydrated = await KnowledgeGraphEntry.get(edge.id)
        assert rehydrated is not None

    async def test_self_to_object_task_persists(self) -> None:
        # ``has`` now also accepts (person, object) per #029.
        user_id = _user_id()
        await _node(user_id, NodeType.PERSON, "self").insert()
        await _node(user_id, NodeType.OBJECT, "ship demo", subtype="task").insert()
        edge = _edge(
            user_id,
            edge_type=EdgeType.HAS,
            src_type=NodeType.PERSON,
            src_name="self",
            tgt_type=NodeType.OBJECT,
            tgt_name="ship demo",
        )
        await edge.insert()
        rehydrated = await KnowledgeGraphEntry.get(edge.id)
        assert rehydrated is not None


# ---------------------------------------------------------------------------
# Partial index declared on the model is created by Beanie
# ---------------------------------------------------------------------------


class TestSemanticTypePartialIndex:
    """The ``(user_id, type, semantic_type)`` partial index lives on
    ``KnowledgeGraphEntry.Settings.indexes`` so Beanie creates it as
    part of ``init_mongodb``. Pin the live index here.
    """

    async def test_index_present_with_partial_filter(self, kg_collection) -> None:
        info = await kg_collection.index_information()
        assert "user_type_semantic_type" in info, (
            f"missing user_type_semantic_type index; got {sorted(info)}"
        )
        idx = info["user_type_semantic_type"]
        assert idx["key"] == [("user_id", 1), ("type", 1), ("semantic_type", 1)]
        assert idx.get("partialFilterExpression") == {
            "semantic_type": {"$type": "string"}
        }
