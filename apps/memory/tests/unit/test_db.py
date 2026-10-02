"""``tree.db``: the mode's index set at boot, and the **Mode reset**.

``init_mongodb`` binds the configured mode's ``memory`` index set (ADR-012).
Runs the REAL ``init_mongodb`` against a throwaway database per mode, then
re-runs it against the session database: ``init_beanie`` rebinds the global
``MemoryEntry`` settings, so leaving a mode's binding behind would leak into
every later test.

``reset_memory_mode`` runs against the REAL local Mongo on its own throwaway
database through a RAW client — exactly how the script calls it (no Beanie).
"""

from collections.abc import AsyncIterator

import pytest
from bson import ObjectId
from pymongo import AsyncMongoClient
from pymongo.asynchronous.database import AsyncDatabase

from tests.unit.conftest import TEST_DATABASE
from tree.config.app_config import app_config
from tree.config.settings import settings
from tree.db import MODE_BOUND_COLLECTIONS, init_mongodb, reset_memory_mode
from tree.entities.memory import MEMORY_COLLECTION, MemoryEntry

_BASE = {"text_index", "user_kind_type_subtype", "user_type_name"}
_EXPECTED = {
    "rag": _BASE,
    "graphrag": _BASE
    | {"active_user", "user_kind_source_node", "user_kind_target_node"},
}


@pytest.mark.parametrize("mode", ["rag", "graphrag"])
async def test_init_mongodb_creates_exactly_the_modes_classic_indexes(
    monkeypatch, mode: str
) -> None:
    uri = settings.mongo.mongo_uri.get_secret_value()
    database = f"{TEST_DATABASE}_indexes_{mode}"
    monkeypatch.setattr(app_config.memory, "mode", mode)

    client = await init_mongodb(uri, database)
    try:
        declared = {im.document["name"] for im in MemoryEntry.Settings.indexes}
        live = await client[database][MEMORY_COLLECTION].index_information()

        assert declared == _EXPECTED[mode]
        assert set(live) == {"_id_"} | _EXPECTED[mode]
    finally:
        await client.drop_database(database)
        await client.close()
        monkeypatch.undo()
        restored = await init_mongodb(uri, TEST_DATABASE)
        # Beanie keeps the bound client; do not close the session's one.
        del restored


_RESET_DATABASE = f"{TEST_DATABASE}_mode_reset"


@pytest.fixture
async def reset_client() -> AsyncIterator[AsyncMongoClient]:
    """A raw client whose throwaway database is seeded like a small deployment.

    ``memory``: a document row, a parent and a child sourcing one Document;
    ``memory_clusters``: one cluster; ``documents``: one with content (pending
    after the reset) and one without; ``users``: one tenant.
    """

    client = AsyncMongoClient(settings.mongo.mongo_uri.get_secret_value())
    db = client[_RESET_DATABASE]
    await client.drop_database(_RESET_DATABASE)
    user_id = ObjectId()
    doc_id = ObjectId()
    await db["memory"].insert_many(
        [
            {"_id": f"{user_id}:document:a", "kind": "node", "sources": [doc_id]},
            {"_id": f"{user_id}:chunk:parent", "kind": "node", "sources": [doc_id]},
            {"_id": f"{user_id}:chunk:child", "kind": "node", "sources": [doc_id]},
        ]
    )
    await db["memory_clusters"].insert_one({"_id": f"{user_id}:cluster:0"})
    await db["documents"].insert_many(
        [
            {"_id": doc_id, "user_id": user_id, "content": "text"},
            {"_id": ObjectId(), "user_id": user_id, "content": None},
        ]
    )
    await db["users"].insert_one({"_id": user_id, "identifier": "paul"})
    try:
        yield client
    finally:
        await client.drop_database(_RESET_DATABASE)
        await client.close()


async def _snapshot(db: AsyncDatabase, name: str) -> list[dict]:
    return await db[name].find().sort("_id").to_list()


class TestResetMemoryMode:
    def test_the_mode_bound_collections_are_memory_and_its_clusters(self) -> None:
        assert MODE_BOUND_COLLECTIONS == ("memory", "memory_clusters")

    async def test_a_dry_run_counts_everything_and_drops_nothing(
        self, reset_client: AsyncMongoClient
    ) -> None:
        report = await reset_memory_mode(
            reset_client, _RESET_DATABASE, env_target="local", dry_run=True
        )

        assert report.dry_run is True
        assert report.dropped == {"memory": 3, "memory_clusters": 1}
        assert report.kept == {"documents": 2, "users": 1}
        assert report.pending_documents == 1
        names = await reset_client[_RESET_DATABASE].list_collection_names()
        assert set(names) == {"memory", "memory_clusters", "documents", "users"}

    async def test_a_confirmed_run_drops_exactly_the_mode_bound_collections(
        self, reset_client: AsyncMongoClient
    ) -> None:
        db = reset_client[_RESET_DATABASE]
        documents_before = await _snapshot(db, "documents")
        users_before = await _snapshot(db, "users")

        report = await reset_memory_mode(
            reset_client, _RESET_DATABASE, env_target="local", dry_run=False
        )

        # Counted BEFORE the drop: the numbers are the operator's evidence.
        assert report.dropped == {"memory": 3, "memory_clusters": 1}
        assert set(await db.list_collection_names()) == {"documents", "users"}
        assert await _snapshot(db, "documents") == documents_before
        assert await _snapshot(db, "users") == users_before

    async def test_a_second_confirmed_run_is_a_harmless_no_op(
        self, reset_client: AsyncMongoClient
    ) -> None:
        await reset_memory_mode(
            reset_client, _RESET_DATABASE, env_target="local", dry_run=False
        )

        report = await reset_memory_mode(
            reset_client, _RESET_DATABASE, env_target="local", dry_run=False
        )

        assert report.dropped == {"memory": 0, "memory_clusters": 0}
        assert report.kept == {"documents": 2, "users": 1}

    @pytest.mark.parametrize("mode", ["rag", "graphrag"])
    async def test_the_report_names_the_configured_mode(
        self, reset_client: AsyncMongoClient, monkeypatch, mode: str
    ) -> None:
        monkeypatch.setattr(app_config.memory, "mode", mode)

        report = await reset_memory_mode(
            reset_client, _RESET_DATABASE, env_target="prod", dry_run=True
        )

        assert report.configured_mode == mode
        assert report.env_target == "prod"
        assert report.database == _RESET_DATABASE
        assert report.target == settings.mongo.redacted_target()
