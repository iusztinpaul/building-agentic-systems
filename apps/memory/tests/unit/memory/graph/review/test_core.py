"""Unit tests for the review module's pure logic.

Per project convention (and [[feedback_mcp_tests_integration]]) every
behavioral path through ``review_duplicate`` / ``find_pending_duplicates``
/ ``get_same_as_cluster`` that touches MongoDB lives in the integration
suite. Here we only cover:

* The :func:`_decide_winner` tiebreaker — pure Python, no DB.
* The :class:`PendingDuplicate` / :class:`ReviewResult` dataclass shapes.
* The ``find_pending_duplicates`` ``limit <= 0`` short-circuit (returns
  an empty list without ever touching Mongo).
* The ``sources`` provenance a confirmed merge writes on the winner (a
  mocked collection; edge transfer patched out).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from beanie import PydanticObjectId
from bson import ObjectId

from tree.entities.memory import NodeType
from tree.memory.graph.review.core import (
    _decide_winner,
    _handle_confirm,
    find_pending_duplicates,
)
from tree.memory.graph.review.types import (
    MergeStrategy,
    PendingDuplicate,
    ReviewDecision,
    ReviewResult,
)


_NOW = datetime(2026, 5, 14, 12, 0, 0, tzinfo=UTC)
_USER_ID = PydanticObjectId()


# ---------------------------------------------------------------------------
# _decide_winner — tiebreaker
# ---------------------------------------------------------------------------


class TestDecideWinner:
    def test_older_created_at_wins(self) -> None:
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

        winner, loser = _decide_winner(older, newer)

        # Assert — older wins despite lower confidence and later _id.
        assert winner["_id"] == "person:b"
        assert loser["_id"] == "person:a"

    def test_higher_confidence_wins_on_created_at_tie(self) -> None:
        low = {"_id": "person:a", "created_at": _NOW, "confidence": 0.5}
        high = {"_id": "person:b", "created_at": _NOW, "confidence": 0.9}

        winner, loser = _decide_winner(low, high)

        assert winner["_id"] == "person:b"
        assert loser["_id"] == "person:a"

    def test_lex_id_wins_on_full_tie(self) -> None:
        a = {"_id": "person:alice", "created_at": _NOW, "confidence": 0.8}
        b = {"_id": "person:bob", "created_at": _NOW, "confidence": 0.8}

        winner, loser = _decide_winner(b, a)

        # Assert — "person:alice" < "person:bob" lexicographically.
        assert winner["_id"] == "person:alice"
        assert loser["_id"] == "person:bob"

    def test_missing_created_at_falls_through_to_confidence(self) -> None:
        # Arrange — neither side has created_at.
        a = {"_id": "person:a", "confidence": 0.7}
        b = {"_id": "person:b", "confidence": 0.4}

        winner, loser = _decide_winner(a, b)

        assert winner["_id"] == "person:a"
        assert loser["_id"] == "person:b"


# ---------------------------------------------------------------------------
# Dataclass shapes
# ---------------------------------------------------------------------------


class TestPendingDuplicate:
    def test_constructs_with_all_fields(self) -> None:
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

        assert p.entity_type is NodeType.PERSON
        assert p.match_type == "embedding"
        assert p.flagged_at == _NOW

    def test_is_frozen(self) -> None:
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

        with pytest.raises(Exception):  # FrozenInstanceError ≤ Exception
            p.source_node_id = "person:c"  # type: ignore[misc]


class TestReviewResult:
    def test_confirm_shape(self) -> None:
        r = ReviewResult(
            decision=ReviewDecision.CONFIRM,
            winner_node_id="person:a",
            loser_node_id="person:b",
            applied_strategy=MergeStrategy.KEEP_PRIMARY,
            edges_transferred=5,
            same_as_edge_id="person:a|same_as|person:b",
        )

        assert r.decision is ReviewDecision.CONFIRM
        assert r.edges_transferred == 5

    def test_reject_shape(self) -> None:
        r = ReviewResult(
            decision=ReviewDecision.REJECT,
            winner_node_id=None,
            loser_node_id=None,
            applied_strategy=None,
            edges_transferred=0,
            same_as_edge_id="person:a|same_as|person:b",
        )

        assert r.winner_node_id is None
        assert r.applied_strategy is None


# ---------------------------------------------------------------------------
# find_pending_duplicates — short-circuit on non-positive limit
# ---------------------------------------------------------------------------


class TestFindPendingDuplicatesShortCircuit:
    async def test_zero_limit_returns_empty_without_db_call(self, mocker) -> None:
        database = mocker.MagicMock()
        # Sentry: if the function touches the database at all, this AsyncMock
        # would surface in the call record.
        collection = mocker.MagicMock()
        collection.aggregate = AsyncMock()
        database.__getitem__.return_value = collection

        result = await find_pending_duplicates(database, user_id=_USER_ID, limit=0)

        assert result == []
        collection.aggregate.assert_not_called()

    async def test_negative_limit_returns_empty_without_db_call(self, mocker) -> None:
        database = mocker.MagicMock()
        collection = mocker.MagicMock()
        collection.aggregate = AsyncMock()
        database.__getitem__.return_value = collection

        result = await find_pending_duplicates(database, user_id=_USER_ID, limit=-5)

        assert result == []
        collection.aggregate.assert_not_called()


# ---------------------------------------------------------------------------
# _handle_confirm — the winner's ``sources`` provenance
# ---------------------------------------------------------------------------


_WINNER_ID = "person:alice"
_LOSER_ID = "person:alyce"
_SAME_AS_ID = f"{_LOSER_ID}|same_as|{_WINNER_ID}"


def _confirm_collection(loser_sources: list[ObjectId]) -> MagicMock:
    """A collection holding an older winner and a newer loser (the tiebreak)."""

    rows = {
        _WINNER_ID: {
            "_id": _WINNER_ID,
            "type": "person",
            "name": "alice",
            "sources": [ObjectId()],
            "created_at": _NOW - timedelta(days=1),
        },
        _LOSER_ID: {
            "_id": _LOSER_ID,
            "type": "person",
            "name": "alyce",
            "sources": loser_sources,
            "created_at": _NOW,
        },
    }
    collection = MagicMock(name="memory")
    collection.find_one = AsyncMock(side_effect=lambda query: rows.get(query["_id"]))
    collection.update_one = AsyncMock()
    return collection


async def _confirm(mocker: Any, collection: MagicMock) -> None:
    mocker.patch(
        "tree.memory.graph.review.core._transfer_edges",
        new=AsyncMock(return_value=0),
    )
    await _handle_confirm(
        collection=collection,
        user_id=_USER_ID,
        edge_doc={"source_node_id": _LOSER_ID, "target_node_id": _WINNER_ID},
        edge_id=_SAME_AS_ID,
        current_status="pending",
        reviewed_by="tester",
        merge_strategy=MergeStrategy.KEEP_PRIMARY,
        now=_NOW,
    )


def _winner_sources_unions(collection: MagicMock) -> list[list[Any]]:
    """Every list unioned into the winner's ``sources``, in write order."""

    unions = []
    for call in collection.update_one.call_args_list:
        query, update = call.args[0], call.args[1]
        if query["_id"] != _WINNER_ID or "sources" not in update[0]["$set"]:
            continue
        expr = update[0]["$set"]["sources"]
        set_union = expr["$slice"][0] if "$slice" in expr else expr
        unions.append(set_union["$setUnion"][1])
    return unions


