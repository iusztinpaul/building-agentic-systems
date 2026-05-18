"""Unit tests for ``KGQuery.find_facts`` and ``find_facts_by_similarity`` (#031).

The unit-level tests use ``mocker.patch`` on the underlying Beanie
collection calls so we can inspect the exact filter dict that hit
Mongo without standing up an integration database. The
``user_id``-scoping invariant is the centerpiece: every fact read must
filter on the bound ``self.user_id`` AND on ``type == "fact"`` AND on
``kind == "node"``.

Integration coverage of the helpers against real Mongo (with mongot
for the vector path) lives at
``tests/integration/memory/test_find_facts.py``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from beanie import PydanticObjectId

from tree.entities.knowledge_graph import KnowledgeGraphEntry
from tree.memory.query.kgquery import KGQuery


_USER_A = PydanticObjectId("507f1f77bcf86cd799439011")
_USER_B = PydanticObjectId("507f1f77bcf86cd799439022")


def _patch_find(mocker, return_value=None):
    cursor = MagicMock(name="cursor")
    cursor.to_list = AsyncMock(return_value=return_value or [])
    find_mock = MagicMock(name="find", return_value=cursor)
    mocker.patch.object(KnowledgeGraphEntry, "find", find_mock)
    return find_mock


# ---------------------------------------------------------------------------
# find_facts — exact-match string lookup
# ---------------------------------------------------------------------------


class TestFindFactsScoping:
    async def test_filters_on_user_kind_and_type(self, mocker) -> None:
        find_mock = _patch_find(mocker)
        await KGQuery(_USER_A).find_facts()
        find_mock.assert_called_once()
        call_filter = find_mock.call_args.args[0]
        assert call_filter["user_id"] == _USER_A
        assert call_filter["kind"] == "node"
        assert call_filter["type"] == "fact"

    async def test_two_users_disjoint(self, mocker) -> None:
        find_mock = _patch_find(mocker)
        await KGQuery(_USER_A).find_facts(subject="earth")
        first_filter = find_mock.call_args.args[0]
        assert first_filter["user_id"] == _USER_A

        await KGQuery(_USER_B).find_facts(subject="earth")
        second_filter = find_mock.call_args.args[0]
        assert second_filter["user_id"] == _USER_B


class TestFindFactsFilters:
    async def test_subject_filter_targets_properties_subject(self, mocker) -> None:
        find_mock = _patch_find(mocker)
        await KGQuery(_USER_A).find_facts(subject="earth")
        call_filter = find_mock.call_args.args[0]
        assert call_filter["properties.subject"] == "earth"
        assert "properties.predicate" not in call_filter
        assert "properties.object" not in call_filter

    async def test_predicate_filter(self, mocker) -> None:
        find_mock = _patch_find(mocker)
        await KGQuery(_USER_A).find_facts(predicate="orbits")
        call_filter = find_mock.call_args.args[0]
        assert call_filter["properties.predicate"] == "orbits"

    async def test_object_filter_targets_wire_form_key(self, mocker) -> None:
        # ``FactProperties.object_`` has ``alias="object"``; the
        # wire-form key on the stored doc is ``properties.object``, so
        # the helper MUST filter under that key (not ``object_``).
        find_mock = _patch_find(mocker)
        await KGQuery(_USER_A).find_facts(object="sun")
        call_filter = find_mock.call_args.args[0]
        assert call_filter["properties.object"] == "sun"
        assert "properties.object_" not in call_filter

    async def test_all_three_filters_combined(self, mocker) -> None:
        find_mock = _patch_find(mocker)
        await KGQuery(_USER_A).find_facts(
            subject="earth", predicate="orbits", object="sun"
        )
        call_filter = find_mock.call_args.args[0]
        assert call_filter["properties.subject"] == "earth"
        assert call_filter["properties.predicate"] == "orbits"
        assert call_filter["properties.object"] == "sun"

    async def test_no_filter_returns_all_facts_for_user(self, mocker) -> None:
        find_mock = _patch_find(mocker)
        await KGQuery(_USER_A).find_facts()
        call_filter = find_mock.call_args.args[0]
        # Only the three scoping keys.
        assert set(call_filter.keys()) == {"user_id", "kind", "type"}


# ---------------------------------------------------------------------------
# find_facts_by_similarity — vector search pre-filter
# ---------------------------------------------------------------------------


class TestFindFactsBySimilarity:
    async def test_vector_search_filter_is_user_kind_and_fact_type(
        self, mocker
    ) -> None:
        # Patch the motor-collection accessor so we can capture the
        # aggregation pipeline that hits Mongo without standing up a
        # real database.
        agg_cursor = MagicMock(name="agg_cursor")
        agg_cursor.to_list = AsyncMock(return_value=[])
        collection_mock = MagicMock(name="motor_collection")
        collection_mock.aggregate = AsyncMock(return_value=agg_cursor)
        mocker.patch.object(
            KnowledgeGraphEntry,
            "get_pymongo_collection",
            return_value=collection_mock,
        )

        await KGQuery(_USER_A).find_facts_by_similarity([0.1] * 8, k=3)

        collection_mock.aggregate.assert_awaited_once()
        pipeline = collection_mock.aggregate.call_args.args[0]
        assert len(pipeline) == 1
        vs = pipeline[0]["$vectorSearch"]
        assert vs["filter"]["user_id"] == _USER_A
        assert vs["filter"]["kind"] == "node"
        assert vs["filter"]["type"] == "fact"
        assert vs["limit"] == 3
        # numCandidates is bumped to give the vector index enough room
        # to find good top-k matches.
        assert vs["numCandidates"] >= 3

    async def test_empty_result_when_vector_search_fails(self, mocker) -> None:
        # When mongot is unavailable, the helper logs at WARNING and
        # returns an empty list instead of propagating the exception.
        collection_mock = MagicMock(name="motor_collection")
        collection_mock.aggregate = AsyncMock(
            side_effect=RuntimeError("mongot unreachable")
        )
        mocker.patch.object(
            KnowledgeGraphEntry,
            "get_pymongo_collection",
            return_value=collection_mock,
        )
        rows = await KGQuery(_USER_A).find_facts_by_similarity([0.0] * 8)
        assert rows == []
