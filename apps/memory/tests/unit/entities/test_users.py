"""Unit tests for the ``User`` Beanie entity and its self-person hook.

These tests cover model shape, default values, the ``canonical_name``
fallback rule, and the payload produced by ``after_insert`` (with the
underlying Mongo collection mocked via ``pytest-mock``). End-to-end
behavior against a real MongoDB lives in ``tests/integration/entities/``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

from beanie import PydanticObjectId

from tree.entities.knowledge_graph import (
    KnowledgeGraphEntry,
    NodeType,
    build_node_id,
)
from tree.entities.users import User


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
    :func:`tree.entities.knowledge_graph.build_node_id`. These tests
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
    """Verify the payload the hook upserts into ``knowledge_graph``.

    We bypass Beanie's real pymongo collection by patching
    ``KnowledgeGraphEntry.get_pymongo_collection`` to return an ``AsyncMock``
    and inspect the ``update_one`` call's arguments.
    """

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
            KnowledgeGraphEntry,
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
