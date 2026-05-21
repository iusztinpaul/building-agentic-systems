"""Integration tests for the #050 watermark helpers.

Exercise the real Beanie/Motor write path against a local MongoDB (the
same instance used by ``make local-start``). No ``$vectorSearch`` here, so
these are CI-safe (not marked ``requires_mongot``).

Covers the AC contract:

* missing doc ⇒ epoch ``last_run_at``;
* persisted ``last_run_at`` round-trips;
* ``record_dream_run`` writes ``run_start`` (the captured start time), NOT
  the call time;
* idempotent re-upsert leaves a single doc;
* two tenants keep independent watermarks.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from beanie import PydanticObjectId

from tree.entities.meta_state import KnowledgeGraphMetaState, build_meta_state_id
from tree.memory.consolidation.meta_state import (
    EPOCH,
    load_watermark,
    record_dream_run,
)

TEST_DATABASE = "integration_tests_twin"

# MongoDB stores datetimes at millisecond resolution, so a value written
# with microsecond precision reads back truncated. Compare round-tripped
# instants within a millisecond rather than for exact equality.
_MONGO_DT_TOLERANCE = timedelta(milliseconds=1)


def _assert_dt_close(actual: datetime, expected: datetime) -> None:
    assert abs(actual - expected) < _MONGO_DT_TOLERANCE, (
        f"{actual!r} not within {_MONGO_DT_TOLERANCE} of {expected!r}"
    )


@pytest.fixture()
def database(mongo_client):
    return mongo_client[TEST_DATABASE]


class TestLoadWatermark:
    async def test_missing_doc_returns_epoch(self, database) -> None:
        user_id = PydanticObjectId()

        watermark = await load_watermark(database=database, user_id=user_id)

        assert watermark.last_run_at == EPOCH
        assert watermark.last_run_at == datetime(1970, 1, 1, tzinfo=UTC)
        assert watermark.last_run_at.tzinfo is not None

    async def test_missing_doc_does_not_persist(self, database) -> None:
        user_id = PydanticObjectId()

        await load_watermark(database=database, user_id=user_id)

        # Loading a missing watermark must not write a row.
        collection = database["knowledge_graph_meta_state"]
        count = await collection.count_documents(
            {"_id": build_meta_state_id(user_id, "dream")}
        )
        assert count == 0

    async def test_returns_persisted_last_run_at(self, database) -> None:
        user_id = PydanticObjectId()
        run_start = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)

        await record_dream_run(
            database=database,
            user_id=user_id,
            run_start=run_start,
            last_run_id="flow-1",
            last_stats={"pairs_examined": 3},
        )

        watermark = await load_watermark(database=database, user_id=user_id)
        assert watermark.last_run_at == run_start
        assert watermark.last_run_id == "flow-1"
        assert watermark.last_stats == {"pairs_examined": 3}
        assert watermark.last_run_at.tzinfo is not None


class TestRecordDreamRun:
    async def test_writes_run_start_not_call_time(self, database) -> None:
        user_id = PydanticObjectId()
        # A run that started well in the past — if the helper wrote
        # now() instead of run_start this assertion would fail.
        run_start = datetime.now(UTC) - timedelta(days=7)

        await record_dream_run(
            database=database,
            user_id=user_id,
            run_start=run_start,
            last_run_id="flow-past",
            last_stats={},
        )

        watermark = await load_watermark(database=database, user_id=user_id)
        _assert_dt_close(watermark.last_run_at, run_start)

    async def test_updated_at_is_recent(self, database) -> None:
        user_id = PydanticObjectId()
        run_start = datetime.now(UTC) - timedelta(days=7)
        before = datetime.now(UTC) - _MONGO_DT_TOLERANCE

        await record_dream_run(
            database=database,
            user_id=user_id,
            run_start=run_start,
            last_run_id=None,
            last_stats={},
        )

        doc = await KnowledgeGraphMetaState.find_one(
            {"_id": build_meta_state_id(user_id, "dream")}
        )
        assert doc is not None
        # updated_at tracks wall-clock now, NOT run_start.
        assert doc.updated_at >= before
        # ... and is days away from run_start.
        assert abs(doc.updated_at - run_start) > timedelta(days=6)

    async def test_idempotent_reupsert_single_doc(self, database) -> None:
        user_id = PydanticObjectId()
        run_start = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)

        await record_dream_run(
            database=database,
            user_id=user_id,
            run_start=run_start,
            last_run_id="flow-1",
            last_stats={"n": 1},
        )
        await record_dream_run(
            database=database,
            user_id=user_id,
            run_start=run_start,
            last_run_id="flow-1",
            last_stats={"n": 1},
        )

        collection = database["knowledge_graph_meta_state"]
        count = await collection.count_documents(
            {"_id": build_meta_state_id(user_id, "dream")}
        )
        assert count == 1

        watermark = await load_watermark(database=database, user_id=user_id)
        assert watermark.last_run_at == run_start

    async def test_second_run_advances_watermark(self, database) -> None:
        user_id = PydanticObjectId()
        first = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)
        second = datetime(2026, 5, 2, 12, 0, tzinfo=UTC)

        await record_dream_run(
            database=database,
            user_id=user_id,
            run_start=first,
            last_run_id="flow-1",
            last_stats={},
        )
        await record_dream_run(
            database=database,
            user_id=user_id,
            run_start=second,
            last_run_id="flow-2",
            last_stats={},
        )

        watermark = await load_watermark(database=database, user_id=user_id)
        assert watermark.last_run_at == second
        assert watermark.last_run_id == "flow-2"

    async def test_naive_run_start_rejected(self, database) -> None:
        user_id = PydanticObjectId()
        with pytest.raises(ValueError, match="timezone-aware"):
            await record_dream_run(
                database=database,
                user_id=user_id,
                run_start=datetime(2026, 5, 1, 12, 0),  # naive
                last_run_id=None,
                last_stats={},
            )

    async def test_custom_job_is_isolated_from_dream(self, database) -> None:
        user_id = PydanticObjectId()
        dream_start = datetime(2026, 5, 1, tzinfo=UTC)
        other_start = datetime(2026, 5, 5, tzinfo=UTC)

        await record_dream_run(
            database=database,
            user_id=user_id,
            run_start=dream_start,
            last_run_id="dream",
            last_stats={},
        )
        await record_dream_run(
            database=database,
            user_id=user_id,
            job="compaction",
            run_start=other_start,
            last_run_id="compaction",
            last_stats={},
        )

        dream_wm = await load_watermark(database=database, user_id=user_id)
        other_wm = await load_watermark(
            database=database, user_id=user_id, job="compaction"
        )
        assert dream_wm.last_run_at == dream_start
        assert other_wm.last_run_at == other_start


class TestTenantIsolation:
    async def test_two_users_keep_independent_watermarks(self, database) -> None:
        user_a = PydanticObjectId()
        user_b = PydanticObjectId()
        start_a = datetime(2026, 5, 1, tzinfo=UTC)

        # Only A has run.
        await record_dream_run(
            database=database,
            user_id=user_a,
            run_start=start_a,
            last_run_id="flow-a",
            last_stats={"user": "a"},
        )

        wm_a = await load_watermark(database=database, user_id=user_a)
        wm_b = await load_watermark(database=database, user_id=user_b)

        # A reads its own start; B is untouched (epoch).
        assert wm_a.last_run_at == start_a
        assert wm_a.last_stats == {"user": "a"}
        assert wm_b.last_run_at == EPOCH
        assert wm_b.last_stats == {}

    async def test_advancing_one_tenant_does_not_touch_other(self, database) -> None:
        user_a = PydanticObjectId()
        user_b = PydanticObjectId()
        start_a = datetime(2026, 5, 1, tzinfo=UTC)
        start_b = datetime(2026, 5, 2, tzinfo=UTC)

        await record_dream_run(
            database=database,
            user_id=user_a,
            run_start=start_a,
            last_run_id="flow-a",
            last_stats={},
        )
        await record_dream_run(
            database=database,
            user_id=user_b,
            run_start=start_b,
            last_run_id="flow-b",
            last_stats={},
        )
        # Re-advance A; B must be unaffected.
        new_start_a = datetime(2026, 5, 3, tzinfo=UTC)
        await record_dream_run(
            database=database,
            user_id=user_a,
            run_start=new_start_a,
            last_run_id="flow-a2",
            last_stats={},
        )

        wm_a = await load_watermark(database=database, user_id=user_a)
        wm_b = await load_watermark(database=database, user_id=user_b)
        assert wm_a.last_run_at == new_start_a
        assert wm_b.last_run_at == start_b
        assert wm_b.last_run_id == "flow-b"
