"""Unit tests for ``scripts/visualize_structure.py`` — the **Memory structure** CLI.

The CLI is glue, so every boundary (Mongo, tenant resolution, the embedding
model, the readers, the renderer) is mocked; what is asserted is the mode branch
(rag reads the document → parent → child tree, graphrag today's graph readers),
what reaches the renderer (slug + ranking), and the three one-line exits.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from beanie import PydanticObjectId
from click.testing import CliRunner

from tree.memory.rag.search import SearchUnavailableError
from tree.memory.rag.structure import EMPTY_MEMORY_MESSAGE, NO_RESULTS_MESSAGE
from tree.memory.rag.types import (
    DocumentMeta,
    MemoryStructure,
    RetrievalResult,
    RetrievedParent,
)
from tree.memory.types import QueryResult

_TREE = MemoryStructure(
    nodes=[{"_id": "doc1", "type": "document"}, {"_id": "p1", "parent_id": "doc1"}],
    edges=[{"source_node_id": "p1", "target_node_id": "doc1", "type": "part_of"}],
)


@pytest.fixture
def cli_module():
    """Import the script lazily so module-load side effects stay scoped."""

    import scripts.visualize_structure as module

    return module


@pytest.fixture
def render(mocker, cli_module):
    """Stub Mongo, tenant resolution and the embedding model; return the renderer."""

    mocker.patch.object(cli_module, "init_mongodb", new_callable=AsyncMock)
    mocker.patch.object(
        cli_module,
        "resolve_user_id",
        new_callable=AsyncMock,
        return_value=PydanticObjectId(),
    )
    mocker.patch.object(cli_module, "get_embedding_model")
    return mocker.patch.object(
        cli_module, "visualize_query_result", return_value="/tmp/x.html"
    )


@pytest.fixture
def graph_readers(mocker, cli_module) -> dict[str, AsyncMock]:
    """graphrag's two readers, returning a one-node graph."""

    return {
        name: mocker.patch.object(
            cli_module,
            name,
            new_callable=AsyncMock,
            return_value=QueryResult(nodes=[{"_id": "p1"}], edges=[]),
        )
        for name in ("query_memory", "fetch_full_graph")
    }


@pytest.fixture
def rag_readers(mocker, cli_module) -> dict[str, AsyncMock]:
    """rag's three readers: retrieval finds one parent, both structures a tree."""

    return {
        "retrieve_parents": mocker.patch.object(
            cli_module,
            "retrieve_parents",
            new_callable=AsyncMock,
            return_value=RetrievalResult(parents=[_parent()]),
        ),
        **{
            name: mocker.patch.object(
                cli_module, name, new_callable=AsyncMock, return_value=_TREE
            )
            for name in ("fetch_rag_structure", "fetch_retrieval_structure")
        },
    }


@pytest.fixture
def rag_mode(monkeypatch, cli_module) -> None:
    monkeypatch.setattr(cli_module.app_config.memory, "mode", "rag")


@pytest.fixture
def graphrag_mode(monkeypatch, cli_module) -> None:
    monkeypatch.setattr(cli_module.app_config.memory, "mode", "graphrag")


def _parent() -> RetrievedParent:
    return RetrievedParent(
        parent_id="p1",
        content="parent",
        score=0.03,
        document=DocumentMeta(document_id="doc1"),
    )


