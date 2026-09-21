"""Unit tests for the ``scripts/run_dream_consolidation.py`` CLI wiring.

The script takes no options (the parent flow enumerates users itself), so the
only operator-visible surface worth a unit test is the one shared with the
other dispatchers (#159): a ``TREE_MODELS__`` / ``TREE_MODAL__`` override typed
in THIS shell never reaches the flow, and the script says so before dispatch.

The Prefect client is an external boundary: ``get_client`` is mocked with a
run that is already final + completed, so the poll loop exits on its first
pass and nothing touches a real server.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest


@pytest.fixture
def cli_module():
    """Import the script lazily so module-load side effects stay scoped."""

    import scripts.run_dream_consolidation as module

    return module


@pytest.fixture
def mock_prefect_client(mocker, cli_module):
    """Stub the Prefect client: one already-completed run, no logs, no polling."""

    client = MagicMock()
    client.api_url = "http://127.0.0.1:4200/api"
    client.read_deployment_by_name = AsyncMock(
        return_value=SimpleNamespace(id="deployment-1")
    )
    # A real UUID: the log filter the script builds validates the id.
    client.create_flow_run_from_deployment = AsyncMock(
        return_value=SimpleNamespace(id=uuid4())
    )
    client.read_logs = AsyncMock(return_value=[])
    client.read_flow_run = AsyncMock(
        return_value=SimpleNamespace(
            state=SimpleNamespace(
                is_final=lambda: True,
                is_completed=lambda: True,
                name="Completed",
            )
        )
    )

    @asynccontextmanager
    async def _ctx():
        yield client

    mocker.patch(
        "scripts.run_dream_consolidation.get_client", side_effect=lambda: _ctx()
    )
    return client


class TestRunDreamConsolidationIgnoredOverrides:
    """A model/Modal override typed HERE never reaches the flow (#159)."""

    async def test_it_warns_when_a_model_override_is_set_in_this_shell(
        self,
        cli_module,
        mock_prefect_client,
        monkeypatch,
        caplog,
    ) -> None:
        # The README's "try a Modal LLM for one run" knobs only work in the
        # SERVING process; prefixing this command with them consolidates with
        # the configured provider and bills it, so say so at the mistake.
        monkeypatch.setenv("TREE_MODELS__LLM__PROVIDER", "modal")
        monkeypatch.setenv("TREE_MODAL__REQUEST_TIMEOUT_S", "600")

        with caplog.at_level(logging.WARNING, logger="tree.cli"):
            await cli_module._run()

        messages = [record.getMessage() for record in caplog.records]
        assert any(
            "TREE_MODELS__LLM__PROVIDER" in message
            and "make memory-serve-workflows" in message
            for message in messages
        )
        assert any("TREE_MODAL__REQUEST_TIMEOUT_S" in message for message in messages)
        # It is a hint, not a gate: the run still dispatches.
        mock_prefect_client.create_flow_run_from_deployment.assert_awaited_once()

    async def test_a_clean_shell_dispatches_without_a_warning(
        self,
        cli_module,
        mock_prefect_client,
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
            await cli_module._run()

        assert caplog.records == []
        mock_prefect_client.create_flow_run_from_deployment.assert_awaited_once()
