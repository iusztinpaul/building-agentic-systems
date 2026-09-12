"""Unit tests for the ``scripts/hook_session_end.py`` CLI wiring.

The script is glue around :func:`tree.mcp.hooks.run` (covered in
``tests/unit/mcp/test_hooks.py``): it reads stdin ONCE, resolves the repo-root
``.mcp.json`` and exits with what ``run`` returns. The rule the hook lives by is
pinned here — whatever arrives on stdin, the process exits 0, because a
``SessionEnd`` hook must never stand between the developer and their exit.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from click.testing import CliRunner

# ``<repo>/apps/memory/tests/unit/scripts/…`` → ``<repo>/apps/memory``, ``<repo>``.
_APP_ROOT = Path(__file__).resolve().parents[3]
_REPO_ROOT = _APP_ROOT.parent.parent


@pytest.fixture
def cli_module():
    """Import the script lazily so module-load side effects stay scoped."""

    import scripts.hook_session_end as module

    return module


@pytest.fixture
def mock_run(mocker, cli_module):
    """Stub the hook logic — the client boundary is tested on its own."""

    return mocker.patch.object(
        cli_module, "run", new_callable=AsyncMock, return_value=0
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A checkout whose root carries a ``.mcp.json``."""

    (tmp_path / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"tree-memory-local": {"command": "uv"}}}),
        encoding="utf-8",
    )
    return tmp_path


def test_empty_stdin_exits_zero(cli_module) -> None:
    # No stub: `{}` has to travel the REAL path (no session id → skip) and
    # still exit 0 — the guarantee the hook is wired on.
    result = CliRunner().invoke(cli_module.main, [], input="{}")

    assert result.exit_code == 0


def test_runs_with_no_env_file_variables() -> None:
    # The wired command carries no `--env-file`, because `uv run --env-file`
    # exits 2 on a checkout without `.env` — before Python starts, so the
    # "always exits 0" guarantee never gets a say. Proof it can: run the script
    # in a subprocess whose environment holds NOTHING from `.env` (PATH + HOME
    # only). `tree.logging` and `tree.mcp.hooks` are stdlib + fastmcp +
    # pydantic; the spawned MCP server loads `.env` through its own `.mcp.json`
    # args, not through this process.
    completed = subprocess.run(
        [sys.executable, "scripts/hook_session_end.py", "tree-memory-local"],
        input="{}",
        capture_output=True,
        text=True,
        cwd=_APP_ROOT,
        env={"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "")},
        timeout=60,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr


def test_wired_hook_command_carries_no_env_file() -> None:
    # The regression the test above guards is in the WIRING, so pin the wiring:
    # `.claude/settings.json` is what Claude Code actually runs at session end.
    settings = json.loads(
        (_REPO_ROOT / ".claude" / "settings.json").read_text(encoding="utf-8")
    )

    commands = [
        hook["command"]
        for matcher in settings["hooks"]["SessionEnd"]
        for hook in matcher["hooks"]
    ]

    # Assert the FLAG, not the whole string: that file is developer-edited
    # (plugins, skill overrides, a second hook) and equality would fail on
    # edits that have nothing to do with this guarantee.
    assert any("hook_session_end.py" in command for command in commands)
    assert not any("--env-file" in command for command in commands)


def test_blank_stdin_exits_zero(cli_module) -> None:
    result = CliRunner().invoke(cli_module.main, [], input="")

    assert result.exit_code == 0


def test_calls_run_with_the_repo_root_mcp_json(
    cli_module, mock_run, repo: Path
) -> None:
    payload = json.dumps(
        {
            "session_id": "abc",
            "transcript_path": str(repo / "transcript.jsonl"),
            "cwd": str(repo),
            "hook_event_name": "SessionEnd",
            "reason": "other",
        }
    )

    result = CliRunner().invoke(cli_module.main, ["tree-memory"], input=payload)

    assert result.exit_code == 0
    _stdin, mcp_json, server_name = mock_run.await_args.args
    assert mcp_json == repo / ".mcp.json"
    assert server_name == "tree-memory"


def test_defaults_to_the_local_server(cli_module, mock_run, repo: Path) -> None:
    result = CliRunner().invoke(
        cli_module.main, [], input=json.dumps({"cwd": str(repo)})
    )

    assert result.exit_code == 0
    assert mock_run.await_args.args[2] == "tree-memory-local"


def test_stdin_is_passed_on_to_run(cli_module, mock_run, repo: Path) -> None:
    # The script consumes stdin, so `run` must receive a replayable stream —
    # not an exhausted `sys.stdin`.
    payload = json.dumps({"session_id": "abc", "cwd": str(repo)})

    CliRunner().invoke(cli_module.main, [], input=payload)

    assert json.loads(mock_run.await_args.args[0].read()) == json.loads(payload)


def test_unknown_cwd_falls_back_to_this_checkout(
    cli_module, mock_run, tmp_path: Path
) -> None:
    # A `cwd` with no `.mcp.json` (a subdirectory, another checkout) must not
    # send the hook looking for a config that is not there.
    CliRunner().invoke(cli_module.main, [], input=json.dumps({"cwd": str(tmp_path)}))

    mcp_json = mock_run.await_args.args[1]
    assert mcp_json == cli_module._FALLBACK_REPO_ROOT / ".mcp.json"
    assert mcp_json.is_file()
