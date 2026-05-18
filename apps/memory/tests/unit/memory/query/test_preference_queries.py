"""Unit tests for ``KGQuery.find_current_preferences`` and
``find_preferences_at`` (#032).

Tests mock the underlying Beanie ``find`` so we can inspect the
exact filter dict that hits Mongo - the bi-temporal predicate is the
load-bearing detail.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

from beanie import PydanticObjectId

from tree.entities.knowledge_graph import KnowledgeGraphEntry
from tree.entities.ontology import PreferenceCategory
from tree.memory.query.kgquery import KGQuery

_USER_ID = PydanticObjectId("507f1f77bcf86cd799439011")


def _patch_find(mocker, return_value=None):
    cursor = MagicMock(name="cursor")
    cursor.to_list = AsyncMock(return_value=return_value or [])
    find_mock = MagicMock(name="find", return_value=cursor)
    mocker.patch.object(KnowledgeGraphEntry, "find", find_mock)
    return find_mock


class TestFindCurrentPreferences:
    async def test_filters_on_user_kind_and_type(self, mocker) -> None:
        find_mock = _patch_find(mocker)

        await KGQuery(_USER_ID).find_current_preferences()

        f = find_mock.call_args.args[0]
        assert f["user_id"] == _USER_ID
        assert f["kind"] == "node"
        assert f["type"] == "preference"

    async def test_filters_valid_until_is_none(self, mocker) -> None:
        find_mock = _patch_find(mocker)

        await KGQuery(_USER_ID).find_current_preferences()

        f = find_mock.call_args.args[0]
        # "Current" = valid_until is missing or null.
        assert "$or" in f
        assert {"valid_until": {"$exists": False}} in f["$or"]
        assert {"valid_until": None} in f["$or"]

    async def test_category_filter_targets_properties_category(self, mocker) -> None:
        find_mock = _patch_find(mocker)

        await KGQuery(_USER_ID).find_current_preferences(category=PreferenceCategory.UI)

        f = find_mock.call_args.args[0]
        assert f["properties.category"] == "ui"


class TestFindPreferencesAt:
    async def test_temporal_predicate(self, mocker) -> None:
        find_mock = _patch_find(mocker)
        ts = datetime(2025, 6, 1, tzinfo=UTC)

        await KGQuery(_USER_ID).find_preferences_at(ts)

        f = find_mock.call_args.args[0]
        assert f["user_id"] == _USER_ID
        assert f["type"] == "preference"
        assert "$and" in f
        # The temporal predicate is two $or branches:
        #   valid_from <= ts (or null)
        #   valid_until > ts (or null)
        # Pinned shape so a future refactor that drops the null
        # branch fails loudly (we'd start missing preferences whose
        # valid_from was never set).
        from_branch = f["$and"][0]
        assert "$or" in from_branch
        assert {"valid_from": {"$lte": ts}} in from_branch["$or"]
        until_branch = f["$and"][1]
        assert "$or" in until_branch
        assert {"valid_until": {"$gt": ts}} in until_branch["$or"]
        assert {"valid_until": None} in until_branch["$or"]

    async def test_category_filter_pass_through(self, mocker) -> None:
        find_mock = _patch_find(mocker)
        ts = datetime(2025, 6, 1, tzinfo=UTC)

        await KGQuery(_USER_ID).find_preferences_at(
            ts, category=PreferenceCategory.FOOD
        )

        f = find_mock.call_args.args[0]
        assert f["properties.category"] == "food"
