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

from tree.entities.documents import Document, SourceType
from tree.mcp import tools as mcp_tools


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


def _make_document(user_id: PydanticObjectId) -> Document:
    return Document(
        user_id=user_id,
        source_type=SourceType.CONVERSATION,
        source_uri="conversation://abc",
        title="t",
        content="c",
    )


class TestIngestConversationPropagatesUserId:
    """The headline gap from plan.md: ingest_conversation must carry user_id."""

    async def test_passes_user_id_to_pipeline(self, mocker) -> None:
        user_id = PydanticObjectId()
        ctx = _make_ctx(user_id)
        doc = _make_document(user_id)

        mock_ingest = mocker.patch(
            "tree.mcp.tools._ingest_conversation",
            new_callable=AsyncMock,
            return_value=doc,
        )
        mock_run = mocker.patch(
            "tree.mcp.tools.run_ingestion_pipeline",
            new_callable=AsyncMock,
            return_value={"status": "ingested"},
        )

        await mcp_tools.ingest_conversation("Some text.", ctx)

        # The conversation flow receives the boot-pinned user_id.
        mock_ingest.assert_awaited_once()
        assert mock_ingest.await_args.args[1] == user_id
        # The ingestion summary pipeline is also scoped to the same user_id.
        assert mock_run.await_args.kwargs["user_id"] == user_id

    async def test_persisted_document_carries_user_id(self, mocker) -> None:
        user_id = PydanticObjectId()
        ctx = _make_ctx(user_id)
        doc = _make_document(user_id)

        mocker.patch(
            "tree.mcp.tools._ingest_conversation",
            new_callable=AsyncMock,
            return_value=doc,
        )
        mocker.patch(
            "tree.mcp.tools.run_ingestion_pipeline",
            new_callable=AsyncMock,
            return_value={"status": "ingested"},
        )

        await mcp_tools.ingest_conversation("Some text.", ctx)

        # The document built by the ingest_conversation flow carries the
        # boot-pinned user_id — this is the assertion the two-user
        # isolation test in #021 leans on.
        assert doc.user_id == user_id
        assert doc.source_type == SourceType.CONVERSATION


class TestIngestUrlPropagatesUserId:
    async def test_passes_user_id_to_dispatcher(self, mocker) -> None:
        user_id = PydanticObjectId()
        ctx = _make_ctx(user_id)
        doc = MagicMock()
        mock_dispatch = mocker.patch(
            "tree.mcp.tools._ingest_url_dispatch",
            new_callable=AsyncMock,
            return_value=doc,
        )
        mock_run = mocker.patch(
            "tree.mcp.tools.run_ingestion_pipeline",
            new_callable=AsyncMock,
            return_value={"status": "ingested"},
        )

        await mcp_tools.ingest_url("https://example.com", ctx)

        mock_dispatch.assert_awaited_once_with("https://example.com", user_id)
        assert mock_run.await_args.kwargs["user_id"] == user_id


class TestIngestFilePropagatesUserId:
    async def test_passes_user_id_to_pipeline(self, mocker) -> None:
        user_id = PydanticObjectId()
        ctx = _make_ctx(user_id)
        doc = MagicMock()
        mock_ingest = mocker.patch(
            "tree.mcp.tools._ingest_file",
            new_callable=AsyncMock,
            return_value=doc,
        )
        mocker.patch(
            "tree.mcp.tools.run_ingestion_pipeline",
            new_callable=AsyncMock,
            return_value={"status": "ingested"},
        )

        await mcp_tools.ingest_file("/tmp/x.md", ctx)

        mock_ingest.assert_awaited_once()
        # Positional: (file_path, user_id, title)
        assert mock_ingest.await_args.args[1] == user_id


@pytest.mark.parametrize(
    "tool_name,tool_kwargs",
    [
        ("query_memory", {"query": "x"}),
        ("search_memory", {"query": "x"}),
        ("deep_search_memory", {"query": "x"}),
    ],
)
class TestQueryToolsPropagateUserId:
    async def test_passes_user_id_to_underlying_query(
        self, mocker, tool_name, tool_kwargs
    ) -> None:
        user_id = PydanticObjectId()
        ctx = _make_ctx(user_id)

        # Patch the underlying query callable invoked by every tool.
        if tool_name == "query_memory":
            mock_call = mocker.patch(
                "tree.mcp.tools.execute_nl_query",
                new_callable=AsyncMock,
                return_value=[],
            )
        else:
            mock_call = mocker.patch(
                "tree.mcp.tools.structured_query_memory",
                new_callable=AsyncMock,
            )
            from tree.memory.types import QueryResult

            mock_call.return_value = QueryResult(nodes=[], edges=[])

        tool = getattr(mcp_tools, tool_name)
        await tool(ctx=ctx, **tool_kwargs)

        mock_call.assert_awaited_once()
        assert mock_call.await_args.kwargs["user_id"] == user_id


# ---------------------------------------------------------------------------
# Review tools — rollup #023 BLOCKER fix
# ---------------------------------------------------------------------------


class TestReviewListPendingPropagatesUserId:
    """``review_list_pending`` must propagate the server-pinned user_id."""

    async def test_passes_user_id_to_find_pending_duplicates(self, mocker) -> None:
        user_id = PydanticObjectId()
        ctx = _make_ctx(user_id)

        mock_call = mocker.patch(
            "tree.mcp.tools._find_pending_duplicates",
            new_callable=AsyncMock,
            return_value=[],
        )

        await mcp_tools.review_list_pending(ctx=ctx, limit=10)

        mock_call.assert_awaited_once()
        assert mock_call.await_args.kwargs["user_id"] == user_id


class TestReviewConfirmPropagatesUserId:
    """``review_confirm`` must propagate the server-pinned user_id."""

    async def test_passes_user_id_to_review_duplicate(self, mocker) -> None:
        user_id = PydanticObjectId()
        ctx = _make_ctx(user_id)

        from tree.memory.review.types import (
            MergeStrategy,
            ReviewDecision,
            ReviewResult,
        )

        mock_call = mocker.patch(
            "tree.mcp.tools._review_duplicate",
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

        await mcp_tools.review_confirm(
            source_node_id="x:person:a",
            target_node_id="x:person:b",
            reviewed_by="reviewer",
            ctx=ctx,
        )

        mock_call.assert_awaited_once()
        assert mock_call.await_args.kwargs["user_id"] == user_id


class TestReviewRejectPropagatesUserId:
    """``review_reject`` must propagate the server-pinned user_id."""

    async def test_passes_user_id_to_review_duplicate(self, mocker) -> None:
        user_id = PydanticObjectId()
        ctx = _make_ctx(user_id)

        from tree.memory.review.types import ReviewDecision, ReviewResult

        mock_call = mocker.patch(
            "tree.mcp.tools._review_duplicate",
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

        await mcp_tools.review_reject(
            source_node_id="x:person:a",
            target_node_id="x:person:b",
            reviewed_by="reviewer",
            ctx=ctx,
        )

        mock_call.assert_awaited_once()
        assert mock_call.await_args.kwargs["user_id"] == user_id
