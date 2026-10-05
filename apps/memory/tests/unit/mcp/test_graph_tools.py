"""Unit tests for the graphrag-only MCP tools (:mod:`tree.mcp.graph_tools`).

Registered only when the server boots in graphrag mode (#110); the behaviour
asserted here — the dual graph delivery of ``query_memory`` /
``search_memory(visualize=True)``, the rendering-channel contract of
``visualize_memory_structure`` (which moved here with the neutral-MCP-App split,
ADR-007 §7) and the embedding-stripping serializer — is unchanged by either
move.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from contextlib import nullcontext
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bson import ObjectId, json_util
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
    visualize_memory_structure,
)
from tests.unit.memory.conftest import FakeMemoryCollection, chunk_star_rows
from tree.entities.memory import MEMORY_COLLECTION, to_stored_vector
from tree.mcp.server import mcp
from tree.mcp.viz_app import DOWNLOAD_CONTRACT, GRAPH_VIEW_URI
from tree.memory.rag.search import SearchUnavailableError
from tree.memory.graph import retrieval
from tree.memory.graph.retrieval import expand_graph
from tree.memory.types import QueryResult
from tree.memory.visualize.graph import to_graph_payload


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

    def test_strips_a_stored_bindata_embedding(self):
        # Task 175: rows straight from Mongo carry a float32 ``binData`` vector;
        # it must never reach the model as a base64 blob.
        docs = [{"_id": "person:alice", "embedding": to_stored_vector([0.1, 0.2])}]

        result = _serialize(docs)

        assert "embedding" not in result
        assert "$binary" not in result

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

    def test_strips_the_relevance_rank_and_matches_the_unranked_text(self):
        # Task 168: `doc_rank` is a viz concern; the model's text is unchanged.
        plain = [{"_id": "chunk:p1", "kind": "node", "sources": ["d1"]}]
        ranked = [{**plain[0], "doc_rank": 1}]

        result = _serialize(ranked)

        assert "doc_rank" not in result
        assert result == json_util.dumps(plain, indent=2)


# ---------------------------------------------------------------------------
# query_memory / search_memory with visualize=True — the dual delivery
# (ADR-005, decision 4): both tools route through the ONE shared seam
# ``_dual_graph_result`` → ``viz_app._graph_tool_result``, so from a
# visualization standpoint they behave exactly like visualize_memory_structure.
# ---------------------------------------------------------------------------

_GRAPH_UID = "65f1a2b3c4d5e6f7a8b9c0d1"


@pytest.fixture
def request_user_id() -> str:
    """The patched **Request user** seam resolves to this module's tenant."""

    return _GRAPH_UID


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
    }
    return ctx


def _seed_result() -> QueryResult:
    """The two-node / one-edge graph ``visualize_memory_structure`` is stubbed with."""

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
        assert str(link_block.uri).endswith(".html.gz")
        rendered = tmp_path / link_block.name.removesuffix(".gz")
        assert rendered.is_file()
        assert DOWNLOAD_CONTRACT in text_block.text

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


# --- search_memory keeps ranking first under truncation (task 166, ADR-011 §8):
# the REAL expand_graph over the star fixture — 6 hop nodes + 5 hop edges, the
# part_of closure adds p3 and 3 edges — with the closure on vs patched off.


async def _no_closure(collection, user_id, nodes, edges):
    return nodes, edges


async def _star_search(
    mocker, max_results: int, *, closure: bool = True, **kwargs: Any
) -> str | ToolResult:
    client = {
        "test_db": {
            MEMORY_COLLECTION: FakeMemoryCollection(chunk_star_rows(_GRAPH_UID))
        }
    }

    async def _query(**call: Any) -> QueryResult:
        return await expand_graph(
            client, "test_db", ["p1", "c1b"], call["user_id"], max_hops=1
        )

    mocker.patch("tree.mcp.graph_tools.structured_query_memory", new=_query)
    closure_off = patch.object(retrieval, "_attach_part_of_closure", _no_closure)
    with nullcontext() if closure else closure_off:
        return await search_memory(
            query="q",
            ctx=_make_graph_ctx(ui_supported=True),
            max_results=max_results,
            **kwargs,
        )


