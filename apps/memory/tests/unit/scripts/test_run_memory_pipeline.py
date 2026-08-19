"""Unit tests for the ``scripts/run_memory_pipeline.py`` CLI wiring.

Operator surface only: the ``--mode`` offline/online validation split and
``--doc-ids`` parsing. The Prefect/Mongo work in ``_run`` is mocked (an
external boundary), so nothing touches a real server.

``TestRunMemoryPipelineDispatch`` goes one level deeper (#099): it exercises
``_run`` itself with the dispatcher mocked, asserting the extraction step is a
``dispatch_offline_pipeline`` call with ``run_data=False``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from beanie import PydanticObjectId
from click.testing import CliRunner


@pytest.fixture
def cli_module():
    """Import the script lazily so module-load side effects stay scoped."""

    import scripts.run_memory_pipeline as module

    return module


@pytest.fixture
def cli_main(cli_module):
    """The Click command under test."""

    return cli_module.main


@pytest.fixture
def mock_run(mocker):
    """Stub the Prefect/Mongo entrypoint so the CLI never hits a real server."""

    return mocker.patch("scripts.run_memory_pipeline._run", new_callable=AsyncMock)


@pytest.fixture
def resolved_user_id():
    """The tenant ``connect_and_resolve_user`` hands back."""

    return PydanticObjectId()


@pytest.fixture
def mock_resolve_user(mocker, resolved_user_id):
    """Stub Mongo init + tenant resolution (an external boundary)."""

    return mocker.patch(
        "scripts.run_memory_pipeline.connect_and_resolve_user",
        new_callable=AsyncMock,
        return_value=resolved_user_id,
    )


@pytest.fixture
def mock_dispatch_offline(mocker):
    """Stub the offline dispatcher — covered on its own in ``test_offline.py``."""

    return mocker.patch(
        "scripts.run_memory_pipeline.dispatch_offline_pipeline",
        new_callable=AsyncMock,
        return_value={"status": "scheduled", "flow_run_id": "run-1"},
    )


@pytest.fixture
def mock_wait_for_dispatch(mocker):
    """Stub the blocking log-stream wait (Prefect client plumbing)."""

    return mocker.patch(
        "scripts.run_memory_pipeline.wait_for_dispatch", new_callable=AsyncMock
    )


@pytest.fixture
def mock_flush_opik(mocker):
    """Stub the Opik telemetry flush (a third-party SDK boundary)."""

    return mocker.patch("scripts.run_memory_pipeline.flush_opik")


class TestRunMemoryPipelineCliOptions:
    def test_online_without_doc_ids_is_a_usage_error(self, mock_run, cli_main) -> None:
        # Arrange
        runner = CliRunner()

        # Act
        result = runner.invoke(cli_main, ["--mode", "online"])

        # Assert — hard CLI error BEFORE any flow is dispatched.
        assert result.exit_code != 0
        assert "--doc-ids" in result.output
        mock_run.assert_not_awaited()

    def test_online_rejects_num_shards(self, mock_run, cli_main) -> None:
        # Arrange
        runner = CliRunner()

        # Act
        result = runner.invoke(
            cli_main, ["--mode", "online", "--doc-ids", "68a1", "--num-shards", "2"]
        )

        # Assert
        assert result.exit_code != 0
        assert "offline-only" in result.output
        mock_run.assert_not_awaited()

    def test_num_shards_below_one_is_a_usage_error(self, mock_run, cli_main) -> None:
        # Arrange
        runner = CliRunner()

        # Act
        result = runner.invoke(cli_main, ["--num-shards", "0"])

        # Assert
        assert result.exit_code != 0
        assert ">= 1" in result.output
        mock_run.assert_not_awaited()


class TestRunMemoryPipelineForwarding:
    def test_offline_default_forwards_no_doc_ids(self, mock_run, cli_main) -> None:
        # Arrange
        runner = CliRunner()

        # Act
        result = runner.invoke(cli_main, [])

        # Assert — batch mode: the coordinator resolves every pending doc.
        assert result.exit_code == 0, result.output
        mock_run.assert_awaited_once()
        assert mock_run.await_args.args[2] is None  # document_ids
        assert mock_run.await_args.args[3] is None  # num_shards

    def test_online_parses_comma_separated_doc_ids(self, mock_run, cli_main) -> None:
        # Arrange
        runner = CliRunner()

        # Act
        result = runner.invoke(
            cli_main, ["--mode", "online", "--doc-ids", "68a1, 68b2,"]
        )

        # Assert — split on commas, whitespace stripped, empties dropped.
        assert result.exit_code == 0, result.output
        assert mock_run.await_args.args[2] == ["68a1", "68b2"]


class TestRunMemoryPipelineDispatch:
    """``_run`` funnels through ``dispatch_offline_pipeline`` (#099)."""

    async def test_dispatches_offline_pipeline_with_data_off(
        self,
        cli_module,
        mock_resolve_user,
        mock_dispatch_offline,
        mock_wait_for_dispatch,
        mock_flush_opik,
        resolved_user_id,
    ) -> None:
        # Arrange — a narrowed doc set with an explicit fan-out width.
        document_ids = ["68a1", "68b2"]

        # Act
        await cli_module._run(None, None, document_ids, 4)

        # Assert — the extraction step is the offline flow with data disabled.
        mock_dispatch_offline.assert_awaited_once_with(
            user_id=resolved_user_id,
            document_ids=document_ids,
            num_shards=4,
            run_data=False,
        )
        mock_wait_for_dispatch.assert_awaited_once_with(
            mock_dispatch_offline.return_value
        )

    async def test_omitted_num_shards_defaults_to_one(
        self,
        cli_module,
        mock_resolve_user,
        mock_dispatch_offline,
        mock_wait_for_dispatch,
        mock_flush_opik,
    ) -> None:
        # Arrange — the batch default: no doc narrowing, no fan-out knob.

        # Act
        await cli_module._run(None, None, None, None)

        # Assert — one worker run, every PENDING document for the tenant.
        kwargs = mock_dispatch_offline.await_args.kwargs
        assert kwargs["num_shards"] == 1
        assert kwargs["document_ids"] is None
