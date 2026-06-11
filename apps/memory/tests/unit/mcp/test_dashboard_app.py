"""Unit tests for the custom-HTML dashboard MCP App.

Covers the pure transforms (``node_type_counts``, ``_summary``), the rendering
contract of the ``memory_dashboard`` tool (the payload MUST ride in a
``content`` JSON block — the App-UI host does not forward ``structuredContent``
to a custom iframe), and the invariants of the served ``ui://`` HTML.
"""

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from fastmcp.tools import ToolResult

from tree.mcp.dashboard_app import (
    _DASHBOARD_HTML,
    _summary,
    dashboard_view,
    memory_dashboard,
    node_type_counts,
)
from tree.memory.types import QueryResult

_UID = "65f1a2b3c4d5e6f7a8b9c0d1"


def _node(node_id: str, node_type: str, **props: object) -> dict:
    return {
        "_id": node_id,
        "kind": "node",
        "type": node_type,
        "properties": props,
    }


def _seed_result() -> QueryResult:
    alice = f"{_UID}:person:alice"
    paper = f"{_UID}:document:paper"
    return QueryResult(
        nodes=[_node(alice, "person"), _node(paper, "document")],
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
# Pure transforms
# ---------------------------------------------------------------------------


def test_node_type_counts_descending_then_alpha() -> None:
    # Arrange
    nodes = [
        {"name": "a", "type": "chunk"},
        {"name": "b", "type": "chunk"},
        {"name": "c", "type": "person"},
        {"name": "d", "type": "document"},
        {"name": "e", "type": "document"},
    ]

    # Act
    counts = node_type_counts(nodes)

    # Assert: ordered by count desc, ties broken alphabetically.
    assert counts == [
        {"type": "chunk", "count": 2},
        {"type": "document", "count": 2},
        {"type": "person", "count": 1},
    ]


def test_summary_includes_counts_breakdown() -> None:
    # Arrange
    payload = {
        "nodes": [
            {"name": "a", "type": "person"},
            {"name": "b", "type": "person"},
            {"name": "c", "type": "document"},
        ],
        "edges": [{"source": "a", "target": "c", "type": "mentions"}],
    }

    # Act
    summary = _summary(payload, "'test'")

    # Assert
    assert "3 nodes" in summary
    assert "1 edges" in summary
    assert "person:2" in summary
    assert "document:1" in summary


# ---------------------------------------------------------------------------
# memory_dashboard tool — rendering-channel contract
# ---------------------------------------------------------------------------


async def test_dashboard_ships_payload_in_content_block_for_ui_clients(
    mocker,
) -> None:
    # Arrange
    mocker.patch(
        "tree.mcp.dashboard_app.structured_query_memory",
        new=AsyncMock(return_value=_seed_result()),
    )
    ctx = _make_ctx(ui_supported=True)

    # Act
    result = await memory_dashboard(ctx, query="alice")

    # Assert: payload rides in a content JSON block (the channel the iframe
    # actually receives), marked audience=["user"] so the model skips it.
    assert isinstance(result, ToolResult)
    payload = _content_payload(result)
    assert payload["query"] == "alice"
    assert len(payload["nodes"]) == 2
    assert len(payload["edges"]) == 1
    json_block = next(
        b for b in result.content if b.type == "text" and b.text.startswith("{")
    )
    assert json_block.annotations.audience == ["user"]


async def test_dashboard_keeps_structured_content_for_forwarding_hosts(
    mocker,
) -> None:
    # Arrange
    mocker.patch(
        "tree.mcp.dashboard_app.structured_query_memory",
        new=AsyncMock(return_value=_seed_result()),
    )
    ctx = _make_ctx(ui_supported=True)

    # Act
    result = await memory_dashboard(ctx, query="alice")

    # Assert
    assert result.structured_content is not None
    assert len(result.structured_content["nodes"]) == 2


async def test_dashboard_model_facing_summary_is_short(mocker) -> None:
    # Arrange
    mocker.patch(
        "tree.mcp.dashboard_app.structured_query_memory",
        new=AsyncMock(return_value=_seed_result()),
    )
    ctx = _make_ctx(ui_supported=True)

    # Act
    result = await memory_dashboard(ctx, query="alice")

    # Assert: the first content block is the human/model summary, not data.
    first = result.content[0]
    assert first.type == "text"
    assert "2 nodes" in first.text
    assert "interactive dashboard" in first.text


async def test_dashboard_falls_back_to_text_without_ui_extension(mocker) -> None:
    # Arrange
    mocker.patch(
        "tree.mcp.dashboard_app.structured_query_memory",
        new=AsyncMock(return_value=_seed_result()),
    )
    ctx = _make_ctx(ui_supported=False)

    # Act
    result = await memory_dashboard(ctx, query="alice")

    # Assert: plain text summary, no app payload.
    assert isinstance(result, str)
    assert "2 nodes" in result


async def test_dashboard_empty_query_covers_full_graph(mocker) -> None:
    # Arrange
    query_mock = mocker.patch(
        "tree.mcp.dashboard_app.structured_query_memory", new=AsyncMock()
    )
    full_graph_mock = mocker.patch(
        "tree.mcp.dashboard_app.fetch_full_graph",
        new=AsyncMock(return_value=_seed_result()),
    )
    ctx = _make_ctx(ui_supported=True)

    # Act
    result = await memory_dashboard(ctx)

    # Assert: no query → the full-graph fetch, labelled as such.
    full_graph_mock.assert_awaited_once()
    query_mock.assert_not_awaited()
    assert "your full memory" in result.content[0].text


# ---------------------------------------------------------------------------
# ui:// resource — served HTML invariants
# ---------------------------------------------------------------------------


def test_dashboard_view_serves_resolved_html() -> None:
    # Act
    html = dashboard_view()

    # Assert: a complete document with the CDN token spliced in.
    assert html is _DASHBOARD_HTML
    assert html.startswith("<!DOCTYPE html>")
    assert "__EXT_APPS_CDN__" not in html
    assert "@modelcontextprotocol/ext-apps" in html


def test_dashboard_html_reads_content_blocks_before_structured_content() -> None:
    # Assert: the widget parses content JSON blocks FIRST (the host forwards
    # only `content` to a custom iframe), with structuredContent as fallback.
    assert "ontoolresult" in _DASHBOARD_HTML
    assert _DASHBOARD_HTML.index("r.content") < _DASHBOARD_HTML.index(
        "r.structuredContent"
    )


def test_dashboard_html_renders_tables_and_chart_client_side() -> None:
    # Assert: the dashboard pieces (KPIs, chart, tables) are hand-rolled DOM —
    # no Prefab renderer, no extra CDN libraries.
    assert "makeTable" in _DASHBOARD_HTML
    assert 'id="metrics"' in _DASHBOARD_HTML
    assert 'id="chart"' in _DASHBOARD_HTML
    assert "prefab" not in _DASHBOARD_HTML.lower()
    assert "cdn.jsdelivr.net" not in _DASHBOARD_HTML


def test_dashboard_html_escapes_user_controlled_values() -> None:
    # Assert: the esc() helper exists and is applied to row values.
    assert "function esc(" in _DASHBOARD_HTML
    assert 'esc(val ?? "")' in _DASHBOARD_HTML
