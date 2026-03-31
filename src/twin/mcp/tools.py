"""MCP tool handlers — thin delegation to business logic."""

import logging
from typing import Any

from bson import json_util
from fastmcp import Context

from twin.mcp.server import mcp
from twin.memory.query.core import query_memory as structured_query_memory
from twin.memory.query.nl_query import execute_nl_query

logger = logging.getLogger(__name__)


def _serialize(docs: list[dict[str, Any]]) -> str:
    """Serialize MongoDB documents to JSON, stripping embedding fields."""

    cleaned = [{k: v for k, v in doc.items() if k != "embedding"} for doc in docs]
    return json_util.dumps(cleaned, indent=2)


@mcp.tool
async def query_memory(query: str, ctx: Context) -> str:
    """Query the knowledge graph using natural language.

    Dynamically translates the query into a MongoDB aggregation pipeline.
    Supports hybrid search (vector + text), graph traversals, filters,
    and aggregations.

    Args:
        query: Natural language question about the knowledge graph.
    """

    lc = ctx.lifespan_context
    results = await execute_nl_query(
        client=lc["client"],
        database=lc["database"],
        query=query,
        llm=lc["llm"],
        embedding_model=lc["embedding_model"],
    )
    return _serialize(results)


@mcp.tool
async def search_memory(
    query: str, ctx: Context, top_k: int = 10, max_hops: int = 3
) -> str:
    """Search the knowledge graph using semantic + text search with graph expansion.

    Uses vector similarity + text search with RRF fusion to find seed nodes,
    then expands the graph around them. Reliable fallback for semantic similarity.

    Args:
        query: Search query text.
        top_k: Number of seed nodes to retrieve.
        max_hops: Maximum hops for graph expansion.
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
    return _serialize(result.nodes + result.edges)
