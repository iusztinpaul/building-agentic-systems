"""Every MCP tool runs as the **Request user** its call resolves (ADR-014 §1).

Two sides of one contract, parametrised over EVERY tool function of both
modes (the eight in ``tools``, the seven in ``graph_tools`` and the
dashboard — a graphrag server registers 14 of these 16, a rag server 8):

* the id the seam resolves is the id the business-logic call receives;
* a seam that raises answers the **Tool error envelope** — ``configuration_error``
  (not retryable) for a missing / unknown user, ``storage_unavailable``
  (retryable) for an unreachable ``users`` collection — and the business call
  never runs (nothing searched, nothing ingested, no SERP credit spent).

The seam is patched at ``tree.mcp.request_user.resolve_request_user`` (the
package conftest does it for every test; the failure tests re-patch it).
"""

import ast
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from beanie import PydanticObjectId
from pymongo.errors import ServerSelectionTimeoutError

from tree.mcp import dashboard_app, request_user
from tree.mcp import graph_tools as mcp_graph_tools
from tree.mcp import tools as mcp_tools
from tree.mcp.request_user import (
    MISSING_ENV_MESSAGE,
    MISSING_HEADER_MESSAGE,
    RequestUserError,
    unknown_identifier_message,
)
from tree.memory.graph.review.types import (
    MergeStrategy,
    ReviewDecision,
    ReviewResult,
)
from tree.memory.rag.types import RetrievalResult
from tree.memory.types import QueryResult
from tree.online import IngestReceipt

_MCP_SRC = Path(mcp_tools.__file__).resolve().parent
_REPO_ROOT = Path(__file__).resolve().parents[5]

_SUBMITTED = IngestReceipt(
    source_uri="https://example.com",
    duplicate=False,
    flow_run_id="run-1",
    status="scheduled",
)

_REVIEWED = ReviewResult(
    decision=ReviewDecision.CONFIRM,
    winner_node_id="x:person:a",
    loser_node_id="x:person:b",
    applied_strategy=MergeStrategy.KEEP_PRIMARY,
    edges_transferred=0,
    same_as_edge_id="x:person:a|same_as|x:person:b",
)

_REVIEW_PAIR = {"source_node_id": "a", "target_node_id": "b", "reviewed_by": "paul"}


@dataclass(frozen=True)
class ToolCase:
    """One tool, the arguments that pass its input guards, and its user sink.

    ``target`` is the business-logic call the resolved id must reach;
    ``user_arg`` is where that call carries it — a keyword name or a
    positional index. ``None`` for ``scrape_web``, which reads nothing per
    user but resolves anyway ("every tool").
    """

    tool: Any
    kwargs: dict[str, Any]
    target: str
    returns: Any
    user_arg: str | int | None
    extra_patches: dict[str, Any] = field(default_factory=dict)


