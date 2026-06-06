"""Integration tests for MCP tool handlers — end-to-end through real MongoDB."""

import json


from tree.mcp.tools import query_memory, search_memory
from tree.models.fake_model import FakeEmbeddingModel, FakeLLM


# ---------------------------------------------------------------------------
# query_memory (NL-to-pipeline tool)
# ---------------------------------------------------------------------------


class TestQueryMemoryTool:
    async def test_returns_matching_nodes(self, make_mcp_ctx, seed_graph):
        """FakeLLM returns a pipeline that matches person nodes — tool returns them."""

        llm = FakeLLM(
            [
                {
                    "pipeline": [
                        {"$match": {"kind": "node", "type": "person"}},
                        {"$limit": 10},
                    ]
                }
            ]
        )
        ctx = make_mcp_ctx(
            llm=llm,
            embedding_model=FakeEmbeddingModel(),
            user_id=seed_graph["user_id"],
        )

        result = await query_memory("find all people", ctx)

        parsed = json.loads(result)
        names = {doc["name"] for doc in parsed}
        assert "alice" in names
        assert "bob" in names

    async def test_strips_embeddings_from_output(self, make_mcp_ctx, seed_graph):
        llm = FakeLLM(
            [
                {
                    "pipeline": [
                        {"$match": {"kind": "node", "_id": "person:alice"}},
                        {"$limit": 1},
                    ]
                }
            ]
        )
        ctx = make_mcp_ctx(
            llm=llm,
            embedding_model=FakeEmbeddingModel(),
            user_id=seed_graph["user_id"],
        )

        result = await query_memory("find alice", ctx)

        assert "embedding" not in result

    async def test_returns_edges(self, make_mcp_ctx, seed_graph):
        llm = FakeLLM(
            [
                {
                    "pipeline": [
                        {"$match": {"kind": "edge", "type": "related_to"}},
                        {"$limit": 10},
                    ]
                }
            ]
        )
        ctx = make_mcp_ctx(
            llm=llm,
            embedding_model=FakeEmbeddingModel(),
            user_id=seed_graph["user_id"],
        )

        result = await query_memory("find relationships", ctx)

        parsed = json.loads(result)
        assert len(parsed) >= 1
        assert parsed[0]["source_node_id"] == seed_graph["alice_id"]
        assert parsed[0]["target_node_id"] == seed_graph["bob_id"]

    async def test_empty_result_returns_empty_list(self, make_mcp_ctx, seed_graph):
        llm = FakeLLM(
            [
                {
                    "pipeline": [
                        {"$match": {"kind": "node", "name": "__no_such_node__"}},
                        {"$limit": 10},
                    ]
                }
            ]
        )
        ctx = make_mcp_ctx(
            llm=llm,
            embedding_model=FakeEmbeddingModel(),
            user_id=seed_graph["user_id"],
        )

        result = await query_memory("find a nonexistent entity", ctx)

        parsed = json.loads(result)
        assert parsed == []

    async def test_retries_on_bad_first_pipeline(self, make_mcp_ctx, seed_graph):
        """First LLM response has a blocked stage; second is valid — retry succeeds."""

        llm = FakeLLM(
            [
                {"pipeline": [{"$out": "evil"}]},
                {
                    "pipeline": [
                        {"$match": {"kind": "node", "type": "person"}},
                        {"$limit": 10},
                    ]
                },
            ]
        )
        ctx = make_mcp_ctx(
            llm=llm,
            embedding_model=FakeEmbeddingModel(),
            user_id=seed_graph["user_id"],
        )

        result = await query_memory("find people", ctx)

        parsed = json.loads(result)
        assert len(parsed) >= 1
        assert llm.call_count == 2

    async def test_max_results_limits_output(self, make_mcp_ctx, seed_graph):
        """max_results caps the number of documents returned."""

        llm = FakeLLM(
            [
                {
                    "pipeline": [
                        {"$match": {"kind": "node", "type": "person"}},
                        {"$limit": 10},
                    ]
                }
            ]
        )
        ctx = make_mcp_ctx(
            llm=llm,
            embedding_model=FakeEmbeddingModel(),
            user_id=seed_graph["user_id"],
        )

        result = await query_memory("find all people", ctx, max_results=1)

        parsed = json.loads(result)
        assert len(parsed) == 1

    async def test_visualize_flag_generates_html(
        self, make_mcp_ctx, seed_graph, tmp_path, mocker
    ):
        llm = FakeLLM(
            [
                {
                    "pipeline": [
                        {"$match": {"kind": "node", "type": "person"}},
                        {"$limit": 10},
                    ]
                }
            ]
        )
        output_file = tmp_path / "graph.html"
        mocker.patch(
            "tree.mcp.tools.render_html",
            wraps=lambda g, **kw: __import__(
                "tree.memory.query.visualize", fromlist=["render_html"]
            ).render_html(g, output=output_file, open_browser=False),
        )
        ctx = make_mcp_ctx(
            llm=llm,
            embedding_model=FakeEmbeddingModel(),
            user_id=seed_graph["user_id"],
        )

        result = await query_memory("find people", ctx, visualize=True)

        assert "Graph visualized" in result
        assert "nodes" in result
        assert output_file.exists()


