"""Unit tests for ``KGQuery``.

The class is a tenant-locked reader for ``knowledge_graph``: every read
must filter on ``self.user_id``. These tests exercise that contract via
``mocker.patch`` on the underlying ``KnowledgeGraphEntry.find`` /
``KnowledgeGraphEntry.find_one`` calls so we can inspect the exact
filter dict that hit Beanie.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from beanie import PydanticObjectId

from tree.entities.knowledge_graph import EdgeType, KnowledgeGraphEntry, NodeType
from tree.memory.query.kgquery import KGQuery


_USER_A = PydanticObjectId("507f1f77bcf86cd799439011")
_USER_B = PydanticObjectId("507f1f77bcf86cd799439022")


def _patch_find(mocker, return_value=None):
    """Patch ``KnowledgeGraphEntry.find`` to capture the filter argument.

    Beanie's ``find`` returns a query builder; the next ``.to_list()`` call
    is what actually issues the find. We return a mock that records the
    filter and surfaces a ``to_list`` mock.
    """

    cursor = MagicMock(name="cursor")
    cursor.to_list = AsyncMock(return_value=return_value or [])
    find_mock = MagicMock(name="find", return_value=cursor)
    mocker.patch.object(KnowledgeGraphEntry, "find", find_mock)
    return find_mock


def _patch_find_one(mocker, return_value=None):
    return mocker.patch.object(
        KnowledgeGraphEntry,
        "find_one",
        new=AsyncMock(return_value=return_value),
    )


class TestKGQueryInit:
    def test_binds_user_id(self) -> None:
        q = KGQuery(_USER_A)
        assert q.user_id == _USER_A


class TestFindNodes:
    async def test_injects_user_id_into_filter(self, mocker) -> None:
        find_mock = _patch_find(mocker)
        await KGQuery(_USER_A).find_nodes(type=NodeType.PERSON)
        find_mock.assert_called_once()
        call_filter = find_mock.call_args.args[0]
        assert call_filter["user_id"] == _USER_A
        assert call_filter["kind"] == "node"
        assert call_filter["type"] == NodeType.PERSON.value

    async def test_strips_caller_supplied_user_id(self, mocker) -> None:
        """Even when the caller passes ``filter={"user_id": SOMEONE_ELSE}``,
        the bound ``self.user_id`` is used."""

        find_mock = _patch_find(mocker)
        await KGQuery(_USER_A).find_nodes(filter={"user_id": _USER_B})
        call_filter = find_mock.call_args.args[0]
        assert call_filter["user_id"] == _USER_A

    async def test_caller_filter_keys_are_preserved(self, mocker) -> None:
        find_mock = _patch_find(mocker)
        await KGQuery(_USER_A).find_nodes(filter={"properties.is_active_user": True})
        call_filter = find_mock.call_args.args[0]
        assert call_filter["user_id"] == _USER_A
        assert call_filter["properties.is_active_user"] is True

    async def test_two_users_disjoint(self, mocker) -> None:
        """``KGQuery(A)`` and ``KGQuery(B)`` issue distinct filters."""

        find_mock = _patch_find(mocker)
        await KGQuery(_USER_A).find_nodes()
        await KGQuery(_USER_B).find_nodes()

        first = find_mock.call_args_list[0].args[0]
        second = find_mock.call_args_list[1].args[0]
        assert first["user_id"] == _USER_A
        assert second["user_id"] == _USER_B


class TestFindNodeById:
    async def test_includes_user_id_filter(self, mocker) -> None:
        find_one_mock = _patch_find_one(mocker)
        await KGQuery(_USER_A).find_node_by_id("xyz:person:alice")
        call_filter = find_one_mock.call_args.args[0]
        assert call_filter == {
            "_id": "xyz:person:alice",
            "user_id": _USER_A,
            "kind": "node",
        }


class TestFindSelfPerson:
    async def test_builds_exact_filter(self, mocker) -> None:
        """``find_self_person`` issues the exact filter the spec mandates."""

        find_one_mock = _patch_find_one(mocker)
        await KGQuery(_USER_A).find_self_person()
        call_filter = find_one_mock.call_args.args[0]
        assert call_filter == {
            "user_id": _USER_A,
            "kind": "node",
            "type": NodeType.PERSON.value,
            "properties.is_active_user": True,
        }


class TestFindEdges:
    async def test_injects_user_id_and_kind(self, mocker) -> None:
        find_mock = _patch_find(mocker)
        await KGQuery(_USER_A).find_edges(type=EdgeType.MENTIONS)
        call_filter = find_mock.call_args.args[0]
        assert call_filter["user_id"] == _USER_A
        assert call_filter["kind"] == "edge"
        assert call_filter["type"] == EdgeType.MENTIONS.value

    async def test_strips_caller_supplied_user_id(self, mocker) -> None:
        find_mock = _patch_find(mocker)
        await KGQuery(_USER_A).find_edges(filter={"user_id": _USER_B})
        call_filter = find_mock.call_args.args[0]
        assert call_filter["user_id"] == _USER_A


class TestFindNeighbors:
    async def test_returns_empty_for_zero_hops(self, mocker) -> None:
        _patch_find(mocker)
        out = await KGQuery(_USER_A).find_neighbors("xyz:person:alice", max_hops=0)
        assert out == []

    async def test_first_hop_filter_includes_user_id(self, mocker) -> None:
        find_mock = _patch_find(mocker, return_value=[])
        await KGQuery(_USER_A).find_neighbors("xyz:person:alice", max_hops=1)
        # First call is the single hop.
        call_filter = find_mock.call_args_list[0].args[0]
        assert call_filter["user_id"] == _USER_A
        assert call_filter["kind"] == "edge"

    async def test_edge_type_filter_applied(self, mocker) -> None:
        # Post-#029 ``EdgeType.TODO`` is gone; the umbrella ``RELATED_TO``
        # replaces it (semantic discrimination via ``semantic_type``).
        find_mock = _patch_find(mocker, return_value=[])
        await KGQuery(_USER_A).find_neighbors(
            "xyz:person:alice",
            edge_types=[EdgeType.RELATED_TO, EdgeType.MENTIONS],
            max_hops=1,
        )
        call_filter = find_mock.call_args.args[0]
        assert call_filter["type"] == {"$in": ["related_to", "mentions"]}


class TestUserIdValidation:
    def test_none_user_id_rejected(self) -> None:
        with pytest.raises(ValueError, match="user_id"):
            KGQuery(None)  # type: ignore[arg-type]
