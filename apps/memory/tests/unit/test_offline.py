"""Unit tests for ``tree.offline`` — the offline end-to-end glue flow.

``etl_offline`` composes the two coordinators (data as inline subflow, then one
extraction coordinator per target user) without re-implementing either; these
tests pin that composition contract: pass-through of source selectors, the
per-user extraction fan-out, and per-user failure isolation. The coordinators
themselves are covered in their own suites.
"""

from dataclasses import dataclass, field
from unittest.mock import AsyncMock

from beanie import PydanticObjectId

from tree.offline import etl_offline

_USER_ID = PydanticObjectId("507f1f77bcf86cd799439011")
_OTHER_USER_ID = PydanticObjectId("507f1f77bcf86cd799439012")


@dataclass
class _FakeStats:
    shards_total: int = 1
    succeeded: int = 1
    failed: int = 0
    failures: dict = field(default_factory=dict)


def _patch_coordinators(mocker) -> tuple[AsyncMock, AsyncMock, AsyncMock]:
    data = mocker.patch(
        "tree.offline.data_etl_coordinator",
        new_callable=AsyncMock,
        return_value=_FakeStats(),
    )
    extract = mocker.patch(
        "tree.offline.memory_extract_etl_coordinator",
        new_callable=AsyncMock,
        return_value=_FakeStats(),
    )
    users = mocker.patch(
        "tree.offline.resolve_target_user_ids",
        new_callable=AsyncMock,
        return_value=[_USER_ID],
    )
    return data, extract, users


class TestEtlOffline:
    async def test_passes_selectors_through_and_chains_extraction(self, mocker) -> None:
        data, extract, _ = _patch_coordinators(mocker)

        result = await etl_offline(
            user_id=_USER_ID,
            source_files=["sources/listen.yaml"],
            num_shards=2,
        )

        # Data phase: selectors forwarded untouched — the data coordinator owns
        # source resolution, so end-to-end source semantics stay identical.
        data.assert_awaited_once_with(
            user_id=_USER_ID, source_files=["sources/listen.yaml"], sources=None
        )
        # Memory phase: one extraction coordinator per target user, with the
        # extraction fan-out width forwarded.
        extract.assert_awaited_once_with(_USER_ID, num_shards=2)
        assert result["data"]["shards_total"] == 1
        assert result["extraction"][str(_USER_ID)]["succeeded"] == 1

    async def test_all_users_mode_extracts_per_active_user(self, mocker) -> None:
        _, extract, users = _patch_coordinators(mocker)
        users.return_value = [_USER_ID, _OTHER_USER_ID]

        result = await etl_offline(user_id=None)

        # user_id=None (the nightly-cron semantics) fans extraction out across
        # every active user the data phase just ingested for.
        users.assert_awaited_once_with(None)
        assert extract.await_count == 2
        assert set(result["extraction"]) == {str(_USER_ID), str(_OTHER_USER_ID)}

    async def test_one_users_extraction_failure_is_isolated(self, mocker) -> None:
        _, extract, users = _patch_coordinators(mocker)
        users.return_value = [_USER_ID, _OTHER_USER_ID]
        extract.side_effect = [RuntimeError("llm down"), _FakeStats()]

        result = await etl_offline(user_id=None)

        # Mirror of the shard-isolation convention: the first user's blown
        # extraction is recorded, the second still runs.
        assert extract.await_count == 2
        assert result["extraction"][str(_USER_ID)] == {"error": "llm down"}
        assert result["extraction"][str(_OTHER_USER_ID)]["succeeded"] == 1