class TestRagMode:
    def test_no_query_draws_the_recent_tree_slugged_structure(
        self, cli_module, rag_mode, render, rag_readers, graph_readers
    ) -> None:
        result = CliRunner().invoke(cli_module.main, ["--no-open"])

        assert result.exit_code == 0, result.output
        assert rag_readers["fetch_rag_structure"].await_args.kwargs == {
            "max_docs": cli_module.app_config.query.full_graph_max_docs
        }
        assert render.call_args.args[0] is _TREE
        assert render.call_args.kwargs["query"] == "structure"
        assert render.call_args.kwargs["document_order"] == "recency"
        assert "Wrote /tmp/x.html" in result.output

    def test_max_docs_is_forwarded_to_the_tree_read(
        self, cli_module, rag_mode, render, rag_readers
    ) -> None:
        CliRunner().invoke(cli_module.main, ["--max-docs", "7", "--no-open"])

        assert rag_readers["fetch_rag_structure"].await_args.kwargs == {"max_docs": 7}

    def test_query_retrieves_parents_then_reads_their_tree(
        self, cli_module, rag_mode, render, rag_readers
    ) -> None:
        result = CliRunner().invoke(
            cli_module.main, ["--query", "memory", "--top-k", "4", "--no-open"]
        )

        assert result.exit_code == 0, result.output
        assert rag_readers["retrieve_parents"].await_args.kwargs == {"top_k": 4}
        retrieval = rag_readers["fetch_retrieval_structure"].await_args.args[-1]
        assert retrieval.parents[0].parent_id == "p1"
        assert render.call_args.kwargs["query"] == "memory"
        assert render.call_args.kwargs["document_order"] == "relevance"

    def test_nothing_found_prints_the_no_results_line_and_draws_nothing(
        self, cli_module, rag_mode, render, rag_readers
    ) -> None:
        rag_readers["retrieve_parents"].return_value = RetrievalResult(
            outcome="nothing_found"
        )

        result = CliRunner().invoke(cli_module.main, ["--query", "zzzq", "--no-open"])

        assert result.exit_code == 1
        assert result.output.splitlines() == [NO_RESULTS_MESSAGE.format(query="zzzq")]
        assert result.output.strip() == 'No results for "zzzq" — nothing to draw.'
        rag_readers["fetch_retrieval_structure"].assert_not_awaited()
        render.assert_not_called()

    def test_an_empty_memory_prints_the_empty_line_and_draws_nothing(
        self, cli_module, rag_mode, render, rag_readers
    ) -> None:
        rag_readers["fetch_rag_structure"].return_value = MemoryStructure()

        result = CliRunner().invoke(cli_module.main, ["--no-open"])

        assert result.exit_code == 1
        assert result.output.strip() == EMPTY_MEMORY_MESSAGE
        assert EMPTY_MEMORY_MESSAGE == (
            "Memory is empty for this user — run make memory-run-pipeline first."
        )
        render.assert_not_called()

    def test_the_graph_readers_are_never_called(
        self, cli_module, rag_mode, render, rag_readers, graph_readers
    ) -> None:
        CliRunner().invoke(cli_module.main, ["--no-open"])
        CliRunner().invoke(cli_module.main, ["--query", "q", "--no-open"])

        graph_readers["fetch_full_graph"].assert_not_awaited()
        graph_readers["query_memory"].assert_not_awaited()


