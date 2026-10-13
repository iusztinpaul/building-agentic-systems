"""The **Graph download** — how every visualization tool delivers its view.

A tool builds its payload (a **Graph payload** or an **Embedding map**) and
returns through :func:`_graph_tool_result`: the payload is rendered into a
self-contained HTML page (data embedded inline), gzipped and stored as a
**Graph file** in MongoDB (ADR-014 §4) — one row owned by the **Request user**,
named by an unguessable token, dropped by a TTL after
``mcp.graph_file_ttl_seconds``. The answer carries a ``graphs://<token>.html.gz``
resource link and tells the harness to download it and open it in a browser
(:data:`DOWNLOAD_CONTRACT`); :func:`graph_file` serves the row back to its
owner only, as a gzip BLOB, from whichever instance takes the read — the bytes
never touch the server's disk, so a stateless multi-instance host (Prefect
Horizon) serves it. Only on a local ``stdio`` server does the tool ALSO write
``.tree/graphs/<slug>-<stamp>.html`` and open it in a browser, best effort.

No MCP App view: an inline iframe needed a ``ui://`` resource, CSP wiring, a
client-capability check and a second payload copy in the answer — and still
failed through Horizon on large views. One download path is the whole contract.

The **Graph renderer** itself — the payload builders, the shared templates and
the self-contained-file writer — lives in ``tree.memory.visualize.graph``
(dependency direction memory ← mcp); this module owns only the MCP delivery.
"""

import gzip
import logging
import re
import secrets
import webbrowser
from typing import Any

from beanie import PydanticObjectId
from fastmcp import Context
from fastmcp.exceptions import ResourceError
from fastmcp.resources import ResourceContent, ResourceResult
from fastmcp.tools import ToolResult
from mcp import types
from pymongo.errors import PyMongoError

from tree.config.app_config import app_config
from tree.entities.graph_files import GraphFile
from tree.mcp import request_user
from tree.mcp.server import mcp
from tree.memory.visualize.graph import (
    _payload_noun,
    _render_graph_file,
    render_graph_html,
)

logger = logging.getLogger(__name__)

# How a ranked view's model-visible summary names its Documents slider's
# ranking — shared by both modes' ``visualize_memory_structure``.
_ORDER_ADJECTIVE = {"recency": "most-recent", "relevance": "most-relevant"}

#: The **Graph download**'s media type — the resource link, the template and
#: every blob it answers carry it.
GZIP_MIME = "application/gzip"

#: The ONE instruction for downloading a rendered graph through the **Graph
#: download** (``graphs://<name>.html.gz``, a gzip blob). It sits in the file
#: branch's tool text and is a TEST ANCHOR, the ``ERROR_CONTRACT`` pattern:
#: every graph-capable tool's docstring and :func:`graph_file`'s must CONTAIN it
#: verbatim (modulo line wrapping), so the decode step cannot drift between six
#: places. FastMCP reads ``__doc__`` at import time, so the docstrings repeat
#: the text rather than interpolate it.
DOWNLOAD_CONTRACT = (
    "Read the linked `graphs://…html.gz` resource — an `application/gzip` blob: "
    "write its base64 `blob` to a file and run "
    "`base64 -d < blob.b64 | gunzip > <name>.html` "
    "(Python: `gzip.decompress(base64.b64decode(blob))`), "
    "then open the `.html` in a browser."
)


#: The shape of every **Graph file** name the file branch mints:
#: ``secrets.token_urlsafe(16)`` is 22 URL-safe base64 characters, then the
#: ``.html.gz`` the download always carries. Anything else is not a link this
#: server handed out.
_GRAPH_FILE_NAME = re.compile(r"[A-Za-z0-9_-]{22}\.html\.gz")


