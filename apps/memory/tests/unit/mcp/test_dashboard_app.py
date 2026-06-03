"""Unit tests for the dashboard MCP App pure helpers.

Only the renderer-agnostic transforms are unit-tested; the tool / FastMCPApp /
Generative-UI wiring is exercised via integration tests.
"""

from prefab_ui import PrefabApp

from tree.mcp.dashboard_app import build_dashboard, node_type_counts


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
