"""Unit tests for ``tree.offline`` — the offline end-to-end glue flow.

``offline_pipeline`` composes the two coordinators (data as inline subflow, then one
extraction coordinator per target user) without re-implementing either; these
tests pin that composition contract: pass-through of source selectors, the
per-user extraction fan-out, and per-user failure isolation. The coordinators
themselves are covered in their own suites.

They also pin the #098 single-step surface — the ``run_data`` / ``run_extraction``
phase flags that let the step CLIs funnel through this ONE flow, the
``document_ids`` narrowing forwarded to every per-user extraction call, and the
single-tenant guard that fires at BOTH edges (flow and fire-and-forget
dispatcher).
"""

import logging
from dataclasses import dataclass, field
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from beanie import PydanticObjectId
from prefect.client.schemas.objects import State, StateType

from tree.offline import dispatch_offline_pipeline, offline_pipeline

_USER_ID = PydanticObjectId("507f1f77bcf86cd799439011")
_OTHER_USER_ID = PydanticObjectId("507f1f77bcf86cd799439012")
_DOC_ID = "68a1f1f77bcf86cd799439ab"


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


def _flow_run(
    run_id: str = "run-1", state_type: StateType = StateType.SCHEDULED
) -> SimpleNamespace:
    """A just-CREATED flow run, as ``run_deployment(timeout=0)`` returns it."""

    return SimpleNamespace(id=run_id, state=State(type=state_type))


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
        extract.assert_awaited_once_with(_USER_ID, document_ids=None, num_shards=2)
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


class TestOfflinePipelinePhaseFlags:
    """The #098 single-step surface: either phase can be turned off."""

    async def test_run_data_false_skips_ingestion_and_still_extracts(
        self, mocker
    ) -> None:
        data, extract, _ = _patch_coordinators(mocker)

        result = await offline_pipeline(user_id=_USER_ID, run_data=False)

        # The data phase is skipped entirely; extraction still runs, and the
        # result carries an explicit "no data phase" marker.
        data.assert_not_awaited()
        extract.assert_awaited_once_with(_USER_ID, document_ids=None, num_shards=1)
        assert result["data"] is None

    async def test_run_extraction_false_skips_user_resolution_and_extraction(
        self, mocker
    ) -> None:
        data, extract, users = _patch_coordinators(mocker)

        result = await offline_pipeline(user_id=_USER_ID, run_extraction=False)

        # Target-user resolution is part of the memory phase, so it is skipped
        # too — a data-only run must not touch the users collection.
        data.assert_awaited_once_with(user_id=_USER_ID, source_files=None, sources=None)
        users.assert_not_awaited()
        extract.assert_not_awaited()
        assert result["extraction"] == {}

    async def test_both_phases_disabled_is_a_logged_no_op(self, mocker, caplog) -> None:
        data, extract, users = _patch_coordinators(mocker)

        with caplog.at_level(logging.INFO, logger="tree.offline"):
            result = await offline_pipeline(
                user_id=_USER_ID, run_data=False, run_extraction=False
            )

        # A misconfigured caller gets a Completed run it can read, not a crash.
        assert result == {"data": None, "extraction": {}}
        data.assert_not_awaited()
        users.assert_not_awaited()
        extract.assert_not_awaited()
        no_op_logs = [
            record
            for record in caplog.records
            if "both phases disabled" in record.getMessage()
        ]
        assert len(no_op_logs) == 1
        assert no_op_logs[0].levelno == logging.INFO

    async def test_document_ids_are_forwarded_to_every_extraction_call(
        self, mocker
    ) -> None:
        _, extract, _ = _patch_coordinators(mocker)

        await offline_pipeline(
            user_id=_USER_ID, document_ids=[_DOC_ID], num_shards=2, run_data=False
        )

        # Narrowing is verbatim pass-through: the coordinator, not this flow,
        # decides what an explicit doc-id set means.
        extract.assert_awaited_once_with(_USER_ID, document_ids=[_DOC_ID], num_shards=2)

    async def test_document_ids_without_user_id_is_rejected(self, mocker) -> None:
        data, extract, _ = _patch_coordinators(mocker)

        with pytest.raises(ValueError, match="document_ids is single-tenant"):
            await offline_pipeline(user_id=None, document_ids=[_DOC_ID])

        # Guarded at the edge — no phase runs against the wrong tenant.
        data.assert_not_awaited()
        extract.assert_not_awaited()


