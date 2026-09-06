"""Unit tests for the ``scripts/run_clustering_pipeline.py`` CLI wiring.

Clustering is the fourth **Offline phase** (ADR-007 Decision 5) and the ONE
phase that is off everywhere else — the nightly cron, every other script — so
this command is the only way it ever runs. These pin that contract (the
dispatcher, the wait and the tenant resolution are mocked — all external
boundaries): the other three phases OFF and ``run_clustering=True``, plus the
CLI-layer rule that a failure exits non-zero rather than reading green.
"""

from __future__ import annotations

import inspect
import logging
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from beanie import PydanticObjectId
from click.testing import CliRunner


@pytest.fixture
def cli_module():
    """Import the script lazily so module-load side effects stay scoped."""

    import scripts.run_clustering_pipeline as module

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


class TestRunClusteringPipeline:
    async def test_dispatches_the_clustering_phase_only(
        self,
        cli_module,
        mock_resolve_user,
        mock_dispatch_offline,
        mock_wait_for_dispatch,
        mock_flush_opik,
        resolved_user_id,
    ) -> None:
        await cli_module._run(None, None)

        # The standalone clustering run IS the offline pipeline with the other
        # three phases off — one deployment run, scoped to the resolved tenant.
        mock_dispatch_offline.assert_awaited_once_with(
            user_id=resolved_user_id,
            run_data=False,
            run_extraction=False,
            run_indexing=False,
            run_clustering=True,
        )
        mock_wait_for_dispatch.assert_awaited_once_with(
            mock_dispatch_offline.return_value
        )

    async def test_it_warns_when_a_clustering_knob_is_set_in_this_shell(
        self,
        cli_module,
        mock_resolve_user,
        mock_dispatch_offline,
        mock_wait_for_dispatch,
        mock_flush_opik,
        monkeypatch,
        caplog,
    ) -> None:
        # The README's small-corpus knob only works in the SERVING process;
        # prefixing this command with it is a silent no-op, so the dispatcher
        # says so at the moment the operator makes the mistake.
        monkeypatch.setenv("TREE_MEMORY__CLUSTERING__HDBSCAN__MIN_CLUSTER_SIZE", "5")

        with caplog.at_level(logging.WARNING, logger="tree.cli"):
            await cli_module._run(None, None)

        assert any(
            "TREE_MEMORY__CLUSTERING__HDBSCAN__MIN_CLUSTER_SIZE" in record.getMessage()
            and "make memory-serve-workflows" in record.getMessage()
            for record in caplog.records
        )
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
        monkeypatch.delenv(
            "TREE_MEMORY__CLUSTERING__HDBSCAN__MIN_CLUSTER_SIZE", raising=False
        )

        with caplog.at_level(logging.WARNING, logger="tree.cli"):
            await cli_module._run(None, None)

        assert caplog.records == []

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

    def test_the_make_target_is_wired_to_this_script(self) -> None:
        """``make memory-run-clustering-pipeline`` is the operator's entry point.

        A script nobody can reach through the Makefile is a script nobody runs.
        """

        makefile = (Path(__file__).resolve().parents[3] / "Makefile").read_text(
            encoding="utf-8"
        )

        assert "run-clustering-pipeline:" in makefile
        assert "scripts/run_clustering_pipeline.py $(USER_FLAGS)" in makefile

    def test_the_script_is_glue_only(self, cli_module) -> None:
        """No flow import, no in-process run — every script dispatches.

        Running ``memory_clustering`` here would also drag the UMAP stack into
        the operator's own process (ADR-007 §6).
        """

        source = inspect.getsource(cli_module)

        assert "memory_clustering" not in source
        assert not hasattr(cli_module, "memory_clustering")
