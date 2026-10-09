"""Unit tests for the MODE-NEUTRAL MCP App layer (:mod:`tree.mcp.viz_app`).

Covers ``_graph_tool_result`` — the ONE dual-path helper every visualization
tool delivers through (ADR-005, decision 4; payload in a ``content`` JSON block,
because the App-UI host does not forward ``structuredContent`` to a custom
iframe) — the file branch's **Graph file** row and its expiring
``graphs://<token>.html.gz`` link (the **Graph download**, ADR-014 §4), and both
resource handlers. The **Graph file** rows go to the session's REAL test
database (``tests/unit/conftest.py`` boots Beanie), so the ``{name, user_id}``
scoping is proven against Mongo, not against a mock's call args. The TOOLS that
call the helper are tested where they are registered (``test_graph_tools.py`` /
``test_tools.py``); the Graph renderer it delegates to lives in
``tree.memory.visualize.graph`` and is tested in
``tests/unit/memory/visualize/test_graph.py``.
"""

import base64
import gzip
import inspect
import json
import re
import secrets
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from beanie import PydanticObjectId
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ResourceError
from fastmcp.tools import ToolResult
from mcp.shared.exceptions import McpError
from mcp.types import BlobResourceContents
from pymongo.errors import PyMongoError, ServerSelectionTimeoutError

from tree.config.app_config import app_config
from tree.entities.graph_files import GraphFile
from tree.mcp import tools as mcp_tools
from tree.mcp.request_user import RequestUserError, unknown_identifier_message
from tree.mcp.server import mcp
from tree.mcp.viz_app import (
    _GRAPH_HTML,
    DOWNLOAD_CONTRACT,
    GRAPH_VIEW_URI,
    _graph_tool_result,
    _ui_capability,
    graph_file,
    graph_view,
)
from tree.memory.types import QueryResult
from tree.memory.visualize.graph import (
    _D3_FORCE_CDN,
    _FILE_HTML_BASE,
    _payload_noun,
    render_graph_html,
    to_graph_payload,
)

_UID = "65f1a2b3c4d5e6f7a8b9c0d1"
_TOKEN_NAME = re.compile(r"[A-Za-z0-9_-]{22}\.html\.gz")


def _node(node_id: str, node_type: str, **props: object) -> dict:
    return {
        "_id": node_id,
        "kind": "node",
        "type": node_type,
        "properties": props,
    }


def _edge(source: str, edge_type: str, target: str) -> dict:
    return {
        "_id": f"{source}|{edge_type}|{target}",
        "kind": "edge",
        "type": edge_type,
        "source_node_id": source,
        "target_node_id": target,
    }


def _seed_result() -> QueryResult:
    alice = f"{_UID}:person:alice"
    paper = f"{_UID}:document:paper"
    return QueryResult(
        nodes=[_node(alice, "person"), _node(paper, "document")],
        edges=[_edge(alice, "mentions", paper)],
    )


def _make_ctx(*, ui_supported: bool, transport: str = "streamable-http") -> MagicMock:
    ctx = MagicMock()
    ctx.client_supports_extension.return_value = ui_supported
    ctx.transport = transport
    ctx.lifespan_context = {
        "client": MagicMock(),
        "database": "test",
        "embedding_model": MagicMock(),
    }
    return ctx


def _unknown_ctx(*, how: str, transport: str = "streamable-http") -> MagicMock:
    """A ctx whose capability cannot be known — the stateless-HTTP shape.

    ``client_supports_extension`` answers ``False`` here, exactly as FastMCP
    does on a session that never saw ``initialize``.
    """

    ctx = _make_ctx(ui_supported=False, transport=transport)
    if how == "stateless-session":
        ctx.session.client_params = None
    else:
        ctx.request_context = None
    return ctx


@pytest.fixture
def graphs_dir(mocker, tmp_path: Path) -> Path:
    """Where ``_render_graph_file`` writes by default — empty unless stdio wrote."""

    mocker.patch("tree.memory.visualize.graph.GRAPHS_DIR", tmp_path)
    return tmp_path


@pytest.fixture
def browser_open(mocker) -> MagicMock:
    return mocker.patch("tree.mcp.viz_app.webbrowser.open", return_value=False)


