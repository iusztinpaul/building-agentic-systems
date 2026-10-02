"""Unit tests for ``scripts/search_memory.py`` — the mode-neutral text search.

The CLI is glue, so every boundary (Mongo, tenant resolution, the embedding
model, retrieval) is mocked; what is asserted is the printed ranking view and
that **Parent-document retrieval** is the only reader in BOTH modes.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from beanie import PydanticObjectId
from click.testing import CliRunner

from tree.memory.rag.search import SearchUnavailableError
from tree.memory.rag.types import (
    DocumentMeta,
    MatchedChild,
    RetrievalResult,
    RetrievedParent,
)


@pytest.fixture
def cli_module():
    """Import the script lazily so module-load side effects stay scoped."""

    import scripts.search_memory as module

    return module


@pytest.fixture
def mocked_boundaries(mocker, cli_module) -> None:
    """Stub Mongo, tenant resolution and the embedding model."""

    mocker.patch.object(cli_module, "init_mongodb", new_callable=AsyncMock)
    mocker.patch.object(
        cli_module,
        "resolve_user_id",
        new_callable=AsyncMock,
        return_value=PydanticObjectId(),
    )
    mocker.patch.object(cli_module, "get_embedding_model")


@pytest.fixture
def retrieve(mocker, cli_module) -> AsyncMock:
    return mocker.patch.object(
        cli_module,
        "retrieve_parents",
        new_callable=AsyncMock,
        return_value=RetrievalResult(parents=[_parent()]),
    )


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


class TestPrintedRanking:
    def test_query_prints_one_block_per_parent(
        self, cli_module, cli_module_mode, mocked_boundaries, retrieve
    ) -> None:
        result = CliRunner().invoke(cli_module.main, ["--query", "what is a parent"])

        assert result.exit_code == 0
        assert "[0.032] Memory for AI Agents" in result.output
        assert "Retrieval > Parent-document retrieval" in result.output
        assert "matched children: 2" in result.output
        assert retrieve.await_args.kwargs["top_k"] == cli_module.app_config.query.top_k

    def test_top_k_is_forwarded(
        self, cli_module, cli_module_mode, mocked_boundaries, retrieve
    ) -> None:
        CliRunner().invoke(cli_module.main, ["--query", "q", "--top-k", "3"])

        assert retrieve.await_args.kwargs["top_k"] == 3

    def test_query_prints_only_the_first_300_content_chars(
        self, cli_module, cli_module_mode, mocked_boundaries, retrieve
    ) -> None:
        parent = _parent()

        result = CliRunner().invoke(cli_module.main, ["--query", "q"])

        assert parent.content[:300] in result.output.replace("    ", "")
        assert parent.content[:310] not in result.output.replace("    ", "")

    def test_it_writes_no_file(
        self, mocker, cli_module, cli_module_mode, mocked_boundaries, retrieve, tmp_path
    ) -> None:
        mocker.patch("tree.memory.visualize.graph.GRAPHS_DIR", tmp_path)

        result = CliRunner().invoke(cli_module.main, ["--query", "q"])

        assert result.exit_code == 0
        assert list(tmp_path.iterdir()) == []
        assert "Wrote" not in result.output

    @pytest.mark.parametrize(
        "graph_reader", ["query_memory", "fetch_full_graph", "fetch_rag_structure"]
    )
    def test_parent_document_retrieval_is_the_only_reader(
        self, cli_module, cli_module_mode, mocked_boundaries, retrieve, graph_reader
    ) -> None:
        CliRunner().invoke(cli_module.main, ["--query", "q"])

        retrieve.assert_awaited_once()
        assert not hasattr(cli_module, graph_reader)

    def test_empty_result_prints_no_results_and_exits_zero(
        self, cli_module, cli_module_mode, mocked_boundaries, retrieve
    ) -> None:
        retrieve.return_value = RetrievalResult(outcome="nothing_found")

        result = CliRunner().invoke(cli_module.main, ["--query", "q"])

        assert result.exit_code == 0
        assert "No results." in result.output

    def test_a_degraded_search_prints_the_caveat_as_the_first_line(
        self, cli_module, cli_module_mode, mocked_boundaries, retrieve
    ) -> None:
        # The vector index is mid-rebuild: the operator must read the short list
        # as "half the index answered", not as "the memory has nothing".
        retrieve.return_value = RetrievalResult(
            parents=[_parent()], search_mode="text_only"
        )

        result = CliRunner().invoke(cli_module.main, ["--query", "voyage rate limit"])

        assert result.output.splitlines()[0] == (
            "Search ran text_only — the other leg was unavailable; "
            "results may miss matches."
        )
        assert "[0.032] Memory for AI Agents" in result.output

    def test_a_degraded_empty_search_still_prints_the_caveat(
        self, cli_module, cli_module_mode, mocked_boundaries, retrieve
    ) -> None:
        retrieve.return_value = RetrievalResult(search_mode="vector_only")

        result = CliRunner().invoke(cli_module.main, ["--query", "q"])

        assert result.output.splitlines()[0].startswith("Search ran vector_only —")
        assert "No results." in result.output

    def test_a_hybrid_search_prints_no_caveat(
        self, cli_module, cli_module_mode, mocked_boundaries, retrieve
    ) -> None:
        result = CliRunner().invoke(cli_module.main, ["--query", "q"])

        assert "Search ran" not in result.output

    def test_both_legs_down_is_one_retryable_line_and_exit_one(
        self, cli_module, cli_module_mode, mocked_boundaries, retrieve
    ) -> None:
        retrieve.side_effect = SearchUnavailableError(
            "vector and text search are both unavailable"
        )

        result = CliRunner().invoke(cli_module.main, ["--query", "prefect"])

        assert result.exit_code == 1
        assert result.output.splitlines() == [
            "Search unavailable: vector and text search are both unavailable "
            "— retryable"
        ]


class TestUsage:
    def test_a_missing_query_is_a_click_usage_error(
        self, cli_module, mocked_boundaries, retrieve
    ) -> None:
        result = CliRunner().invoke(cli_module.main, [])

        assert result.exit_code == 2
        assert "Missing option '--query'" in result.output
        retrieve.assert_not_awaited()


@pytest.fixture(params=["rag", "graphrag"])
def cli_module_mode(request, monkeypatch, cli_module) -> str:
    """Every printed-ranking case runs in BOTH modes: there is no mode branch."""

    monkeypatch.setattr(cli_module.app_config.memory, "mode", request.param)
    return request.param