_CASES: dict[str, ToolCase] = {
    "rag:search_memory": ToolCase(
        mcp_tools.search_memory,
        {"query": "x"},
        "tree.mcp.tools.retrieve_parents",
        RetrievalResult(),
        "user_id",
    ),
    "rag:visualize_memory_structure": ToolCase(
        mcp_tools.visualize_memory_structure,
        {},
        "tree.mcp.tools.fetch_rag_structure",
        MagicMock(nodes=[]),
        2,
    ),
    "visualize_memory_embeddings": ToolCase(
        mcp_tools.visualize_memory_embeddings,
        {},
        "tree.mcp.tools.load_embedding_map",
        None,
        2,
    ),
    "ingest_url": ToolCase(
        mcp_tools.ingest_url,
        {"url": "https://example.com"},
        "tree.mcp.tools.dispatch_online_pipeline",
        _SUBMITTED,
        1,
    ),
    "ingest_file": ToolCase(
        mcp_tools.ingest_file,
        {"file_path": "/tmp/x.md", "content": "file text"},
        "tree.mcp.tools.dispatch_online_pipeline",
        _SUBMITTED,
        1,
    ),
    "ingest_conversation": ToolCase(
        mcp_tools.ingest_conversation,
        {"conversation_text": "Some text."},
        "tree.mcp.tools.dispatch_online_pipeline",
        _SUBMITTED,
        1,
    ),
    "search_web": ToolCase(
        mcp_tools.search_web,
        {"query": "prefect", "ingest": True, "ingest_urls": ["https://example.com"]},
        "tree.mcp.tools._trigger_url_batch_ingest",
        {"flow_run_id": "run-1", "tracking_url": "https://prefect/run-1"},
        1,
        extra_patches={"tree.mcp.tools.web_search": []},
    ),
    "scrape_web": ToolCase(
        mcp_tools.scrape_web,
        {"urls": ["https://example.com/a"]},
        "tree.mcp.tools._scrape_one",
        {"success": True},
        None,
    ),
    "graphrag:search_memory": ToolCase(
        mcp_graph_tools.search_memory,
        {"query": "x"},
        "tree.mcp.graph_tools.structured_query_memory",
        QueryResult(nodes=[], edges=[]),
        "user_id",
    ),
    "graphrag:visualize_memory_structure": ToolCase(
        mcp_graph_tools.visualize_memory_structure,
        {},
        "tree.mcp.graph_tools.fetch_full_graph",
        QueryResult(nodes=[], edges=[]),
        "user_id",
    ),
    "query_memory": ToolCase(
        mcp_graph_tools.query_memory,
        {"query": "x"},
        "tree.mcp.graph_tools.execute_nl_query",
        [],
        "user_id",
    ),
    "deep_search_memory": ToolCase(
        mcp_graph_tools.deep_search_memory,
        {"query": "x"},
        "tree.mcp.graph_tools.structured_query_memory",
        QueryResult(nodes=[], edges=[]),
        "user_id",
    ),
    "review_list_pending": ToolCase(
        mcp_graph_tools.review_list_pending,
        {},
        "tree.mcp.graph_tools._find_pending_duplicates",
        [],
        "user_id",
    ),
    "review_confirm": ToolCase(
        mcp_graph_tools.review_confirm,
        _REVIEW_PAIR,
        "tree.mcp.graph_tools._review_duplicate",
        _REVIEWED,
        "user_id",
    ),
    "review_reject": ToolCase(
        mcp_graph_tools.review_reject,
        _REVIEW_PAIR,
        "tree.mcp.graph_tools._review_duplicate",
        _REVIEWED,
        "user_id",
    ),
    "memory_dashboard": ToolCase(
        dashboard_app.memory_dashboard,
        {},
        "tree.mcp.dashboard_app.fetch_full_graph",
        QueryResult(nodes=[], edges=[]),
        "user_id",
    ),
}

_USER_SINK_CASES = sorted(name for name, c in _CASES.items() if c.user_arg is not None)

_SEAM_FAILURES = [
    pytest.param(
        RequestUserError(MISSING_HEADER_MESSAGE),
        "configuration_error",
        False,
        MISSING_HEADER_MESSAGE,
        id="missing-header",
    ),
    pytest.param(
        RequestUserError(MISSING_ENV_MESSAGE),
        "configuration_error",
        False,
        MISSING_ENV_MESSAGE,
        id="missing-env",
    ),
    pytest.param(
        RequestUserError(unknown_identifier_message("carol@example.com")),
        "configuration_error",
        False,
        unknown_identifier_message("carol@example.com"),
        id="unknown-user",
    ),
    pytest.param(
        ServerSelectionTimeoutError("users unreachable"),
        "storage_unavailable",
        True,
        mcp_tools.STORAGE_UNAVAILABLE_MESSAGE,
        id="users-unreachable",
    ),
]


def _tool_fn(tool: Any) -> Any:
    """Unwrap FastMCP's ``FunctionTool`` back to the coroutine it registered."""

    return getattr(tool, "fn", tool)


def _make_ctx() -> MagicMock:
    ctx = MagicMock()
    ctx.client_supports_extension.return_value = False
    ctx.lifespan_context = {
        "client": MagicMock(),
        "database": "test_db",
        "llm": MagicMock(),
        "embedding_model": MagicMock(),
        "thread_id": "mcp-session-test",
    }
    return ctx


def _patch_case(mocker, case: ToolCase) -> AsyncMock:
    for target, value in case.extra_patches.items():
        mocker.patch(target, new_callable=AsyncMock, return_value=value)
    return mocker.patch(case.target, new_callable=AsyncMock, return_value=case.returns)


@pytest.mark.parametrize("name", _USER_SINK_CASES)
async def test_the_resolved_user_reaches_the_business_call(
    mocker, request_user_id: PydanticObjectId, name: str
) -> None:
    case = _CASES[name]
    business_call = _patch_case(mocker, case)

    await _tool_fn(case.tool)(ctx=_make_ctx(), **case.kwargs)

    business_call.assert_awaited_once()
    call = business_call.await_args
    passed = (
        call.kwargs[case.user_arg]
        if isinstance(case.user_arg, str)
        else call.args[case.user_arg]
    )
    assert passed == request_user_id