@pytest.fixture
def ttl_300(mocker) -> None:
    """Pin the configured TTL so the expiry sentence is deterministic."""

    mocker.patch.object(app_config.mcp, "graph_file_ttl_seconds", 300)


async def _stored(name: str) -> GraphFile:
    row = await GraphFile.find_one(GraphFile.name == name)
    assert row is not None, name
    return row


# ---------------------------------------------------------------------------
# _graph_tool_result — the ONE dual-path seam behind every graph tool
# ---------------------------------------------------------------------------


async def test_graph_tool_result_keeps_summary_model_visible_and_payload_user_only(
    request_user_id: PydanticObjectId,
) -> None:
    payload = to_graph_payload(_seed_result())
    ctx = _make_ctx(ui_supported=True)

    result = await _graph_tool_result(
        ctx, payload, "SUMMARY-SENTINEL", user_id=request_user_id
    )

    # Assert: the model reads the summary; the full node/edge dump is addressed
    # to the iframe alone (audience=["user"]) and travels ONCE — no
    # structured_content copy counting twice against the response cap.
    summary_block, payload_block = result.content
    assert "SUMMARY-SENTINEL" in summary_block.text
    assert summary_block.annotations is None
    assert payload_block.annotations.audience == ["user"]
    assert json.loads(payload_block.text) == payload
    assert result.structured_content is None


@pytest.mark.parametrize(
    ("ui_supported", "how", "expected"),
    [
        (True, None, "supported"),
        (False, None, "declined"),
        (False, "stateless-session", "unknown"),
        (False, "no-request-context", "unknown"),
    ],
)
def test_the_capability_has_three_states(
    ui_supported: bool, how: str | None, expected: str
) -> None:
    ctx = _make_ctx(ui_supported=ui_supported) if how is None else _unknown_ctx(how=how)

    assert _ui_capability(ctx) == expected


@pytest.mark.usefixtures("ttl_300")
@pytest.mark.parametrize("how", ["stateless-session", "no-request-context"])
@pytest.mark.parametrize("transport", ["streamable-http", "stdio"])
async def test_an_unknown_capability_answers_the_payload_and_the_download(
    graphs_dir: Path,
    browser_open: MagicMock,
    request_user_id: PydanticObjectId,
    how: str,
    transport: str,
) -> None:
    # Story: claude.ai on Horizon (stateless HTTP) — the host mounted the
    # iframe from tools/list, the server cannot tell whether it did.
    payload = to_graph_payload(_seed_result())

    result = await _graph_tool_result(
        _unknown_ctx(how=how, transport=transport),
        payload,
        "SUMMARY-SENTINEL",
        user_id=request_user_id,
    )

    # Assert: the iframe's data (user-only, once) AND a working download.
    text_block, payload_block, link_block = result.content
    assert payload_block.annotations.audience == ["user"]
    assert json.loads(payload_block.text) == payload
    row = await _stored(link_block.name)
    assert row.user_id == request_user_id
    assert str(link_block.uri) == f"graphs://{row.name}"
    # The text never claims the client cannot render: it may well have.
    assert text_block.annotations is None
    assert text_block.text.startswith("SUMMARY-SENTINEL (interactive graph view).")
    assert "does not render" not in text_block.text
    assert DOWNLOAD_CONTRACT in text_block.text
    assert text_block.text.endswith("The link expires in about 5 minutes.")
    # No local file or browser on top of a view the host may be showing.
    assert list(graphs_dir.iterdir()) == []
    browser_open.assert_not_called()
    assert result.structured_content is None


async def test_as_html_file_answers_only_the_download_when_the_capability_is_unknown(
    graphs_dir: Path, browser_open: MagicMock, request_user_id: PydanticObjectId
) -> None:
    # Story: Claude Code on Horizon follows the tool description's opt-out.
    result = await _graph_tool_result(
        _unknown_ctx(how="stateless-session"),
        to_graph_payload(_seed_result()),
        "SUMMARY",
        user_id=request_user_id,
        as_html_file=True,
    )

    text_block, link_block = result.content
    assert link_block.type == "resource_link"
    assert text_block.text.startswith("SUMMARY. Since you asked for an HTML file")


