from tree.memory.query.visualize import build_networkx_graph, _truncate
from tree.memory.types import QueryResult


class TestBuildNetworkxGraph:
    def test_empty_result(self):
        G = build_networkx_graph(QueryResult())
        assert G.number_of_nodes() == 0
        assert G.number_of_edges() == 0

    def test_nodes_only(self):
        result = QueryResult(
            nodes=[
                {"_id": "alice", "type": "person", "properties": {"aliases": ["ali"]}},
                {"_id": "bob", "type": "person", "properties": {}},
            ],
        )
        G = build_networkx_graph(result)

        assert G.number_of_nodes() == 2
        assert "alice" in G
        assert "bob" in G

    def test_edges_create_missing_endpoints(self):
        result = QueryResult(
            nodes=[{"_id": "alice", "type": "person", "properties": {}}],
            edges=[
                {
                    "type": "related_to",
                    "source_node_id": "alice",
                    "target_node_id": "bob",
                }
            ],
        )
        G = build_networkx_graph(result)

        assert G.number_of_nodes() == 2
        assert G.number_of_edges() == 1
        assert G.has_edge("alice", "bob")

    def test_edge_label(self):
        result = QueryResult(
            nodes=[
                {"_id": "alice", "type": "person", "properties": {}},
                {"_id": "task1", "type": "task", "properties": {}},
            ],
            edges=[
                {
                    "type": "todo",
                    "source_node_id": "alice",
                    "target_node_id": "task1",
                }
            ],
        )
        G = build_networkx_graph(result)

        edge_data = G.edges["alice", "task1"]
        assert edge_data["label"] == "todo"

    def test_node_colour_by_type(self):
        result = QueryResult(
            nodes=[
                {"_id": "alice", "type": "person", "properties": {}},
                {"_id": "doc1", "type": "document", "properties": {}},
            ],
        )
        G = build_networkx_graph(result)

        assert G.nodes["alice"]["color"] != G.nodes["doc1"]["color"]

    def test_content_truncated_in_hover(self):
        long_content = "x" * 500
        result = QueryResult(
            nodes=[
                {
                    "_id": "chunk-0",
                    "type": "chunk",
                    "properties": {"content": long_content},
                }
            ],
        )
        G = build_networkx_graph(result)

        hover = G.nodes["chunk-0"]["title"]
        assert len(hover) < 500


class TestTruncate:
    def test_short_text_unchanged(self):
        assert _truncate("hello", 10) == "hello"

    def test_long_text_truncated(self):
        result = _truncate("a" * 100, 20)
        assert len(result) == 20
        assert result.endswith("...")

    def test_exact_length_unchanged(self):
        assert _truncate("12345", 5) == "12345"
