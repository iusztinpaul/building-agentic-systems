"""``tree.db``: the mode's index set at boot, and the **Mode reset**.

``init_mongodb`` binds the configured mode's ``memory`` index set (ADR-012).
Runs the REAL ``init_mongodb`` against a throwaway database per mode, then
re-runs it against the session database: ``init_beanie`` rebinds the global
``MemoryEntry`` settings, so leaving a mode's binding behind would leak into
every later test.

``reset_memory_mode`` runs against the REAL local Mongo on its own throwaway
database through a RAW client — exactly how the script calls it (no Beanie).

``reconcile_graph_file_ttl`` (ADR-014 §4) runs against a fake database for the
branch logic, and once against the REAL local Mongo to prove a changed
``mcp.graph_file_ttl_seconds`` no longer crashes ``init_mongodb``.
"""

import logging
from collections.abc import AsyncIterator, Iterator
from typing import Any

import pytest
from bson import ObjectId
from pymongo import AsyncMongoClient, IndexModel
from pymongo.asynchronous.database import AsyncDatabase
from pymongo.errors import OperationFailure, PyMongoError

from tests.unit.conftest import TEST_DATABASE
from tree.config.app_config import app_config
from tree.config.settings import settings
from tree.db import (
    ALL_DOCUMENT_MODELS,
    MODE_BOUND_COLLECTIONS,
    init_mongodb,
    reconcile_graph_file_ttl,
    reset_memory_mode,
)
from tree.entities.graph_files import (
    GRAPH_FILE_TTL_INDEX,
    GRAPH_FILES_COLLECTION,
    GraphFile,
)
from tree.entities.memory import MEMORY_COLLECTION, MemoryEntry

# No classic ``text_index`` since ADR-015 — the lexical leg is mongot's.
_BASE = {"user_kind_type_subtype", "user_type_name"}
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


# ---------------------------------------------------------------------------
# reconcile_graph_file_ttl (ADR-014 §4)
# ---------------------------------------------------------------------------


def _ttl_info(expire_after_seconds: int) -> dict[str, dict[str, Any]]:
    """``index_information()`` of a ``graph_files`` carrying the TTL index."""

    return {
        "_id_": {"key": [("_id", 1)], "v": 2},
        GRAPH_FILE_TTL_INDEX: {
            "key": [("created_at", 1)],
            "v": 2,
            "expireAfterSeconds": expire_after_seconds,
        },
    }


class _FakeCollection:
    def __init__(
        self, info: dict[str, dict[str, Any]], error: Exception | None
    ) -> None:
        self._info = info
        self._error = error

    async def index_information(self) -> dict[str, dict[str, Any]]:
        if self._error is not None:
            raise self._error
        return self._info


class _FakeDatabase:
    """Just the two calls the reconcile makes: ``index_information`` + ``command``."""

    def __init__(
        self,
        info: dict[str, dict[str, Any]] | None = None,
        *,
        index_error: Exception | None = None,
        command_error: Exception | None = None,
    ) -> None:
        self._collection = _FakeCollection(info or {}, index_error)
        self._command_error = command_error
        self.collections: list[str] = []
        self.commands: list[dict[str, Any]] = []

    def __getitem__(self, name: str) -> _FakeCollection:
        self.collections.append(name)
        return self._collection

    async def command(self, document: dict[str, Any]) -> dict[str, Any]:
        self.commands.append(document)
        if self._command_error is not None:
            raise self._command_error
        return {"ok": 1.0}


def _collmod(ttl_seconds: int) -> dict[str, Any]:
    return {
        "collMod": GRAPH_FILES_COLLECTION,
        "index": {"name": GRAPH_FILE_TTL_INDEX, "expireAfterSeconds": ttl_seconds},
    }


class TestReconcileGraphFileTtl:
    async def test_a_different_live_ttl_is_collmodded_once(self, caplog) -> None:
        db = _FakeDatabase(_ttl_info(300))

        with caplog.at_level(logging.INFO, logger="tree.db"):
            effective = await reconcile_graph_file_ttl(db, 120)  # type: ignore[arg-type]

        assert db.collections == [GRAPH_FILES_COLLECTION]
        assert db.commands == [_collmod(120)]
        assert effective == 120
        assert "expireAfterSeconds 300 → 120" in caplog.text

    async def test_an_equal_live_ttl_runs_no_command(self) -> None:
        db = _FakeDatabase(_ttl_info(300))

        effective = await reconcile_graph_file_ttl(db, 300)  # type: ignore[arg-type]

        assert db.commands == []
        assert effective == 300

    async def test_a_collection_without_the_ttl_index_runs_no_command(self) -> None:
        db = _FakeDatabase({"_id_": {"key": [("_id", 1)], "v": 2}})

        effective = await reconcile_graph_file_ttl(db, 120)  # type: ignore[arg-type]

        assert db.commands == []
        assert effective == 120

    async def test_a_missing_collection_runs_no_command(self) -> None:
        # pymongo answers ``{}`` for a collection that does not exist yet.
        db = _FakeDatabase({})

        effective = await reconcile_graph_file_ttl(db, 120)  # type: ignore[arg-type]

        assert db.commands == []
        assert effective == 120

    async def test_an_index_information_error_warns_once_and_does_not_raise(
        self, caplog
    ) -> None:
        db = _FakeDatabase(index_error=PyMongoError("connection refused"))

        with caplog.at_level(logging.WARNING, logger="tree.db"):
            effective = await reconcile_graph_file_ttl(db, 120)  # type: ignore[arg-type]

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert [r.getMessage() for r in warnings] == [
            "graph_files TTL reconcile skipped: connection refused"
        ]
        assert db.commands == []
        assert effective == 120

    async def test_a_rejected_collmod_warns_once_and_keeps_the_live_ttl(
        self, caplog
    ) -> None:
        db = _FakeDatabase(
            _ttl_info(300),
            command_error=OperationFailure("expireAfterSeconds out of range"),
        )

        with caplog.at_level(logging.WARNING, logger="tree.db"):
            effective = await reconcile_graph_file_ttl(db, -5)  # type: ignore[arg-type]

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert warnings[0].getMessage().startswith("graph_files TTL reconcile skipped:")
        # The old TTL stays in force, so Beanie must declare it, not the bad one.
        assert effective == 300


