"""Unit tests for the ``scripts/query_graph.py`` mode branch (ADR-006 §5).

The CLI is glue, so every boundary (Mongo, tenant resolution, the embedding
model, retrieval, the renderer) is mocked; what is asserted is the branch: in
``rag`` a query prints parent blocks and writes NO HTML, and a missing query is
a refusal with exit 1 rather than an empty graph.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from beanie import PydanticObjectId
from click.testing import CliRunner

from tree.memory.rag.types import (
    DocumentMeta,
    MatchedChild,
    RetrievalResult,
    RetrievedParent,
)
from tree.memory.types import QueryResult


@pytest.fixture
def cli_module():
    """Import the script lazily so module-load side effects stay scoped."""

    import scripts.query_graph as module

    return module


@pytest.fixture
def mocked_boundaries(mocker, cli_module):
    """Stub Mongo, tenant resolution, the embedding model and the renderer."""

    mocker.patch.object(cli_module, "init_mongodb", new_callable=AsyncMock)
    mocker.patch.object(
        cli_module,
        "resolve_user_id",
        new_callable=AsyncMock,
        return_value=PydanticObjectId(),
    )
    mocker.patch.object(cli_module, "get_embedding_model")
    return mocker.patch.object(cli_module, "visualize_query_result")


@pytest.fixture
def rag_mode(mocker, cli_module):
    mocker.patch.object(cli_module.app_config.memory, "mode", "rag")


@pytest.fixture
def graphrag_mode(mocker, cli_module):
    mocker.patch.object(cli_module.app_config.memory, "mode", "graphrag")


def _parent(score: float = 0.032) -> RetrievedParent:
    return RetrievedParent(
        parent_id="p1",
        chunk_index=0,
        heading_path=["Retrieval", "Parent-document retrieval"],
        content="It stores parents without vectors. " * 20,
        score=score,
        document=DocumentMeta(
            document_id="doc1",
            title="Memory for AI Agents",
            source_uri="file://doc1",
        ),
        matched_children=[
            MatchedChild(child_id="c0", chunk_index=0, content="a", score=score),
            MatchedChild(child_id="c1", chunk_index=1, content="b", score=0.01),
        ],
    )


class TestRagMode:
    def test_query_prints_one_block_per_parent(
        self, mocker, cli_module, rag_mode, mocked_boundaries
    ) -> None:
        retrieve = mocker.patch.object(
            cli_module,
            "retrieve_parents",
            new_callable=AsyncMock,
            return_value=RetrievalResult(parents=[_parent()]),
        )

        result = CliRunner().invoke(cli_module.main, ["--query", "what is a parent"])

        assert result.exit_code == 0
        assert "[0.032] Memory for AI Agents" in result.output
        assert "Retrieval > Parent-document retrieval" in result.output
        assert "matched children: 2" in result.output
        assert retrieve.await_args.kwargs["top_k"] == cli_module.app_config.query.top_k

    def test_query_prints_only_the_first_300_content_chars(
        self, mocker, cli_module, rag_mode, mocked_boundaries
    ) -> None:
        parent = _parent()
        mocker.patch.object(
            cli_module,
            "retrieve_parents",
            new_callable=AsyncMock,
            return_value=RetrievalResult(parents=[parent]),
        )

        result = CliRunner().invoke(cli_module.main, ["--query", "q"])

        assert parent.content[:300] in result.output.replace("    ", "")
        assert parent.content[:310] not in result.output.replace("    ", "")

    def test_query_writes_no_graph_file(
        self, mocker, cli_module, rag_mode, mocked_boundaries
    ) -> None:
        mocker.patch.object(
            cli_module,
            "retrieve_parents",
            new_callable=AsyncMock,
            return_value=RetrievalResult(parents=[_parent()]),
        )

        CliRunner().invoke(cli_module.main, ["--query", "q"])

        mocked_boundaries.assert_not_called()

    def test_empty_result_prints_no_results_and_exits_zero(
        self, mocker, cli_module, rag_mode, mocked_boundaries
    ) -> None:
        mocker.patch.object(
            cli_module,
            "retrieve_parents",
            new_callable=AsyncMock,
            return_value=RetrievalResult(),
        )

        result = CliRunner().invoke(cli_module.main, ["--query", "q"])

        assert result.exit_code == 0
        assert "No results." in result.output

    def test_without_a_query_it_refuses_and_exits_one(
        self, cli_module, rag_mode, mocked_boundaries
    ) -> None:
        result = CliRunner().invoke(cli_module.main, [])

        assert result.exit_code == 1
        assert cli_module.RAG_FULL_GRAPH_UNAVAILABLE in result.output

    def test_the_refusal_message_names_the_mode_and_the_way_out(
        self, cli_module
    ) -> None:
        assert cli_module.RAG_FULL_GRAPH_UNAVAILABLE == (
            "Full-graph visualization is unavailable in rag mode "
            '(memory.mode=rag): there are no edges. Pass QUERY="..." for '
            "parent-document search or switch to graphrag."
        )


class TestGraphragModeUnchanged:
    def test_query_expands_the_graph_and_renders_it(
        self, mocker, cli_module, graphrag_mode, mocked_boundaries
    ) -> None:
        mocker.patch.object(
            cli_module,
            "query_memory",
            new_callable=AsyncMock,
            return_value=QueryResult(nodes=[{"_id": "p1"}], edges=[]),
        )

        result = CliRunner().invoke(cli_module.main, ["--query", "q", "--no-open"])

        assert result.exit_code == 0
        mocked_boundaries.assert_called_once()

    def test_without_a_query_it_loads_the_full_graph(
        self, mocker, cli_module, graphrag_mode, mocked_boundaries
    ) -> None:
        full_graph = mocker.patch.object(
            cli_module,
            "fetch_full_graph",
            new_callable=AsyncMock,
            return_value=QueryResult(nodes=[{"_id": "p1"}], edges=[]),
        )

        result = CliRunner().invoke(cli_module.main, ["--no-open"])

        assert result.exit_code == 0
        full_graph.assert_awaited_once()
        mocked_boundaries.assert_called_once()

    def test_an_empty_graph_exits_one(
        self, mocker, cli_module, graphrag_mode, mocked_boundaries
    ) -> None:
        mocker.patch.object(
            cli_module,
            "fetch_full_graph",
            new_callable=AsyncMock,
            return_value=QueryResult(),
        )

        result = CliRunner().invoke(cli_module.main, ["--no-open"])

        assert result.exit_code == 1
        mocked_boundaries.assert_not_called()
