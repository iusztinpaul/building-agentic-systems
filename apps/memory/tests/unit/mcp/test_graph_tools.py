"""Unit tests for the graphrag-only MCP tools (:mod:`tree.mcp.graph_tools`).

Registered only when the server boots in graphrag mode (#110); the behaviour
asserted here — the dual graph delivery of ``query_memory`` /
``search_memory(visualize=True)``, the rendering-channel contract of
``visualize_memory_graph`` (which moved here with the neutral-MCP-App split,
ADR-007 §7) and the embedding-stripping serializer — is unchanged by either
move.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from bson import ObjectId
from fastmcp.tools import ToolResult

from pymongo.errors import PyMongoError, ServerSelectionTimeoutError

from tree.mcp.graph_tools import (
    _serialize,
    deep_search_memory,
    query_memory,
    review_confirm,
    review_list_pending,
    review_reject,
    search_memory,
    visualize_memory_graph,
)
from tree.mcp.server import mcp
from tree.mcp.viz_app import GRAPH_VIEW_URI
from tree.memory.rag.search import SearchUnavailableError
from tree.memory.types import QueryResult


class TestSerialize:
    def test_strips_embedding_field(self):
        docs = [
            {"_id": "person:alice", "name": "Alice", "embedding": [0.1, 0.2]},
            {"_id": "person:bob", "name": "Bob", "embedding": [0.3, 0.4]},
        ]
        result = _serialize(docs)

        assert "embedding" not in result
        assert "Alice" in result
        assert "Bob" in result

    def test_handles_objectid(self):
        oid = ObjectId("507f1f77bcf86cd799439011")
        docs = [{"_id": "person:alice", "source": oid}]
        result = _serialize(docs)

        assert "507f1f77bcf86cd799439011" in result

    def test_handles_datetime(self):
        dt = datetime(2024, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
        docs = [{"_id": "person:alice", "created_at": dt}]
        result = _serialize(docs)

        assert "2024" in result

    def test_empty_list(self):
        result = _serialize([])

        assert result == "[]"

    def test_does_not_mutate_original(self):
        docs = [{"_id": "person:alice", "embedding": [0.1]}]
        _serialize(docs)

        assert "embedding" in docs[0]


# ---------------------------------------------------------------------------
# query_memory / search_memory with visualize=True — the dual delivery
# (ADR-005, decision 4): both tools route through the ONE shared seam
# ``_dual_graph_result`` → ``viz_app._graph_tool_result``, so from a
# visualization standpoint they behave exactly like visualize_memory_graph.
# ---------------------------------------------------------------------------

_GRAPH_UID = "65f1a2b3c4d5e6f7a8b9c0d1"


def _node(node_id: str, node_type: str) -> dict[str, Any]:
    return {
        "_id": f"{_GRAPH_UID}:{node_type}:{node_id}",
        "kind": "node",
        "type": node_type,
        "properties": {"name": node_id},
    }


def _edge(source: str, edge_type: str, target: str) -> dict[str, Any]:
    return {
        "_id": f"{source}|{edge_type}|{target}",
        "kind": "edge",
        "type": edge_type,
        "source_node_id": source,
        "target_node_id": target,
    }


_ALICE = f"{_GRAPH_UID}:person:alice"
_PAPER = f"{_GRAPH_UID}:document:paper"
_GRAPH_DOCS = [
    _node("alice", "person"),
    _node("paper", "document"),
    _edge(_ALICE, "mentions", _PAPER),
]


def _tool_fn(tool):
    """Unwrap FastMCP's ``FunctionTool`` back to the coroutine it registered."""

    return getattr(tool, "fn", tool)


def _make_graph_ctx(*, ui_supported: bool) -> MagicMock:
    ctx = MagicMock()
    ctx.client_supports_extension.return_value = ui_supported
    ctx.lifespan_context = {
        "client": MagicMock(),
        "database": "test_db",
        "llm": MagicMock(),
        "embedding_model": MagicMock(),
        "user_id": _GRAPH_UID,
    }
    return ctx


