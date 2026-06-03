"""MCP App: render a knowledge-graph ``QueryResult`` as an interactive graph.

Read-only: visualizes exactly what the structured query returned — no
click-to-expand, no server round-trips from the UI. The flow follows the
low-level MCP Apps pattern (https://gofastmcp.com/apps/low-level):

* ``visualize_memory_graph`` is the model-visible tool (the entry point). It
  runs the structured query and returns the graph as ``structured_content``.
* ``graph_view`` serves the ``ui://`` HTML resource — a sandboxed iframe that
  receives the tool result via the MCP Apps ``ontoolresult`` channel and draws
  the graph.

**Rendering stack.** The browser side uses graphology (graph data structure),
graphology-layout-forceatlas2 (computes node positions), and Sigma.js (WebGL
renderer) — loaded as ESM from jsdelivr. The UI is themed after the "Tree
Memory" design: a header with live counts + node search, a per-type legend, a
node-detail panel (label + type + id on click), and zoom controls. Node labels
show only the name; the type is revealed on click. Edge labels show the
relationship type.

**Fallback (Option B).** Not every client renders MCP App UIs (e.g. the
Claude Code terminal, or agentic surfaces that only consume tool text). The
tool checks ``ctx.client_supports_extension(UI_EXTENSION_ID)``; when the UI
extension is absent — or when the caller explicitly asks via ``as_html_file``
— it renders the *same* graph into a self-contained HTML file under
``.tree/graphs/`` (data embedded inline, no ext-apps round-trip) and returns
the path. That keeps the slow path off the model: it never has to hand-author
HTML from the payload.

The iframe payload travels on ``structured_content`` (the model only sees a
short text summary, not the full node/edge dump). The JS prefers
``structuredContent`` and falls back to parsing a JSON text block.
"""

import json
import logging
import re
import webbrowser
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastmcp import Context
from fastmcp.apps import UI_EXTENSION_ID, AppConfig, ResourceCSP
from fastmcp.tools import ToolResult
from mcp import types

from tree.config.paths import GRAPHS_DIR
from tree.mcp.server import mcp
from tree.memory.query.core import query_memory as structured_query_memory
from tree.memory.query.visualize import (
    _extract_display_name,
    _truncate,
)
from tree.memory.types import QueryResult

logger = logging.getLogger(__name__)

GRAPH_VIEW_URI = "ui://tree-memory/graph.html"
_FALLBACK_COLOUR = "#9aa6b2"  # unknown / unmapped node types

# Node-type palette tuned for a WHITE background. Three hue families:
# brown = entities, blue = documents/structure, orange = knowledge/events.
# Defined locally so visualize.py's dark-theme palette (used by the pyvis
# renderer) is left untouched.
_NODE_COLOURS: dict[str, str] = {
    # Blue — documents & structural nodes
    "document": "#0060b1",
    "chunk": "#c5dfef",
    "episode": "#6ba5d7",
    # Brown — entities
    "person": "#834622",
    "organization": "#591f06",
    "location": "#d1a672",
    "object": "#ecd5b8",
    # Orange — knowledge & events
    "event": "#ffb458",
    "preference": "#cc4e01",
    "fact": "#fee3ac",
    "task": "#e37b45",
}

# Browser deps, pinned. graphology-layout-forceatlas2 ships CJS-only and Sigma
# v3 declares no UMD global, so all three load as ESM via jsdelivr's +esm
# endpoint (which also dedupes graphology across them).
_GRAPHOLOGY_CDN = "https://cdn.jsdelivr.net/npm/graphology@0.26.0/+esm"
_SIGMA_CDN = "https://cdn.jsdelivr.net/npm/sigma@3.0.3/+esm"
_FA2_CDN = "https://cdn.jsdelivr.net/npm/graphology-layout-forceatlas2@0.10.1/+esm"
_EXT_APPS_CDN = "https://unpkg.com/@modelcontextprotocol/ext-apps@0.4.0/app-with-deps"


