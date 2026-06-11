"""MCP App dashboard — a custom-HTML interactive view over the knowledge graph.

A companion to the Sigma graph view (``graph_app``). Where the graph view shows
topology, this shows a **dashboard**: KPI metrics + a node-type bar chart +
searchable/sortable tables of the returned nodes and relationships, each row
carrying the curated metadata from ``_curated_meta``.

``memory_dashboard`` follows the same low-level MCP Apps pattern as
``visualize_memory_graph`` (https://gofastmcp.com/apps/low-level):

* the tool runs the structured query and ships the ``{query, nodes, edges}``
  payload in a ``content`` JSON block (``audience=["user"]``), and
* ``dashboard_view`` serves the ``ui://`` HTML resource — a sandboxed iframe
  that receives the tool result via the ext-apps ``ontoolresult`` channel and
  renders the dashboard client-side (vanilla JS, no chart/table libraries).

It deliberately does NOT use FastMCP's Prefab renderer: the Prefab renderer
iframe reads ONLY ``structuredContent`` from the tool result, and the App-UI
host forwards only ``content`` blocks to app iframes (the same host behaviour
that broke the graph view — see ``graph_app``'s module docstring). A Prefab
dashboard therefore renders blank; a custom HTML app reading ``content`` works.
"""

import json
from collections import Counter
from typing import Any

from fastmcp import Context
from fastmcp.apps import UI_EXTENSION_ID, AppConfig, ResourceCSP
from fastmcp.tools import ToolResult
from mcp import types

from tree.mcp.graph_app import to_graph_payload
from tree.mcp.server import mcp
from tree.memory.query.core import fetch_full_graph
from tree.memory.query.core import query_memory as structured_query_memory

DASHBOARD_VIEW_URI = "ui://tree-memory/dashboard.html"

_EXT_APPS_CDN = "https://unpkg.com/@modelcontextprotocol/ext-apps@0.4.0/app-with-deps"


async def _fetch_payload(
    ctx: Context, query: str, top_k: int, max_hops: int
) -> dict[str, list[dict[str, Any]]]:
    """Run the structured query (or full-graph fetch) → {nodes, edges} payload."""

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
    else:
        result = await fetch_full_graph(
            client=lc["client"],
            database=lc["database"],
            user_id=lc["user_id"],
        )
    return to_graph_payload(result)


def _summary(payload: dict[str, list[dict[str, Any]]], label: str) -> str:
    """One-line model-facing summary (so non-UI clients still get context)."""

    counts = Counter(n["type"] for n in payload["nodes"])
    breakdown = ", ".join(f"{t}:{c}" for t, c in counts.most_common())
    return (
        f"Dashboard for {label}: {len(payload['nodes'])} nodes, "
        f"{len(payload['edges'])} edges across {len(counts)} types"
        + (f" ({breakdown})." if breakdown else ".")
    )


@mcp.tool(app=AppConfig(resource_uri=DASHBOARD_VIEW_URI))
async def memory_dashboard(
    ctx: Context,
    query: str = "",
    top_k: int = 20,
    max_hops: int = 2,
) -> ToolResult | str:
    """Dashboard of the knowledge graph: KPIs, node-type chart, node/edge tables.

    Interactive custom-HTML UI (sortable/searchable tables, bar chart). Use
    when the user wants a *summary* of what's in memory rather than the graph
    topology (for topology use ``visualize_memory_graph``).

    With a ``query``, the dashboard covers the matching subgraph; with NO query
    (the default) it covers the user's ENTIRE memory graph.

    Args:
        query: Search query text — seeds which nodes the dashboard covers.
            Omit (empty) to cover the whole memory graph.
        top_k: Number of seed nodes to retrieve (default 20). Ignored with no query.
        max_hops: Hops of graph expansion around the seeds (default 2). Ignored
            with no query.
    """

    payload = await _fetch_payload(ctx, query, top_k, max_hops)
    label = repr(query) if query else "your full memory"
    summary = _summary(payload, label)

    if not ctx.client_supports_extension(UI_EXTENSION_ID):
        # Text-only client: the summary already carries the per-type breakdown.
        return (
            f"{summary} (This client does not render inline MCP App UIs — "
            "use query_memory / search_memory to inspect the details.)"
        )

    # The iframe reads the tool result's ``content`` via ``ontoolresult`` —
    # the host does NOT forward ``structuredContent`` to a custom iframe (see
    # module docstring). The payload rides in a ``content`` JSON block marked
    # ``audience=["user"]`` so the iframe gets the full dump while the MODEL
    # sees only the short summary. ``structured_content`` is kept for any host
    # that forwards it too.
    app_payload = {"query": query, **payload}
    return ToolResult(
        content=[
            types.TextContent(
                type="text",
                text=f"{summary} (interactive dashboard view).",
            ),
            types.TextContent(
                type="text",
                text=json.dumps(app_payload),
                annotations=types.Annotations(audience=["user"]),
            ),
        ],
        structured_content=app_payload,
    )


