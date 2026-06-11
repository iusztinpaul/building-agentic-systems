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
Memory" design: a header with live counts + node search, a per-type legend, and
zoom controls. Node labels show only the name. Hovering a node or an edge shows
a tooltip with its name / relationship type plus a curated metadata card (node
type + subtype + the fields from ``_curated_meta``); clicking a node only
highlights it. Edge labels show the relationship type.

**Fallback (Option B).** Not every client renders MCP App UIs (e.g. the
Claude Code terminal, or agentic surfaces that only consume tool text). The
tool checks ``ctx.client_supports_extension(UI_EXTENSION_ID)``; when the UI
extension is absent — or when the caller explicitly asks via ``as_html_file``
— it renders the *same* graph into a self-contained HTML file under
``.tree/graphs/`` (data embedded inline, no ext-apps round-trip) and returns
the path PLUS a ``graphs://<name>`` resource link. The path serves the local
(stdio) deployment; the resource link serves remote ones (Prefect Horizon),
where the client can't reach the server's filesystem and instead downloads
the HTML over the MCP connection (read the resource, save the text). Either
way the slow path stays off the model: it never hand-authors HTML.

The iframe payload travels in a ``content`` JSON block (a custom HTML app reads
the tool result's ``content`` via ``ontoolresult`` — ``structuredContent`` is
FastMCP's *Prefab*-renderer channel and is NOT forwarded to a custom iframe).
That block is marked ``audience=["user"]`` so the iframe gets the full node/edge
dump while the MODEL sees only the short text summary. The JS reads ``content``
first, then falls back to ``structuredContent`` for hosts that forward it.
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
from tree.entities.colours import Colours
from tree.mcp.server import mcp
from tree.memory.query.core import fetch_full_graph
from tree.memory.query.core import query_memory as structured_query_memory
from tree.memory.query.visualize import (
    _extract_display_name,
    _truncate,
)
from tree.memory.types import QueryResult

logger = logging.getLogger(__name__)

GRAPH_VIEW_URI = "ui://tree-memory/graph.html"
_FALLBACK_COLOUR = Colours.BLACK_LEVEL_1  # unknown / unmapped node types

# Node-type palette tuned for a WHITE background, drawn from the brand palette
# (``Colours``). Three hue families: brown = entities, blue = documents/
# structure, orange = knowledge/events. Defined locally so visualize.py's
# dark-theme palette (used by the pyvis renderer) is left untouched.
_NODE_COLOURS: dict[str, str] = {
    # Structural
    "document": Colours.GREEN_LEVEL_3,
    "chunk": Colours.GREEN_LEVEL_2,
    # POLE+O
    "person": Colours.ORANGE_LEVEL_4,
    "object": Colours.ORANGE_LEVEL_2,
    "location": Colours.BROWN_LEVEL_4,
    "event": Colours.BROWN_LEVEL_2,
    "organization": Colours.BROWN_LEVEL_1,
    # Others
    "preference": Colours.BLUE_LEVEL_2,
    "fact": Colours.BLUE_LEVEL_4,
}

# Browser deps, pinned. graphology-layout-forceatlas2 ships CJS-only and Sigma
# v3 declares no UMD global, so all three load as ESM via jsdelivr's +esm
# endpoint (which also dedupes graphology across them).
_GRAPHOLOGY_CDN = "https://cdn.jsdelivr.net/npm/graphology@0.26.0/+esm"
_SIGMA_CDN = "https://cdn.jsdelivr.net/npm/sigma@3.0.3/+esm"
_FA2_CDN = "https://cdn.jsdelivr.net/npm/graphology-layout-forceatlas2@0.10.1/+esm"
_EXT_APPS_CDN = "https://unpkg.com/@modelcontextprotocol/ext-apps@0.4.0/app-with-deps"


# Curated metadata surfaced on node / edge hover + the dashboard table. Kept
# small on purpose: the full ``properties`` dump is left out so tooltips and
# table cells stay legible. Mirrors the hover lines the pyvis renderer builds.
_NODE_META_FIELDS = ("subtype", "description", "confidence", "aliases", "created_at")
_EDGE_META_FIELDS = (
    "semantic_type",
    "confidence",
    "description",
    "valid_from",
    "valid_until",
)


def _curated_meta(row: dict[str, Any], fields: tuple[str, ...]) -> dict[str, Any]:
    """Pull a curated, JSON-safe metadata subset off a node / edge row.

    Empty / null / empty-list values are dropped so the hover card and table
    cells stay sparse. Datetimes are ISO-8601 strings (the payload is
    ``json.dumps``-ed for the HTML file variant), floats are rounded for
    display, and lists are comma-joined.
    """

    meta: dict[str, Any] = {}
    for key in fields:
        value = row.get(key)
        if value is None or value == "" or value == []:
            continue
        if isinstance(value, datetime):
            meta[key] = value.isoformat()
        elif isinstance(value, float):
            meta[key] = round(value, 3)
        elif isinstance(value, list):
            meta[key] = ", ".join(str(v) for v in value)
        else:
            meta[key] = value
    return meta


def to_graph_payload(result: QueryResult) -> dict[str, list[dict[str, Any]]]:
    """Flatten a ``QueryResult`` into a graph ``{nodes, edges}`` payload.

    Every edge endpoint is guaranteed to also exist as a node — partial graphs
    may reference nodes that were not in the seed set, and the renderer drops
    edges with a dangling endpoint. Endpoints discovered only via edges are
    added with type ``"unknown"``; the leading-prefix display-name logic is
    reused from ``visualize`` so labels match the pyvis renderer.

    Each node and edge carries a curated ``meta`` dict (see ``_curated_meta``)
    surfaced on hover and in the dashboard table; nodes materialised only from a
    dangling edge endpoint get an empty ``meta``.
    """

    nodes: list[dict[str, Any]] = []
    seen: set[str] = set()

    def _add_node(
        node_id: str,
        node_type: str,
        props: dict[str, Any],
        meta: dict[str, Any] | None = None,
    ) -> None:
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
                "meta": meta or {},
            }
        )

    for node in result.nodes:
        _add_node(
            str(node["_id"]),
            node.get("type", "unknown"),
            node.get("properties") or {},
            _curated_meta(node, _NODE_META_FIELDS),
        )

    edges: list[dict[str, Any]] = []
    for edge in result.edges:
        src = str(edge.get("source_node_id", ""))
        tgt = str(edge.get("target_node_id", ""))
        if not src or not tgt:
            continue
        _add_node(src, "unknown", {})
        _add_node(tgt, "unknown", {})
        edges.append(
            {
                "source": src,
                "target": tgt,
                "type": edge.get("type", ""),
                "meta": _curated_meta(edge, _EDGE_META_FIELDS),
            }
        )

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

    ui_supported = ctx.client_supports_extension(UI_EXTENSION_ID)
    if ui_supported and not as_html_file:
        # A CUSTOM HTML app's iframe reads the tool result's ``content`` via
        # ``ontoolresult`` — ``structuredContent`` is FastMCP's *Prefab*-renderer
        # channel and is NOT forwarded to a custom iframe. So the graph payload
        # rides in a ``content`` JSON block; it's marked ``audience=["user"]`` so
        # the iframe gets it while the MODEL still sees only the short summary.
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
    #brand { font-weight: 700; font-size: 15px; letter-spacing: 0.2px; color: #000000; }
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

    /* Hover tooltip: full, untruncated name + type/subtype + curated metadata. */
    #tooltip { position: absolute; z-index: 5; display: none; pointer-events: none;
      max-width: 300px; background: rgba(255,255,255,0.97); border: 1px solid var(--border);
      color: var(--text); font-size: 12px; padding: 7px 10px; border-radius: 8px;
      box-shadow: 0 4px 16px rgba(0,0,0,0.18); word-break: break-word; }
    #tooltip.show { display: block; }
    #tooltip .tt-title { font-weight: 600; }

    /* Key/value metadata rows shown inside the tooltip. */
    #tooltip .meta { margin-top: 5px; }
    .meta-row { display: flex; gap: 8px; font-size: 11px; margin: 2px 0; }
    .meta-k { color: var(--muted); flex: 0 0 auto; min-width: 62px; text-transform: capitalize; }
    .meta-v { color: var(--text); flex: 1; word-break: break-word; }

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
      <span id="brand">Tree: Your Rooted Memory</span>
      <span id="counts">loading…</span>
      <input id="search" type="search" placeholder="Search nodes…" autocomplete="off" />
    </div>
    <div id="stage">
      <div id="sigma-container"></div>
      <div id="legend"></div>
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

    // HTML-escape user-controlled metadata before it reaches innerHTML.
    function esc(s) {
      return String(s).replace(/[&<>"']/g, (c) =>
        ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
    }
    // Render a curated {key: value} meta dict as key/value rows (or "").
    function metaRows(meta) {
      const keys = meta ? Object.keys(meta) : [];
      if (!keys.length) return "";
      return '<div class="meta">' + keys.map((k) =>
        '<div class="meta-row"><span class="meta-k">' + esc(k) +
        '</span><span class="meta-v">' + esc(meta[k]) + "</span></div>"
      ).join("") + "</div>";
    }

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
          graph.addEdge(e.source, e.target, {
            label: e.type, type: "arrow", size: 1.2, color: "#c2c8d2",
            relType: e.type, meta: e.meta || {},   // carried for the edge hover card
          });
        }
      }

      // Layout.
      const settings = forceAtlas2.inferSettings(graph);
      forceAtlas2.assign(graph, { iterations: 300, settings });

      // Interaction state, applied via reducers.
      const state = { search: "", selected: null, hovered: null, hoveredEdge: null };

      const renderer = new Sigma(graph, container, {
        renderEdgeLabels: true,        // relationship labels
        enableEdgeEvents: true,        // needed for enterEdge / leaveEdge hover
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

      // --- Click only highlights a node; all details are shown on hover. ---
      renderer.on("clickNode", ({ node }) => { state.selected = node; renderer.refresh(); });
      renderer.on("clickStage", () => { state.selected = null; renderer.refresh(); });

      // --- Hover tooltip: full name + type/subtype + curated metadata. ---
      // Shared by node hover and edge hover; positioned at the node, or at the
      // midpoint of the hovered edge.
      const tooltip = document.getElementById("tooltip");
      function placeTooltipAt(vp, above) {
        const rect = container.getBoundingClientRect();
        const tw = tooltip.offsetWidth, th = tooltip.offsetHeight, gap = 12;
        const left = Math.max(8, Math.min(vp.x + gap, rect.width - tw - 8));
        const top = above
          ? Math.max(8, vp.y - th - gap)                               // above the node
          : Math.max(8, Math.min(vp.y - th / 2, rect.height - th - 8)); // beside the edge
        tooltip.style.left = left + "px";
        tooltip.style.top = top + "px";
      }
      function edgeMidViewport(edge) {
        const [s, t] = graph.extremities(edge);
        const a = graph.getNodeAttributes(s), b = graph.getNodeAttributes(t);
        return renderer.graphToViewport({ x: (a.x + b.x) / 2, y: (a.y + b.y) / 2 });
      }
      function positionTooltip() {
        if (state.hovered) {
          const a = graph.getNodeAttributes(state.hovered);
          placeTooltipAt(renderer.graphToViewport({ x: a.x, y: a.y }), true);
        } else if (state.hoveredEdge) {
          placeTooltipAt(edgeMidViewport(state.hoveredEdge), false);
        }
      }
      renderer.on("enterNode", ({ node }) => {
        const n = nodeById.get(node);
        if (!n) return;
        // Lead with type (+ subtype, already in meta), then the rest of the meta.
        const card = Object.assign({ type: n.type }, n.meta);
        tooltip.innerHTML = '<div class="tt-title">' + esc(n.name) + "</div>" + metaRows(card);
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
      // --- Edge hover: relationship type + curated edge metadata ---
      renderer.on("enterEdge", ({ edge }) => {
        const a = graph.getEdgeAttributes(edge);
        const title = a.relType || a.label || "related";
        tooltip.innerHTML = '<div class="tt-title">' + esc(title) + "</div>" + metaRows(a.meta);
        tooltip.classList.add("show");
        state.hoveredEdge = edge;
        positionTooltip();
        container.style.cursor = "pointer";
      });
      renderer.on("leaveEdge", () => {
        state.hoveredEdge = null;
        tooltip.classList.remove("show");
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