@pytest.mark.usefixtures("ttl_300")
async def test_the_http_file_branch_stores_a_graph_file_and_links_it(
    graphs_dir: Path, browser_open: MagicMock, request_user_id: PydanticObjectId
) -> None:
    # Story: Codex downloads the map from Horizon across instances.
    payload = to_graph_payload(_seed_result())

    result = await _graph_tool_result(
        _make_ctx(ui_supported=False),
        payload,
        "SUMMARY-SENTINEL",
        user_id=request_user_id,
        query="alice",
    )

    # Assert: ONE row, owned by the request user, under a token name, whose
    # bytes gunzip to the very page the renderer produces.
    text_block, link_block = result.content
    assert _TOKEN_NAME.fullmatch(link_block.name), link_block.name
    row = await _stored(link_block.name)
    assert row.user_id == request_user_id
    assert gzip.decompress(row.html_gz).decode("utf-8") == render_graph_html(payload)
    assert link_block.type == "resource_link"
    assert str(link_block.uri) == f"graphs://{row.name}"
    assert link_block.mimeType == "application/gzip"
    # The text: the summary, the decode contract, the expiry — and NOTHING the
    # remote client cannot use (no server path, no browser on the server).
    assert "SUMMARY-SENTINEL" in text_block.text
    assert DOWNLOAD_CONTRACT in text_block.text
    assert text_block.text.endswith("The link expires in about 5 minutes.")
    assert str(graphs_dir) not in text_block.text
    assert ".tree/graphs" not in text_block.text
    assert list(graphs_dir.iterdir()) == []
    browser_open.assert_not_called()
    # No payload block on this branch — the data is inside the file.
    assert not any(b.type == "text" and b.text.startswith("{") for b in result.content)


async def test_every_render_gets_its_own_token(
    graphs_dir: Path, browser_open: MagicMock, request_user_id: PydanticObjectId
) -> None:
    payload = to_graph_payload(_seed_result())
    ctx = _make_ctx(ui_supported=False)

    first = await _graph_tool_result(ctx, payload, "S", user_id=request_user_id)
    second = await _graph_tool_result(ctx, payload, "S", user_id=request_user_id)

    assert first.content[1].name != second.content[1].name


@pytest.mark.parametrize(
    ("ttl_seconds", "sentence"),
    [
        (300, "expires in about 5 minutes."),
        (90, "expires in about 1 minute."),
        (30, "expires in about 1 minute."),  # never "about 0 minutes"
    ],
)
async def test_the_expiry_sentence_follows_the_configured_ttl(
    mocker,
    graphs_dir: Path,
    browser_open: MagicMock,
    request_user_id: PydanticObjectId,
    ttl_seconds: int,
    sentence: str,
) -> None:
    mocker.patch.object(app_config.mcp, "graph_file_ttl_seconds", ttl_seconds)

    result = await _graph_tool_result(
        _make_ctx(ui_supported=False),
        to_graph_payload(_seed_result()),
        "S",
        user_id=request_user_id,
    )

    assert result.content[0].text.endswith(sentence)


@pytest.mark.usefixtures("ttl_300")
async def test_the_stdio_file_branch_also_saves_and_opens_the_local_file(
    graphs_dir: Path, browser_open: MagicMock, request_user_id: PydanticObjectId
) -> None:
    # Story: the local user is not bothered with gzip.
    browser_open.return_value = True
    payload = to_graph_payload(_seed_result())

    result = await _graph_tool_result(
        _make_ctx(ui_supported=False, transport="stdio"),
        payload,
        "SUMMARY",
        user_id=request_user_id,
        query="alice",
    )

    # Assert: the row AND the local file — two names, the token in the URI and
    # the slug+stamp on disk — and the link survives in case the open failed.
    text_block, link_block = result.content
    row = await _stored(link_block.name)
    assert row.user_id == request_user_id
    (written,) = graphs_dir.iterdir()
    assert re.fullmatch(r"alice-\d{8}-\d{6}\.html", written.name), written.name
    assert written.read_text(encoding="utf-8") == render_graph_html(payload)
    browser_open.assert_called_once_with(written.resolve().as_uri())
    assert f"to:\n{written}\n" in text_block.text
    assert text_block.text.endswith(
        "Opened it in your browser. The graphs:// link expires in about 5 minutes."
    )
    assert DOWNLOAD_CONTRACT not in text_block.text
    assert str(link_block.uri) == f"graphs://{row.name}"


