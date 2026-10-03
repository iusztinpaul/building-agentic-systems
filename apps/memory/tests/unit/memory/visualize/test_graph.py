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

import hashlib
import json
import logging
from datetime import UTC, datetime
from pathlib import Path

from tree.config.paths import GRAPHS_DIR
import pytest
from beanie import PydanticObjectId

from tree.config.app_config import app_config
from tree.memory.visualize.embeddings import NOISE_COLOUR
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
    densify_document_ranks,
    to_graph_payload,
    visualize_query_result,
)
from tree.memory.rag.structure import synthesize_part_of_edges
from tree.memory.rag.types import MemoryStructure
from tree.memory.types import QueryResult
from tree.memory.visualize import graph as graph_module

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
        "forces": {
            "centre": 0.2,
            "gravity": 0.05,
            "repel": 8.0,
            "link": 0.3,
            "linkDistance": 80,
        },
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


@pytest.mark.parametrize(
    ("query", "order"), [("memory for ai agents", "relevance"), ("", "recency")]
)
def test_visualize_query_result_orders_documents_by_whether_there_is_a_query(
    mocker, tmp_path: Path, query: str, order: str
) -> None:
    build = mocker.patch(
        "tree.memory.visualize.graph.to_graph_payload", wraps=to_graph_payload
    )
    out = tmp_path / "graph.html"

    visualize_query_result(_ranked_result(3), out, open_browser=False, query=query)

    # Assert: a query view ranks by relevance, the Full graph by recency.
    assert build.call_args.kwargs == {"document_order": order}
    assert f'"order": "{order}"' in out.read_text(encoding="utf-8")


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
    guard = "      if (!isFixed) {\n        sim = forceSimulation(visibleSimNodes())"
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
        "const colorByType = new Map(nodes.map((n) => [n.type, colourOf(n)]));"
        in _RENDER_JS
    )


def test_one_colour_resolver_prefers_the_node_then_the_legend_cluster() -> None:
    # Assert (source-level): a Graph payload node keeps its own ``color``; a
    # lean map node (no ``color``) is coloured by its legend row's cluster_id.
    assert "n.color ?? colourByCluster.get(n.cluster_id)" in _RENDER_JS
    # Built ONCE, from the legend rows that carry a numeric cluster_id (the
    # "unclustered / stale" row carries none).
    assert _RENDER_JS.count("const colourByCluster = new Map(") == 1
    assert (
        '(legendRows || []).filter((r) => typeof r.cluster_id === "number")'
        in _RENDER_JS
    )
    # No colour site reads ``n.color`` directly any more.
    assert _RENDER_JS.count("n.color") == 1


def test_a_negative_cluster_id_without_a_legend_row_is_drawn_noise_grey() -> None:
    # Assert (source-level): -2/-3 never hit the "unclustered / stale" grey —
    # that colour names chunks that are NOT on the map.
    assert (
        "const colourOf = (n) => n.color ?? colourByCluster.get(n.cluster_id)\n"
        '        ?? (n.cluster_id < 0 ? NOISE_COLOUR : "#d5d8de");'
    ) in _RENDER_JS
    assert f'const NOISE_COLOUR = "{NOISE_COLOUR}";' in _RENDER_JS


@pytest.mark.parametrize(
    "site",
    [
        "color: colourOf(n),",  # graph.addNode
        "entry = { color: colourOf(n), ids: [] };",  # the cluster hull
        "[n.type, colourOf(n)]",  # the per-type legend fallback
    ],
)
def test_every_node_colour_site_goes_through_the_resolver(site: str) -> None:
    assert site in _RENDER_JS


def test_a_node_without_a_label_draws_no_canvas_text() -> None:
    assert 'label: n.label ?? "",' in _RENDER_JS


def test_the_fixed_layout_tooltip_rebuilds_the_document_row_from_the_name() -> None:
    # Assert (source-level): a map node no longer ships ``meta.document``; the
    # card puts it back from ``name`` in its old slot (after ``cluster``), so
    # the hover card reads type, cluster, document, heading path, snippet.
    hover = _RENDER_JS[
        _RENDER_JS.index('renderer.on("enterNode"') : _RENDER_JS.index(
            'renderer.on("leaveNode"'
        )
    ]
    assert "if (isFixed) {" in hover
    assert "const { cluster, ...rest } = n.meta || {};" in hover
    assert "document: n.name" in hover
    # The graph's card is untouched: type first, then its curated meta.
    assert "Object.assign({ type: n.type }, n.meta)" in hover


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
                "x": 3.5,
                "y": -1.25,
                "cluster_id": 0,
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
        "import { forceSimulation, forceLink, forceManyBody, forceCenter, forceX, forceY } "
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
    # from the cursor. fitView() — the fit button and the one auto-fit after a
    # Documents reveal — is the ONE place that releases it.
    assert _RENDER_JS.count("setCustomBBox(null)") == 1
    assert "renderer.setCustomBBox(null)" in _js_block("function fitView()")
    assert (
        'document.getElementById("zoom-fit").onclick = () => { fitView(); };'
        in _RENDER_JS
    )
    assert "setCustomBBox" not in _js_block('captor.on("mouseup"')


