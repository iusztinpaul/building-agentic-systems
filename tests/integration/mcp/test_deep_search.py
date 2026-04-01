"""Integration tests for the deep_search_memory MCP tool."""

import shutil
from pathlib import Path

import pytest
import yaml

from twin.config.paths import MEMORY_DIR
from twin.mcp.deep_search import slugify, write_deep_search_results
from twin.mcp.tools import deep_search_memory
from twin.memory.types import QueryResult
from twin.models.fake_model import FakeEmbeddingModel

TEST_DATABASE = "integration_tests_twin"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def sample_results():
    """A small QueryResult with two nodes and one edge."""

    nodes = [
        {
            "_id": "person:alice",
            "kind": "node",
            "type": "person",
            "name": "alice",
            "properties": {"aliases": ["alice doe"], "email": "alice@example.com"},
            "embedding": [0.0] * 384,
            "sources": [],
            "created_at": "2025-01-15T10:00:00Z",
            "updated_at": "2025-01-15T10:00:00Z",
        },
        {
            "_id": "task:write report",
            "kind": "node",
            "type": "task",
            "name": "write report",
            "properties": {
                "content": "Write the quarterly ML report",
                "date": "2025-02-01",
            },
            "embedding": [0.0] * 384,
            "sources": [],
            "created_at": "2025-01-15T10:00:00Z",
            "updated_at": "2025-01-15T10:00:00Z",
        },
    ]
    edges = [
        {
            "_id": "person:alice|todo|task:write report",
            "kind": "edge",
            "type": "todo",
            "source_node_id": "person:alice",
            "source_type": "person",
            "target_node_id": "task:write report",
            "target_type": "task",
            "sources": [],
            "created_at": "2025-01-15T10:00:00Z",
            "updated_at": "2025-01-15T10:00:00Z",
        },
    ]
    return QueryResult(nodes=nodes, edges=edges)


@pytest.fixture()
def memory_dir():
    """Ensure MEMORY_DIR (.twin/memory/) is cleaned up after tests."""

    yield
    if MEMORY_DIR.exists():
        shutil.rmtree(MEMORY_DIR)


# ---------------------------------------------------------------------------
# slugify
# ---------------------------------------------------------------------------


class TestSlugify:
    @pytest.mark.parametrize(
        "input_id, expected",
        [
            ("person:alice", "person-alice"),
            (
                "person:alice|related_to|person:bob",
                "person-alice--related_to--person-bob",
            ),
            ("task:write report", "task-write_report"),
        ],
        ids=["node_id", "edge_id", "spaces_replaced"],
    )
    def test_known_ids(self, input_id: str, expected: str) -> None:
        assert slugify(input_id) == expected

    def test_complex_id_strips_url_chars(self) -> None:
        result = slugify("chunk:https://example.com/p/article#chunk-0")
        assert "/" not in result
        assert "#" not in result


# ---------------------------------------------------------------------------
# write_deep_search_results
# ---------------------------------------------------------------------------


class TestWriteDeepSearchResults:
    def test_creates_directory_and_index(self, sample_results, memory_dir):
        session_dir, index_yaml = write_deep_search_results(
            "find alice tasks", sample_results, session_id="test-session"
        )

        assert session_dir.exists()
        assert (session_dir / "index.yaml").exists()

        index = yaml.safe_load(index_yaml)
        assert index["session_id"] == "test-session"
        assert index["query"] == "find alice tasks"
        assert index["total_nodes"] == 2
        assert index["total_edges"] == 1
        assert len(index["results"]) == 3

    def test_writes_node_markdown_files(self, sample_results, memory_dir):
        session_dir, _ = write_deep_search_results(
            "test", sample_results, session_id="test-nodes"
        )

        alice_file = session_dir / "person-alice.md"
        assert alice_file.exists()

        content = alice_file.read_text()
        assert "# person: alice" in content
        assert "person:alice" in content
        assert "aliases" in content
        assert "alice doe" in content
        # Embedding should NOT appear in the file.
        assert "embedding" not in content.lower() or "0.0" not in content

    def test_writes_edge_markdown_files(self, sample_results, memory_dir):
        session_dir, _ = write_deep_search_results(
            "test", sample_results, session_id="test-edges"
        )

        edge_file = session_dir / "person-alice--todo--task-write_report.md"
        assert edge_file.exists()

        content = edge_file.read_text()
        assert "todo" in content
        assert "person:alice" in content
        assert "task:write report" in content

    def test_index_entries_have_context(self, sample_results, memory_dir):
        _, index_yaml = write_deep_search_results(
            "test", sample_results, session_id="test-context"
        )

        index = yaml.safe_load(index_yaml)
        for entry in index["results"]:
            assert "context" in entry
            assert len(entry["context"]) > 0

    def test_node_entries_have_metadata(self, sample_results, memory_dir):
        _, index_yaml = write_deep_search_results(
            "test", sample_results, session_id="test-meta"
        )

        index = yaml.safe_load(index_yaml)
        node_entries = [e for e in index["results"] if e["kind"] == "node"]
        assert len(node_entries) == 2

        for entry in node_entries:
            assert "id" in entry
            assert "type" in entry
            assert "name" in entry
            assert "file" in entry

    def test_edge_entries_have_source_target(self, sample_results, memory_dir):
        _, index_yaml = write_deep_search_results(
            "test", sample_results, session_id="test-edge-meta"
        )

        index = yaml.safe_load(index_yaml)
        edge_entries = [e for e in index["results"] if e["kind"] == "edge"]
        assert len(edge_entries) == 1
        assert edge_entries[0]["source"] == "person:alice"
        assert edge_entries[0]["target"] == "task:write report"

    def test_auto_generates_session_id(self, sample_results, memory_dir):
        session_dir, index_yaml = write_deep_search_results("test", sample_results)

        index = yaml.safe_load(index_yaml)
        assert len(index["session_id"]) == 12
        assert session_dir.exists()


# ---------------------------------------------------------------------------
# deep_search_memory MCP tool (end-to-end with real MongoDB)
# ---------------------------------------------------------------------------


class TestDeepSearchMemoryTool:
    async def test_returns_yaml_index(self, make_mcp_ctx, seed_graph, memory_dir):
        ctx = make_mcp_ctx(embedding_model=FakeEmbeddingModel())

        result = await deep_search_memory(
            "alice", ctx, top_k=5, max_hops=1, session_id="e2e-test"
        )

        index = yaml.safe_load(result)
        assert index["session_id"] == "e2e-test"
        assert index["total_nodes"] >= 1
        assert len(index["results"]) >= 1

        # Verify files were written.
        session_dir = Path(index["directory"])
        assert session_dir.exists()
        for entry in index["results"]:
            assert (session_dir / entry["file"]).exists()

    async def test_no_results_returns_message(
        self, make_mcp_ctx, seed_graph, memory_dir
    ):
        ctx = make_mcp_ctx(embedding_model=FakeEmbeddingModel())

        result = await deep_search_memory(
            "xyznonexistent999", ctx, top_k=5, max_hops=0, session_id="e2e-empty"
        )

        assert result == "No results found."
