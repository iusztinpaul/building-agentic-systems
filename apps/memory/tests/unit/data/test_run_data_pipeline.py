"""Unit tests for the ``scripts/run_data_pipeline.py`` CLI wiring (#086, ADR-003).

Exercise the operator surface only — Click flag parsing and the glue that turns
``--uri`` tokens into inline ``sources`` and forwards ``source_files`` / ``sources``
to the coordinator deployment. The Prefect/Mongo work in ``_run`` is mocked (an
external boundary), so nothing touches a real server. The source-building business
logic itself lives in ``tree.config.sources`` and is covered by
``test_sources.py`` — here it is deliberately NOT mocked so the combined run and
the ``huggingface_dataset`` fast-fail are verified end-to-end through the CLI.
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
def mock_run(mocker):
    """Stub the Prefect/Mongo entrypoint so the CLI never hits a real server."""

    return mocker.patch("scripts.run_data_pipeline._run", new_callable=AsyncMock)


class TestRunDataPipelineCliOptions:
    def test_help_lists_source_file_and_uri(self, cli_main) -> None:
        # Arrange
        runner = CliRunner()

        # Act
        result = runner.invoke(cli_main, ["--help"])

        # Assert
        assert result.exit_code == 0
        for opt in ("--user-id", "--user-identifier", "--source-file", "--uri"):
            assert opt in result.output

    def test_scheduled_only_is_no_such_option(self, cli_main) -> None:
        # Arrange — the retired flag must be a hard CLI error, not a silent accept.
        runner = CliRunner()

        # Act
        result = runner.invoke(cli_main, ["--scheduled-only"])

        # Assert
        assert result.exit_code != 0
        assert "no such option" in result.output.lower()


class TestRunDataPipelineForwarding:
    def test_no_flags_forwards_neither_selector(self, mock_run, cli_main) -> None:
        # Arrange
        runner = CliRunner()

        # Act
        result = runner.invoke(cli_main, [])

        # Assert
        assert result.exit_code == 0, result.output
        mock_run.assert_awaited_once()
        kwargs = mock_run.await_args.kwargs
        assert kwargs["source_files"] == []
        assert kwargs["inline_sources"] == []

    def test_end_to_end_flag_forwards_end_to_end_and_num_shards(
        self, mock_run, cli_main
    ) -> None:
        # Arrange
        runner = CliRunner()

        # Act
        result = runner.invoke(cli_main, ["--end-to-end", "--num-shards", "2"])

        # Assert — the flag selects the etl-offline path in _run.
        assert result.exit_code == 0, result.output
        kwargs = mock_run.await_args.kwargs
        assert kwargs["end_to_end"] is True
        assert kwargs["num_shards"] == 2

    def test_default_is_data_step_only(self, mock_run, cli_main) -> None:
        # Arrange
        runner = CliRunner()

        # Act
        result = runner.invoke(cli_main, [])

        # Assert
        assert result.exit_code == 0, result.output
        assert mock_run.await_args.kwargs["end_to_end"] is False

    def test_source_file_only_forwards_source_files(self, mock_run, cli_main) -> None:
        # Arrange
        runner = CliRunner()

        # Act
        result = runner.invoke(
            cli_main,
            ["--source-file", "sources/listen.yaml"],
        )

        # Assert
        assert result.exit_code == 0, result.output
        kwargs = mock_run.await_args.kwargs
        assert kwargs["source_files"] == ["sources/listen.yaml"]
        assert kwargs["inline_sources"] == []

    def test_uri_only_forwards_inferred_inline_sources(
        self, mock_run, cli_main
    ) -> None:
        # Arrange
        runner = CliRunner()

        # Act — an untyped URL must infer to ``web``.
        result = runner.invoke(cli_main, ["--uri", "https://x.com/a"])

        # Assert
        assert result.exit_code == 0, result.output
        kwargs = mock_run.await_args.kwargs
        assert kwargs["source_files"] == []
        assert kwargs["inline_sources"] == [{"uri": "https://x.com/a", "type": "web"}]

    def test_source_file_and_uri_combined_forwards_both(
        self, mock_run, cli_main
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
        kwargs = mock_run.await_args.kwargs
        assert kwargs["source_files"] == ["sources/backfill.yaml"]
        inline = kwargs["inline_sources"]
        # The untyped URL is inferred to ``web``; the ``=substack_rss`` token is honored.
        assert inline[0]["uri"] == "https://x.com/a"
        assert inline[0]["type"] == "web"
        assert inline[1]["uri"] == "https://y.com/feed"
        assert inline[1]["type"] == "substack_rss"

    def test_huggingface_uri_fails_fast_before_any_flow(
        self, mock_run, cli_main
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
        mock_run.assert_not_awaited()
