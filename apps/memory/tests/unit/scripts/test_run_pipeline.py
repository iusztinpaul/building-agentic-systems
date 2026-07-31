"""Unit tests for the ``scripts/run_pipeline.py`` CLI wiring.

Operator surface only: the ``--mode`` offline/online split and the forwarding
into the two end-to-end dispatch entrypoints. The Prefect/Mongo work in
``_run_offline`` / ``_run_online`` is mocked (an external boundary); the
dispatchers themselves are covered by ``test_offline.py`` / ``test_online.py``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from click.testing import CliRunner


@pytest.fixture
def cli_main():
    """Import the CLI command lazily so module-load side effects stay scoped."""

    import scripts.run_pipeline as cli_module

    return cli_module.main


@pytest.fixture
def mock_run_offline(mocker):
    """Stub the offline dispatch entrypoint — never hit a real server."""

    return mocker.patch("scripts.run_pipeline._run_offline", new_callable=AsyncMock)


@pytest.fixture
def mock_run_online(mocker):
    """Stub the online dispatch entrypoint — never hit a real server."""

    return mocker.patch("scripts.run_pipeline._run_online", new_callable=AsyncMock)


class TestRunPipelineCliOptions:
    def test_online_without_source_is_a_usage_error(
        self, mock_run_online, cli_main
    ) -> None:
        # Arrange
        runner = CliRunner()

        # Act
        result = runner.invoke(cli_main, ["--mode", "online"])

        # Assert — hard CLI error BEFORE any flow is dispatched.
        assert result.exit_code != 0
        assert "--source" in result.output
        mock_run_online.assert_not_awaited()

    def test_offline_rejects_online_source(self, mock_run_offline, cli_main) -> None:
        # Arrange — the default mode is offline; --source belongs to online.
        runner = CliRunner()

        # Act
        result = runner.invoke(cli_main, ["--source", "https://x.com/a"])

        # Assert
        assert result.exit_code != 0
        assert "online-only" in result.output
        mock_run_offline.assert_not_awaited()


class TestRunPipelineForwarding:
    def test_offline_forwards_selectors_and_num_shards(
        self, mock_run_offline, cli_main
    ) -> None:
        # Arrange
        runner = CliRunner()

        # Act
        result = runner.invoke(
            cli_main,
            [
                "--source-file",
                "sources/listen.yaml",
                "--uri",
                "https://x.com/a",
                "--num-shards",
                "2",
            ],
        )

        # Assert
        assert result.exit_code == 0, result.output
        mock_run_offline.assert_awaited_once()
        args = mock_run_offline.await_args.args
        assert args[2] == ["sources/listen.yaml"]
        assert args[3] == [{"uri": "https://x.com/a", "type": "web"}]
        assert args[4] == 2

    def test_online_forwards_source_and_title(self, mock_run_online, cli_main) -> None:
        # Arrange
        runner = CliRunner()

        # Act
        result = runner.invoke(
            cli_main,
            ["--mode", "online", "--source", "/tmp/notes.md", "--title", "Notes"],
        )

        # Assert
        assert result.exit_code == 0, result.output
        mock_run_online.assert_awaited_once()
        args = mock_run_online.await_args.args
        assert args[2] == "/tmp/notes.md"
        assert args[3] == "Notes"
