"""Unit tests for the ``scripts/run_data_pipeline.py`` CLI wiring (#086, ADR-003).

Exercise the operator surface only — Click flag parsing, the ``--mode``
offline/online split, and the glue that turns ``--uri`` tokens into inline
``sources``. The Prefect/Mongo work in ``_run_offline`` / ``_run_online`` is
mocked (an external boundary), so nothing touches a real server. The
source-building business logic itself lives in ``tree.config.sources`` and is
covered by ``test_sources.py`` — here it is deliberately NOT mocked so the
combined run and the ``huggingface_dataset`` fast-fail are verified end-to-end
through the CLI.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from click.testing import CliRunner


@pytest.fixture
def cli_main():
    """Import the CLI command lazily so module-load side effects stay scoped."""

    import scripts.run_data_pipeline as cli_module

    return cli_module.main


@pytest.fixture
def mock_run_offline(mocker):
    """Stub the offline Prefect/Mongo entrypoint — never hit a real server."""

    return mocker.patch(
        "scripts.run_data_pipeline._run_offline", new_callable=AsyncMock
    )


@pytest.fixture
def mock_run_online(mocker):
    """Stub the online dispatcher entrypoint — never hit a real server."""

    return mocker.patch("scripts.run_data_pipeline._run_online", new_callable=AsyncMock)


class TestRunDataPipelineCliOptions:
    def test_help_lists_mode_and_selectors(self, cli_main) -> None:
        # Arrange
        runner = CliRunner()

        # Act
        result = runner.invoke(cli_main, ["--help"])

        # Assert
        assert result.exit_code == 0
        for opt in ("--mode", "--user-id", "--source-file", "--uri", "--source"):
            assert opt in result.output

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

    def test_online_rejects_offline_selectors(self, mock_run_online, cli_main) -> None:
        # Arrange
        runner = CliRunner()

        # Act
        result = runner.invoke(
            cli_main,
            ["--mode", "online", "--source", "https://x.com/a", "--uri", "https://y"],
        )

        # Assert
        assert result.exit_code != 0
        assert "offline-only" in result.output
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


class TestRunDataPipelineOfflineForwarding:
    def test_no_flags_forwards_neither_selector(
        self, mock_run_offline, cli_main
    ) -> None:
        # Arrange
        runner = CliRunner()

        # Act
        result = runner.invoke(cli_main, [])

        # Assert
        assert result.exit_code == 0, result.output
        mock_run_offline.assert_awaited_once()
        args = mock_run_offline.await_args.args
        assert args[2] == []  # source_files
        assert args[3] == []  # inline_sources

    def test_source_file_and_uri_combined_forwards_both(
        self, mock_run_offline, cli_main
    ) -> None:
        # Arrange
        runner = CliRunner()

        # Act — combine a file with a typed and an untyped URL.
        result = runner.invoke(
            cli_main,
            [
                "--source-file",
                "sources/backfill.yaml",
                "--uri",
                "https://x.com/a",
                "--uri",
                "https://y.com/feed=substack_rss",
            ],
        )

        # Assert
        assert result.exit_code == 0, result.output
        args = mock_run_offline.await_args.args
        assert args[2] == ["sources/backfill.yaml"]
        inline = args[3]
        # The untyped URL is inferred to ``web``; the ``=substack_rss`` token is honored.
        assert inline[0]["uri"] == "https://x.com/a"
        assert inline[0]["type"] == "web"
        assert inline[1]["uri"] == "https://y.com/feed"
        assert inline[1]["type"] == "substack_rss"

    def test_huggingface_uri_fails_fast_before_any_flow(
        self, mock_run_offline, cli_main
    ) -> None:
        # Arrange
        runner = CliRunner()

        # Act — an explicit huggingface_dataset token must error out up front.
        result = runner.invoke(
            cli_main,
            ["--uri", "https://x.com/ds=huggingface_dataset"],
        )

        # Assert — non-zero exit, the error names a YAML file, and NO flow triggered.
        assert result.exit_code != 0
        assert isinstance(result.exception, ValueError)
        assert "sources/backfill.yaml" in str(result.exception)
        mock_run_offline.assert_not_awaited()


class TestRunDataPipelineOnlineForwarding:
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
