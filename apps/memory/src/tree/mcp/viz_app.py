"""MCP App layer of the **Graph renderer** — MODE-NEUTRAL, no tools of its own.

What a rendered view needs from MCP, and nothing else (ADR-007 §7): the
``ui://`` iframe resource, the ``graphs://`` download resource, and the ONE
dual-delivery helper every visualization tool returns through. The tools
themselves live with their mode — ``visualize_memory_structure`` is two
functions under one name (the rag **Memory structure** tree in
:mod:`tree.mcp.tools`, the knowledge graph in :mod:`tree.mcp.graph_tools`), the
**Embedding map** tool (both modes) in :mod:`tree.mcp.tools`. So a rag-mode
server can import this module without pulling a single graph tool in with it.

The flow follows the low-level MCP Apps pattern
(https://gofastmcp.com/apps/low-level): a tool runs its query and returns the
payload, and ``graph_view`` serves the ``ui://`` HTML resource — a sandboxed
iframe that receives the tool result via the MCP Apps ``ontoolresult`` channel
and draws it.

**Rendering stack.** This module owns MCP concerns ONLY. The **Graph renderer**
itself — the **Graph payload** builder, the shared CSS/DOM/JS templates, and
the self-contained-file writer — lives in ``tree.memory.visualize.graph`` and
is imported from here (dependency direction memory ← mcp). What stays local:
the ext-apps iframe runtime CDN, the ``ui://`` HTML variant, CSP wiring, and
the ``graphs://`` resource.

**Fallback (Option B).** Not every client renders MCP App UIs (e.g. the
Claude Code terminal, or agentic surfaces that only consume tool text).
:func:`_graph_tool_result` checks ``ctx.client_supports_extension(UI_EXTENSION_ID)``;
when the UI extension is absent — or when the caller explicitly asks via
``as_html_file`` — it renders the *same* graph into a self-contained HTML page
(data embedded inline, no ext-apps round-trip), gzips it and stores it as a
**Graph file** in MongoDB (ADR-014 §4): one row owned by the **Request user**,
named by an unguessable token, dropped by a TTL after
``mcp.graph_file_ttl_seconds``. The answer carries a
``graphs://<token>.html.gz`` resource link (the **Graph download**) and says
when it expires; :func:`graph_file` serves the row back to its owner only, as a
gzip BLOB, from whichever instance takes the read — the bytes never touch the
server's disk, so a stateless multi-instance host (Prefect Horizon) serves it.
The decode instruction is the one :data:`DOWNLOAD_CONTRACT`. Only on a local
``stdio`` server does the branch ALSO write ``.tree/graphs/<slug>-<stamp>.html``
and open it in a browser, best effort. Either way the slow path stays off the
model: it never hand-authors HTML.

**One dual-path helper for every visualization tool (ADR-005, decision 4).**
:func:`_graph_tool_result` owns the capability check and BOTH branches, and is
the only place either exists. Every graph-capable MCP tool calls it —
``visualize_memory_structure`` plus ``query_memory(visualize=True)`` and
``search_memory(visualize=True)`` (via ``graph_tools._dual_graph_result``) — so
from a visualization standpoint they behave identically. A new tool builds a
payload and calls this helper; it never reimplements a branch.

The iframe payload travels in a ``content`` JSON block (a custom HTML app reads
the tool result's ``content`` via ``ontoolresult`` — ``structuredContent`` is
FastMCP's *Prefab*-renderer channel and is NOT forwarded to a custom iframe).
That block is marked ``audience=["user"]`` so the iframe gets the full node/edge
dump while the MODEL sees only the short text summary. The JS reads ``content``
first, then falls back to ``structuredContent`` for hosts that forward it.
"""

import gzip
import json
import logging
import re
import secrets
import webbrowser
from typing import Any

from beanie import PydanticObjectId
from fastmcp import Context
from fastmcp.apps import UI_EXTENSION_ID, AppConfig, ResourceCSP
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
    _resolve_static,
    render_graph_html,
)

