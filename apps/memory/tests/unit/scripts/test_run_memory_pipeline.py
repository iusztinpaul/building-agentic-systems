"""Unit tests for the ``scripts/run_memory_pipeline.py`` CLI wiring.

Operator surface only: the ``--mode`` offline/online validation split and
``--doc-ids`` parsing. The Prefect/Mongo work in ``_run`` is mocked (an
external boundary), so nothing touches a real server.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from click.testing import CliRunner


@pytest.fixture
def cli_main():
    """Import the CLI command lazily so module-load side effects stay scoped."""

    import scripts.run_memory_pipeline as cli_module

    return cli_module.main


@pytest.fixture
def mock_run(mocker):
    """Stub the Prefect/Mongo entrypoint so the CLI never hits a real server."""

    return mocker.patch("scripts.run_memory_pipeline._run", new_callable=AsyncMock)


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
