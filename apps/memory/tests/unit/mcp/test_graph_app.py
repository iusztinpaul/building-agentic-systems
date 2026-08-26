"""Unit tests for the MCP layer of the read-only Sigma graph MCP App.

Covers the rendering contract of the ``visualize_memory_graph`` tool (payload in
a ``content`` JSON block — the App-UI host does not forward
``structuredContent`` to a custom iframe), the file fallback's ``graphs://``
resource link, and the resource handler itself. The Graph renderer it delegates
to (``to_graph_payload`` / ``_render_graph_file`` / the shared templates) lives
in ``tree.memory.query.visualize`` and is tested in
``tests/unit/memory/query/test_visualize.py``.
"""

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp.tools import ToolResult

from tree.mcp.graph_app import _GRAPH_HTML, graph_file, visualize_memory_graph
from tree.memory.query.visualize import _render_graph_file, to_graph_payload
from tree.memory.types import QueryResult

_UID = "65f1a2b3c4d5e6f7a8b9c0d1"


def _node(node_id: str, node_type: str, **props: object) -> dict:
    return {
        "_id": node_id,
        "kind": "node",
        "type": node_type,
        "properties": props,
    }


def _edge(source: str, edge_type: str, target: str) -> dict:
    return {
        "_id": f"{source}|{edge_type}|{target}",
        "kind": "edge",
        "type": edge_type,
        "source_node_id": source,
        "target_node_id": target,
    }


def _seed_result() -> QueryResult:
    alice = f"{_UID}:person:alice"
    paper = f"{_UID}:document:paper"
    return QueryResult(
        nodes=[_node(alice, "person"), _node(paper, "document")],
        edges=[_edge(alice, "mentions", paper)],
    )


def _make_ctx(*, ui_supported: bool) -> MagicMock:
    ctx = MagicMock()
    ctx.client_supports_extension.return_value = ui_supported
    ctx.lifespan_context = {
        "client": MagicMock(),
        "database": "test",
        "embedding_model": MagicMock(),
        "user_id": _UID,
    }
    return ctx


def _content_payload(result: ToolResult) -> dict[str, Any]:
    """Extract the JSON payload block the iframe reads (mirrors its JS)."""

    for block in result.content:
        if block.type == "text":
            try:
                parsed = json.loads(block.text)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict) and isinstance(parsed.get("nodes"), list):
                return parsed
    raise AssertionError("No JSON payload content block found in tool result.")


# ---------------------------------------------------------------------------
# visualize_memory_graph tool — rendering-channel contract
# ---------------------------------------------------------------------------


async def test_visualize_ships_payload_in_content_block_for_ui_clients(
    mocker,
) -> None:
    # Arrange
    mocker.patch(
        "tree.mcp.graph_app.structured_query_memory",
        new=AsyncMock(return_value=_seed_result()),
    )
    ctx = _make_ctx(ui_supported=True)

    # Act
    result = await visualize_memory_graph(ctx, query="alice")

    # Assert: payload rides in a content JSON block (the channel the iframe
    # actually receives), marked audience=["user"] so the model skips it.
    assert isinstance(result, ToolResult)
    payload = _content_payload(result)
    assert len(payload["nodes"]) == 2
    assert len(payload["edges"]) == 1
    json_block = next(
        b for b in result.content if b.type == "text" and b.text.startswith("{")
    )
    assert json_block.annotations.audience == ["user"]
    assert result.structured_content is not None


async def test_visualize_empty_query_fetches_full_graph(mocker) -> None:
    # Arrange
    query_mock = mocker.patch(
        "tree.mcp.graph_app.structured_query_memory", new=AsyncMock()
    )
    full_graph_mock = mocker.patch(
        "tree.mcp.graph_app.fetch_full_graph",
        new=AsyncMock(return_value=_seed_result()),
    )
    ctx = _make_ctx(ui_supported=True)

    # Act
    result = await visualize_memory_graph(ctx)

    # Assert
    full_graph_mock.assert_awaited_once()
    query_mock.assert_not_awaited()
    assert "your full memory" in result.content[0].text


