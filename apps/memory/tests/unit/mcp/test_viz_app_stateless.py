"""The MCP App capability gate over a REAL streamable-http transport (task 196).

Horizon serves the MCP server STATELESS: every request gets a fresh
``ServerSession(stateless=True)`` that never saw the client's ``initialize``,
so ``session.client_params`` is ``None`` on ``tools/call`` and the UI
extension the client advertised is invisible. The mocked suites
(``test_viz_app.py`` & co.) fake ``ctx`` and cannot see that — so these tests
drive the real transport in-process: a bare ``FastMCP`` app built with
``http_app(stateless_http=...)``, reached over an ``httpx.ASGITransport`` (no
network), speaking raw JSON-RPC exactly as a host does. The data readers are
patched; the **Graph file** row the unknown branch writes goes to the session's
test Mongo (``tests/unit/conftest.py``).

The unknown branch's cost — a text-only client receives the payload block too —
has an opt-out every app-declaring tool's description advertises; the last
test pins it.
"""

import json
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from beanie import PydanticObjectId
from fastmcp import FastMCP
from fastmcp.apps import UI_EXTENSION_ID, UI_MIME_TYPE, AppConfig
from fastmcp.server.lifespan import lifespan
from fastmcp.tools import Tool

from tree.entities.graph_files import GraphFile
from tree.mcp import dashboard_app, graph_tools
from tree.mcp import tools as mcp_tools
from tree.mcp.viz_app import GRAPH_VIEW_URI
from tree.memory.rag.structure import synthesize_part_of_edges
from tree.memory.rag.types import MemoryStructure
from tree.memory.types import QueryResult

_USER_ID = PydanticObjectId("507f1f77bcf86cd799439011")

#: What an MCP Apps host (claude.ai, Claude Desktop) advertises at initialize.
_UI_CAPABILITIES = {"extensions": {UI_EXTENSION_ID: {"mimeTypes": [UI_MIME_TYPE]}}}
_PROTOCOL_VERSION = "2025-06-18"
_ACCEPT = "application/json, text/event-stream"


@lifespan
async def _fake_lifespan(server: FastMCP) -> AsyncGenerator[dict[str, Any], None]:
    """The tools' lifespan keys, faked: the readers that use them are patched."""

    yield {
        "client": MagicMock(),
        "database": "test",
        "llm": MagicMock(),
        "embedding_model": MagicMock(),
        "thread_id": "mcp-session-test",
    }


def _server() -> FastMCP:
    """The REAL tool functions, declared as MCP Apps exactly as production does.

    Registered by function rather than fetched from the shared ``mcp``: which
    ``visualize_memory_structure`` that instance holds depends on which test
    modules imported ``graph_tools`` first.
    """

    server = FastMCP("stateless-gate-test", lifespan=_fake_lifespan)
    server.tool(
        mcp_tools.visualize_memory_structure,
        app=AppConfig(resource_uri=GRAPH_VIEW_URI),
    )
    server.tool(
        dashboard_app.memory_dashboard,
        app=AppConfig(resource_uri=dashboard_app.DASHBOARD_VIEW_URI),
    )
    return server


def _rag_structure() -> MemoryStructure:
    """One document → one parent → one child."""

    nodes = [
        {"_id": "doc1", "kind": "node", "type": "document", "doc_rank": 1},
        {
            "_id": "p1",
            "kind": "node",
            "type": "chunk",
            "subtype": "parent",
            "parent_id": "doc1",
            "doc_rank": 1,
        },
        {
            "_id": "c1",
            "kind": "node",
            "type": "chunk",
            "subtype": "child",
            "parent_id": "p1",
            "doc_rank": 1,
        },
    ]
    return MemoryStructure(nodes=nodes, edges=synthesize_part_of_edges(nodes, _USER_ID))


def _knowledge_graph() -> QueryResult:
    alice, paper = f"{_USER_ID}:person:alice", f"{_USER_ID}:document:paper"
    return QueryResult(
        nodes=[
            {"_id": alice, "kind": "node", "type": "person", "properties": {}},
            {"_id": paper, "kind": "node", "type": "document", "properties": {}},
        ],
        edges=[
            {
                "_id": f"{alice}|mentions|{paper}",
                "kind": "edge",
                "type": "mentions",
                "source_node_id": alice,
                "target_node_id": paper,
            }
        ],
    )


@pytest.fixture(autouse=True)
def _readers(mocker) -> None:
    mocker.patch(
        "tree.mcp.tools.fetch_rag_structure",
        new_callable=AsyncMock,
        return_value=_rag_structure(),
    )
    mocker.patch(
        "tree.mcp.dashboard_app.fetch_full_graph",
        new_callable=AsyncMock,
        return_value=_knowledge_graph(),
    )


class _Host:
    """A minimal MCP host on the wire: raw JSON-RPC POSTs, as a browser sends."""

    def __init__(self, http: httpx.AsyncClient) -> None:
        self._http = http
        self._session_id: str | None = None
        self._next_id = 0

    async def _post(self, message: dict[str, Any]) -> httpx.Response:
        headers = {"accept": _ACCEPT, "mcp-protocol-version": _PROTOCOL_VERSION}
        if self._session_id is not None:
            headers["mcp-session-id"] = self._session_id
        response = await self._http.post("/mcp", json=message, headers=headers)
        response.raise_for_status()
        return response

    async def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self._next_id += 1
        response = await self._post(
            {"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": params}
        )
        body = response.json()
        assert "error" not in body, body
        return body["result"]

    async def initialize(self, *, ui: bool) -> None:
        self._next_id += 1
        response = await self._post(
            {
                "jsonrpc": "2.0",
                "id": self._next_id,
                "method": "initialize",
                "params": {
                    "protocolVersion": _PROTOCOL_VERSION,
                    "capabilities": _UI_CAPABILITIES if ui else {},
                    "clientInfo": {"name": "test-host", "version": "0"},
                },
            }
        )
        # A stateful server mints a session id; a stateless one answers none.
        self._session_id = response.headers.get("mcp-session-id")
        await self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    async def list_tools(self) -> list[dict[str, Any]]:
        return (await self._request("tools/list", {}))["tools"]

    async def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return await self._request("tools/call", {"name": name, "arguments": arguments})


