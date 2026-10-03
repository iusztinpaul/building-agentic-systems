"""The Graph renderer: draw a ``MemoryStructure`` as interactive HTML.

Single home of the **Graph renderer** (ADR-005, ADR-011): the browser-side
graphology (graph model) + d3-force (live layout) + Sigma.js (WebGL) stack,
loaded as pinned ESM from jsdelivr. Every graph surface — the
``visualize_structure.py`` CLI, the ``visualize_memory_structure`` MCP App
(both modes: the rag **Memory structure** tree and the graphrag knowledge
graph), the ``query_memory`` / ``search_memory`` tools — consumes the same
**Graph payload** built here and the same HTML templates; no surface owns
rendering code of its own.

What lives here:

* :func:`to_graph_payload` — ``MemoryStructure`` → the renderer-agnostic
  ``{nodes, edges, controls}`` **Graph payload** (curated hover metadata,
  palette colours, a per-role node ``size``, every edge endpoint materialised
  as a node, and the force / display defaults the template seeds from).
* :data:`_GRAPH_STYLE` / :data:`_BODY_MARKUP` / :data:`_RENDER_JS` +
  :func:`_resolve_static` — the shared CSS / DOM / JS spliced into a template.
* :func:`_render_graph_file` — write a self-contained HTML file (data embedded
  inline as ``const DATA = …``, so it works as a plain ``file://`` page) under
  the one output convention ``.tree/graphs/<query-slug>-<UTC-stamp>.html``.
* :func:`densify_document_ranks` — re-key a TRUNCATED payload's ``docRank``
  on the document stars it draws.
* :func:`visualize_query_result` — the CLI-facing entry point.

The UI is themed after the "Tree Memory" design: a header with live counts +
node search, a per-type legend, and zoom controls. Node labels show only the
name; hovering a node or edge shows a tooltip with its name / relationship type
plus a curated metadata card. The layout is LIVE (ADR-011): d3-force keeps
ticking until it cools; dragging a node pulls its neighbours along and DROPS it
as a **Pinned node** (dark centre dot), which a double-click unpins. A click
selects a node (orange ring), Shift+click toggles, Shift+drag on the stage
box-selects, Esc or a stage click clears; dragging a selected node moves the
whole selection, and every dragged node carries its unpinned direct
``part_of`` children rigidly. A collapsible Controls panel (top-left), seeded
from ``payload["controls"]``, tunes the forces (reheat), the display (redraw
only) and offers Pause / Unpin all / Reset to defaults. On a ranked payload
(``controls.documents``: the **Full graph** by recency, a query view by search
relevance) its first section is a ``Documents`` slider that hides nodes beyond
the N best-ranked documents in Sigma AND in the simulation.

Needs network at VIEW time (the libraries load from the CDN rather than being
vendored — ADR-005 decision 2); offline, the page renders an empty canvas.

Dependency direction is memory ← mcp: this module imports NOTHING from
``tree.mcp``. MCP-only concerns (the tools, the ``ui://`` / ``graphs://``
resources, CSP wiring, the ext-apps iframe runtime) live in
``tree.mcp.viz_app``, which imports from here.

The template also draws the **Embedding map** (ADR-007 §2) through four
OPTIONAL payload keys — ``layout: "fixed"`` (use each node's ``x``/``y`` and
build no simulation; drag, pin and the display knobs still work), ``legend``
(explicit rows instead of the per-type one; a row's ``cluster_id`` colours the
lean map nodes, which ship no ``color`` of their own), ``warning`` (an amber
banner) and ``hulls`` (the convex-hull overlay). A payload without them renders the live
graph; the map payload builder is :mod:`tree.memory.visualize.embeddings`.
"""

import json
import logging
import re
import webbrowser
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from tree.config.app_config import app_config
from tree.config.paths import GRAPHS_DIR
from tree.entities.colours import Colours
from tree.memory.rag.types import MemoryStructure

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

# Browser deps, pinned (ADR-011 §2). Sigma v3 declares no UMD global, so every
# dep loads as ESM via jsdelivr's +esm endpoint. The d3-force bundle imports
# its own three deps (d3-quadtree / d3-dispatch / d3-timer) from jsdelivr, so
# ONE import line replaces the four ordered UMD <script> tags pulse needs.
_GRAPHOLOGY_CDN = "https://cdn.jsdelivr.net/npm/graphology@0.26.0/+esm"
_SIGMA_CDN = "https://cdn.jsdelivr.net/npm/sigma@3.0.3/+esm"
_D3_FORCE_CDN = "https://cdn.jsdelivr.net/npm/d3-force@3.0.0/+esm"

# Node radius by role (ADR-011 §6): a document outranks its parent chunks,
# which outrank the child chunks they split into, so a document→chunk star
# reads at a glance. Keys are ``type`` or ``type:subtype``; only a chunk's
# subtype is a role, so an entity is looked up by its bare type.
_NODE_SIZES: dict[str, int] = {"document": 10, "chunk:parent": 7, "chunk:child": 4}
_DEFAULT_NODE_SIZE = 6  # entities, ``unknown`` endpoints, a role-less chunk

# What the template seeds the live d3-force simulation and its reducers with
# (ADR-011 §6 — Python decides, the JS obeys). ``forces`` mirror pulse's
# sliders (repel is multiplied by the template's REPEL_SCALE = 30); ``gravity``
# is the strength of the forceX(0) / forceY(0) pair (ADR-011 §1): a compact
# layout (the real full graph's extent 4511 -> 1669 at 0.05, no overlaps) with
# edge-less nodes held near the stars — 0 disables it. It does NOT keep a
# reveal inside a frozen viewport (it shrinks that viewport too, task 165 Log):
# the one auto-fit after a Documents reveal does;
# ``display`` values are MULTIPLIERS / switches over what Python resolved
# (``nodeSize: 1.0`` draws each node's ``size`` as is).
_DEFAULT_FORCES: dict[str, float] = {
    "centre": 0.2,
    "gravity": 0.05,
    "repel": 8.0,
    "link": 0.3,
    "linkDistance": 80,
}
_DEFAULT_DISPLAY: dict[str, float | bool] = {
    "nodeSize": 1.0,
    "linkThickness": 1.0,
    "labelFade": 1,
    "arrows": True,
    "edgeLabels": True,
}


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


def _node_size(node_type: str, subtype: str | None) -> int:
    """The radius a node is drawn at, by role (see ``_NODE_SIZES``)."""

    if node_type == "chunk" and subtype:
        return _NODE_SIZES.get(f"chunk:{subtype}", _DEFAULT_NODE_SIZE)
    return _NODE_SIZES.get(node_type, _DEFAULT_NODE_SIZE)