@pytest.fixture
def restore_graph_file_indexes() -> Iterator[None]:
    """Put back ``GraphFile.Settings.indexes`` after a test rebinds it."""

    declared: list[IndexModel] = list(GraphFile.Settings.indexes)
    try:
        yield
    finally:
        GraphFile.Settings.indexes = declared


def _declared_ttl() -> int:
    by_name = {im.document["name"]: im.document for im in GraphFile.Settings.indexes}
    return by_name[GRAPH_FILE_TTL_INDEX]["expireAfterSeconds"]


class TestInitMongodbGraphFiles:
    def test_graph_file_is_registered(self) -> None:
        assert GraphFile in ALL_DOCUMENT_MODELS

    async def test_reconcile_runs_before_init_beanie_with_the_configured_ttl(
        self, mocker, restore_graph_file_indexes
    ) -> None:
        calls: list[str] = []
        reconcile = mocker.patch(
            "tree.db.reconcile_graph_file_ttl",
            side_effect=lambda _db, ttl: calls.append("reconcile") or ttl,
        )
        beanie = mocker.patch(
            "tree.db.init_beanie",
            side_effect=lambda **_: calls.append("init_beanie"),
        )

        client = await init_mongodb("mongodb://localhost:1", "order_db")
        await client.close()

        assert calls == ["reconcile", "init_beanie"]
        reconcile.assert_awaited_once()
        assert reconcile.await_args.args[1] == app_config.mcp.graph_file_ttl_seconds
        assert reconcile.await_args.args[0].name == "order_db"
        assert GraphFile in beanie.await_args.kwargs["document_models"]

    async def test_a_failed_reconcile_makes_beanie_declare_the_live_ttl(
        self, mocker, monkeypatch, restore_graph_file_indexes
    ) -> None:
        monkeypatch.setattr(app_config.mcp, "graph_file_ttl_seconds", -5)
        mocker.patch("tree.db.reconcile_graph_file_ttl", return_value=300)
        mocker.patch("tree.db.init_beanie")

        client = await init_mongodb("mongodb://localhost:1", "fallback_db")
        await client.close()

        assert _declared_ttl() == 300


_GRAPH_FILES_DATABASE = f"{TEST_DATABASE}_graph_files"


async def test_init_mongodb_heals_a_changed_graph_file_ttl_against_live_mongo(
    monkeypatch, restore_graph_file_indexes
) -> None:
    """The regression ADR-014 §4 exists for: without the reconcile, a second
    boot with a different TTL dies in ``init_beanie`` on IndexOptionsConflict."""

    uri = settings.mongo.mongo_uri.get_secret_value()
    raw = AsyncMongoClient(uri)
    await raw.drop_database(_GRAPH_FILES_DATABASE)
    await raw[_GRAPH_FILES_DATABASE][GRAPH_FILES_COLLECTION].create_index(
        [("created_at", 1)], expireAfterSeconds=300, name=GRAPH_FILE_TTL_INDEX
    )
    monkeypatch.setattr(app_config.mcp, "graph_file_ttl_seconds", 120)

    try:
        client = await init_mongodb(uri, _GRAPH_FILES_DATABASE)
        live = await client[_GRAPH_FILES_DATABASE][
            GRAPH_FILES_COLLECTION
        ].index_information()
        await client.close()

        assert set(live) == {"_id_", GRAPH_FILE_TTL_INDEX, "name_user_id_unique"}
        assert live[GRAPH_FILE_TTL_INDEX]["expireAfterSeconds"] == 120
        assert live["name_user_id_unique"]["unique"] is True
    finally:
        await raw.drop_database(_GRAPH_FILES_DATABASE)
        await raw.close()
        monkeypatch.undo()
        restored = await init_mongodb(uri, TEST_DATABASE)
        # Beanie keeps the bound client; do not close the session's one.
        del restored
