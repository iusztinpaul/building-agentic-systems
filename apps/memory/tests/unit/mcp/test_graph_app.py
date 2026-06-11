"""Unit tests for the read-only Sigma graph MCP App.

Covers the pure ``to_graph_payload`` transform, the rendering contract of the
``visualize_memory_graph`` tool (payload in a ``content`` JSON block — the
App-UI host does not forward ``structuredContent`` to a custom iframe), the
file fallback's ``graphs://`` resource link, and the resource handler itself.
"""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastmcp.tools import ToolResult

from tree.config.paths import GRAPHS_DIR
from tree.mcp.graph_app import (
    _FALLBACK_COLOUR,
    _GRAPH_HTML,
    _default_graph_path,
    _render_graph_file,
    _slugify,
    graph_file,
    to_graph_payload,
    visualize_memory_graph,
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


def _edge(source: str, edge_type: str, target: str) -> dict:
    return {
        "_id": f"{source}|{edge_type}|{target}",
        "kind": "edge",
        "type": edge_type,
        "source_node_id": source,
        "target_node_id": target,
    }


def test_payload_maps_nodes_and_edges() -> None:
    # Arrange
    alice = f"{_UID}:person:alice"
    paper = f"{_UID}:document:paper"
    result = QueryResult(
        nodes=[_node(alice, "person"), _node(paper, "document")],
        edges=[_edge(alice, "mentions", paper)],
    )

    # Act
    payload = to_graph_payload(result)

    # Assert
    assert {n["id"] for n in payload["nodes"]} == {alice, paper}
    assert payload["edges"] == [
        {"source": alice, "target": paper, "type": "mentions", "meta": {}}
    ]


def test_label_strips_user_and_type_prefix() -> None:
    # Arrange
    alice = f"{_UID}:person:alice"
    result = QueryResult(nodes=[_node(alice, "person")], edges=[])

    # Act
    payload = to_graph_payload(result)

    # Assert: the {user_id}:{type}: prefix is stripped to the bare name.
    assert payload["nodes"][0]["label"] == "alice"


def test_full_name_kept_while_canvas_label_is_truncated() -> None:
    # Arrange: a name longer than the 40-char canvas-label cap.
    long = "prefers-knowledge-graph-memory-over-file-based-or-vector-based-systems"
    nid = f"{_UID}:preference:{long}"
    result = QueryResult(nodes=[_node(nid, "preference")], edges=[])

    # Act
    node = to_graph_payload(result)["nodes"][0]

    # Assert: full name preserved for hover/detail; label clipped for the canvas.
    assert node["name"] == long
    assert node["label"].endswith("...")
    assert len(node["label"]) <= 40


def test_canonical_name_property_wins_over_id() -> None:
    # Arrange
    alice = f"{_UID}:person:alice"
    result = QueryResult(
        nodes=[_node(alice, "person", canonical_name="Alice Smith")], edges=[]
    )

    # Act
    payload = to_graph_payload(result)

    # Assert
    assert payload["nodes"][0]["label"] == "Alice Smith"


def test_node_carries_curated_metadata() -> None:
    # Arrange: a node row with the curated top-level metadata fields.
    created = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    node = {
        "_id": f"{_UID}:person:alice",
        "kind": "node",
        "type": "person",
        "properties": {"canonical_name": "Alice"},
        "subtype": "engineer",
        "confidence": 0.876543,
        "description": "An engineer.",
        "aliases": ["Ali", "Al"],
        "created_at": created,
    }

    # Act
    meta = to_graph_payload(QueryResult(nodes=[node], edges=[]))["nodes"][0]["meta"]

    # Assert: floats rounded, lists joined, datetimes ISO-formatted.
    assert meta["subtype"] == "engineer"
    assert meta["confidence"] == 0.877
    assert meta["description"] == "An engineer."
    assert meta["aliases"] == "Ali, Al"
    assert meta["created_at"] == created.isoformat()


def test_edge_carries_curated_metadata() -> None:
    # Arrange
    alice, bob = f"{_UID}:person:alice", f"{_UID}:person:bob"
    edge = {
        "_id": f"{alice}|knows|{bob}",
        "kind": "edge",
        "type": "knows",
        "source_node_id": alice,
        "target_node_id": bob,
        "semantic_type": "social",
        "confidence": 0.5,
        "description": "They met in 2024.",
    }
    result = QueryResult(
        nodes=[_node(alice, "person"), _node(bob, "person")], edges=[edge]
    )

    # Act
    meta = to_graph_payload(result)["edges"][0]["meta"]

    # Assert
    assert meta == {
        "semantic_type": "social",
        "confidence": 0.5,
        "description": "They met in 2024.",
    }


def test_curated_meta_drops_empty_and_null_fields() -> None:
    # Arrange: empty string / None / empty list must be dropped, real values kept.
    node = {
        "_id": f"{_UID}:person:x",
        "kind": "node",
        "type": "person",
        "properties": {},
        "subtype": "",
        "description": None,
        "aliases": [],
        "confidence": 1.0,
    }

    # Act
    meta = to_graph_payload(QueryResult(nodes=[node], edges=[]))["nodes"][0]["meta"]

    # Assert
    assert meta == {"confidence": 1.0}


def test_dangling_endpoint_has_empty_meta() -> None:
    # Arrange: a node materialised only from an edge endpoint has no metadata.
    alice, ghost = f"{_UID}:person:alice", f"{_UID}:document:ghost"
    result = QueryResult(
        nodes=[_node(alice, "person")], edges=[_edge(alice, "mentions", ghost)]
    )

    # Act
    payload = to_graph_payload(result)

    # Assert
    ghost_node = next(n for n in payload["nodes"] if n["id"] == ghost)
    assert ghost_node["meta"] == {}


def test_dangling_edge_endpoints_are_materialised_as_nodes() -> None:
    # Arrange: an edge references a node that is not in result.nodes.
    alice = f"{_UID}:person:alice"
    ghost = f"{_UID}:document:ghost"
    result = QueryResult(
        nodes=[_node(alice, "person")], edges=[_edge(alice, "mentions", ghost)]
    )

    # Act
    payload = to_graph_payload(result)

    # Assert: d3.forceLink requires every endpoint to exist as a node.
    ids = {n["id"] for n in payload["nodes"]}
    assert ghost in ids
    ghost_node = next(n for n in payload["nodes"] if n["id"] == ghost)
    assert ghost_node["type"] == "unknown"
    assert ghost_node["color"] == _FALLBACK_COLOUR


def test_edges_with_missing_endpoints_are_dropped() -> None:
    # Arrange
    alice = f"{_UID}:person:alice"
    result = QueryResult(
        nodes=[_node(alice, "person")],
        edges=[
            {"_id": "x", "kind": "edge", "type": "mentions", "source_node_id": alice},
        ],
    )

    # Act
    payload = to_graph_payload(result)

    # Assert: an edge with no target is skipped entirely.
    assert payload["edges"] == []


def test_node_type_colour_is_assigned() -> None:
    # Arrange
    alice = f"{_UID}:person:alice"
    result = QueryResult(nodes=[_node(alice, "person")], edges=[])

    # Act
    payload = to_graph_payload(result)

    # Assert: a known type gets a palette colour, not the fallback.
    assert payload["nodes"][0]["color"] != _FALLBACK_COLOUR


def test_empty_result_yields_empty_payload() -> None:
    # Act
    payload = to_graph_payload(QueryResult())

    # Assert
    assert payload == {"nodes": [], "edges": []}


def test_render_graph_file_writes_self_contained_html(tmp_path: Path) -> None:
    # Arrange
    alice = f"{_UID}:person:alice"
    paper = f"{_UID}:document:paper"
    payload = to_graph_payload(
        QueryResult(
            nodes=[_node(alice, "person"), _node(paper, "document")],
            edges=[_edge(alice, "mentions", paper)],
        )
    )
    out = tmp_path / "graph.html"

    # Act
    path = _render_graph_file(payload, output=out)

    # Assert
    assert path == out
    html = out.read_text(encoding="utf-8")
    # Self-contained: data embedded inline, Sigma + ForceAtlas2 present, NO ext-apps.
    assert "const DATA =" in html
    assert "new Sigma(" in html
    assert "forceAtlas2" in html
    assert "ext-apps" not in html
    assert "ontoolresult" not in html
    # The actual node id made it into the embedded data.
    assert alice in html


def test_render_graph_file_wires_metadata_and_edge_hover(tmp_path: Path) -> None:
    # Arrange
    alice, bob = f"{_UID}:person:alice", f"{_UID}:person:bob"
    payload = to_graph_payload(
        QueryResult(
            nodes=[_node(alice, "person"), _node(bob, "person")],
            edges=[_edge(alice, "knows", bob)],
        )
    )
    out = tmp_path / "graph.html"

    # Act
    _render_graph_file(payload, output=out)

    # Assert: the metadata-card + edge-hover JS made it into the rendered file.
    html = out.read_text(encoding="utf-8")
    assert "function metaRows" in html
    assert "enableEdgeEvents" in html
    assert 'renderer.on("enterEdge"' in html


def test_slugify_makes_filesystem_safe_stem() -> None:
    # Act / Assert
    assert _slugify("Overview of All Topics!") == "overview-of-all-topics"
    assert _slugify("  Tree/Memory: graph  ") == "tree-memory-graph"


def test_slugify_falls_back_to_graph_for_empty_or_symbol_only() -> None:
    # Act / Assert
    assert _slugify("") == "graph"
    assert _slugify("!!!") == "graph"


def test_default_graph_path_is_unique_html_under_graphs_dir() -> None:
    # Act
    path = _default_graph_path("overview of all topics")

    # Assert: discoverable .tree/graphs/ location, query-slug prefix, .html.
    assert path.parent == GRAPHS_DIR
    assert path.suffix == ".html"
    assert path.name.startswith("overview-of-all-topics-")


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
    mocker.patch("tree.mcp.graph_app.GRAPHS_DIR", tmp_path)
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
    mocker.patch("tree.mcp.graph_app.GRAPHS_DIR", tmp_path)
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


def test_render_graph_file_escapes_script_close_in_labels(tmp_path: Path) -> None:
    # Arrange: a label containing </script> must not break the inline block.
    nid = f"{_UID}:person:x"
    payload = to_graph_payload(
        QueryResult(
            nodes=[_node(nid, "person", canonical_name="</script>evil")], edges=[]
        )
    )
    out = tmp_path / "graph.html"

    # Act
    _render_graph_file(payload, output=out)

    # Assert: the raw closing tag is escaped, the escaped form is present.
    html = out.read_text(encoding="utf-8")
    assert "</script>evil" not in html
    assert "<\\/script>evil" in html
