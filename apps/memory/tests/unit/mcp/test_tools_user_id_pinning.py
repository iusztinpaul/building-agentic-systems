"""Unit tests that prove the MCP tools route writes through ``user_id``.

This is a per-tool spot-check: for each ingestion tool the underlying
business-logic call must receive ``user_id=ctx.lifespan_context["user_id"]``
(which is itself pinned to ``_SERVER_USER_ID`` at boot, per #020). The
ingest_conversation gap surfaced in plan.md is the most important case
— the tool now writes a ``Document`` with the boot-pinned user_id, no
silent fallback to a default.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from beanie import PydanticObjectId

from tree.data.online_pipeline import ConversationSource, FileSource, UrlSource
from tree.memory.rag.types import RetrievalResult
from tree.memory.types import QueryResult
from tree.mcp import graph_tools as mcp_graph_tools
from tree.mcp import tools as mcp_tools

_SUBMITTED = {"status": "scheduled", "flow_run_id": "run-1"}


def _make_ctx(user_id: PydanticObjectId) -> MagicMock:
    ctx = MagicMock()
    ctx.lifespan_context = {
        "client": MagicMock(),
        "database": "test_db",
        "llm": MagicMock(),
        "embedding_model": MagicMock(),
        "user_id": user_id,
    }
    return ctx


class TestIngestConversationPropagatesUserId:
    """The headline gap from plan.md: ingest_conversation must carry user_id."""

    async def test_passes_user_id_to_dispatcher(self, mocker) -> None:
        user_id = PydanticObjectId()
        ctx = _make_ctx(user_id)

        mock_dispatch = mocker.patch(
            "tree.mcp.tools.dispatch_online_pipeline",
            new_callable=AsyncMock,
            return_value=_SUBMITTED,
        )

        await mcp_tools.ingest_conversation("Some text.", ctx)

        # The dispatcher receives a ConversationSource + the boot-pinned user_id
        # (it carries that user_id into the flow run's parameters).
        mock_dispatch.assert_awaited_once()
        source, passed_user_id = mock_dispatch.await_args.args
        assert isinstance(source, ConversationSource)
        assert source.text == "Some text."
        assert passed_user_id == user_id


class TestIngestUrlPropagatesUserId:
    async def test_passes_user_id_to_dispatcher(self, mocker) -> None:
        user_id = PydanticObjectId()
        ctx = _make_ctx(user_id)
        mock_dispatch = mocker.patch(
            "tree.mcp.tools.dispatch_online_pipeline",
            new_callable=AsyncMock,
            return_value=_SUBMITTED,
        )

        await mcp_tools.ingest_url("https://example.com", ctx)

        mock_dispatch.assert_awaited_once_with(
            UrlSource(uri="https://example.com"), user_id
        )


class TestIngestFilePropagatesUserId:
    async def test_passes_user_id_to_dispatcher(self, mocker) -> None:
        user_id = PydanticObjectId()
        ctx = _make_ctx(user_id)
        mock_dispatch = mocker.patch(
            "tree.mcp.tools.dispatch_online_pipeline",
            new_callable=AsyncMock,
            return_value=_SUBMITTED,
        )

        await mcp_tools.ingest_file("/tmp/x.md", "file text", ctx)

        mock_dispatch.assert_awaited_once()
        # Positional: (FileSource, user_id)
        source, passed_user_id = mock_dispatch.await_args.args
        assert isinstance(source, FileSource)
        assert source.path == "/tmp/x.md"
        assert passed_user_id == user_id


# Every retrieval tool of both modes, with the callable it delegates to. The two
# ``search_memory`` entries are the SAME tool name in different modes (#110):
# rag's goes to ``retrieve_parents``, graphrag's to the graph query.
@pytest.mark.parametrize(
    "tool,patch_target,query_result",
    [
        pytest.param(
            mcp_graph_tools.query_memory,
            "tree.mcp.graph_tools.execute_nl_query",
            [],
            id="query_memory",
        ),
        pytest.param(
            mcp_graph_tools.search_memory,
            "tree.mcp.graph_tools.structured_query_memory",
            QueryResult(nodes=[], edges=[]),
            id="graphrag_search_memory",
        ),
        pytest.param(
            mcp_graph_tools.deep_search_memory,
            "tree.mcp.graph_tools.structured_query_memory",
            QueryResult(nodes=[], edges=[]),
            id="deep_search_memory",
        ),
        pytest.param(
            mcp_tools.search_memory,
            "tree.mcp.tools.retrieve_parents",
            RetrievalResult(),
            id="rag_search_memory",
        ),
    ],
)
class TestQueryToolsPropagateUserId:
    async def test_passes_user_id_to_underlying_query(
        self, mocker, tool, patch_target, query_result
    ) -> None:
        user_id = PydanticObjectId()
        ctx = _make_ctx(user_id)
        mock_call = mocker.patch(
            patch_target, new_callable=AsyncMock, return_value=query_result
        )

        await tool(query="x", ctx=ctx)

        mock_call.assert_awaited_once()
        assert mock_call.await_args.kwargs["user_id"] == user_id


# ---------------------------------------------------------------------------
# Review tools — rollup #023 BLOCKER fix
# ---------------------------------------------------------------------------


class TestReviewListPendingPropagatesUserId:
    async def test_passes_user_id_to_find_pending_duplicates(self, mocker) -> None:
        user_id = PydanticObjectId()
        ctx = _make_ctx(user_id)

        mock_call = mocker.patch(
            "tree.mcp.graph_tools._find_pending_duplicates",
            new_callable=AsyncMock,
            return_value=[],
        )

        await mcp_graph_tools.review_list_pending(ctx=ctx, limit=10)

        mock_call.assert_awaited_once()
        assert mock_call.await_args.kwargs["user_id"] == user_id


class TestReviewConfirmPropagatesUserId:
    async def test_passes_user_id_to_review_duplicate(self, mocker) -> None:
        user_id = PydanticObjectId()
        ctx = _make_ctx(user_id)

        from tree.memory.graph.review.types import (
            MergeStrategy,
            ReviewDecision,
            ReviewResult,
        )

        mock_call = mocker.patch(
            "tree.mcp.graph_tools._review_duplicate",
            new_callable=AsyncMock,
            return_value=ReviewResult(
                decision=ReviewDecision.CONFIRM,
                winner_node_id="x:person:a",
                loser_node_id="x:person:b",
                applied_strategy=MergeStrategy.KEEP_PRIMARY,
                edges_transferred=0,
                same_as_edge_id="x:person:a|same_as|x:person:b",
            ),
        )

        await mcp_graph_tools.review_confirm(
            source_node_id="x:person:a",
            target_node_id="x:person:b",
            reviewed_by="reviewer",
            ctx=ctx,
        )

        mock_call.assert_awaited_once()
        assert mock_call.await_args.kwargs["user_id"] == user_id


class TestReviewRejectPropagatesUserId:
    async def test_passes_user_id_to_review_duplicate(self, mocker) -> None:
        user_id = PydanticObjectId()
        ctx = _make_ctx(user_id)

        from tree.memory.graph.review.types import ReviewDecision, ReviewResult

        mock_call = mocker.patch(
            "tree.mcp.graph_tools._review_duplicate",
            new_callable=AsyncMock,
            return_value=ReviewResult(
                decision=ReviewDecision.REJECT,
                winner_node_id=None,
                loser_node_id=None,
                applied_strategy=None,
                edges_transferred=0,
                same_as_edge_id="x:person:a|same_as|x:person:b",
            ),
        )

        await mcp_graph_tools.review_reject(
            source_node_id="x:person:a",
            target_node_id="x:person:b",
            reviewed_by="reviewer",
            ctx=ctx,
        )

        mock_call.assert_awaited_once()
        assert mock_call.await_args.kwargs["user_id"] == user_id
