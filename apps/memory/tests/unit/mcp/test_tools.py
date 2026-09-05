"""Unit tests for the MCP tools BOTH **Memory modes** register.

The graphrag-only tools moved to ``tests/unit/mcp/test_graph_tools.py`` with
their module (#110); ``search_memory`` here is the rag-mode function — the one
:mod:`tree.mcp.server` registers when ``memory.mode == "rag"``. Which of the two
gets registered per mode is asserted in ``test_tool_gating.py``.
"""

import json
from unittest.mock import AsyncMock, MagicMock

from beanie import PydanticObjectId

from tree.data.online_pipeline import UrlSource
from tree.mcp.tools import _ingest, search_memory
from tree.memory.rag.types import (
    DocumentMeta,
    MatchedChild,
    RetrievalResult,
    RetrievedParent,
)

_USER_ID = PydanticObjectId("507f1f77bcf86cd799439011")


class TestIngestTail:
    """``_ingest`` delegates to ``dispatch_online_pipeline`` and serializes to JSON.

    The submit contract itself (status derived from the new flow run, failures
    propagating) is the dispatcher's, covered in ``tests/unit/test_online.py``.
    """

    async def test_merges_dup_extra_into_the_dispatch_result(self, mocker):
        mock_dispatch = mocker.patch(
            "tree.mcp.tools.dispatch_online_pipeline",
            new_callable=AsyncMock,
            return_value={"status": "scheduled", "flow_run_id": "run-1"},
        )

        result = await _ingest(
            UrlSource(uri="https://example.com"),
            user_id=_USER_ID,
            dup_extra={"url": "https://example.com"},
        )

        assert json.loads(result) == {
            "status": "scheduled",
            "flow_run_id": "run-1",
            "url": "https://example.com",
        }
        mock_dispatch.assert_awaited_once_with(
            UrlSource(uri="https://example.com"), _USER_ID
        )


# ---------------------------------------------------------------------------
# rag-mode ``search_memory`` — **Parent-document retrieval** in, JSON out
# ---------------------------------------------------------------------------


def _make_ctx() -> MagicMock:
    ctx = MagicMock()
    ctx.lifespan_context = {
        "client": MagicMock(),
        "database": "test_db",
        "embedding_model": MagicMock(),
        "user_id": _USER_ID,
        "thread_id": "mcp-session-test",
    }
    return ctx


def _parent(parent_id: str, score: float) -> RetrievedParent:
    return RetrievedParent(
        parent_id=parent_id,
        chunk_index=1,
        heading_path=["Memory", "Parent retrieval"],
        content=f"Body of {parent_id}.",
        score=score,
        document=DocumentMeta(
            document_id="doc-1",
            title="How memory for agents works",
            source_type="substack",
            source_uri="https://example.com/p/memory",
            date="2026-01-02",
        ),
        matched_children=[
            MatchedChild(
                child_id=f"{parent_id}#child-0",
                chunk_index=0,
                content="A matching passage.",
                score=score,
            )
        ],
    )


class TestRagSearchMemory:
    """The rag answer surface: parents with their document metadata, as JSON."""

    async def test_returns_one_json_entry_per_retrieved_parent(self, mocker) -> None:
        mocker.patch(
            "tree.mcp.tools.retrieve_parents",
            new_callable=AsyncMock,
            return_value=RetrievalResult(
                parents=[_parent("parent-a", 0.032), _parent("parent-b", 0.016)]
            ),
        )

        result = await search_memory(query="parent chunk", ctx=_make_ctx(), top_k=3)

        payload = json.loads(result)
        assert len(payload["parents"]) == 2
        for parent in payload["parents"]:
            assert set(parent) == {
                "parent_id",
                "chunk_index",
                "heading_path",
                "content",
                "score",
                "document",
                "matched_children",
            }

    async def test_never_leaks_an_embedding(self, mocker) -> None:
        # The child rows the retrieval walked carry vectors; the answer must not.
        mocker.patch(
            "tree.mcp.tools.retrieve_parents",
            new_callable=AsyncMock,
            return_value=RetrievalResult(parents=[_parent("parent-a", 0.032)]),
        )

        result = await search_memory(query="parent chunk", ctx=_make_ctx(), top_k=3)

        assert "embedding" not in result

    async def test_no_hits_returns_an_empty_parents_array(self, mocker) -> None:
        mocker.patch(
            "tree.mcp.tools.retrieve_parents",
            new_callable=AsyncMock,
            return_value=RetrievalResult(),
        )

        result = await search_memory(query="nothing here", ctx=_make_ctx(), top_k=3)

        assert json.loads(result) == {"parents": []}

    async def test_top_k_is_the_result_cap_passed_to_retrieval(self, mocker) -> None:
        # ``top_k`` IS the cap in rag mode — there is no second ``max_results``.
        mock_retrieve = mocker.patch(
            "tree.mcp.tools.retrieve_parents",
            new_callable=AsyncMock,
            return_value=RetrievalResult(),
        )

        await search_memory(query="parent chunk", ctx=_make_ctx(), top_k=3)

        assert mock_retrieve.await_args.kwargs["top_k"] == 3

    async def test_docstring_states_the_parent_document_contract(self) -> None:
        # The docstring IS the tool description an MCP client shows the model.
        summary = " ".join(search_memory.__doc__.split())

        assert summary.startswith(
            "Hybrid (vector + text) search over child chunks, returning the "
            "distinct parent chunks with their document metadata."
        )
