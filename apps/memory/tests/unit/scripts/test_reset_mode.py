"""Unit tests for ``scripts/reset_mode.py`` — the **Mode reset** CLI.

The script is glue: Mongo and ``reset_memory_mode`` are mocked, and what is
asserted is the contract an operator sees on a command that drops the memory of
EVERY user — the target and counts first, a dry run (exit 1) unless the token
matches, and a token that must say ``prod`` whenever the target is prod or an
Atlas ``mongodb+srv`` URI (direnv exports the prod vars into every shell).
"""

from __future__ import annotations

import logging
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock

import pytest
from click.testing import CliRunner, Result

from tree.config.settings import MongoSettings
from tree.db import ModeResetReport


@pytest.fixture
def cli_module():
    """Import the script lazily so module-load side effects stay scoped."""

    import scripts.reset_mode as module

    return module


@pytest.fixture
def local_mongo(mocker, cli_module) -> MongoSettings:
    """A plain ``mongodb://`` target, whatever the operator's ``.env`` says."""

    mongo = MongoSettings(mongo_scheme="mongodb", mongo_host="localhost")
    mocker.patch.object(cli_module.settings, "mongo", mongo)
    return mongo


@pytest.fixture
def reset_spy(mocker, cli_module, local_mongo) -> AsyncMock:
    """Stub the raw client and the reset; the spy echoes ``dry_run`` back."""

    client = MagicMock()
    client.close = AsyncMock()
    mocker.patch.object(cli_module, "AsyncMongoClient", return_value=client)

    async def _report(
        _client: object, database: str, *, env_target: str, dry_run: bool
    ) -> ModeResetReport:
        return ModeResetReport(
            env_target=env_target,
            target="mongodb://localhost:27017",
            database=database,
            configured_mode="rag",
            dropped={"memory": 118, "memory_clusters": 4},
            kept={"documents": 3, "users": 1},
            pending_documents=3,
            dry_run=dry_run,
        )

    return mocker.patch.object(
        cli_module, "reset_memory_mode", new_callable=AsyncMock, side_effect=_report
    )


def _invoke(
    cli_module: ModuleType, caplog: pytest.LogCaptureFixture, *args: str
) -> tuple[Result, str]:
    runner = CliRunner()
    with caplog.at_level(logging.INFO):
        result = runner.invoke(cli_module.main, list(args))
    return result, caplog.text


def _dropped(reset_spy: AsyncMock) -> bool:
    return any(not call.kwargs["dry_run"] for call in reset_spy.await_args_list)


class TestResetModeCli:
    def test_without_a_token_it_prints_the_block_drops_nothing_and_exits_one(
        self, cli_module, reset_spy, caplog
    ) -> None:
        result, logs = _invoke(cli_module, caplog, "--env-target", "local")

        assert result.exit_code == 1
        assert not _dropped(reset_spy)
        for needle in (
            "env target: local (mongodb://localhost:27017)",
            "memory.mode (configured): rag",
            "ALL users",
            "118 rows  → DROP",
            "4 rows  → DROP",
            "3 rows  → kept (3 become pending)",
            "1 rows  → kept",
            "Re-run with CONFIRM=yes",
        ):
            assert needle in logs

    def test_yes_on_a_local_target_drops_and_names_the_next_step(
        self, cli_module, reset_spy, caplog
    ) -> None:
        result, logs = _invoke(
            cli_module, caplog, "--env-target", "local", "--confirm", "yes"
        )

        assert result.exit_code == 0, result.output
        assert _dropped(reset_spy)
        assert "Next: set memory.mode" in logs
        assert "3 document(s) are pending" in logs

    def test_yes_on_the_prod_target_is_refused(
        self, cli_module, reset_spy, caplog
    ) -> None:
        result, logs = _invoke(
            cli_module, caplog, "--env-target", "prod", "--confirm", "yes"
        )

        assert result.exit_code == 1
        assert not _dropped(reset_spy)
        assert "Target is prod — re-run with CONFIRM=prod to drop it." in logs
        # The local hint would be wrong advice here.
        assert "Re-run with CONFIRM=yes" not in logs

    def test_prod_on_the_prod_target_drops(self, cli_module, reset_spy) -> None:
        result = CliRunner().invoke(
            cli_module.main, ["--env-target", "prod", "--confirm", "prod"]
        )

        assert result.exit_code == 0, result.output
        assert _dropped(reset_spy)

    @pytest.mark.parametrize("env_target", ["local", "unknown"])
    def test_an_srv_uri_is_prod_whatever_the_env_target_says(
        self, cli_module, reset_spy, mocker, caplog, env_target: str
    ) -> None:
        mocker.patch.object(
            cli_module.settings,
            "mongo",
            MongoSettings(mongo_scheme="mongodb+srv", mongo_host="tree.x.mongodb.net"),
        )

        result, logs = _invoke(
            cli_module, caplog, "--env-target", env_target, "--confirm", "yes"
        )

        assert result.exit_code == 1
        assert not _dropped(reset_spy)
        assert "CONFIRM=prod" in logs

    def test_a_direct_invocation_on_a_plain_uri_takes_yes(
        self, cli_module, reset_spy, caplog
    ) -> None:
        result, logs = _invoke(cli_module, caplog, "--confirm", "yes")

        assert result.exit_code == 0, result.output
        assert _dropped(reset_spy)
        assert "env target: unknown" in logs

    def test_the_script_never_boots_beanie(self, cli_module) -> None:
        # Booting Beanie right before the drop would recreate the OLD mode's
        # indexes for nothing — the script opens a raw client instead.
        assert not hasattr(cli_module, "init_mongodb")