# ---------------------------------------------------------------------------
# search_memory (semantic + text search tool)
# ---------------------------------------------------------------------------


class TestSearchMemoryTool:
    async def test_returns_nodes_via_text_search(self, make_mcp_ctx, seed_graph):
        """Text index is available — search_memory finds nodes by name."""

        ctx = make_mcp_ctx(
            embedding_model=FakeEmbeddingModel(),
            user_id=seed_graph["user_id"],
        )

        result = await search_memory("alice", ctx, top_k=5, max_hops=1)

        parsed = json.loads(result)
        node_ids = {doc.get("_id") for doc in parsed}
        assert seed_graph["alice_id"] in node_ids

    async def test_graph_expansion_includes_edges(self, make_mcp_ctx, seed_graph):
        """Graph expansion from alice should discover the related_to edge and bob."""

        ctx = make_mcp_ctx(
            embedding_model=FakeEmbeddingModel(),
            user_id=seed_graph["user_id"],
        )

        result = await search_memory("alice", ctx, top_k=5, max_hops=1)

        parsed = json.loads(result)
        ids = {doc.get("_id") for doc in parsed}
        assert seed_graph["edge_id"] in ids or seed_graph["bob_id"] in ids

    async def test_max_results_truncates_output(self, make_mcp_ctx, seed_graph):
        """max_results caps total nodes + edges returned."""

        ctx = make_mcp_ctx(
            embedding_model=FakeEmbeddingModel(),
            user_id=seed_graph["user_id"],
        )

        # Without cap, alice search returns alice node + edge + bob node = 3 docs.
        result = await search_memory("alice", ctx, top_k=5, max_hops=1, max_results=2)

        parsed = json.loads(result)
        assert len(parsed) <= 2

    async def test_no_results_returns_empty(self, make_mcp_ctx, seed_graph):
        """A query with no matches returns an empty list."""

        ctx = make_mcp_ctx(
            embedding_model=FakeEmbeddingModel(),
            user_id=seed_graph["user_id"],
        )

        result = await search_memory("xyznonexistent999", ctx, top_k=5, max_hops=0)

        parsed = json.loads(result)
        assert parsed == []

    async def test_visualize_flag_generates_html(
        self, make_mcp_ctx, seed_graph, tmp_path, mocker
    ):
        output_file = tmp_path / "graph.html"
        mocker.patch(
            "tree.mcp.tools.render_html",
            wraps=lambda g, **kw: __import__(
                "tree.memory.query.visualize", fromlist=["render_html"]
            ).render_html(g, output=output_file, open_browser=False),
        )
        ctx = make_mcp_ctx(
            embedding_model=FakeEmbeddingModel(),
            user_id=seed_graph["user_id"],
        )

        result = await search_memory("alice", ctx, top_k=5, max_hops=1, visualize=True)

        assert "Graph visualized" in result
        assert output_file.exists()
