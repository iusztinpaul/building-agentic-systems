"""Unit tests for the ``scripts/run_indexing_pipeline.py`` CLI wiring.

Since ADR-007 Decision 5 indexing is an **Offline phase**, so this command is
glue like every other script: it DISPATCHES the ``offline-pipeline`` deployment
with the data and extraction phases OFF, then blocks on the run. These pin that
contract (the dispatcher, the wait and the tenant resolution are mocked — all
external boundaries) and the CLI-layer rule that a failure exits non-zero rather
than reading green.
"""

from __future__ import annotations

import inspect
import logging
import os
from unittest.mock import AsyncMock

import pytest
from beanie import PydanticObjectId
from click.testing import CliRunner


@pytest.fixture
def cli_module():
    """Import the script lazily so module-load side effects stay scoped."""

    import scripts.run_indexing_pipeline as module

    return module


@pytest.fixture
def resolved_user_id():
    """The tenant ``connect_and_resolve_user`` hands back."""

    return PydanticObjectId()


@pytest.fixture
def mock_resolve_user(mocker, cli_module, resolved_user_id):
    """Stub Mongo init + tenant resolution (an external boundary)."""

    return mocker.patch.object(
        cli_module,
        "connect_and_resolve_user",
        new_callable=AsyncMock,
        return_value=resolved_user_id,
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
def mock_wait_for_dispatch(mocker, cli_module):
    """Stub the blocking log-stream wait (Prefect client plumbing)."""

    return mocker.patch.object(cli_module, "wait_for_dispatch", new_callable=AsyncMock)


@pytest.fixture
def mock_flush_opik(mocker, cli_module):
    """Stub the Opik telemetry flush (a third-party SDK boundary)."""

    return mocker.patch.object(cli_module, "flush_opik")


class TestRunIndexingPipeline:
    async def test_dispatches_the_indexing_phase_only(
        self,
        cli_module,
        mock_resolve_user,
        mock_dispatch_offline,
        mock_wait_for_dispatch,
        mock_flush_opik,
        resolved_user_id,
    ) -> None:
        await cli_module._run(None, None)

        # The standalone indexing run IS the offline pipeline with the other two
        # phases off — one deployment run, scoped to the resolved tenant.
        mock_dispatch_offline.assert_awaited_once_with(
            user_id=resolved_user_id,
            run_data=False,
            run_extraction=False,
            run_indexing=True,
        )
        mock_wait_for_dispatch.assert_awaited_once_with(
            mock_dispatch_offline.return_value
        )

    def test_a_failing_run_exits_non_zero(
        self,
        cli_module,
        mock_resolve_user,
        mock_dispatch_offline,
        mock_wait_for_dispatch,
        mock_flush_opik,
    ) -> None:
        # Arrange — submission blows up (unreachable API, missing deployment).
        mock_dispatch_offline.side_effect = RuntimeError("Failed to reach API")
        runner = CliRunner()

        result = runner.invoke(cli_module.main, [])

        # Assert — CLI-layer semantics: the operator sees a non-zero exit.
        assert result.exit_code != 0

    def test_user_identifier_option_is_forwarded(
        self,
        cli_module,
        mock_resolve_user,
        mock_dispatch_offline,
        mock_wait_for_dispatch,
        mock_flush_opik,
    ) -> None:
        runner = CliRunner()

        result = runner.invoke(cli_module.main, ["--user-identifier", "paul"])

        # Assert — the tenant override reaches resolution untouched.
        assert result.exit_code == 0, result.output
        mock_resolve_user.assert_awaited_once_with(None, "paul")

    def test_the_script_never_runs_the_flow_in_process(self, cli_module) -> None:
        """Glue only: no ``memory_indexing`` import, no in-process flow run.

        Every other script dispatches; this one used to be the exception (it ran
        the flow in the operator's process), which is exactly what ADR-007
        Decision 5 removed.
        """

        source = inspect.getsource(cli_module)

        assert "memory_indexing" not in source
        assert not hasattr(cli_module, "memory_indexing")


class TestRunIndexingPipelineIgnoredOverrides:
    """A model/Modal override typed HERE never reaches the flow (#159)."""

    async def test_it_warns_when_a_model_override_is_set_in_this_shell(
        self,
        cli_module,
        mock_resolve_user,
        mock_dispatch_offline,
        mock_wait_for_dispatch,
        mock_flush_opik,
        monkeypatch,
        caplog,
    ) -> None:
        # The backfill re-embeds with whatever the SERVING process reads — an
        # embedding override typed here would silently re-embed with the old
        # model, which is the one thing an Embedding reset must not do.
        monkeypatch.setenv("TREE_MODELS__SEARCH_EMBEDDING__PROVIDER", "modal")
        monkeypatch.setenv("TREE_MODAL__WARMUP_DEADLINE_S", "1200")

        with caplog.at_level(logging.WARNING, logger="tree.cli"):
            await cli_module._run(None, None)

        messages = [record.getMessage() for record in caplog.records]
        assert any(
            "TREE_MODELS__SEARCH_EMBEDDING__PROVIDER" in message
            and "make memory-serve-workflows" in message
            for message in messages
        )
        assert any("TREE_MODAL__WARMUP_DEADLINE_S" in message for message in messages)
        # It is a hint, not a gate: the run still dispatches.
        mock_dispatch_offline.assert_awaited_once()

    async def test_a_clean_shell_dispatches_without_a_warning(
        self,
        cli_module,
        mock_resolve_user,
        mock_dispatch_offline,
        mock_wait_for_dispatch,
        mock_flush_opik,
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
            await cli_module._run(None, None)

        assert caplog.records == []