@pytest.mark.parametrize("max_results", [3, 6, 8, 10])
async def test_search_memory_answers_the_same_rows_with_the_closure(
    mocker, max_results: int
) -> None:
    without = await _star_search(mocker, max_results, closure=False)

    with_closure = await _star_search(mocker, max_results)

    # Assert: 11 original rows >= max_results, so the answer is byte-identical.
    assert with_closure == without


async def test_search_memory_appends_the_closure_after_every_original_row(
    mocker,
) -> None:
    without = json.loads(await _star_search(mocker, 20, closure=False))

    with_closure = await _star_search(mocker, 20)

    ids = [row["_id"] for row in json.loads(with_closure)]
    assert ids[: len(without)] == [row["_id"] for row in without]
    # The closure's node, then its edges (in the order Mongo returned them).
    assert ids[len(without)] == "p3"
    assert set(ids[len(without) + 1 :]) == {"p2>doc1", "c3a>p3", "p3>doc1"}
    assert "_closure_added" not in with_closure


async def test_search_memory_never_leaks_the_closure_marker_to_the_graph(
    mocker,
) -> None:
    result = await _star_search(mocker, 20, visualize=True)

    assert isinstance(result, ToolResult)
    assert all("_closure_added" not in block.text for block in result.content)
    assert "_closure_added" not in json.dumps(result.structured_content, default=str)


# ---------------------------------------------------------------------------
# visualize_memory_structure (graphrag form) — moved here with the split
# (ADR-007 §7): the rendering-channel contract of the tool itself. The helper
# it delivers through lives in the neutral ``viz_app`` and is tested there.
# ---------------------------------------------------------------------------


async def test_all_three_graph_tools_declare_the_shared_ui_resource() -> None:
    # Arrange / Act: importing ``tree.mcp.graph_tools`` (module level) registered
    # all three graph tools on the server.
    uris = {
        name: ((await mcp.get_tool(name)).meta or {}).get("ui", {}).get("resourceUri")
        for name in ("visualize_memory_structure", "query_memory", "search_memory")
    }

    # Assert: one ui:// resource serves all three (ADR-005, decision 4).
    assert set(uris.values()) == {GRAPH_VIEW_URI}
    assert (await mcp.get_resource(GRAPH_VIEW_URI)) is not None


# ---------------------------------------------------------------------------
# visualize_memory_structure tool — rendering-channel contract
# ---------------------------------------------------------------------------


async def test_visualize_ships_payload_in_content_block_for_ui_clients(
    mocker,
) -> None:
    mocker.patch(
        "tree.mcp.graph_tools.structured_query_memory",
        new=AsyncMock(return_value=_seed_result()),
    )
    ctx = _make_graph_ctx(ui_supported=True)

    result = await visualize_memory_structure(ctx, query="alice")

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

    result = await visualize_memory_structure(ctx)

    full_graph_mock.assert_awaited_once()
    query_mock.assert_not_awaited()
    assert "your full memory" in result.content[0].text


def _ranked_result(n_documents: int) -> QueryResult:
    """A **Full graph** result: every row stamped ``doc_rank`` 1..n."""

    nodes = [
        {"_id": f"doc{rank}", "kind": "node", "type": "document", "doc_rank": rank}
        for rank in range(1, n_documents + 1)
    ]
    return QueryResult(nodes=nodes, edges=[])


async def test_visualize_forwards_max_docs_to_the_full_graph_read(mocker) -> None:
    full_graph_mock = mocker.patch(
        "tree.mcp.graph_tools.fetch_full_graph",
        new=AsyncMock(return_value=_ranked_result(3)),
    )

    await visualize_memory_structure(_make_graph_ctx(ui_supported=True), max_docs=7)

    assert full_graph_mock.await_args.kwargs["max_docs"] == 7