@pytest.mark.usefixtures("ttl_300")
@pytest.mark.parametrize(
    "open_outcome",
    [{"return_value": False}, {"side_effect": RuntimeError("no browser")}],
    ids=["open-returns-false", "open-raises"],
)
async def test_a_stdio_browser_open_that_fails_answers_the_contract(
    mocker,
    graphs_dir: Path,
    request_user_id: PydanticObjectId,
    open_outcome: dict,
) -> None:
    # Arrange: a headless stdio host has no browser to open.
    mocker.patch("tree.mcp.viz_app.webbrowser.open", **open_outcome)

    result = await _graph_tool_result(
        _make_ctx(ui_supported=False, transport="stdio"),
        to_graph_payload(_seed_result()),
        "SUMMARY",
        user_id=request_user_id,
    )

    # Assert: swallowed — a missing browser never fails the tool; the text
    # falls back to the decode contract and still states the expiry.
    assert isinstance(result, ToolResult)
    text_block, link_block = result.content
    assert link_block.type == "resource_link"
    assert str(graphs_dir) in text_block.text
    assert DOWNLOAD_CONTRACT in text_block.text
    assert text_block.text.endswith("The graphs:// link expires in about 5 minutes.")


@pytest.mark.parametrize("transport", ["streamable-http", "stdio"])
async def test_a_failed_insert_answers_storage_unavailable(
    mocker,
    graphs_dir: Path,
    browser_open: MagicMock,
    request_user_id: PydanticObjectId,
    transport: str,
) -> None:
    mocker.patch.object(
        GraphFile, "insert", side_effect=ServerSelectionTimeoutError("mongo down")
    )

    result = await _graph_tool_result(
        _make_ctx(ui_supported=False, transport=transport),
        to_graph_payload(_seed_result()),
        "SUMMARY",
        user_id=request_user_id,
    )

    # Assert: the retryable envelope, and nothing half-delivered — no local
    # file, no browser, no link to a row that does not exist.
    assert json.loads(result) == {
        "error_type": "storage_unavailable",
        "retryable": True,
        "message": mcp_tools.STORAGE_UNAVAILABLE_MESSAGE,
    }
    assert list(graphs_dir.iterdir()) == []
    browser_open.assert_not_called()


def _map_payload() -> dict:
    """A fixed-layout payload, as ``to_embedding_map_payload`` builds it."""

    return {
        "nodes": [
            {
                "id": "chunk-1",
                "type": "chunk",
                "name": "Paper",
                "x": 1.0,
                "y": 2.0,
                "cluster_id": 0,
                "meta": {},
            }
        ],
        "edges": [],
        "layout": "fixed",
        "summary": "Embedding map: 1 chunks in 1 clusters (+0 noise)",
    }


@pytest.mark.parametrize(
    ("payload", "noun", "other"),
    [
        (to_graph_payload(_seed_result()), "graph", None),
        (_map_payload(), "embedding map", "graph"),
    ],
    ids=["graph", "embedding-map"],
)
@pytest.mark.parametrize("transport", ["streamable-http", "stdio"])
async def test_every_branch_names_the_payload_by_its_noun(
    graphs_dir: Path,
    browser_open: MagicMock,
    request_user_id: PydanticObjectId,
    payload: dict,
    noun: str,
    other: str | None,
    transport: str,
) -> None:
    inline = await _graph_tool_result(
        _make_ctx(ui_supported=True, transport=transport),
        payload,
        "SUMMARY",
        user_id=request_user_id,
    )
    fallback = await _graph_tool_result(
        _make_ctx(ui_supported=False, transport=transport),
        payload,
        "SUMMARY",
        user_id=request_user_id,
    )

    # Assert: in rag mode there is no graph at all, so every string the model
    # (or the user) reads calls the map a map — the glossary keeps **Embedding
    # map** and **Graph payload** distinct, and the copy follows.
    assert inline.content[0].text == f"SUMMARY (interactive {noun} view)."
    text_block, link_block = fallback.content
    assert (
        "SUMMARY. Since this client does not render inline MCP App UIs, I "
    ) in text_block.text
    assert f"self-contained interactive {noun} " in text_block.text
    assert link_block.description == (
        f"gzip-compressed self-contained interactive {noun} — base64-decode the "
        "blob, gunzip, open the .html"
    )
    if other is not None:
        assert f"interactive {other}" not in text_block.text


