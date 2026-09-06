"""Unit tests for the Graph renderer — the single home of every rendering.

Covers the pure ``to_graph_payload`` transform (the **Graph payload**), the
self-contained-file writer, the ``.tree/graphs/<slug>-<stamp>.html`` output
convention, the CLI-facing ``visualize_query_result`` entry point, and the ONE
template's OPTIONAL **Embedding map** keys — asserted here from the graph's
side: a payload without them must still render the force-directed graph it
always did. The map payload that switches those keys on is built (and its
rendering asserted) in ``test_embeddings.py``; the MCP-layer concerns (tool
result channels, ``graphs://`` resource) live in
``tests/unit/mcp/test_viz_app.py``.
"""

from datetime import UTC, datetime
from pathlib import Path

from tree.config.paths import GRAPHS_DIR
from tree.memory.visualize.graph import (
    _FALLBACK_COLOUR,
    _RENDER_JS,
    _default_graph_path,
    _render_graph_file,
    _slugify,
    _truncate,
    to_graph_payload,
    visualize_query_result,
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


def _seed_result() -> QueryResult:
    alice = f"{_UID}:person:alice"
    paper = f"{_UID}:document:paper"
    return QueryResult(
        nodes=[_node(alice, "person"), _node(paper, "document")],
        edges=[_edge(alice, "mentions", paper)],
    )


# ---------------------------------------------------------------------------
# to_graph_payload — the Graph payload contract
# ---------------------------------------------------------------------------


def test_payload_maps_nodes_and_edges() -> None:
    alice = f"{_UID}:person:alice"
    paper = f"{_UID}:document:paper"
    result = QueryResult(
        nodes=[_node(alice, "person"), _node(paper, "document")],
        edges=[_edge(alice, "mentions", paper)],
    )

    payload = to_graph_payload(result)

    assert {n["id"] for n in payload["nodes"]} == {alice, paper}
    assert payload["edges"] == [
        {"source": alice, "target": paper, "type": "mentions", "meta": {}}
    ]


def test_label_strips_user_and_type_prefix() -> None:
    alice = f"{_UID}:person:alice"
    result = QueryResult(nodes=[_node(alice, "person")], edges=[])

    payload = to_graph_payload(result)

    # Assert: the {user_id}:{type}: prefix is stripped to the bare name.
    assert payload["nodes"][0]["label"] == "alice"


def test_full_name_kept_while_canvas_label_is_truncated() -> None:
    # Arrange: a name longer than the 40-char canvas-label cap.
    long = "prefers-knowledge-graph-memory-over-file-based-or-vector-based-systems"
    nid = f"{_UID}:preference:{long}"
    result = QueryResult(nodes=[_node(nid, "preference")], edges=[])

    node = to_graph_payload(result)["nodes"][0]

    # Assert: full name preserved for hover/detail; label clipped for the canvas.
    assert node["name"] == long
    assert node["label"].endswith("...")
    assert len(node["label"]) <= 40


def test_canonical_name_property_wins_over_id() -> None:
    alice = f"{_UID}:person:alice"
    result = QueryResult(
        nodes=[_node(alice, "person", canonical_name="Alice Smith")], edges=[]
    )

    payload = to_graph_payload(result)

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

    meta = to_graph_payload(QueryResult(nodes=[node], edges=[]))["nodes"][0]["meta"]

    # Assert: floats rounded, lists joined, datetimes ISO-formatted.
    assert meta["subtype"] == "engineer"
    assert meta["confidence"] == 0.877
    assert meta["description"] == "An engineer."
    assert meta["aliases"] == "Ali, Al"
    assert meta["created_at"] == created.isoformat()


def test_edge_carries_curated_metadata() -> None:
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

    meta = to_graph_payload(result)["edges"][0]["meta"]

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

    meta = to_graph_payload(QueryResult(nodes=[node], edges=[]))["nodes"][0]["meta"]

    assert meta == {"confidence": 1.0}


def test_dangling_endpoint_has_empty_meta() -> None:
    # Arrange: a node materialised only from an edge endpoint has no metadata.
    alice, ghost = f"{_UID}:person:alice", f"{_UID}:document:ghost"
    result = QueryResult(
        nodes=[_node(alice, "person")], edges=[_edge(alice, "mentions", ghost)]
    )

    payload = to_graph_payload(result)

    ghost_node = next(n for n in payload["nodes"] if n["id"] == ghost)
    assert ghost_node["meta"] == {}


def test_dangling_edge_endpoints_are_materialised_as_nodes() -> None:
    # Arrange: an edge references a node that is not in result.nodes.
    alice = f"{_UID}:person:alice"
    ghost = f"{_UID}:document:ghost"
    result = QueryResult(
        nodes=[_node(alice, "person")], edges=[_edge(alice, "mentions", ghost)]
    )

    payload = to_graph_payload(result)

    # Assert: the renderer requires every endpoint to exist as a node.
    ids = {n["id"] for n in payload["nodes"]}
    assert ghost in ids
    ghost_node = next(n for n in payload["nodes"] if n["id"] == ghost)
    assert ghost_node["type"] == "unknown"
    assert ghost_node["color"] == _FALLBACK_COLOUR


def test_edges_with_missing_endpoints_are_dropped() -> None:
    alice = f"{_UID}:person:alice"
    result = QueryResult(
        nodes=[_node(alice, "person")],
        edges=[
            {"_id": "x", "kind": "edge", "type": "mentions", "source_node_id": alice},
        ],
    )

    payload = to_graph_payload(result)

    # Assert: an edge with no target is skipped entirely.
    assert payload["edges"] == []


def test_node_type_colour_is_assigned() -> None:
    alice = f"{_UID}:person:alice"
    result = QueryResult(nodes=[_node(alice, "person")], edges=[])

    payload = to_graph_payload(result)

    # Assert: a known type gets a palette colour, not the fallback.
    assert payload["nodes"][0]["color"] != _FALLBACK_COLOUR


def test_empty_result_yields_empty_payload() -> None:
    payload = to_graph_payload(QueryResult())

    assert payload == {"nodes": [], "edges": []}


# ---------------------------------------------------------------------------
# _render_graph_file — the self-contained HTML file
# ---------------------------------------------------------------------------


def test_render_graph_file_writes_self_contained_html(tmp_path: Path) -> None:
    alice = f"{_UID}:person:alice"
    paper = f"{_UID}:document:paper"
    payload = to_graph_payload(
        QueryResult(
            nodes=[_node(alice, "person"), _node(paper, "document")],
            edges=[_edge(alice, "mentions", paper)],
        )
    )
    out = tmp_path / "graph.html"

    path = _render_graph_file(payload, output=out)

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
    alice, bob = f"{_UID}:person:alice", f"{_UID}:person:bob"
    payload = to_graph_payload(
        QueryResult(
            nodes=[_node(alice, "person"), _node(bob, "person")],
            edges=[_edge(alice, "knows", bob)],
        )
    )
    out = tmp_path / "graph.html"

    _render_graph_file(payload, output=out)

    # Assert: the metadata-card + edge-hover JS made it into the rendered file.
    html = out.read_text(encoding="utf-8")
    assert "function metaRows" in html
    assert "enableEdgeEvents" in html
    assert 'renderer.on("enterEdge"' in html


def test_render_graph_file_escapes_script_close_in_labels(tmp_path: Path) -> None:
    # Arrange: a label containing </script> must not break the inline block.
    nid = f"{_UID}:person:x"
    payload = to_graph_payload(
        QueryResult(
            nodes=[_node(nid, "person", canonical_name="</script>evil")], edges=[]
        )
    )
    out = tmp_path / "graph.html"

    _render_graph_file(payload, output=out)

    # Assert: the raw closing tag is escaped, the escaped form is present.
    html = out.read_text(encoding="utf-8")
    assert "</script>evil" not in html
    assert "<\\/script>evil" in html


# ---------------------------------------------------------------------------
# Output convention — .tree/graphs/<query-slug>-<UTC-stamp>.html
# ---------------------------------------------------------------------------


def test_slugify_makes_filesystem_safe_stem() -> None:
    assert _slugify("Overview of All Topics!") == "overview-of-all-topics"
    assert _slugify("  Tree/Memory: graph  ") == "tree-memory-graph"


def test_slugify_falls_back_to_graph_for_empty_or_symbol_only() -> None:
    assert _slugify("") == "graph"
    assert _slugify("!!!") == "graph"


def test_default_graph_path_is_unique_html_under_graphs_dir() -> None:
    path = _default_graph_path("overview of all topics")

    # Assert: discoverable .tree/graphs/ location, query-slug prefix, .html.
    assert path.parent == GRAPHS_DIR
    assert path.suffix == ".html"
    assert path.name.startswith("overview-of-all-topics-")


# ---------------------------------------------------------------------------
# visualize_query_result — the CLI-facing entry point
# ---------------------------------------------------------------------------


def test_visualize_query_result_writes_explicit_output(mocker, tmp_path: Path) -> None:
    open_mock = mocker.patch(
        "tree.memory.visualize.graph.webbrowser.open", return_value=False
    )
    out = tmp_path / "pinned.html"

    path = visualize_query_result(
        _seed_result(), out, open_browser=False, query="alice"
    )

    # Assert: an explicit output wins over the .tree/graphs/ default.
    assert path == out
    assert "const DATA =" in out.read_text(encoding="utf-8")
    open_mock.assert_not_called()


def test_visualize_query_result_defaults_to_graphs_dir(mocker, tmp_path: Path) -> None:
    mocker.patch("tree.memory.visualize.graph.GRAPHS_DIR", tmp_path)
    mocker.patch("tree.memory.visualize.graph.webbrowser.open", return_value=False)

    path = visualize_query_result(_seed_result(), open_browser=True, query="MLOps talk")

    # Assert: <query-slug>-<UTC-stamp>.html under the graphs dir.
    assert path.parent == tmp_path
    assert path.name.startswith("mlops-talk-")
    assert path.suffix == ".html"
    assert path.is_file()


def test_visualize_query_result_empty_query_uses_graph_stem(
    mocker, tmp_path: Path
) -> None:
    mocker.patch("tree.memory.visualize.graph.GRAPHS_DIR", tmp_path)
    mocker.patch("tree.memory.visualize.graph.webbrowser.open", return_value=False)

    path = visualize_query_result(_seed_result(), open_browser=False)

    # Assert: the empty-query slug falls back to "graph".
    assert path.name.startswith("graph-")


def test_visualize_query_result_survives_a_headless_browser_open(
    mocker, tmp_path: Path
) -> None:
    # Arrange: a host with no browser makes webbrowser.open raise.
    mocker.patch("tree.memory.visualize.graph.GRAPHS_DIR", tmp_path)
    mocker.patch(
        "tree.memory.visualize.graph.webbrowser.open",
        side_effect=RuntimeError("no browser"),
    )

    path = visualize_query_result(_seed_result(), open_browser=True, query="alice")

    # Assert: the render still succeeded — opening a browser is best-effort.
    assert path.is_file()


# ---------------------------------------------------------------------------
# _truncate
# ---------------------------------------------------------------------------


def test_truncate_leaves_short_text_unchanged() -> None:
    assert _truncate("hello", 10) == "hello"


def test_truncate_clips_long_text_with_ellipsis() -> None:
    result = _truncate("a" * 100, 20)

    assert len(result) == 20
    assert result.endswith("...")


def test_truncate_leaves_exact_length_unchanged() -> None:
    assert _truncate("12345", 5) == "12345"


# ---------------------------------------------------------------------------
# The ONE shared template — a Graph payload never triggers the map extensions
# ---------------------------------------------------------------------------


def test_graph_payload_render_asks_for_no_fixed_layout(tmp_path: Path) -> None:
    payload = to_graph_payload(_seed_result())
    out = tmp_path / "graph.html"

    _render_graph_file(payload, output=out)

    # Assert: none of the Embedding-map keys reach the embedded data, so the
    # template takes exactly the branches it took before the map existed.
    html = out.read_text(encoding="utf-8")
    assert '"layout": "fixed"' not in html
    # ``:`` anchors these to the embedded JSON — the DOM ids are still there.
    assert '"nodeSize":' not in html
    assert '"legend":' not in html
    assert '"warning":' not in html


def test_graph_payload_render_leaves_the_hull_toggle_and_banner_hidden(
    tmp_path: Path,
) -> None:
    payload = to_graph_payload(_seed_result())
    out = tmp_path / "graph.html"

    _render_graph_file(payload, output=out)

    # Assert: the markup ships (ONE template) but stays hidden — only a payload
    # with a legend + a boolean ``hulls`` un-hides the toggle, only a
    # ``warning`` un-hides the banner.
    html = out.read_text(encoding="utf-8")
    assert '<label id="hulls-toggle" hidden>' in html
    assert '<div id="warning" hidden></div>' in html
    assert "toggle.hidden = false" in html
    assert "warningEl.hidden = false" in html


def test_force_atlas_runs_only_outside_the_fixed_layout_branch(
    tmp_path: Path,
) -> None:
    payload = to_graph_payload(_seed_result())
    out = tmp_path / "graph.html"

    _render_graph_file(payload, output=out)

    # Assert: the layout pass is guarded, not unconditional — a fixed-layout
    # payload must keep its stored coordinates.
    html = out.read_text(encoding="utf-8")
    assert 'const isFixed = payload.layout === "fixed";' in html
    guard = "      if (!isFixed) {\n        const settings = forceAtlas2.inferSettings(graph);"
    assert guard in html
    assert "forceAtlas2.assign" in html
    assert html.index(guard) < html.index("forceAtlas2.assign")


def test_fixed_layout_payload_keeps_its_stored_coordinates(tmp_path: Path) -> None:
    # Arrange: the minimal fixed-layout payload the Embedding map builds.
    payload = {
        "nodes": [
            {
                "id": "chunk-1",
                "type": "chunk",
                "name": "Paper",
                "label": "",
                "x": 3.5,
                "y": -1.25,
                "cluster_id": 0,
                "color": "#1f77b4",
                "meta": {},
            }
        ],
        "edges": [],
        "layout": "fixed",
        "nodeSize": 4,
        "hulls": True,
        "legend": [{"label": "Topic", "size": 1, "color": "#1f77b4"}],
        "warning": None,
    }
    out = tmp_path / "map.html"

    _render_graph_file(payload, output=out)

    # Assert: the coordinates travel verbatim and the node reads them.
    html = out.read_text(encoding="utf-8")
    assert '"x": 3.5' in html
    assert '"y": -1.25' in html
    assert "x: isFixed ? n.x : Math.random()" in html


def test_template_carries_the_hull_overlay_machinery(tmp_path: Path) -> None:
    payload = to_graph_payload(_seed_result())
    out = tmp_path / "graph.html"

    _render_graph_file(payload, output=out)

    # Assert: ONE template — the hull code ships with the graph variant too,
    # dormant until a payload asks for it (Sigma has no hull primitive, so it
    # is a canvas overlay redrawn on afterRender / resize).
    html = out.read_text(encoding="utf-8")
    assert '<canvas id="hulls-layer"></canvas>' in html
    assert "function convexHull(points)" in html
    assert "renderer.graphToViewport({ x: p[0], y: p[1] })" in html
    assert 'renderer.on("afterRender", drawHulls)' in html
    assert 'renderer.on("resize", drawHulls)' in html


def test_hulls_are_never_drawn_for_noise() -> None:
    # Assert (source-level): the hull loop skips every negative cluster id, so
    # noise can never gain an outline.
    assert (
        'if (typeof n.cluster_id !== "number" || n.cluster_id < 0) continue;'
        in _RENDER_JS
    )


def test_legend_rows_are_taken_from_the_payload_when_present() -> None:
    # Assert (source-level): the per-type legend is the ELSE branch now.
    assert "if (legendRows) {" in _RENDER_JS
    assert (
        "const colorByType = new Map(nodes.map((n) => [n.type, n.color]));"
        in _RENDER_JS
    )


def test_the_header_counts_read_the_map_summary_on_a_fixed_layout() -> None:
    # Assert (source-level): a map has no edges, so the graph's header line
    # ("1448 nodes · 0 edges") reads as a broken graph. On the fixed-layout
    # branch the header shows the map's own summary instead — the ONE template,
    # two headers, chosen by the payload.
    assert 'countsEl.textContent = isFixed && typeof payload.summary === "string"' in (
        _RENDER_JS
    )
    assert '? payload.summary.replace(/^Embedding map: /, "")' in _RENDER_JS
    assert ': nodes.length + " nodes · " + edges.length + " edges";' in _RENDER_JS
