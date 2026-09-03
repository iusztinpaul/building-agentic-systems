"""Unit tests for the MCP layer of the read-only Sigma graph MCP App.

Covers ``_graph_tool_result`` — the ONE dual-path helper every graph-capable
MCP tool delivers through (ADR-005, decision 4) — plus the rendering contract
of the ``visualize_memory_graph`` tool that calls it (payload in a ``content``
JSON block, because the App-UI host does not forward ``structuredContent`` to a
custom iframe), the file fallback's ``graphs://`` resource link, and the
resource handler itself. The Graph renderer it delegates to
(``to_graph_payload`` / ``_render_graph_file`` / the shared templates) lives in
``tree.memory.query.visualize`` and is tested in
``tests/unit/memory/query/test_visualize.py``; the ``query_memory`` /
``search_memory`` half of the shared helper is tested in ``test_tools.py``.
"""

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp.tools import ToolResult

import tree.mcp.tools  # noqa: F401 — registers query_memory / search_memory
from tree.mcp.graph_app import (
    _GRAPH_HTML,
    GRAPH_VIEW_URI,
    _graph_tool_result,
    graph_file,
    visualize_memory_graph,
)
from tree.mcp.server import mcp
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
# _graph_tool_result — the ONE dual-path seam behind every graph tool
# ---------------------------------------------------------------------------


def test_graph_tool_result_keeps_summary_model_visible_and_payload_user_only() -> None:
    payload = to_graph_payload(_seed_result())
    ctx = _make_ctx(ui_supported=True)

    result = _graph_tool_result(ctx, payload, "SUMMARY-SENTINEL")

    # Assert: the model reads the summary; the full node/edge dump is addressed
    # to the iframe alone (audience=["user"]) and mirrored on structured_content.
    summary_block, payload_block = result.content
    assert "SUMMARY-SENTINEL" in summary_block.text
    assert summary_block.annotations is None
    assert payload_block.annotations.audience == ["user"]
    assert json.loads(payload_block.text) == payload
    assert result.structured_content == payload


def test_graph_tool_result_writes_a_file_and_links_it_for_non_ui_clients(
    mocker, tmp_path: Path
) -> None:
    mocker.patch("tree.memory.query.visualize.GRAPHS_DIR", tmp_path)
    mocker.patch("tree.mcp.graph_app.webbrowser.open", return_value=False)
    payload = to_graph_payload(_seed_result())
    ctx = _make_ctx(ui_supported=False)

    result = _graph_tool_result(ctx, payload, "SUMMARY-SENTINEL", query="alice")

    # Assert: the summary survives the fallback branch too, alongside the
    # server-side path and the resource link a remote client downloads.
    text_block, link_block = result.content
    assert "SUMMARY-SENTINEL" in text_block.text
    assert str(tmp_path) in text_block.text
    assert link_block.type == "resource_link"
    rendered = tmp_path / str(link_block.uri).removeprefix("graphs://")
    assert rendered.is_file()
    # No payload block on this branch — the data is inside the file.
    assert not any(b.type == "text" and b.text.startswith("{") for b in result.content)


def test_graph_tool_result_survives_a_headless_browser_open(
    mocker, tmp_path: Path
) -> None:
    # Arrange: a headless / remote server has no browser to open.
    mocker.patch("tree.memory.query.visualize.GRAPHS_DIR", tmp_path)
    mocker.patch(
        "tree.mcp.graph_app.webbrowser.open",
        side_effect=RuntimeError("no browser"),
    )
    payload = to_graph_payload(_seed_result())
    ctx = _make_ctx(ui_supported=False)

    result = _graph_tool_result(ctx, payload, "SUMMARY-SENTINEL")

    # Assert: swallowed — a missing browser never turns a good query into an error.
    assert isinstance(result, ToolResult)
    assert result.content[1].type == "resource_link"


async def test_all_three_graph_tools_declare_the_shared_ui_resource() -> None:
    # Arrange / Act: importing ``tree.mcp.tools`` (module level) registered all
    # three graph tools on the server.
    uris = {
        name: ((await mcp.get_tool(name)).meta or {}).get("ui", {}).get("resourceUri")
        for name in ("visualize_memory_graph", "query_memory", "search_memory")
    }

    # Assert: one ui:// resource serves all three (ADR-005, decision 4).
    assert set(uris.values()) == {GRAPH_VIEW_URI}
    assert (await mcp.get_resource(GRAPH_VIEW_URI)) is not None


# ---------------------------------------------------------------------------
# visualize_memory_graph tool — rendering-channel contract
# ---------------------------------------------------------------------------


async def test_visualize_ships_payload_in_content_block_for_ui_clients(
    mocker,
) -> None:
    mocker.patch(
        "tree.mcp.graph_app.structured_query_memory",
        new=AsyncMock(return_value=_seed_result()),
    )
    ctx = _make_ctx(ui_supported=True)

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
    query_mock = mocker.patch(
        "tree.mcp.graph_app.structured_query_memory", new=AsyncMock()
    )
    full_graph_mock = mocker.patch(
        "tree.mcp.graph_app.fetch_full_graph",
        new=AsyncMock(return_value=_seed_result()),
    )
    ctx = _make_ctx(ui_supported=True)

    result = await visualize_memory_graph(ctx)

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

    result = await visualize_memory_graph(ctx, query="alice", as_html_file=True)

    assert result.content[0].text.count("you asked for an HTML file") == 1
    assert result.content[1].type == "resource_link"


# ---------------------------------------------------------------------------
# graphs://{name} resource — remote download of rendered files
# ---------------------------------------------------------------------------


def test_graph_file_resource_serves_rendered_html(mocker, tmp_path: Path) -> None:
    mocker.patch("tree.mcp.graph_app.GRAPHS_DIR", tmp_path)
    payload = to_graph_payload(_seed_result())
    rendered = _render_graph_file(payload, output=tmp_path / "alice-x.html")

    html = graph_file(rendered.name)

    assert html == rendered.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "bad_name",
    ["../../../etc/passwd", "../escape.html", "not-html.txt", "sub/dir.html"],
)
def test_graph_file_resource_rejects_unsafe_names(
    mocker, tmp_path: Path, bad_name: str
) -> None:
    mocker.patch("tree.mcp.graph_app.GRAPHS_DIR", tmp_path)

    with pytest.raises(ValueError, match="Invalid graph file name"):
        graph_file(bad_name)


def test_graph_file_resource_missing_file_raises(mocker, tmp_path: Path) -> None:
    mocker.patch("tree.mcp.graph_app.GRAPHS_DIR", tmp_path)

    with pytest.raises(FileNotFoundError, match="No rendered graph"):
        graph_file("missing.html")


def test_graph_html_reads_content_blocks_before_structured_content() -> None:
    # Assert: the widget parses content JSON blocks FIRST (the host forwards
    # only `content` to a custom iframe), with structuredContent as fallback.
    assert "ontoolresult" in _GRAPH_HTML
    assert _GRAPH_HTML.index("r.content") < _GRAPH_HTML.index("r.structuredContent")