def _seed_result() -> QueryResult:
    """The two-node / one-edge graph ``visualize_memory_graph`` is stubbed with."""

    return QueryResult(
        nodes=[d for d in _GRAPH_DOCS if d["kind"] == "node"],
        edges=[d for d in _GRAPH_DOCS if d["kind"] == "edge"],
    )


def _content_payload(result: ToolResult) -> dict[str, Any]:
    """Extract the JSON payload block the iframe reads (mirrors its JS)."""

    for block in result.content:
        if block.type == "text":
            try:
                parsed = json.loads(block.text)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict) and isinstance(parsed.get("nodes"), list):
                return parsed
    raise AssertionError("No JSON payload content block found in tool result.")


def _patch_query(mocker, tool_name: str, docs: list[dict[str, Any]]) -> None:
    """Stub whichever query engine the tool under test delegates to."""

    if tool_name == "query_memory":
        mocker.patch(
            "tree.mcp.graph_tools.execute_nl_query", new=AsyncMock(return_value=docs)
        )
        return
    edges = [d for d in docs if d.get("kind") == "edge"]
    nodes = [d for d in docs if d.get("kind") != "edge"]
    mocker.patch(
        "tree.mcp.graph_tools.structured_query_memory",
        new=AsyncMock(return_value=QueryResult(nodes=nodes, edges=edges)),
    )


_TOOLS = [("query_memory", query_memory), ("search_memory", search_memory)]


@pytest.mark.parametrize("tool_name,tool", _TOOLS)
class TestGraphToolsDualDelivery:
    async def test_visualize_false_returns_the_plain_serialized_string(
        self, mocker, tool_name, tool
    ) -> None:
        _patch_query(mocker, tool_name, _GRAPH_DOCS)
        ctx = _make_graph_ctx(ui_supported=True)

        result = await tool(query="alice", ctx=ctx)

        # Assert: unchanged contract — no ToolResult, no graph, no iframe payload.
        assert isinstance(result, str)
        assert not isinstance(result, ToolResult)
        assert result == _serialize(_GRAPH_DOCS)

    async def test_visualize_keeps_serialized_output_visible_to_the_model(
        self, mocker, tool_name, tool
    ) -> None:
        _patch_query(mocker, tool_name, _GRAPH_DOCS)
        ctx = _make_graph_ctx(ui_supported=True)

        result = await tool(query="alice", ctx=ctx, visualize=True)

        # Assert: the tool still answers the question — the serialized rows are
        # in the model-visible block, not swapped out for the graph.
        assert isinstance(result, ToolResult)
        assert _serialize(_GRAPH_DOCS) in result.content[0].text

    async def test_visualize_ships_the_graph_payload_to_the_iframe_only(
        self, mocker, tool_name, tool
    ) -> None:
        _patch_query(mocker, tool_name, _GRAPH_DOCS)
        ctx = _make_graph_ctx(ui_supported=True)

        result = await tool(query="alice", ctx=ctx, visualize=True)

        # Assert: node/edge dump rides in the audience=["user"] block.
        payload_block = result.content[1]
        assert payload_block.annotations.audience == ["user"]
        payload = json.loads(payload_block.text)
        assert len(payload["nodes"]) == 2
        assert len(payload["edges"]) == 1
        assert result.structured_content == payload

    async def test_visualize_falls_back_to_a_file_and_resource_link(
        self, mocker, tool_name, tool, tmp_path: Path
    ) -> None:
        # Arrange: a client that renders no MCP App UIs (e.g. the terminal).
        _patch_query(mocker, tool_name, _GRAPH_DOCS)
        mocker.patch("tree.memory.visualize.graph.GRAPHS_DIR", tmp_path)
        mocker.patch("tree.mcp.viz_app.webbrowser.open", return_value=False)
        ctx = _make_graph_ctx(ui_supported=False)

        result = await tool(query="alice", ctx=ctx, visualize=True)

        # Assert: serialized rows + the server-side path + the download link.
        text_block, link_block = result.content
        assert _serialize(_GRAPH_DOCS) in text_block.text
        assert str(tmp_path) in text_block.text
        assert str(link_block.uri).startswith("graphs://")
        rendered = tmp_path / str(link_block.uri).removeprefix("graphs://")
        assert rendered.is_file()

    async def test_docs_without_kind_skip_the_graph_and_stay_a_plain_string(
        self, mocker, tool_name, tool
    ) -> None:
        # Arrange: an aggregation / projection that dropped the ``kind`` field.
        docs = [{"_id": "agg", "count": 7}]
        _patch_query(mocker, tool_name, docs)
        ctx = _make_graph_ctx(ui_supported=True)

        result = await tool(query="how many", ctx=ctx, visualize=True)

        assert isinstance(result, str)
        assert not isinstance(result, ToolResult)
        assert result.startswith(_serialize(docs))
        assert "Visualization skipped" in result


