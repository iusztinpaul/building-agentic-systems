"""Unit tests for the ``User`` Beanie entity and its self-person hook.

These tests cover model shape, default values, the ``canonical_name``
fallback rule, and the payload produced by ``after_insert`` (with the
underlying Mongo collection mocked via ``pytest-mock``).
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from beanie import PydanticObjectId

from tree.config.app_config import app_config
from tree.entities.memory import (
    MemoryEntry,
    NodeType,
    build_node_id,
    memory_indexes,
)
from tree.entities.users import User, select_active_user_ids


class TestUserModel:
    async def test_minimal_user_constructs_with_defaults(self):
        user = User(identifier="paul@example.com")

        assert user.identifier == "paul@example.com"
        assert user.attributes == {}
        assert isinstance(user.created_at, datetime)
        assert isinstance(user.updated_at, datetime)
        assert user.created_at.tzinfo is not None
        assert user.updated_at.tzinfo is not None
        assert user.created_at.utcoffset() == UTC.utcoffset(user.created_at)

    async def test_user_accepts_attributes_dict(self):
        user = User(
            identifier="paul@example.com",
            attributes={"name": "Paul", "locale": "en-US"},
        )

        assert user.attributes == {"name": "Paul", "locale": "en-US"}

    async def test_model_dump_round_trips(self):
        user = User(identifier="paul@example.com", attributes={"name": "Paul"})

        dumped = user.model_dump()

        assert dumped["identifier"] == "paul@example.com"
        assert dumped["attributes"] == {"name": "Paul"}
        assert "created_at" in dumped
        assert "updated_at" in dumped

    async def test_no_self_person_id_field_exists(self):
        """Decision #1 / decision #3: there is NO ``self_person_id`` field on
        ``User``. The ``is_active_user`` flag on the KG node is the single
        source of truth. This guard makes accidental reintroduction a test
        failure."""

        assert "self_person_id" not in User.model_fields


class TestBuildSelfPersonId:
    """The transitional ``_build_self_person_id`` helper from #017 was
    retired in #018 in favour of the canonical
    :func:`tree.entities.memory.build_node_id`. These tests
    now exercise the canonical builder for the ``person:self`` shape."""

    def test_id_shape_is_user_id_colon_person_colon_self(self):
        user_id = PydanticObjectId()

        result = build_node_id(user_id, NodeType.PERSON, "self")

        assert result == f"{user_id}:person:self"

    def test_two_users_get_two_distinct_ids(self):
        a = PydanticObjectId()
        b = PydanticObjectId()

        assert build_node_id(a, NodeType.PERSON, "self") != build_node_id(
            b, NodeType.PERSON, "self"
        )


class TestAfterInsertHook:
    """Verify the payload the hook upserts into ``memory`` in ``graphrag``.

    We bypass Beanie's real pymongo collection by patching
    ``MemoryEntry.get_pymongo_collection`` to return an ``AsyncMock``
    and inspect the ``update_one`` call's arguments. The payload tests pin
    ``graphrag`` (the only mode that writes the self node); ``rag`` writes
    nothing (task 170).
    """

    @pytest.fixture(autouse=True)
    def _graphrag_mode(self, monkeypatch) -> None:
        monkeypatch.setattr(app_config.memory, "mode", "graphrag")

    async def _run_hook(
        self,
        mocker,
        *,
        identifier: str,
        attributes: dict,
    ) -> tuple[User, AsyncMock]:
        user = User(identifier=identifier, attributes=attributes)
        # Give the user a stable id so we can assert on the resulting _id.
        user.id = PydanticObjectId()

        fake_collection = AsyncMock()
        mocker.patch.object(
            MemoryEntry,
            "get_pymongo_collection",
            return_value=fake_collection,
        )

        await user.after_insert()

        return user, fake_collection

    async def test_writes_upsert_with_expected_id_and_payload(self, mocker):
        user, fake_collection = await self._run_hook(
            mocker,
            identifier="paul@example.com",
            attributes={"name": "Paul"},
        )

        fake_collection.update_one.assert_awaited_once()
        args, kwargs = fake_collection.update_one.call_args

        filter_arg = args[0] if args else kwargs["filter"]
        update_arg = args[1] if len(args) > 1 else kwargs["update"]

        assert filter_arg == {"_id": f"{user.id}:person:self"}
        assert kwargs.get("upsert") is True

        set_on_insert = update_arg["$setOnInsert"]
        assert set_on_insert["kind"] == "node"
        assert set_on_insert["type"] == NodeType.PERSON.value
        assert set_on_insert["name"] == "self"
        assert set_on_insert["canonical_name"] == "Paul"
        # Per #018: the tenant ``user_id`` is stamped on the row.
        assert set_on_insert["user_id"] == user.id
        # Flag wins, attributes mirrored after.
        assert set_on_insert["properties"]["is_active_user"] is True
        assert set_on_insert["properties"]["name"] == "Paul"
        # Timestamps are tz-aware UTC.
        assert set_on_insert["created_at"].tzinfo is not None
        assert set_on_insert["updated_at"].tzinfo is not None

    async def test_self_node_is_seeded_without_an_embedding(self, mocker):
        # Task 175: pending = an ABSENT ``embedding`` (the backfill selects
        # ``{"embedding": None}``), never an empty vector.
        _user, fake_collection = await self._run_hook(
            mocker, identifier="paul@example.com", attributes={}
        )

        set_on_insert = fake_collection.update_one.call_args.args[1]["$setOnInsert"]

        assert "embedding" not in set_on_insert

    async def test_canonical_name_falls_back_to_identifier_when_no_name(self, mocker):
        user, fake_collection = await self._run_hook(
            mocker,
            identifier="dev@example.com",
            attributes={},
        )

        update_arg = fake_collection.update_one.call_args.args[1]
        set_on_insert = update_arg["$setOnInsert"]

        assert set_on_insert["canonical_name"] == "dev@example.com"
        assert set_on_insert["properties"] == {"is_active_user": True}

    async def test_canonical_name_prefers_attributes_name(self, mocker):
        user, fake_collection = await self._run_hook(
            mocker,
            identifier="dev@example.com",
            attributes={"name": "Dev User"},
        )

        update_arg = fake_collection.update_one.call_args.args[1]
        set_on_insert = update_arg["$setOnInsert"]

        assert set_on_insert["canonical_name"] == "Dev User"

    async def test_properties_merge_does_not_drop_caller_keys(self, mocker):
        user, fake_collection = await self._run_hook(
            mocker,
            identifier="dev@example.com",
            attributes={"name": "Dev User", "locale": "en-US", "prefs": {"x": 1}},
        )

        update_arg = fake_collection.update_one.call_args.args[1]
        properties = update_arg["$setOnInsert"]["properties"]

        assert properties["is_active_user"] is True
        assert properties["name"] == "Dev User"
        assert properties["locale"] == "en-US"
        assert properties["prefs"] == {"x": 1}

    async def test_properties_caller_cannot_override_is_active_user_flag(self, mocker):
        """Even if a user adventurously puts ``is_active_user=False`` in
        ``attributes``, the hook must keep the flag True. The flag is the
        single source of truth — protect it from accidental shadowing."""

        user, fake_collection = await self._run_hook(
            mocker,
            identifier="adv@example.com",
            attributes={"is_active_user": False, "name": "Adv"},
        )

        update_arg = fake_collection.update_one.call_args.args[1]
        properties = update_arg["$setOnInsert"]["properties"]

        assert properties["is_active_user"] is True

    async def test_two_users_produce_two_distinct_self_person_ids(self, mocker):
        user_a, coll_a = await self._run_hook(
            mocker, identifier="a@example.com", attributes={"name": "A"}
        )
        user_b, coll_b = await self._run_hook(
            mocker, identifier="b@example.com", attributes={"name": "B"}
        )

        filter_a = coll_a.update_one.call_args.args[0]
        filter_b = coll_b.update_one.call_args.args[0]

        assert filter_a["_id"] == f"{user_a.id}:person:self"
        assert filter_b["_id"] == f"{user_b.id}:person:self"
        assert filter_a["_id"] != filter_b["_id"]

    async def test_ensure_self_person_restores_a_missing_node(self, mocker) -> None:
        # An EXISTING user (no insert event) whose node is gone after a
        # rag -> graphrag switch: the same idempotent upsert re-creates it.
        user = User(identifier="paul@example.com")
        user.id = PydanticObjectId()
        fake_collection = AsyncMock()
        mocker.patch.object(
            MemoryEntry, "get_pymongo_collection", return_value=fake_collection
        )

        await user.ensure_self_person()

        fake_collection.update_one.assert_awaited_once()
        query, update = fake_collection.update_one.await_args.args
        assert query == {"_id": build_node_id(user.id, NodeType.PERSON, "self")}
        assert "$setOnInsert" in update
        assert fake_collection.update_one.await_args.kwargs["upsert"] is True

    async def test_rag_mode_writes_nothing(self, mocker, monkeypatch, caplog) -> None:
        monkeypatch.setattr(app_config.memory, "mode", "rag")

        with caplog.at_level(logging.INFO, logger="tree.entities.users"):
            user, fake_collection = await self._run_hook(
                mocker, identifier="rag@example.com", attributes={"name": "Rag"}
            )

        fake_collection.update_one.assert_not_awaited()
        assert [
            record.getMessage()
            for record in caplog.records
            if record.name == "tree.entities.users"
        ] == [
            f"Self-person node skipped for user_id={user.id}: memory.mode=rag "
            "writes document + chunk rows only (ADR-006)"
        ]


class TestUserExports:
    async def test_user_is_exported_from_entities_package(self):
        from tree.entities import User as ExportedUser

        assert ExportedUser is User

    async def test_user_is_registered_in_db_document_models(self):
        from tree.db import ALL_DOCUMENT_MODELS

        assert User in ALL_DOCUMENT_MODELS


class TestUserSettings:
    async def test_collection_name_is_users(self):
        assert User.Settings.name == "users"


class TestActiveUserIndexParity:
    """ADR-012: in graphrag the partial ``active_user`` index covers the fan-out."""

    async def test_query_predicate_equals_the_partial_filter(self, monkeypatch) -> None:
        monkeypatch.setattr(app_config.memory, "mode", "graphrag")
        collection = MagicMock()
        collection.find = MagicMock(return_value=_EmptyCursor())
        database = MagicMock()
        database.__getitem__ = MagicMock(return_value=collection)

        await select_active_user_ids(database=database)

        query = collection.find.call_args.args[0]
        partial = next(
            im.document["partialFilterExpression"]
            for im in memory_indexes("graphrag")
            if im.document["name"] == "active_user"
        )
        assert {k: query[k] for k in partial} == partial


class TestSelectActiveUserIdsRag:
    """In rag there is no ``person:self`` row: every ``users`` row is a tenant."""

    @pytest.fixture(autouse=True)
    def _rag_mode(self, monkeypatch) -> None:
        monkeypatch.setattr(app_config.memory, "mode", "rag")

    async def test_reads_every_users_row_sorted_and_never_memory(self) -> None:
        ids = [
            PydanticObjectId("507f1f77bcf86cd799439013"),
            PydanticObjectId("507f1f77bcf86cd799439011"),
            PydanticObjectId("507f1f77bcf86cd799439012"),
        ]
        database = _RecordingDatabase([{"_id": uid} for uid in ids])

        result = await select_active_user_ids(database=database)

        assert result == sorted(ids, key=str)
        assert database.opened == ["users"]
        assert database.collection.find.call_args.args == ({}, {"_id": 1})

    async def test_empty_users_collection_yields_no_tenant(self) -> None:
        database = _RecordingDatabase([])

        assert await select_active_user_ids(database=database) == []
        assert database.opened == ["users"]


class _RecordingDatabase:
    """A fake ``AsyncDatabase`` that records every collection name it opens."""

    def __init__(self, rows: list[dict]) -> None:
        self.opened: list[str] = []
        self.collection = MagicMock()
        self.collection.find = MagicMock(return_value=_ListCursor(rows))

    def __getitem__(self, name: str) -> MagicMock:
        self.opened.append(name)
        return self.collection


class _ListCursor:
    def __init__(self, rows: list[dict]) -> None:
        self._rows = iter(rows)

    def __aiter__(self) -> _ListCursor:
        return self

    async def __anext__(self) -> dict:
        try:
            return next(self._rows)
        except StopIteration:
            raise StopAsyncIteration from None


class _EmptyCursor:
    def __aiter__(self) -> _EmptyCursor:
        return self

    async def __anext__(self) -> None:
        raise StopAsyncIteration
