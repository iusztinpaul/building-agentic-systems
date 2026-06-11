"""Unit tests for MCP server boot-time user_id resolution (#020).

Replaces the transient ``_resolve_active_user_id`` helper from #019 with
strict ``--user-id`` / ``TREE_USER_IDENTIFIER`` resolution. The server
MUST fail to boot if neither is provided — there is no silent fallback
to a default user.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from beanie import PydanticObjectId

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


class TestResolveServerUserId:
    async def test_cli_user_id_wins(self, mocker) -> None:
        """A direct ``user_id`` argument bypasses the env lookup entirely."""

        oid = PydanticObjectId()
        # ``User.find_one`` must NOT be called when the id is provided.
        mock_find = mocker.patch(
            "tree.entities.users.User.find_one", new_callable=AsyncMock
        )

        resolved = await server_module._resolve_server_user_id(
            user_id=oid, identifier=None
        )

        assert resolved == oid
        mock_find.assert_not_awaited()

    async def test_env_identifier_resolves_via_user_lookup(self, mocker) -> None:
        """``TREE_USER_IDENTIFIER`` path looks the user up by identifier."""

        target_id = PydanticObjectId()
        fake_user = mocker.MagicMock()
        fake_user.id = target_id
        mock_find = mocker.patch(
            "tree.entities.users.User.find_one",
            new_callable=AsyncMock,
            return_value=fake_user,
        )

        resolved = await server_module._resolve_server_user_id(
            user_id=None, identifier="paul@example.com"
        )

        assert resolved == target_id
        mock_find.assert_awaited_once()

    async def test_identifier_with_no_matching_user_raises(self, mocker) -> None:
        """Identifier set but no matching user → loud, actionable failure."""

        mocker.patch(
            "tree.entities.users.User.find_one",
            new_callable=AsyncMock,
            return_value=None,
        )

        with pytest.raises(RuntimeError, match="no User"):
            await server_module._resolve_server_user_id(
                user_id=None, identifier="missing@example.com"
            )

    async def test_neither_provided_raises(self) -> None:
        """Both inputs absent → boot must fail with an actionable message."""

        with pytest.raises(RuntimeError, match=r"--user-id.*TREE_USER_IDENTIFIER"):
            await server_module._resolve_server_user_id(user_id=None, identifier=None)


class TestSetServerUserId:
    """The module-level ``_SERVER_USER_ID`` is the single read source for tools."""

    def test_set_and_get_round_trip(self) -> None:
        oid = PydanticObjectId()
        previous = server_module._SERVER_USER_ID
        try:
            server_module.set_server_user_id(oid)
            assert server_module.get_server_user_id() == oid
        finally:
            server_module._SERVER_USER_ID = previous

    def test_get_before_set_raises(self) -> None:
        previous = server_module._SERVER_USER_ID
        try:
            server_module._SERVER_USER_ID = None
            with pytest.raises(RuntimeError, match=r"not been initialised"):
                server_module.get_server_user_id()
        finally:
            server_module._SERVER_USER_ID = previous
