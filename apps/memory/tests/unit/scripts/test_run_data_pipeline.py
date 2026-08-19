"""Unit tests for the ``scripts/run_data_pipeline.py`` CLI wiring (#086, ADR-003).

Exercise the operator surface only — Click flag parsing, the ``--mode``
offline/online split, and the glue that turns ``--uri`` tokens into inline
``sources``. The Prefect/Mongo work in ``_run_offline`` / ``_run_online`` is
mocked (an external boundary), so nothing touches a real server. The
source-building business logic itself lives in ``tree.config.sources`` and is
covered by ``test_sources.py`` — here it is deliberately NOT mocked so the
combined run and the ``huggingface_dataset`` fast-fail are verified end-to-end
through the CLI.

``TestRunDataPipelineOfflineDispatch`` goes one level deeper (#099): it exercises
``_run_offline`` itself with the dispatcher mocked, asserting the data step is a
``dispatch_offline_pipeline`` call with ``run_extraction=False``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from beanie import PydanticObjectId
from click.testing import CliRunner


@pytest.fixture
def cli_module():
    """Import the script lazily so module-load side effects stay scoped."""

    import scripts.run_data_pipeline as module

    return module


@pytest.fixture
def cli_main(cli_module):
    """The Click command under test."""

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


@pytest.fixture
def resolved_user_id():
    """The tenant ``connect_and_resolve_user`` hands back."""

    return PydanticObjectId()


@pytest.fixture
def mock_resolve_user(mocker, resolved_user_id):
    """Stub Mongo init + tenant resolution (an external boundary)."""

    return mocker.patch(
        "scripts.run_data_pipeline.connect_and_resolve_user",
        new_callable=AsyncMock,
        return_value=resolved_user_id,
    )


@pytest.fixture
def mock_dispatch_offline(mocker):
    """Stub the offline dispatcher — covered on its own in ``test_offline.py``."""

    return mocker.patch(
        "scripts.run_data_pipeline.dispatch_offline_pipeline",
        new_callable=AsyncMock,
        return_value={"status": "scheduled", "flow_run_id": "run-1"},
    )


@pytest.fixture
def mock_wait_for_dispatch(mocker):
    """Stub the blocking log-stream wait (Prefect client plumbing)."""

    return mocker.patch(
        "scripts.run_data_pipeline.wait_for_dispatch", new_callable=AsyncMock
    )


@pytest.fixture
def mock_flush_opik(mocker):
    """Stub the Opik telemetry flush (a third-party SDK boundary)."""

    return mocker.patch("scripts.run_data_pipeline.flush_opik")


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


class TestRunDataPipelineOfflineDispatch:
    """``_run_offline`` funnels through ``dispatch_offline_pipeline`` (#099)."""

    async def test_offline_dispatches_offline_pipeline_with_extraction_off(
        self,
        cli_module,
        mock_resolve_user,
        mock_dispatch_offline,
        mock_wait_for_dispatch,
        mock_flush_opik,
        resolved_user_id,
    ) -> None:
        # Arrange — one source file, no inline sources.
        source_files = ["sources/listen.yaml"]

        # Act
        await cli_module._run_offline(None, None, source_files, [])

        # Assert — the data step is the offline flow with extraction disabled.
        mock_dispatch_offline.assert_awaited_once_with(
            user_id=resolved_user_id,
            source_files=source_files,
            sources=None,
            run_extraction=False,
        )
        mock_wait_for_dispatch.assert_awaited_once_with(
            mock_dispatch_offline.return_value
        )

    async def test_offline_forwards_none_when_no_selector_is_passed(
        self,
        cli_module,
        mock_resolve_user,
        mock_dispatch_offline,
        mock_wait_for_dispatch,
        mock_flush_opik,
    ) -> None:
        # Arrange — neither selector: the data coordinator owns the default set.

        # Act
        await cli_module._run_offline(None, None, [], [])

        # Assert
        kwargs = mock_dispatch_offline.await_args.kwargs
        assert kwargs["source_files"] is None
        assert kwargs["sources"] is None

    async def test_offline_forwards_inline_sources(
        self,
        cli_module,
        mock_resolve_user,
        mock_dispatch_offline,
        mock_wait_for_dispatch,
        mock_flush_opik,
    ) -> None:
        # Arrange
        inline_sources = [{"uri": "https://x.com/a", "type": "web"}]

        # Act
        await cli_module._run_offline(None, None, [], inline_sources)

        # Assert
        assert mock_dispatch_offline.await_args.kwargs["sources"] == inline_sources