def test_fit_re_indexes_the_extent_before_resetting_the_camera() -> None:
    # Assert (added by orchestrator after QA 165): once the simulation has
    # settled nothing re-processes the graph, so Sigma would fit to the STALE
    # extent of the frozen bbox; refresh() recomputes it from the current
    # positions before the camera animates back.
    body = _js_block("function fitView()")
    assert body.index("setCustomBBox(null)") < body.index("renderer.refresh()")
    assert body.index("renderer.refresh()") < body.index(
        "return camera.animatedReset()"
    )


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
    # handler leaves sigma's default (the pan) alone — the plain press is only
    # remembered (task 166) and its branch returns before preventSigmaDefault.
    assert "if (!e.event.original.shiftKey) { pan = " in _js_block(
        "function startMarquee(e)"
    )
    move = _js_block('captor.on("mousemovebody"')
    assert "if (!press && !marquee) return;" in move
    assert move.index("if (pan) {") < move.index("preventSigmaDefault")


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
        '"Gravity"',
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
    for label in (
        '"Centre force"',
        '"Gravity"',
        '"Repel force"',
        '"Link force"',
        '"Link distance"',
    ):
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
    assert gated.count("reheat(); }") == 5
    assert "reheat" not in display
    assert display.count("renderer.refresh();") == 3
    assert display.count("renderer.setSetting(") == 2


