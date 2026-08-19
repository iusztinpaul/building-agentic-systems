"""Unit tests for the ``scripts/run_indexing_pipeline.py`` CLI wiring.

Since ``free-tier-deployments`` indexing is no longer a deployment, so this
command no longer submits anything: it runs the ``memory_indexing`` flow IN THE
OPERATOR'S OWN PROCESS for the resolved user. These pin that contract (the flow
and the tenant resolution are mocked — both external boundaries) and the
CLI-layer rule that a failure exits non-zero rather than reading green.
"""

from __future__ import annotations

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
def mock_indexing(mocker, cli_module):
    """Stub the in-process ``memory_indexing`` flow run."""

    return mocker.patch.object(cli_module, "memory_indexing", new_callable=AsyncMock)


class TestRunIndexingPipeline:
    async def test_runs_the_flow_in_process_for_the_resolved_user(
        self, cli_module, mock_resolve_user, mock_indexing, resolved_user_id
    ) -> None:
        # Act
        await cli_module._run(None, None)

        # Assert — a direct flow call, scoped to the resolved tenant. No
        # deployment name, no flow-run polling: nothing is submitted anywhere.
        mock_indexing.assert_awaited_once_with(user_id=resolved_user_id)

    def test_a_failing_run_exits_non_zero(
        self, cli_module, mock_resolve_user, mock_indexing
    ) -> None:
        # Arrange — the flow raises where a deployment run used to fail remotely.
        mock_indexing.side_effect = RuntimeError("mongot down")
        runner = CliRunner()

        # Act
        result = runner.invoke(cli_module.main, [])

        # Assert — CLI-layer semantics: the operator sees a non-zero exit.
        assert result.exit_code != 0

    def test_user_identifier_option_is_forwarded(
        self, mocker, cli_module, mock_resolve_user, mock_indexing
    ) -> None:
        # Arrange
        runner = CliRunner()

        # Act
        result = runner.invoke(cli_module.main, ["--user-identifier", "paul"])

        # Assert — the tenant override reaches resolution untouched.
        assert result.exit_code == 0, result.output
        mock_resolve_user.assert_awaited_once_with(None, "paul")