async def test_as_html_file_names_the_request_as_the_reason(
    graphs_dir: Path, browser_open: MagicMock, request_user_id: PydanticObjectId
) -> None:
    result = await _graph_tool_result(
        _make_ctx(ui_supported=True),
        to_graph_payload(_seed_result()),
        "SUMMARY",
        user_id=request_user_id,
        as_html_file=True,
    )

    assert result.content[0].text.startswith(
        "SUMMARY. Since you asked for an HTML file, I rendered a self-contained "
        "interactive graph as a download."
    )


def test_the_delivered_noun_comes_from_the_payloads_layout_key() -> None:
    # Assert: ONE mechanism decides the wording everywhere (the delivery helper
    # AND the file writer's log line) — the payload's own ``layout`` key.
    assert _payload_noun({"layout": "fixed"}) == "embedding map"
    assert _payload_noun({"nodes": [], "edges": []}) == "graph"


# ---------------------------------------------------------------------------
# graphs://{name} — the Graph download, read from MongoDB per Request user
# ---------------------------------------------------------------------------


async def _insert_row(user_id: PydanticObjectId) -> tuple[GraphFile, str]:
    """Store a rendered graph the way the file branch does; return it + its HTML."""

    html = render_graph_html(to_graph_payload(_seed_result()))
    name = secrets.token_urlsafe(16) + ".html.gz"
    row = GraphFile(
        user_id=user_id, name=name, html_gz=gzip.compress(html.encode("utf-8"))
    )
    await row.insert()
    return row, html


def _not_found(name: str, minutes: str = "5 minutes") -> str:
    return (
        f"Graph file '{name}' not found or expired (download links expire after "
        f"about {minutes}) — run the visualize tool again."
    )


async def test_graph_file_resource_serves_the_request_users_row(
    request_user_id: PydanticObjectId,
) -> None:
    row, html = await _insert_row(request_user_id)

    result = await graph_file(row.name, MagicMock())

    # Assert: the stored gzip, as is, as an application/gzip blob.
    (content,) = result.contents
    assert isinstance(content.content, bytes)
    assert content.mime_type == "application/gzip"
    assert content.content == row.html_gz
    assert gzip.decompress(content.content).decode("utf-8") == html


@pytest.mark.usefixtures("ttl_300")
async def test_another_users_row_reads_exactly_like_an_unknown_one(
    request_user_id: PydanticObjectId,
) -> None:
    # Story: another user guesses a link — nothing distinguishes "not yours"
    # from "gone" (an expired row is a deleted row: the same lookup miss).
    alices_row, _ = await _insert_row(PydanticObjectId())
    unknown = "Q2hhbmdlTWVQbGVhc2UxMj.html.gz"

    with pytest.raises(ResourceError) as foreign:
        await graph_file(alices_row.name, MagicMock())
    with pytest.raises(ResourceError) as missing:
        await graph_file(unknown, MagicMock())

    assert str(foreign.value) == _not_found(alices_row.name)
    assert str(missing.value) == _not_found(unknown)


@pytest.mark.parametrize(
    "bad_name",
    [
        "embedding-map-20261003-181500.html",  # the retired text name
        "alice-x.html.gz",  # a pre-ADR-014 slug+stamp name
        "../Q2hhbmdlTWVQbGVhc2UxMj.html.gz",
        "../../../etc/passwd.gz",
        "Q2hhbmdlTWVQbGVhc2UxMj.txt.gz",
        "Q2hhbmdlTWVQbGVhc2UxM.html.gz",  # 21 chars: not a token_urlsafe(16)
        "Q2hhbmdlTWVQbGVhc2UxMj.html.gz.gz",
        "Q2hhbmdlTWVQbGVhc2UxMj.html.gz\n",
        "Q2hhbmdlTWVQbGVhc2Ux\x00j.html.gz",  # NUL
        ".gz",
    ],
)
async def test_graph_file_resource_rejects_names_it_never_handed_out(
    bad_name: str, _patched_request_user: AsyncMock
) -> None:
    with pytest.raises(
        ResourceError,
        match=r"^Invalid graph file name: .*\(expected <name>\.html\.gz\)$",
    ):
        await graph_file(bad_name, MagicMock())

    # The guard runs before the seam: a malformed name costs no users read.
    _patched_request_user.assert_not_awaited()