# ---------------------------------------------------------------------------
# visualize_memory_graph — the graphrag-only tool, moved here with the split
# (ADR-007 §7): the rendering-channel contract of the tool itself. The helper
# it delivers through lives in the neutral ``viz_app`` and is tested there.
# ---------------------------------------------------------------------------


async def test_all_three_graph_tools_declare_the_shared_ui_resource() -> None:
    # Arrange / Act: importing ``tree.mcp.graph_tools`` (module level) registered
    # all three graph tools on the server.
    uris = {
        name: ((await mcp.get_tool(name)).meta or {}).get("ui", {}).get("resourceUri")
        for name in ("visualize_memory_graph", "query_memory", "search_memory")
    }

    # Assert: one ui:// resource serves all three (ADR-005, decision 4).
    assert set(uris.values()) == {GRAPH_VIEW_URI}
    assert (await mcp.get_resource(GRAPH_VIEW_URI)) is not None


# ---------------------------------------------------------------------------
# visualize_memory_graph tool — rendering-channel contract
# ---------------------------------------------------------------------------


async def test_visualize_ships_payload_in_content_block_for_ui_clients(
    mocker,
) -> None:
    mocker.patch(
        "tree.mcp.graph_tools.structured_query_memory",
        new=AsyncMock(return_value=_seed_result()),
    )
    ctx = _make_graph_ctx(ui_supported=True)

    result = await visualize_memory_graph(ctx, query="alice")

    # Assert: payload rides in a content JSON block (the channel the iframe
    # actually receives), marked audience=["user"] so the model skips it.
    assert isinstance(result, ToolResult)
    payload = _content_payload(result)
    assert len(payload["nodes"]) == 2
    assert len(payload["edges"]) == 1
    json_block = next(
        b for b in result.content if b.type == "text" and b.text.startswith("{")
    )
    assert json_block.annotations.audience == ["user"]
    assert result.structured_content is not None


async def test_visualize_empty_query_fetches_full_graph(mocker) -> None:
    query_mock = mocker.patch(
        "tree.mcp.graph_tools.structured_query_memory", new=AsyncMock()
    )
    full_graph_mock = mocker.patch(
        "tree.mcp.graph_tools.fetch_full_graph",
        new=AsyncMock(return_value=_seed_result()),
    )
    ctx = _make_graph_ctx(ui_supported=True)

    result = await visualize_memory_graph(ctx)

    full_graph_mock.assert_awaited_once()
    query_mock.assert_not_awaited()
    assert "your full memory" in result.content[0].text


