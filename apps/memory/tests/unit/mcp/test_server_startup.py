"""Unit tests for the MCP server boot (``tree.mcp.server``).

Since ADR-014 the boot pins NO user: the five pinning names are gone and the
lifespan context carries no ``user_id`` — every tool resolves the **Request
user** per call (``tree.mcp.request_user``).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from tree.mcp import server as server_module

_SERVER_PY = Path(server_module.__file__).resolve()


def test_entrypoint_loaded_by_path_registers_all_tools() -> None:
    """FastMCP Cloud loads the entrypoint ``server.py:mcp`` BY FILE PATH (module
    name ``server``), not as the package ``tree.mcp.server``. The tool modules
    register via ``from tree.mcp.server import mcp``; without the module alias in
    ``server.py`` that import builds a SECOND ``FastMCP`` instance and the
    path-loaded object Horizon serves ends up with 0 tools.

    Regression guard: load the file exactly as the platform does and assert the
    served object carries the same (non-zero) tool set as the package import.
    Runs in a fresh interpreter so the test process's own ``tree.mcp.server``
    import can't mask the double-import.
    """

    probe = (
        "import importlib.util, asyncio, sys;"
        f"spec = importlib.util.spec_from_file_location('server', r'{_SERVER_PY}');"
        "mod = importlib.util.module_from_spec(spec);"
        "sys.modules['server'] = mod;"
        "spec.loader.exec_module(mod);"
        "import tree.mcp.server as pkg;"
        "a = len(asyncio.run(mod.mcp.list_tools()));"
        "b = len(asyncio.run(pkg.mcp.list_tools()));"
        "print(a, b, mod.mcp is pkg.mcp);"
        "assert a == b and a > 0, f'path-loaded={a} package={b}'"
    )

    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True
    )

    assert result.returncode == 0, (
        f"entrypoint path-load lost its tools: {result.stderr}"
    )


_REMOVED_PINNING_NAMES = [
    "_SERVER_USER_ID",
    "set_server_user_id",
    "get_server_user_id",
    "_resolve_server_user_id",
    "_resolve_user_id_from_env",
]


@pytest.mark.parametrize("name", _REMOVED_PINNING_NAMES)
def test_server_module_no_longer_pins_a_user(name: str) -> None:
    assert not hasattr(server_module, name)


@pytest.fixture
def booted_lifespan(mocker) -> dict[str, AsyncMock | MagicMock]:
    """The lifespan's infrastructure, faked: no Mongo, no models, no Opik."""

    client = MagicMock()
    client.close = AsyncMock()
    mocker.patch.object(server_module, "configure_opik")
    mocker.patch.object(server_module, "flush_opik")
    mocker.patch.object(server_module, "get_llm", return_value=MagicMock())
    mocker.patch.object(server_module, "get_embedding_model", return_value=MagicMock())
    return {
        "init_mongodb": mocker.patch.object(
            server_module, "init_mongodb", new_callable=AsyncMock, return_value=client
        ),
        "ensure_indexes": mocker.patch.object(
            server_module, "ensure_indexes", new_callable=AsyncMock
        ),
        "assert_index": mocker.patch.object(
            server_module,
            "assert_settings_match_live_vector_index",
            new_callable=AsyncMock,
        ),
    }


async def test_lifespan_context_carries_no_user(mocker, booted_lifespan) -> None:
    mocker.patch.object(server_module.settings, "mcp_skip_index_bootstrap", False)

    async with server_module.app_lifespan(server_module.mcp) as context:
        keys = set(context)

    assert keys == {"client", "database", "llm", "embedding_model", "thread_id"}


async def test_lifespan_ensures_indexes_without_a_user(mocker, booted_lifespan) -> None:
    mocker.patch.object(server_module.settings, "mcp_skip_index_bootstrap", False)

    async with server_module.app_lifespan(server_module.mcp):
        pass

    booted_lifespan["ensure_indexes"].assert_awaited_once()
    assert "user_id" not in booted_lifespan["ensure_indexes"].await_args.kwargs


async def test_lifespan_connects_to_mongo_exactly_once(mocker, booted_lifespan) -> None:
    """The ONE ``init_mongodb`` per boot: the entrypoint no longer pre-connects."""

    mocker.patch.object(server_module.settings, "mcp_skip_index_bootstrap", True)

    async with server_module.app_lifespan(server_module.mcp):
        pass

    booted_lifespan["init_mongodb"].assert_awaited_once()