class TestDispatchOfflineIngest:
    """The caller-edge dispatcher: ONE path — submit the core deployment.

    ``offline-pipeline`` is always registered, so dispatch simply requires a
    reachable Prefect API: no in-process fallback, and submission failures
    reach the caller instead of turning into a silent blocking local run.
    """

    async def test_submits_the_deployment_fire_and_forget(self, mocker) -> None:
        mock_run = mocker.patch(
            "tree.offline.run_deployment",
            new_callable=AsyncMock,
            return_value=_flow_run(),
        )
        mock_flow = mocker.patch(
            "tree.offline.offline_pipeline", new_callable=AsyncMock
        )

        result = await dispatch_offline_pipeline(
            user_id=_USER_ID, source_files=["sources/listen.yaml"], num_shards=2
        )

        assert result == {"status": "scheduled", "flow_run_id": "run-1"}
        mock_run.assert_awaited_once()
        assert mock_run.await_args.args == ("offline-pipeline/offline-pipeline",)
        assert mock_run.await_args.kwargs["timeout"] == 0
        assert mock_run.await_args.kwargs["parameters"] == {
            "user_id": str(_USER_ID),
            "source_files": ["sources/listen.yaml"],
            "sources": None,
            "num_shards": 2,
            "run_data": True,
            "run_extraction": True,
            "document_ids": None,
        }
        mock_flow.assert_not_awaited()

    @pytest.mark.parametrize(
        ("state_type", "expected"),
        [(StateType.SCHEDULED, "scheduled"), (StateType.PENDING, "pending")],
    )
    async def test_status_reports_the_flow_runs_own_state(
        self, mocker, state_type: StateType, expected: str
    ) -> None:
        mocker.patch(
            "tree.offline.run_deployment",
            new_callable=AsyncMock,
            return_value=_flow_run(state_type=state_type),
        )

        result = await dispatch_offline_pipeline(user_id=_USER_ID)

        # The status is Prefect's, not ours: a caller can look the run up
        # under that exact state name.
        assert result["status"] == expected

    async def test_submission_failure_propagates(self, mocker) -> None:
        mocker.patch(
            "tree.offline.run_deployment",
            new_callable=AsyncMock,
            side_effect=RuntimeError("Failed to reach API"),
        )

        # No fallback swallows it: an unreachable API, a missing deployment or
        # bad parameters must surface to the caller.
        with pytest.raises(RuntimeError, match="Failed to reach API"):
            await dispatch_offline_pipeline(user_id=_USER_ID)

    async def test_forwards_the_phase_flags_and_document_ids_to_the_deployment(
        self, mocker
    ) -> None:
        mock_run = mocker.patch(
            "tree.offline.run_deployment",
            new_callable=AsyncMock,
            return_value=_flow_run("run-2"),
        )

        await dispatch_offline_pipeline(
            user_id=_USER_ID,
            run_data=False,
            run_extraction=True,
            document_ids=[_DOC_ID],
        )

        # A narrowed single-step run must survive the trip through the
        # deployment parameters, not silently fall back to the defaults.
        parameters = mock_run.await_args.kwargs["parameters"]
        assert parameters["run_data"] is False
        assert parameters["run_extraction"] is True
        assert parameters["document_ids"] == [_DOC_ID]

    async def test_document_ids_without_user_id_never_creates_a_flow_run(
        self, mocker
    ) -> None:
        mock_run = mocker.patch("tree.offline.run_deployment", new_callable=AsyncMock)

        with pytest.raises(ValueError, match="document_ids is single-tenant"):
            await dispatch_offline_pipeline(user_id=None, document_ids=[_DOC_ID])

        # Dispatch is fire-and-forget, so edge validation is the ONLY way the
        # caller sees this synchronously — it must precede run_deployment.
        mock_run.assert_not_awaited()