async def test_visualize_without_max_docs_leaves_the_cap_to_config(mocker) -> None:
    full_graph_mock = mocker.patch(
        "tree.mcp.graph_tools.fetch_full_graph",
        new=AsyncMock(return_value=_ranked_result(3)),
    )

    await visualize_memory_structure(_make_graph_ctx(ui_supported=True))

    # None -> fetch_full_graph reads query.full_graph_max_docs itself.
    assert full_graph_mock.await_args.kwargs["max_docs"] is None


@pytest.mark.parametrize("max_docs", [0, -3])
async def test_visualize_refuses_max_docs_below_one_before_any_read(
    mocker, max_docs: int
) -> None:
    full_graph_mock = mocker.patch(
        "tree.mcp.graph_tools.fetch_full_graph", new=AsyncMock()
    )
    query_mock = mocker.patch(
        "tree.mcp.graph_tools.structured_query_memory", new=AsyncMock()
    )

    result = await visualize_memory_structure(
        _make_graph_ctx(ui_supported=True), max_docs=max_docs
    )

    assert json.loads(result) == {
        "error_type": "invalid_input",
        "retryable": False,
        "message": "max_docs must be ≥ 1",
    }
    full_graph_mock.assert_not_awaited()
    query_mock.assert_not_awaited()


async def test_visualize_never_forwards_max_docs_with_a_query(mocker) -> None:
    query_mock = mocker.patch(
        "tree.mcp.graph_tools.structured_query_memory",
        new=AsyncMock(return_value=_seed_result()),
    )
    full_graph_mock = mocker.patch(
        "tree.mcp.graph_tools.fetch_full_graph", new=AsyncMock()
    )

    result = await visualize_memory_structure(
        _make_graph_ctx(ui_supported=True), query="alice", max_docs=7
    )

    assert "max_docs" not in query_mock.await_args.kwargs
    full_graph_mock.assert_not_awaited()
    assert "most-recent documents" not in result.content[0].text


async def test_visualize_full_graph_summary_says_how_many_documents_show(
    mocker,
) -> None:
    mocker.patch(
        "tree.mcp.graph_tools.fetch_full_graph",
        new=AsyncMock(return_value=_ranked_result(3)),
    )

    result = await visualize_memory_structure(_make_graph_ctx(ui_supported=True))

    # Assert: the model reads that the view is capped, not "everything".
    assert result.content[0].text.startswith(
        "Knowledge graph for your full memory: 3 of 3 most-recent documents "
        "shown by default, 3 nodes, 0 edges"
    )


# --- task 168: query views rank documents by relevance -----------------------


def _ranked_docs() -> list[dict[str, Any]]:
    """``_GRAPH_DOCS`` as a seed-based query view stamps them (task 168)."""

    return [
        {**doc, "doc_rank": 1} if doc["kind"] == "node" else doc for doc in _GRAPH_DOCS
    ]


async def test_search_memory_text_never_carries_the_relevance_rank(mocker) -> None:
    _patch_query(mocker, "search_memory", _ranked_docs())

    result = await search_memory(query="q", ctx=_make_graph_ctx(ui_supported=True))

    # Assert: byte-identical to the unranked rows' serialization.
    assert "doc_rank" not in result
    assert result == json_util.dumps(_GRAPH_DOCS, indent=2)


async def test_search_memory_visualize_ranks_its_graph_by_relevance(mocker) -> None:
    _patch_query(mocker, "search_memory", _ranked_docs())
    build = mocker.patch(
        "tree.mcp.graph_tools.to_graph_payload", wraps=to_graph_payload
    )

    result = await search_memory(
        query="q", ctx=_make_graph_ctx(ui_supported=True), visualize=True
    )

    assert build.call_args.kwargs == {"document_order": "relevance"}
    assert _content_payload(result)["controls"]["documents"] == {
        "shown": 1,
        "total": 1,
        "order": "relevance",
    }
    assert "doc_rank" not in result.content[0].text


