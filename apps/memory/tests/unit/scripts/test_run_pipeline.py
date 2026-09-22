"""Unit tests for the ``scripts/run_pipeline.py`` CLI wiring.

Operator surface only: the ``--mode`` offline/online split and the forwarding
into the two end-to-end dispatch entrypoints. The Prefect/Mongo work in
``_run_offline`` / ``_run_online`` is mocked (an external boundary); the
dispatchers themselves are covered by ``test_offline.py`` / ``test_online.py``.
"""

from __future__ import annotations

import logging
import os
from unittest.mock import AsyncMock

import pytest
from beanie import PydanticObjectId
from click.testing import CliRunner


@pytest.fixture
def cli_module():
    """Import the script lazily so module-load side effects stay scoped."""

    import scripts.run_pipeline as module

    return module


@pytest.fixture
def cli_main(cli_module):
    """The Click command under test."""

    return cli_module.main


@pytest.fixture
def mock_resolve_user(mocker, cli_module):
    """Stub Mongo init + tenant resolution (an external boundary)."""

    return mocker.patch.object(
        cli_module,
        "connect_and_resolve_user",
        new_callable=AsyncMock,
        return_value=PydanticObjectId(),
    )


@pytest.fixture
def mock_dispatch_offline(mocker, cli_module):
    """Stub the offline dispatcher — covered on its own in ``test_offline.py``."""

    return mocker.patch.object(
        cli_module,
        "dispatch_offline_pipeline",
        new_callable=AsyncMock,
        return_value={"status": "scheduled", "flow_run_id": "run-1"},
    )


@pytest.fixture
def mock_dispatch_online(mocker, cli_module):
    """Stub the online dispatcher — covered on its own in ``test_online.py``."""

    return mocker.patch.object(
        cli_module,
        "dispatch_online_pipeline",
        new_callable=AsyncMock,
        return_value={"status": "scheduled", "flow_run_id": "run-1"},
    )


@pytest.fixture
def mock_wait_for_dispatch(mocker, cli_module):
    """Stub the blocking log-stream wait (Prefect client plumbing)."""

    return mocker.patch.object(cli_module, "wait_for_dispatch", new_callable=AsyncMock)


@pytest.fixture
def mock_flush_opik(mocker, cli_module):
    """Stub the Opik telemetry flush (a third-party SDK boundary)."""

    return mocker.patch.object(cli_module, "flush_opik")


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
        runner = CliRunner()

        result = runner.invoke(cli_main, ["--mode", "online"])

        # Assert — hard CLI error BEFORE any flow is dispatched.
        assert result.exit_code != 0
        assert "--source" in result.output
        mock_run_online.assert_not_awaited()

    def test_offline_rejects_online_source(self, mock_run_offline, cli_main) -> None:
        # Arrange — the default mode is offline; --source belongs to online.
        runner = CliRunner()

        result = runner.invoke(cli_main, ["--source", "https://x.com/a"])

        assert result.exit_code != 0
        assert "online-only" in result.output
        mock_run_offline.assert_not_awaited()


class TestRunPipelineForwarding:
    def test_offline_forwards_selectors_and_num_shards(
        self, mock_run_offline, cli_main
    ) -> None:
        runner = CliRunner()

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

        assert result.exit_code == 0, result.output
        mock_run_offline.assert_awaited_once()
        args = mock_run_offline.await_args.args
        assert args[2] == ["sources/listen.yaml"]
        assert args[3] == [{"uri": "https://x.com/a", "type": "web"}]
        assert args[4] == 2

    def test_online_forwards_source_and_title(self, mock_run_online, cli_main) -> None:
        runner = CliRunner()

        result = runner.invoke(
            cli_main,
            ["--mode", "online", "--source", "/tmp/notes.md", "--title", "Notes"],
        )

        assert result.exit_code == 0, result.output
        mock_run_online.assert_awaited_once()
        args = mock_run_online.await_args.args
        assert args[2] == "/tmp/notes.md"
        assert args[3] == "Notes"


class TestRunPipelineIgnoredOverrides:
    """A model/Modal override typed HERE never reaches the flow (#159)."""

    async def test_it_warns_when_a_model_override_is_set_in_this_shell(
        self,
        cli_module,
        mock_resolve_user,
        mock_dispatch_offline,
        mock_wait_for_dispatch,
        monkeypatch,
        caplog,
    ) -> None:
        # The README's "try a Modal LLM for one run" knobs only work in the
        # SERVING process; prefixing this command with them extracts with the
        # configured provider and bills it, so say so at the mistake.
        monkeypatch.setenv("TREE_MODELS__LLM__PROVIDER", "modal")
        monkeypatch.setenv("TREE_MODAL__REQUEST_TIMEOUT_S", "600")

        with caplog.at_level(logging.WARNING, logger="tree.cli"):
            await cli_module._run_offline(None, None, [], [], 1)

        messages = [record.getMessage() for record in caplog.records]
        assert any(
            "TREE_MODELS__LLM__PROVIDER" in message
            and "make memory-serve-workflows" in message
            for message in messages
        )
        assert any("TREE_MODAL__REQUEST_TIMEOUT_S" in message for message in messages)
        # It is a hint, not a gate: the run still dispatches.
        mock_dispatch_offline.assert_awaited_once()

    async def test_the_online_path_warns_too(
        self,
        cli_module,
        mock_resolve_user,
        mock_dispatch_online,
        mock_wait_for_dispatch,
        mock_flush_opik,
        monkeypatch,
        caplog,
    ) -> None:
        # Both paths dispatch, so both must warn — ``_run_online`` had no test
        # of its own (PR #44, Nit 20).
        monkeypatch.setenv("TREE_MODELS__LLM__PROVIDER", "modal")
        monkeypatch.setenv("TREE_MODAL__REQUEST_TIMEOUT_S", "600")

        with caplog.at_level(logging.WARNING, logger="tree.cli"):
            await cli_module._run_online(None, None, "https://x.com/a", None)

        messages = [record.getMessage() for record in caplog.records]
        assert any(
            "TREE_MODELS__LLM__PROVIDER" in message
            and "make memory-serve-workflows" in message
            for message in messages
        )
        assert any("TREE_MODAL__REQUEST_TIMEOUT_S" in message for message in messages)
        mock_dispatch_online.assert_awaited_once()

    async def test_a_clean_shell_dispatches_without_a_warning(
        self,
        cli_module,
        mock_resolve_user,
        mock_dispatch_offline,
        mock_wait_for_dispatch,
        monkeypatch,
        caplog,
    ) -> None:
        # Hermetic: `make` exports `.env`, which may itself carry an override.
        for name in [
            name
            for name in os.environ
            if name.startswith(("TREE_MODELS__", "TREE_MODAL__"))
        ]:
            monkeypatch.delenv(name, raising=False)

        with caplog.at_level(logging.WARNING, logger="tree.cli"):
            await cli_module._run_offline(None, None, [], [], 1)

        assert caplog.records == []
