"""MCP App: render a knowledge-graph ``QueryResult`` as an interactive graph.

Read-only: visualizes exactly what the structured query returned — no
click-to-expand, no server round-trips from the UI. The flow follows the
low-level MCP Apps pattern (https://gofastmcp.com/apps/low-level):

* ``visualize_memory_graph`` is the model-visible tool (the entry point). It
  runs the structured query and returns the graph as ``structured_content``.
* ``graph_view`` serves the ``ui://`` HTML resource — a sandboxed iframe that
  receives the tool result via the MCP Apps ``ontoolresult`` channel and draws
  the graph.

**Rendering stack.** This module owns MCP concerns ONLY. The **Graph renderer**
itself — the **Graph payload** builder, the shared CSS/DOM/JS templates, and
the self-contained-file writer — lives in ``tree.memory.query.visualize`` and
is imported from here (dependency direction memory ← mcp). What stays local:
the ext-apps iframe runtime CDN, the ``ui://`` HTML variant, CSP wiring, the
tool, and the ``graphs://`` resource.

**Fallback (Option B).** Not every client renders MCP App UIs (e.g. the
Claude Code terminal, or agentic surfaces that only consume tool text).
:func:`_graph_tool_result` checks ``ctx.client_supports_extension(UI_EXTENSION_ID)``;
when the UI extension is absent — or when the caller explicitly asks via
``as_html_file`` — it renders the *same* graph into a self-contained HTML file
under ``.tree/graphs/`` (data embedded inline, no ext-apps round-trip) and
returns the path PLUS a ``graphs://<name>`` resource link. The path serves the
local (stdio) deployment; the resource link serves remote ones (Prefect
Horizon), where the client can't reach the server's filesystem and instead
downloads the HTML over the MCP connection (read the resource, save the text).
Either way the slow path stays off the model: it never hand-authors HTML.

**One dual-path helper for every graph tool (ADR-005, decision 4).**
:func:`_graph_tool_result` owns the capability check and BOTH branches, and is
the only place either exists. All three graph-capable MCP tools call it —
``visualize_memory_graph`` here, plus ``query_memory(visualize=True)`` and
``search_memory(visualize=True)`` in ``tree.mcp.tools`` (via that module's
``_dual_graph_result`` seam) — so from a visualization standpoint they behave
identically. A new graph tool builds a **Graph payload** and calls this helper;
it never reimplements a branch.

The iframe payload travels in a ``content`` JSON block (a custom HTML app reads
the tool result's ``content`` via ``ontoolresult`` — ``structuredContent`` is
FastMCP's *Prefab*-renderer channel and is NOT forwarded to a custom iframe).
That block is marked ``audience=["user"]`` so the iframe gets the full node/edge
dump while the MODEL sees only the short text summary. The JS reads ``content``
first, then falls back to ``structuredContent`` for hosts that forward it.
"""

import json
import logging
import webbrowser
from typing import Any

from fastmcp import Context
from fastmcp.apps import UI_EXTENSION_ID, AppConfig, ResourceCSP
from fastmcp.tools import ToolResult
from mcp import types

from tree.config.paths import GRAPHS_DIR
from tree.mcp.server import mcp
from tree.memory.graph.retrieval import fetch_full_graph
from tree.memory.graph.retrieval import query_memory as structured_query_memory
from tree.memory.query.visualize import (
    _render_graph_file,
    _resolve_static,
    to_graph_payload,
)

logger = logging.getLogger(__name__)

GRAPH_VIEW_URI = "ui://tree-memory/graph.html"

# The MCP Apps iframe runtime — an MCP-layer concern, hence spliced in here
# rather than by ``_resolve_static`` (which the standalone file variant shares).
_EXT_APPS_CDN = "https://unpkg.com/@modelcontextprotocol/ext-apps@0.4.0/app-with-deps"


def _graph_tool_result(
    ctx: Context,
    payload: dict[str, list[dict[str, Any]]],
    summary: str,
    *,
    query: str = "",
    as_html_file: bool = False,
) -> ToolResult:
    """Deliver a **Graph payload** to whichever channel the client can render.

    The ONE dual-path seam behind every graph-capable MCP tool (ADR-005,
    decision 4) — both paths, chosen by client capability, never one or the
    other:

    * **Inline MCP App iframe** when the client supports the UI extension: the
      ``summary`` goes in a model-visible text block and the full node/edge
      payload rides in a second block marked ``audience=["user"]``, so the
      iframe gets the graph while the model reads only ``summary``.
    * **Self-contained HTML file** otherwise (or when ``as_html_file`` is set):
      the same payload is rendered under ``.tree/graphs/``, the browser is
      opened BEST EFFORT, and the result carries the server-side path plus a
      ``graphs://<name>`` resource link for clients of a remote server.

    Args:
        ctx: The MCP request context (used for the capability check).
        payload: The **Graph payload** from ``to_graph_payload``.
        summary: The model-visible text. Callers whose tool contract is
            answering a question (``query_memory`` / ``search_memory``) put
            their serialized results in here — both branches keep it verbatim,
            so the model never loses data to the visualization.
        query: Search query text, used to slug the fallback file name.
        as_html_file: Force the file branch even for a UI-capable client.
    """

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
                    text=f"{summary} (interactive graph view).",
                ),
                types.TextContent(
                    type="text",
                    text=json.dumps(payload),
                    annotations=types.Annotations(audience=["user"]),
                ),
            ],
            structured_content=payload,
        )

    # Fallback: client can't render MCP App UIs, or a file was requested.
    path = _render_graph_file(payload, query=query)
    reason = (
        "you asked for an HTML file"
        if as_html_file
        else "this client does not render inline MCP App UIs"
    )

    # Best-effort: pop it open on the local machine. No-op / harmless on a
    # headless or remote host (returns False or raises, which we swallow).
    opened = False
    try:
        opened = webbrowser.open(path.resolve().as_uri())
    except Exception:  # noqa: BLE001 — opening a browser must never fail the tool.
        opened = False

    closing = (
        "Opened it in your browser."
        if opened
        else (
            "Open it in a browser to explore (drag nodes, zoom/pan). If that "
            "path is NOT on your machine (the MCP server runs remotely, e.g. "
            "on Prefect Horizon), read the linked MCP resource instead and "
            "save its text locally as an .html file."
        )
    )
    # The file lives on the SERVER's filesystem. For remote deployments the
    # path alone is unreachable, so the same HTML is also exposed as an MCP
    # resource (``graphs://<name>``, see :func:`graph_file`) the client can
    # fetch over the existing connection.
    return ToolResult(
        content=[
            types.TextContent(
                type="text",
                text=(
                    f"{summary}. Since {reason}, I saved a self-contained "
                    f"interactive graph to:\n{path}\n{closing}"
                ),
            ),
            types.ResourceLink(
                type="resource_link",
                uri=f"graphs://{path.name}",  # type: ignore[arg-type]
                name=path.name,
                mimeType="text/html",
                description="Self-contained interactive graph (download me)",
            ),
        ]
    )