class TestConfirmMergeProvenance:
    async def test_winner_gains_every_loser_source_as_an_objectid(self, mocker) -> None:
        doc_a, doc_b = ObjectId(), ObjectId()
        collection = _confirm_collection(loser_sources=[doc_a, doc_b])

        await _confirm(mocker, collection)

        # Assert: the strategy unions the first, the post-merge $setUnion all of
        # them — ObjectIds, exactly as Mongo returned them.
        unions = _winner_sources_unions(collection)
        assert unions == [[doc_a], [doc_a, doc_b]]
        assert all(isinstance(source, ObjectId) for union in unions for source in union)

    async def test_a_loser_without_sources_adds_no_provenance(self, mocker) -> None:
        collection = _confirm_collection(loser_sources=[])

        await _confirm(mocker, collection)

        # Assert: the merge still lands (aliases) but writes no `sources` and no
        # synthetic `merge:` id anywhere.
        winner_writes = [
            call
            for call in collection.update_one.call_args_list
            if call.args[0]["_id"] == _WINNER_ID
        ]
        assert len(winner_writes) == 1
        set_stage = winner_writes[0].args[1][0]["$set"]
        assert "aliases" in set_stage
        assert "sources" not in set_stage
        assert "merge:" not in repr(collection.update_one.call_args_list)
