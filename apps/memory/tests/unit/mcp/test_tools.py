"""Unit tests for the MCP tools BOTH **Memory modes** register.

The graphrag-only tools moved to ``tests/unit/mcp/test_graph_tools.py`` with
their module (#110); ``search_memory`` here is the rag-mode function — the one
:mod:`tree.mcp.server` registers when ``memory.mode == "rag"``. Which of the two
gets registered per mode is asserted in ``test_tool_gating.py``.

``visualize_memory_embeddings`` is here for the same reason it lives in
``tree.mcp.tools``: clustering is mode-orthogonal, so the map tool is part of
BOTH surfaces. Its payload builder is tested in
``tests/unit/memory/visualize/test_embeddings.py`` and its dual delivery in
``tests/unit/mcp/test_viz_app.py`` — what is asserted here is the tool's
contract: read, warn, deliver.
"""

import json
import logging
import re
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from beanie import PydanticObjectId
from fastmcp.tools import ToolResult
from prefect.exceptions import ObjectNotFound, PrefectHTTPStatusError
from pymongo.errors import PyMongoError, ServerSelectionTimeoutError

from tree.data.online_pipeline import UrlSource
from tree.mcp.tools import (
    _ingest,
    ingest_conversation,
    ingest_file,
    ingest_url,
    search_memory,
    visualize_memory_embeddings,
)
from tree.memory.clustering.types import EmbeddingMap, MapPoint, MemoryClusterInfo
from tree.memory.rag.types import (
    DocumentMeta,
    MatchedChild,
    RetrievalResult,
    RetrievedParent,
)
from tree.memory.rag.search import SearchUnavailableError
from tree.memory.visualize.embeddings import NO_CLUSTERING_RUN_MESSAGE
from tree.models.exceptions import ExtractionError, ModelError
from tree.online import IngestReceipt

_USER_ID = PydanticObjectId("507f1f77bcf86cd799439011")

# The two **Ingest receipt** shapes every ingest tool can answer with.
_DISPATCHED = IngestReceipt(
    source_uri="https://example.com",
    duplicate=False,
    document_id=None,
    flow_run_id="run-1",
    status="scheduled",
)
_DUPLICATE = IngestReceipt(
    source_uri="https://example.com",
    duplicate=True,
    document_id="507f1f77bcf86cd799439012",
    flow_run_id=None,
    status="duplicate",
)
_RECEIPT_KEYS = {"source_uri", "duplicate", "document_id", "flow_run_id", "status"}


def _tool_fn(tool):
    """Unwrap FastMCP's ``FunctionTool`` back to the coroutine it registered."""

    return getattr(tool, "fn", tool)


