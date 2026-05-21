"""Unit tests for ``scripts/sync_concurrency_limits.py`` (#054 / ADR-002).

The script's live run talks to a Prefect server (integration concern), but
the command-construction and create-vs-update branch are pure decision logic
worth unit-testing in isolation so a regression on the derived ``prefect gcl``
argv never makes it to ``main``.

Loaded as a module via ``importlib.util.spec_from_file_location`` to avoid
putting the script on ``sys.path`` (mirrors ``tests/unit/test_migrate_multi_tenancy.py``).
"""

from __future__ import annotations

import importlib.util
import pathlib
import subprocess
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

_SCRIPT = (
    pathlib.Path(__file__).resolve().parents[2]
    / "scripts"
    / "sync_concurrency_limits.py"
)
_spec = importlib.util.spec_from_file_location("sync_concurrency_limits", _SCRIPT)
_module = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
assert _spec is not None and _spec.loader is not None
_spec.loader.exec_module(_module)


class TestBuildCommand:
    @pytest.mark.parametrize("action", ["create", "update"])
    def test_derives_limit_and_decay_from_rpm(self, action):
        """The argv carries ``--limit <rpm>`` and ``--slot-decay-per-second
        <rpm/60>`` for the ``voyage-embeddings`` GCL (ADR-002 §1)."""

        command = _module._build_command(action, limit=3, slot_decay_per_second=0.05)

        assert command == [
            "prefect",
            "gcl",
            action,
            "voyage-embeddings",
            "--limit",
            "3",
            "--slot-decay-per-second",
            "0.05",
        ]


class TestRun:
    def test_creates_when_limit_absent(self, mocker):
        """When ``voyage-embeddings`` does not exist, the script issues
        ``prefect gcl create`` with the config-derived limit and decay."""

        mocker.patch.object(
            _module,
            "app_config",
            SimpleNamespace(concurrency=SimpleNamespace(voyage_rpm=3)),
        )
        mocker.patch.object(_module, "_limit_exists", return_value=False)
        run = mocker.patch.object(
            _module.subprocess,
            "run",
            return_value=subprocess.CompletedProcess([], 0, stdout="ok", stderr=""),
        )

        _module._run()

        argv = run.call_args.args[0]
        assert argv[:4] == ["prefect", "gcl", "create", "voyage-embeddings"]
        assert "--limit" in argv and argv[argv.index("--limit") + 1] == "3"
        decay = argv[argv.index("--slot-decay-per-second") + 1]
        assert float(decay) == pytest.approx(0.05)

    def test_updates_when_limit_present(self, mocker):
        """When the limit already exists, the script issues ``update`` instead
        of ``create``."""

        mocker.patch.object(
            _module,
            "app_config",
            SimpleNamespace(concurrency=SimpleNamespace(voyage_rpm=3)),
        )
        mocker.patch.object(_module, "_limit_exists", return_value=True)
        run = mocker.patch.object(
            _module.subprocess,
            "run",
            return_value=subprocess.CompletedProcess([], 0, stdout="ok", stderr=""),
        )

        _module._run()

        argv = run.call_args.args[0]
        assert argv[:4] == ["prefect", "gcl", "update", "voyage-embeddings"]

    def test_exits_nonzero_when_prefect_fails(self, mocker):
        """A non-zero ``prefect gcl`` exit propagates as ``SystemExit(1)``."""

        mocker.patch.object(
            _module,
            "app_config",
            SimpleNamespace(concurrency=SimpleNamespace(voyage_rpm=3)),
        )
        mocker.patch.object(_module, "_limit_exists", return_value=False)
        mocker.patch.object(
            _module.subprocess,
            "run",
            return_value=subprocess.CompletedProcess([], 1, stdout="", stderr="boom"),
        )

        with pytest.raises(SystemExit) as exc:
            _module._run()

        assert exc.value.code == 1

    def test_cli_invokes_run(self, mocker):
        """The Click entrypoint delegates to ``_run``."""

        run = mocker.patch.object(_module, "_run")

        result = CliRunner().invoke(_module.main, [])

        assert result.exit_code == 0
        run.assert_called_once_with()
