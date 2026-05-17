"""Unit tests for ``scripts/migrate_multi_tenancy.py``.

The migration is largely an integration concern — it talks to real
collections and Prefect — but a few decision-level branches are worth
unit-testing in isolation so a regression on the safety check or dry-run
mode never makes it to ``main``.

Loaded as a module via ``importlib.util.spec_from_file_location`` to
avoid putting the script on ``sys.path`` (mirrors the pattern in
``tests/unit/test_check_kgquery_discipline.py``).
"""

from __future__ import annotations

import importlib.util
import pathlib
from unittest.mock import AsyncMock

import pytest
from beanie import PydanticObjectId
from click.testing import CliRunner


_SCRIPT = (
    pathlib.Path(__file__).resolve().parents[2] / "scripts" / "migrate_multi_tenancy.py"
)
_spec = importlib.util.spec_from_file_location("migrate_multi_tenancy", _SCRIPT)
_module = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
assert _spec is not None and _spec.loader is not None
_spec.loader.exec_module(_module)


class TestAssertSafeToMigrate:
    async def test_passes_when_no_user_ids_populated(self, mocker):
        mocker.patch.object(
            _module,
            "_distinct_document_user_ids",
            new=AsyncMock(return_value=[None]),
        )

        # Should not raise.
        await _module._assert_safe_to_migrate(PydanticObjectId())

    async def test_passes_when_all_populated_match_seed(self, mocker):
        seed_id = PydanticObjectId()
        mocker.patch.object(
            _module,
            "_distinct_document_user_ids",
            new=AsyncMock(return_value=[seed_id, None]),
        )

        # Idempotent re-run: every populated user_id matches the seed.
        await _module._assert_safe_to_migrate(seed_id)

    async def test_aborts_when_a_different_tenant_is_present(self, mocker):
        seed_id = PydanticObjectId()
        other_id = PydanticObjectId()
        mocker.patch.object(
            _module,
            "_distinct_document_user_ids",
            new=AsyncMock(return_value=[seed_id, other_id]),
        )

        with pytest.raises(_module.MigrationAbort) as exc:
            await _module._assert_safe_to_migrate(seed_id)

        assert "already carries user_id values" in str(exc.value)

    async def test_aborts_when_multiple_tenants_present(self, mocker):
        other_a = PydanticObjectId()
        other_b = PydanticObjectId()
        mocker.patch.object(
            _module,
            "_distinct_document_user_ids",
            new=AsyncMock(return_value=[other_a, other_b]),
        )

        with pytest.raises(_module.MigrationAbort):
            await _module._assert_safe_to_migrate(PydanticObjectId())


class TestDryRunPlan:
    async def test_dry_run_creates_no_writes_and_reuses_existing_user(self, mocker):
        """Dry run must not invoke any write steps even when called via
        ``_run_migration`` directly."""

        # Stub init_mongodb so we don't hit a real Mongo.
        mocker.patch.object(_module, "init_mongodb", new=AsyncMock(return_value=None))
        # Pretend an existing user exists.
        existing_user = _module.User(
            identifier="dev@example.com", attributes={"name": "Dev"}
        )
        existing_user.id = PydanticObjectId()
        mocker.patch.object(
            _module.User,
            "find_one",
            new=AsyncMock(return_value=existing_user),
        )
        # Stub the counts so the planner has data to print.
        mocker.patch.object(_module, "_count_documents", new=AsyncMock(return_value=42))
        mocker.patch.object(
            _module, "_count_kg_entries", new=AsyncMock(return_value=100)
        )
        mocker.patch.object(
            _module,
            "_distinct_document_user_ids",
            new=AsyncMock(return_value=[None]),
        )

        # Sentinels on every mutation step — none must be called.
        backfill = mocker.patch.object(
            _module, "_backfill_documents", new=AsyncMock(return_value=0)
        )
        drop_kg = mocker.patch.object(
            _module, "_drop_knowledge_graph", new=AsyncMock(return_value=None)
        )
        refire = mocker.patch.object(
            _module, "_refire_self_person", new=AsyncMock(return_value=None)
        )
        trigger = mocker.patch.object(
            _module, "_trigger_pipelines", new=AsyncMock(return_value=None)
        )

        result = await _module._run_migration(
            identifier="dev@example.com",
            name="Dev",
            dry_run=True,
            trigger_pipelines=True,
        )

        assert result is existing_user
        backfill.assert_not_called()
        drop_kg.assert_not_called()
        refire.assert_not_called()
        trigger.assert_not_called()

    async def test_dry_run_synthesizes_user_when_missing(self, mocker):
        mocker.patch.object(_module, "init_mongodb", new=AsyncMock(return_value=None))
        mocker.patch.object(_module.User, "find_one", new=AsyncMock(return_value=None))
        mocker.patch.object(_module, "_count_documents", new=AsyncMock(return_value=0))
        mocker.patch.object(_module, "_count_kg_entries", new=AsyncMock(return_value=0))
        mocker.patch.object(
            _module,
            "_distinct_document_user_ids",
            new=AsyncMock(return_value=[]),
        )

        result = await _module._run_migration(
            identifier="newcomer@example.com",
            name=None,
            dry_run=True,
            trigger_pipelines=True,
        )

        # The synthesized user is in-memory only (no .id assigned).
        assert isinstance(result, _module.User)
        assert result.identifier == "newcomer@example.com"


class TestCLIEntryPoint:
    def test_identifier_required(self):
        """Click's ``required=True`` enforces ``--identifier`` is non-empty."""

        runner = CliRunner()
        result = runner.invoke(_module.main, [])

        assert result.exit_code != 0
        assert "Missing option" in result.output or "Usage" in result.output

    def test_abort_returns_exit_code_2(self, mocker):
        """``MigrationAbort`` from the inner coroutine surfaces as exit 2."""

        mocker.patch.object(
            _module,
            "_run_migration",
            new=AsyncMock(side_effect=_module.MigrationAbort("nope")),
        )

        runner = CliRunner()
        result = runner.invoke(_module.main, ["--identifier", "x@example.com"])

        assert result.exit_code == 2