@pytest.mark.parametrize(
    "seam_error",
    [
        RequestUserError(unknown_identifier_message("bob@example.com")),
        RequestUserError("No horizon-actor-email header on this request."),
    ],
    ids=["unknown-identifier", "missing-header"],
)
async def test_a_seam_failure_reaches_the_reader_with_the_seams_message(
    mocker, seam_error: RequestUserError
) -> None:
    mocker.patch(
        "tree.mcp.request_user.resolve_request_user",
        new_callable=AsyncMock,
        side_effect=seam_error,
    )

    with pytest.raises(ResourceError) as raised:
        await graph_file("Q2hhbmdlTWVQbGVhc2UxMj.html.gz", MagicMock())

    assert str(raised.value) == str(seam_error)


@pytest.mark.parametrize("failing", ["seam", "find_one"])
async def test_an_unreachable_store_answers_the_storage_message(
    mocker, failing: str
) -> None:
    if failing == "seam":
        mocker.patch(
            "tree.mcp.request_user.resolve_request_user",
            new_callable=AsyncMock,
            side_effect=PyMongoError("users unreachable"),
        )
    else:
        mocker.patch.object(
            GraphFile, "find_one", side_effect=PyMongoError("graph_files unreachable")
        )

    with pytest.raises(ResourceError) as raised:
        await graph_file("Q2hhbmdlTWVQbGVhc2UxMj.html.gz", MagicMock())

    assert str(raised.value) == mcp_tools.STORAGE_UNAVAILABLE_MESSAGE


def test_graph_file_reads_no_filesystem_path() -> None:
    # Source guard: the bytes live in Mongo — the server's disk is not
    # consulted, so any instance answers (ADR-014 §4).
    source = inspect.getsource(getattr(graph_file, "fn", graph_file))

    assert "GRAPHS_DIR" not in source
    assert "Path" not in source


def test_graph_file_docstring_states_the_download_contract() -> None:
    doc = " ".join((graph_file.__doc__ or "").split())

    assert DOWNLOAD_CONTRACT in doc


def test_the_download_contract_presupposes_no_path() -> None:
    assert DOWNLOAD_CONTRACT.startswith("Read the linked `graphs://…html.gz` resource")
    assert "path" not in DOWNLOAD_CONTRACT


async def test_the_registered_graphs_template_declares_application_gzip() -> None:
    template = await mcp.get_resource_template("graphs://{name}")

    assert template is not None
    assert template.mime_type == "application/gzip"


async def _bare_server(*, mask_error_details: bool = False) -> FastMCP:
    """The REAL registered template on a bare server: the client round trip
    skips the app lifespan (Mongo client + models) it does not need."""

    server = FastMCP("graph-download-test", mask_error_details=mask_error_details)
    server.add_template(await mcp.get_resource_template("graphs://{name}"))
    return server


async def test_a_client_reads_the_download_as_a_base64_gzip_blob(
    request_user_id: PydanticObjectId,
) -> None:
    row, html = await _insert_row(request_user_id)

    async with Client(await _bare_server()) as client:
        contents = await client.read_resource(f"graphs://{row.name}")

    # Assert: the protocol carries the blob base64-encoded; the documented
    # decode gives back the rendered page byte for byte.
    (content,) = contents
    assert isinstance(content, BlobResourceContents)
    assert content.mimeType == "application/gzip"
    assert gzip.decompress(base64.b64decode(content.blob)).decode("utf-8") == html


@pytest.mark.usefixtures("ttl_300")
async def test_the_not_found_message_survives_error_masking() -> None:
    # Story: the link is read too late. A plain exception would reach the
    # client as a masked "Error reading resource"; a ResourceError keeps its
    # message even with ``mask_error_details`` on (Horizon's hosts may set it).
    name = "Q2hhbmdlTWVQbGVhc2UxMj.html.gz"

    async with Client(await _bare_server(mask_error_details=True)) as client:
        with pytest.raises(McpError) as raised:
            await client.read_resource(f"graphs://{name}")

    assert str(raised.value) == _not_found(name)