class TestGraphragMode:
    def test_query_expands_the_graph_and_renders_it(
        self, cli_module, graphrag_mode, render, graph_readers
    ) -> None:
        result = CliRunner().invoke(cli_module.main, ["--query", "q", "--no-open"])

        assert result.exit_code == 0
        render.assert_called_once()
        assert render.call_args.kwargs["query"] == "q"
        assert render.call_args.kwargs["document_order"] == "relevance"

    def test_without_a_query_it_loads_the_full_graph(
        self, cli_module, graphrag_mode, render, graph_readers
    ) -> None:
        result = CliRunner().invoke(cli_module.main, ["--no-open"])

        assert result.exit_code == 0
        graph_readers["fetch_full_graph"].assert_awaited_once()
        render.assert_called_once()
        # Same file name and ranking as before task 173: graph-<stamp>.html.
        assert render.call_args.kwargs["query"] == "graph"
        assert render.call_args.kwargs["document_order"] == "recency"

    def test_max_docs_is_forwarded_to_the_full_graph_read(
        self, cli_module, graphrag_mode, render, graph_readers
    ) -> None:
        result = CliRunner().invoke(cli_module.main, ["--max-docs", "7", "--no-open"])

        assert result.exit_code == 0
        assert graph_readers["fetch_full_graph"].await_args.kwargs == {"max_docs": 7}

    def test_max_docs_defaults_to_the_configured_cap(
        self, cli_module, graphrag_mode, render, graph_readers
    ) -> None:
        CliRunner().invoke(cli_module.main, ["--no-open"])

        assert graph_readers["fetch_full_graph"].await_args.kwargs == {
            "max_docs": cli_module.app_config.query.full_graph_max_docs
        }
        option = next(o for o in cli_module.main.params if o.name == "max_docs")
        assert option.default == cli_module.app_config.query.full_graph_max_docs
        assert option.show_default is True

    def test_an_empty_graph_exits_one(
        self, cli_module, graphrag_mode, render, graph_readers
    ) -> None:
        graph_readers["fetch_full_graph"].return_value = QueryResult()

        result = CliRunner().invoke(cli_module.main, ["--no-open"])

        assert result.exit_code == 1
        assert result.output.strip() == EMPTY_MEMORY_MESSAGE
        render.assert_not_called()

    def test_an_empty_query_view_prints_the_no_results_line(
        self, cli_module, graphrag_mode, render, graph_readers
    ) -> None:
        graph_readers["query_memory"].return_value = QueryResult()

        result = CliRunner().invoke(cli_module.main, ["--query", "zzzq", "--no-open"])

        assert result.exit_code == 1
        assert result.output.strip() == NO_RESULTS_MESSAGE.format(query="zzzq")
        render.assert_not_called()


@pytest.mark.parametrize("mode_fixture", ["rag_mode", "graphrag_mode"])
class TestBothModes:
    def test_max_docs_below_one_is_a_usage_error(
        self, request, cli_module, render, rag_readers, graph_readers, mode_fixture
    ) -> None:
        request.getfixturevalue(mode_fixture)

        result = CliRunner().invoke(cli_module.main, ["--max-docs", "0", "--no-open"])

        assert result.exit_code == 2
        assert "Invalid value for '--max-docs'" in result.output
        graph_readers["fetch_full_graph"].assert_not_awaited()
        rag_readers["fetch_rag_structure"].assert_not_awaited()

    def test_max_docs_never_reaches_a_query(
        self, request, cli_module, render, rag_readers, graph_readers, mode_fixture
    ) -> None:
        request.getfixturevalue(mode_fixture)

        result = CliRunner().invoke(
            cli_module.main, ["--query", "q", "--max-docs", "7", "--no-open"]
        )

        assert result.exit_code == 0, result.output
        graph_readers["fetch_full_graph"].assert_not_awaited()
        rag_readers["fetch_rag_structure"].assert_not_awaited()
        for reader in ("query_memory", "fetch_retrieval_structure"):
            awaited = {**graph_readers, **rag_readers}[reader].await_args
            assert awaited is None or "max_docs" not in awaited.kwargs


class TestSearchUnavailable:
    """Both search legs down is an operator outcome, not a traceback (#126)."""

    @pytest.mark.parametrize(
        "mode_fixture,retrieval_attr",
        [("rag_mode", "retrieve_parents"), ("graphrag_mode", "query_memory")],
        ids=["rag", "graphrag"],
    )
    def test_one_retryable_line_and_exit_one(
        self,
        request,
        mocker,
        cli_module,
        render,
        mode_fixture: str,
        retrieval_attr: str,
    ) -> None:
        request.getfixturevalue(mode_fixture)
        mocker.patch.object(
            cli_module,
            retrieval_attr,
            new_callable=AsyncMock,
            side_effect=SearchUnavailableError(
                "vector and text search are both unavailable"
            ),
        )

        result = CliRunner().invoke(cli_module.main, ["--query", "prefect"])

        assert result.exit_code == 1
        assert result.output.splitlines() == [
            "Search unavailable: vector and text search are both unavailable "
            "— retryable"
        ]
        render.assert_not_called()
