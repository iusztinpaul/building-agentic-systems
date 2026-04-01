"""MCP tool handlers — thin delegation to business logic."""

import logging
from typing import Any

from bson import json_util
from fastmcp import Context

from twin.mcp.server import mcp
from twin.memory.query.core import query_memory as structured_query_memory
from twin.memory.query.nl_query import execute_nl_query
from twin.memory.query.visualize import build_networkx_graph, render_html
from twin.memory.types import QueryResult

logger = logging.getLogger(__name__)


def _serialize(docs: list[dict[str, Any]]) -> str:
    """Serialize MongoDB documents to JSON, stripping embedding fields."""

    cleaned = [{k: v for k, v in doc.items() if k != "embedding"} for doc in docs]
    return json_util.dumps(cleaned, indent=2)


def _visualize(docs: list[dict[str, Any]]) -> str:
    """Render docs as an interactive HTML graph and return the file path."""

    nodes = [d for d in docs if d.get("kind") == "node"]
    edges = [d for d in docs if d.get("kind") == "edge"]

    if not nodes and not edges:
        logger.warning(
            "Visualization skipped: no documents have a 'kind' field "
            "(query may have projected it away)."
        )
        return "\n\nVisualization skipped: returned documents lack 'kind' field."

    result = QueryResult(nodes=nodes, edges=edges)

    graph = build_networkx_graph(result)
    path = render_html(graph, open_browser=True)

    return (
        f"\n\nGraph visualized: {graph.number_of_nodes()} nodes, "
        f"{graph.number_of_edges()} edges → {path}"
    )


@mcp.tool
async def query_memory(
    query: str,
    ctx: Context,
    visualize: bool = False,
    max_results: int = 10,
) -> str:
    """Query the knowledge graph using natural language.

    Dynamically translates the query into a MongoDB aggregation pipeline.
    Supports hybrid search (vector + text), graph traversals, filters,
    and aggregations.

    Args:
        query: Natural language question about the knowledge graph.
        visualize: If true, also render an interactive HTML graph visualization.
        max_results: Maximum number of documents to return (default 10).
    """

    lc = ctx.lifespan_context
    results = await execute_nl_query(
        client=lc["client"],
        database=lc["database"],
        query=query,
        llm=lc["llm"],
        embedding_model=lc["embedding_model"],
        max_results=max_results,
    )
    output = _serialize(results)

    if visualize and results:
        output += _visualize(results)

    return output


@mcp.tool
async def search_memory(
    query: str,
    ctx: Context,
    top_k: int = 10,
    max_hops: int = 1,
    max_results: int = 10,
    visualize: bool = False,
) -> str:
    """Search the knowledge graph using semantic + text search with graph expansion.

    Uses vector similarity + text search with RRF fusion to find seed nodes,
    then expands the graph around them. Reliable fallback for semantic similarity.

    Args:
        query: Search query text.
        top_k: Number of seed nodes to retrieve.
        max_hops: Maximum hops for graph expansion.
        max_results: Maximum total documents (nodes + edges) to return (default 10).
        visualize: If true, also render an interactive HTML graph visualization.
    """

    lc = ctx.lifespan_context
    result = await structured_query_memory(
        client=lc["client"],
        database=lc["database"],
        query=query,
        embedding_model=lc["embedding_model"],
        top_k=top_k,
        max_hops=max_hops,
    )
    docs = result.nodes + result.edges
    if len(docs) > max_results:
        docs = docs[:max_results]
    output = _serialize(docs)

    if visualize and docs:
        output += _visualize(docs)

    return output