def to_graph_payload(result: QueryResult) -> dict[str, list[dict[str, Any]]]:
    """Flatten a ``QueryResult`` into a graph ``{nodes, edges}`` payload.

    Every edge endpoint is guaranteed to also exist as a node — partial graphs
    may reference nodes that were not in the seed set, and the renderer drops
    edges with a dangling endpoint. Endpoints discovered only via edges are
    added with type ``"unknown"``; the leading-prefix display-name logic is
    reused from ``visualize`` so labels match the pyvis renderer.
    """

    nodes: list[dict[str, Any]] = []
    seen: set[str] = set()

    def _add_node(node_id: str, node_type: str, props: dict[str, Any]) -> None:
        if not node_id or node_id in seen:
            return
        seen.add(node_id)
        name = _extract_display_name(node_id, node_type, props)
        nodes.append(
            {
                "id": node_id,
                "type": node_type,
                "name": name,  # full, untruncated — shown on hover / click
                "label": _truncate(name, 40),  # shown on the canvas
                "color": _NODE_COLOURS.get(node_type, _FALLBACK_COLOUR),
            }
        )

    for node in result.nodes:
        _add_node(
            str(node["_id"]),
            node.get("type", "unknown"),
            node.get("properties") or {},
        )

    edges: list[dict[str, Any]] = []
    for edge in result.edges:
        src = str(edge.get("source_node_id", ""))
        tgt = str(edge.get("target_node_id", ""))
        if not src or not tgt:
            continue
        _add_node(src, "unknown", {})
        _add_node(tgt, "unknown", {})
        edges.append({"source": src, "target": tgt, "type": edge.get("type", "")})

    return {"nodes": nodes, "edges": edges}


def _slugify(text: str, max_len: int = 48) -> str:
    """Turn a query into a filesystem-safe filename stem."""

    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:max_len].strip("-") or "graph"


def _default_graph_path(query: str) -> Path:
    """Build a unique, discoverable output path under ``.tree/graphs/``.

    Filename is ``<query-slug>-<UTC-timestamp>.html`` so repeated renders never
    clobber each other and the file is easy to find / share.
    """

    stamp = datetime.now(tz=UTC).strftime("%Y%m%d-%H%M%S")
    return GRAPHS_DIR / f"{_slugify(query)}-{stamp}.html"


def _render_graph_file(
    payload: dict[str, list[dict[str, Any]]],
    query: str = "",
    output: Path | None = None,
) -> Path:
    """Write a self-contained HTML file (data embedded inline) and return it.

    Used as the fallback when the client does not render MCP App UIs, or when
    the caller explicitly asks for an openable file. The HTML carries its data
    directly (``const DATA = …``) rather than waiting for the ext-apps
    ``ontoolresult`` channel, so it works as a plain ``file://`` page.

    Defaults to a uniquely-named file under ``.tree/graphs/`` (created on
    demand); pass ``output`` to write somewhere specific.
    """

    # Guard against ``</script>`` (or ``</`` generally) appearing inside a
    # label and prematurely closing the inline <script> block.
    data_json = json.dumps(payload).replace("</", "<\\/")
    html = _FILE_HTML_BASE.replace("__DATA__", data_json)

    path = output or _default_graph_path(query)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")
    logger.info(
        "Wrote self-contained graph HTML (%d nodes, %d edges) to %s",
        len(payload["nodes"]),
        len(payload["edges"]),
        path,
    )
    return path