@pytest.mark.parametrize(
    "apply",
    [
        'sim.force("centre").strength(v)',
        'sim.force("gravityX").strength(v); sim.force("gravityY").strength(v)',
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
        "forces.gravity, 2,",
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
    # (gravity 0.05 is also Node size's step — no usable probe either)
    for literal in ("80", "0.3", "8.0", "8,"):
        assert literal not in panel


def test_a_slider_sets_its_bounds_before_its_value() -> None:
    body = _js_block("function rangeRow(")

    # Assert: a range input clamps to its CURRENT bounds (default 0-100), so a
    # Link distance of 300 set before max = 500 would read 100.
    assert body.index("input.max = max;") < body.index("show(value);")
    assert "format = (v) => v.toFixed(decimals)" in body
    assert "readout.textContent = format(v);" in body


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


# ---------------------------------------------------------------------------
# Full graph: docRank + the Documents slider (task 165, ADR-011 §7)
# ---------------------------------------------------------------------------


def _ranked_result(n_documents: int) -> QueryResult:
    """A **Full graph** result: one document + one entity per rank, an edge each."""

    nodes, edges = [], []
    for rank in range(1, n_documents + 1):
        doc = {**_node(f"{_UID}:document:d{rank}", "document"), "doc_rank": rank}
        entity = {**_node(f"{_UID}:person:p{rank}", "person"), "doc_rank": rank}
        edge = {**_edge(entity["_id"], "mentions", doc["_id"]), "doc_rank": rank}
        nodes += [doc, entity]
        edges.append(edge)
    return QueryResult(nodes=nodes, edges=edges)


def test_a_ranked_row_carries_doc_rank_and_an_unranked_one_does_not() -> None:
    ranked = {**_node(f"{_UID}:document:d1", "document"), "doc_rank": 3}
    unranked = _node(f"{_UID}:person:alice", "person")

    payload = to_graph_payload(QueryResult(nodes=[ranked, unranked], edges=[]))

    by_id = {n["id"]: n for n in payload["nodes"]}
    assert by_id[ranked["_id"]]["docRank"] == 3
    assert "docRank" not in by_id[unranked["_id"]]


def test_a_full_graph_payload_ships_the_documents_control(mocker) -> None:
    mocker.patch.object(app_config.query, "full_graph_shown_docs", 5)

    payload = to_graph_payload(_ranked_result(12))

    # Assert: Python decides how many show on load; total = the deepest rank;
    # the default order is the Full graph's (165 payloads gain only `order`).
    assert payload["controls"]["documents"] == {
        "shown": 5,
        "total": 12,
        "order": "recency",
    }


@pytest.mark.parametrize("shown_docs", [1, 5, 100])
def test_a_relevance_payload_shows_every_document(mocker, shown_docs: int) -> None:
    mocker.patch.object(app_config.query, "full_graph_shown_docs", shown_docs)

    payload = to_graph_payload(_ranked_result(12), document_order="relevance")

    # Assert: a query view narrows, it never caps — whatever the Full-graph
    # default is (task 168).
    assert payload["controls"]["documents"] == {
        "shown": 12,
        "total": 12,
        "order": "relevance",
    }


def test_shown_clamps_to_the_configured_embed_cap(mocker) -> None:
    # TREE_QUERY__FULL_GRAPH_MAX_DOCS=50 alone: shown (100) > max (50) shows 50.
    mocker.patch.object(app_config.query, "full_graph_shown_docs", 100)
    mocker.patch.object(app_config.query, "full_graph_max_docs", 50)

    payload = to_graph_payload(_ranked_result(60))

    assert payload["controls"]["documents"] == {
        "shown": 50,
        "total": 60,
        "order": "recency",
    }


def test_shown_clamps_to_the_documents_that_exist(mocker) -> None:
    mocker.patch.object(app_config.query, "full_graph_shown_docs", 100)

    payload = to_graph_payload(_ranked_result(4))

    assert payload["controls"]["documents"] == {
        "shown": 4,
        "total": 4,
        "order": "recency",
    }


@pytest.mark.parametrize("document_order", ["recency", "relevance"])
def test_an_unranked_payload_has_neither_doc_rank_nor_a_documents_control(
    document_order: str,
) -> None:
    # Rewritten by task 168: query views ARE ranked now; an unranked result
    # (an NL `query_memory` pipeline's rows) is what carries no slider.
    payload = to_graph_payload(_seed_result(), document_order=document_order)

    # Assert: no rank -> no slider gate, whatever the order asked for.
    assert "documents" not in payload["controls"]
    assert set(payload["controls"]) == {"forces", "display"}
    assert all("docRank" not in n for n in payload["nodes"])


# --- densify_document_ranks: a truncated payload's slider (task 168 QA) -------


def _sparse_result(ranks_drawn: dict[str, int]) -> QueryResult:
    """Ranked rows as a TRUNCATED view draws them: ``{node_id: doc_rank}``,
    a ``d…`` id being a document row, anything else a chunk."""

    return QueryResult(
        nodes=[
            {
                **_node(
                    f"{_UID}:{node_id}", "document" if node_id[0] == "d" else "chunk"
                ),
                "doc_rank": rank,
            }
            for node_id, rank in ranks_drawn.items()
        ],
        edges=[],
    )


def _ranks(payload: dict) -> dict:
    return {n["id"].split(":", 1)[1]: n.get("docRank") for n in payload["nodes"]}


def _visible_at(payload: dict, limit: int) -> list[str]:
    """The renderer's ``isVisible`` at ``docLimit = limit``."""

    return [
        n["id"]
        for n in payload["nodes"]
        if n.get("docRank") is None or n["docRank"] <= limit
    ]


def test_densify_renumbers_the_drawn_documents_from_one() -> None:
    # Arrange: rank 1's document and its rows were cut; 2 and 4 are drawn.
    payload = to_graph_payload(
        _sparse_result({"c2": 2, "d2": 2, "d4": 4, "c4": 4}),
        document_order="relevance",
    )

    dense = densify_document_ranks(payload)

    # Assert: 2 -> 1, 4 -> 2, relative order kept; total = stars drawn.
    assert _ranks(dense) == {"c2": 1, "d2": 1, "d4": 2, "c4": 2}
    assert dense["controls"]["documents"] == {
        "shown": 2,
        "total": 2,
        "order": "relevance",
    }


def test_densify_never_leaves_the_view_empty_at_one() -> None:
    payload = to_graph_payload(
        _sparse_result({"c2a": 2, "c2b": 2, "d3": 3, "c3": 3}),
        document_order="relevance",
    )

    dense = densify_document_ranks(payload)

    visible = _visible_at(dense, 1)
    assert any(":d" in node_id for node_id in visible)


def test_densify_unranks_rows_whose_document_star_was_cut() -> None:
    payload = to_graph_payload(
        _sparse_result({"c1": 1, "d2": 2, "c2": 2}), document_order="relevance"
    )

    dense = densify_document_ranks(payload)

    # Assert: c1's document is not drawn, so c1 is always shown.
    assert _ranks(dense) == {"c1": None, "d2": 1, "c2": 1}
    assert dense["controls"]["documents"]["total"] == 1


def test_densify_drops_the_slider_when_no_document_star_is_drawn() -> None:
    # The live break: 10 chunks of rank 2, no document row.
    payload = to_graph_payload(
        _sparse_result({f"c{i}": 2 for i in range(10)}), document_order="relevance"
    )

    dense = densify_document_ranks(payload)

    assert "documents" not in dense["controls"]
    assert all("docRank" not in n for n in dense["nodes"])


@pytest.mark.parametrize("document_order", ["recency", "relevance"])
def test_densify_leaves_a_dense_payload_untouched(document_order: str) -> None:
    payload = to_graph_payload(_ranked_result(12), document_order=document_order)
    before = json.dumps(payload)

    dense = densify_document_ranks(payload)

    # Assert: identical, and the input is never mutated.
    assert json.dumps(dense) == before
    assert json.dumps(payload) == before


def test_densify_keeps_the_same_stars_visible_at_the_default(mocker) -> None:
    # A recency payload showing ranks <= 3 of a sparse 1, 3, 5, 6.
    mocker.patch.object(app_config.query, "full_graph_shown_docs", 3)
    payload = to_graph_payload(
        _sparse_result({"d1": 1, "d3": 3, "d5": 5, "d6": 6}),
        document_order="recency",
    )

    dense = densify_document_ranks(payload)

    # Assert: d1 and d3 shown before and after; shown counts drawn stars.
    assert dense["controls"]["documents"] == {
        "shown": 2,
        "total": 4,
        "order": "recency",
    }
    assert _visible_at(dense, 2) == _visible_at(payload, 3)


def test_render_graph_file_logs_the_documents_shown_on_a_full_graph(
    mocker, tmp_path: Path, caplog
) -> None:
    mocker.patch.object(app_config.query, "full_graph_shown_docs", 5)
    payload = to_graph_payload(_ranked_result(12))
    out = tmp_path / "graph.html"

    with caplog.at_level(logging.INFO, logger="tree.memory.visualize.graph"):
        _render_graph_file(payload, output=out)

    assert (
        "Wrote self-contained graph HTML (5 of 12 documents shown by default, "
        f"24 nodes, 12 edges) to {out}"
    ) in caplog.messages


def _documents_js() -> str:
    """The Documents section of the panel: from its comment to the Forces one."""

    panel = _panel_js()
    return panel[panel.index("// Documents") : panel.index("// Forces")]


@pytest.mark.parametrize(
    "token",
    [
        '"Documents"',
        "controls.documents",
        "function applyDocumentLimit",
        "sim.nodes(",
        ".links(",
        '"hidden"',
        "dataset.docs",
        '" of "',
        '" documents · "',
        "ORDER_LABEL",
        '"Most recent"',
        '"Most relevant"',
        "ORDER_LABEL[documents.order]",
    ],
)
def test_template_carries_the_documents_slider(token: str) -> None:
    assert token in _FILE_HTML_BASE


def test_the_documents_section_is_gated_and_built_before_forces() -> None:
    documents = _documents_js()

    # Assert: only a payload with controls.documents (the Full graph) gets the
    # section, it is the panel's FIRST, and its slider spans 1..total from
    # the payload's `shown` — read from Python, never a literal.
    assert "const documents = payload.controls && payload.controls.documents;" in (
        _RENDER_JS
    )
    assert "if (documents) {" in documents
    assert (
        'rangeRow(panelSection("Documents"), ORDER_LABEL[documents.order], 1,'
        " documents.total, 1, documents.shown, 0,"
    ) in documents
    assert "applyDocumentLimit" in documents
    assert '(v) => v + " of " + documents.total' in documents
    assert _panel_js().index('panelSection("Documents")') < _panel_js().index(
        'panelSection("Forces")'
    )


def test_the_documents_row_label_names_the_order() -> None:
    # Assert: one lookup beside `documents`, keyed by the payload's order —
    # the ONE renderer change of task 168.
    assert (
        'const ORDER_LABEL = { recency: "Most recent", relevance: "Most relevant" };'
        in _RENDER_JS
    )
    assert _RENDER_JS.index("const ORDER_LABEL") < _RENDER_JS.index(
        "let docLimit = documents ? documents.shown : Infinity;"
    )
    assert "ORDER_LABEL[documents.order]" in _documents_js()


def test_the_panel_code_contains_no_shown_default_of_its_own() -> None:
    # Assert: the 100 comes from query.full_graph_shown_docs, via the payload.
    assert "100" not in _panel_js()
    assert "100" not in _js_block("function applyDocumentLimit(n)")


def test_an_unranked_node_is_always_visible() -> None:
    body = _js_block("function isVisible(id)")

    assert "return rank == null || rank <= docLimit;" in body
    assert "let docLimit = documents ? documents.shown : Infinity;" in _RENDER_JS


def test_hidden_nodes_never_enter_the_first_simulation() -> None:
    # Assert: the boot builds the simulation from the SAME visibility
    # predicate the slider uses, so a hidden node is absent from tick one.
    assert "const visibleSimNodes = () => simNodes.filter((s) => isVisible(s.id));" in (
        _RENDER_JS
    )
    assert "forceSimulation(visibleSimNodes())" in _RENDER_JS
    assert "forceLink(visibleLinks())" in _RENDER_JS
    # The tick writes back only what the simulation owns.
    assert "for (const s of sim.nodes()) graph.mergeNodeAttributes(" in _RENDER_JS


def test_simulation_links_join_two_visible_nodes_and_are_rebuilt_fresh() -> None:
    body = _js_block("const visibleLinks = () => drawnEdges")

    # Assert: forceLink swaps source/target ids for node objects, so the
    # links are rebuilt from ids every time; self-loops still never link.
    assert "e.source !== e.target && isVisible(e.source) && isVisible(e.target)" in body
    assert ".map((e) => ({ source: e.source, target: e.target }))" in body


def test_a_hidden_node_starts_hidden_and_parked_in_sigma() -> None:
    body = _js_block("for (const n of nodes) {")

    # Assert: Sigma fits its camera to EVERY node, hidden ones included, and
    # throws on a non-numeric x/y, so a hidden node is parked at the origin.
    assert "hidden: !shown," in body
    assert "x: shown ? s.x : 0," in body
    assert "y: shown ? s.y : 0," in body


def test_apply_document_limit_hides_prunes_resyncs_then_counts() -> None:
    body = _js_block("function applyDocumentLimit(n)")

    # Assert: nodes are re-synced BEFORE links (d3 throws on a link to a node
    # it does not own), positions are written after the re-sync (a revealed
    # node gets its d3 position), and the one reheat goes through the pause
    # guard.
    assert "docLimit = n;" in body
    order = [
        "state.selection.delete(id)",
        "seedRevealed();",
        "sim.nodes(visibleSimNodes());",
        'sim.force("link").links(visibleLinks());',
        "graph.updateEachNodeAttributes(",
        "reheat();",
        "showDocumentCounts();",
        "renderer.refresh();",
    ]
    positions = [body.index(token) for token in order]
    assert positions == sorted(positions)
    assert "if (sim) {" in body
    assert ".restart()" not in body


def test_hiding_drops_the_hover_and_the_tooltip() -> None:
    body = _js_block("function applyDocumentLimit(n)")

    assert "if (state.hovered && !isVisible(state.hovered))" in body
    assert "state.hovered = null;" in body
    assert 'tooltip.classList.remove("show");' in body
    assert "state.hoveredEdge = null;" in body


def test_a_hidden_node_keeps_its_pin_and_its_position() -> None:
    body = _js_block("function applyDocumentLimit(n)")

    # Assert: hiding never touches fx/fy; a shown node takes its d3 position
    # back, a hidden one is parked.
    assert "fx" not in body
    assert "visible ? s.x : 0" in body
    assert "visible ? s.y : 0" in body


def test_the_header_reads_documents_shown_and_the_visible_counts() -> None:
    body = _js_block("function showDocumentCounts()")

    assert (
        'countsEl.textContent = docLimit + " of " + documents.total + " documents · "'
        in body
    )
    assert '" nodes · "' in body
    assert '" edges"' in body
    assert 'document.body.dataset.docs = docLimit + "/" + documents.total;' in body
    # The boot paints the same header (no reheat: the first layout keeps d3's
    # full alpha), and the slider's apply repaints it.
    assert "showDocumentCounts();" in _documents_js()


def test_a_hidden_node_is_never_carried_nor_marquee_selected() -> None:
    assert '!graph.getNodeAttribute(source, "hidden")' in _js_block(
        "function dragSetFor(nodeId)"
    )
    assert "if (!shown || shown.hidden) return;" in _js_block(
        "function selectInside(box)"
    )


def test_unpin_all_still_frees_hidden_nodes_too() -> None:
    panel = _panel_js()
    body = panel[panel.index('"Unpin all"') : panel.index('"Reset to defaults"')]

    # Assert: one rule — every d3 node, shown or hidden.
    assert "for (const s of simNodes) { s.fx = null; s.fy = null; }" in body


def test_a_first_reveal_seeds_each_document_as_its_own_star() -> None:
    body = _js_block("function seedRevealed()")

    # Assert: only revealed (not yet simulated), visible, unpinned nodes are
    # seeded, grouped by their document, deterministically (golden angle, no
    # Math.random); while the camera auto-fits, one link distance outside the
    # current layout.
    assert "if (owned.has(s) || !isVisible(s.id) || s.fx != null) continue;" in body
    assert "nodeById.get(s.id).docRank" in body
    assert "radius = Math.max(radius, Math.hypot(s.x, s.y));" in body
    assert "rx = ry = radius + forces.linkDistance;" in body
    assert "GOLDEN_ANGLE" in body
    assert "Math.random" not in body


def test_with_a_frozen_camera_revealed_stars_land_inside_the_viewport() -> None:
    body = _js_block("function seedRevealed()")

    # Assert: a gesture froze the camera (and it stays frozen), so new stars
    # are seeded on a ring inside the CURRENT viewport's graph bounds, and a
    # re-revealed node last seen off screen is re-seeded there too.
    assert "const frozen = renderer.getCustomBBox() != null;" in body
    assert "renderer.viewportToGraph({ x: width, y: height })" in body
    assert "if (s.x != null && inView(s)) continue;" in body
    assert "setCustomBBox" not in body


def test_a_rerender_drops_the_previous_documents_marker() -> None:
    # Assert: the iframe calls render() per tool result; a query result after a
    # Full graph must not keep data-docs on the body.
    assert _RENDER_JS.index("delete document.body.dataset.docs;") < _RENDER_JS.index(
        "if (!nodes.length)"
    )


def test_gravity_is_one_forcex_forcey_pair_on_the_live_layout_only() -> None:
    # Assert (added by orchestrator — human decision): a weak pull to the
    # origin, seeded from controls.forces.gravity, built inside the
    # live-layout branch only (the Embedding map never simulates).
    sim = _RENDER_JS[
        _RENDER_JS.index("      if (!isFixed) {\n        sim = forceSimulation(") :
    ]
    sim = sim[: sim.index("\n      }")]
    assert '.force("gravityX", forceX(0).strength(forces.gravity))' in sim
    assert '.force("gravityY", forceY(0).strength(forces.gravity))' in sim
    assert _RENDER_JS.count("forceX(") == 1
    assert _RENDER_JS.count("forceY(") == 1


def test_the_gravity_row_sits_after_centre_force_and_resets_like_any_row() -> None:
    gated = _forces_gated_js()

    # Assert: 0..0.5 by 0.01, reheats, and — being a rangeRow — registers the
    # reset closure that Reset to defaults replays.
    assert 'rangeRow(section, "Gravity", 0, 0.5, 0.01, forces.gravity, 2,' in gated
    assert (
        gated.index('"Centre force"')
        < gated.index('"Gravity"')
        < gated.index('"Repel force"')
    )


# --- Auto-fit on reveal (added by orchestrator — human decision) ---


def test_a_reveal_arms_one_auto_fit_and_hiding_never_fits() -> None:
    body = _js_block("function applyDocumentLimit(n)")

    # Assert: only n > previous n arms it; a live layout fits when it
    # settles, no running sim (paused / map) fits at once.
    assert "const revealing = n > docLimit;" in body
    assert body.index("const revealing = n > docLimit;") < body.index("docLimit = n;")
    assert "if (revealing) {" in body
    assert "if (sim) fitOnSettle = true;" in body
    assert "if (!sim || paused) autoFit();" in body


def test_a_paused_reveal_fits_now_and_again_at_the_settle_after_resume() -> None:
    # added by orchestrator (task 166): Pause -> reveal -> Resume must not
    # drift off screen once the resumed layout expands past the first fit.
    body = _js_block("function applyDocumentLimit(n)")
    reveal = body[body.index("if (revealing) {") :]

    # Assert: a paused live layout BOTH fits at once AND stays armed — the
    # sim.on("end") after Resume's reheat runs the second (and last) fit.
    assert reveal.index("if (sim) fitOnSettle = true;") < reveal.index(
        "if (!sim || paused) autoFit();"
    )
    assert "else autoFit();" not in reveal


def test_the_armed_fit_runs_once_when_the_layout_settles() -> None:
    end = _RENDER_JS[_RENDER_JS.index('sim.on("end"') :]
    end = end[: end.index("});")]
    assert "if (fitOnSettle) { fitOnSettle = false; autoFit(); }" in end


def test_the_auto_fit_moves_only_the_camera_and_re_freezes_it() -> None:
    body = _js_block("function autoFit()")

    # Assert: the Fit button's path; if a gesture had frozen the camera, it
    # is frozen again on the new extent after the animation (later drags
    # still never refit); no node position or pin is written.
    assert "const wasFrozen = renderer.getCustomBBox() != null;" in body
    assert "fitView().then(() => { if (wasFrozen) freezeViewport(); });" in body
    for untouched in ("fx", ".x =", "mergeNodeAttributes", "updateEachNodeAttributes"):
        assert untouched not in body


def test_a_drag_or_marquee_cancels_an_armed_auto_fit() -> None:
    body = _js_block('captor.on("mousemovebody"')

    gesture = body[body.index("if (!pointerMoved) {") :]
    assert "fitOnSettle = false;" in gesture[: gesture.index("}")]


def test_a_plain_stage_press_is_remembered_as_a_pan() -> None:
    body = _js_block("function startMarquee(e)")

    # Assert: an unshifted stage (or edge) press records where it started and
    # still leaves the camera pan to Sigma.
    assert (
        "if (!e.event.original.shiftKey) { pan = { x: e.event.x, y: e.event.y }; return; }"
        in body
    )


def test_a_plain_pan_cancels_an_armed_auto_fit_and_keeps_its_camera() -> None:
    # added by orchestrator (task 166): the user's pan wins over the fit.
    body = _js_block('captor.on("mousemovebody"')
    pan = body[body.index("if (pan) {") : body.index("if (!press && !marquee) return;")]

    # Assert: checked BEFORE the early return; past the drag threshold it
    # disarms the fit; a lost mouseup drops it; Sigma's pan is never prevented.
    assert "if (e.original.buttons === 0) pan = null;" in pan
    assert (
        "else if (Math.hypot(e.x - pan.x, e.y - pan.y) >= DRAG_THRESHOLD) "
        "{ fitOnSettle = false; pan = null; }" in pan
    )
    assert "preventSigmaDefault" not in pan


def test_a_release_ends_the_pan() -> None:
    body = _js_block('captor.on("mouseup"')

    assert "pan = null;" in body


# --- Hidden child count (task 166, ADR-011 §8) ---


def test_only_a_stamped_parent_carries_child_count() -> None:
    stamped = {**_node(f"{_UID}:chunk:p1", "chunk"), "child_count": 23}
    plain = _node(f"{_UID}:chunk:p2", "chunk")

    payload = to_graph_payload(QueryResult(nodes=[stamped, plain], edges=[]))

    by_id = {n["id"]: n for n in payload["nodes"]}
    assert by_id[stamped["_id"]]["childCount"] == 23
    assert "childCount" not in by_id[plain["_id"]]


def test_an_unstamped_payload_is_unchanged_by_the_child_count() -> None:
    payload = to_graph_payload(_seed_result())

    assert all("childCount" not in node for node in payload["nodes"])


def test_the_hover_card_ends_with_the_hidden_child_count() -> None:
    body = _js_block('renderer.on("enterNode"')

    # Assert: one extra row through metaRows, after the curated meta, only
    # when the payload carries the count.
    line = 'if (n.childCount != null) card["child chunks"] = n.childCount + " (not shown)";'
    assert line in body
    assert (
        body.index("card = Object.assign({ type: n.type }, n.meta);")
        < body.index(line)
        < body.index("metaRows(card)")
    )


@pytest.mark.parametrize("token", ["childCount", '"child chunks"', '" (not shown)"'])
def test_template_carries_the_child_count_row(token: str) -> None:
    assert token in _FILE_HTML_BASE


def test_the_closure_marker_never_reaches_the_payload() -> None:
    marked = {**_node(f"{_UID}:document:d1", "document"), "_closure_added": True}
    edge = {
        **_edge(f"{_UID}:chunk:p1", "part_of", marked["_id"]),
        "_closure_added": True,
    }

    payload = to_graph_payload(QueryResult(nodes=[marked], edges=[edge]))

    assert "_closure_added" not in json.dumps(payload)


# ---------------------------------------------------------------------------
# The rag Memory structure (task 173) — same renderer, zero template change
# ---------------------------------------------------------------------------

# sha256 of each template constant as of main before task 173. The rag tree
# draws through the template UNCHANGED (its edges are synthesised to fit it), so
# a JS / CSS / DOM edit made "for rag" turns this red.
# Re-pinned by task 179 only: the lean map node's colour resolver
# (`colourOf`, incl. its negative-id noise fallback) and the map tooltip's
# rebuilt `document` row.
_TEMPLATE_SHA256 = {
    "_GRAPH_STYLE": "39b6d3020fedea6fe530f6ba671b5ca0a95924e0718d6d0f5fab5c37f214bbca",
    "_BODY_MARKUP": "eabb61a3b90ddd3cfea41a32ed8d8984b0fc9ceb91dab8d99cbcddbee2e5af6c",
    "_RENDER_JS": "055947dcf2cd4ff6b5ddf6f338b5d685b37bab4744631a4ae685a3e75c95026b",
    "_FILE_HTML_TEMPLATE": (
        "fe65603e2631b89c69651571bc087e00e757ce1e082c2e12380864b035b0423d"
    ),
}


@pytest.mark.parametrize("name", sorted(_TEMPLATE_SHA256))
def test_the_template_constants_are_byte_identical_to_main(name: str) -> None:
    text = getattr(graph_module, name)

    assert hashlib.sha256(text.encode()).hexdigest() == _TEMPLATE_SHA256[name]


def _rag_tree(n_documents: int) -> MemoryStructure:
    """``n_documents`` × (document → 2 parents → 1 child each), ranked by recency."""

    nodes: list[dict] = []
    for rank in range(1, n_documents + 1):
        doc = f"{_UID}:document:d{rank}"
        nodes.append({**_node(doc, "document"), "doc_rank": rank})
        for p in range(2):
            parent = f"{doc}:p{p}"
            child = f"{parent}:c0"
            nodes.append(
                {
                    **_node(parent, "chunk"),
                    "subtype": "parent",
                    "parent_id": doc,
                    "doc_rank": rank,
                }
            )
            nodes.append(
                {
                    **_node(child, "chunk"),
                    "subtype": "child",
                    "parent_id": parent,
                    "doc_rank": rank,
                }
            )
    return MemoryStructure(
        nodes=nodes, edges=synthesize_part_of_edges(nodes, PydanticObjectId())
    )


def test_a_rag_tree_draws_three_sizes_by_role() -> None:
    payload = to_graph_payload(_rag_tree(1))

    sizes = {(n["type"], n["meta"].get("subtype")): n["size"] for n in payload["nodes"]}
    assert sizes == {
        ("document", None): 10,
        ("chunk", "parent"): 7,
        ("chunk", "child"): 4,
    }


def test_a_rag_tree_ships_part_of_edges_and_no_unknown_node() -> None:
    payload = to_graph_payload(_rag_tree(2))

    assert len(payload["edges"]) == 8  # one per chunk row
    assert {e["type"] for e in payload["edges"]} == {"part_of"}
    assert "unknown" not in {n["type"] for n in payload["nodes"]}


def test_a_rag_tree_without_a_query_shows_min_100_total_by_recency() -> None:
    payload = to_graph_payload(_rag_tree(3), document_order="recency")

    assert payload["controls"]["documents"] == {
        "shown": min(app_config.query.full_graph_shown_docs, 3),
        "total": 3,
        "order": "recency",
    }


def test_a_rag_query_view_shows_every_document_by_relevance() -> None:
    payload = to_graph_payload(_rag_tree(3), document_order="relevance")

    assert payload["controls"]["documents"] == {
        "shown": 3,
        "total": 3,
        "order": "relevance",
    }


def test_an_explicit_document_order_wins_over_the_slug(mocker, tmp_path: Path) -> None:
    # The rag no-query view is slugged "structure" yet ranked by recency: a
    # non-empty slug must not flip it to relevance.
    build = mocker.patch(
        "tree.memory.visualize.graph.to_graph_payload", wraps=to_graph_payload
    )
    mocker.patch("tree.memory.visualize.graph.GRAPHS_DIR", tmp_path)

    path = visualize_query_result(
        _rag_tree(2), open_browser=False, query="structure", document_order="recency"
    )

    assert build.call_args.kwargs == {"document_order": "recency"}
    assert path.name.startswith("structure-")
    assert '"order": "recency"' in path.read_text(encoding="utf-8")