@pytest.mark.parametrize("name", sorted(_CASES))
async def test_every_tool_resolves_the_request_user(
    mocker, _patched_request_user: AsyncMock, name: str
) -> None:
    case = _CASES[name]
    _patch_case(mocker, case)
    ctx = _make_ctx()

    await _tool_fn(case.tool)(ctx=ctx, **case.kwargs)

    _patched_request_user.assert_awaited_once_with(ctx)


@pytest.mark.parametrize("error,error_type,retryable,message", _SEAM_FAILURES)
@pytest.mark.parametrize("name", sorted(_CASES))
async def test_a_failing_seam_answers_the_envelope_and_runs_nothing(
    mocker,
    name: str,
    error: Exception,
    error_type: str,
    retryable: bool,
    message: str,
) -> None:
    case = _CASES[name]
    business_call = _patch_case(mocker, case)
    mocker.patch(
        "tree.mcp.request_user.resolve_request_user",
        new_callable=AsyncMock,
        side_effect=error,
    )
    serp = mocker.patch("tree.mcp.tools.web_search", new_callable=AsyncMock)

    result = await _tool_fn(case.tool)(ctx=_make_ctx(), **case.kwargs)

    assert json.loads(result) == {
        "error_type": error_type,
        "retryable": retryable,
        "message": message,
    }
    business_call.assert_not_awaited()
    serp.assert_not_awaited()


async def test_search_web_without_ingest_still_resolves_the_user(mocker) -> None:
    """``search_web`` resolves on EVERY call — not only when it ingests."""

    mocker.patch(
        "tree.mcp.request_user.resolve_request_user",
        new_callable=AsyncMock,
        side_effect=RequestUserError(MISSING_HEADER_MESSAGE),
    )
    serp = mocker.patch("tree.mcp.tools.web_search", new_callable=AsyncMock)

    result = await _tool_fn(mcp_tools.search_web)(query="prefect", ctx=_make_ctx())

    assert json.loads(result)["error_type"] == "configuration_error"
    serp.assert_not_awaited()


@pytest.mark.parametrize(
    "name",
    [
        "rag:search_memory",
        "graphrag:search_memory",
        "query_memory",
        "deep_search_memory",
        "visualize_memory_embeddings",
        "rag:visualize_memory_structure",
    ],
)
async def test_opik_threads_are_tagged_with_the_resolved_user(
    mocker, request_user_id: PydanticObjectId, name: str
) -> None:
    case = _CASES[name]
    _patch_case(mocker, case)
    tag = mocker.patch("tree.mcp.tools.update_current_trace")

    await _tool_fn(case.tool)(ctx=_make_ctx(), **case.kwargs)

    tag.assert_called_once()
    assert tag.call_args.kwargs["metadata"]["user_id"] == str(request_user_id)


# ---------------------------------------------------------------------------
# Source guards — the "save its text" pattern
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", sorted(_MCP_SRC.glob("*.py")), ids=lambda p: p.name)
def test_no_tool_reads_a_user_from_the_lifespan_context(path: Path) -> None:
    source = path.read_text()

    assert '["user_id"]' not in source
    assert '.get("user_id")' not in source


def test_the_seam_imports_nothing_from_tree_mcp() -> None:
    """``request_user`` must stay importable from ``tools`` without a cycle."""

    tree = ast.parse(Path(request_user.__file__).read_text())
    imported = {
        node.module if isinstance(node, ast.ImportFrom) else alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import | ast.ImportFrom)
        for alias in node.names
    }

    assert not {name for name in imported if name.startswith("tree.mcp")}


def test_no_client_side_identity_header_name_exists_anywhere() -> None:
    """The identity rides Horizon's ``horizon-actor-email`` — no invented header."""

    needle = "X-Tree-" + "User"
    roots = [
        _REPO_ROOT / "apps" / "memory" / "src",
        _REPO_ROOT / "apps" / "memory" / "tests",
        _REPO_ROOT / "docs",
    ]
    files = [_REPO_ROOT / ".mcp.json", _REPO_ROOT / ".pi" / "mcp.json"]
    for root in roots:
        files.extend(
            p
            for p in root.rglob("*")
            if p.is_file() and p.suffix in {".py", ".md", ".json", ".yaml"}
        )

    offenders = [str(p) for p in files if needle.lower() in p.read_text().lower()]

    assert not offenders