logger = logging.getLogger(__name__)

GRAPH_VIEW_URI = "ui://tree-memory/graph.html"

# How a ranked view's model-visible summary names its Documents slider's
# ranking — shared by both modes' ``visualize_memory_structure``.
_ORDER_ADJECTIVE = {"recency": "most-recent", "relevance": "most-relevant"}

# The MCP Apps iframe runtime — an MCP-layer concern, hence spliced in here
# rather than by ``_resolve_static`` (which the standalone file variant shares).
_EXT_APPS_CDN = "https://unpkg.com/@modelcontextprotocol/ext-apps@0.4.0/app-with-deps"

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
    as_html_file: bool = False,
) -> ToolResult | str:
    """Deliver a **Graph payload** or **Embedding map** to whichever channel
    the client can render.

    The ONE dual-path seam behind every graph-capable MCP tool (ADR-005,
    decision 4) — both paths, chosen by client capability, never one or the
    other:

    * **Inline MCP App iframe** when the client supports the UI extension: the
      ``summary`` goes in a model-visible text block and the full node/edge
      payload rides in a second block marked ``audience=["user"]``, so the
      iframe gets the graph while the model reads only ``summary``.
    * **Graph file** otherwise (or when ``as_html_file`` is set, ADR-014 §4):
      the same payload is rendered to self-contained HTML, gzipped and stored
      in MongoDB as one row owned by ``user_id`` under an unguessable
      ``<token>.html.gz`` name that expires after
      ``mcp.graph_file_ttl_seconds``. The result carries a
      ``graphs://<token>.html.gz`` resource link (the **Graph download**) and
      the expiry, never a server path. Read the linked `graphs://…html.gz`
      resource — an `application/gzip` blob: write its base64 `blob` to a file
      and run `base64 -d < blob.b64 | gunzip > <name>.html` (Python:
      `gzip.decompress(base64.b64decode(blob))`), then open the `.html` in a
      browser. Only on a local ``stdio`` server does the branch ALSO write
      ``.tree/graphs/<slug>-<stamp>.html``, open it best effort and name the
      path — so a stdio answer carries TWO names: the token in the URI, the
      slug+stamp on disk.

    Args:
        ctx: The MCP request context (capability check and transport).
        payload: The **Graph payload** from ``to_graph_payload``, or the
            **Embedding map** payload from ``to_embedding_map_payload`` (the
            same ``{nodes, edges}`` shape plus the template's optional keys).
        summary: The model-visible text. Callers whose tool contract is
            answering a question (``query_memory`` / ``search_memory``) put
            their serialized results in here — both branches keep it verbatim,
            so the model never loses data to the visualization.
        user_id: The **Request user** — the owner of the **Graph file** row.
        query: Search query text, used to slug the stdio file name.
        as_html_file: Force the file branch even for a UI-capable client.

    Returns:
        The ``ToolResult`` for either branch, or the ``storage_unavailable``
        **Tool error envelope** (a ``str``) when the row cannot be written.
    """

    # What the payload IS, in one word, for every string below: a Graph
    # payload is a "graph", an Embedding map is an "embedding map" (ADR-007 §2
    # — in rag mode there is no graph anywhere to confuse it with).
    noun = _payload_noun(payload)

    ui_supported = ctx.client_supports_extension(UI_EXTENSION_ID)
    if ui_supported and not as_html_file:
        # A CUSTOM HTML app's iframe reads the tool result's ``content`` via
        # ``ontoolresult`` — ``structuredContent`` is FastMCP's *Prefab*-renderer
        # channel and is NOT forwarded to a custom iframe. So the graph payload
        # rides in a ``content`` JSON block; it's marked ``audience=["user"]`` so
        # the iframe gets it while the MODEL still sees only ``summary``.
        # ``structured_content`` is kept for any host that forwards it too.
        return ToolResult(
            content=[
                types.TextContent(
                    type="text",
                    text=f"{summary} (interactive {noun} view).",
                ),
                types.TextContent(
                    type="text",
                    text=json.dumps(payload),
                    annotations=types.Annotations(audience=["user"]),
                ),
            ],
            structured_content=payload,
        )

    # Fallback: client can't render MCP App UIs, or a file was requested. The
    # bytes go to Mongo, not the server's disk: the ``resources/read`` that
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

    reason = (
        "you asked for an HTML file"
        if as_html_file
        else "this client does not render inline MCP App UIs"
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
            f"{summary}. Since {reason}, I saved a self-contained interactive "
            f"{noun} to:\n{path}\n{closing} "
            f"The graphs:// link expires in about {ttl}."
        )
    else:
        text = (
            f"{summary}. Since {reason}, I rendered a self-contained interactive "
            f"{noun} as a download. {DOWNLOAD_CONTRACT} "
            f"The link expires in about {ttl}."
        )

    return ToolResult(
        content=[
            types.TextContent(type="text", text=text),
            types.ResourceLink(
                type="resource_link",
                uri=f"graphs://{name}",  # type: ignore[arg-type]
                name=name,
                mimeType=GZIP_MIME,
                description=(
                    f"gzip-compressed self-contained interactive {noun} — "
                    "base64-decode the blob, gunzip, open the .html"
                ),
            ),
        ]
    )


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


