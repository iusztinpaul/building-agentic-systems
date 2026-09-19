"""Unit tests for ``scripts/reset_embeddings.py`` — the **Embedding reset** CLI.

The script is glue, so every boundary (Mongo, tenant resolution, the reset
itself) is mocked; what is asserted is the CONTRACT an operator sees on a
DESTRUCTIVE command — no ``CONFIRM=yes`` means a dry run that writes nothing and
exits non-zero, ``--yes`` means the write plus the "what to run next" line. The
guard is the whole point: the command empties vectors on whichever environment
is active, production included (ADR-009 §7).
"""

from __future__ import annotations

import inspect
from unittest.mock import AsyncMock

import pytest
from beanie import PydanticObjectId
from click.testing import CliRunner


@pytest.fixture
def cli_module():
    """Import the script lazily so module-load side effects stay scoped."""

    import scripts.reset_embeddings as module

    return module


@pytest.fixture
def resolved_user_id():
    """The tenant ``resolve_user_id`` hands back."""

    return PydanticObjectId()


@pytest.fixture
def mocked_boundaries(mocker, cli_module, resolved_user_id):
    """Stub Mongo init, tenant resolution and the reset; return the reset spy."""

    mocker.patch.object(cli_module, "init_mongodb", new_callable=AsyncMock)
    mocker.patch.object(
        cli_module,
        "resolve_user_id",
        new_callable=AsyncMock,
        return_value=resolved_user_id,
    )
    return mocker.patch.object(
        cli_module, "reset_embeddings", new_callable=AsyncMock, return_value=1842
    )


class TestResetEmbeddingsCli:
    def test_without_yes_it_is_a_dry_run_and_exits_one(
        self, cli_module, mocked_boundaries
    ) -> None:
        runner = CliRunner()

        result = runner.invoke(cli_module.main, [])

        # The operator gets a count and a non-zero exit — nothing was written,
        # so reading the command as "done" must be impossible.
        assert result.exit_code == 1
        assert mocked_boundaries.await_args.kwargs["dry_run"] is True

    def test_yes_performs_the_reset_and_exits_zero(
        self, cli_module, mocked_boundaries
    ) -> None:
        runner = CliRunner()

        result = runner.invoke(cli_module.main, ["--yes"])

        assert result.exit_code == 0, result.output
        assert mocked_boundaries.await_args.kwargs["dry_run"] is False

    def test_the_resolved_tenant_is_what_gets_reset(
        self, cli_module, mocked_boundaries, resolved_user_id
    ) -> None:
        """The reset is per user: the override must reach BOTH calls untouched.

        A tenant mix-up here empties the wrong person's vectors.
        """

        runner = CliRunner()

        result = runner.invoke(cli_module.main, ["--user-identifier", "paul", "--yes"])

        assert result.exit_code == 0, result.output
        cli_module.resolve_user_id.assert_awaited_once_with(None, "paul")
        assert mocked_boundaries.await_args.args[2] == resolved_user_id

    def test_the_script_is_glue_only(self, cli_module) -> None:
        """Entry-point rule: ``init_logger()`` at module level, no ``print``,
        and a ``_run`` that only resolves the tenant and calls the one function
        that holds the logic."""

        source = inspect.getsource(cli_module)
        run_source = inspect.getsource(cli_module._run)

        assert "init_logger()" in source
        assert "print(" not in source
        assert "update_many" not in run_source
        assert run_source.count("await ") == 3  # init_mongodb, resolve, reset
