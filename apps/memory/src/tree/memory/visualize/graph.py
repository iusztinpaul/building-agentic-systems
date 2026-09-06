"""The Graph renderer: draw a knowledge-graph ``QueryResult`` as interactive HTML.

Single home of the **Graph renderer** (ADR-005): the browser-side graphology
(graph model) + graphology-layout-forceatlas2 (layout) + Sigma.js (WebGL)
stack, loaded as pinned ESM from jsdelivr. Every graph surface — the
``query_graph.py`` CLI, the ``visualize_memory_graph`` MCP App, the
``query_memory`` / ``search_memory`` tools — consumes the same **Graph
payload** built here and the same HTML templates; no surface owns rendering
code of its own.

What lives here:

* :func:`to_graph_payload` — ``QueryResult`` → the renderer-agnostic
  ``{nodes, edges}`` **Graph payload** (curated hover metadata, palette
  colours, every edge endpoint materialised as a node).
* :data:`_GRAPH_STYLE` / :data:`_BODY_MARKUP` / :data:`_RENDER_JS` +
  :func:`_resolve_static` — the shared CSS / DOM / JS spliced into a template.
* :func:`_render_graph_file` — write a self-contained HTML file (data embedded
  inline as ``const DATA = …``, so it works as a plain ``file://`` page) under
  the one output convention ``.tree/graphs/<query-slug>-<UTC-stamp>.html``.
* :func:`visualize_query_result` — the CLI-facing entry point.

The UI is themed after the "Tree Memory" design: a header with live counts +
node search, a per-type legend, and zoom controls. Node labels show only the
name; hovering a node or edge shows a tooltip with its name / relationship type
plus a curated metadata card. Clicking a node only highlights it.

Needs network at VIEW time (the three libraries load from the CDN rather than
being vendored — ADR-005 decision 2); offline, the page renders an empty canvas.

Dependency direction is memory ← mcp: this module imports NOTHING from
``tree.mcp``. MCP-only concerns (the tools, the ``ui://`` / ``graphs://``
resources, CSP wiring, the ext-apps iframe runtime) live in
``tree.mcp.viz_app``, which imports from here.

The template also draws the **Embedding map** (ADR-007 §2) through four
OPTIONAL payload keys — ``layout: "fixed"`` (use each node's ``x``/``y`` and
skip ForceAtlas2), ``legend`` (explicit rows instead of the per-type one),
``warning`` (an amber banner) and ``hulls`` (the convex-hull overlay), plus
``nodeSize``. A payload without them renders exactly the graph it did before;
the map payload builder is :mod:`tree.memory.visualize.embeddings`.
"""

import json
import logging
import re
import webbrowser
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tree.config.paths import GRAPHS_DIR
from tree.entities.colours import Colours
from tree.memory.types import QueryResult

logger = logging.getLogger(__name__)

_FALLBACK_COLOUR = Colours.BLACK_LEVEL_1  # unknown / unmapped node types

# Node-type palette tuned for a WHITE background, drawn from the brand palette
# (``Colours``). Three hue families: brown = entities, blue = documents/
# structure, orange = knowledge/events. The ONE palette of the Graph
# renderer — every surface (CLI file, MCP App iframe) draws from it.
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


