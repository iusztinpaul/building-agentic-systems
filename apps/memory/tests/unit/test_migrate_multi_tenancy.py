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

    def test_reset_ontology_flag_plumbs_through_to_run_migration(self, mocker):
        """``--reset-ontology`` is wired through ``main`` to ``_run_migration``."""

        run_mock = mocker.patch.object(
            _module,
            "_run_migration",
            new=AsyncMock(return_value=None),
        )

        runner = CliRunner()
        result = runner.invoke(
            _module.main,
            ["--identifier", "x@example.com", "--reset-ontology"],
        )

        assert result.exit_code == 0, result.output
        assert run_mock.await_count == 1
        kwargs = run_mock.await_args.kwargs
        assert kwargs["reset_ontology"] is True
        assert kwargs["identifier"] == "x@example.com"

    def test_default_run_has_reset_ontology_false(self, mocker):
        """Default invocation leaves ``reset_ontology=False`` (Phase-1 path)."""

        run_mock = mocker.patch.object(
            _module,
            "_run_migration",
            new=AsyncMock(return_value=None),
        )

        runner = CliRunner()
        result = runner.invoke(_module.main, ["--identifier", "x@example.com"])

        assert result.exit_code == 0, result.output
        kwargs = run_mock.await_args.kwargs
        assert kwargs["reset_ontology"] is False


class TestResetOntologyPath:
    """Branch-level checks for the ``--reset-ontology`` migration path."""

    async def test_aborts_when_seed_user_missing(self, mocker):
        """Live ``--reset-ontology`` aborts when the seed user doesn't exist."""

        mocker.patch.object(_module, "init_mongodb", new=AsyncMock(return_value=None))
        mocker.patch.object(_module.User, "find_one", new=AsyncMock(return_value=None))

        with pytest.raises(_module.MigrationAbort) as exc:
            await _module._run_migration(
                identifier="newcomer@example.com",
                name=None,
                dry_run=False,
                trigger_pipelines=False,
                reset_ontology=True,
            )

        # The error must point operators at the bootstrap path so they
        # know the recovery procedure.
        assert "Phase-1" not in str(exc.value) or "without --reset-ontology" in str(
            exc.value
        )
        assert "newcomer@example.com" in str(exc.value)

    async def test_dry_run_reset_ontology_creates_no_writes(self, mocker):
        """Dry-run ``--reset-ontology`` must invoke no mutation step."""

        mocker.patch.object(_module, "init_mongodb", new=AsyncMock(return_value=None))
        existing_user = _module.User(
            identifier="dev@example.com", attributes={"name": "Dev"}
        )
        existing_user.id = PydanticObjectId()
        mocker.patch.object(
            _module.User, "find_one", new=AsyncMock(return_value=existing_user)
        )
        mocker.patch.object(
            _module, "_count_kg_entries", new=AsyncMock(return_value=42)
        )
        mocker.patch.object(
            _module, "_count_extraction_rejections", new=AsyncMock(return_value=3)
        )
        mocker.patch.object(
            _module, "_count_extraction_dropped_fields", new=AsyncMock(return_value=7)
        )

        drop_kg = mocker.patch.object(
            _module, "_drop_knowledge_graph", new=AsyncMock(return_value=None)
        )
        drop_audits = mocker.patch.object(
            _module,
            "_drop_extraction_audit_collections",
            new=AsyncMock(return_value=None),
        )
        refire = mocker.patch.object(
            _module, "_refire_self_person", new=AsyncMock(return_value=None)
        )
        ensure = mocker.patch.object(
            _module, "_ensure_kg_indexes", new=AsyncMock(return_value=None)
        )
        trigger = mocker.patch.object(
            _module, "_trigger_pipelines", new=AsyncMock(return_value=None)
        )

        result = await _module._run_migration(
            identifier="dev@example.com",
            name=None,
            dry_run=True,
            trigger_pipelines=True,
            reset_ontology=True,
        )

        assert result is existing_user
        drop_kg.assert_not_called()
        drop_audits.assert_not_called()
        refire.assert_not_called()
        ensure.assert_not_called()
        trigger.assert_not_called()

    async def test_reset_ontology_runs_all_steps_in_order(self, mocker):
        """Live ``--reset-ontology`` invokes every mutation step exactly once."""

        mocker.patch.object(_module, "init_mongodb", new=AsyncMock(return_value=None))
        existing_user = _module.User(
            identifier="dev@example.com", attributes={"name": "Dev"}
        )
        existing_user.id = PydanticObjectId()
        mocker.patch.object(
            _module.User, "find_one", new=AsyncMock(return_value=existing_user)
        )

        drop_kg = mocker.patch.object(
            _module, "_drop_knowledge_graph", new=AsyncMock(return_value=None)
        )
        drop_audits = mocker.patch.object(
            _module,
            "_drop_extraction_audit_collections",
            new=AsyncMock(return_value=None),
        )
        refire = mocker.patch.object(
            _module, "_refire_self_person", new=AsyncMock(return_value=None)
        )
        ensure = mocker.patch.object(
            _module, "_ensure_kg_indexes", new=AsyncMock(return_value=None)
        )
        trigger = mocker.patch.object(
            _module, "_trigger_pipelines", new=AsyncMock(return_value=None)
        )

        result = await _module._run_migration(
            identifier="dev@example.com",
            name=None,
            dry_run=False,
            trigger_pipelines=True,
            reset_ontology=True,
        )

        assert result is existing_user
        drop_kg.assert_awaited_once()
        drop_audits.assert_awaited_once()
        refire.assert_awaited_once_with(existing_user)
        ensure.assert_awaited_once()
        trigger.assert_awaited_once_with(existing_user.id)

    async def test_reset_ontology_skips_phase1_steps(self, mocker):
        """``--reset-ontology`` must NOT touch ``documents`` or call the
        Phase-1 safety check.

        Pin so a refactor that accidentally re-routes through the
        bootstrap path's ``_assert_safe_to_migrate`` (and would crash
        when a real multi-tenant DB is present) fails loudly.
        """

        mocker.patch.object(_module, "init_mongodb", new=AsyncMock(return_value=None))
        existing_user = _module.User(identifier="dev@example.com", attributes={})
        existing_user.id = PydanticObjectId()
        mocker.patch.object(
            _module.User, "find_one", new=AsyncMock(return_value=existing_user)
        )

        backfill = mocker.patch.object(
            _module, "_backfill_documents", new=AsyncMock(return_value=0)
        )
        safety = mocker.patch.object(
            _module, "_assert_safe_to_migrate", new=AsyncMock(return_value=None)
        )
        find_or_create = mocker.patch.object(
            _module,
            "_find_or_create_seed_user",
            new=AsyncMock(return_value=(existing_user, False)),
        )
        # Stub the mutation steps so the path runs cleanly.
        mocker.patch.object(
            _module, "_drop_knowledge_graph", new=AsyncMock(return_value=None)
        )
        mocker.patch.object(
            _module,
            "_drop_extraction_audit_collections",
            new=AsyncMock(return_value=None),
        )
        mocker.patch.object(
            _module, "_refire_self_person", new=AsyncMock(return_value=None)
        )
        mocker.patch.object(
            _module, "_ensure_kg_indexes", new=AsyncMock(return_value=None)
        )
        mocker.patch.object(
            _module, "_trigger_pipelines", new=AsyncMock(return_value=None)
        )

        await _module._run_migration(
            identifier="dev@example.com",
            name=None,
            dry_run=False,
            trigger_pipelines=True,
            reset_ontology=True,
        )

        backfill.assert_not_called()
        safety.assert_not_called()
        find_or_create.assert_not_called()