def to_graph_payload(
    result: MemoryStructure,
    *,
    document_order: Literal["recency", "relevance"] = "recency",
) -> dict[str, Any]:
    """Flatten a ``MemoryStructure`` into a graph ``{nodes, edges, controls}`` payload.

    A graphrag ``QueryResult`` is one; so is the rag **Memory structure**, whose
    ``part_of`` edges were synthesised from ``parent_id``.

    Every edge endpoint is guaranteed to also exist as a node — partial graphs
    may reference nodes that were not in the seed set, and the renderer drops
    edges with a dangling endpoint. Endpoints discovered only via edges are
    added with type ``"unknown"``; labels come from the shared
    leading-prefix display-name logic (:func:`_extract_display_name`).

    Each node and edge carries a curated ``meta`` dict (see ``_curated_meta``)
    surfaced on hover and in the dashboard table; nodes materialised only from a
    dangling edge endpoint get an empty ``meta``.

    Each node also carries its drawn ``size`` by role (:func:`_node_size`), and
    ``controls`` ships the force / display defaults the template seeds its live
    layout and reducers from.

    A row stamped ``doc_rank`` — by recency on the **Full graph**
    (:func:`fetch_full_graph`), by search relevance on a seed-based query view
    (:func:`~tree.memory.graph.retrieval.query_memory`) — gives its node a
    ``docRank``, and then ``controls.documents`` = ``{shown, total, order}``:
    the gate for the renderer's ``Documents`` slider. ``order`` is the
    caller's ``document_order`` (Python decides which ranking the rows carry);
    ``total`` is the deepest rank; ``shown`` is ``query.full_graph_shown_docs``
    clamped to ``query.full_graph_max_docs`` and to ``total`` for ``recency``,
    and ``total`` for ``relevance`` — a query result is ``top_k`` seeds wide,
    so its slider narrows, it never caps (task 168). An unranked payload (an
    NL ``query_memory`` pipeline's rows) carries neither.

    A parent chunk a query view stamped ``child_count`` (its children exist but
    none was pulled in, ADR-011 §8) gives its node a ``childCount``, which the
    hover card shows; every other node has no such key.
    """

    nodes: list[dict[str, Any]] = []
    seen: set[str] = set()

    def _add_node(
        node_id: str,
        node_type: str,
        props: dict[str, Any],
        meta: dict[str, Any] | None = None,
        subtype: str | None = None,
        doc_rank: int | None = None,
        child_count: int | None = None,
    ) -> None:
        if not node_id or node_id in seen:
            return
        seen.add(node_id)
        name = _extract_display_name(node_id, node_type, props)
        node: dict[str, Any] = {
            "id": node_id,
            "type": node_type,
            "name": name,  # full, untruncated — shown on hover / click
            "label": _truncate(name, 40),  # shown on the canvas
            "color": _NODE_COLOURS.get(node_type, _FALLBACK_COLOUR),
            "size": _node_size(node_type, subtype),
            "meta": meta or {},
        }
        if doc_rank is not None:
            node["docRank"] = doc_rank
        if child_count is not None:
            node["childCount"] = child_count
        nodes.append(node)

    for node in result.nodes:
        _add_node(
            str(node["_id"]),
            node.get("type", "unknown"),
            node.get("properties") or {},
            _curated_meta(node, _NODE_META_FIELDS),
            node.get("subtype"),
            node.get("doc_rank"),
            node.get("child_count"),
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

    controls: dict[str, Any] = {
        "forces": dict(_DEFAULT_FORCES),
        "display": dict(_DEFAULT_DISPLAY),
    }
    ranks = [node["docRank"] for node in nodes if "docRank" in node]
    if ranks:
        total = max(ranks)
        shown = total
        if document_order == "recency":
            query = app_config.query
            shown = min(query.full_graph_shown_docs, query.full_graph_max_docs, total)
        controls["documents"] = {
            "shown": shown,
            "total": total,
            "order": document_order,
        }

    return {"nodes": nodes, "edges": edges, "controls": controls}


def densify_document_ranks(payload: dict[str, Any]) -> dict[str, Any]:
    """Re-key a payload's ``docRank`` on the document stars it actually draws.

    A view built from TRUNCATED rows (``search_memory(visualize=True)`` keeps
    ``max_results`` of them) can draw rows ranked 2 while rank 1's document
    and every row it owns were cut — the slider at 1 would then empty the
    canvas (task 168 QA). The drawn ``document`` nodes' ranks are renumbered
    densely ``1..k`` in their original order; a node whose rank no drawn
    star carries loses ``docRank`` (always shown); ``controls.documents``
    becomes ``total = k`` with ``shown`` = the drawn stars the old default
    showed (at least 1), or is dropped when no star is drawn. A dense payload
    (every surface that does not truncate) comes back unchanged. Pure: the
    input is never mutated.
    """

    documents = (payload.get("controls") or {}).get("documents")
    if not documents:
        return payload
    drawn = sorted(
        {
            node["docRank"]
            for node in payload["nodes"]
            if node["type"] == "document" and "docRank" in node
        }
    )
    dense = {rank: position for position, rank in enumerate(drawn, start=1)}

    nodes: list[dict[str, Any]] = []
    for node in payload["nodes"]:
        rank = node.get("docRank")
        if rank is None or dense.get(rank) == rank:
            nodes.append(node)
        elif rank in dense:
            nodes.append({**node, "docRank": dense[rank]})
        else:
            nodes.append({k: v for k, v in node.items() if k != "docRank"})

    controls = {k: v for k, v in payload["controls"].items() if k != "documents"}
    if drawn:
        shown = sum(rank <= documents["shown"] for rank in drawn)
        controls["documents"] = {
            **documents,
            "shown": max(shown, 1),
            "total": len(drawn),
        }
    return {**payload, "nodes": nodes, "controls": controls}


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


def _payload_noun(payload: dict[str, Any]) -> str:
    """What this payload IS, in the operator's words: a map or a graph.

    ONE mechanism behind every user-visible string about a rendered payload
    (this module's log line and ``tree.mcp.viz_app``'s three delivery
    sentences): an **Embedding map** is the payload that draws stored
    coordinates (``layout: "fixed"``), everything else is a **Graph payload**.
    Calling a map a "graph" is wrong in `rag` mode, where no graph exists at
    all, and the glossary keeps the two terms distinct — the map has no edges.
    """

    return "embedding map" if payload.get("layout") == "fixed" else "graph"


def _render_graph_file(
    payload: dict[str, Any],
    query: str = "",
    output: Path | None = None,
) -> Path:
    """Write a self-contained HTML file (data embedded inline) and return it.

    Takes any payload the ONE template understands: a **Graph payload** or an
    **Embedding map** payload (the same ``{nodes, edges}`` plus the optional
    ``layout`` / ``legend`` / ``warning`` / ``hulls`` keys).

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
    # A map has no edges BY DESIGN, so "0 edges" would read as a broken render
    # (the same reasoning as the template's header counts).
    noun = _payload_noun(payload)
    documents = (payload.get("controls") or {}).get("documents")
    counts = f"{len(payload['nodes'])} nodes, {len(payload['edges'])} edges"
    if noun == "embedding map":
        counts = f"{len(payload['nodes'])} points"
    elif documents:
        counts = (
            f"{documents['shown']} of {documents['total']} documents shown by "
            f"default, {counts}"
        )
    logger.info("Wrote self-contained %s HTML (%s) to %s", noun, counts, path)
    return path


def visualize_query_result(
    result: MemoryStructure,
    output: str | Path | None = None,
    *,
    open_browser: bool = True,
    query: str = "",
    document_order: Literal["recency", "relevance"] | None = None,
) -> Path:
    """Render a ``MemoryStructure`` to a self-contained HTML file and return its path.

    The CLI-facing entry point of the **Graph renderer**: builds the **Graph
    payload** and hands it to :func:`_render_graph_file`.

    Args:
        result: The query result (or full graph) to draw.
        output: Explicit destination file. ``None`` (the default) writes to
            ``.tree/graphs/<query-slug>-<UTC-stamp>.html``.
        open_browser: Pop the file open locally. BEST EFFORT — a headless or
            remote host simply gets no browser, never an exception.
        query: The query text, used for the default filename's slug — and,
            unless ``document_order`` is given, the ranking: a query view's
            documents rank by relevance, the **Full graph**'s (no query) by
            recency.
        document_order: The ranking the rows carry, when the slug is not a
            query (the rag no-query view is slugged ``structure`` yet ranked
            by recency).
    """

    if document_order is None:
        document_order = "relevance" if query else "recency"
    payload = to_graph_payload(result, document_order=document_order)
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
    /* The overlay (cluster hulls, pin dots) is drawn ON TOP of Sigma's
       canvases (Sigma has no primitive for either) but must never swallow a
       hover / drag — hence pointer-events. */
    #overlay { position: absolute; inset: 0; pointer-events: none; z-index: 1; }

    /* Legend */
    #legend { position: absolute; top: 10px; right: 12px; font-size: 11px; z-index: 2;
      background: rgba(255,255,255,0.92); border: 1px solid var(--border);
      padding: 8px 10px; border-radius: 10px; max-height: 60%; overflow: auto; }
    #legend .title { color: var(--muted); text-transform: uppercase; letter-spacing: 0.6px;
      font-size: 9px; margin-bottom: 5px; }
    #legend div.row { display: flex; align-items: center; gap: 7px; margin: 3px 0; }
    #legend span.dot { width: 10px; height: 10px; border-radius: 50%; display: inline-block; }
    #legend span.size { margin-left: auto; padding-left: 10px; color: var(--muted); }

    /* Controls panel: top-left (the legend owns the top-right). The toggle
       hides #panel-body, leaving just the button. */
    #panel { position: absolute; top: 10px; left: 12px; z-index: 2; width: 190px;
      box-sizing: border-box; font-size: 11px; background: rgba(255,255,255,0.92);
      border: 1px solid var(--border); padding: 6px 10px; border-radius: 10px;
      max-height: calc(100% - 20px); overflow: auto; }
    #panel:has(#panel-body[hidden]) { width: auto; }   /* collapsed: hug the button */
    #panel button { padding: 3px 8px; border-radius: 6px; cursor: pointer; font-size: 11px;
      background: var(--panel); color: var(--text); border: 1px solid var(--border); }
    #panel button:hover { border-color: var(--accent1); }
    #panel .panel-title { color: var(--muted); text-transform: uppercase; letter-spacing: 0.6px;
      font-size: 9px; margin: 8px 0 3px; }
    #panel .panel-row { padding: 2px 0; }
    #panel .panel-head { display: flex; justify-content: space-between; gap: 8px; }
    #panel .panel-head span:last-child { color: var(--muted); font-variant-numeric: tabular-nums; }
    #panel .panel-check { display: flex; align-items: center; justify-content: space-between;
      padding: 2px 0; cursor: pointer; }
    #panel input { margin: 2px 0 0; accent-color: var(--accent1); }
    #panel input[type=range] { width: 100%; }
    #panel .panel-buttons { display: flex; flex-wrap: wrap; gap: 5px; }

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
      <canvas id="overlay"></canvas>
      <div id="panel">
        <button id="panel-toggle" type="button">Controls</button>
        <div id="panel-body"></div>
      </div>
      <div id="legend"></div>
      <div id="tooltip"></div>
      <div id="zoom">
        <button id="zoom-in" title="Zoom in">+</button>
        <button id="zoom-out" title="Zoom out">−</button>
        <button id="zoom-fit" title="Fit to view">⊡</button>
      </div>
    </div>
  </div>"""

# graphology + Sigma + d3-force render. Defines render(payload). Expects Graph /
# Sigma / forceSimulation / forceLink / forceManyBody / forceCenter imported
# above, and the _BODY_MARKUP DOM present.
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
      // The iframe calls render() once per tool result: rebuild the panel's
      // rows (bound to the previous renderer) from this payload.
      const panelBody = document.getElementById("panel-body");
      panelBody.textContent = "";
      delete document.body.dataset.docs;   // set again below on a ranked payload only
      document.getElementById("panel").hidden = !nodes.length;

      if (!nodes.length) { countsEl.textContent = "No graph data returned."; return; }

      // Optional Embedding-map keys. Absent (a Graph payload) => the live
      // d3-force layout, per-type legend, no banner, no hulls.
      const isFixed = payload.layout === "fixed";
      const legendRows = Array.isArray(payload.legend) ? payload.legend : null;
      // Display knobs: multipliers / switches over what Python resolved. Both
      // payload builders ship them; a hand-made fixed-layout payload without
      // `controls` simply draws as is.
      const display = Object.assign(
        { nodeSize: 1, linkThickness: 1, labelFade: 1, arrows: true, edgeLabels: true },
        payload.controls && payload.controls.display);

      // Header counts. A map has no edges, so "N nodes · 0 edges" would read as
      // a broken graph; its payload summary already counts the two things that
      // matter ("1448 chunks in 37 clusters (+63 noise)").
      countsEl.textContent = isFixed && typeof payload.summary === "string"
        ? payload.summary.replace(/^Embedding map: /, "")
        : nodes.length + " nodes · " + edges.length + " edges";
      const hullsEnabled = legendRows !== null && typeof payload.hulls === "boolean";

      // ONE colour resolver for every site that paints a node. A Graph payload
      // node ships its own `color`; a lean Embedding-map node does not — its
      // legend row (matched on `cluster_id`, noise -1) carries the hue. Any
      // other negative id is noise too (embeddings.NOISE_COLOUR), never the
      // "unclustered / stale" grey, which names chunks NOT on the map.
      const NOISE_COLOUR = "#9e9e9e";
      const colourByCluster = new Map(
        (legendRows || []).filter((r) => typeof r.cluster_id === "number")
          .map((r) => [r.cluster_id, r.color]));
      const colourOf = (n) => n.color ?? colourByCluster.get(n.cluster_id)
        ?? (n.cluster_id < 0 ? NOISE_COLOUR : "#d5d8de");

      const nodeById = new Map(nodes.map((n) => [n.id, n]));
      // Only an edge whose two endpoints exist is drawn — or simulated.
      const drawnEdges = edges.filter((e) => nodeById.has(e.source) && nodeById.has(e.target));

      // The Documents slider (ADR-011 §7). A ranked payload ranks a node by
      // its best document (`docRank`: recency on the Full graph, search
      // relevance on a query view — `documents.order`) and shows the first
      // `docLimit` documents; an unranked node always shows.
      const documents = payload.controls && payload.controls.documents;
      const ORDER_LABEL = { recency: "Most recent", relevance: "Most relevant" };
      let docLimit = documents ? documents.shown : Infinity;
      function isVisible(id) {
        const rank = nodeById.get(id).docRank;
        return rank == null || rank <= docLimit;
      }

      // Pin state lives on the d3 node object on BOTH layouts: numeric fx/fy =
      // a Pinned node, null = free. A fixed layout seeds the STORED
      // coordinates; the live layout leaves x/y undefined so forceSimulation
      // places every node on its phyllotaxis spiral.
      const simNodes = nodes.map((n) => ({
        id: n.id,
        x: isFixed ? n.x : undefined,
        y: isFixed ? n.y : undefined,
        fx: null,
        fy: null,
      }));
      const simById = new Map(simNodes.map((s) => [s.id, s]));

      // Live layout (ADR-011): d3 owns the positions and ticks them into
      // graphology, which is what Sigma redraws from; it cools to a stop on
      // d3's default alphaDecay. NOT on a fixed layout: an Embedding map's
      // coordinates come from the stored UMAP projection, and a force pass
      // would drift every point off the position it was clustered at.
      const REPEL_SCALE = 30;   // pulse's repel slider -> d3 manyBody strength (8 -> -240)
      const forces = isFixed ? null : payload.controls.forces;
      // The simulation owns the VISIBLE nodes only, so a hidden node is absent
      // from the first tick on. Links join two visible nodes and are rebuilt
      // as fresh ids every time: forceLink swaps them for node objects, and a
      // reused link would keep pointing at a hidden node. A self-loop has zero
      // length — a division by zero inside forceLink.
      const visibleSimNodes = () => simNodes.filter((s) => isVisible(s.id));
      const visibleLinks = () => drawnEdges
        .filter((e) => e.source !== e.target && isVisible(e.source) && isVisible(e.target))
        .map((e) => ({ source: e.source, target: e.target }));
      let sim = null;
      if (!isFixed) {
        sim = forceSimulation(visibleSimNodes())
          .force("centre", forceCenter(0, 0).strength(forces.centre))
          // Gravity: one forceX/forceY pair pulling every node gently to the
          // origin — a compact layout, edge-less nodes kept near the stars.
          .force("gravityX", forceX(0).strength(forces.gravity))
          .force("gravityY", forceY(0).strength(forces.gravity))
          .force("repel", forceManyBody().strength(-forces.repel * REPEL_SCALE))
          .force("link", forceLink(visibleLinks()).id((s) => s.id)
            .strength(forces.link).distance(forces.linkDistance));
      }
      // Every reheat (a force slider, an unpin, Unpin all, Resume) goes
      // through here, so Pause holds until the operator resumes.
      let paused = false;
      function reheat() {
        if (sim && !paused) sim.alpha(0.5).restart();
      }

      // Build the graphology graph. Multi + directed so parallel edges and
      // self-loops don't throw, and edges can carry arrowheads.
      const graph = new Graph({ type: "directed", multi: true });
      for (const n of nodes) {
        const s = simById.get(n.id);
        const shown = isVisible(n.id);
        graph.addNode(n.id, {
          label: n.label ?? "",      // labels in-view show ONLY the name (a map ships none)
          nodeType: n.type,          // (Sigma reserves "type" for the program)
          color: colourOf(n),
          size: n.size,              // by role, resolved in Python
          // Sigma skips a hidden node and every edge touching it.
          hidden: !shown,
          // Sigma throws on a non-numeric x/y: the live layout's start comes
          // from d3 (placed synchronously above), a map's is the stored one.
          // It also fits its camera to EVERY node, hidden ones included, so a
          // hidden node is parked at the origin (forceCenter's centre).
          x: shown ? s.x : 0,
          y: shown ? s.y : 0,
        });
      }
      for (const e of drawnEdges) {
        graph.addEdge(e.source, e.target, {
          label: e.type, type: "arrow", size: 1.2, color: "#c2c8d2",
          relType: e.type, meta: e.meta || {},   // carried for the edge hover card
        });
      }

      // Interaction state, applied via reducers. `selection` is a Set of node
      // ids: what a drag moves together (pinning is separate — the fx/fy above).
      const state = { search: "", selection: new Set(), hovered: null, hoveredEdge: null };

      const renderer = new Sigma(graph, container, {
        renderEdgeLabels: display.edgeLabels,   // relationship labels
        enableEdgeEvents: true,        // needed for enterEdge / leaveEdge hover
        defaultEdgeType: "arrow",
        labelColor: { color: "#1f2430" },
        edgeLabelColor: { color: "#6b7280" },
        labelSize: 11,
        edgeLabelSize: 9,
        labelRenderedSizeThreshold: display.labelFade,
        labelDensity: 0.7,
        nodeReducer: (node, data) => {
          const res = Object.assign({}, data);
          res.size = data.size * display.nodeSize;
          const searching = state.search && !(data.label || "").toLowerCase().includes(state.search);
          const selected = state.selection.has(node);
          // Selected nodes get the highlighted label box (the single-click
          // look) — except a chunk in a multi-selection: a box drawn over a
          // document star would stack dozens of chunk label boxes. Its ring
          // marks it instead.
          const lone = selected && state.selection.size === 1;
          // Chunks are the most numerous nodes; hide their labels unless the
          // node is hovered or the lone selection (full name in the tooltip).
          const quietChunk = !state.search && data.nodeType === "chunk"
            && state.hovered !== node && !lone;
          if (searching) { res.color = "#d5d8de"; res.label = ""; }
          else if (quietChunk) { res.label = ""; }
          if (selected) res.zIndex = 2;
          if (lone || (selected && data.nodeType !== "chunk")) res.highlighted = true;
          return res;
        },
        edgeReducer: (edge, data) => {
          const res = Object.assign({}, data);
          res.size = data.size * display.linkThickness;
          // Sigma 3 registers both edge programs by default.
          res.type = display.arrows ? "arrow" : "line";
          if (state.search) res.color = "#e6e8ec";   // dim edges while searching
          return res;
        },
      });
      document.body.dataset.layout = isFixed ? "fixed" : "live";
      // One auto-fit after a Documents REVEAL (ADR-011 §4/§7): armed by
      // applyDocumentLimit, run when the reheated layout settles, cancelled by
      // a drag, a marquee or a camera pan (the user is placing things).
      let fitOnSettle = false;

      // Headless evidence + debugging only: "running" while d3 ticks (again
      // after a reheat), "settled" once it has cooled to a stop.
      if (sim) {
        sim.on("tick", () => {
          for (const s of sim.nodes()) graph.mergeNodeAttributes(s.id, { x: s.x, y: s.y });
          if (document.body.dataset.sim !== "running") document.body.dataset.sim = "running";
        });
        sim.on("end", () => {
          document.body.dataset.sim = "settled";
          if (fitOnSettle) { fitOnSettle = false; autoFit(); }
        });
      }

      // A drag or a marquee ends in a trailing click: Sigma does not count a
      // move whose default we prevented, so it emits `click` on whatever lies
      // under the release — node or stage. That click must not change the
      // selection. Reset on every press (a release off-canvas fires no click).
      let pointerMoved = false;

      // --- Selection: click selects one node, Shift+click toggles it, a
      //     click on the empty stage or Esc clears. Details show on hover. ---
      // ONE click path per target, shared with the double-click handlers below.
      function clickOnNode(node, shiftKey) {
        if (shiftKey) { if (!state.selection.delete(node)) state.selection.add(node); }
        else state.selection = new Set([node]);
        renderer.refresh();
      }
      // A Shift+click that misses a node keeps the selection being built.
      function clickOnStage(shiftKey) {
        if (shiftKey) return;
        state.selection.clear();
        renderer.refresh();
      }
      renderer.on("clickNode", ({ node, event }) => {
        if (pointerMoved) return;
        clickOnNode(node, event.original.shiftKey);
      });
      renderer.on("clickStage", ({ event }) => {
        if (pointerMoved) return;
        clickOnStage(event.original.shiftKey);
      });
      // Esc mid-marquee cancels the box and keeps the selection. The button is
      // still held, so the box is only MARKED cancelled: its moves stay
      // swallowed (no pan) until the release.
      document.addEventListener("keydown", (e) => {
        if (e.key !== "Escape") return;
        if (marquee) { marquee.cancelled = true; drawOverlay(); return; }
        if (!state.selection.size) return;
        state.selection.clear();
        renderer.refresh();
      });

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
        let card;
        if (isFixed) {
          // A map node ships no meta.document (it IS the name): put the row
          // back in its old slot, after `cluster`, so the card reads as before.
          const { cluster, ...rest } = n.meta || {};
          card = Object.assign({ type: n.type }, cluster === undefined ? {} : { cluster },
            { document: n.name }, rest);
        } else {
          card = Object.assign({ type: n.type }, n.meta);
        }
        // A parent whose children were not pulled in says how many exist.
        if (n.childCount != null) card["child chunks"] = n.childCount + " (not shown)";
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
      // Fit is the ONE place that hands the viewport back to Sigma (see freezeViewport).
      // fitView() is the ONE place that hands the viewport back to Sigma (see
      // freezeViewport). refresh() re-indexes the extent from the CURRENT
      // positions: a settled simulation no longer ticks, so Sigma would fit
      // to a stale one. Returns the camera animation's promise.
      function fitView() {
        renderer.setCustomBBox(null);
        renderer.refresh();
        return camera.animatedReset();
      }
      document.getElementById("zoom-fit").onclick = () => { fitView(); };

      // Redraw when the window resizes (Sigma tracks the container; this keeps
      // the standalone-file view crisp as the window grows/shrinks).
      window.addEventListener("resize", () => renderer.refresh());

      // --- Drag, group drag and the marquee (ADR-011 §4) ---
      // A press becomes a gesture only once the pointer travels DRAG_THRESHOLD
      // px, so a plain click neither pins, reheats nor freezes anything. The
      // first gesture freezes the viewport and it STAYS frozen: handing it
      // back to Sigma on release re-fits the extent and the dropped node lands
      // 60-100 px away from the cursor. Only the fit button releases it.
      const DRAG_THRESHOLD = 3;   // px — Sigma's own draggedEventsTolerance
      function freezeViewport() {
        if (!renderer.getCustomBBox()) renderer.setCustomBBox(renderer.getBBox());
      }

      // The drag set of one press. HELD = the selection when the pressed node
      // is in it, else the pressed node alone (dragging never changes the
      // selection); held nodes pin on drop. CARRIED = every held node's direct
      // unpinned `part_of` children — the sources of its in-edges (child chunk
      // -> parent chunk -> document, one level) — moved rigidly by the same
      // delta and released on drop. A user-pinned or a hidden node is never carried.
      function dragSetFor(nodeId) {
        const held = state.selection.has(nodeId) ? new Set(state.selection) : new Set([nodeId]);
        const carried = new Set();
        for (const id of held) {
          graph.forEachInEdge(id, (edge, attrs, source) => {
            if (attrs.relType === "part_of" && !held.has(source)
              && simById.get(source).fx == null
              && !graph.getNodeAttribute(source, "hidden")) carried.add(source);
          });
        }
        return { held, carried };
      }

      let press = null;     // { node, x, y }: a node press, until mouseup
      let drag = null;      // { carried, members, anchor }: once that press moved
      let marquee = null;   // { x0, y0, x1, y1 } in viewport px: a Shift+press on the stage
      let pan = null;       // { x, y }: a plain stage press — Sigma pans the camera
      // Left button only: Sigma reports a press for EVERY button but its
      // captor's mouseup only after a left one, so a right-click press would
      // stay open and the node would follow the bare pointer.
      renderer.on("downNode", (e) => {
        if (e.event.original.button !== 0) return;
        pointerMoved = false;
        press = { node: e.node, x: e.event.x, y: e.event.y };
      });
      function startMarquee(e) {
        if (e.event.original.button !== 0) return;
        pointerMoved = false;
        if (!e.event.original.shiftKey) { pan = { x: e.event.x, y: e.event.y }; return; }
        marquee = { x0: e.event.x, y0: e.event.y, x1: e.event.x, y1: e.event.y };
      }
      renderer.on("downStage", startMarquee);
      renderer.on("downEdge", startMarquee);       // a press on an edge line is a stage press too

      // d3's idiom (pulse): every member is held with fx/fy while
      // alphaTarget(0.3) keeps the simulation hot, so the set cannot be pulled
      // apart and its unpinned neighbours follow live. The map has no
      // simulation, so there only the drag set moves.
      function startDrag() {
        freezeViewport();
        const { held, carried } = dragSetFor(press.node);
        const members = [...held, ...carried].map((id) => {
          const s = simById.get(id);
          const at = graph.getNodeAttributes(id);
          s.fx = at.x;
          s.fy = at.y;
          return { s, x0: at.x, y0: at.y, carried: carried.has(id) };
        });
        drag = { carried, members, anchor: renderer.viewportToGraph({ x: press.x, y: press.y }) };
        if (sim && !paused) sim.alphaTarget(0.3).restart();   // paused: only the drag set moves
      }

      const captor = renderer.getMouseCaptor();
      captor.on("mousemovebody", (e) => {
        // A plain stage drag is Sigma's camera pan: never prevented, but once
        // it really moves the user owns the camera — an armed auto-fit is off.
        if (pan) {
          if (e.original.buttons === 0) pan = null;
          else if (Math.hypot(e.x - pan.x, e.y - pan.y) >= DRAG_THRESHOLD) { fitOnSettle = false; pan = null; }
          return;
        }
        if (!press && !marquee) return;
        e.preventSigmaDefault();          // no camera pan under a held node or a marquee
        e.original.preventDefault();
        e.original.stopPropagation();
        // A lost mouseup (focus loss, an OS dialog): the button is no longer
        // held, so release sigma's captor — its stuck press would pan the
        // camera under the bare pointer — which emits the mouseup below and
        // ends the gesture where it was last held.
        if (e.original.buttons === 0) { captor.handleUp(e.original); return; }
        if (marquee && marquee.cancelled) return;
        const from = press || { x: marquee.x0, y: marquee.y0 };
        if (!pointerMoved && Math.hypot(e.x - from.x, e.y - from.y) < DRAG_THRESHOLD) return;
        if (!pointerMoved) {
          pointerMoved = true;
          fitOnSettle = false;            // a gesture cancels an armed auto-fit
          if (press) startDrag(); else freezeViewport();
        }
        if (marquee) {
          marquee.x1 = e.x;
          marquee.y1 = e.y;
          drawOverlay();                  // the camera is still, so Sigma renders nothing
          return;
        }
        // Every member moves by the pointer's delta: relative offsets hold.
        const pos = renderer.viewportToGraph(e);
        const dx = pos.x - drag.anchor.x, dy = pos.y - drag.anchor.y;
        for (const m of drag.members) {
          m.s.fx = m.s.x = m.x0 + dx;
          m.s.fy = m.s.y = m.y0 + dy;
          graph.mergeNodeAttributes(m.s.id, { x: m.s.x, y: m.s.y });
        }
      });

      // REPLACE semantics: the box IS the new selection (Shift only starts it).
      function selectInside(box) {
        const left = Math.min(box.x0, box.x1), right = Math.max(box.x0, box.x1);
        const top = Math.min(box.y0, box.y1), bottom = Math.max(box.y0, box.y1);
        const inside = new Set();
        graph.forEachNode((id, a) => {
          const shown = renderer.getNodeDisplayData(id);
          if (!shown || shown.hidden) return;
          const vp = renderer.graphToViewport({ x: a.x, y: a.y });
          if (vp.x >= left && vp.x <= right && vp.y >= top && vp.y <= bottom) inside.add(id);
        });
        state.selection = inside;
        renderer.refresh();
      }

      captor.on("mouseup", () => {
        if (marquee) {
          const box = marquee;
          marquee = null;
          if (pointerMoved && !box.cancelled) selectInside(box); else drawOverlay();
        }
        if (drag) {
          // HELD nodes keep fx/fy — Pinned nodes; the CARRIED children are
          // released to keep settling (on the map they stay where they were left).
          for (const m of drag.members) if (m.carried) { m.s.fx = null; m.s.fy = null; }
          if (sim) sim.alphaTarget(0);
          drag = null;
          drawOverlay();                  // the map re-renders nothing by itself
        }
        press = null;
        pan = null;
      });

      // --- Double clicks. Sigma reads ANY two clicks <300 ms apart as a
      //     double-click (a drag's trailing click counts as the first) and
      //     emits no click for the second one. So its zoom never runs and the
      //     second click acts as a click — except a plain one on a Pinned
      //     node, which unpins it. ---
      renderer.on("doubleClickNode", (e) => {
        e.preventSigmaDefault();
        if (pointerMoved) return;            // the second click IS a drag's trailing click
        const shiftKey = e.event.original.shiftKey;
        const s = simById.get(e.node);
        if (shiftKey || !s || s.fx == null) { clickOnNode(e.node, shiftKey); return; }
        s.fx = null;
        s.fy = null;
        reheat();                            // drift back into the layout
        drawOverlay();                       // the map re-renders nothing by itself
      });
      renderer.on("doubleClickStage", (e) => {
        e.preventSigmaDefault();
        if (pointerMoved) return;
        clickOnStage(e.event.original.shiftKey);
      });

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
        const colorByType = new Map(nodes.map((n) => [n.type, colourOf(n)]));
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

      // --- Cluster hulls (Embedding map only): membership is fixed once,
      //     positions are not — each hull is rebuilt from the CURRENT graph
      //     coordinates on every draw, so it follows a dragged point. ---
      const checkbox = document.getElementById("hulls");
      const byCluster = new Map();
      if (hullsEnabled) {
        const toggle = document.getElementById("hulls-toggle");
        toggle.hidden = false;
        checkbox.checked = payload.hulls;
        checkbox.onchange = drawOverlay;
        for (const n of nodes) {
          // Noise (-1) is a residue, not a group: it never gets a hull.
          if (typeof n.cluster_id !== "number" || n.cluster_id < 0) continue;
          let entry = byCluster.get(n.cluster_id);
          if (!entry) { entry = { color: colourOf(n), ids: [] }; byCluster.set(n.cluster_id, entry); }
          entry.ids.push(n.id);
        }
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
      function rgba(hex, alpha) {
        const m = /^#?([0-9a-f]{6})$/i.exec(String(hex));
        if (!m) return hex;
        const v = parseInt(m[1], 16);
        return "rgba(" + ((v >> 16) & 255) + "," + ((v >> 8) & 255) + "," + (v & 255) + "," + alpha + ")";
      }

      // --- The overlay: everything Sigma cannot draw (ADR-011 §5) ---
      // ONE 2D canvas on top of Sigma's (pointer-events: none), redrawn in
      // VIEWPORT coordinates on every render so it stays glued to the nodes
      // through pan, zoom, drag and simulation ticks: hulls, pin dots,
      // selection rings, then the marquee.
      const overlay = document.getElementById("overlay");
      const overlayCtx = overlay.getContext("2d");
      function drawOverlay() {
        const dpr = window.devicePixelRatio || 1;
        const width = container.offsetWidth, height = container.offsetHeight;
        overlay.width = Math.round(width * dpr);      // also clears the canvas
        overlay.height = Math.round(height * dpr);
        overlay.style.width = width + "px";
        overlay.style.height = height + "px";
        overlayCtx.setTransform(dpr, 0, 0, dpr, 0, 0);
        if (hullsEnabled && checkbox.checked) {
          for (const { color, ids } of byCluster.values()) {
            // Fewer than 3 points cannot bound an area — those clusters draw nothing.
            if (ids.length < 3) continue;
            const hull = convexHull(ids.map((id) => {
              const a = graph.getNodeAttributes(id);
              return [a.x, a.y];
            }));
            if (hull.length < 3) continue;
            overlayCtx.beginPath();
            hull.forEach((p, i) => {
              const vp = renderer.graphToViewport({ x: p[0], y: p[1] });
              if (i === 0) overlayCtx.moveTo(vp.x, vp.y); else overlayCtx.lineTo(vp.x, vp.y);
            });
            overlayCtx.closePath();
            overlayCtx.fillStyle = rgba(color, 0.12);
            overlayCtx.fill();
            overlayCtx.lineWidth = 1.5;
            overlayCtx.strokeStyle = rgba(color, 0.6);
            overlayCtx.stroke();
          }
        }
        // Pin dots: a dark centre disc on every Pinned node still on screen.
        // Carried children hold fx/fy only while they ride along — not pins.
        overlayCtx.fillStyle = "#1f2430";
        for (const s of simNodes) {
          if (s.fx == null || (drag && drag.carried.has(s.id))) continue;
          const shown = renderer.getNodeDisplayData(s.id);
          if (!shown || shown.hidden) continue;
          const a = graph.getNodeAttributes(s.id);
          const vp = renderer.graphToViewport({ x: a.x, y: a.y });
          overlayCtx.beginPath();
          overlayCtx.arc(vp.x, vp.y, 0.35 * renderer.scaleSize(shown.size), 0, 2 * Math.PI);
          overlayCtx.fill();
        }
        // Selection rings: a 2 px accent ring 3 px outside every selected node.
        overlayCtx.strokeStyle = "#ea580c";
        overlayCtx.lineWidth = 2;
        for (const id of state.selection) {
          const shown = renderer.getNodeDisplayData(id);
          if (!shown || shown.hidden) continue;
          const a = graph.getNodeAttributes(id);
          const vp = renderer.graphToViewport({ x: a.x, y: a.y });
          overlayCtx.beginPath();
          overlayCtx.arc(vp.x, vp.y, renderer.scaleSize(shown.size) + 3, 0, 2 * Math.PI);
          overlayCtx.stroke();
        }
        // The marquee while Shift+dragging: a faint fill, a 1 px dashed border.
        if (marquee && !marquee.cancelled) {
          const x = Math.min(marquee.x0, marquee.x1), y = Math.min(marquee.y0, marquee.y1);
          const w = Math.abs(marquee.x1 - marquee.x0), h = Math.abs(marquee.y1 - marquee.y0);
          overlayCtx.fillStyle = "rgba(234,88,12,0.08)";
          overlayCtx.fillRect(x, y, w, h);
          overlayCtx.lineWidth = 1;
          overlayCtx.setLineDash([4, 3]);
          overlayCtx.strokeRect(x + 0.5, y + 0.5, w, h);
          overlayCtx.setLineDash([]);
        }
      }
      // --- Controls panel (ADR-011 §6): every row starts from payload.controls
      //     — the template has no default of its own. A section is a titled
      //     block appended to #panel-body in build order. Force rows reheat
      //     the layout; Display rows only redraw it. ---
      document.getElementById("panel-toggle").onclick = () => { panelBody.hidden = !panelBody.hidden; };
      const resets = [];   // one per row: restore its load value and apply it
      function panelSection(title) {
        const section = document.createElement("div");
        const head = document.createElement("div");
        head.className = "panel-title";
        head.textContent = title;
        section.appendChild(head);
        panelBody.appendChild(section);
        return section;
      }
      // A labelled slider with a live readout; `apply` gets the number on
      // every move and on Reset.
      function rangeRow(section, label, min, max, step, value, decimals, apply,
        format = (v) => v.toFixed(decimals)) {
        const row = document.createElement("div");
        row.className = "panel-row";
        const head = document.createElement("div");
        head.className = "panel-head";
        const name = document.createElement("span");
        name.textContent = label;
        const readout = document.createElement("span");
        const input = document.createElement("input");
        input.type = "range";
        input.min = min;    // bounds first: a range input clamps its value to them
        input.max = max;
        input.step = step;
        const show = (v) => { input.value = v; readout.textContent = format(v); };
        show(value);
        input.oninput = () => { const v = parseFloat(input.value); show(v); apply(v); };
        resets.push(() => { show(value); apply(value); });
        head.append(name, readout);
        row.append(head, input);
        section.appendChild(row);
      }
      function checkboxRow(section, label, checked, apply) {
        const row = document.createElement("label");
        row.className = "panel-check";
        const name = document.createElement("span");
        name.textContent = label;
        const input = document.createElement("input");
        input.type = "checkbox";
        input.checked = checked;
        input.onchange = () => apply(input.checked);
        resets.push(() => { input.checked = checked; apply(checked); });
        row.append(name, input);
        section.appendChild(row);
      }
      function actionButton(row, label, onClick) {
        const button = document.createElement("button");
        button.type = "button";
        button.textContent = label;
        button.onclick = onClick;
        row.appendChild(button);
        return button;
      }

      // --- Documents (ADR-011 §7): the slider hides what the read already
      //     embedded — it never re-fetches. ---
      function showDocumentCounts() {
        let shownNodes = 0, shownEdges = 0;
        for (const n of nodes) if (isVisible(n.id)) shownNodes++;
        for (const e of drawnEdges) if (isVisible(e.source) && isVisible(e.target)) shownEdges++;
        countsEl.textContent = docLimit + " of " + documents.total + " documents · "
          + shownNodes + " nodes · " + shownEdges + " edges";
        document.body.dataset.docs = docLimit + "/" + documents.total;   // headless evidence
      }
      // Where a revealed document's nodes start. d3 would put a never-placed
      // node on its spiral around the centre, scattered through the stars
      // already there, so each newly revealed document is seeded as a small
      // spiral of its own around one point: one link distance outside the
      // layout while the camera auto-fits, or — once a gesture froze the
      // camera (it stays frozen) — on a ring inside the CURRENT viewport, so
      // the new stars land on screen. With a frozen camera a re-revealed
      // node whose last position is off screen is re-seeded too; a Pinned
      // node always keeps its spot.
      const GOLDEN_ANGLE = Math.PI * (3 - Math.sqrt(5));   // d3's phyllotaxis angle
      function seedRevealed() {
        const owned = new Set(sim.nodes());
        const frozen = renderer.getCustomBBox() != null;
        let cx = 0, cy = 0, rx, ry, inView = () => true;
        if (frozen) {
          const { width, height } = renderer.getDimensions();
          const a = renderer.viewportToGraph({ x: 0, y: 0 });
          const b = renderer.viewportToGraph({ x: width, y: height });
          const [x0, x1] = [Math.min(a.x, b.x), Math.max(a.x, b.x)];
          const [y0, y1] = [Math.min(a.y, b.y), Math.max(a.y, b.y)];
          cx = (x0 + x1) / 2; cy = (y0 + y1) / 2;
          rx = (x1 - x0) / 4; ry = (y1 - y0) / 4;   // halfway to the viewport edge
          inView = (s) => s.x >= x0 && s.x <= x1 && s.y >= y0 && s.y <= y1;
        } else {
          let radius = 0;
          for (const s of owned) radius = Math.max(radius, Math.hypot(s.x, s.y));
          rx = ry = radius + forces.linkDistance;
        }
        const byRank = new Map();
        for (const s of simNodes) {
          if (owned.has(s) || !isVisible(s.id) || s.fx != null) continue;
          if (s.x != null && inView(s)) continue;
          const rank = nodeById.get(s.id).docRank;
          if (!byRank.has(rank)) byRank.set(rank, []);
          byRank.get(rank).push(s);
        }
        for (const [rank, group] of byRank) {
          const at = rank * GOLDEN_ANGLE;
          group.forEach((s, i) => {
            const r = 10 * Math.sqrt(0.5 + i), a = i * GOLDEN_ANGLE;
            s.x = cx + rx * Math.cos(at) + r * Math.cos(a);
            s.y = cy + ry * Math.sin(at) + r * Math.sin(a);
            s.vx = 0; s.vy = 0;
          });
        }
      }
      // The reveal's auto-fit: the Fit button's path, then — if a gesture had
      // frozen the camera — freeze it again on the new extent once the camera
      // has animated, so later drags still never refit. Only the camera moves.
      function autoFit() {
        const wasFrozen = renderer.getCustomBBox() != null;
        fitView().then(() => { if (wasFrozen) freezeViewport(); });
      }
      function applyDocumentLimit(n) {
        const revealing = n > docLimit;
        docLimit = n;
        // A hidden node leaves the selection and the hover; its pin stays.
        for (const id of [...state.selection]) if (!isVisible(id)) state.selection.delete(id);
        if (state.hovered && !isVisible(state.hovered)) {
          state.hovered = null;
          tooltip.classList.remove("show");
        }
        if (state.hoveredEdge && !graph.extremities(state.hoveredEdge).every(isVisible)) {
          state.hoveredEdge = null;
          tooltip.classList.remove("show");
        }
        // Live layout: nodes BEFORE links — d3 throws on a link to a node it
        // does not own. A revealed node never shown before is placed here.
        if (sim) {
          seedRevealed();
          sim.nodes(visibleSimNodes());
          sim.force("link").links(visibleLinks());
        }
        // One graph event for the whole pass. A shown node takes its d3
        // position back (a pinned one: where it was pinned); a hidden one is
        // parked at the origin so the camera fits only what is drawn.
        graph.updateEachNodeAttributes((id, attrs) => {
          const s = simById.get(id), visible = isVisible(id);
          return Object.assign(attrs, { hidden: !visible, x: visible ? s.x : 0, y: visible ? s.y : 0 });
        });
        reheat();                            // paused: the sim stays stopped
        showDocumentCounts();
        renderer.refresh();
        // Hiding never fits. A reveal fits once the layout settles — at once
        // when there is no running simulation to wait for (paused, or a map).
        // Paused, it stays armed too: Resume expands the layout past that
        // first fit, so the settle after Resume fits once more.
        if (revealing) {
          if (sim) fitOnSettle = true;
          if (!sim || paused) autoFit();
        }
      }
      // Documents — any ranked payload (the gate: controls.documents), FIRST.
      if (documents) {
        rangeRow(panelSection("Documents"), ORDER_LABEL[documents.order], 1, documents.total, 1, documents.shown, 0,
          applyDocumentLimit, (v) => v + " of " + documents.total);
        showDocumentCounts();                // the boot already hid the rest
      }

      // Forces — a live layout only (the Embedding map ships no forces).
      if (forces) {
        const section = panelSection("Forces");
        rangeRow(section, "Centre force", 0, 1, 0.01, forces.centre, 2,
          (v) => { sim.force("centre").strength(v); reheat(); });
        rangeRow(section, "Gravity", 0, 0.5, 0.01, forces.gravity, 2,
          (v) => { sim.force("gravityX").strength(v); sim.force("gravityY").strength(v); reheat(); });
        rangeRow(section, "Repel force", 0, 20, 0.01, forces.repel, 2,
          (v) => { sim.force("repel").strength(-v * REPEL_SCALE); reheat(); });
        rangeRow(section, "Link force", 0, 1, 0.01, forces.link, 2,
          (v) => { sim.force("link").strength(v); reheat(); });
        rangeRow(section, "Link distance", 0, 500, 1, forces.linkDistance, 0,
          (v) => { sim.force("link").distance(v); reheat(); });
      }

      // Display — every payload; redraw only, never reheat.
      const displaySection = panelSection("Display");
      rangeRow(displaySection, "Node size", 0.2, 3, 0.05, display.nodeSize, 2,
        (v) => { display.nodeSize = v; renderer.refresh(); });
      rangeRow(displaySection, "Link thickness", 0.2, 3, 0.05, display.linkThickness, 2,
        (v) => { display.linkThickness = v; renderer.refresh(); });
      rangeRow(displaySection, "Label fade", 0, 12, 0.5, display.labelFade, 1,
        (v) => renderer.setSetting("labelRenderedSizeThreshold", v));
      checkboxRow(displaySection, "Arrows", display.arrows,
        (on) => { display.arrows = on; renderer.refresh(); });
      checkboxRow(displaySection, "Edge labels", display.edgeLabels,
        (on) => renderer.setSetting("renderEdgeLabels", on));

      // Actions. Reset restores every row (the force rows' reheats land in
      // one synchronous burst = one reheat); it neither unpins nor resumes.
      const actionRow = document.createElement("div");
      actionRow.className = "panel-buttons";
      panelSection("Actions").appendChild(actionRow);
      if (forces) {
        const pause = actionButton(actionRow, "Pause", () => {
          paused = !paused;
          pause.textContent = paused ? "Resume" : "Pause";
          // Resume REHEATS: a bare restart ticks once on a cooled layout.
          if (paused) { sim.stop(); document.body.dataset.sim = "paused"; }
          else reheat();
        });
      }
      actionButton(actionRow, "Unpin all", () => {
        for (const s of simNodes) { s.fx = null; s.fy = null; }
        drawOverlay();                       // the map re-renders nothing by itself
        reheat();
      });
      actionButton(actionRow, "Reset to defaults", () => {
        for (const reset of resets) reset();
      });

      renderer.on("afterRender", drawOverlay);   // pan / zoom / drag / tick / refresh
      renderer.on("resize", drawOverlay);        // container size changed
      drawOverlay();
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
    import { forceSimulation, forceLink, forceManyBody, forceCenter, forceX, forceY } from "__D3_FORCE_CDN__";

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
        .replace("__D3_FORCE_CDN__", _D3_FORCE_CDN)
    )


# Everything but the per-call ``__DATA__`` substitution is resolved once here.
_FILE_HTML_BASE = _resolve_static(_FILE_HTML_TEMPLATE, "100vh")