@mcp.tool(app=AppConfig(resource_uri=GRAPH_VIEW_URI))
async def visualize_memory_graph(
    query: str,
    ctx: Context,
    top_k: int = 15,
    max_hops: int = 2,
    as_html_file: bool = False,
) -> ToolResult | str:
    """Visualize the knowledge graph for a query as an interactive graph.

    Runs semantic + text search with graph expansion (same engine as
    ``search_memory``) and renders the resulting nodes/edges in a read-only,
    interactive Sigma.js force-directed view. Use this when the user wants to
    *see* the graph rather than read node/edge JSON.

    When the client renders MCP App UIs, the graph appears inline. Otherwise
    (or when ``as_html_file`` is set) the same graph is written to a
    self-contained HTML file and the path is returned — do NOT re-author the
    HTML yourself; just share the path.

    Args:
        query: Search query text — seeds the subgraph to visualize.
        top_k: Number of seed nodes to retrieve (default 15).
        max_hops: Hops of graph expansion around the seeds (default 2).
        as_html_file: Set true when the user explicitly asks for a downloadable
            / openable HTML file instead of the inline interactive view.
    """

    lc = ctx.lifespan_context
    result = await structured_query_memory(
        client=lc["client"],
        database=lc["database"],
        query=query,
        embedding_model=lc["embedding_model"],
        user_id=lc["user_id"],
        top_k=top_k,
        max_hops=max_hops,
    )

    payload = to_graph_payload(result)
    n_nodes, n_edges = len(payload["nodes"]), len(payload["edges"])
    summary = f"Knowledge graph for {query!r}: {n_nodes} nodes, {n_edges} edges"

    ui_supported = ctx.client_supports_extension(UI_EXTENSION_ID)
    if ui_supported and not as_html_file:
        return ToolResult(
            content=[
                types.TextContent(
                    type="text",
                    text=f"{summary} (rendered in the interactive graph view).",
                )
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
        else "Open it in a browser to explore (drag nodes, zoom/pan)."
    )
    return (
        f"{summary}. Since {reason}, I saved a self-contained interactive "
        f"graph to:\n{path}\n{closing}"
    )


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

_GRAPH_STYLE = """\
  <style>
    :root {
      --bg: #ffffff; --panel: #f5f6f8; --border: #d9dde4; --muted: #6b7280;
      --text: #1f2430; --accent1: #ea580c; --accent2: #f59e0b;
    }
    /* Height is per-variant (see __APP_HEIGHT__): the MCP App iframe gets a
       fixed 760px (hosts size the iframe to the body's height; 100vh would
       collapse to a tiny default), while the standalone HTML file gets 100vh
       so it fills the browser window. */
    html, body { margin: 0; height: __APP_HEIGHT__; background: var(--bg); color: var(--text);
      font-family: ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
      overflow: hidden; }
    #wrap { position: relative; width: 100%; height: 100%; display: flex; flex-direction: column; }

    /* Header */
    #header { display: flex; align-items: center; gap: 14px; padding: 9px 14px;
      background: var(--panel); border-bottom: 1px solid var(--border); z-index: 3; }
    #brand { font-weight: 700; font-size: 15px; letter-spacing: 0.2px;
      background: linear-gradient(90deg, var(--accent1), var(--accent2));
      -webkit-background-clip: text; background-clip: text; -webkit-text-fill-color: transparent; }
    #counts { font-size: 12px; color: var(--muted); }
    #search { margin-left: auto; width: 240px; max-width: 45%; padding: 6px 10px;
      background: #ffffff; border: 1px solid var(--border); border-radius: 8px;
      color: var(--text); font-size: 12px; outline: none; }
    #search:focus { border-color: var(--accent1); }
    #search::placeholder { color: var(--muted); }

    /* Graph stage */
    #stage { position: relative; flex: 1; min-height: 0; }
    #sigma-container { width: 100%; height: 100%; }

    /* Legend */
    #legend { position: absolute; top: 10px; right: 12px; font-size: 11px; z-index: 2;
      background: rgba(255,255,255,0.92); border: 1px solid var(--border);
      padding: 8px 10px; border-radius: 10px; max-height: 60%; overflow: auto; }
    #legend .title { color: var(--muted); text-transform: uppercase; letter-spacing: 0.6px;
      font-size: 9px; margin-bottom: 5px; }
    #legend div.row { display: flex; align-items: center; gap: 7px; margin: 3px 0; }
    #legend span.dot { width: 10px; height: 10px; border-radius: 50%; display: inline-block; }

    /* Node detail panel */
    /* Positioned next to the clicked node in JS (left/top set at click time). */
    #detail { position: absolute; left: 0; top: 0; z-index: 4; display: none;
      min-width: 200px; max-width: 300px; background: rgba(255,255,255,0.97);
      border: 1px solid var(--border); border-radius: 12px; padding: 11px 13px;
      box-shadow: 0 6px 24px rgba(0,0,0,0.15); pointer-events: auto; }
    #detail.show { display: block; }
    #detail-close { position: absolute; top: 8px; right: 10px; cursor: pointer;
      color: var(--muted); font-size: 14px; line-height: 1; border: none; background: none; }
    #detail-label { font-size: 14px; font-weight: 600; margin: 0 18px 8px 0; word-break: break-word; }
    #detail-type { display: inline-block; font-size: 11px; font-weight: 600; color: #0b0d14;
      padding: 2px 9px; border-radius: 999px; }
    #detail-id { margin-top: 9px; font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
      font-size: 10px; color: var(--muted); word-break: break-all; }

    /* Hover tooltip: full, untruncated name. */
    #tooltip { position: absolute; z-index: 5; display: none; pointer-events: none;
      max-width: 280px; background: rgba(255,255,255,0.97); border: 1px solid var(--border);
      color: var(--text); font-size: 12px; padding: 6px 9px; border-radius: 8px;
      box-shadow: 0 4px 16px rgba(0,0,0,0.18); word-break: break-word; }
    #tooltip.show { display: block; }

    /* Zoom controls */
    #zoom { position: absolute; right: 12px; bottom: 12px; z-index: 3;
      display: flex; flex-direction: column; gap: 6px; }
    #zoom button { width: 32px; height: 32px; border-radius: 8px; cursor: pointer;
      background: var(--panel); color: var(--text); border: 1px solid var(--border);
      font-size: 15px; line-height: 1; display: flex; align-items: center; justify-content: center; }
    #zoom button:hover { border-color: var(--accent1); color: var(--accent2); }
  </style>"""

# Shared DOM markup for both the iframe and the file variant.
_BODY_MARKUP = """\
  <div id="wrap">
    <div id="header">
      <span id="brand">Tree Memory</span>
      <span id="counts">loading…</span>
      <input id="search" type="search" placeholder="Search nodes…" autocomplete="off" />
    </div>
    <div id="stage">
      <div id="sigma-container"></div>
      <div id="legend"></div>
      <div id="detail">
        <button id="detail-close" title="Close">✕</button>
        <div id="detail-label"></div>
        <span id="detail-type"></span>
        <div id="detail-id"></div>
      </div>
      <div id="tooltip"></div>
      <div id="zoom">
        <button id="zoom-in" title="Zoom in">+</button>
        <button id="zoom-out" title="Zoom out">−</button>
        <button id="zoom-fit" title="Fit to view">⊡</button>
      </div>
    </div>
  </div>"""

# graphology + Sigma + ForceAtlas2 render. Defines render(payload). Expects
# Graph / Sigma / forceAtlas2 imported above, and the _BODY_MARKUP DOM present.
_RENDER_JS = """\
    const countsEl = document.getElementById("counts");

    function render({ nodes, edges }) {
      const container = document.getElementById("sigma-container");
      container.innerHTML = "";

      if (!nodes.length) { countsEl.textContent = "No graph data returned."; return; }
      countsEl.textContent = nodes.length + " nodes · " + edges.length + " edges";

      const nodeById = new Map(nodes.map((n) => [n.id, n]));

      // Build the graphology graph. Multi + directed so parallel edges and
      // self-loops don't throw, and edges can carry arrowheads.
      const graph = new Graph({ type: "directed", multi: true });
      for (const n of nodes) {
        graph.addNode(n.id, {
          label: n.label,            // labels in-view show ONLY the name
          nodeType: n.type,          // (Sigma reserves "type" for the program)
          color: n.color,
          size: 6,
          x: Math.random(),          // ForceAtlas2 needs distinct starts
          y: Math.random(),
        });
      }
      for (const e of edges) {
        if (graph.hasNode(e.source) && graph.hasNode(e.target)) {
          graph.addEdge(e.source, e.target, { label: e.type, type: "arrow", size: 1.2, color: "#c2c8d2" });
        }
      }

      // Layout.
      const settings = forceAtlas2.inferSettings(graph);
      forceAtlas2.assign(graph, { iterations: 300, settings });

      // Interaction state, applied via reducers.
      const state = { search: "", selected: null, hovered: null };

      const renderer = new Sigma(graph, container, {
        renderEdgeLabels: true,        // relationship labels
        defaultEdgeType: "arrow",
        labelColor: { color: "#1f2430" },
        edgeLabelColor: { color: "#6b7280" },
        labelSize: 11,
        edgeLabelSize: 9,
        labelRenderedSizeThreshold: 1,
        labelDensity: 0.7,
        nodeReducer: (node, data) => {
          const res = Object.assign({}, data);
          const searching = state.search && !(data.label || "").toLowerCase().includes(state.search);
          // Chunks are the most numerous nodes; hide their labels unless the
          // node is hovered or selected (full name still shows in the tooltip).
          const quietChunk = !state.search && data.nodeType === "chunk"
            && state.hovered !== node && state.selected !== node;
          if (searching) { res.color = "#d5d8de"; res.label = ""; }
          else if (quietChunk) { res.label = ""; }
          if (state.selected === node) { res.highlighted = true; res.zIndex = 2; }
          return res;
        },
        edgeReducer: (edge, data) => {
          const res = Object.assign({}, data);
          if (state.search) res.color = "#e6e8ec";   // dim edges while searching
          return res;
        },
      });

      // --- Node detail panel (label + type + id on click) ---
      const detail = document.getElementById("detail");
      // Black or white pill text depending on the node colour's luminance.
      function pillText(hex) {
        const c = hex.replace("#", "");
        const r = parseInt(c.slice(0, 2), 16), g = parseInt(c.slice(2, 4), 16), b = parseInt(c.slice(4, 6), 16);
        return (0.299 * r + 0.587 * g + 0.114 * b) > 140 ? "#1f2430" : "#ffffff";
      }
      function showDetail(n) {
        document.getElementById("detail-label").textContent = n.name;  // full name
        const typeEl = document.getElementById("detail-type");
        typeEl.textContent = n.type;
        typeEl.style.background = n.color;
        typeEl.style.color = pillText(n.color);
        // Show the qualified name (type:label), not the user-id-prefixed _id.
        document.getElementById("detail-id").textContent = n.type + ":" + n.name;
        detail.classList.add("show");
      }
      function hideDetail() { detail.classList.remove("show"); }

      // Pin the detail card beside the selected node; clamp inside the stage.
      // Re-run on every frame so it tracks pan / zoom / drag.
      function positionDetail() {
        if (!state.selected || !detail.classList.contains("show")) return;
        const a = graph.getNodeAttributes(state.selected);
        const vp = renderer.graphToViewport({ x: a.x, y: a.y });
        const rect = container.getBoundingClientRect();
        const pw = detail.offsetWidth, ph = detail.offsetHeight, gap = 16;
        let left = vp.x + gap;
        if (left + pw > rect.width - 8) left = vp.x - pw - gap;   // flip to the left
        left = Math.max(8, Math.min(left, rect.width - pw - 8));
        let top = Math.max(8, Math.min(vp.y - ph / 2, rect.height - ph - 8));
        detail.style.left = left + "px";
        detail.style.top = top + "px";
      }

      renderer.on("clickNode", ({ node }) => {
        state.selected = node;
        const n = nodeById.get(node);
        if (n) { showDetail(n); positionDetail(); }
        renderer.refresh();
      });
      renderer.on("clickStage", () => { state.selected = null; hideDetail(); renderer.refresh(); });
      renderer.on("afterRender", positionDetail);
      document.getElementById("detail-close").onclick = () => {
        state.selected = null; hideDetail(); renderer.refresh();
      };

      // --- Hover tooltip: full, untruncated name (canvas labels are clipped) ---
      const tooltip = document.getElementById("tooltip");
      function positionTooltip() {
        if (!state.hovered) return;
        const a = graph.getNodeAttributes(state.hovered);
        const vp = renderer.graphToViewport({ x: a.x, y: a.y });
        const rect = container.getBoundingClientRect();
        const tw = tooltip.offsetWidth, th = tooltip.offsetHeight, gap = 12;
        const left = Math.max(8, Math.min(vp.x + gap, rect.width - tw - 8));
        const top = Math.max(8, vp.y - th - gap);   // above the node
        tooltip.style.left = left + "px";
        tooltip.style.top = top + "px";
      }
      renderer.on("enterNode", ({ node }) => {
        const n = nodeById.get(node);
        if (!n) return;
        tooltip.textContent = n.name;
        tooltip.classList.add("show");
        state.hovered = node;
        positionTooltip();
        renderer.refresh();              // reveal a hidden chunk label on hover
        container.style.cursor = "pointer";
      });
      renderer.on("leaveNode", () => {
        state.hovered = null;
        tooltip.classList.remove("show");
        renderer.refresh();
        container.style.cursor = "default";
      });
      renderer.on("afterRender", positionTooltip);

      // --- Search (client-side highlight/dim) ---
      const searchEl = document.getElementById("search");
      searchEl.oninput = () => { state.search = searchEl.value.trim().toLowerCase(); renderer.refresh(); };

      // --- Zoom controls ---
      const camera = renderer.getCamera();
      document.getElementById("zoom-in").onclick = () => camera.animatedZoom();
      document.getElementById("zoom-out").onclick = () => camera.animatedUnzoom();
      document.getElementById("zoom-fit").onclick = () => camera.animatedReset();

      // Redraw when the window resizes (Sigma tracks the container; this keeps
      // the standalone-file view crisp as the window grows/shrinks).
      window.addEventListener("resize", () => renderer.refresh());

      // --- Node dragging (Sigma camera-based pattern) ---
      let dragged = null;
      renderer.on("downNode", (e) => { dragged = e.node; });
      renderer.getMouseCaptor().on("mousemovebody", (e) => {
        if (!dragged) return;
        const pos = renderer.viewportToGraph(e);
        graph.setNodeAttribute(dragged, "x", pos.x);
        graph.setNodeAttribute(dragged, "y", pos.y);
        e.preventSigmaDefault();
        e.original.preventDefault();
        e.original.stopPropagation();
      });
      renderer.getMouseCaptor().on("mouseup", () => { dragged = null; });

      // --- Legend (one swatch per node type present) ---
      const colorByType = new Map(nodes.map((n) => [n.type, n.color]));
      const rows = [...colorByType.entries()].sort((a, b) => a[0].localeCompare(b[0]))
        .map(([t, c]) => '<div class="row"><span class="dot" style="background:' + c + '"></span>' + t + "</div>")
        .join("");
      document.getElementById("legend").innerHTML = '<div class="title">Legend</div>' + rows;
    }"""

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
      let data = result && result.structuredContent;
      if (!data) {
        const txt = result && result.content && result.content.find((c) => c.type === "text");
        if (txt) { try { data = JSON.parse(txt.text); } catch (_e) { /* not a JSON block */ } }
      }
      if (data && Array.isArray(data.nodes)) render(data);
      else countsEl.textContent = "No graph data in tool result.";
    };

    await app.connect();
  </script>