def _truncation_corpus() -> list[dict[str, Any]]:
    """A query view ranked B=2, C=3 first in the row order, A (rank 1) last —
    so ``max_results`` truncation cuts the most relevant document's rows."""

    def ranked(node_id: str, node_type: str, rank: int) -> dict[str, Any]:
        return {**_node(node_id, node_type), "doc_rank": rank}

    return [
        ranked("b-p0", "chunk", 2),
        ranked("b-p1", "chunk", 2),
        ranked("b", "document", 2),
        ranked("c-p0", "chunk", 3),
        ranked("c", "document", 3),
        ranked("a-p0", "chunk", 1),
        ranked("a", "document", 1),
    ]


def _visible_at_one(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """The renderer's ``isVisible`` with the slider at 1."""

    return [n for n in payload["nodes"] if n.get("docRank") in (None, 1)]


@pytest.mark.parametrize("max_results", [1, 2, 3, 4, 5, 6, 7])
async def test_a_truncated_search_view_is_never_empty_at_one(
    mocker, max_results: int
) -> None:
    # Regression (task 168 QA): the cut rows took rank 1's star with them.
    _patch_query(mocker, "search_memory", _truncation_corpus())

    result = await search_memory(
        query="q",
        ctx=_make_graph_ctx(ui_supported=True),
        max_results=max_results,
        visualize=True,
    )

    # Assert: either no slider at all, or one whose total is the stars drawn
    # and whose position 1 leaves exactly one star (and never an empty view).
    payload = _content_payload(result)
    stars = [n for n in payload["nodes"] if n["type"] == "document"]
    documents = payload["controls"].get("documents")
    if not stars:
        assert documents is None
        assert all("docRank" not in n for n in payload["nodes"])
        return
    assert documents["total"] == len(stars)
    assert sorted(n["docRank"] for n in stars) == list(range(1, len(stars) + 1))
    visible = _visible_at_one(payload)
    assert [n["type"] for n in visible].count("document") == 1


async def test_a_truncated_search_view_renumbers_the_drawn_stars(mocker) -> None:
    _patch_query(mocker, "search_memory", _truncation_corpus())

    result = await search_memory(
        query="q", ctx=_make_graph_ctx(ui_supported=True), max_results=5, visualize=True
    )

    # Assert: A (rank 1) was cut; B and C are drawn as 1 and 2; the model's
    # text is the same as before the renumbering.
    payload = _content_payload(result)
    ranks = {n["id"].rsplit(":", 1)[1]: n.get("docRank") for n in payload["nodes"]}
    assert ranks == {"b-p0": 1, "b-p1": 1, "b": 1, "c-p0": 2, "c": 2}
    assert payload["controls"]["documents"] == {
        "shown": 2,
        "total": 2,
        "order": "relevance",
    }
    assert "doc_rank" not in result.content[0].text


async def test_an_nl_query_view_carries_no_documents_control(mocker) -> None:
    _patch_query(mocker, "query_memory", _GRAPH_DOCS)

    result = await query_memory(
        query="q", ctx=_make_graph_ctx(ui_supported=True), visualize=True
    )

    # Assert: NL pipeline rows carry no rank, so there is nothing to slide.
    payload = _content_payload(result)
    assert "documents" not in payload["controls"]
    assert all("docRank" not in node for node in payload["nodes"])


async def test_visualize_query_summary_says_how_many_relevant_documents_show(
    mocker,
) -> None:
    mocker.patch(
        "tree.mcp.graph_tools.structured_query_memory",
        new=AsyncMock(return_value=_ranked_result(3)),
    )
    build = mocker.patch(
        "tree.mcp.graph_tools.to_graph_payload", wraps=to_graph_payload
    )

    result = await visualize_memory_structure(
        _make_graph_ctx(ui_supported=True), query="memory for ai agents"
    )

    # Assert: a query view shows every document by default, most relevant first.
    assert build.call_args.kwargs == {"document_order": "relevance"}
    assert result.content[0].text.startswith(
        "Knowledge graph for 'memory for ai agents': 3 of 3 most-relevant "
        "documents shown by default, 3 nodes, 0 edges"
    )


async def test_visualize_full_graph_ranks_by_recency(mocker) -> None:
    mocker.patch(
        "tree.mcp.graph_tools.fetch_full_graph",
        new=AsyncMock(return_value=_ranked_result(3)),
    )
    build = mocker.patch(
        "tree.mcp.graph_tools.to_graph_payload", wraps=to_graph_payload
    )

    result = await visualize_memory_structure(_make_graph_ctx(ui_supported=True))

    assert build.call_args.kwargs == {"document_order": "recency"}
    assert _content_payload(result)["controls"]["documents"]["order"] == "recency"


async def test_visualize_an_unranked_query_keeps_the_plain_summary(mocker) -> None:
    mocker.patch(
        "tree.mcp.graph_tools.structured_query_memory",
        new=AsyncMock(return_value=_seed_result()),
    )

    result = await visualize_memory_structure(
        _make_graph_ctx(ui_supported=True), query="alice"
    )

    assert result.content[0].text.startswith(
        "Knowledge graph for 'alice': 2 nodes, 1 edges"
    )
    assert "documents" not in _content_payload(result)["controls"]


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

    result = await visualize_memory_structure(ctx, query="alice")

    # Assert: the text block carries the server-side path; the resource link
    # lets a client of a REMOTE server download the same HTML over MCP.
    assert isinstance(result, ToolResult)
    text_block, link_block = result.content
    assert str(tmp_path) in text_block.text
    assert link_block.type == "resource_link"
    assert str(link_block.uri) == f"graphs://{link_block.name}"
    assert link_block.name.endswith(".html.gz")
    assert link_block.mimeType == "application/gzip"
    rendered = tmp_path / link_block.name.removesuffix(".gz")
    assert rendered.is_file()
    assert DOWNLOAD_CONTRACT in text_block.text


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

    result = await visualize_memory_structure(ctx, query="alice", as_html_file=True)

    assert result.content[0].text.count("you asked for an HTML file") == 1
    assert result.content[1].type == "resource_link"


# ---------------------------------------------------------------------------
# The retrieval boundary — the SAME two envelopes as rag mode (ADR-008 §2)
# ---------------------------------------------------------------------------


# Every graphrag reader, with the engine it delegates to and the args that
# reach it. ``visualize_memory_structure`` is listed on its query path — the shape
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
        "visualize_memory_structure",
        visualize_memory_structure,
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
        await visualize_memory_structure(_make_graph_ctx(ui_supported=False))
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

    ``graphrag`` was the DEFAULT mode, so without this guard the most common
    server answered a whitespace query with a real (and meaningless) search
    while the rag server answered the envelope — one tool name, two behaviours.
    ``visualize_memory_structure`` is deliberately NOT guarded: an empty query
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


async def test_visualize_memory_structure_still_draws_the_whole_graph_on_no_query(
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

    result = await visualize_memory_structure(_make_graph_ctx(ui_supported=True))

    assert isinstance(result, ToolResult)


@pytest.mark.parametrize(
    "tool",
    [visualize_memory_structure, query_memory, search_memory],
    ids=["visualize_memory_structure", "query_memory", "search_memory"],
)
def test_every_graph_tool_docstring_states_the_download_contract(tool) -> None:
    # Assert: the one decode instruction reaches the model verbatim (modulo
    # wrapping) from every graphrag tool that can answer with a file.
    doc = " ".join((tool.__doc__ or "").split())

    assert DOWNLOAD_CONTRACT in doc
    assert "save its text" not in doc