@mcp.resource(
    DASHBOARD_VIEW_URI,
    app=AppConfig(csp=ResourceCSP(resource_domains=["https://unpkg.com"])),
)
def dashboard_view() -> str:
    """Interactive knowledge-graph dashboard (read-only)."""

    return _DASHBOARD_HTML


# ---------------------------------------------------------------------------
# HTML template. A plain (non-f) string: it contains JS ``{}`` blocks that
# must reach the browser verbatim. The only remote dep is the ext-apps bridge;
# the chart and tables are hand-rolled DOM (no chart/table libraries).
# ---------------------------------------------------------------------------

_DASHBOARD_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="color-scheme" content="light" />
  <style>
    :root {
      --bg: #ffffff; --panel: #f5f6f8; --border: #d9dde4; --muted: #6b7280;
      --text: #1f2430; --accent1: #ea580c; --accent2: #f59e0b;
    }
    /* Fixed height: hosts size the MCP App iframe to the body's height. */
    html, body { margin: 0; height: 760px; background: var(--bg); color: var(--text);
      font-family: ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif; }
    #wrap { display: flex; flex-direction: column; height: 100%; }

    /* Header */
    #header { display: flex; align-items: baseline; gap: 14px; padding: 10px 16px;
      background: var(--panel); border-bottom: 1px solid var(--border); }
    #brand { font-weight: 700; font-size: 15px; letter-spacing: 0.2px; color: #000; }
    #query { font-size: 12px; color: var(--muted); overflow: hidden;
      text-overflow: ellipsis; white-space: nowrap; }

    #scroll { flex: 1; overflow: auto; padding: 14px 16px; }

    /* KPI metrics */
    #metrics { display: flex; gap: 12px; margin-bottom: 14px; }
    .metric { background: var(--panel); border: 1px solid var(--border);
      border-radius: 10px; padding: 10px 16px; min-width: 90px; }
    .metric .value { font-size: 22px; font-weight: 700; }
    .metric .label { font-size: 11px; color: var(--muted); text-transform: uppercase;
      letter-spacing: 0.6px; }

    /* Node-type bar chart (pure CSS horizontal bars) */
    #chart { margin-bottom: 16px; }
    .bar-row { display: flex; align-items: center; gap: 8px; margin: 4px 0; font-size: 12px; }
    .bar-label { flex: 0 0 110px; text-align: right; color: var(--muted); }
    .bar-track { flex: 1; }
    .bar { height: 16px; border-radius: 4px; min-width: 2px; }
    .bar-count { flex: 0 0 40px; font-weight: 600; }

    h2 { font-size: 13px; text-transform: uppercase; letter-spacing: 0.6px;
      color: var(--muted); margin: 18px 0 8px; }

    /* Tables */
    .table-tools { display: flex; align-items: center; gap: 10px; margin-bottom: 6px; }
    .table-tools input { width: 220px; padding: 5px 9px; background: #fff;
      border: 1px solid var(--border); border-radius: 8px; font-size: 12px; outline: none; }
    .table-tools input:focus { border-color: var(--accent1); }
    .pager { margin-left: auto; display: flex; align-items: center; gap: 6px; font-size: 12px;
      color: var(--muted); }
    .pager button { border: 1px solid var(--border); background: var(--panel);
      border-radius: 6px; padding: 3px 9px; cursor: pointer; font-size: 12px; }
    .pager button:disabled { opacity: 0.4; cursor: default; }
    table { width: 100%; border-collapse: collapse; font-size: 12px; }
    th { text-align: left; padding: 6px 8px; background: var(--panel);
      border-bottom: 1px solid var(--border); cursor: pointer; user-select: none;
      white-space: nowrap; }
    th .dir { color: var(--accent1); }
    td { padding: 6px 8px; border-bottom: 1px solid var(--border); vertical-align: top;
      word-break: break-word; }
    .num { text-align: right; }
    #empty { color: var(--muted); font-size: 13px; }
  </style>