def _ttl_minutes() -> int:
    """The **Graph file** lifetime in whole minutes (at least 1), read per call."""

    return max(1, app_config.mcp.graph_file_ttl_seconds // 60)


def _minutes(n: int) -> str:
    return f"{n} minute" if n == 1 else f"{n} minutes"


async def _graph_tool_result(
    ctx: Context,
    payload: dict[str, Any],
    summary: str,
    *,
    user_id: PydanticObjectId,
    query: str = "",
) -> ToolResult | str:
    """Deliver a **Graph payload** or **Embedding map** as a **Graph download**.

    The ONE delivery seam behind every visualization tool (ADR-005, decision
    4): the payload is rendered to self-contained HTML, gzipped and stored in
    MongoDB as one row owned by ``user_id`` under an unguessable
    ``<token>.html.gz`` name that expires after ``mcp.graph_file_ttl_seconds``.
    The result carries the ``summary``, the download instruction and a
    ``graphs://<token>.html.gz`` resource link — never the payload JSON, so
    the answer stays small whatever the view's size. Only on a local ``stdio``
    server does it ALSO write ``.tree/graphs/<slug>-<stamp>.html``, open it
    best effort and name the path.

    Args:
        ctx: The MCP request context (for the transport).
        payload: The **Graph payload** from ``to_graph_payload``, or the
            **Embedding map** payload from ``to_embedding_map_payload``.
        summary: The model-visible text. Callers whose tool contract is
            answering a question (``query_memory`` / ``search_memory``) put
            their serialized results in here — it is kept verbatim.
        user_id: The **Request user** — the owner of the **Graph file** row.
        query: Search query text, used to slug the stdio file name.

    Returns:
        The ``ToolResult``, or the ``storage_unavailable`` **Tool error
        envelope** (a ``str``) when the row cannot be written.
    """

    # What the payload IS, in one word: a Graph payload is a "graph", an
    # Embedding map is an "embedding map" (ADR-007 §2).
    noun = _payload_noun(payload)

    # The bytes go to Mongo, not the server's disk: the ``resources/read`` that
    # follows may land on another instance (ADR-014 §4). Gzip at WRITE time —
    # the blob the resource answers is the stored bytes, as is.
    html = render_graph_html(payload)
    name = secrets.token_urlsafe(16) + ".html.gz"
    try:
        await GraphFile(
            user_id=user_id,
            name=name,
            html_gz=gzip.compress(html.encode("utf-8")),
        ).insert()
    except PyMongoError as exc:
        # Function-level: ``tools`` imports this module at load time.
        from tree.mcp.tools import storage_error

        return storage_error("graph file write", exc)

    link = types.ResourceLink(
        type="resource_link",
        uri=f"graphs://{name}",  # type: ignore[arg-type]
        name=name,
        mimeType=GZIP_MIME,
        description=(
            f"gzip-compressed self-contained interactive {noun} — "
            "base64-decode the blob, gunzip, open the .html"
        ),
    )
    ttl = _minutes(_ttl_minutes())

    if ctx.transport == "stdio":
        # The local convenience: the server's disk IS the user's disk.
        path = _render_graph_file(payload, query=query)
        # Best-effort: no-op / harmless on a headless host (returns False or
        # raises, which we swallow).
        opened = False
        try:
            opened = webbrowser.open(path.resolve().as_uri())
        except Exception:  # noqa: BLE001 — opening a browser must never fail the tool.
            opened = False
        closing = (
            "Opened it in your browser."
            if opened
            else (
                "Open it in a browser to explore (drag nodes, zoom/pan). "
                f"{DOWNLOAD_CONTRACT}"
            )
        )
        text = (
            f"{summary}. I saved a self-contained interactive {noun} to:\n"
            f"{path}\n{closing} The graphs:// link expires in about {ttl}."
        )
    else:
        text = (
            f"{summary}. I rendered a self-contained interactive {noun} as a "
            f"download. {DOWNLOAD_CONTRACT} The link expires in about {ttl}."
        )

    return ToolResult(content=[types.TextContent(type="text", text=text), link])


@mcp.resource("graphs://{name}", mime_type=GZIP_MIME)
async def graph_file(name: str, ctx: Context) -> ResourceResult:
    """Gzip of a rendered graph visualization — the **Graph download**.

    Serves the **Graph file** a visualize tool stored in MongoDB, as an MCP
    BLOB (base64 in the frame), to the user who rendered it — any server
    instance can answer. Only the ``<token>.html.gz`` names the tools hand out
    are served, and only until the link expires (a few minutes). Read the
    linked `graphs://…html.gz` resource — an `application/gzip` blob: write its
    base64 `blob` to a file and run `base64 -d < blob.b64 | gunzip > <name>.html`
    (Python: `gzip.decompress(base64.b64decode(blob))`), then open the `.html`
    in a browser.
    """

    # Every failure is a ``ResourceError``: FastMCP's ``read_resource``
    # forwards that class unmasked and masks anything else when
    # ``mask_error_details`` is on — the messages below must reach the client.
    if not _GRAPH_FILE_NAME.fullmatch(name):
        raise ResourceError(
            f"Invalid graph file name: {name!r} (expected <name>.html.gz)"
        )
    try:
        user_id = await request_user.resolve_request_user(ctx)
        # One message for "unknown", "another user's" and "expired" (the TTL
        # monitor deleted it): nothing tells a guesser which.
        row = await GraphFile.find_one(
            GraphFile.name == name, GraphFile.user_id == user_id
        )
    except request_user.RequestUserError as exc:
        raise ResourceError(str(exc)) from exc
    except PyMongoError as exc:
        from tree.mcp.tools import STORAGE_UNAVAILABLE_MESSAGE

        logger.exception("graphs://%s read failed: memory store unreachable", name)
        raise ResourceError(STORAGE_UNAVAILABLE_MESSAGE) from exc
    if row is None:
        raise ResourceError(
            f"Graph file '{name}' not found or expired (download links expire "
            f"after about {_minutes(_ttl_minutes())}) — run the visualize tool "
            "again."
        )
    # The mime type is set HERE, not left to the decorator: FastMCP 3.2 wraps a
    # TEMPLATE's bare ``bytes`` return as ``application/octet-stream`` (only
    # static resources forward their declared ``mime_type``); the decorator's
    # copy is what ``list`` shows. ``bytes`` content becomes a base64
    # ``BlobResourceContents``.
    return ResourceResult([ResourceContent(row.html_gz, mime_type=GZIP_MIME)])
