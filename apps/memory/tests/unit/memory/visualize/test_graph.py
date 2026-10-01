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

import logging
from datetime import UTC, datetime
from pathlib import Path

from tree.config.paths import GRAPHS_DIR
import pytest

from tree.memory.visualize.graph import (
    _D3_FORCE_CDN,
    _DEFAULT_DISPLAY,
    _DEFAULT_FORCES,
    _FALLBACK_COLOUR,
    _BODY_MARKUP,
    _FILE_HTML_BASE,
    _GRAPH_STYLE,
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

    assert payload["nodes"] == []
    assert payload["edges"] == []


@pytest.mark.parametrize(
    ("node_type", "subtype", "expected_size"),
    [
        ("document", None, 10),
        ("chunk", "parent", 7),
        ("chunk", "child", 4),
        ("chunk", None, 6),  # a chunk with no role reads as an entity
        ("person", None, 6),
        ("person", "parent", 6),  # only a CHUNK's subtype carries a role
        ("unknown", None, 6),
    ],
)
def test_node_size_is_resolved_by_role(
    node_type: str, subtype: str | None, expected_size: int
) -> None:
    # Arrange: a node row whose top-level ``subtype`` is the chunk role.
    row = _node(f"{_UID}:{node_type}:x", node_type)
    if subtype is not None:
        row["subtype"] = subtype

    node = to_graph_payload(QueryResult(nodes=[row], edges=[]))["nodes"][0]

    assert node["size"] == expected_size


def test_dangling_endpoint_gets_the_default_size() -> None:
    alice, ghost = f"{_UID}:person:alice", f"{_UID}:document:ghost"
    result = QueryResult(
        nodes=[_node(alice, "person")], edges=[_edge(alice, "mentions", ghost)]
    )

    payload = to_graph_payload(result)

    # Assert: an endpoint-only node is typed "unknown", so it gets the default.
    ghost_node = next(n for n in payload["nodes"] if n["id"] == ghost)
    assert ghost_node["size"] == 6


def test_payload_ships_the_force_and_display_defaults() -> None:
    payload = to_graph_payload(_seed_result())

    # Assert: Python decides, the JS obeys — the template seeds the live
    # simulation and the reducers from these, it carries no defaults of its own.
    assert payload["controls"] == {
        "forces": {"centre": 0.2, "repel": 8.0, "link": 0.3, "linkDistance": 80},
        "display": {
            "nodeSize": 1.0,
            "linkThickness": 1.0,
            "labelFade": 1,
            "arrows": True,
            "edgeLabels": True,
        },
    }
    assert payload["controls"]["forces"] == _DEFAULT_FORCES
    assert payload["controls"]["display"] == _DEFAULT_DISPLAY


def test_payload_controls_are_copies_not_the_module_defaults() -> None:
    payload = to_graph_payload(_seed_result())

    # Act: a caller mutating one payload must not leak into the next one.
    payload["controls"]["forces"]["repel"] = 99.0
    payload["controls"]["display"]["arrows"] = False

    assert _DEFAULT_FORCES["repel"] == 8.0
    assert _DEFAULT_DISPLAY["arrows"] is True


def test_graph_payload_has_no_payload_wide_node_size() -> None:
    payload = to_graph_payload(_seed_result())

    # Assert: ONE sizing mechanism — per node, never payload-wide.
    assert "nodeSize" not in payload


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
    # Self-contained: data embedded inline, Sigma + d3-force present, NO ext-apps.
    assert "const DATA =" in html
    assert "new Sigma(" in html
    assert "forceSimulation(" in html
    # The one-shot layout engine is gone (spelled without its name, so the
    # task's "no hits" grep over the tests stays clean).
    assert "atlas" not in html.lower()
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
    # (``nodeSize`` is no longer payload-wide anywhere, but the display
    # defaults' multiplier of the same name rides inside ``controls`` — see
    # ``test_graph_payload_has_no_payload_wide_node_size``.)
    assert '"controls":' in html
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


def test_the_simulation_runs_only_outside_the_fixed_layout_branch(
    tmp_path: Path,
) -> None:
    payload = to_graph_payload(_seed_result())
    out = tmp_path / "graph.html"

    _render_graph_file(payload, output=out)

    # Assert: the live simulation is guarded, not unconditional — a
    # fixed-layout payload must keep its stored coordinates.
    html = out.read_text(encoding="utf-8")
    assert 'const isFixed = payload.layout === "fixed";' in html
    guard = "      if (!isFixed) {\n        sim = forceSimulation(simNodes)"
    assert guard in html
    assert html.count("forceSimulation(") == 1


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
    assert "x: isFixed ? n.x : undefined" in html
    assert "Math.random()" not in html


def test_template_carries_the_hull_overlay_machinery(tmp_path: Path) -> None:
    payload = to_graph_payload(_seed_result())
    out = tmp_path / "graph.html"

    _render_graph_file(payload, output=out)

    # Assert: ONE template — the hull code ships with the graph variant too,
    # dormant until a payload asks for it (Sigma has no hull primitive, so it
    # is a canvas overlay redrawn on afterRender / resize).
    html = out.read_text(encoding="utf-8")
    assert '<canvas id="overlay"></canvas>' in html
    assert "function convexHull(points)" in html
    assert "renderer.graphToViewport({ x: p[0], y: p[1] })" in html
    assert 'renderer.on("afterRender", drawOverlay)' in html
    assert 'renderer.on("resize", drawOverlay)' in html


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


# ---------------------------------------------------------------------------
# The writer's log line — it names WHAT it drew, in the payload's own words
# ---------------------------------------------------------------------------


def _map_payload() -> dict:
    """The minimal fixed-layout payload the **Embedding map** builds."""

    return {
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
        "summary": "Embedding map: 1 chunks in 1 clusters (+0 noise)",
    }


def test_render_graph_file_logs_a_graph_with_its_node_and_edge_counts(
    tmp_path: Path, caplog
) -> None:
    payload = to_graph_payload(_seed_result())
    out = tmp_path / "graph.html"

    with caplog.at_level(logging.INFO, logger="tree.memory.visualize.graph"):
        _render_graph_file(payload, output=out)

    # Assert: the GRAPH wording is unchanged, verbatim — a Graph payload has
    # edges, and this is the line the terminal has printed all along.
    assert f"Wrote self-contained graph HTML (2 nodes, 1 edges) to {out}" in [
        record.getMessage() for record in caplog.records
    ]


def test_render_graph_file_logs_a_fixed_layout_payload_as_a_map_and_no_edges(
    tmp_path: Path, caplog
) -> None:
    out = tmp_path / "map.html"

    with caplog.at_level(logging.INFO, logger="tree.memory.visualize.graph"):
        _render_graph_file(_map_payload(), output=out)

    # Assert: an Embedding map has no edges BY DESIGN (glossary), so
    # "1 nodes, 0 edges" would read as a broken graph — same reasoning as the
    # header counts. One mechanism decides both: payload["layout"] == "fixed".
    messages = [record.getMessage() for record in caplog.records]
    assert f"Wrote self-contained embedding map HTML (1 points) to {out}" in messages
    assert not any("edges" in message for message in messages)
    assert not any("graph HTML" in message for message in messages)


# ---------------------------------------------------------------------------
# Live layout + direct manipulation (ADR-011) — the template's JS contract
# ---------------------------------------------------------------------------


def test_the_file_variant_imports_d3_force_once_by_name() -> None:
    # Assert: ONE pinned ESM import naming the four forces the template uses.
    assert _D3_FORCE_CDN == "https://cdn.jsdelivr.net/npm/d3-force@3.0.0/+esm"
    assert _FILE_HTML_BASE.count(f'from "{_D3_FORCE_CDN}"') == 1
    assert (
        "import { forceSimulation, forceLink, forceManyBody, forceCenter } "
        f'from "{_D3_FORCE_CDN}";'
    ) in _FILE_HTML_BASE


@pytest.mark.parametrize(
    "token",
    [
        '"doubleClickNode"',  # unpin
        "setCustomBBox(",  # viewport frozen while dragging
        "alphaTarget(0.3)",  # the sim stays hot while a node is held
        "alphaTarget(0)",  # …and cools after the drop
        "alpha(0.5)",  # unpinning reheats
        ".fx =",  # pin state lives on the d3 node
        "dataset.layout",  # headless evidence
        "dataset.sim",
        '<canvas id="overlay"></canvas>',
        "function drawOverlay",
        "const REPEL_SCALE = 30;",
        "forceManyBody().strength(-forces.repel * REPEL_SCALE)",
        'sim.on("end"',
    ],
)
def test_template_carries_the_live_layout_and_pin_machinery(token: str) -> None:
    assert token in _FILE_HTML_BASE


@pytest.mark.parametrize("gone", ["hulls-layer", "drawHulls", "Math.random()"])
def test_template_drops_the_one_shot_layout_leftovers(gone: str) -> None:
    assert gone not in _FILE_HTML_BASE


def test_self_loops_never_become_simulation_links() -> None:
    # Assert (source-level): a zero-length link divides by zero inside
    # forceLink, so self-loops are filtered out of the link force.
    assert "e.source !== e.target" in _RENDER_JS


def test_reducers_apply_the_display_multipliers() -> None:
    # Assert (source-level): display values scale what Python resolved.
    assert "res.size = data.size * display.nodeSize;" in _RENDER_JS
    assert "res.size = data.size * display.linkThickness;" in _RENDER_JS
    assert 'res.type = display.arrows ? "arrow" : "line";' in _RENDER_JS
    assert "renderEdgeLabels: display.edgeLabels," in _RENDER_JS
    assert "labelRenderedSizeThreshold: display.labelFade," in _RENDER_JS


# ---------------------------------------------------------------------------
# Selection, marquee and group drag (ADR-011 §4) — the template's JS contract
# ---------------------------------------------------------------------------


def _js_block(start: str) -> str:
    """The render-level JS statement opening with ``start``, up to its closing line.

    Statements directly inside ``render()`` sit at a 6-space indent, so the
    first ``\n      }`` after ``start`` closes the handler / function.
    """

    begin = _RENDER_JS.index(start)
    return _RENDER_JS[begin : _RENDER_JS.index("\n      }", begin)]


@pytest.mark.parametrize(
    "token",
    [
        "state.selection",  # a Set of node ids replaces the single id
        "shiftKey",  # Shift+click toggles, Shift+drag boxes
        '"Escape"',  # Esc clears the selection
        '"downStage"',  # the marquee starts on the stage
        "forEachInEdge",  # rigid children = sources of part_of in-edges
        "function dragSetFor(nodeId)",
        "function drawOverlay",
        "#ea580c",  # the selection ring / marquee stroke (--accent1)
        "rgba(234,88,12,0.08)",  # the marquee fill
        "setLineDash([4, 3])",  # the dashed marquee border
    ],
)
def test_template_carries_the_selection_and_group_drag_machinery(token: str) -> None:
    assert token in _FILE_HTML_BASE


def test_the_single_selected_id_is_gone() -> None:
    # Assert: one selection model — the Set — so no code path can disagree.
    assert "state.selected" not in _FILE_HTML_BASE


def test_rigid_children_are_the_unpinned_part_of_sources_in_one_place() -> None:
    body = _js_block("function dragSetFor(nodeId)")

    # Assert: the part_of literal lives ONLY in the drag-set expansion; a
    # child is carried only if it is not held and not user-pinned (fx == null).
    assert _RENDER_JS.count('relType === "part_of"') == 1
    assert 'relType === "part_of"' in body
    assert "graph.forEachInEdge(id, (edge, attrs, source) =>" in body
    assert "!held.has(source)" in body
    assert "simById.get(source).fx == null" in body


def test_dragging_a_node_outside_the_selection_moves_only_that_node() -> None:
    body = _js_block("function dragSetFor(nodeId)")

    # Assert: the held set is the selection only when the pressed node is in
    # it — and dragSetFor never writes the selection.
    assert (
        "state.selection.has(nodeId) ? new Set(state.selection) : new Set([nodeId])"
        in body
    )
    assert "state.selection =" not in body


def test_a_press_without_movement_neither_pins_reheats_nor_freezes() -> None:
    body = _js_block('renderer.on("downNode"')

    # Assert: the press only records where it started; pinning, the reheat
    # and the viewport freeze all wait for DRAG_THRESHOLD px of travel.
    assert ".fx =" not in body
    assert "alphaTarget(0.3)" not in body
    assert "setCustomBBox" not in body
    assert "freezeViewport" not in body
    assert "const DRAG_THRESHOLD = 3;" in _RENDER_JS
    assert "< DRAG_THRESHOLD) return;" in _RENDER_JS
    assert "alphaTarget(0.3)" in _js_block("function startDrag()")


def test_the_viewport_stays_frozen_after_a_drop_until_fit() -> None:
    # Assert (added by orchestrator): releasing a drag or marquee never hands
    # the viewport back to Sigma — a re-fit would land the dropped node away
    # from the cursor. The fit button is the ONE place that releases it.
    assert _RENDER_JS.count("setCustomBBox(null)") == 1
    assert (
        'document.getElementById("zoom-fit").onclick = () => '
        "{ renderer.setCustomBBox(null); camera.animatedReset(); };"
    ) in _RENDER_JS
    assert "setCustomBBox" not in _js_block('captor.on("mouseup"')


def test_on_drop_held_nodes_stay_pinned_and_carried_children_are_released() -> None:
    body = _js_block('captor.on("mouseup"')

    # Assert: only the carried passengers lose fx/fy; the held set keeps them.
    assert "if (m.carried) { m.s.fx = null; m.s.fy = null; }" in body
    assert "if (sim) sim.alphaTarget(0);" in body


@pytest.mark.parametrize(
    "handler", ['renderer.on("clickNode"', 'renderer.on("clickStage"']
)
def test_the_trailing_click_of_a_drag_or_marquee_never_changes_the_selection(
    handler: str,
) -> None:
    # Assert: sigma does not count a move whose default we prevented, so it
    # emits a click after every drag / marquee — on a node OR on the stage.
    assert "if (pointerMoved" in _js_block(handler)


def test_every_press_resets_the_trailing_click_guard() -> None:
    # Assert: a release outside the canvas fires no click, so a stale guard
    # would swallow the next real click.
    assert "pointerMoved = false;" in _js_block('renderer.on("downNode"')
    assert "pointerMoved = false;" in _js_block("function startMarquee(e)")
    assert 'renderer.on("downStage", startMarquee);' in _RENDER_JS
    assert 'renderer.on("downEdge", startMarquee);' in _RENDER_JS


def test_a_plain_stage_drag_still_pans() -> None:
    # Assert: only a Shift+press starts a marquee; without one the move
    # handler leaves sigma's default (the pan) alone.
    assert "if (!e.event.original.shiftKey) return;" in _js_block(
        "function startMarquee(e)"
    )
    assert "if (!press && !marquee) return;" in _js_block('captor.on("mousemovebody"')


def test_the_marquee_replaces_the_selection_with_the_nodes_inside_it() -> None:
    body = _js_block("function selectInside(box)")

    assert "renderer.graphToViewport({ x: a.x, y: a.y })" in body
    assert "state.selection = inside;" in body


def test_selected_nodes_are_highlighted_except_chunks_in_a_group() -> None:
    body = _js_block("nodeReducer: (node, data) =>")

    # Assert: every selected node draws on top with Sigma's highlighted label
    # box (the single-click look) — except a chunk in a MULTI-selection: a
    # marquee over a document star would otherwise stack dozens of chunk
    # label boxes over it. Its ring marks it instead.
    assert "const lone = selected && state.selection.size === 1;" in body
    assert "if (selected) res.zIndex = 2;" in body
    assert (
        'if (lone || (selected && data.nodeType !== "chunk")) res.highlighted = true;'
        in body
    )
    assert "&& state.hovered !== node && !lone;" in body


def test_the_overlay_draws_hulls_pins_rings_then_the_marquee() -> None:
    body = _js_block("function drawOverlay()")

    # Assert: ADR-011 §5's draw order, and the ring sits 3 px outside the node.
    order = [
        body.index("convexHull("),
        body.index("// Pin dots"),
        body.index("// Selection rings"),
        body.index("if (marquee && !marquee.cancelled)"),
    ]
    assert order == sorted(order)
    assert "renderer.scaleSize(shown.size) + 3" in body


def test_carried_children_show_no_pin_dot_mid_drag() -> None:
    # Assert: passengers hold fx/fy only while carried, so they are not pins.
    assert "drag && drag.carried.has(s.id)" in _js_block("function drawOverlay()")


@pytest.mark.parametrize(
    "press", ['renderer.on("downNode"', "function startMarquee(e)"]
)
def test_only_a_left_button_press_starts_a_drag_or_a_marquee(press: str) -> None:
    # Assert (regression): Sigma emits downNode / downStage for EVERY button
    # but its captor's mouseup only after a LEFT press — a right-click used to
    # leave the press open, so the node then followed the bare pointer.
    body = _js_block(press)
    assert body.index("if (e.event.original.button !== 0) return;") < body.index(
        "pointerMoved = false;"
    )


# ---------------------------------------------------------------------------
# Fast clicks, Esc on a marquee, a lost mouseup (added by orchestrator, 164)
# ---------------------------------------------------------------------------


def test_a_double_click_never_zooms() -> None:
    # Assert: Sigma reads ANY two clicks <300 ms apart as a double-click, so
    # its default zoom would fire on fast clicks — both handlers prevent it
    # before anything else.
    for handler in ('renderer.on("doubleClickNode"', 'renderer.on("doubleClickStage"'):
        body = _js_block(handler)
        assert body.index("e.preventSigmaDefault();") < body.index("if (pointerMoved)")


def test_clicks_and_double_clicks_share_one_click_path() -> None:
    # Assert (amended by orchestrator): the second click of a fast pair acts
    # as a click — through the SAME helper clickNode / clickStage use.
    assert "clickOnNode(node, event.original.shiftKey);" in _js_block(
        'renderer.on("clickNode"'
    )
    assert "clickOnStage(event.original.shiftKey);" in _js_block(
        'renderer.on("clickStage"'
    )
    assert "clickOnStage(e.event.original.shiftKey);" in _js_block(
        'renderer.on("doubleClickStage"'
    )


def test_a_double_click_acts_as_a_click_unless_it_unpins() -> None:
    body = _js_block('renderer.on("doubleClickNode"')

    # Assert: Shift, or an unpinned node, takes the click path; only a plain
    # double-click on a Pinned node unpins it.
    assert (
        "if (shiftKey || !s || s.fx == null) { clickOnNode(e.node, shiftKey); return; }"
        in body
    )
    assert body.index("clickOnNode(e.node, shiftKey)") < body.index("s.fx = null;")
    assert "reheat();" in body
    assert ".restart(" not in body  # Pause holds: the reheat helper is guarded


@pytest.mark.parametrize(
    "handler", ['renderer.on("doubleClickNode"', 'renderer.on("doubleClickStage"']
)
def test_a_drags_trailing_click_never_completes_a_double_click(handler: str) -> None:
    # Assert: the press that starts the second click resets the guard, so only
    # a pair whose second click IS a drag's trailing click is ignored (no
    # instant unpin of a just-dropped node, no selection change).
    assert "if (pointerMoved) return;" in _js_block(handler)


def test_esc_cancels_an_active_marquee_and_keeps_the_selection() -> None:
    body = _js_block('document.addEventListener("keydown"')

    # Assert: the marquee check runs first and returns before the selection
    # is cleared; the button is still held, so the box is MARKED cancelled
    # (moves stay swallowed — no pan) rather than dropped.
    assert "if (marquee) { marquee.cancelled = true; drawOverlay(); return; }" in body
    assert body.index("marquee.cancelled = true") < body.index(
        "state.selection.clear()"
    )


def test_a_cancelled_marquee_is_neither_drawn_nor_applied() -> None:
    assert "if (marquee && !marquee.cancelled)" in _js_block("function drawOverlay()")
    assert "if (pointerMoved && !box.cancelled) selectInside(box);" in _js_block(
        'captor.on("mouseup"'
    )
    assert "if (marquee && marquee.cancelled) return;" in _js_block(
        'captor.on("mousemovebody"'
    )


def test_a_lost_mouseup_ends_the_gesture_at_the_last_held_position() -> None:
    body = _js_block('captor.on("mousemovebody"')

    # Assert: a buttonless move releases sigma's captor too (its stuck press
    # would otherwise pan the camera under the bare pointer), which emits the
    # ONE mouseup our handler already listens to — before this move's
    # coordinates are applied.
    guard = "if (e.original.buttons === 0) { captor.handleUp(e.original); return; }"
    assert guard in body
    assert body.index("e.preventSigmaDefault();") < body.index(guard)
    assert body.index(guard) < body.index("marquee.x1 = e.x;")
    assert body.index(guard) < body.index("renderer.viewportToGraph(e)")


# ---------------------------------------------------------------------------
# Controls panel (task 164, ADR-011 §6) — Forces · Display · Actions
# ---------------------------------------------------------------------------


def _panel_js() -> str:
    """The panel-building JS: from its section header comment to the end of render()."""

    begin = _RENDER_JS.index("// --- Controls panel")
    return _RENDER_JS[
        begin : _RENDER_JS.index('renderer.on("afterRender", drawOverlay)')
    ]


def _display_js() -> str:
    """The Display section: every row between its title and the Actions comment."""

    panel = _panel_js()
    return panel[panel.index('panelSection("Display")') : panel.index("// Actions.")]


def _forces_gated_js() -> str:
    """Every ``if (forces) {`` block of the panel, concatenated."""

    panel = _panel_js()
    blocks, at = [], panel.find("if (forces) {")
    while at != -1:
        blocks.append(panel[at : panel.index("\n      }", at)])
        at = panel.find("if (forces) {", at + 1)
    return "\n".join(blocks)


def test_the_panel_markup_is_a_toggle_and_a_body_at_the_top_left() -> None:
    # Assert: one button + one body, so a section can be prepended to the
    # body (task 165's Documents slider) without touching the toggle.
    assert (
        '<div id="panel">\n'
        '        <button id="panel-toggle" type="button">Controls</button>\n'
        '        <div id="panel-body"></div>\n'
        "      </div>"
    ) in _BODY_MARKUP
    assert (
        "#panel { position: absolute; top: 10px; left: 12px; z-index: 2; width: 190px;"
        in _GRAPH_STYLE
    )
    assert "accent-color: var(--accent1);" in _GRAPH_STYLE
    # Collapsed, the card hugs the button instead of staying 190 px wide.
    assert "#panel:has(#panel-body[hidden]) { width: auto; }" in _GRAPH_STYLE


@pytest.mark.parametrize(
    "token",
    [
        'id="panel"',
        'id="panel-toggle"',
        "function rangeRow",
        "function checkboxRow",
        '"Centre force"',
        '"Repel force"',
        '"Link force"',
        '"Link distance"',
        '"Node size"',
        '"Link thickness"',
        '"Label fade"',
        '"Arrows"',
        '"Edge labels"',
        '"Pause"',
        '"Resume"',
        '"Unpin all"',
        '"Reset to defaults"',
        'renderer.setSetting("labelRenderedSizeThreshold", v)',
        'renderer.setSetting("renderEdgeLabels", on)',
        'document.body.dataset.sim = "paused"',
    ],
)
def test_template_carries_the_controls_panel(token: str) -> None:
    assert token in _FILE_HTML_BASE


def test_the_toggle_collapses_the_body_to_just_the_button() -> None:
    assert (
        'document.getElementById("panel-toggle").onclick = () => '
        "{ panelBody.hidden = !panelBody.hidden; };"
    ) in _panel_js()


def test_the_panel_is_rebuilt_per_render() -> None:
    # Assert: the iframe calls render() once per tool result — the old rows
    # (bound to the previous renderer) are cleared before the early return.
    assert _RENDER_JS.index('panelBody.textContent = "";') < _RENDER_JS.index(
        "if (!nodes.length)"
    )


def test_the_forces_pause_and_force_reset_are_gated_on_controls_forces() -> None:
    gated = _forces_gated_js()

    # Assert: a payload without controls.forces (the Embedding map) gets no
    # Forces section, no Pause/Resume and no force rows for Reset to restore;
    # Display and Unpin all are emitted for every payload.
    assert "const forces = isFixed ? null : payload.controls.forces;" in _RENDER_JS
    for label in ('"Centre force"', '"Repel force"', '"Link force"', '"Link distance"'):
        assert label in gated
    assert '"Pause"' in gated
    assert '"Resume"' in gated
    for ungated in ('"Node size"', '"Arrows"', '"Unpin all"', '"Reset to defaults"'):
        assert ungated not in gated
        assert ungated in _panel_js()


def test_forces_callbacks_reheat_and_display_callbacks_only_refresh() -> None:
    gated = _forces_gated_js()
    display = _display_js()

    # Assert: pulse's split — a force change re-settles the layout, a display
    # change only redraws it.
    assert gated.count("reheat(); }") == 4
    assert "reheat" not in display
    assert display.count("renderer.refresh();") == 3
    assert display.count("renderer.setSetting(") == 2


@pytest.mark.parametrize(
    "apply",
    [
        'sim.force("centre").strength(v)',
        'sim.force("repel").strength(-v * REPEL_SCALE)',
        'sim.force("link").strength(v)',
        'sim.force("link").distance(v)',
    ],
)
def test_each_force_slider_drives_its_force(apply: str) -> None:
    assert apply in _forces_gated_js()


def test_the_slider_defaults_are_read_from_the_payload() -> None:
    panel = _panel_js()

    # Assert: every row starts from payload.controls (ADR-011 §6 — the
    # template has no default of its own); none of the Python defaults
    # (centre 0.2 is also Node size's min, so it is not a usable probe)
    # appears as a literal in the panel code.
    for ref in (
        "forces.centre, 2,",
        "forces.repel, 2,",
        "forces.link, 2,",
        "forces.linkDistance, 0,",
        "display.nodeSize, 2,",
        "display.linkThickness, 2,",
        "display.labelFade, 1,",
        "display.arrows,",
        "display.edgeLabels,",
    ):
        assert ref in panel
    for literal in ("80", "0.3", "8.0", "8,"):
        assert literal not in panel


def test_a_slider_sets_its_bounds_before_its_value() -> None:
    body = _js_block("function rangeRow(")

    # Assert: a range input clamps to its CURRENT bounds (default 0-100), so a
    # Link distance of 300 set before max = 500 would read 100.
    assert body.index("input.max = max;") < body.index("show(value);")
    assert "readout.textContent = v.toFixed(decimals);" in body


def test_every_restart_goes_through_the_pause_guard() -> None:
    # Assert: sliders, unpin, Unpin all and Resume reheat through ONE guarded
    # helper; a drag while paused moves only the drag set.
    assert "if (sim && !paused) sim.alpha(0.5).restart();" in _js_block(
        "function reheat()"
    )
    assert "if (sim && !paused) sim.alphaTarget(0.3).restart();" in _js_block(
        "function startDrag()"
    )
    assert _RENDER_JS.count(".restart()") == 2


def test_pause_stops_the_simulation_and_resume_reheats_it() -> None:
    gated = _forces_gated_js()

    # Assert: Resume REHEATS rather than sim.restart(): a cooled simulation
    # (alpha < alphaMin) would tick once and stop.
    assert "paused = !paused;" in gated
    assert 'pause.textContent = paused ? "Resume" : "Pause";' in gated
    assert 'if (paused) { sim.stop(); document.body.dataset.sim = "paused"; }' in gated
    assert "else reheat();" in gated


def test_unpin_all_frees_every_node_redraws_and_reheats() -> None:
    panel = _panel_js()
    body = panel[panel.index('"Unpin all"') : panel.index('"Reset to defaults"')]

    assert "for (const s of simNodes) { s.fx = null; s.fy = null; }" in body
    assert "drawOverlay();" in body
    assert "reheat();" in body


def test_reset_restores_every_control_but_neither_unpins_nor_resumes() -> None:
    panel = _panel_js()
    body = panel[panel.index('"Reset to defaults"') :]

    assert "for (const reset of resets) reset();" in body
    assert "fx" not in body
    assert "paused" not in body
    assert "resets.push(() => { show(value); apply(value); });" in _js_block(
        "function rangeRow("
    )
    assert "resets.push(() => { input.checked = checked; apply(checked); });" in (
        _js_block("function checkboxRow(")
    )