</head>
<body>
  <div id="wrap">
    <div id="header">
      <span id="brand">Tree Memory Dashboard</span>
      <span id="query"></span>
    </div>
    <div id="scroll">
      <div id="metrics"></div>
      <div id="chart"></div>
      <div id="tables"></div>
      <div id="empty" hidden>No results for this query.</div>
    </div>
  </div>
  <script type="module">
    import { App } from "__EXT_APPS_CDN__";

    // HTML-escape user-controlled values before they reach innerHTML.
    function esc(s) {
      return String(s).replace(/[&<>"']/g, (c) =>
        ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
    }

    function render({ query, nodes, edges }) {
      document.getElementById("query").textContent =
        query ? "query: " + query : "full memory graph";

      if (!nodes.length) { document.getElementById("empty").hidden = false; return; }

      // --- Node-type counts (desc, then alpha) with the node palette colour ---
      const colorByType = new Map(), countByType = new Map();
      for (const n of nodes) {
        countByType.set(n.type, (countByType.get(n.type) || 0) + 1);
        if (!colorByType.has(n.type)) colorByType.set(n.type, n.color || "#999");
      }
      const counts = [...countByType.entries()]
        .sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));

      // --- KPI metrics ---
      const metrics = [["Nodes", nodes.length], ["Edges", edges.length], ["Types", counts.length]];
      document.getElementById("metrics").innerHTML = metrics.map(([l, v]) =>
        '<div class="metric"><div class="value">' + v + '</div><div class="label">' + l + "</div></div>"
      ).join("");

      // --- Bar chart ---
      const max = counts[0][1];
      document.getElementById("chart").innerHTML = counts.map(([t, c]) =>
        '<div class="bar-row"><span class="bar-label">' + esc(t) + '</span>' +
        '<span class="bar-track"><span class="bar" style="width:' + (100 * c / max) +
        "%;background:" + esc(colorByType.get(t)) + '"></span></span>' +
        '<span class="bar-count">' + c + "</span></div>"
      ).join("");

      // --- Tables ---
      const nameById = new Map(nodes.filter((n) => n.id).map((n) => [n.id, n.name]));
      const meta = (o, k) => (o.meta || {})[k];
      const nodeRows = nodes.map((n) => ({
        name: n.name, type: n.type, subtype: meta(n, "subtype") || "",
        confidence: meta(n, "confidence"), description: meta(n, "description") || "",
        aliases: meta(n, "aliases") || "",
      }));
      const edgeRows = edges.map((e) => ({
        source: nameById.get(e.source) || e.source, type: e.type,
        target: nameById.get(e.target) || e.target,
        semantic_type: meta(e, "semantic_type") || "",
        confidence: meta(e, "confidence"), description: meta(e, "description") || "",
      }));

      const tables = document.getElementById("tables");
      makeTable(tables, "Nodes", nodeRows, [
        ["name", "Name"], ["type", "Type"], ["subtype", "Subtype"],
        ["confidence", "Confidence", "num"], ["description", "Description"],
        ["aliases", "Aliases"],
      ]);
      if (edgeRows.length) {
        makeTable(tables, "Relationships", edgeRows, [
          ["source", "Source"], ["type", "Type"], ["target", "Target"],
          ["semantic_type", "Semantic"], ["confidence", "Confidence", "num"],
          ["description", "Description"],
        ]);
      }
    }

    // A searchable / sortable / paginated table, re-rendered from state.
    const PAGE_SIZE = 10;
    function makeTable(parent, title, rows, columns) {
      const section = document.createElement("div");
      section.innerHTML = '<h2>' + esc(title) + '</h2>' +
        '<div class="table-tools"><input type="search" placeholder="Search…" />' +
        '<span class="pager"><button class="prev">‹</button><span class="page"></span>' +
        '<button class="next">›</button></span></div><table></table>';
      parent.appendChild(section);

      const state = { search: "", sortKey: null, sortDir: 1, page: 0 };
      const input = section.querySelector("input");
      const prev = section.querySelector(".prev"), next = section.querySelector(".next");
      const pageEl = section.querySelector(".page"), table = section.querySelector("table");

      function view() {
        let v = rows;
        if (state.search) {
          v = v.filter((r) => columns.some(([k]) =>
            String(r[k] ?? "").toLowerCase().includes(state.search)));
        }
        if (state.sortKey) {
          const k = state.sortKey, d = state.sortDir;
          v = [...v].sort((a, b) => {
            const x = a[k], y = b[k];
            if (x == null) return 1;             // nulls always sink
            if (y == null) return -1;
            if (typeof x === "number" && typeof y === "number") return (x - y) * d;
            return String(x).localeCompare(String(y)) * d;
          });
        }
        return v;
      }

      function draw() {
        const v = view();
        const pages = Math.max(1, Math.ceil(v.length / PAGE_SIZE));
        state.page = Math.min(state.page, pages - 1);
        const slice = v.slice(state.page * PAGE_SIZE, (state.page + 1) * PAGE_SIZE);

        table.innerHTML =
          "<tr>" + columns.map(([k, h]) =>
            '<th data-key="' + k + '">' + esc(h) +
            (state.sortKey === k ? ' <span class="dir">' + (state.sortDir > 0 ? "▲" : "▼") + "</span>" : "") +
            "</th>").join("") + "</tr>" +
          slice.map((r) => "<tr>" + columns.map(([k, _h, cls]) => {
            let val = r[k];
            if (k === "confidence" && typeof val === "number") val = val.toFixed(2);
            return '<td class="' + (cls || "") + '">' + esc(val ?? "") + "</td>";
          }).join("") + "</tr>").join("");

        pageEl.textContent = (state.page + 1) + " / " + pages + " (" + v.length + ")";
        prev.disabled = state.page === 0;
        next.disabled = state.page >= pages - 1;

        for (const th of table.querySelectorAll("th")) {
          th.onclick = () => {
            const k = th.dataset.key;
            state.sortDir = state.sortKey === k ? -state.sortDir : 1;
            state.sortKey = k;
            draw();
          };
        }
      }

      input.oninput = () => { state.search = input.value.trim().toLowerCase(); state.page = 0; draw(); };
      prev.onclick = () => { state.page--; draw(); };
      next.onclick = () => { state.page++; draw(); };
      draw();
    }

    const app = new App({ name: "Tree Memory Dashboard", version: "1.0.0" });

    app.ontoolresult = (result) => {
      const r = result || {};
      // The host forwards only `content` to a custom iframe; read that first,
      // then fall back to `structuredContent` for hosts that do forward it.
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
      else { document.getElementById("empty").hidden = false;
             document.getElementById("empty").textContent = "No dashboard data in tool result."; }
    };

    await app.connect();
  </script>
</body>
</html>"""

_DASHBOARD_HTML = _DASHBOARD_HTML_TEMPLATE.replace("__EXT_APPS_CDN__", _EXT_APPS_CDN)
