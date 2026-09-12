"""Unit tests for the MODE-NEUTRAL MCP App layer (:mod:`tree.mcp.viz_app`).

Covers ``_graph_tool_result`` — the ONE dual-path helper every visualization
tool delivers through (ADR-005, decision 4; payload in a ``content`` JSON block,
because the App-UI host does not forward ``structuredContent`` to a custom
iframe) — the file fallback's ``graphs://`` resource link, and both resource
handlers. The TOOLS that call the helper are tested where they are registered
(``visualize_memory_graph`` / ``query_memory`` / ``search_memory`` in
``test_graph_tools.py``); the Graph renderer it delegates to
(``to_graph_payload`` / ``_render_graph_file`` / the shared templates) lives in
``tree.memory.visualize.graph`` and is tested in
``tests/unit/memory/visualize/test_graph.py``.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastmcp.tools import ToolResult

from tree.mcp.server import mcp
from tree.mcp.viz_app import (
    _GRAPH_HTML,
    GRAPH_VIEW_URI,
    _graph_tool_result,
    graph_file,
    graph_view,
)
from tree.memory.types import QueryResult
from tree.memory.visualize.graph import (
    _FILE_HTML_BASE,
    _payload_noun,
    _render_graph_file,
    to_graph_payload,
)

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
    mocker.patch("tree.memory.visualize.graph.GRAPHS_DIR", tmp_path)
    mocker.patch("tree.mcp.viz_app.webbrowser.open", return_value=False)
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
    mocker.patch("tree.memory.visualize.graph.GRAPHS_DIR", tmp_path)
    mocker.patch(
        "tree.mcp.viz_app.webbrowser.open",
        side_effect=RuntimeError("no browser"),
    )
    payload = to_graph_payload(_seed_result())
    ctx = _make_ctx(ui_supported=False)

    result = _graph_tool_result(ctx, payload, "SUMMARY-SENTINEL")

    # Assert: swallowed — a missing browser never turns a good query into an error.
    assert isinstance(result, ToolResult)
    assert result.content[1].type == "resource_link"


def _map_payload() -> dict:
    """A fixed-layout payload, as ``to_embedding_map_payload`` builds it."""

    return {
        "nodes": [
            {
                "id": "chunk-1",
                "type": "chunk",
                "name": "Paper",
                "label": "",
                "x": 1.0,
                "y": 2.0,
                "cluster_id": 0,
                "color": "#1f77b4",
                "meta": {},
            }
        ],
        "edges": [],
        "layout": "fixed",
        "summary": "Embedding map: 1 chunks in 1 clusters (+0 noise)",
    }


def test_graph_tool_result_calls_a_graph_a_graph_in_both_branches(
    mocker, tmp_path: Path
) -> None:
    mocker.patch("tree.memory.visualize.graph.GRAPHS_DIR", tmp_path)
    mocker.patch("tree.mcp.viz_app.webbrowser.open", return_value=False)
    payload = to_graph_payload(_seed_result())

    inline = _graph_tool_result(_make_ctx(ui_supported=True), payload, "SUMMARY")
    fallback = _graph_tool_result(_make_ctx(ui_supported=False), payload, "SUMMARY")

    # Assert: the graph strings are BYTE-IDENTICAL to what graph tools have
    # always returned — deriving the noun from the payload changes nothing here.
    assert inline.content[0].text == "SUMMARY (interactive graph view)."
    text_block, link_block = fallback.content
    assert (
        "SUMMARY. Since this client does not render inline MCP App UIs, I saved "
        "a self-contained interactive graph to:\n"
    ) in text_block.text
    assert link_block.description == "Self-contained interactive graph (download me)"


def test_graph_tool_result_calls_an_embedding_map_a_map_in_both_branches(
    mocker, tmp_path: Path
) -> None:
    mocker.patch("tree.memory.visualize.graph.GRAPHS_DIR", tmp_path)
    mocker.patch("tree.mcp.viz_app.webbrowser.open", return_value=False)
    payload = _map_payload()

    inline = _graph_tool_result(_make_ctx(ui_supported=True), payload, "SUMMARY")
    fallback = _graph_tool_result(_make_ctx(ui_supported=False), payload, "SUMMARY")

    # Assert: in rag mode there is no graph at all, so every string the model
    # (or the user) reads calls the map a map — the glossary keeps **Embedding
    # map** and **Graph payload** distinct, and the copy follows.
    assert inline.content[0].text == "SUMMARY (interactive embedding map view)."
    text_block, link_block = fallback.content
    assert (
        "SUMMARY. Since this client does not render inline MCP App UIs, I saved "
        "a self-contained interactive embedding map to:\n"
    ) in text_block.text
    assert (
        link_block.description
        == "Self-contained interactive embedding map (download me)"
    )
    assert "graph" not in inline.content[0].text
    assert "interactive graph" not in text_block.text


def test_the_delivered_noun_comes_from_the_payloads_layout_key() -> None:
    # Assert: ONE mechanism decides the wording everywhere (the delivery helper
    # AND the file writer's log line) — the payload's own ``layout`` key.
    assert _payload_noun({"layout": "fixed"}) == "embedding map"
    assert _payload_noun({"nodes": [], "edges": []}) == "graph"


# ---------------------------------------------------------------------------
# graphs://{name} resource — remote download of rendered files
# ---------------------------------------------------------------------------


def test_graph_file_resource_serves_rendered_html(mocker, tmp_path: Path) -> None:
    mocker.patch("tree.mcp.viz_app.GRAPHS_DIR", tmp_path)
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
    mocker.patch("tree.mcp.viz_app.GRAPHS_DIR", tmp_path)

    with pytest.raises(ValueError, match="Invalid graph file name"):
        graph_file(bad_name)


def test_graph_file_resource_missing_file_raises(mocker, tmp_path: Path) -> None:
    mocker.patch("tree.mcp.viz_app.GRAPHS_DIR", tmp_path)

    with pytest.raises(FileNotFoundError, match="No rendered graph"):
        graph_file("missing.html")


def test_graph_html_reads_content_blocks_before_structured_content() -> None:
    # Assert: the widget parses content JSON blocks FIRST (the host forwards
    # only `content` to a custom iframe), with structuredContent as fallback.
    assert "ontoolresult" in _GRAPH_HTML
    assert _GRAPH_HTML.index("r.content") < _GRAPH_HTML.index("r.structuredContent")


# ---------------------------------------------------------------------------
# ui:// resource — the ONE iframe every visualization tool points at
# ---------------------------------------------------------------------------


async def test_graph_view_resource_is_registered_by_the_neutral_app_layer() -> None:
    # Assert: importing viz_app alone (no tool module) registers the shared
    # ui:// resource, so a rag-mode server can serve it too (ADR-007 §7).
    assert (await mcp.get_resource(GRAPH_VIEW_URI)) is not None
    assert graph_view() == _GRAPH_HTML


# ---------------------------------------------------------------------------
# ONE template, two variants — the iframe must not drift from the file
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "token",
    [
        'const isFixed = payload.layout === "fixed";',  # fixed coordinates
        "if (legendRows) {",  # payload legend
        '<div id="warning" hidden></div>',  # stale-map banner
        '<label id="hulls-toggle" hidden>',  # hull toggle
        '<canvas id="hulls-layer"></canvas>',  # hull overlay
        "function convexHull(points)",
        "renderer.graphToViewport({ x: p[0], y: p[1] })",
        'renderer.on("afterRender", drawHulls)',
        'renderer.on("resize", drawHulls)',
    ],
)
@pytest.mark.parametrize(
    "variant", [_GRAPH_HTML, _FILE_HTML_BASE], ids=["iframe", "file"]
)
def test_both_variants_carry_the_embedding_map_extensions(
    variant: str, token: str
) -> None:
    # Assert: the map extensions live in the SHARED pieces (_GRAPH_STYLE /
    # _BODY_MARKUP / _RENDER_JS), so the ui:// iframe and the self-contained
    # file draw a map identically — no second template to keep in sync.
    assert token in variant


def test_the_iframe_variant_only_adds_the_ext_apps_runtime() -> None:
    # Assert: the two variants differ ONLY in the MCP-layer concerns (the
    # ext-apps runtime + the ontoolresult channel) and the body height.
    assert "ext-apps" in _GRAPH_HTML
    assert "ext-apps" not in _FILE_HTML_BASE
    assert "height: 760px" in _GRAPH_HTML
    assert "height: 100vh" in _FILE_HTML_BASE
