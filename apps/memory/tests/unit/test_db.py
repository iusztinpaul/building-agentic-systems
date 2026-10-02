"""``init_mongodb`` binds the configured mode's ``memory`` index set (ADR-012).

Runs the REAL ``init_mongodb`` against a throwaway database per mode, then
re-runs it against the session database: ``init_beanie`` rebinds the global
``MemoryEntry`` settings, so leaving a mode's binding behind would leak into
every later test.
"""

import pytest

from tests.unit.conftest import TEST_DATABASE
from tree.config.app_config import app_config
from tree.config.settings import settings
from tree.db import init_mongodb
from tree.entities.memory import MEMORY_COLLECTION, MemoryEntry

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
