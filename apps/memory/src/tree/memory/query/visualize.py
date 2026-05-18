"""
Visualize a QueryResult (or the full knowledge_graph) as an interactive HTML graph.

Uses networkx for the graph model and pyvis for rendering.
"""

import logging
import webbrowser
from pathlib import Path

import networkx as nx
from pyvis.network import Network

from tree.memory.types import QueryResult

logger = logging.getLogger(__name__)

# Colour palette per node type.
_NODE_COLOURS: dict[str, str] = {
    "person": "#e74c3c",
    "document": "#3498db",
    "chunk": "#95a5a6",
    "task": "#2ecc71",
    "episode": "#f39c12",
    "preference": "#9b59b6",
}

_PYVIS_OPTIONS = """\
{
  "interaction": {
    "hover": true,
    "tooltipDelay": 100,
    "navigationButtons": true
  },
  "nodes": {
    "font": {"size": 12, "multi": "html"},
    "shape": "dot",
    "size": 16
  },
  "edges": {
    "font": {"size": 10, "align": "middle"},
    "arrows": {"to": {"enabled": true, "scaleFactor": 0.8}},
    "smooth": {"type": "curvedCW", "roundness": 0.2}
  },
  "physics": {
    "forceAtlas2Based": {
      "gravitationalConstant": -50,
      "centralGravity": 0.01,
      "springLength": 200,
      "springConstant": 0.05,
      "damping": 0.4
    },
    "solver": "forceAtlas2Based",
    "stabilization": {"iterations": 150}
  }
}
"""


def build_networkx_graph(result: QueryResult) -> nx.DiGraph:
    """Convert a QueryResult into a networkx DiGraph."""

    G = nx.DiGraph()

    for node in result.nodes:
        node_id = str(node["_id"])
        node_type = node.get("type", "unknown")
        props = node.get("properties", {})

        name = _extract_display_name(node_id, node_type, props)
        label_parts = [_truncate(name, 40), f"[{node_type}]"]

        hover_lines = [f"id: {node_id}", f"type: {node_type}"]
        for k, v in props.items():
            if k == "content":
                v = _truncate(str(v), 200)
            elif isinstance(v, list):
                v = ", ".join(str(x) for x in v)
            hover_lines.append(f"{k}: {v}")

        G.add_node(
            node_id,
            label="\n".join(label_parts),
            title="\n".join(hover_lines),
            group=node_type,
            color=_NODE_COLOURS.get(node_type, "#7f8c8d"),
        )

    for edge in result.edges:
        edge_type = edge.get("type", "")
        src = str(edge.get("source_node_id", ""))
        tgt = str(edge.get("target_node_id", ""))

        # Ensure endpoints exist (may be missing if query returned partial graph).
        if src not in G:
            G.add_node(
                src,
                label=_truncate(_extract_display_name(src, "unknown", {}), 40),
                title=src,
                group="unknown",
            )
        if tgt not in G:
            G.add_node(
                tgt,
                label=_truncate(_extract_display_name(tgt, "unknown", {}), 40),
                title=tgt,
                group="unknown",
            )

        G.add_edge(src, tgt, label=edge_type, title=f"type: {edge_type}")

    return G


def render_html(
    G: nx.DiGraph,
    output: str | Path = "knowledge_graph.html",
    *,
    open_browser: bool = True,
) -> Path:
    """Render a networkx DiGraph as an interactive HTML file via pyvis."""

    net = Network(
        height="900px",
        width="100%",
        directed=True,
        bgcolor="#1a1a2e",
        font_color="white",
        cdn_resources="in_line",
    )
    net.from_nx(G)
    net.set_options(_PYVIS_OPTIONS)

    output_path = Path(output).resolve()
    net.save_graph(str(output_path))
    logger.info(
        "Graph saved to %s (%d nodes, %d edges)",
        output_path,
        G.number_of_nodes(),
        G.number_of_edges(),
    )

    if open_browser:
        webbrowser.open(f"file://{output_path}")

    return output_path


def visualize_query_result(
    result: QueryResult,
    output: str | Path = "knowledge_graph.html",
    *,
    open_browser: bool = True,
) -> Path:
    """One-shot: QueryResult → networkx → HTML file."""

    G = build_networkx_graph(result)
    return render_html(G, output, open_browser=open_browser)


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