async def test_visualize_fallback_returns_path_and_resource_link(
    mocker, tmp_path: Path
) -> None:
    # Arrange: no UI extension → file fallback (browser-open suppressed).
    mocker.patch(
        "tree.mcp.graph_tools.structured_query_memory",
        new=AsyncMock(return_value=_seed_result()),
    )
    mocker.patch("tree.memory.visualize.graph.GRAPHS_DIR", tmp_path)
    mocker.patch("tree.mcp.viz_app.webbrowser.open", return_value=False)
    ctx = _make_graph_ctx(ui_supported=False)

    result = await visualize_memory_graph(ctx, query="alice")

    # Assert: the text block carries the server-side path; the resource link
    # lets a client of a REMOTE server download the same HTML over MCP.
    assert isinstance(result, ToolResult)
    text_block, link_block = result.content
    assert str(tmp_path) in text_block.text
    assert link_block.type == "resource_link"
    assert str(link_block.uri).startswith("graphs://")
    assert link_block.mimeType == "text/html"
    rendered = tmp_path / str(link_block.uri).removeprefix("graphs://")
    assert rendered.is_file()


async def test_visualize_as_html_file_forces_fallback_for_ui_clients(
    mocker, tmp_path: Path
) -> None:
    # Arrange: UI extension present but the caller asked for a file.
    mocker.patch(
        "tree.mcp.graph_tools.structured_query_memory",
        new=AsyncMock(return_value=_seed_result()),
    )
    mocker.patch("tree.memory.visualize.graph.GRAPHS_DIR", tmp_path)
    mocker.patch("tree.mcp.viz_app.webbrowser.open", return_value=False)
    ctx = _make_graph_ctx(ui_supported=True)

    result = await visualize_memory_graph(ctx, query="alice", as_html_file=True)

    assert result.content[0].text.count("you asked for an HTML file") == 1
    assert result.content[1].type == "resource_link"


# ---------------------------------------------------------------------------
# The retrieval boundary — the SAME two envelopes as rag mode (ADR-008 §2)
# ---------------------------------------------------------------------------


# Every graphrag reader, with the engine it delegates to and the args that
# reach it. ``visualize_memory_graph`` is listed on its query path — the shape
# the story describes — and its no-query path is covered separately below.
_READERS = [
    ("search_memory", search_memory, "structured_query_memory", {"query": "prefect"}),
    ("query_memory", query_memory, "execute_nl_query", {"query": "who is paul?"}),
    (
        "deep_search_memory",
        deep_search_memory,
        "structured_query_memory",
        {"query": "prefect"},
    ),
    (
        "visualize_memory_graph",
        visualize_memory_graph,
        "structured_query_memory",
        {"query": "prefect"},
    ),
]


@pytest.mark.parametrize(
    "name,tool,engine,kwargs", _READERS, ids=[r[0] for r in _READERS]
)
class TestReaderErrors:
    """Mongo down / a dead embedding provider reads the same in BOTH modes.

    The graph readers import the one helper from :mod:`tree.mcp.tools`, so the
    codes cannot drift from the rag ``search_memory`` — that sameness IS the
    contract a model relies on when it moves between servers.
    """

    async def test_unavailable_retrieval_is_a_retryable_envelope(
        self, mocker, name: str, tool, engine: str, kwargs: dict[str, Any]
    ) -> None:
        mocker.patch(
            f"tree.mcp.graph_tools.{engine}",
            new=AsyncMock(side_effect=SearchUnavailableError("both legs down")),
        )

        payload = json.loads(
            await tool(ctx=_make_graph_ctx(ui_supported=False), **kwargs)
        )

        assert payload["error_type"] == "search_unavailable"
        assert payload["retryable"] is True
        assert set(payload) == {"error_type", "retryable", "message"}

    async def test_any_other_failure_is_a_non_retryable_internal_error(
        self, mocker, name: str, tool, engine: str, kwargs: dict[str, Any]
    ) -> None:
        mocker.patch(
            f"tree.mcp.graph_tools.{engine}",
            new=AsyncMock(side_effect=RuntimeError("aggregation blew up")),
        )

        payload = json.loads(
            await tool(ctx=_make_graph_ctx(ui_supported=False), **kwargs)
        )

        assert payload["error_type"] == "internal_error"
        assert payload["retryable"] is False


