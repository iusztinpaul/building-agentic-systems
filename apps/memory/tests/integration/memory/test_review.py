"""Integration tests for the human-review API (``tree.memory.review``).

Per project convention every test that touches MongoDB lives in the
integration suite, so the bulk of the behavioral surface for
``find_pending_duplicates`` / ``review_duplicate`` /
``get_same_as_cluster`` lives here. The MCP tools that wrap those
functions are tested in the integration MCP suite (#014 AC).

These tests do NOT require Atlas search — they use the local MongoDB
instance and seed nodes/edges directly.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from beanie import PydanticObjectId

from tree.entities.knowledge_graph import (
    EdgeType,
    NodeType,
    build_edge_id,
)
from tree.entities.knowledge_graph import build_node_id as _build_node_id
from tree.mcp.tools import review_confirm, review_list_pending, review_reject
from tree.memory.review import (
    MergeStrategy,
    ReviewDecision,
    find_pending_duplicates,
    get_same_as_cluster,
    review_duplicate,
)


# Per #018: all KG ids are tenant-scoped. Review-pipeline tests in this
# module operate on a single fixture user — multi-tenant isolation is the
# subject of the #021 acceptance test, not these review-logic tests.
_REVIEW_USER_ID = PydanticObjectId("000000000000000000000018")


def build_node_id(node_type: NodeType, name: str) -> str:
    """Local 2-arg wrapper so this test module reads cleanly.

    Delegates to the real ``build_node_id`` with the module-level fixture
    ``user_id``. Once #019/#020 thread user_id end-to-end and #021 lands
    the isolation test, the review integration tests can be updated to
    parameterise on user_id and this shim removed.
    """

    return _build_node_id(_REVIEW_USER_ID, node_type, name)


TEST_DATABASE = "integration_tests_twin"
_NOW = datetime(2026, 5, 14, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def kg_collection(mongo_client):
    """Return the ``knowledge_graph`` collection; drop after each test."""

    db = mongo_client[TEST_DATABASE]
    col = db["knowledge_graph"]
    yield col
    await db.drop_collection("knowledge_graph")


@pytest.fixture
async def database(mongo_client):
    return mongo_client[TEST_DATABASE]


@pytest.fixture
def make_mcp_ctx(mongo_client):
    """Factory fixture: minimal MCP Context mock with lifespan_context.

    Mirrors the fixture in ``tests/integration/mcp/conftest.py`` so the
    MCP tool tests in this file can share the same MongoDB client without
    pulling that conftest in. The fixture pins ``user_id`` to
    ``_REVIEW_USER_ID`` so the review tools (post-#023) propagate it
    down into the business-logic calls.
    """

    from unittest.mock import MagicMock

    def _factory(
        llm: Any = None,
        embedding_model: Any = None,
        user_id: PydanticObjectId | None = None,
    ) -> MagicMock:
        ctx = MagicMock()
        ctx.lifespan_context = {
            "client": mongo_client,
            "database": TEST_DATABASE,
            "llm": llm,
            "embedding_model": embedding_model,
            "user_id": user_id if user_id is not None else _REVIEW_USER_ID,
        }
        return ctx

    return _factory


def _make_node(
    node_id: str,
    *,
    name: str | None = None,
    node_type: NodeType = NodeType.PERSON,
    subtype: str | None = None,
    created_at: datetime | None = None,
    properties: dict[str, Any] | None = None,
    confidence: float = 1.0,
    aliases: list[str] | None = None,
    sources: list[Any] | None = None,
    merged_into: str | None = None,
) -> dict[str, Any]:
    now = created_at or _NOW
    # ``node_id`` now has the shape "{user_id}:{type}:{name}" — strip
    # the user prefix when reverse-deriving the name fallback.
    fallback_name = node_id.split(":", 2)[2] if node_id.count(":") >= 2 else node_id
    return {
        "_id": node_id,
        "user_id": _REVIEW_USER_ID,
        "kind": "node",
        "type": node_type.value,
        "subtype": subtype,
        "name": name or fallback_name,
        "canonical_name": name or fallback_name,
        "aliases": aliases or [],
        "properties": properties or {},
        "sources": sources or [],
        "confidence": confidence,
        "embedding": [],
        "merged_into": merged_into,
        "created_at": now,
        "updated_at": now,
    }


def _make_same_as_edge(
    source_id: str,
    target_id: str,
    *,
    status: str = "pending",
    confidence: float = 0.92,
    match_type: str = "embedding",
    created_at: datetime | None = None,
) -> dict[str, Any]:
    edge_id = build_edge_id(source_id, EdgeType.SAME_AS, target_id)
    now = created_at or _NOW
    return {
        "_id": edge_id,
        "user_id": _REVIEW_USER_ID,
        "kind": "edge",
        "type": EdgeType.SAME_AS.value,
        "source_node_id": source_id,
        "source_type": NodeType.PERSON.value,
        "target_node_id": target_id,
        "target_type": NodeType.PERSON.value,
        "sources": [],
        "properties": {
            "status": status,
            "confidence": confidence,
            "match_type": match_type,
            "created_at": now,
        },
        "created_at": now,
        "updated_at": now,
    }


def _make_edge(
    source_id: str,
    edge_type: EdgeType,
    target_id: str,
    *,
    source_type: NodeType = NodeType.PERSON,
    target_type: NodeType = NodeType.PERSON,
    semantic_type: str | None = None,
    sources: list[Any] | None = None,
    properties: dict[str, Any] | None = None,
) -> dict[str, Any]:
    edge_id = build_edge_id(source_id, edge_type, target_id)
    return {
        "_id": edge_id,
        "user_id": _REVIEW_USER_ID,
        "kind": "edge",
        "type": edge_type.value,
        "semantic_type": semantic_type,
        "source_node_id": source_id,
        "source_type": source_type.value,
        "target_node_id": target_id,
        "target_type": target_type.value,
        "sources": sources or [],
        "properties": properties or {},
        "created_at": _NOW,
        "updated_at": _NOW,
    }


# ---------------------------------------------------------------------------
# find_pending_duplicates
# ---------------------------------------------------------------------------


class TestFindPendingDuplicates:
    async def test_returns_only_pending_filtered_by_type(
        self, kg_collection, database
    ) -> None:
        # Arrange — 3 pending PERSON pairs + 2 pending TASK pairs + 1 confirmed.
        docs: list[dict[str, Any]] = []
        for i in range(6):
            docs.append(_make_node(f"person:p{i}", name=f"p{i}"))
        docs.append(_make_node("task:t0", name="t0", node_type=NodeType.TASK))
        docs.append(_make_node("task:t1", name="t1", node_type=NodeType.TASK))
        docs.append(_make_node("task:t2", name="t2", node_type=NodeType.TASK))
        docs.append(_make_node("task:t3", name="t3", node_type=NodeType.TASK))

        # PERSON pairs (pending) — descending confidence so we can assert sort.
        docs.append(_make_same_as_edge("person:p0", "person:p1", confidence=0.99))
        docs.append(_make_same_as_edge("person:p2", "person:p3", confidence=0.92))
        docs.append(_make_same_as_edge("person:p4", "person:p5", confidence=0.88))

        # TASK pairs (pending).
        task_edge_0 = _make_same_as_edge("task:t0", "task:t1", confidence=0.91)
        task_edge_0["source_type"] = NodeType.TASK.value
        task_edge_0["target_type"] = NodeType.TASK.value
        docs.append(task_edge_0)

        task_edge_1 = _make_same_as_edge("task:t2", "task:t3", confidence=0.90)
        task_edge_1["source_type"] = NodeType.TASK.value
        task_edge_1["target_type"] = NodeType.TASK.value
        docs.append(task_edge_1)

        # Confirmed pair (should NOT be returned).
        docs.append(
            _make_same_as_edge(
                "person:p0", "person:p2", status="confirmed", confidence=0.95
            )
        )
        # Rejected pair (should NOT be returned).
        docs.append(
            _make_same_as_edge(
                "person:p1", "person:p3", status="rejected", confidence=0.94
            )
        )

        await kg_collection.insert_many(docs)

        # Act — PERSON-only, limit 10.
        results = await find_pending_duplicates(
            database, user_id=_REVIEW_USER_ID, entity_type=NodeType.PERSON, limit=10
        )

        # Assert
        assert len(results) == 3
        assert {r.entity_type for r in results} == {NodeType.PERSON}
        # Sorted by similarity score descending.
        scores = [r.similarity_score for r in results]
        assert scores == sorted(scores, reverse=True)

    async def test_limit_caps_result_count(self, kg_collection, database) -> None:
        # Arrange — 5 pending pairs.
        docs: list[dict[str, Any]] = []
        for i in range(10):
            docs.append(_make_node(f"person:p{i}"))
        for i in range(5):
            docs.append(
                _make_same_as_edge(
                    f"person:p{i * 2}",
                    f"person:p{i * 2 + 1}",
                    confidence=0.9 - i * 0.01,
                )
            )
        await kg_collection.insert_many(docs)

        # Act
        results = await find_pending_duplicates(
            database, user_id=_REVIEW_USER_ID, limit=2
        )

        # Assert
        assert len(results) == 2

    async def test_confirmed_and_rejected_excluded(
        self, kg_collection, database
    ) -> None:
        # Arrange — only confirmed and rejected edges, no pending.
        docs: list[dict[str, Any]] = [
            _make_node("person:a"),
            _make_node("person:b"),
            _make_node("person:c"),
            _make_node("person:d"),
            _make_same_as_edge("person:a", "person:b", status="confirmed"),
            _make_same_as_edge("person:c", "person:d", status="rejected"),
        ]
        await kg_collection.insert_many(docs)

        # Act
        results = await find_pending_duplicates(
            database, user_id=_REVIEW_USER_ID, limit=50
        )

        # Assert
        assert results == []


# ---------------------------------------------------------------------------
# review_duplicate — CONFIRM
# ---------------------------------------------------------------------------


class TestReviewDuplicateConfirmKeepPrimary:
    async def test_winner_loser_and_edge_transfer(
        self, kg_collection, database
    ) -> None:
        # Arrange — alice (older) + alice-s (newer); 3 inbound MENTIONS + 2
        # outbound TODO on loser.
        older = _NOW - timedelta(days=2)
        newer = _NOW
        alice_id = build_node_id(NodeType.PERSON, "alice")
        bob_id = build_node_id(NodeType.PERSON, "alice s")

        docs: list[dict[str, Any]] = [
            _make_node(alice_id, name="alice", created_at=older),
            _make_node(
                bob_id,
                name="alice s",
                created_at=newer,
                properties={"email": "alice@example.com"},
            ),
            # 3 inbound MENTIONS on loser (bob_id) coming from 3 chunks.
            _make_node("chunk:c1", node_type=NodeType.CHUNK),
            _make_node("chunk:c2", node_type=NodeType.CHUNK),
            _make_node("chunk:c3", node_type=NodeType.CHUNK),
            _make_edge(
                "chunk:c1",
                EdgeType.MENTIONS,
                bob_id,
                source_type=NodeType.CHUNK,
                target_type=NodeType.PERSON,
                sources=["doc:s1"],
            ),
            _make_edge(
                "chunk:c2",
                EdgeType.MENTIONS,
                bob_id,
                source_type=NodeType.CHUNK,
                target_type=NodeType.PERSON,
                sources=["doc:s2"],
            ),
            _make_edge(
                "chunk:c3",
                EdgeType.MENTIONS,
                bob_id,
                source_type=NodeType.CHUNK,
                target_type=NodeType.PERSON,
                sources=["doc:s3"],
            ),
            # 2 outbound RELATED_TO[has_task] on loser (post-#029
            # replacement for the legacy TODO shape).
            _make_node("object:write", node_type=NodeType.OBJECT, subtype="task"),
            _make_node("object:review", node_type=NodeType.OBJECT, subtype="task"),
            _make_edge(
                bob_id,
                EdgeType.RELATED_TO,
                "object:write",
                source_type=NodeType.PERSON,
                target_type=NodeType.OBJECT,
                semantic_type="has_task",
            ),
            _make_edge(
                bob_id,
                EdgeType.RELATED_TO,
                "object:review",
                source_type=NodeType.PERSON,
                target_type=NodeType.OBJECT,
                semantic_type="has_task",
            ),
            _make_same_as_edge(alice_id, bob_id, confidence=0.92),
        ]
        await kg_collection.insert_many(docs)

        # Act
        result = await review_duplicate(
            database,
            user_id=_REVIEW_USER_ID,
            source_node_id=alice_id,
            target_node_id=bob_id,
            decision=ReviewDecision.CONFIRM,
            reviewed_by="alice@example.com",
            merge_strategy=MergeStrategy.KEEP_PRIMARY,
        )

        # Assert — winner/loser per tiebreaker.
        assert result.decision is ReviewDecision.CONFIRM
        assert result.winner_node_id == alice_id  # older wins
        assert result.loser_node_id == bob_id
        assert result.applied_strategy is MergeStrategy.KEEP_PRIMARY
        assert result.edges_transferred == 5  # 3 MENTIONS + 2 TODO

        # Winner has the loser's name in aliases.
        winner = await kg_collection.find_one({"_id": alice_id})
        assert winner is not None
        assert "alice s" in winner["aliases"]

        # Loser is tombstoned (merged_into + merged_at set).
        loser = await kg_collection.find_one({"_id": bob_id})
        assert loser is not None
        assert loser["merged_into"] == alice_id
        assert loser["merged_at"] is not None
        assert loser["merged_at"].tzinfo is not None

        # Zero non-SAME_AS edges still reference loser as source or target.
        leftover = await kg_collection.count_documents(
            {
                "kind": "edge",
                "type": {"$ne": EdgeType.SAME_AS.value},
                "$or": [
                    {"source_node_id": bob_id},
                    {"target_node_id": bob_id},
                ],
            }
        )
        assert leftover == 0

        # All 5 edges now reference winner.
        winner_edges = await kg_collection.count_documents(
            {
                "kind": "edge",
                "type": {"$ne": EdgeType.SAME_AS.value},
                "$or": [
                    {"source_node_id": alice_id},
                    {"target_node_id": alice_id},
                ],
            }
        )
        assert winner_edges == 5

        # SAME_AS audit edge: status=confirmed + reviewer + timestamps.
        audit_edge = await kg_collection.find_one({"_id": result.same_as_edge_id})
        assert audit_edge is not None
        assert audit_edge["properties"]["status"] == "confirmed"
        assert audit_edge["properties"]["reviewed_by"] == "alice@example.com"
        assert audit_edge["properties"]["reviewed_at"] is not None
        assert audit_edge["properties"]["updated_at"] is not None

    async def test_merge_properties_strategy(self, kg_collection, database) -> None:
        # Arrange — winner short description; loser longer + extra key.
        older = _NOW - timedelta(days=1)
        newer = _NOW
        a_id = build_node_id(NodeType.PERSON, "a")
        b_id = build_node_id(NodeType.PERSON, "b")
        docs: list[dict[str, Any]] = [
            _make_node(
                a_id,
                name="a",
                created_at=older,
                properties={"description": "short", "tags": ["a"]},
            ),
            _make_node(
                b_id,
                name="b",
                created_at=newer,
                properties={
                    "description": "a much longer story",
                    "tags": ["b"],
                    "extra": "x",
                },
            ),
            _make_same_as_edge(a_id, b_id, confidence=0.93),
        ]
        await kg_collection.insert_many(docs)

        # Act
        result = await review_duplicate(
            database,
            user_id=_REVIEW_USER_ID,
            source_node_id=a_id,
            target_node_id=b_id,
            decision=ReviewDecision.CONFIRM,
            reviewed_by="tester",
            merge_strategy=MergeStrategy.MERGE_PROPERTIES,
        )

        # Assert
        assert result.winner_node_id == a_id  # older
        winner = await kg_collection.find_one({"_id": a_id})
        assert winner is not None
        props = winner["properties"]
        # Longer string wins.
        assert props["description"] == "a much longer story"
        # Lists set-unioned.
        assert set(props["tags"]) == {"a", "b"}
        # Missing key taken from incoming.
        assert props["extra"] == "x"

    async def test_idempotent_second_confirm(self, kg_collection, database) -> None:
        # Arrange — confirm once, capture the state hash, confirm again.
        older = _NOW - timedelta(days=1)
        newer = _NOW
        a_id = build_node_id(NodeType.PERSON, "alice")
        b_id = build_node_id(NodeType.PERSON, "alice s")
        docs: list[dict[str, Any]] = [
            _make_node(a_id, name="alice", created_at=older),
            _make_node(b_id, name="alice s", created_at=newer),
            _make_same_as_edge(a_id, b_id, confidence=0.92),
        ]
        await kg_collection.insert_many(docs)

        # Act — first confirm.
        first = await review_duplicate(
            database,
            user_id=_REVIEW_USER_ID,
            source_node_id=a_id,
            target_node_id=b_id,
            decision=ReviewDecision.CONFIRM,
            reviewed_by="tester",
            merge_strategy=MergeStrategy.KEEP_PRIMARY,
        )
        winner_after_first = await kg_collection.find_one({"_id": a_id})
        assert winner_after_first is not None
        aliases_after_first = list(winner_after_first["aliases"])

        # Act — second confirm with identical args.
        second = await review_duplicate(
            database,
            user_id=_REVIEW_USER_ID,
            source_node_id=a_id,
            target_node_id=b_id,
            decision=ReviewDecision.CONFIRM,
            reviewed_by="tester",
            merge_strategy=MergeStrategy.KEEP_PRIMARY,
        )

        # Assert — same identifiers, same strategy.
        assert first.winner_node_id == second.winner_node_id
        assert first.loser_node_id == second.loser_node_id
        assert first.edges_transferred == second.edges_transferred
        assert first.same_as_edge_id == second.same_as_edge_id

        # No re-merge: winner state unchanged.
        winner_after_second = await kg_collection.find_one({"_id": a_id})
        assert winner_after_second is not None
        assert list(winner_after_second["aliases"]) == aliases_after_first

    async def test_reject_after_confirm_raises(self, kg_collection, database) -> None:
        # Arrange
        older = _NOW - timedelta(days=1)
        a_id = build_node_id(NodeType.PERSON, "a")
        b_id = build_node_id(NodeType.PERSON, "b")
        docs: list[dict[str, Any]] = [
            _make_node(a_id, created_at=older),
            _make_node(b_id),
            _make_same_as_edge(a_id, b_id),
        ]
        await kg_collection.insert_many(docs)

        await review_duplicate(
            database,
            user_id=_REVIEW_USER_ID,
            source_node_id=a_id,
            target_node_id=b_id,
            decision=ReviewDecision.CONFIRM,
            reviewed_by="tester",
            merge_strategy=MergeStrategy.KEEP_PRIMARY,
        )

        # Act / Assert
        with pytest.raises(ValueError) as exc:
            await review_duplicate(
                database,
                user_id=_REVIEW_USER_ID,
                source_node_id=a_id,
                target_node_id=b_id,
                decision=ReviewDecision.REJECT,
                reviewed_by="tester",
            )

        msg = str(exc.value)
        assert "confirmed" in msg
        assert build_edge_id(a_id, EdgeType.SAME_AS, b_id) in msg


# ---------------------------------------------------------------------------
# review_duplicate — REJECT
# ---------------------------------------------------------------------------


class TestReviewDuplicateReject:
    async def test_reject_marks_audit_and_leaves_nodes_alone(
        self, kg_collection, database
    ) -> None:
        # Arrange
        a_id = build_node_id(NodeType.PERSON, "apple inc")
        b_id = build_node_id(NodeType.PERSON, "apple")
        docs: list[dict[str, Any]] = [
            _make_node(a_id, name="apple inc"),
            _make_node(b_id, name="apple"),
            # One non-SAME_AS edge to confirm it survives.
            _make_node("chunk:c1", node_type=NodeType.CHUNK),
            _make_edge(
                "chunk:c1",
                EdgeType.MENTIONS,
                b_id,
                source_type=NodeType.CHUNK,
                target_type=NodeType.PERSON,
            ),
            _make_same_as_edge(a_id, b_id, confidence=0.88),
        ]
        await kg_collection.insert_many(docs)

        # Act
        result = await review_duplicate(
            database,
            user_id=_REVIEW_USER_ID,
            source_node_id=a_id,
            target_node_id=b_id,
            decision=ReviewDecision.REJECT,
            reviewed_by="alice",
        )

        # Assert — result shape.
        assert result.decision is ReviewDecision.REJECT
        assert result.winner_node_id is None
        assert result.loser_node_id is None
        assert result.applied_strategy is None
        assert result.edges_transferred == 0

        # Audit edge has reject + reviewer + timestamps.
        audit = await kg_collection.find_one({"_id": result.same_as_edge_id})
        assert audit is not None
        assert audit["properties"]["status"] == "rejected"
        assert audit["properties"]["reviewed_by"] == "alice"
        assert audit["properties"]["reviewed_at"] is not None

        # Neither node tombstoned.
        a_node = await kg_collection.find_one({"_id": a_id})
        b_node = await kg_collection.find_one({"_id": b_id})
        assert a_node is not None
        assert b_node is not None
        assert a_node.get("merged_into") is None
        assert b_node.get("merged_into") is None

        # Non-SAME_AS edge survives untouched.
        mention = await kg_collection.find_one(
            {
                "kind": "edge",
                "type": EdgeType.MENTIONS.value,
                "target_node_id": b_id,
            }
        )
        assert mention is not None

    async def test_confirm_after_reject_raises(self, kg_collection, database) -> None:
        # Arrange
        a_id = build_node_id(NodeType.PERSON, "a")
        b_id = build_node_id(NodeType.PERSON, "b")
        docs: list[dict[str, Any]] = [
            _make_node(a_id),
            _make_node(b_id),
            _make_same_as_edge(a_id, b_id),
        ]
        await kg_collection.insert_many(docs)

        await review_duplicate(
            database,
            user_id=_REVIEW_USER_ID,
            source_node_id=a_id,
            target_node_id=b_id,
            decision=ReviewDecision.REJECT,
            reviewed_by="alice",
        )

        # Act / Assert
        with pytest.raises(ValueError) as exc:
            await review_duplicate(
                database,
                user_id=_REVIEW_USER_ID,
                source_node_id=a_id,
                target_node_id=b_id,
                decision=ReviewDecision.CONFIRM,
                reviewed_by="alice",
            )

        assert "rejected" in str(exc.value)

    async def test_no_edge_raises(self, kg_collection, database) -> None:
        # Arrange — two nodes but no SAME_AS edge between them.
        await kg_collection.insert_many(
            [
                _make_node("person:a"),
                _make_node("person:b"),
            ]
        )

        # Act / Assert
        with pytest.raises(ValueError) as exc:
            await review_duplicate(
                database,
                user_id=_REVIEW_USER_ID,
                source_node_id="person:a",
                target_node_id="person:b",
                decision=ReviewDecision.REJECT,
                reviewed_by="alice",
            )

        assert "no SAME_AS edge" in str(exc.value)


# ---------------------------------------------------------------------------
# get_same_as_cluster
# ---------------------------------------------------------------------------


class TestGetSameAsCluster:
    async def test_single_hop_includes_self_and_immediate_neighbors(
        self, kg_collection, database
    ) -> None:
        # Arrange — graph: a <-confirmed-> b <-pending-> c <-rejected-> d.
        a, b, c, d = (build_node_id(NodeType.PERSON, x) for x in ("a", "b", "c", "d"))
        docs: list[dict[str, Any]] = [
            _make_node(a),
            _make_node(b),
            _make_node(c),
            _make_node(d),
            _make_same_as_edge(a, b, status="confirmed"),
            _make_same_as_edge(b, c, status="pending"),
            _make_same_as_edge(c, d, status="rejected"),
        ]
        await kg_collection.insert_many(docs)

        # Act
        cluster = await get_same_as_cluster(database, b, user_id=_REVIEW_USER_ID)

        # Assert — single-hop from b reaches a and c (in either direction).
        # d is two hops away — must not be included.
        assert cluster == {a, b, c}

    async def test_includes_input_even_when_no_edges(
        self, kg_collection, database
    ) -> None:
        # Arrange
        await kg_collection.insert_one(_make_node("person:lonely"))

        # Act
        cluster = await get_same_as_cluster(
            database, "person:lonely", user_id=_REVIEW_USER_ID
        )

        # Assert
        assert cluster == {"person:lonely"}


# ---------------------------------------------------------------------------
# MCP tool integration
# ---------------------------------------------------------------------------


class TestReviewMcpTools:
    async def test_list_pending_tool_returns_json_array(
        self, kg_collection, make_mcp_ctx
    ) -> None:
        # Arrange
        docs: list[dict[str, Any]] = [
            _make_node("person:a"),
            _make_node("person:b"),
            _make_same_as_edge("person:a", "person:b", confidence=0.91),
        ]
        await kg_collection.insert_many(docs)

        ctx = make_mcp_ctx()

        # Act
        raw = await review_list_pending(ctx=ctx, limit=10)

        # Assert
        parsed = json.loads(raw)
        assert isinstance(parsed, list)
        assert len(parsed) == 1
        assert parsed[0]["source_node_id"] == "person:a"
        assert parsed[0]["target_node_id"] == "person:b"
        assert parsed[0]["match_type"] == "embedding"

    async def test_confirm_tool_mutates_db_and_returns_json(
        self, kg_collection, make_mcp_ctx
    ) -> None:
        # Arrange
        older = _NOW - timedelta(days=1)
        a_id = build_node_id(NodeType.PERSON, "a")
        b_id = build_node_id(NodeType.PERSON, "b")
        await kg_collection.insert_many(
            [
                _make_node(a_id, created_at=older),
                _make_node(b_id),
                _make_same_as_edge(a_id, b_id),
            ]
        )

        ctx = make_mcp_ctx()

        # Act
        raw = await review_confirm(
            source_node_id=a_id,
            target_node_id=b_id,
            reviewed_by="alice",
            ctx=ctx,
            merge_strategy="keep_primary",
        )

        # Assert
        parsed = json.loads(raw)
        assert parsed["decision"] == "confirm"
        assert parsed["winner_node_id"] == a_id
        assert parsed["loser_node_id"] == b_id
        assert parsed["applied_strategy"] == "keep_primary"

        loser = await kg_collection.find_one({"_id": b_id})
        assert loser is not None
        assert loser["merged_into"] == a_id

    async def test_reject_tool_mutates_db_and_returns_json(
        self, kg_collection, make_mcp_ctx
    ) -> None:
        # Arrange
        a_id = build_node_id(NodeType.PERSON, "a")
        b_id = build_node_id(NodeType.PERSON, "b")
        await kg_collection.insert_many(
            [
                _make_node(a_id),
                _make_node(b_id),
                _make_same_as_edge(a_id, b_id),
            ]
        )
        ctx = make_mcp_ctx()

        # Act
        raw = await review_reject(
            source_node_id=a_id,
            target_node_id=b_id,
            reviewed_by="alice",
            ctx=ctx,
        )

        # Assert
        parsed = json.loads(raw)
        assert parsed["decision"] == "reject"
        assert parsed["winner_node_id"] is None
        assert parsed["edges_transferred"] == 0

        audit = await kg_collection.find_one(
            {"_id": build_edge_id(a_id, EdgeType.SAME_AS, b_id)}
        )
        assert audit is not None
        assert audit["properties"]["status"] == "rejected"

    async def test_terminal_state_surfaces_as_json_error(
        self, kg_collection, make_mcp_ctx
    ) -> None:
        # Arrange — confirm first, then try to reject.
        older = _NOW - timedelta(days=1)
        a_id = build_node_id(NodeType.PERSON, "a")
        b_id = build_node_id(NodeType.PERSON, "b")
        await kg_collection.insert_many(
            [
                _make_node(a_id, created_at=older),
                _make_node(b_id),
                _make_same_as_edge(a_id, b_id),
            ]
        )
        ctx = make_mcp_ctx()
        await review_confirm(
            source_node_id=a_id,
            target_node_id=b_id,
            reviewed_by="alice",
            ctx=ctx,
        )

        # Act
        raw = await review_reject(
            source_node_id=a_id,
            target_node_id=b_id,
            reviewed_by="alice",
            ctx=ctx,
        )

        # Assert — error is a structured JSON object, not a traceback.
        parsed = json.loads(raw)
        assert parsed["error"] == "invalid_state"
        assert "confirmed" in parsed["detail"]

    async def test_list_pending_invalid_entity_type_returns_error(
        self, kg_collection, make_mcp_ctx
    ) -> None:
        # Arrange
        ctx = make_mcp_ctx()

        # Act
        raw = await review_list_pending(ctx=ctx, entity_type="not_a_type", limit=10)

        # Assert
        parsed = json.loads(raw)
        assert parsed["error"] == "invalid_input"


# ---------------------------------------------------------------------------
# Script integrity
# ---------------------------------------------------------------------------


class TestCliScriptCallsInitLogger:
    """CLAUDE.md requires every memory-app script to call init_logger() at
    module level. Verify by reading the file."""

    def test_init_logger_called_at_module_level(self) -> None:
        # Arrange
        from pathlib import Path

        script = (
            Path(__file__).resolve().parents[3] / "scripts" / "review_duplicates.py"
        )

        # Act
        source = script.read_text(encoding="utf-8")

        # Assert — the call must happen at module load time, not inside a
        # function. Cheap check: the call appears before the first def/class.
        first_def = min(
            (source.find("\ndef "), source.find("\nclass ")),
            key=lambda i: i if i != -1 else len(source),
        )
        init_idx = source.find("init_logger()")
        assert init_idx != -1, "scripts/review_duplicates.py never calls init_logger()"
        assert init_idx < first_def, (
            "init_logger() must be called at module level (before any def/class)"
        )
