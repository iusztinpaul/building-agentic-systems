"""The ONE **Tool error envelope** every MCP tool answers with (ADR-008 §2).

``{error_type, retryable, message}`` — nothing else, in BOTH **Memory mode**s.
What is asserted here is the CONTRACT rather than any single tool: the helper's
exact shape, the legacy-code → (``error_type``, ``retryable``) mapping across
tools, and a source-level guard that the pre-ADR-008 ``error`` / ``detail`` keys
never come back.

Each tool's own failure paths stay with the tool (``test_tools.py``,
``test_graph_tools.py``, ``test_search_web.py``); this file is the cross-tool
table.
"""

import ast
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from beanie import PydanticObjectId
from pymongo.errors import ServerSelectionTimeoutError

from tree.mcp import graph_tools, tools
from tree.mcp.graph_tools import review_confirm, review_list_pending
from tree.mcp.tools import (
    ToolErrorEnvelope,
    ingest_conversation,
    ingest_file,
    ingest_url,
    scrape_web,
    search_web,
    tool_error,
)

_USER_ID = PydanticObjectId("507f1f77bcf86cd799439011")

# The ONE dict literal in either module that legitimately keeps a legacy key:
# the nested ``ingest`` sub-result of a SUCCESSFUL ``search_web`` answer, which
# is not an envelope (ADR-008 §2, task #126 "Out of scope").
_LEGACY_KEY_EXEMPT_FUNCTION = "_build_ingest_block"

_LEGACY_KEYS = {"error", "detail"}

#: How many exempt dicts each module is allowed: ``_build_ingest_block``'s two
#: returns (no urls to ingest / the trigger failed) live in ``tools.py``, and
#: ``graph_tools.py`` has no exempt function at all.
_EXEMPT_DICT_COUNT = {"tree.mcp.tools": 2, "tree.mcp.graph_tools": 0}


def _tool_fn(tool):
    """Unwrap FastMCP's ``FunctionTool`` back to the coroutine it registered."""

    return getattr(tool, "fn", tool)


def _make_ctx() -> MagicMock:
    ctx = MagicMock()
    ctx.lifespan_context = {
        "client": MagicMock(),
        "database": "test_db",
        "embedding_model": MagicMock(),
        "llm": MagicMock(),
        "user_id": _USER_ID,
        "thread_id": "mcp-session-test",
    }
    return ctx


def _envelope(raw: str) -> dict[str, Any]:
    """Parse a tool answer and assert it IS an envelope — three keys, no more."""

    payload = json.loads(raw)
    assert set(payload) == {"error_type", "retryable", "message"}, payload
    return payload


