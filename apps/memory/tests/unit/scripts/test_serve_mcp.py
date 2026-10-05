"""``scripts/serve_mcp.py`` is glue: logging + ``mcp.run(transport)``, no user.

The server pins no user (ADR-014 §1), so the entrypoint takes ``--transport``
only and never touches Mongo — the lifespan's ``init_mongodb`` is the one
connection per boot.
"""

from pathlib import Path

import pytest
from click.testing import CliRunner

from tree.mcp.server import mcp

_MEMORY_APP = Path(__file__).resolve().parents[3]
_SERVE_MCP = _MEMORY_APP / "scripts" / "serve_mcp.py"
_MAKEFILE = _MEMORY_APP / "Makefile"


@pytest.fixture
def cli_module():
    """Import the script lazily so module-load side effects stay scoped."""

    import scripts.serve_mcp as module

    return module


@pytest.mark.parametrize(
    "argv,expected",
    [([], {}), (["--transport", "streamable-http"], {"transport": "streamable-http"})],
    ids=["default-stdio", "streamable-http"],
)
def test_main_runs_the_server_on_the_requested_transport(
    mocker, cli_module, argv: list[str], expected: dict[str, str]
) -> None:
    run = mocker.patch.object(mcp, "run")

    result = CliRunner().invoke(cli_module.main, argv)

    assert result.exit_code == 0, result.output
    run.assert_called_once_with(**expected)


@pytest.mark.parametrize("flag", ["--user-id", "--identifier"])
def test_main_rejects_the_removed_user_flags(mocker, cli_module, flag: str) -> None:
    run = mocker.patch.object(mcp, "run")

    result = CliRunner().invoke(cli_module.main, [flag, "paul@example.com"])

    assert result.exit_code == 2
    run.assert_not_called()


def test_serve_mcp_source_names_no_user_flag() -> None:
    source = _SERVE_MCP.read_text()

    assert "--user-id" not in source
    assert "--identifier" not in source
    assert "init_mongodb" not in source


def _serve_mcp_recipe() -> str:
    """The ``serve-mcp`` target line plus its tab-indented recipe lines."""

    lines = _MAKEFILE.read_text().splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("serve-mcp:"))
    recipe = [lines[start]]
    for line in lines[start + 1 :]:
        if not line.startswith("\t"):
            break
        recipe.append(line)
    return "\n".join(recipe)


def test_serve_mcp_make_target_needs_no_user() -> None:
    recipe = _serve_mcp_recipe()

    assert "USER_ID" not in recipe
    assert "--user-id" not in recipe


def test_pipeline_targets_keep_their_per_run_tenant_flags() -> None:
    """``USER_FLAGS`` is the operator's per-run tenant, not the server's user."""

    flags = next(
        line
        for line in _MAKEFILE.read_text().splitlines()
        if line.startswith("USER_FLAGS")
    )

    assert "--user-id" in flags
    assert "--user-identifier" in flags
