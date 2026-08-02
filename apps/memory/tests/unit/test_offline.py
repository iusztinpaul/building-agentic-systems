"""Unit tests for ``tree.offline`` — the offline end-to-end glue flow.

``offline_pipeline`` composes the two coordinators (data as inline subflow, then one
extraction coordinator per target user) without re-implementing either; these
tests pin that composition contract: pass-through of source selectors, the
per-user extraction fan-out, and per-user failure isolation. The coordinators
themselves are covered in their own suites.
"""

from dataclasses import dataclass, field
from unittest.mock import AsyncMock, MagicMock

from beanie import PydanticObjectId

from tree.offline import dispatch_offline_pipeline, offline_pipeline

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

        result = await offline_pipeline(
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

        result = await offline_pipeline(user_id=None)

        # user_id=None (the nightly-cron semantics) fans extraction out across
        # every active user the data phase just ingested for.
        users.assert_awaited_once_with(None)
        assert extract.await_count == 2
        assert set(result["extraction"]) == {str(_USER_ID), str(_OTHER_USER_ID)}

    async def test_one_users_extraction_failure_is_isolated(self, mocker) -> None:
        _, extract, users = _patch_coordinators(mocker)
        users.return_value = [_USER_ID, _OTHER_USER_ID]
        extract.side_effect = [RuntimeError("llm down"), _FakeStats()]

        result = await offline_pipeline(user_id=None)

        # Mirror of the shard-isolation convention: the first user's blown
        # extraction is recorded, the second still runs.
        assert extract.await_count == 2
        assert result["extraction"][str(_USER_ID)] == {"error": "llm down"}
        assert result["extraction"][str(_OTHER_USER_ID)]["succeeded"] == 1


class TestDispatchOfflineIngest:
    """The caller-edge dispatcher: deployment first, inline flow fallback."""

    async def test_submits_the_deployment_fire_and_forget(self, mocker) -> None:
        flow_run = MagicMock()
        flow_run.id = "run-1"
        mock_run = mocker.patch(
            "tree.offline.run_deployment",
            new_callable=AsyncMock,
            return_value=flow_run,
        )
        mock_flow = mocker.patch(
            "tree.offline.offline_pipeline", new_callable=AsyncMock
        )

        result = await dispatch_offline_pipeline(
            user_id=_USER_ID, source_files=["sources/listen.yaml"], num_shards=2
        )

        assert result == {
            "status": "submitted",
            "flow_run_id": "run-1",
            "mode": "deployment",
        }
        mock_run.assert_awaited_once()
        assert mock_run.await_args.args == ("offline-pipeline/offline-pipeline",)
        assert mock_run.await_args.kwargs["timeout"] == 0
        assert mock_run.await_args.kwargs["parameters"] == {
            "user_id": str(_USER_ID),
            "source_files": ["sources/listen.yaml"],
            "sources": None,
            "num_shards": 2,
        }
        mock_flow.assert_not_awaited()

    async def test_runs_the_same_flow_inline_when_deployment_unavailable(
        self, mocker
    ) -> None:
        mocker.patch(
            "tree.offline.run_deployment",
            new_callable=AsyncMock,
            side_effect=RuntimeError("deployment not found"),
        )
        mock_flow = mocker.patch(
            "tree.offline.offline_pipeline",
            new_callable=AsyncMock,
            return_value={"data": {"shards_total": 0}, "extraction": {}},
        )

        result = await dispatch_offline_pipeline(user_id=_USER_ID)

        # The fallback runs the SAME flow inline and hands back its result.
        assert result == {
            "status": "completed",
            "result": {"data": {"shards_total": 0}, "extraction": {}},
            "mode": "in_process",
        }
        mock_flow.assert_awaited_once_with(
            user_id=_USER_ID, source_files=None, sources=None, num_shards=1
        )
