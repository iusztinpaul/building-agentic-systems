"""Unit tests for the dream watermark helpers (``…graph.consolidation.meta_state``).

The watermark is the dream sweep's only memory of "how far did I get". Two
claims decide whether the sweep is correct and are asserted here against a
mocked collection (the ops are pure payload building; what MongoDB does with an
upsert is not what is in question):

* a MISSING document means "never run" ⇒ :data:`EPOCH`, so the first run sweeps
  everything instead of silently sweeping nothing;
* the persisted ``last_run_at`` is the caller's ``run_start`` (captured BEFORE
  processing), never ``now()`` — the no-gap rule that makes nodes written
  mid-run get re-driven rather than skipped forever.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from beanie import PydanticObjectId

from tree.entities.meta_state import build_meta_state_id
from tree.memory.graph.consolidation.meta_state import (
    EPOCH,
    load_watermark,
    record_dream_run,
)

_USER_ID = PydanticObjectId("507f1f77bcf86cd799439011")
_RUN_START = datetime(2026, 9, 5, 4, 0, tzinfo=UTC)


def _database(find_one_result: dict[str, Any] | None = None) -> tuple[Any, Any]:
    """A mocked ``AsyncDatabase`` and the collection it hands out."""

    collection = MagicMock(name="collection")
    collection.find_one = AsyncMock(return_value=find_one_result)
    collection.update_one = AsyncMock()
    database = MagicMock(name="database")
    database.__getitem__.return_value = collection
    return database, collection


class TestLoadWatermark:
    async def test_a_missing_document_yields_the_epoch_watermark(self) -> None:
        database, _ = _database(None)

        watermark = await load_watermark(database=database, user_id=_USER_ID)

        assert watermark.last_run_at == EPOCH

    async def test_a_missing_document_is_not_persisted(self) -> None:
        """ "Never run" is an in-memory answer — only a real run writes.

        Persisting here would stamp a watermark for a sweep that never
        happened and skip every node ingested before it.
        """

        database, collection = _database(None)

        await load_watermark(database=database, user_id=_USER_ID)

        collection.update_one.assert_not_called()

    async def test_an_existing_document_is_returned_as_the_entity(self) -> None:
        stored = {
            "_id": build_meta_state_id(_USER_ID, "dream"),
            "user_id": _USER_ID,
            "job": "dream",
            "last_run_at": _RUN_START,
            "last_run_id": "run-1",
            "last_stats": {"merged": 3},
            "updated_at": _RUN_START,
        }
        database, _ = _database(stored)

        watermark = await load_watermark(database=database, user_id=_USER_ID)

        assert watermark.last_run_at == _RUN_START
        assert watermark.last_run_id == "run-1"
        assert watermark.last_stats == {"merged": 3}

    async def test_the_read_is_keyed_on_the_tenant_scoped_id(self) -> None:
        database, collection = _database(None)

        await load_watermark(database=database, user_id=_USER_ID, job="dream")

        collection.find_one.assert_awaited_once_with(
            {"_id": build_meta_state_id(_USER_ID, "dream")}
        )


class TestRecordDreamRun:
    async def test_persists_run_start_as_the_new_watermark(self) -> None:
        database, collection = _database()

        await record_dream_run(
            database=database,
            user_id=_USER_ID,
            run_start=_RUN_START,
            last_run_id="run-1",
            last_stats={"merged": 3},
        )

        payload = collection.update_one.await_args.args[1]["$set"]
        assert payload["last_run_at"] == _RUN_START

    async def test_updated_at_is_wall_clock_not_the_run_start(self) -> None:
        """The two timestamps answer different questions.

        ``last_run_at`` is the sweep boundary; ``updated_at`` tells an operator
        when the watermark itself was last touched.
        """

        database, collection = _database()

        await record_dream_run(
            database=database,
            user_id=_USER_ID,
            run_start=_RUN_START,
            last_run_id=None,
            last_stats={},
        )

        payload = collection.update_one.await_args.args[1]["$set"]
        assert payload["updated_at"] > _RUN_START

    async def test_writes_are_upserts_on_the_tenant_scoped_id(self) -> None:
        database, collection = _database()

        await record_dream_run(
            database=database,
            user_id=_USER_ID,
            run_start=_RUN_START,
            last_run_id=None,
            last_stats={},
        )

        call = collection.update_one.await_args
        assert call.args[0] == {"_id": build_meta_state_id(_USER_ID, "dream")}
        assert call.kwargs["upsert"] is True

    async def test_a_naive_run_start_is_rejected_before_the_write(self) -> None:
        """Project rule: no naive datetimes. Rejecting late would corrupt
        every later ``updated_at > last_run_at`` comparison."""

        database, collection = _database()

        with pytest.raises(ValueError, match="timezone-aware"):
            await record_dream_run(
                database=database,
                user_id=_USER_ID,
                run_start=datetime(2026, 9, 5, 4, 0),  # noqa: DTZ001 — the point
                last_run_id=None,
                last_stats={},
            )

        collection.update_one.assert_not_called()