def _http_status_error(status_code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://example.com")
    return httpx.HTTPStatusError(
        f"HTTP {status_code}",
        request=request,
        response=httpx.Response(status_code, request=request),
    )


class TestHelper:
    def test_helper_shape(self) -> None:
        # The whole answer, verbatim: a model parses these three keys and
        # nothing else — no ``error``, no ``detail``, no extra field.
        assert json.loads(tool_error("invalid_input", "x", retryable=False)) == {
            "error_type": "invalid_input",
            "retryable": False,
            "message": "x",
        }

    def test_envelope_model_is_the_serialized_shape(self) -> None:
        assert set(ToolErrorEnvelope.model_fields) == {
            "error_type",
            "retryable",
            "message",
        }

    def test_retryable_is_keyword_only(self) -> None:
        # A positional bool at a call site reads as noise; ``retryable`` is the
        # field the model acts on, so it is always named.
        with pytest.raises(TypeError):
            tool_error("invalid_input", "x", False)  # type: ignore[misc]


class TestMapping:
    """Every legacy code maps to one (error_type, retryable) pair, across tools.

    ``network_error`` is exercised on ``search_web`` rather than ``ingest_url``:
    since #122 the only call ``ingest_url`` makes is the Prefect dispatch, so a
    connection failure there IS ``pipeline_unavailable`` (ADR-008 §2, Story 1),
    while ``search_web`` still calls Bright Data in-process.
    """

    async def test_ingest_url_rejects_an_unsupported_scheme(self) -> None:
        result = await _tool_fn(ingest_url)("ftp://example.com/post", _make_ctx())

        payload = _envelope(result)
        assert payload["error_type"] == "unsupported_url"
        assert payload["retryable"] is False

    async def test_ingest_file_reports_an_oversized_payload(self, mocker) -> None:
        mocker.patch(
            "tree.mcp.tools.dispatch_online_pipeline",
            new_callable=AsyncMock,
            side_effect=ValueError("Source payload is 9 bytes — over the cap."),
        )

        result = await _tool_fn(ingest_file)("/tmp/notes.md", "body", _make_ctx())

        payload = _envelope(result)
        assert payload["error_type"] == "file_error"
        assert payload["retryable"] is False

    @pytest.mark.parametrize("text", ["", "   ", "\n\t "])
    async def test_ingest_conversation_rejects_blank_text(self, text: str) -> None:
        result = await _tool_fn(ingest_conversation)(text, _make_ctx())

        payload = _envelope(result)
        assert payload["error_type"] == "invalid_input"
        assert payload["retryable"] is False

    @pytest.mark.parametrize(
        "name,tool",
        [
            ("rag", tools.search_memory),
            ("graphrag", graph_tools.search_memory),
            ("query_memory", graph_tools.query_memory),
            ("deep_search_memory", graph_tools.deep_search_memory),
        ],
        ids=["rag:search_memory", "graphrag:search_memory", "query_memory", "deep"],
    )
    async def test_a_blank_query_is_invalid_input_in_both_modes(
        self, name: str, tool
    ) -> None:
        # One tool name, one behaviour: ``graphrag`` is the DEFAULT mode, so a
        # guard that only existed in rag left the common server answering a
        # whitespace query with a real search (#126 QA).
        payload = _envelope(await _tool_fn(tool)(query="   ", ctx=_make_ctx()))

        assert payload["error_type"] == "invalid_input"
        assert payload["retryable"] is False
        assert payload["message"] == "query must not be empty"

    async def test_search_web_rejects_ingest_top_k_below_one(self) -> None:
        result = await _tool_fn(search_web)(
            "prefect", _make_ctx(), ingest=True, ingest_top_k=0
        )

        payload = _envelope(result)
        assert payload["error_type"] == "invalid_input"
        assert payload["retryable"] is False

    async def test_scrape_web_rejects_more_than_five_urls(self) -> None:
        result = await _tool_fn(scrape_web)(
            [f"https://example.com/{i}" for i in range(6)], _make_ctx()
        )

        payload = _envelope(result)
        assert payload["error_type"] == "invalid_input"
        assert payload["retryable"] is False

    async def test_review_list_pending_rejects_an_unknown_entity_type(self) -> None:
        result = await _tool_fn(review_list_pending)(_make_ctx(), entity_type="dragon")

        payload = _envelope(result)
        assert payload["error_type"] == "invalid_input"
        assert payload["retryable"] is False

    async def test_review_confirm_reports_an_unreviewable_pair(self, mocker) -> None:
        mocker.patch(
            "tree.mcp.graph_tools._review_duplicate",
            new_callable=AsyncMock,
            side_effect=ValueError("No pending SAME_AS edge for this pair."),
        )

        result = await _tool_fn(review_confirm)("node-a", "node-b", "paul", _make_ctx())

        payload = _envelope(result)
        assert payload["error_type"] == "invalid_state"
        assert payload["retryable"] is False

    @pytest.mark.parametrize(
        "tool,args,patch_target",
        [
            (
                ingest_url,
                ("https://example.com/post",),
                "tree.online.Document.find_one",
            ),
            (
                review_list_pending,
                (),
                "tree.mcp.graph_tools._find_pending_duplicates",
            ),
        ],
        ids=["ingest_url", "review_list_pending"],
    )
    async def test_an_unreachable_mongo_is_storage_unavailable(
        self, mocker, tool, args: tuple, patch_target: str
    ) -> None:
        # NOT ``search_unavailable``: neither call searched anything, and a
        # model told "search is unavailable" after an ingest learns the wrong
        # thing about the memory.
        mocker.patch(
            patch_target,
            new_callable=AsyncMock,
            side_effect=ServerSelectionTimeoutError("no servers available"),
        )

        payload = _envelope(await _tool_fn(tool)(*args, _make_ctx()))

        assert payload["error_type"] == "storage_unavailable"
        assert payload["retryable"] is True
        assert payload["message"] == "memory store unreachable — try again"

    @pytest.mark.parametrize(
        "exc", [httpx.ConnectError("refused"), httpx.TimeoutException("timed out")]
    )
    async def test_search_web_reports_a_connect_failure_as_network_error(
        self, mocker, exc: Exception
    ) -> None:
        mocker.patch(
            "tree.mcp.tools.web_search", new_callable=AsyncMock, side_effect=exc
        )

        payload = _envelope(await _tool_fn(search_web)("prefect", _make_ctx()))

        assert payload["error_type"] == "network_error"
        assert payload["retryable"] is True

    @pytest.mark.parametrize(
        "status_code,retryable",
        [(429, True), (500, True), (503, True), (400, False), (404, False)],
        ids=["429", "500", "503", "400", "404"],
    )
    async def test_http_error_is_retryable_only_on_429_or_5xx(
        self, mocker, status_code: int, retryable: bool
    ) -> None:
        # The one rule the model acts on: rate limits and server faults come
        # back, a 404 never does.
        mocker.patch(
            "tree.mcp.tools.web_search",
            new_callable=AsyncMock,
            side_effect=_http_status_error(status_code),
        )

        payload = _envelope(await _tool_fn(search_web)("prefect", _make_ctx()))

        assert payload["error_type"] == "http_error"
        assert payload["retryable"] is retryable
        assert str(status_code) in payload["message"]


# ---------------------------------------------------------------------------
# Source-level regression guard
# ---------------------------------------------------------------------------


def _legacy_key_dicts(node: ast.AST) -> list[ast.Dict]:
    """Every dict literal under ``node`` with an ``error`` / ``detail`` key."""

    return [
        child
        for child in ast.walk(node)
        if isinstance(child, ast.Dict)
        and _LEGACY_KEYS & {k.value for k in child.keys if isinstance(k, ast.Constant)}
    ]


def _exempt_dicts(module: ast.Module) -> set[int]:
    """Ids of the legacy-key dicts inside the one exempt function."""

    return {
        id(d)
        for fn in ast.walk(module)
        if isinstance(fn, ast.FunctionDef | ast.AsyncFunctionDef)
        and fn.name == _LEGACY_KEY_EXEMPT_FUNCTION
        for d in _legacy_key_dicts(fn)
    }


@pytest.mark.parametrize("module", [tools, graph_tools], ids=["tools", "graph_tools"])
def test_no_legacy_error_keys(module) -> None:
    """No tool answers the pre-ADR-008 ``{"error", "detail"}`` shape any more.

    Asserted over the parsed AST, not the raw text, for two reasons: a regex
    would also match the docstrings that legitimately QUOTE the old shape while
    documenting the new one, and only an AST can tell "a dict literal with an
    ``error`` key" from the word ``error`` in prose. Exactly TWO dicts are
    exempt — the no-urls and failed-trigger returns of ``_build_ingest_block``,
    i.e. the nested ``ingest`` sub-result of a SUCCESSFUL ``search_web`` answer,
    which is not an envelope (task #126, "Out of scope"). The guard allowlists
    that function by name AND pins the count, so a THIRD legacy-key dict there
    fails too, as does any new one anywhere else.
    """

    source = Path(module.__file__).read_text()
    tree = ast.parse(source)
    exempt = _exempt_dicts(tree)
    assert len(exempt) == _EXEMPT_DICT_COUNT[module.__name__], (
        "the search_web ingest sub-result changed shape — re-read it before "
        "widening this exemption."
    )

    offenders = [d.lineno for d in _legacy_key_dicts(tree) if id(d) not in exempt]

    assert not offenders, (
        f"{Path(module.__file__).name} still answers legacy error keys at "
        f"lines {offenders} — use tree.mcp.tools.tool_error instead."
    )


# Every tool that CAN answer an envelope, in both modules. Tools that only ever
# answer data (``visualize_memory_embeddings``, the dashboard) are absent on
# purpose — promising an error shape they never emit would be a lie to the model.
_ENVELOPE_TOOLS = [
    ("rag:search_memory", tools.search_memory),
    ("ingest_url", tools.ingest_url),
    ("ingest_file", tools.ingest_file),
    ("ingest_conversation", tools.ingest_conversation),
    ("search_web", tools.search_web),
    ("scrape_web", tools.scrape_web),
    ("graphrag:search_memory", graph_tools.search_memory),
    ("query_memory", graph_tools.query_memory),
    ("deep_search_memory", graph_tools.deep_search_memory),
    ("visualize_memory_graph", graph_tools.visualize_memory_graph),
    ("review_list_pending", graph_tools.review_list_pending),
    ("review_confirm", graph_tools.review_confirm),
    ("review_reject", graph_tools.review_reject),
]


@pytest.mark.parametrize(
    "name,tool", _ENVELOPE_TOOLS, ids=[t[0] for t in _ENVELOPE_TOOLS]
)
def test_every_tool_docstring_states_the_error_contract(name: str, tool) -> None:
    """The docstring IS what the model reads before it decides to retry.

    A tool that answers the envelope but never says so leaves the model
    guessing at ``retryable``; the ONE sentence is asserted verbatim (modulo
    line wrapping) so the wording cannot drift tool by tool.
    """

    summary = " ".join((_tool_fn(tool).__doc__ or "").split())

    assert tools.ERROR_CONTRACT in summary


# ---------------------------------------------------------------------------
# The contract sweep: NO tool raises through FastMCP (ADR-008 §2 Consequences)
# ---------------------------------------------------------------------------

# Every envelope-answering tool with the FIRST call it awaits. Injecting at that
# call is what makes this a contract test rather than thirteen unit tests: a new
# tool added without a trailing catch-all fails here the moment it is listed,
# and a tool left OFF the list fails `test_every_tool_docstring_...`'s sibling
# count check below.
_FIRST_AWAIT = {
    "rag:search_memory": (tools.search_memory, {}, "tree.mcp.tools.retrieve_parents"),
    "ingest_url": (
        tools.ingest_url,
        {"url": "https://example.com/post"},
        "tree.mcp.tools.dispatch_online_pipeline",
    ),
    "ingest_file": (
        tools.ingest_file,
        {"file_path": "/tmp/notes.md", "content": "body"},
        "tree.mcp.tools.dispatch_online_pipeline",
    ),
    "ingest_conversation": (
        tools.ingest_conversation,
        {"conversation_text": "Alice likes Python."},
        "tree.mcp.tools.dispatch_online_pipeline",
    ),
    "search_web": (
        tools.search_web,
        {"query": "prefect"},
        "tree.mcp.tools.web_search",
    ),
    "scrape_web": (
        tools.scrape_web,
        {"urls": ["https://example.com/a"]},
        "tree.mcp.tools._scrape_one",
    ),
    "graphrag:search_memory": (
        graph_tools.search_memory,
        {},
        "tree.mcp.graph_tools.structured_query_memory",
    ),
    "query_memory": (
        graph_tools.query_memory,
        {},
        "tree.mcp.graph_tools.execute_nl_query",
    ),
    "deep_search_memory": (
        graph_tools.deep_search_memory,
        {},
        "tree.mcp.graph_tools.structured_query_memory",
    ),
    "visualize_memory_graph": (
        graph_tools.visualize_memory_graph,
        {},
        "tree.mcp.graph_tools.structured_query_memory",
    ),
    "review_list_pending": (
        graph_tools.review_list_pending,
        {},
        "tree.mcp.graph_tools._find_pending_duplicates",
    ),
    "review_confirm": (
        graph_tools.review_confirm,
        {"source_node_id": "a", "target_node_id": "b", "reviewed_by": "paul"},
        "tree.mcp.graph_tools._review_duplicate",
    ),
    "review_reject": (
        graph_tools.review_reject,
        {"source_node_id": "a", "target_node_id": "b", "reviewed_by": "paul"},
        "tree.mcp.graph_tools._review_duplicate",
    ),
}

# Tools whose first awaited call takes a free-text query: they need a NON-blank
# one, or the blank-query guard answers before the injected failure.
_QUERY_TOOLS = {
    "rag:search_memory",
    "graphrag:search_memory",
    "query_memory",
    "deep_search_memory",
    "visualize_memory_graph",
}


@pytest.mark.parametrize("name", sorted(_FIRST_AWAIT), ids=sorted(_FIRST_AWAIT))
async def test_no_tool_raises_through_fastmcp(mocker, name: str) -> None:
    """An UNENUMERATED failure is still an envelope, on every tool.

    ADR-008 §2's consequence is "no tool raises through FastMCP any more" — a
    protocol error carries neither ``retryable`` nor a message the model can
    act on. ``RuntimeError`` stands in for the exception nobody predicted; the
    answer must be a non-retryable ``internal_error`` rather than a crash.
    """

    tool, kwargs, target = _FIRST_AWAIT[name]
    mocker.patch(
        target, new_callable=AsyncMock, side_effect=RuntimeError("unexpected boom")
    )
    if name in _QUERY_TOOLS:
        kwargs = {**kwargs, "query": "prefect"}

    payload = _envelope(await _tool_fn(tool)(ctx=_make_ctx(), **kwargs))

    assert payload["error_type"] == "internal_error"
    assert payload["retryable"] is False


def test_the_sweep_covers_every_envelope_answering_tool() -> None:
    # The two lists are the same surface seen from two sides (docstring promise
    # vs behaviour); letting them drift is how a tool loses its catch-all.
    assert set(_FIRST_AWAIT) == {name for name, _ in _ENVELOPE_TOOLS}
