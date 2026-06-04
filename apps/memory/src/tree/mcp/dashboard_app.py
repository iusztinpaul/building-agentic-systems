"""MCP App dashboard — a Prefab interactive tool over the knowledge graph.

A companion to the custom-HTML Sigma graph view (``graph_app``). Where the
graph view shows topology, this shows a **dashboard**: a bar chart counting
each node type + searchable/sortable tables of the returned nodes and
relationships, each row carrying the curated metadata from ``_curated_meta``.

``memory_dashboard`` is a Prefab "interactive tool": ``@mcp.tool(app=True)``
returning a ``PrefabApp`` (declarative Python components, client-side Pyodide
render). https://gofastmcp.com/apps/prefab — the right paradigm here because
the dashboard is read-only: the server renders once, and all interactivity
(sort / search / paginate) is client-side state with no backend round-trip.

This needs the ``fastmcp[apps]`` extra (``prefab-ui``); the import is guarded
at the call site (``tools.py``) so a missing extra degrades gracefully rather
than taking down the custom-HTML graph tool.
"""

import logging
from collections import Counter
from typing import Any

from fastmcp import Context
from fastmcp.tools import ToolResult
from prefab_ui import PrefabApp
from prefab_ui.components import (
    Column,
    DataTable,
    DataTableColumn,
    Heading,
    Metric,
    Row,
    Separator,
    Text,
)
from prefab_ui.components.charts import BarChart, ChartSeries

from tree.mcp.graph_app import to_graph_payload
from tree.mcp.server import mcp
from tree.memory.query.core import query_memory as structured_query_memory

logger = logging.getLogger(__name__)


async def _fetch_payload(
    ctx: Context, query: str, top_k: int, max_hops: int
) -> dict[str, list[dict[str, Any]]]:
    """Run the structured query and flatten it to a {nodes, edges} payload."""

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
    return to_graph_payload(result)


def node_type_counts(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Count nodes per type, descending — the bar-chart series data."""

    counts = Counter(n["type"] for n in nodes)
    return [
        {"type": t, "count": c}
        for t, c in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    ]


def _node_rows(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Flatten each node + its curated ``meta`` into a flat DataTable row."""

    rows: list[dict[str, Any]] = []
    for n in nodes:
        meta = n.get("meta") or {}
        rows.append(
            {
                "name": n["name"],
                "type": n["type"],
                "subtype": meta.get("subtype", ""),
                "confidence": meta.get("confidence"),
                "description": meta.get("description", ""),
                "aliases": meta.get("aliases", ""),
            }
        )
    return rows


def _edge_rows(
    edges: list[dict[str, Any]], name_by_id: dict[str, str]
) -> list[dict[str, Any]]:
    """Flatten each edge + its curated ``meta`` into a flat DataTable row.

    Endpoints are shown by display name (falling back to the raw id for
    endpoints not present among the returned nodes).
    """

    rows: list[dict[str, Any]] = []
    for e in edges:
        meta = e.get("meta") or {}
        rows.append(
            {
                "source": name_by_id.get(e["source"], e["source"]),
                "type": e["type"],
                "target": name_by_id.get(e["target"], e["target"]),
                "semantic_type": meta.get("semantic_type", ""),
                "confidence": meta.get("confidence"),
                "description": meta.get("description", ""),
            }
        )
    return rows


def build_dashboard(payload: dict[str, list[dict[str, Any]]], query: str) -> PrefabApp:
    """Compose the dashboard: KPI metrics + node-type bar chart + node/edge tables."""

    nodes, edges = payload["nodes"], payload["edges"]
    counts = node_type_counts(nodes)
    name_by_id = {n["id"]: n["name"] for n in nodes if n.get("id")}

    with PrefabApp(title="Tree Memory Dashboard", mode="light") as app:
        with Column(gap=4, css_class="p-6"):
            Heading(f"Knowledge graph — {query}")
            if not nodes:
                Text("No results for this query.")
            else:
                with Row(gap=6):
                    Metric(label="Nodes", value=str(len(nodes)))
                    Metric(label="Edges", value=str(len(edges)))
                    Metric(label="Types", value=str(len(counts)))
                BarChart(
                    data=counts,
                    series=[ChartSeries(dataKey="count", label="Nodes")],
                    xAxis="type",
                    height=280,
                )
                Separator()
                Heading("Nodes")
                DataTable(
                    columns=[
                        DataTableColumn(key="name", header="Name", sortable=True),
                        DataTableColumn(key="type", header="Type", sortable=True),
                        DataTableColumn(key="subtype", header="Subtype", sortable=True),
                        DataTableColumn(
                            key="confidence",
                            header="Confidence",
                            sortable=True,
                            format="number:2",
                            align="right",
                        ),
                        DataTableColumn(key="description", header="Description"),
                        DataTableColumn(key="aliases", header="Aliases"),
                    ],
                    rows=_node_rows(nodes),
                    search=True,
                    paginated=True,
                    pageSize=10,
                )
                if edges:
                    Separator()
                    Heading("Relationships")
                    DataTable(
                        columns=[
                            DataTableColumn(
                                key="source", header="Source", sortable=True
                            ),
                            DataTableColumn(key="type", header="Type", sortable=True),
                            DataTableColumn(
                                key="target", header="Target", sortable=True
                            ),
                            DataTableColumn(
                                key="semantic_type", header="Semantic", sortable=True
                            ),
                            DataTableColumn(
                                key="confidence",
                                header="Confidence",
                                sortable=True,
                                format="number:2",
                                align="right",
                            ),
                            DataTableColumn(key="description", header="Description"),
                        ],
                        rows=_edge_rows(edges, name_by_id),
                        search=True,
                        paginated=True,
                        pageSize=10,
                    )
    return app


def _summary(payload: dict[str, list[dict[str, Any]]], query: str) -> str:
    """One-line model-facing summary (so non-UI clients still get context)."""

    counts = Counter(n["type"] for n in payload["nodes"])
    breakdown = ", ".join(f"{t}:{c}" for t, c in counts.most_common())
    return (
        f"Dashboard for {query!r}: {len(payload['nodes'])} nodes, "
        f"{len(payload['edges'])} edges across {len(counts)} types"
        + (f" ({breakdown})." if breakdown else ".")
    )


# ---------------------------------------------------------------------------
# Prefab interactive tool — @mcp.tool(app=True) returning a PrefabApp.
# ---------------------------------------------------------------------------
@mcp.tool(app=True)
async def memory_dashboard(
    query: str,
    ctx: Context,
    top_k: int = 20,
    max_hops: int = 2,
) -> ToolResult:
    """Dashboard of the knowledge graph: node-type bar chart + node table.

    Interactive Prefab UI (sortable/searchable table, hover-able chart). Use
    when the user wants a *summary* of what's in memory rather than the graph
    topology (for topology use ``visualize_memory_graph``).

    Args:
        query: Search query text — seeds which nodes the dashboard covers.
        top_k: Number of seed nodes to retrieve (default 20).
        max_hops: Hops of graph expansion around the seeds (default 2).
    """

    payload = await _fetch_payload(ctx, query, top_k, max_hops)
    return ToolResult(
        content=_summary(payload, query),
        structured_content=build_dashboard(payload, query),
    )
