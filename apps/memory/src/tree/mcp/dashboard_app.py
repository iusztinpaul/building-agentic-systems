"""MCP App dashboards — three FastMCP app paradigms over the same data.

A companion to the custom-HTML Sigma graph view (``graph_app``). Where the
graph view shows topology, these show a **dashboard**: a bar chart counting
each node type + a searchable table of the returned nodes. The point is to
compare, in practice, the three higher-level FastMCP app styles:

1. ``memory_dashboard``      — Prefab "interactive tool": ``@mcp.tool(app=True)``
   returning a ``PrefabApp`` (declarative Python components, client-side
   Pyodide render). https://gofastmcp.com/apps/prefab
2. ``memory_dashboard_app``  — same UI, built via a ``FastMCPApp`` provider and
   its ``@app.ui()`` entry point. https://gofastmcp.com/apps/fastmcp-app
3. Generative UI             — ``GenerativeUI`` provider registers
   ``generate_prefab_ui`` + ``search_prefab_components`` so the model can author
   Prefab UIs at runtime. https://gofastmcp.com/apps/generative

All three need the ``fastmcp[apps]`` extra (``prefab-ui``); the import is guarded
at the call site (``tools.py``) so a missing extra degrades gracefully rather
than taking down the custom-HTML graph tool.

Generative UI also needs **Deno** at runtime (auto-installs on first use) for
server-side validation of the generated code.
"""

import logging
from collections import Counter
from typing import Any

from fastmcp import Context, FastMCPApp
from fastmcp.apps.generative import GenerativeUI
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


def build_dashboard(payload: dict[str, list[dict[str, Any]]], query: str) -> PrefabApp:
    """Compose the dashboard: KPI metrics + node-type bar chart + node table."""

    nodes, edges = payload["nodes"], payload["edges"]
    counts = node_type_counts(nodes)
    rows = [{"name": n["name"], "type": n["type"]} for n in nodes]

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
                DataTable(
                    columns=[
                        DataTableColumn(key="name", header="Name", sortable=True),
                        DataTableColumn(key="type", header="Type", sortable=True),
                    ],
                    rows=rows,
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
# 1. Prefab interactive tool — @mcp.tool(app=True) returning a PrefabApp.
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


# ---------------------------------------------------------------------------
# 2. FastMCPApp provider — same UI via an @app.ui() entry point.
# ---------------------------------------------------------------------------
dashboard_provider = FastMCPApp("Tree Memory Dashboard")


@dashboard_provider.ui()
async def memory_dashboard_app(
    query: str,
    ctx: Context,
    top_k: int = 20,
    max_hops: int = 2,
) -> PrefabApp:
    """Open the Tree Memory dashboard (FastMCPApp variant of memory_dashboard).

    Same node-type bar chart + node table, built through the FastMCPApp
    provider pattern instead of a bare ``@mcp.tool(app=True)``.

    Args:
        query: Search query text — seeds which nodes the dashboard covers.
        top_k: Number of seed nodes to retrieve (default 20).
        max_hops: Hops of graph expansion around the seeds (default 2).
    """

    payload = await _fetch_payload(ctx, query, top_k, max_hops)
    return build_dashboard(payload, query)


def register_providers() -> None:
    """Mount the FastMCPApp dashboard and the Generative UI provider on ``mcp``.

    Generative UI registers ``generate_prefab_ui`` + ``search_prefab_components``
    so the model can author Prefab UIs at runtime (needs Deno at runtime).
    """

    mcp.add_provider(dashboard_provider)
    mcp.add_provider(GenerativeUI())


register_providers()