async def test_the_contracts_shell_one_liner_decodes_the_blob(
    tmp_path: Path, request_user_id: PydanticObjectId
) -> None:
    # Regression: ``base64 -d blob.b64`` (a positional file) fails on macOS's
    # BSD base64 ("invalid argument"); the contract must run as written on
    # both BSD and GNU, so the test runs the contract's OWN command.
    row, html = await _insert_row(request_user_id)
    (content,) = (await graph_file(row.name, MagicMock())).contents
    (tmp_path / "blob.b64").write_text(base64.b64encode(content.content).decode())
    match = re.search(r"`(base64 -d [^`]+)`", DOWNLOAD_CONTRACT)
    assert match is not None
    command = match.group(1).replace("<name>", "decoded")

    subprocess.run(command, shell=True, check=True, cwd=tmp_path)  # noqa: S602

    assert (tmp_path / "decoded.html").read_text(encoding="utf-8") == html


@pytest.mark.parametrize(
    "retired",
    ["save its text", "If that path is not on your machine", "server-side path"],
)
def test_no_mcp_source_still_carries_a_retired_download_sentence(
    retired: str,
) -> None:
    # Source guard: the text download and the path-first contract are retired
    # everywhere in the MCP layer (task 189 updates the skill / tutorial).
    mcp_src = Path(__file__).parents[3] / "src" / "tree" / "mcp"
    offenders = [
        p.name
        for p in mcp_src.rglob("*.py")
        if retired in p.read_text(encoding="utf-8")
    ]

    assert mcp_src.is_dir()
    assert offenders == []


def test_graph_html_reads_only_the_content_blocks() -> None:
    # Assert: the widget parses the content JSON block — the one channel the
    # payload rides in (ADR-014 §5); the structuredContent fallback is gone.
    assert "ontoolresult" in _GRAPH_HTML
    assert "r.content" in _GRAPH_HTML
    assert "structuredContent" not in _GRAPH_HTML


def test_no_mcp_source_sends_structured_content() -> None:
    # Source guard: the payload is sent once, in the audience=["user"] content
    # block; no MCP tool re-adds the structured_content copy (ADR-014 §5).
    mcp_src = Path(__file__).parents[3] / "src" / "tree" / "mcp"
    offenders = [
        p.name
        for p in mcp_src.rglob("*.py")
        if "structured_content" in p.read_text(encoding="utf-8")
    ]

    assert mcp_src.is_dir()
    assert offenders == []


# ---------------------------------------------------------------------------
# ui:// resource — the ONE iframe every visualization tool points at
# ---------------------------------------------------------------------------


async def test_graph_view_resource_is_registered_by_the_neutral_app_layer() -> None:
    # Assert: importing viz_app alone (no tool module) registers the shared
    # ui:// resource, so a rag-mode server can serve it too (ADR-007 §7).
    assert (await mcp.get_resource(GRAPH_VIEW_URI)) is not None
    assert graph_view() == _GRAPH_HTML