@asynccontextmanager
async def _host(*, stateless: bool, ui: bool) -> AsyncIterator[_Host]:
    """A host that has run ``initialize`` against a real in-process HTTP app."""

    app = _server().http_app(stateless_http=stateless, json_response=True)
    # ``ASGITransport`` does not run the ASGI lifespan, and the streamable-http
    # session manager refuses requests until its task group is up.
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as http:
            host = _Host(http)
            await host.initialize(ui=ui)
            yield host


def _payload_blocks(result: dict[str, Any]) -> list[dict[str, Any]]:
    """The blocks the iframe's ``ontoolresult`` handler would render from.

    Mirrors ``viz_app``'s handler: a ``text`` block whose JSON has a ``nodes``
    list — and, for the model to skip it, ``audience=["user"]``.
    """

    found = []
    for block in result["content"]:
        if block["type"] != "text":
            continue
        try:
            parsed = json.loads(block["text"])
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and isinstance(parsed.get("nodes"), list):
            assert block.get("annotations", {}).get("audience") == ["user"], block
            found.append(parsed)
    return found


def _graph_links(result: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        block
        for block in result["content"]
        if block["type"] == "resource_link" and block["uri"].startswith("graphs://")
    ]


# ---------------------------------------------------------------------------
# The regression: a UI host on a stateless server mounts the view AND gets data
# ---------------------------------------------------------------------------


async def test_ui_client_on_stateless_http_gets_the_graph_payload() -> None:
    # Story: claude.ai calls visualize_memory_structure on Horizon. The host
    # mounts the iframe because tools/list declares the view; the result must
    # carry the payload the iframe reads, or it prints "No graph data in tool
    # result.".
    async with _host(stateless=True, ui=True) as host:
        tools = {tool["name"]: tool for tool in await host.list_tools()}
        result = await host.call_tool("visualize_memory_structure", {})

    assert tools["visualize_memory_structure"]["_meta"]["ui"]["resourceUri"] == (
        GRAPH_VIEW_URI
    )
    assert result.get("isError") is not True, result
    (payload,) = _payload_blocks(result)
    assert {node["id"] for node in payload["nodes"]} >= {"doc1", "p1", "c1"}
    # The capability is UNKNOWN on a stateless request, so a text-only client
    # in the same position keeps a working file path: one download link, to a
    # row that exists.
    (link,) = _graph_links(result)
    assert await GraphFile.find_one(GraphFile.name == link["name"]) is not None


async def test_ui_client_on_stateless_http_gets_the_dashboard_payload() -> None:
    async with _host(stateless=True, ui=True) as host:
        result = await host.call_tool("memory_dashboard", {})

    assert result.get("isError") is not True, result
    (payload,) = _payload_blocks(result)
    assert len(payload["nodes"]) == 2
    assert payload["query"] == ""


# ---------------------------------------------------------------------------
# Controls: a session that DID see initialize keeps today's two answers
# ---------------------------------------------------------------------------


async def test_ui_client_on_stateful_http_gets_the_payload_and_no_download() -> None:
    async with _host(stateless=False, ui=True) as host:
        result = await host.call_tool("visualize_memory_structure", {})

    assert len(_payload_blocks(result)) == 1
    assert _graph_links(result) == []


async def test_non_ui_client_on_stateful_http_gets_only_the_download() -> None:
    async with _host(stateless=False, ui=False) as host:
        graph = await host.call_tool("visualize_memory_structure", {})
        dashboard = await host.call_tool("memory_dashboard", {})

    assert _payload_blocks(graph) == []
    assert len(_graph_links(graph)) == 1
    assert _payload_blocks(dashboard) == []


async def test_as_html_file_on_stateless_http_answers_only_the_download() -> None:
    # Story: Claude Code (text-only) follows the tool description and asks for
    # the file — it must not receive the payload block.
    async with _host(stateless=True, ui=False) as host:
        result = await host.call_tool(
            "visualize_memory_structure", {"as_html_file": True}
        )

    assert _payload_blocks(result) == []
    assert len(_graph_links(result)) == 1


# ---------------------------------------------------------------------------
# The opt-out: a client that cannot show the view asks for the file alone
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fn",
    [
        mcp_tools.visualize_memory_structure,
        mcp_tools.visualize_memory_embeddings,
        graph_tools.visualize_memory_structure,
        graph_tools.query_memory,
        graph_tools.search_memory,
    ],
    ids=[
        "rag-visualize_memory_structure",
        "visualize_memory_embeddings",
        "graphrag-visualize_memory_structure",
        "query_memory",
        "search_memory",
    ],
)
def test_every_graph_tool_description_tells_text_only_clients_to_ask_for_the_file(
    fn: Any,
) -> None:
    # The description FastMCP registers is what a client's model reads.
    description = " ".join((Tool.from_function(fn).description or "").split())

    assert (
        "cannot display interactive MCP App views (e.g. a terminal client such "
        "as Claude Code)"
    ) in description
    assert "``as_html_file=true``" in description