class TestIngestTail:
    """``_ingest`` delegates to ``dispatch_online_pipeline`` and serializes to JSON.

    The submit contract itself (status derived from the new flow run, failures
    propagating) is the dispatcher's, covered in ``tests/unit/test_online.py``.
    """

    async def test_merges_dup_extra_into_the_receipt(self, mocker):
        mock_dispatch = mocker.patch(
            "tree.mcp.tools.dispatch_online_pipeline",
            new_callable=AsyncMock,
            return_value=_DISPATCHED,
        )

        result = await _ingest(
            UrlSource(uri="https://example.com"),
            user_id=_USER_ID,
            dup_extra={"url": "https://example.com"},
        )

        assert json.loads(result) == {
            "source_uri": "https://example.com",
            "duplicate": False,
            "document_id": None,
            "flow_run_id": "run-1",
            "status": "scheduled",
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

    async def test_no_hits_answers_nothing_found_with_the_mode(self, mocker) -> None:
        # ADR-008 §3: the model reads the WORD, not the empty list — an off-topic
        # query and a half-dead index must not look the same.
        mocker.patch(
            "tree.mcp.tools.retrieve_parents",
            new_callable=AsyncMock,
            return_value=RetrievalResult(outcome="nothing_found"),
        )

        result = await search_memory(query="nothing here", ctx=_make_ctx(), top_k=3)

        assert json.loads(result) == {
            "parents": [],
            "outcome": "nothing_found",
            "search_mode": "hybrid",
        }

    async def test_top_k_is_the_result_cap_passed_to_retrieval(self, mocker) -> None:
        # ``top_k`` IS the cap in rag mode — there is no second ``max_results``.
        mock_retrieve = mocker.patch(
            "tree.mcp.tools.retrieve_parents",
            new_callable=AsyncMock,
            return_value=RetrievalResult(),
        )

        await search_memory(query="parent chunk", ctx=_make_ctx(), top_k=3)

        assert mock_retrieve.await_args.kwargs["top_k"] == 3

    @pytest.mark.parametrize("query", ["", "   ", "\n\t "])
    async def test_blank_query_returns_the_standard_invalid_input_envelope(
        self, mocker, query: str
    ) -> None:
        # #112 Issue 6: a blank query used to reach Voyage and surface its raw
        # "400 Input cannot contain empty strings" text, while every sibling
        # tool answers with the **Tool error envelope**.
        mock_retrieve = mocker.patch(
            "tree.mcp.tools.retrieve_parents", new_callable=AsyncMock
        )

        result = await search_memory(query=query, ctx=_make_ctx(), top_k=3)

        assert json.loads(result) == {
            "error_type": "invalid_input",
            "retryable": False,
            "message": "query must not be empty",
        }
        mock_retrieve.assert_not_awaited()

    async def test_docstring_states_the_parent_document_contract(self) -> None:
        # The docstring IS the tool description an MCP client shows the model.
        summary = " ".join(search_memory.__doc__.split())

        assert summary.startswith(
            "Hybrid (vector + text) search over child chunks, returning the "
            "distinct parent chunks with their document metadata."
        )


class TestSearchMemoryErrors:
    """The retrieval boundary (ADR-008 §2): a JSON answer, never a protocol error.

    A model that gets an MCP protocol error learns nothing; it gets the
    **Tool error envelope** instead, and reads ``retryable`` to decide whether
    to call again. The server log keeps the traceback either way.
    """

    @pytest.mark.parametrize(
        "exc",
        [
            SearchUnavailableError("vector and text search are both unavailable"),
            PyMongoError("connection refused"),
            ExtractionError("Voyage embedding call failed"),
            ModelError("Failed to resolve Modal web URL"),
        ],
        ids=["search-legs", "pymongo", "voyage", "modal"],
    )
    async def test_unavailable_retrieval_is_a_retryable_envelope(
        self, mocker, exc: Exception
    ) -> None:
        mocker.patch(
            "tree.mcp.tools.retrieve_parents",
            new_callable=AsyncMock,
            side_effect=exc,
        )

        payload = json.loads(
            await search_memory(query="prefect", ctx=_make_ctx(), top_k=3)
        )

        assert payload["error_type"] == "search_unavailable"
        assert payload["retryable"] is True
        assert set(payload) == {"error_type", "retryable", "message"}

    async def test_any_other_failure_is_a_non_retryable_internal_error(
        self, mocker
    ) -> None:
        mocker.patch(
            "tree.mcp.tools.retrieve_parents",
            new_callable=AsyncMock,
            side_effect=RuntimeError("parent lookup blew up"),
        )

        payload = json.loads(
            await search_memory(query="prefect", ctx=_make_ctx(), top_k=3)
        )

        assert payload["error_type"] == "internal_error"
        assert payload["retryable"] is False

    @pytest.mark.parametrize(
        "exc",
        [SearchUnavailableError("both legs down"), RuntimeError("boom")],
        ids=["unavailable", "internal"],
    )
    async def test_both_envelopes_log_the_traceback_at_error(
        self, mocker, caplog, exc: Exception
    ) -> None:
        # ``exc_info`` is what separates ``logger.exception`` from a bare
        # ``logger.error``: without it, whoever is on call gets a one-line
        # message and no stack for a failure the model silently retried past.
        mocker.patch(
            "tree.mcp.tools.retrieve_parents",
            new_callable=AsyncMock,
            side_effect=exc,
        )

        with caplog.at_level(logging.ERROR, logger="tree.mcp.tools"):
            await search_memory(query="prefect", ctx=_make_ctx(), top_k=3)

        errors = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert errors, "the tool swallowed the failure without an ERROR log"
        assert all(r.exc_info is not None for r in errors)


class TestDispatchErrors:
    """The dispatch boundary (ADR-008 §2): Prefect down vs never served.

    Both are infrastructure, but only ONE is worth retrying — the model must
    not loop on a deployment nobody registered.
    """

    @pytest.mark.parametrize(
        "exc",
        [
            httpx.ConnectError("connection refused"),
            httpx.TimeoutException("timed out"),
            PrefectHTTPStatusError(
                "server error",
                request=httpx.Request("POST", "http://localhost:4200/api"),
                response=httpx.Response(
                    502, request=httpx.Request("POST", "http://localhost:4200/api")
                ),
            ),
        ],
        ids=["connect-error", "timeout", "prefect-status"],
    )
    async def test_unreachable_prefect_is_pipeline_unavailable(
        self, mocker, exc: Exception
    ) -> None:
        mocker.patch(
            "tree.mcp.tools.dispatch_online_pipeline",
            new_callable=AsyncMock,
            side_effect=exc,
        )

        payload = json.loads(
            await _tool_fn(ingest_url)("https://example.com/post", _make_ctx())
        )

        assert payload == {
            "error_type": "pipeline_unavailable",
            "retryable": True,
            "message": "Prefect API unreachable — try again",
        }

    async def test_unregistered_deployment_is_a_configuration_error(
        self, mocker
    ) -> None:
        mocker.patch(
            "tree.mcp.tools.dispatch_online_pipeline",
            new_callable=AsyncMock,
            side_effect=ObjectNotFound("deployment not found"),
        )

        payload = json.loads(
            await _tool_fn(ingest_url)("https://example.com/post", _make_ctx())
        )

        assert payload["error_type"] == "configuration_error"
        # Not retryable: the same call fails identically until an operator acts,
        # so the message names the deployment AND the command that registers it.
        assert payload["retryable"] is False
        assert "online-pipeline/online-pipeline" in payload["message"]
        assert "make memory-serve-workflows" in payload["message"]

    async def test_mongo_down_on_preflight_is_storage_unavailable(self, mocker) -> None:
        # The dispatcher's pre-flight ``Document.find_one`` runs BEFORE Prefect,
        # so an unreachable memory store is neither ``pipeline_unavailable`` nor
        # ``search_unavailable`` — nothing was searched and nothing dispatched.
        mocker.patch(
            "tree.online.Document.find_one",
            new_callable=AsyncMock,
            side_effect=ServerSelectionTimeoutError("no servers available"),
        )
        mock_run = mocker.patch("tree.online.run_deployment", new_callable=AsyncMock)

        payload = json.loads(
            await _tool_fn(ingest_url)("https://example.com/post", _make_ctx())
        )

        assert payload == {
            "error_type": "storage_unavailable",
            "retryable": True,
            "message": "memory store unreachable — try again",
        }
        mock_run.assert_not_awaited()

    @pytest.mark.parametrize(
        "tool,args",
        [
            (ingest_url, ("https://example.com/post",)),
            (ingest_file, ("/tmp/notes.md", "body")),
            (ingest_conversation, ("Alice likes Python.",)),
        ],
        ids=["ingest_url", "ingest_file", "ingest_conversation"],
    )
    async def test_every_ingest_tool_shares_the_dispatch_boundary(
        self, mocker, tool, args: tuple
    ) -> None:
        # The catch lives in ``_ingest``, so all three answer identically — a
        # per-tool copy is exactly what would drift.
        mocker.patch(
            "tree.mcp.tools.dispatch_online_pipeline",
            new_callable=AsyncMock,
            side_effect=httpx.ConnectError("connection refused"),
        )

        payload = json.loads(await _tool_fn(tool)(*args, _make_ctx()))

        assert payload["error_type"] == "pipeline_unavailable"
        assert payload["retryable"] is True


# ---------------------------------------------------------------------------
# ``visualize_memory_embeddings`` — the **Embedding map**, in BOTH modes
# ---------------------------------------------------------------------------


def _viz_ctx(*, ui_supported: bool) -> MagicMock:
    ctx = _make_ctx()
    ctx.client_supports_extension.return_value = ui_supported
    return ctx


def _embedding_map(*, unclustered: int = 0, total_children: int = 12) -> EmbeddingMap:
    """A two-cluster map with ``total_children - unclustered`` drawn points."""

    drawn = total_children - unclustered
    return EmbeddingMap(
        run_id="run-1",
        clusters=[
            MemoryClusterInfo(
                cluster_id=0,
                label="Agent memory design",
                summary="How agents remember.",
                keywords=["memory", "agents", "design"],
                size=drawn,
                sample_chunk_ids=["chunk-0"],
                centroid_x=0.5,
                centroid_y=0.5,
            )
        ],
        points=[
            MapPoint(
                chunk_id=f"chunk-{index}",
                x=float(index),
                y=float(index) / 2,
                cluster_id=0,
                title="Memory for AI Agents",
                heading_path=["Memory"],
                snippet=f"Snippet {index}.",
            )
            for index in range(drawn)
        ],
        total_children=total_children,
        unclustered=unclustered,
    )


class TestVisualizeMemoryEmbeddings:
    """READS the latest **Clustering run** and delivers it (ADR-007 §8)."""

    async def test_no_clustering_run_answers_with_the_command_to_run(
        self, mocker
    ) -> None:
        # Story 4: a fresh user gets an explanation, never an empty canvas —
        # and never a ToolResult, because there is no payload to deliver.
        mocker.patch(
            "tree.mcp.tools.load_embedding_map",
            new_callable=AsyncMock,
            return_value=None,
        )
        deliver = mocker.patch("tree.mcp.tools._graph_tool_result")

        result = await visualize_memory_embeddings(ctx=_viz_ctx(ui_supported=True))

        assert result == NO_CLUSTERING_RUN_MESSAGE
        assert isinstance(result, str)
        deliver.assert_not_called()

    async def test_a_ui_capable_client_gets_the_map_inline_with_hulls_on(
        self, mocker
    ) -> None:
        # Story 2: the iframe gets the full payload (fixed layout, hulls on);
        # the model reads only the summary line.
        mocker.patch(
            "tree.mcp.tools.load_embedding_map",
            new_callable=AsyncMock,
            return_value=_embedding_map(),
        )

        result = await visualize_memory_embeddings(
            ctx=_viz_ctx(ui_supported=True), hulls=True
        )

        assert isinstance(result, ToolResult)
        summary_block, payload_block = result.content
        assert summary_block.text.startswith("Embedding map:")
        assert payload_block.annotations.audience == ["user"]
        payload = json.loads(payload_block.text)
        assert payload["layout"] == "fixed"
        assert payload["hulls"] is True

    async def test_hulls_default_off_leaves_the_toggle_unchecked(self, mocker) -> None:
        mocker.patch(
            "tree.mcp.tools.load_embedding_map",
            new_callable=AsyncMock,
            return_value=_embedding_map(),
        )

        result = await visualize_memory_embeddings(ctx=_viz_ctx(ui_supported=True))

        assert json.loads(result.content[1].text)["hulls"] is False

    async def test_a_stale_map_answers_with_the_warning_line_first(
        self, mocker
    ) -> None:
        # Story 3: the model's FIRST line says the map under-reports the corpus,
        # so an agent relaying one line still relays the warning.
        mocker.patch(
            "tree.mcp.tools.load_embedding_map",
            new_callable=AsyncMock,
            return_value=_embedding_map(unclustered=2),
        )

        result = await visualize_memory_embeddings(ctx=_viz_ctx(ui_supported=True))

        assert result.content[0].text.startswith(
            "2 of 12 chunks have no cluster assignment (or a stale one) — run "
            "make memory-run-clustering-pipeline"
        )
        assert "Embedding map:" in result.content[0].text

    async def test_a_non_ui_client_gets_a_file_and_its_graphs_resource_link(
        self, mocker, tmp_path
    ) -> None:
        # Story 1: the Claude Code terminal renders no MCP App UI, so the same
        # map arrives as a self-contained file plus a downloadable resource.
        mocker.patch("tree.memory.visualize.graph.GRAPHS_DIR", tmp_path)
        mocker.patch("tree.mcp.viz_app.webbrowser.open", return_value=False)
        mocker.patch(
            "tree.mcp.tools.load_embedding_map",
            new_callable=AsyncMock,
            return_value=_embedding_map(),
        )

        result = await visualize_memory_embeddings(ctx=_viz_ctx(ui_supported=False))

        _, link_block = result.content
        assert link_block.type == "resource_link"
        assert re.fullmatch(
            r"graphs://embedding-map-\d{8}-\d{6}\.html", str(link_block.uri)
        ), link_block.uri
        assert (tmp_path / link_block.name).is_file()

    async def test_the_delivered_copy_calls_the_picture_a_map_not_a_graph(
        self, mocker, tmp_path
    ) -> None:
        # Story 1 in rag mode: there is no graph anywhere in this server, so
        # "I saved a self-contained interactive graph" is a lie the model
        # relays. Both delivery branches name the **Embedding map**.
        mocker.patch("tree.memory.visualize.graph.GRAPHS_DIR", tmp_path)
        mocker.patch("tree.mcp.viz_app.webbrowser.open", return_value=False)
        mocker.patch(
            "tree.mcp.tools.load_embedding_map",
            new_callable=AsyncMock,
            return_value=_embedding_map(),
        )

        answer = await visualize_memory_embeddings(ctx=_viz_ctx(ui_supported=False))
        inline = await visualize_memory_embeddings(ctx=_viz_ctx(ui_supported=True))

        text_block, link_block = answer.content
        assert "self-contained interactive embedding map to:" in text_block.text
        assert "interactive graph" not in text_block.text
        assert link_block.description == (
            "Self-contained interactive embedding map (download me)"
        )
        assert inline.content[0].text.endswith("(interactive embedding map view).")

    async def test_as_html_file_forces_the_file_branch_for_a_ui_client(
        self, mocker, tmp_path
    ) -> None:
        mocker.patch("tree.memory.visualize.graph.GRAPHS_DIR", tmp_path)
        mocker.patch("tree.mcp.viz_app.webbrowser.open", return_value=False)
        mocker.patch(
            "tree.mcp.tools.load_embedding_map",
            new_callable=AsyncMock,
            return_value=_embedding_map(),
        )

        result = await visualize_memory_embeddings(
            ctx=_viz_ctx(ui_supported=True), as_html_file=True
        )

        assert result.content[1].type == "resource_link"
        assert list(tmp_path.glob("embedding-map-*.html"))

    async def test_the_docstring_tells_the_model_when_to_draw_a_map(self) -> None:
        # The docstring IS the tool description an MCP client shows the model.
        summary = " ".join(visualize_memory_embeddings.__doc__.split())

        assert summary.startswith(
            "Show the memory's embedding space as a 2D map: every child chunk "
            "is a point, coloured by its cluster from the latest clustering run"
        )
        assert "no clustering run exists" in summary


class TestIngestReceipt:
    """Every ingest tool answers the **Ingest receipt** — and nothing else.

    A model reads ``duplicate`` to tell "already in memory" from "on its way",
    and ``source_uri`` is the key an operator retries with, so the key set is
    the contract (ADR-008 §1): the receipt's five fields plus the tool's own
    echo (``url`` / ``file_path``).
    """

    @pytest.mark.parametrize(
        "receipt", [_DISPATCHED, _DUPLICATE], ids=["dispatched", "duplicate"]
    )
    async def test_ingest_url_answers_the_receipt_plus_the_url(
        self, mocker, receipt: IngestReceipt
    ) -> None:
        mocker.patch(
            "tree.mcp.tools.dispatch_online_pipeline",
            new_callable=AsyncMock,
            return_value=receipt,
        )

        payload = json.loads(
            await _tool_fn(ingest_url)("https://example.com", _make_ctx())
        )

        assert set(payload) == _RECEIPT_KEYS | {"url"}
        assert payload == {**receipt.model_dump(), "url": "https://example.com"}

    @pytest.mark.parametrize(
        "receipt", [_DISPATCHED, _DUPLICATE], ids=["dispatched", "duplicate"]
    )
    async def test_ingest_file_answers_the_receipt_plus_the_path(
        self, mocker, receipt: IngestReceipt
    ) -> None:
        mocker.patch(
            "tree.mcp.tools.dispatch_online_pipeline",
            new_callable=AsyncMock,
            return_value=receipt,
        )

        payload = json.loads(
            await _tool_fn(ingest_file)("/tmp/notes.md", "body", _make_ctx())
        )

        assert set(payload) == _RECEIPT_KEYS | {"file_path"}
        assert payload == {**receipt.model_dump(), "file_path": "/tmp/notes.md"}

    @pytest.mark.parametrize(
        "receipt", [_DISPATCHED, _DUPLICATE], ids=["dispatched", "duplicate"]
    )
    async def test_ingest_conversation_answers_the_receipt_alone(
        self, mocker, receipt: IngestReceipt
    ) -> None:
        mocker.patch(
            "tree.mcp.tools.dispatch_online_pipeline",
            new_callable=AsyncMock,
            return_value=receipt,
        )

        payload = json.loads(
            await _tool_fn(ingest_conversation)("Alice likes Python.", _make_ctx())
        )

        # No echo field: the conversation text is not an identifier.
        assert set(payload) == _RECEIPT_KEYS
        assert payload == receipt.model_dump()