@mcp.resource(
    GRAPH_VIEW_URI,
    app=AppConfig(
        csp=ResourceCSP(
            resource_domains=["https://unpkg.com", "https://cdn.jsdelivr.net"],
        )
    ),
)
def graph_view() -> str:
    """Interactive Sigma.js viewer for knowledge graphs and embedding maps
    (read-only)."""

    return _GRAPH_HTML


# ---------------------------------------------------------------------------
# HTML templates. Plain (non-f) strings: they contain JS ``{}`` blocks that
# must reach the browser verbatim. Shared pieces are spliced in via
# ``str.replace`` on ``__TOKEN__`` placeholders (no brace-doubling).
# ---------------------------------------------------------------------------

# ui:// resource: data arrives via the ext-apps ontoolresult channel.
_GRAPH_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="color-scheme" content="light" />
__STYLE__
</head>
<body>
__BODY__
  <script type="module">
    import { App } from "__EXT_APPS_CDN__";
    import Graph from "__GRAPHOLOGY_CDN__";
    import Sigma from "__SIGMA_CDN__";
    import { forceSimulation, forceLink, forceManyBody, forceCenter, forceX, forceY } from "__D3_FORCE_CDN__";

__RENDER_JS__

    const app = new App({ name: "Tree Graph View", version: "1.0.0" });

    app.ontoolresult = (result) => {
      const r = result || {};
      // A custom HTML app receives the payload via `content` (the host does not
      // forward `structuredContent` to a custom iframe); read that first, then
      // fall back to `structuredContent` for hosts that do forward it.
      let data = null;
      for (const c of (r.content || [])) {
        if (c && c.type === "text") {
          try { const p = JSON.parse(c.text); if (p && Array.isArray(p.nodes)) { data = p; break; } }
          catch (_e) { /* not a JSON block (e.g. the human summary) */ }
        }
      }
      if (!data && r.structuredContent && Array.isArray(r.structuredContent.nodes)) {
        data = r.structuredContent;
      }
      if (data && Array.isArray(data.nodes)) render(data);
      else countsEl.textContent = "No graph data in tool result.";
    };

    await app.connect();
  </script>
</body>
</html>"""


# iframe: fixed height (host sizes to body); the standalone file variant
# (100vh) is resolved in ``visualize/graph.py``. Only the ext-apps runtime token
# is spliced here — everything else comes from the shared templates.
_GRAPH_HTML = _resolve_static(_GRAPH_HTML_TEMPLATE, "760px").replace(
    "__EXT_APPS_CDN__", _EXT_APPS_CDN
)
