"""Unit tests for the dashboard MCP App pure helpers.

Only the renderer-agnostic transforms are unit-tested; the interactive-tool
(``memory_dashboard``) wiring is exercised via integration tests.
"""

from prefab_ui import PrefabApp

from tree.mcp.dashboard_app import (
    _edge_rows,
    _node_rows,
    build_dashboard,
    node_type_counts,
)


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


def test_build_dashboard_returns_serializable_prefabapp() -> None:
    # Arrange
    payload = {
        "nodes": [
            {"name": "alice", "type": "person"},
            {"name": "paper", "type": "document"},
        ],
        "edges": [{"source": "x", "target": "y", "type": "mentions"}],
    }

    # Act
    app = build_dashboard(payload, "test query")

    # Assert
    assert isinstance(app, PrefabApp)
    dumped = app.model_dump()
    assert dumped["view"] is not None


def test_build_dashboard_handles_empty_payload() -> None:
    # Act
    app = build_dashboard({"nodes": [], "edges": []}, "nothing")

    # Assert: still a valid PrefabApp (shows a "no results" message).
    assert isinstance(app, PrefabApp)
    assert app.model_dump()["view"] is not None


def test_node_rows_flatten_curated_metadata() -> None:
    # Arrange
    nodes = [
        {
            "id": "u:person:alice",
            "name": "alice",
            "type": "person",
            "meta": {
                "subtype": "engineer",
                "confidence": 0.9,
                "description": "An engineer.",
                "aliases": "Ali, Al",
            },
        },
        # A node with no metadata → empty cells, confidence None.
        {"id": "u:document:paper", "name": "paper", "type": "document"},
    ]

    # Act
    rows = _node_rows(nodes)

    # Assert
    assert rows[0] == {
        "name": "alice",
        "type": "person",
        "subtype": "engineer",
        "confidence": 0.9,
        "description": "An engineer.",
        "aliases": "Ali, Al",
    }
    assert rows[1] == {
        "name": "paper",
        "type": "document",
        "subtype": "",
        "confidence": None,
        "description": "",
        "aliases": "",
    }


def test_edge_rows_resolve_endpoint_names_and_metadata() -> None:
    # Arrange
    edges = [
        {
            "source": "u:person:alice",
            "target": "u:person:bob",
            "type": "knows",
            "meta": {"semantic_type": "social", "confidence": 0.5},
        },
        # Endpoint missing from the name map → falls back to the raw id.
        {"source": "u:person:alice", "target": "u:person:ghost", "type": "mentions"},
    ]
    name_by_id = {"u:person:alice": "alice", "u:person:bob": "bob"}

    # Act
    rows = _edge_rows(edges, name_by_id)

    # Assert
    assert rows[0] == {
        "source": "alice",
        "type": "knows",
        "target": "bob",
        "semantic_type": "social",
        "confidence": 0.5,
        "description": "",
    }
    assert rows[1]["target"] == "u:person:ghost"  # unresolved id kept verbatim
    assert rows[1]["confidence"] is None


def test_build_dashboard_with_metadata_renders() -> None:
    # Arrange: a payload shaped like to_graph_payload's output (nodes + edges).
    payload = {
        "nodes": [
            {
                "id": "u:person:alice",
                "name": "alice",
                "type": "person",
                "meta": {"subtype": "engineer", "confidence": 0.9},
            }
        ],
        "edges": [
            {
                "source": "u:person:alice",
                "target": "u:person:bob",
                "type": "knows",
                "meta": {"semantic_type": "social"},
            }
        ],
    }

    # Act
    app = build_dashboard(payload, "test query")

    # Assert: builds a valid PrefabApp (node + relationships tables).
    assert isinstance(app, PrefabApp)
    assert app.model_dump()["view"] is not None