async def test_visualize_fallback_returns_path_and_resource_link(
    mocker, tmp_path: Path
) -> None:
    # Arrange: no UI extension → file fallback (browser-open suppressed).
    mocker.patch(
        "tree.mcp.graph_app.structured_query_memory",
        new=AsyncMock(return_value=_seed_result()),
    )
    mocker.patch("tree.memory.query.visualize.GRAPHS_DIR", tmp_path)
    mocker.patch("tree.mcp.graph_app.webbrowser.open", return_value=False)
    ctx = _make_ctx(ui_supported=False)

    # Act
    result = await visualize_memory_graph(ctx, query="alice")

    # Assert: the text block carries the server-side path; the resource link
    # lets a client of a REMOTE server download the same HTML over MCP.
    assert isinstance(result, ToolResult)
    text_block, link_block = result.content
    assert str(tmp_path) in text_block.text
    assert link_block.type == "resource_link"
    assert str(link_block.uri).startswith("graphs://")
    assert link_block.mimeType == "text/html"
    rendered = tmp_path / str(link_block.uri).removeprefix("graphs://")
    assert rendered.is_file()


async def test_visualize_as_html_file_forces_fallback_for_ui_clients(
    mocker, tmp_path: Path
) -> None:
    # Arrange: UI extension present but the caller asked for a file.
    mocker.patch(
        "tree.mcp.graph_app.structured_query_memory",
        new=AsyncMock(return_value=_seed_result()),
    )
    mocker.patch("tree.memory.query.visualize.GRAPHS_DIR", tmp_path)
    mocker.patch("tree.mcp.graph_app.webbrowser.open", return_value=False)
    ctx = _make_ctx(ui_supported=True)

    # Act
    result = await visualize_memory_graph(ctx, query="alice", as_html_file=True)

    # Assert
    assert result.content[0].text.count("you asked for an HTML file") == 1
    assert result.content[1].type == "resource_link"


# ---------------------------------------------------------------------------
# graphs://{name} resource — remote download of rendered files
# ---------------------------------------------------------------------------


def test_graph_file_resource_serves_rendered_html(mocker, tmp_path: Path) -> None:
    # Arrange
    mocker.patch("tree.mcp.graph_app.GRAPHS_DIR", tmp_path)
    payload = to_graph_payload(_seed_result())
    rendered = _render_graph_file(payload, output=tmp_path / "alice-x.html")

    # Act
    html = graph_file(rendered.name)

    # Assert
    assert html == rendered.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "bad_name",
    ["../../../etc/passwd", "../escape.html", "not-html.txt", "sub/dir.html"],
)
def test_graph_file_resource_rejects_unsafe_names(
    mocker, tmp_path: Path, bad_name: str
) -> None:
    # Arrange
    mocker.patch("tree.mcp.graph_app.GRAPHS_DIR", tmp_path)

    # Act / Assert
    with pytest.raises(ValueError, match="Invalid graph file name"):
        graph_file(bad_name)


def test_graph_file_resource_missing_file_raises(mocker, tmp_path: Path) -> None:
    # Arrange
    mocker.patch("tree.mcp.graph_app.GRAPHS_DIR", tmp_path)

    # Act / Assert
    with pytest.raises(FileNotFoundError, match="No rendered graph"):
        graph_file("missing.html")


def test_graph_html_reads_content_blocks_before_structured_content() -> None:
    # Assert: the widget parses content JSON blocks FIRST (the host forwards
    # only `content` to a custom iframe), with structuredContent as fallback.
    assert "ontoolresult" in _GRAPH_HTML
    assert _GRAPH_HTML.index("r.content") < _GRAPH_HTML.index("r.structuredContent")
