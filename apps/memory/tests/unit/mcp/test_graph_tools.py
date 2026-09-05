"""Unit tests for the graphrag-only MCP tools (:mod:`tree.mcp.graph_tools`).

Registered only when the server boots in graphrag mode (#110); the behaviour
asserted here — the dual graph delivery of ``query_memory`` /
``search_memory(visualize=True)`` and the embedding-stripping serializer — is
unchanged by that move.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from bson import ObjectId
from fastmcp.tools import ToolResult

from tree.mcp.graph_tools import _serialize, query_memory, search_memory
from tree.memory.types import QueryResult


class TestSerialize:
    def test_strips_embedding_field(self):
        docs = [
            {"_id": "person:alice", "name": "Alice", "embedding": [0.1, 0.2]},
            {"_id": "person:bob", "name": "Bob", "embedding": [0.3, 0.4]},
        ]
        result = _serialize(docs)

        assert "embedding" not in result
        assert "Alice" in result
        assert "Bob" in result

    def test_handles_objectid(self):
        oid = ObjectId("507f1f77bcf86cd799439011")
        docs = [{"_id": "person:alice", "source": oid}]
        result = _serialize(docs)

        assert "507f1f77bcf86cd799439011" in result

    def test_handles_datetime(self):
        dt = datetime(2024, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        docs = [{"_id": "person:alice", "created_at": dt}]
        result = _serialize(docs)

        assert "2024" in result

    def test_empty_list(self):
        result = _serialize([])

        assert result == "[]"

    def test_does_not_mutate_original(self):
        docs = [{"_id": "person:alice", "embedding": [0.1]}]
        _serialize(docs)

        assert "embedding" in docs[0]


# ---------------------------------------------------------------------------
# query_memory / search_memory with visualize=True — the dual delivery
# (ADR-005, decision 4): both tools route through the ONE shared seam
# ``_dual_graph_result`` → ``graph_app._graph_tool_result``, so from a
# visualization standpoint they behave exactly like visualize_memory_graph.
# ---------------------------------------------------------------------------

_GRAPH_UID = "65f1a2b3c4d5e6f7a8b9c0d1"


def _node(node_id: str, node_type: str) -> dict[str, Any]:
    return {
        "_id": f"{_GRAPH_UID}:{node_type}:{node_id}",
        "kind": "node",
        "type": node_type,
        "properties": {"name": node_id},
    }


def _edge(source: str, edge_type: str, target: str) -> dict[str, Any]:
    return {
        "_id": f"{source}|{edge_type}|{target}",
        "kind": "edge",
        "type": edge_type,
        "source_node_id": source,
        "target_node_id": target,
    }


_ALICE = f"{_GRAPH_UID}:person:alice"
_PAPER = f"{_GRAPH_UID}:document:paper"
_GRAPH_DOCS = [
    _node("alice", "person"),
    _node("paper", "document"),
    _edge(_ALICE, "mentions", _PAPER),
]


def _make_graph_ctx(*, ui_supported: bool) -> MagicMock:
    ctx = MagicMock()
    ctx.client_supports_extension.return_value = ui_supported
    ctx.lifespan_context = {
        "client": MagicMock(),
        "database": "test_db",
        "llm": MagicMock(),
        "embedding_model": MagicMock(),
        "user_id": _GRAPH_UID,
    }
    return ctx


def _patch_query(mocker, tool_name: str, docs: list[dict[str, Any]]) -> None:
    """Stub whichever query engine the tool under test delegates to."""

    if tool_name == "query_memory":
        mocker.patch(
            "tree.mcp.graph_tools.execute_nl_query", new=AsyncMock(return_value=docs)
        )
        return
    edges = [d for d in docs if d.get("kind") == "edge"]
    nodes = [d for d in docs if d.get("kind") != "edge"]
    mocker.patch(
        "tree.mcp.graph_tools.structured_query_memory",
        new=AsyncMock(return_value=QueryResult(nodes=nodes, edges=edges)),
    )


_TOOLS = [("query_memory", query_memory), ("search_memory", search_memory)]


@pytest.mark.parametrize("tool_name,tool", _TOOLS)
class TestGraphToolsDualDelivery:
    async def test_visualize_false_returns_the_plain_serialized_string(
        self, mocker, tool_name, tool
    ) -> None:
        _patch_query(mocker, tool_name, _GRAPH_DOCS)
        ctx = _make_graph_ctx(ui_supported=True)

        result = await tool(query="alice", ctx=ctx)

        # Assert: unchanged contract — no ToolResult, no graph, no iframe payload.
        assert isinstance(result, str)
        assert not isinstance(result, ToolResult)
        assert result == _serialize(_GRAPH_DOCS)

    async def test_visualize_keeps_serialized_output_visible_to_the_model(
        self, mocker, tool_name, tool
    ) -> None:
        _patch_query(mocker, tool_name, _GRAPH_DOCS)
        ctx = _make_graph_ctx(ui_supported=True)

        result = await tool(query="alice", ctx=ctx, visualize=True)

        # Assert: the tool still answers the question — the serialized rows are
        # in the model-visible block, not swapped out for the graph.
        assert isinstance(result, ToolResult)
        assert _serialize(_GRAPH_DOCS) in result.content[0].text

    async def test_visualize_ships_the_graph_payload_to_the_iframe_only(
        self, mocker, tool_name, tool
    ) -> None:
        _patch_query(mocker, tool_name, _GRAPH_DOCS)
        ctx = _make_graph_ctx(ui_supported=True)

        result = await tool(query="alice", ctx=ctx, visualize=True)

        # Assert: node/edge dump rides in the audience=["user"] block.
        payload_block = result.content[1]
        assert payload_block.annotations.audience == ["user"]
        payload = json.loads(payload_block.text)
        assert len(payload["nodes"]) == 2
        assert len(payload["edges"]) == 1
        assert result.structured_content == payload

    async def test_visualize_falls_back_to_a_file_and_resource_link(
        self, mocker, tool_name, tool, tmp_path: Path
    ) -> None:
        # Arrange: a client that renders no MCP App UIs (e.g. the terminal).
        _patch_query(mocker, tool_name, _GRAPH_DOCS)
        mocker.patch("tree.memory.query.visualize.GRAPHS_DIR", tmp_path)
        mocker.patch("tree.mcp.graph_app.webbrowser.open", return_value=False)
        ctx = _make_graph_ctx(ui_supported=False)

        result = await tool(query="alice", ctx=ctx, visualize=True)

        # Assert: serialized rows + the server-side path + the download link.
        text_block, link_block = result.content
        assert _serialize(_GRAPH_DOCS) in text_block.text
        assert str(tmp_path) in text_block.text
        assert str(link_block.uri).startswith("graphs://")
        rendered = tmp_path / str(link_block.uri).removeprefix("graphs://")
        assert rendered.is_file()

    async def test_docs_without_kind_skip_the_graph_and_stay_a_plain_string(
        self, mocker, tool_name, tool
    ) -> None:
        # Arrange: an aggregation / projection that dropped the ``kind`` field.
        docs = [{"_id": "agg", "count": 7}]
        _patch_query(mocker, tool_name, docs)
        ctx = _make_graph_ctx(ui_supported=True)

        result = await tool(query="how many", ctx=ctx, visualize=True)

        assert isinstance(result, str)
        assert not isinstance(result, ToolResult)
        assert result.startswith(_serialize(docs))
        assert "Visualization skipped" in result
