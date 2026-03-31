"""Integration tests for MCP tool handlers — end-to-end through real MongoDB."""

import json
from datetime import datetime, timezone

import pytest

from twin.entities.knowledge_graph import (
    EdgeType,
    NodeType,
    build_edge_id,
    build_node_id,
)
from twin.mcp.tools import query_memory, search_memory
from twin.models.fake_model import FakeEmbeddingModel, FakeLLM

TEST_DATABASE = "integration_tests_twin"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
async def seed_graph(mongo_client):
    """Insert a small graph: two person nodes + one edge, with text index."""

    now = datetime.now(tz=timezone.utc)
    col = mongo_client[TEST_DATABASE]["knowledge_graph"]

    # Ensure text index exists for search_memory tests.
    await col.create_index(
        [
            ("name", "text"),
            ("properties.content", "text"),
            ("properties.aliases", "text"),
        ],
        name="text_index",
    )

    alice_id = build_node_id(NodeType.PERSON, "alice")
    bob_id = build_node_id(NodeType.PERSON, "bob")
    edge_id = build_edge_id(alice_id, EdgeType.RELATED_TO, bob_id)

    alice = {
        "_id": alice_id,
        "kind": "node",
        "type": NodeType.PERSON,
        "name": "alice",
        "properties": {"aliases": ["alice doe"], "content": "Alice is an ML engineer."},
        "embedding": [0.0] * 768,
        "sources": [],
        "created_at": now,
        "updated_at": now,
    }
    bob = {
        "_id": bob_id,
        "kind": "node",
        "type": NodeType.PERSON,
        "name": "bob",
        "properties": {"aliases": ["bob smith"], "content": "Bob is a data scientist."},
        "embedding": [0.0] * 768,
        "sources": [],
        "created_at": now,
        "updated_at": now,
    }
    edge = {
        "_id": edge_id,
        "kind": "edge",
        "type": EdgeType.RELATED_TO,
        "source_node_id": alice_id,
        "source_type": NodeType.PERSON,
        "target_node_id": bob_id,
        "target_type": NodeType.PERSON,
        "sources": [],
        "created_at": now,
        "updated_at": now,
    }

    for doc in [alice, bob, edge]:
        await col.replace_one({"_id": doc["_id"]}, doc, upsert=True)

    yield {"alice_id": alice_id, "bob_id": bob_id, "edge_id": edge_id}

    await col.delete_many({"_id": {"$in": [alice_id, bob_id, edge_id]}})


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
        ctx = make_mcp_ctx(llm=llm, embedding_model=FakeEmbeddingModel())

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
        ctx = make_mcp_ctx(llm=llm, embedding_model=FakeEmbeddingModel())

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
        ctx = make_mcp_ctx(llm=llm, embedding_model=FakeEmbeddingModel())

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
                        {"$match": {"kind": "node", "type": "episode"}},
                        {"$limit": 10},
                    ]
                }
            ]
        )
        ctx = make_mcp_ctx(llm=llm, embedding_model=FakeEmbeddingModel())

        result = await query_memory("find episodes", ctx)

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
        ctx = make_mcp_ctx(llm=llm, embedding_model=FakeEmbeddingModel())

        result = await query_memory("find people", ctx)

        parsed = json.loads(result)
        assert len(parsed) >= 1
        assert llm.call_count == 2

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
            "twin.mcp.tools.render_html",
            wraps=lambda g, **kw: __import__(
                "twin.memory.query.visualize", fromlist=["render_html"]
            ).render_html(g, output=output_file, open_browser=False),
        )
        ctx = make_mcp_ctx(llm=llm, embedding_model=FakeEmbeddingModel())

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

        ctx = make_mcp_ctx(embedding_model=FakeEmbeddingModel())

        result = await search_memory("alice", ctx, top_k=5, max_hops=1)

        parsed = json.loads(result)
        node_ids = {doc.get("_id") for doc in parsed}
        assert seed_graph["alice_id"] in node_ids

    async def test_graph_expansion_includes_edges(self, make_mcp_ctx, seed_graph):
        """Graph expansion from alice should discover the related_to edge and bob."""

        ctx = make_mcp_ctx(embedding_model=FakeEmbeddingModel())

        result = await search_memory("alice", ctx, top_k=5, max_hops=1)

        parsed = json.loads(result)
        ids = {doc.get("_id") for doc in parsed}
        assert seed_graph["edge_id"] in ids or seed_graph["bob_id"] in ids

    async def test_no_results_returns_empty(self, make_mcp_ctx, seed_graph):
        """A query with no matches returns an empty list."""

        ctx = make_mcp_ctx(embedding_model=FakeEmbeddingModel())

        result = await search_memory("xyznonexistent999", ctx, top_k=5, max_hops=0)

        parsed = json.loads(result)
        assert parsed == []

    async def test_visualize_flag_generates_html(
        self, make_mcp_ctx, seed_graph, tmp_path, mocker
    ):
        output_file = tmp_path / "graph.html"
        mocker.patch(
            "twin.mcp.tools.render_html",
            wraps=lambda g, **kw: __import__(
                "twin.memory.query.visualize", fromlist=["render_html"]
            ).render_html(g, output=output_file, open_browser=False),
        )
        ctx = make_mcp_ctx(embedding_model=FakeEmbeddingModel())

        result = await search_memory("alice", ctx, top_k=5, max_hops=1, visualize=True)

        assert "Graph visualized" in result
        assert output_file.exists()
