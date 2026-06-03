"""Unit tests for the read-only D3 graph MCP App transform.

Only the pure ``to_graph_payload`` transform is unit-tested here; the tool and
``ui://`` resource handlers are exercised end-to-end in integration tests.
"""

from pathlib import Path

from tree.config.paths import GRAPHS_DIR
from tree.mcp.graph_app import (
    _FALLBACK_COLOUR,
    _default_graph_path,
    _render_graph_file,
    _slugify,
    to_graph_payload,
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
    assert payload["edges"] == [{"source": alice, "target": paper, "type": "mentions"}]


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