# Curated metadata surfaced on node / edge hover + the dashboard table. Kept
# small on purpose: the full ``properties`` dump is left out so tooltips and
# table cells stay legible.
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
    added with type ``"unknown"``; labels come from the shared
    leading-prefix display-name logic (:func:`_extract_display_name`).

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
    payload: dict[str, Any],
    query: str = "",
    output: Path | None = None,
) -> Path:
    """Write a self-contained HTML file (data embedded inline) and return it.

    Takes any payload the ONE template understands: a **Graph payload** or an
    **Embedding map** payload (the same ``{nodes, edges}`` plus the optional
    ``layout`` / ``legend`` / ``warning`` / ``hulls`` / ``nodeSize`` keys).

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


def visualize_query_result(
    result: QueryResult,
    output: str | Path | None = None,
    *,
    open_browser: bool = True,
    query: str = "",
) -> Path:
    """Render a ``QueryResult`` to a self-contained HTML file and return its path.

    The CLI-facing entry point of the **Graph renderer**: builds the **Graph
    payload** and hands it to :func:`_render_graph_file`.

    Args:
        result: The query result (or full graph) to draw.
        output: Explicit destination file. ``None`` (the default) writes to
            ``.tree/graphs/<query-slug>-<UTC-stamp>.html``.
        open_browser: Pop the file open locally. BEST EFFORT — a headless or
            remote host simply gets no browser, never an exception.
        query: The query text, used for the default filename's slug.
    """

    payload = to_graph_payload(result)
    path = _render_graph_file(
        payload, query=query, output=Path(output) if output else None
    )

    if open_browser:
        try:
            webbrowser.open(path.resolve().as_uri())
        except Exception:  # noqa: BLE001 — a missing browser must never fail a render.
            logger.debug("Could not open a browser for %s", path, exc_info=True)

    return path


def _truncate(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


def _extract_display_name(node_id: str, node_type: str, props: dict) -> str:
    """Derive a human-readable label from a node row.

    Prefers ``properties.canonical_name`` (set by the resolver for typed
    nodes). Falls back to stripping the ``{user_id}:{type}:`` prefix from
    the canonical ``_id`` (``{24-char ObjectId}:{type}:{name}`` per the
    Phase-1 multi-tenancy ID scheme). Names themselves may contain
    further ``:`` segments (e.g. chunk ids), so we strip only the two
    leading prefix segments rather than splitting from the right.
    """

    canonical = props.get("canonical_name") if isinstance(props, dict) else None
    if isinstance(canonical, str) and canonical.strip():
        return canonical

    parts = node_id.split(":", 2)
    if len(parts) == 3 and parts[1] == node_type:
        return parts[2]
    return node_id


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
    /* Embedding-map extras. Both carry the `hidden` attribute until the payload
       asks for them, so the ``display:flex`` needs the explicit override. */
    #warning { display: block; font-size: 12px; padding: 4px 9px; border-radius: 6px;
      background: #fff3bf; border: 1px solid #f08c00; color: #7c4a03; }
    #warning[hidden] { display: none; }
    #hulls-toggle { display: flex; align-items: center; gap: 5px; font-size: 12px;
      color: var(--muted); cursor: pointer; user-select: none; }
    #hulls-toggle[hidden] { display: none; }
    #search:focus { border-color: var(--accent1); }
    #search::placeholder { color: var(--muted); }

    /* Graph stage */
    #stage { position: relative; flex: 1; min-height: 0; }
    #sigma-container { width: 100%; height: 100%; }
    /* Cluster hulls are drawn ON TOP of Sigma's canvases (Sigma has no hull
       primitive) but must never swallow a hover / drag — hence pointer-events. */
    #hulls-layer { position: absolute; inset: 0; pointer-events: none; z-index: 1; }

    /* Legend */
    #legend { position: absolute; top: 10px; right: 12px; font-size: 11px; z-index: 2;
      background: rgba(255,255,255,0.92); border: 1px solid var(--border);
      padding: 8px 10px; border-radius: 10px; max-height: 60%; overflow: auto; }
    #legend .title { color: var(--muted); text-transform: uppercase; letter-spacing: 0.6px;
      font-size: 9px; margin-bottom: 5px; }
    #legend div.row { display: flex; align-items: center; gap: 7px; margin: 3px 0; }
    #legend span.dot { width: 10px; height: 10px; border-radius: 50%; display: inline-block; }
    #legend span.size { margin-left: auto; padding-left: 10px; color: var(--muted); }

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
      <div id="warning" hidden></div>
      <label id="hulls-toggle" hidden><input type="checkbox" id="hulls"> Cluster hulls</label>
      <input id="search" type="search" placeholder="Search nodes…" autocomplete="off" />
    </div>
    <div id="stage">
      <div id="sigma-container"></div>
      <canvas id="hulls-layer"></canvas>
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

    function render(payload) {
      const { nodes, edges } = payload;
      const container = document.getElementById("sigma-container");
      container.innerHTML = "";

      if (!nodes.length) { countsEl.textContent = "No graph data returned."; return; }

      // Optional Embedding-map keys. Absent (a Graph payload) => today's graph:
      // random start + ForceAtlas2, per-type legend, no banner, no hulls.
      const isFixed = payload.layout === "fixed";
      const nodeSize = typeof payload.nodeSize === "number" ? payload.nodeSize : 6;
      const legendRows = Array.isArray(payload.legend) ? payload.legend : null;

      // Header counts. A map has no edges, so "N nodes · 0 edges" would read as
      // a broken graph; its payload summary already counts the two things that
      // matter ("1448 chunks in 37 clusters (+63 noise)").
      countsEl.textContent = isFixed && typeof payload.summary === "string"
        ? payload.summary.replace(/^Embedding map: /, "")
        : nodes.length + " nodes · " + edges.length + " edges";
      const hullsEnabled = legendRows !== null && typeof payload.hulls === "boolean";

      const nodeById = new Map(nodes.map((n) => [n.id, n]));

      // Build the graphology graph. Multi + directed so parallel edges and
      // self-loops don't throw, and edges can carry arrowheads.
      const graph = new Graph({ type: "directed", multi: true });
      for (const n of nodes) {
        graph.addNode(n.id, {
          label: n.label,            // labels in-view show ONLY the name
          nodeType: n.type,          // (Sigma reserves "type" for the program)
          color: n.color,
          size: nodeSize,
          // A fixed layout draws the STORED coordinates (Sigma throws on a
          // non-numeric x/y); otherwise ForceAtlas2 needs distinct starts.
          x: isFixed ? n.x : Math.random(),
          y: isFixed ? n.y : Math.random(),
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

      // Layout. ForceAtlas2 runs ONLY when layout !== "fixed": an Embedding
      // map's coordinates come from the stored UMAP projection, and a force
      // pass would drift every point off the position it was clustered at.
      if (!isFixed) {
        const settings = forceAtlas2.inferSettings(graph);
        forceAtlas2.assign(graph, { iterations: 300, settings });
      }

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

      // --- Legend: the payload's own rows (Embedding map), else one swatch
      //     per node type present (Graph payload). ---
      let rows;
      if (legendRows) {
        rows = legendRows.map((r) =>
          '<div class="row"><span class="dot" style="background:' + esc(r.color) + '"></span>' +
          "<span>" + esc(r.label) + "</span>" +
          (r.size === undefined || r.size === null
            ? ""
            : '<span class="size">· ' + esc(r.size) + "</span>") + "</div>"
        ).join("");
      } else {
        const colorByType = new Map(nodes.map((n) => [n.type, n.color]));
        rows = [...colorByType.entries()].sort((a, b) => a[0].localeCompare(b[0]))
          .map(([t, c]) => '<div class="row"><span class="dot" style="background:' + c + '"></span>' + t + "</div>")
          .join("");
      }
      document.getElementById("legend").innerHTML = '<div class="title">Legend</div>' + rows;

      // --- Stale-map banner: shown only when the payload carries one. ---
      if (payload.warning) {
        const warningEl = document.getElementById("warning");
        warningEl.textContent = payload.warning;
        warningEl.hidden = false;
      }

      // --- Cluster hulls (Embedding map only) ---
      // Sigma has no hull primitive, so the hulls are a plain <canvas> overlay
      // drawn in VIEWPORT coordinates: the hull of each cluster is computed once
      // in graph space, then mapped through graphToViewport on every render, so
      // it stays glued to its points through pan and zoom.
      if (hullsEnabled) {
        const toggle = document.getElementById("hulls-toggle");
        const checkbox = document.getElementById("hulls");
        toggle.hidden = false;
        checkbox.checked = payload.hulls;

        const byCluster = new Map();
        for (const n of nodes) {
          // Noise (-1) is a residue, not a group: it never gets a hull.
          if (typeof n.cluster_id !== "number" || n.cluster_id < 0) continue;
          let entry = byCluster.get(n.cluster_id);
          if (!entry) { entry = { color: n.color, points: [] }; byCluster.set(n.cluster_id, entry); }
          entry.points.push([n.x, n.y]);
        }
        // Andrew's monotone chain: sort by (x, y), then walk the lower and the
        // upper hull, popping any point that does not turn counter-clockwise.
        function convexHull(points) {
          const pts = points.slice().sort((a, b) => a[0] - b[0] || a[1] - b[1]);
          const cross = (o, a, b) =>
            (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0]);
          const build = (seq) => {
            const out = [];
            for (const p of seq) {
              while (out.length >= 2 && cross(out[out.length - 2], out[out.length - 1], p) <= 0) out.pop();
              out.push(p);
            }
            out.pop();
            return out;
          };
          return build(pts).concat(build(pts.slice().reverse()));
        }
        // Fewer than 3 points cannot bound an area — those clusters draw nothing.
        const hulls = [...byCluster.values()]
          .filter((c) => c.points.length >= 3)
          .map((c) => ({ color: c.color, hull: convexHull(c.points) }))
          .filter((h) => h.hull.length >= 3);

        function rgba(hex, alpha) {
          const m = /^#?([0-9a-f]{6})$/i.exec(String(hex));
          if (!m) return hex;
          const v = parseInt(m[1], 16);
          return "rgba(" + ((v >> 16) & 255) + "," + ((v >> 8) & 255) + "," + (v & 255) + "," + alpha + ")";
        }

        const layer = document.getElementById("hulls-layer");
        const hullCtx = layer.getContext("2d");
        function drawHulls() {
          const dpr = window.devicePixelRatio || 1;
          const width = container.offsetWidth, height = container.offsetHeight;
          layer.width = Math.round(width * dpr);      // also clears the canvas
          layer.height = Math.round(height * dpr);
          layer.style.width = width + "px";
          layer.style.height = height + "px";
          hullCtx.setTransform(dpr, 0, 0, dpr, 0, 0);
          if (!checkbox.checked) return;              // unchecked => cleared
          for (const { color, hull } of hulls) {
            hullCtx.beginPath();
            hull.forEach((p, i) => {
              const vp = renderer.graphToViewport({ x: p[0], y: p[1] });
              if (i === 0) hullCtx.moveTo(vp.x, vp.y); else hullCtx.lineTo(vp.x, vp.y);
            });
            hullCtx.closePath();
            hullCtx.fillStyle = rgba(color, 0.12);
            hullCtx.fill();
            hullCtx.lineWidth = 1.5;
            hullCtx.strokeStyle = rgba(color, 0.6);
            hullCtx.stroke();
          }
        }
        checkbox.onchange = drawHulls;
        renderer.on("afterRender", drawHulls);   // pan / zoom / drag / refresh
        renderer.on("resize", drawHulls);        // container size changed
        drawHulls();
      }
    }"""

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

    Deliberately does NOT resolve ``__EXT_APPS_CDN__``: the MCP Apps iframe
    runtime is an MCP-layer concern, so ``tree.mcp.viz_app`` splices that
    token itself and this module stays free of any ``tree.mcp`` coupling.
    """

    return (
        template.replace("__STYLE__", _GRAPH_STYLE)
        .replace("__APP_HEIGHT__", app_height)
        .replace("__BODY__", _BODY_MARKUP)
        .replace("__RENDER_JS__", _RENDER_JS)
        .replace("__GRAPHOLOGY_CDN__", _GRAPHOLOGY_CDN)
        .replace("__SIGMA_CDN__", _SIGMA_CDN)
        .replace("__FA2_CDN__", _FA2_CDN)
    )


# Everything but the per-call ``__DATA__`` substitution is resolved once here.
_FILE_HTML_BASE = _resolve_static(_FILE_HTML_TEMPLATE, "100vh")
