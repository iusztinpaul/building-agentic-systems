"""Unit tests for the review module's pure logic.

Per project convention (and [[feedback_mcp_tests_integration]]) every
behavioral path through ``review_duplicate`` / ``find_pending_duplicates``
/ ``get_same_as_cluster`` that touches MongoDB lives in the integration
suite. Here we only cover:

* The :func:`_decide_winner` tiebreaker — pure Python, no DB.
* The :class:`PendingDuplicate` / :class:`ReviewResult` dataclass shapes.
* The ``find_pending_duplicates`` ``limit <= 0`` short-circuit (returns
  an empty list without ever touching Mongo).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest

from tree.entities.knowledge_graph import NodeType
from tree.memory.review.core import _decide_winner, find_pending_duplicates
from tree.memory.review.types import (
    MergeStrategy,
    PendingDuplicate,
    ReviewDecision,
    ReviewResult,
)


_NOW = datetime(2026, 5, 14, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# _decide_winner — tiebreaker
# ---------------------------------------------------------------------------


class TestDecideWinner:
    def test_older_created_at_wins(self) -> None:
        # Arrange
        older = {
            "_id": "person:b",
            "created_at": _NOW - timedelta(days=2),
            "confidence": 0.5,
        }
        newer = {
            "_id": "person:a",
            "created_at": _NOW,
            "confidence": 0.99,
        }

        # Act
        winner, loser = _decide_winner(older, newer)

        # Assert — older wins despite lower confidence and later _id.
        assert winner["_id"] == "person:b"
        assert loser["_id"] == "person:a"

    def test_higher_confidence_wins_on_created_at_tie(self) -> None:
        # Arrange
        low = {"_id": "person:a", "created_at": _NOW, "confidence": 0.5}
        high = {"_id": "person:b", "created_at": _NOW, "confidence": 0.9}

        # Act
        winner, loser = _decide_winner(low, high)

        # Assert
        assert winner["_id"] == "person:b"
        assert loser["_id"] == "person:a"

    def test_lex_id_wins_on_full_tie(self) -> None:
        # Arrange
        a = {"_id": "person:alice", "created_at": _NOW, "confidence": 0.8}
        b = {"_id": "person:bob", "created_at": _NOW, "confidence": 0.8}

        # Act
        winner, loser = _decide_winner(b, a)

        # Assert — "person:alice" < "person:bob" lexicographically.
        assert winner["_id"] == "person:alice"
        assert loser["_id"] == "person:bob"

    def test_missing_created_at_falls_through_to_confidence(self) -> None:
        # Arrange — neither side has created_at.
        a = {"_id": "person:a", "confidence": 0.7}
        b = {"_id": "person:b", "confidence": 0.4}

        # Act
        winner, loser = _decide_winner(a, b)

        # Assert
        assert winner["_id"] == "person:a"
        assert loser["_id"] == "person:b"


# ---------------------------------------------------------------------------
# Dataclass shapes
# ---------------------------------------------------------------------------


class TestPendingDuplicate:
    def test_constructs_with_all_fields(self) -> None:
        # Arrange / Act
        p = PendingDuplicate(
            source_node_id="person:a",
            target_node_id="person:b",
            source_name="alice",
            target_name="alice smith",
            entity_type=NodeType.PERSON,
            similarity_score=0.92,
            match_type="embedding",
            flagged_at=_NOW,
            edge_id="person:a|same_as|person:b",
        )

        # Assert
        assert p.entity_type is NodeType.PERSON
        assert p.match_type == "embedding"
        assert p.flagged_at == _NOW

    def test_is_frozen(self) -> None:
        # Arrange
        p = PendingDuplicate(
            source_node_id="person:a",
            target_node_id="person:b",
            source_name="a",
            target_name="b",
            entity_type=NodeType.PERSON,
            similarity_score=0.9,
            match_type="embedding",
            flagged_at=_NOW,
            edge_id="e",
        )

        # Act / Assert
        with pytest.raises(Exception):  # FrozenInstanceError ≤ Exception
            p.source_node_id = "person:c"  # type: ignore[misc]


class TestReviewResult:
    def test_confirm_shape(self) -> None:
        # Arrange / Act
        r = ReviewResult(
            decision=ReviewDecision.CONFIRM,
            winner_node_id="person:a",
            loser_node_id="person:b",
            applied_strategy=MergeStrategy.KEEP_PRIMARY,
            edges_transferred=5,
            same_as_edge_id="person:a|same_as|person:b",
        )

        # Assert
        assert r.decision is ReviewDecision.CONFIRM
        assert r.edges_transferred == 5

    def test_reject_shape(self) -> None:
        # Arrange / Act
        r = ReviewResult(
            decision=ReviewDecision.REJECT,
            winner_node_id=None,
            loser_node_id=None,
            applied_strategy=None,
            edges_transferred=0,
            same_as_edge_id="person:a|same_as|person:b",
        )

        # Assert
        assert r.winner_node_id is None
        assert r.applied_strategy is None


# ---------------------------------------------------------------------------
# find_pending_duplicates — short-circuit on non-positive limit
# ---------------------------------------------------------------------------


class TestFindPendingDuplicatesShortCircuit:
    async def test_zero_limit_returns_empty_without_db_call(self, mocker) -> None:
        # Arrange
        database = mocker.MagicMock()
        # Sentry: if the function touches the database at all, this AsyncMock
        # would surface in the call record.
        collection = mocker.MagicMock()
        collection.aggregate = AsyncMock()
        database.__getitem__.return_value = collection

        # Act
        result = await find_pending_duplicates(database, limit=0)

        # Assert
        assert result == []
        collection.aggregate.assert_not_called()

    async def test_negative_limit_returns_empty_without_db_call(self, mocker) -> None:
        # Arrange
        database = mocker.MagicMock()
        collection = mocker.MagicMock()
        collection.aggregate = AsyncMock()
        database.__getitem__.return_value = collection

        # Act
        result = await find_pending_duplicates(database, limit=-5)

        # Assert
        assert result == []
        collection.aggregate.assert_not_called()