</body>
</html>"""

# Self-contained file: data embedded inline, no ext-apps round-trip.
_FILE_HTML_TEMPLATE = """\
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
    import Graph from "__GRAPHOLOGY_CDN__";
    import Sigma from "__SIGMA_CDN__";
    import forceAtlas2 from "__FA2_CDN__";

__RENDER_JS__

    const DATA = __DATA__;
    render(DATA);
  </script>
</body>
</html>"""


def _resolve_static(template: str, app_height: str) -> str:
    """Splice shared markup/JS + pinned CDN URLs into a template (not data).

    ``app_height`` is the CSS height for ``html, body`` — ``"760px"`` for the
    iframe variant, ``"100vh"`` for the standalone file (fills the window).
    """

    return (
        template.replace("__STYLE__", _GRAPH_STYLE)
        .replace("__APP_HEIGHT__", app_height)
        .replace("__BODY__", _BODY_MARKUP)
        .replace("__RENDER_JS__", _RENDER_JS)
        .replace("__EXT_APPS_CDN__", _EXT_APPS_CDN)
        .replace("__GRAPHOLOGY_CDN__", _GRAPHOLOGY_CDN)
        .replace("__SIGMA_CDN__", _SIGMA_CDN)
        .replace("__FA2_CDN__", _FA2_CDN)
    )


# iframe: fixed height (host sizes to body). File: 100vh (fills the window).
_GRAPH_HTML = _resolve_static(_GRAPH_HTML_TEMPLATE, "760px")
# Everything but the per-call ``__DATA__`` substitution is resolved once here.
_FILE_HTML_BASE = _resolve_static(_FILE_HTML_TEMPLATE, "100vh")