@mcp.tool(app=AppConfig(resource_uri=GRAPH_VIEW_URI))
async def visualize_memory_graph(
    ctx: Context,
    query: str = "",
    top_k: int = 15,
    max_hops: int = 2,
    as_html_file: bool = False,
) -> ToolResult:
    """Visualize the knowledge graph as an interactive graph.

    With a ``query``, runs semantic + text search with graph expansion (same
    engine as ``search_memory``) and visualizes that subgraph. With NO query
    (the default), visualizes the user's ENTIRE memory graph. Renders read-only
    in an interactive Sigma.js force-directed view — use this when the user wants
    to *see* the graph rather than read node/edge JSON.

    When the client renders MCP App UIs, the graph appears inline. Otherwise
    (or when ``as_html_file`` is set) the same graph is written to a
    self-contained HTML file; the result carries the server-side path AND a
    ``graphs://`` resource link — do NOT re-author the HTML yourself. If the
    path exists locally just share it; if the server is remote (cloud), read
    the linked resource and save its text as a local ``.html`` file.

    Args:
        query: Search query text — seeds the subgraph to visualize. Omit (empty)
            to visualize the whole memory graph.
        top_k: Number of seed nodes to retrieve (default 15). Ignored with no query.
        max_hops: Hops of graph expansion around the seeds (default 2). Ignored
            with no query.
        as_html_file: Set true when the user explicitly asks for a downloadable
            / openable HTML file instead of the inline interactive view.
    """

    lc = ctx.lifespan_context
    if query:
        result = await structured_query_memory(
            client=lc["client"],
            database=lc["database"],
            query=query,
            embedding_model=lc["embedding_model"],
            user_id=lc["user_id"],
            top_k=top_k,
            max_hops=max_hops,
        )
        label = repr(query)
    else:
        result = await fetch_full_graph(
            client=lc["client"],
            database=lc["database"],
            user_id=lc["user_id"],
        )
        label = "your full memory"

    payload = to_graph_payload(result)
    n_nodes, n_edges = len(payload["nodes"]), len(payload["edges"])
    summary = f"Knowledge graph for {label}: {n_nodes} nodes, {n_edges} edges"

    return _graph_tool_result(
        ctx, payload, summary, query=query, as_html_file=as_html_file
    )


@mcp.resource("graphs://{name}", mime_type="text/html")
def graph_file(name: str) -> str:
    """Self-contained HTML of a previously rendered graph visualization.

    Lets clients of a REMOTE server (e.g. Prefect Horizon) download the file
    ``visualize_memory_graph`` wrote to the server-side ``.tree/graphs/`` dir:
    read this resource and save its text locally as an ``.html`` file.
    """

    base = GRAPHS_DIR.resolve()
    path = (base / name).resolve()
    # Guard traversal: the rendered files are flat ``<slug>-<stamp>.html``
    # names directly under GRAPHS_DIR.
    if path.parent != base or path.suffix != ".html":
        raise ValueError(f"Invalid graph file name: {name!r}")
    if not path.is_file():
        raise FileNotFoundError(
            f"No rendered graph named {name!r} — run visualize_memory_graph first."
        )
    return path.read_text(encoding="utf-8")


@mcp.resource(
    GRAPH_VIEW_URI,
    app=AppConfig(
        csp=ResourceCSP(
            resource_domains=["https://unpkg.com", "https://cdn.jsdelivr.net"],
        )
    ),
)
def graph_view() -> str:
    """Interactive Sigma.js knowledge-graph viewer (read-only)."""

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
    import forceAtlas2 from "__FA2_CDN__";

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
# (100vh) is resolved in ``visualize.py``. Only the ext-apps runtime token
# is spliced here — everything else comes from the shared templates.
_GRAPH_HTML = _resolve_static(_GRAPH_HTML_TEMPLATE, "760px").replace(
    "__EXT_APPS_CDN__", _EXT_APPS_CDN
)