# ---------------------------------------------------------------------------
# ONE template, two variants — the iframe must not drift from the file
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "token",
    [
        'const isFixed = payload.layout === "fixed";',  # fixed coordinates
        "if (legendRows) {",  # payload legend
        '<div id="warning" hidden></div>',  # stale-map banner
        '<label id="hulls-toggle" hidden>',  # hull toggle
        '<canvas id="overlay"></canvas>',  # hulls + pin dots
        "function convexHull(points)",
        "renderer.graphToViewport({ x: p[0], y: p[1] })",
        'renderer.on("afterRender", drawOverlay)',
        'renderer.on("resize", drawOverlay)',
        "      if (!isFixed) {\n        sim = forceSimulation(visibleSimNodes())",
        '"doubleClickNode"',
        "setCustomBBox(",
        "alphaTarget(0.3)",
        "alphaTarget(0)",
        "alpha(0.5)",
        "dataset.layout",
        "dataset.sim",
        "function drawOverlay",
        "state.selection",  # selection, marquee and group drag (task 163)
        "shiftKey",
        '"Escape"',
        '"downStage"',
        'relType === "part_of"',
        "function dragSetFor(nodeId)",
        "#ea580c",
        "setLineDash([4, 3])",
        'id="panel"',  # the Controls panel (task 164)
        'id="panel-toggle"',
        "function rangeRow",
        "function checkboxRow",
        '"Centre force"',
        '"Gravity"',
        "forceX(",
        "forceY(",
        '"Repel force"',
        '"Link force"',
        '"Link distance"',
        '"Node size"',
        '"Link thickness"',
        '"Label fade"',
        '"Arrows"',
        '"Edge labels"',
        '"Pause"',
        '"Resume"',
        '"Unpin all"',
        '"Reset to defaults"',
        'renderer.setSetting("labelRenderedSizeThreshold", v)',
        'renderer.setSetting("renderEdgeLabels", on)',
        'document.body.dataset.sim = "paused"',
        "const forces = isFixed ? null : payload.controls.forces;",
        'renderer.on("doubleClickStage", (e) => {',
        "clickOnNode(e.node, shiftKey)",
        "marquee.cancelled = true",
        "if (e.original.buttons === 0) { captor.handleUp(e.original); return; }",
        '"Documents"',  # the Full graph's Documents slider (task 165)
        "const documents = payload.controls && payload.controls.documents;",
        "if (documents) {",
        "function applyDocumentLimit(n)",
        "sim.nodes(visibleSimNodes());",
        'sim.force("link").links(visibleLinks());',
        '"hidden"',
        "document.body.dataset.docs",
        '" of "',
        '" documents · "',
        "ORDER_LABEL",  # task 168: the row label names the order
        '"Most recent"',
        '"Most relevant"',
        "ORDER_LABEL[documents.order]",
        "function fitView()",
        "function autoFit()",
        "if (fitOnSettle) { fitOnSettle = false; autoFit(); }",
        "if (!sim || paused) autoFit();",  # task 166: paused reveal re-arms
        "{ fitOnSettle = false; pan = null; }",  # a plain pan cancels the fit
        "n.childCount",  # the hidden child count (ADR-011 §8)
        '"child chunks"',
        '" (not shown)"',
    ],
)
@pytest.mark.parametrize(
    "variant", [_GRAPH_HTML, _FILE_HTML_BASE], ids=["iframe", "file"]
)
def test_both_variants_carry_the_embedding_map_extensions(
    variant: str, token: str
) -> None:
    # Assert: the map extensions live in the SHARED pieces (_GRAPH_STYLE /
    # _BODY_MARKUP / _RENDER_JS), so the ui:// iframe and the self-contained
    # file draw a map identically — no second template to keep in sync.
    assert token in variant


@pytest.mark.parametrize(
    "variant", [_GRAPH_HTML, _FILE_HTML_BASE], ids=["iframe", "file"]
)
def test_both_variants_import_d3_force_and_no_other_layout_engine(
    variant: str,
) -> None:
    # Assert: exactly one pinned d3-force ESM import naming the four forces,
    # and the one-shot layout engine is gone (spelled without its name so the
    # task's "no hits" grep over the tests stays clean).
    assert variant.count(f'from "{_D3_FORCE_CDN}"') == 1
    assert (
        "import { forceSimulation, forceLink, forceManyBody, forceCenter, forceX, forceY } "
        f'from "{_D3_FORCE_CDN}";'
    ) in variant
    assert "atlas" not in variant.lower()
    for gone in ("hulls-layer", "drawHulls", "Math.random()"):
        assert gone not in variant


@pytest.mark.parametrize(
    "variant", [_GRAPH_HTML, _FILE_HTML_BASE], ids=["iframe", "file"]
)
def test_both_variants_drop_the_single_selected_id(variant: str) -> None:
    # Assert: the Set-based selection replaced the single id everywhere.
    assert "state.selected" not in variant


def test_the_iframe_variant_only_adds_the_ext_apps_runtime() -> None:
    # Assert: the two variants differ ONLY in the MCP-layer concerns (the
    # ext-apps runtime + the ontoolresult channel) and the body height.
    assert "ext-apps" in _GRAPH_HTML
    assert "ext-apps" not in _FILE_HTML_BASE
    assert "height: 760px" in _GRAPH_HTML
    assert "height: 100vh" in _FILE_HTML_BASE