async def test_full_graph_visualization_shares_the_retrieval_envelope(mocker) -> None:
    # No query = no search, but the SAME Mongo: an unreachable database must not
    # answer differently just because the caller omitted a query.
    mocker.patch(
        "tree.mcp.graph_tools.fetch_full_graph",
        new=AsyncMock(side_effect=PyMongoError("connection refused")),
    )

    payload = json.loads(
        await visualize_memory_graph(_make_graph_ctx(ui_supported=False))
    )

    assert payload["error_type"] == "search_unavailable"
    assert payload["retryable"] is True


class TestReviewToolErrors:
    """The review queue answers the envelope when Mongo is down, too.

    These three are the boundary ADR-008 §2 did not name, and they are NOT
    retrieval: ``storage_unavailable`` says the memory STORE is unreachable, so
    a model that just tried to confirm a duplicate does not conclude that search
    is broken.
    """

    async def test_review_list_pending_reports_storage_unavailable(
        self, mocker
    ) -> None:
        mocker.patch(
            "tree.mcp.graph_tools._find_pending_duplicates",
            new=AsyncMock(side_effect=ServerSelectionTimeoutError("no servers")),
        )

        payload = json.loads(
            await _tool_fn(review_list_pending)(_make_graph_ctx(ui_supported=False))
        )

        assert payload == {
            "error_type": "storage_unavailable",
            "retryable": True,
            "message": "memory store unreachable — try again",
        }

    @pytest.mark.parametrize(
        "tool", [review_confirm, review_reject], ids=["confirm", "reject"]
    )
    async def test_review_decisions_report_storage_unavailable(self, mocker, tool):
        mocker.patch(
            "tree.mcp.graph_tools._review_duplicate",
            new=AsyncMock(side_effect=ServerSelectionTimeoutError("no servers")),
        )

        payload = json.loads(
            await _tool_fn(tool)(
                "node-a", "node-b", "paul", _make_graph_ctx(ui_supported=False)
            )
        )

        assert payload["error_type"] == "storage_unavailable"
        assert payload["retryable"] is True


_FREE_TEXT_READERS = [
    ("search_memory", search_memory),
    ("query_memory", query_memory),
    ("deep_search_memory", deep_search_memory),
]


@pytest.mark.parametrize(
    "name,tool", _FREE_TEXT_READERS, ids=[r[0] for r in _FREE_TEXT_READERS]
)
@pytest.mark.parametrize("query", ["", "   ", "\n\t "])
async def test_blank_query_is_invalid_input(
    mocker, name: str, tool, query: str
) -> None:
    """graphrag's readers reject a blank query exactly like rag's (#126 QA).

    ``graphrag`` is the DEFAULT mode, so without this guard the most common
    server answered a whitespace query with a real (and meaningless) search
    while the rag server answered the envelope — one tool name, two behaviours.
    ``visualize_memory_graph`` is deliberately NOT guarded: an empty query
    there MEANS "draw the whole graph".
    """

    engine = mocker.patch(
        "tree.mcp.graph_tools.structured_query_memory", new=AsyncMock()
    )
    nl_engine = mocker.patch("tree.mcp.graph_tools.execute_nl_query", new=AsyncMock())

    result = await tool(query=query, ctx=_make_graph_ctx(ui_supported=False))

    assert json.loads(result) == {
        "error_type": "invalid_input",
        "retryable": False,
        "message": "query must not be empty",
    }
    engine.assert_not_awaited()
    nl_engine.assert_not_awaited()


async def test_visualize_memory_graph_still_draws_the_whole_graph_on_no_query(
    mocker,
) -> None:
    # The counter-case that keeps the guard from spreading: no query is a
    # feature here, not a validation failure.
    mocker.patch(
        "tree.mcp.graph_tools.fetch_full_graph",
        new=AsyncMock(return_value=_seed_result()),
    )
    mocker.patch("tree.memory.visualize.graph.GRAPHS_DIR", Path("/tmp"))
    mocker.patch("tree.mcp.viz_app.webbrowser.open", return_value=False)

    result = await visualize_memory_graph(_make_graph_ctx(ui_supported=True))

    assert isinstance(result, ToolResult)
