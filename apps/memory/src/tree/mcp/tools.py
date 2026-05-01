"""MCP tool handlers — thin delegation to business logic."""

import json
import logging
from typing import Any, Literal

import httpx
from bson import json_util
from fastmcp import Context

from tree.data.conversation_pipeline import ingest_conversation as _ingest_conversation
from tree.data.core.ingest import ingest_url as _ingest_url_dispatch
from tree.data.file_pipeline import ingest_file as _ingest_file
from tree.data.web.web_serp import search as web_search
from tree.data.web.web_unlocker import (
    BrightDataConfigurationError,
    BrightDataRequestError,
)
from tree.mcp.deep_search import write_deep_search_results
from tree.mcp.ingest import run_ingestion_pipeline
from tree.mcp.server import mcp
from tree.memory.query.core import query_memory as structured_query_memory
from tree.memory.query.nl_query import execute_nl_query
from tree.memory.query.visualize import build_networkx_graph, render_html
from tree.memory.types import QueryResult

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


@mcp.tool
async def deep_search_memory(
    query: str,
    ctx: Context,
    top_k: int = 50,
    max_hops: int = 3,
    session_id: str | None = None,
) -> str:
    """Broad search across the knowledge graph with progressive disclosure.

    Runs an expanded search (more seeds, deeper traversal) and saves full
    results to disk as individual markdown files. Returns a YAML index with
    one-line summaries for each node and edge found.

    Use the file paths in the index to selectively read only the entries
    you need — avoids flooding the context window with all results at once.

    Args:
        query: Search query text.
        top_k: Number of seed nodes to retrieve (default 50).
        max_hops: Maximum hops for graph expansion (default 3).
        session_id: Optional session identifier for the output directory.
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

    if not result.nodes and not result.edges:
        return "No results found."

    _, index_yaml = write_deep_search_results(query, result, session_id)

    return index_yaml


# ---------------------------------------------------------------------------
# Ingestion tools
# ---------------------------------------------------------------------------


@mcp.tool
async def ingest_url(url: str, ctx: Context) -> str:
    """Fetch a web page and ingest its content into the knowledge graph.

    Routes the URL to the appropriate data pipeline (currently supports
    Substack articles), then runs memory extraction and indexing.

    Args:
        url: The web URL to fetch and ingest.
    """

    try:
        document = await _ingest_url_dispatch(url)
    except ValueError as exc:
        return json.dumps({"error": "unsupported_url", "detail": str(exc)})
    except BrightDataConfigurationError as exc:
        return json.dumps({"error": "configuration_error", "detail": str(exc)})
    except BrightDataRequestError as exc:
        return json.dumps({"error": "fetch_failed", "detail": str(exc)})
    except httpx.HTTPStatusError as exc:
        return json.dumps(
            {"error": "http_error", "detail": f"HTTP {exc.response.status_code}: {url}"}
        )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        return json.dumps(
            {"error": "network_error", "detail": f"Could not reach {url}: {exc}"}
        )

    if document is None:
        return json.dumps({"status": "already_ingested", "url": url})

    lc = ctx.lifespan_context
    summary = await run_ingestion_pipeline(
        document,
        client=lc["client"],
        database=lc["database"],
        llm=lc["llm"],
        embedding_model=lc["embedding_model"],
    )
    return json.dumps(summary)


@mcp.tool
async def ingest_file(
    file_path: str,
    ctx: Context,
    title: str | None = None,
) -> str:
    """Read a local file and ingest its content into the knowledge graph.

    Supports .txt, .md, and .html files. Creates a Document, then runs
    memory extraction and indexing.

    Args:
        file_path: Absolute path to the file to ingest.
        title: Optional title override. Defaults to the filename.
    """

    try:
        document = await _ingest_file(file_path, title)
    except (
        FileNotFoundError,
        IsADirectoryError,
        PermissionError,
        ValueError,
        UnicodeDecodeError,
    ) as exc:
        return json.dumps({"error": "file_error", "detail": str(exc)})

    if document is None:
        return json.dumps({"status": "already_ingested", "file_path": file_path})

    lc = ctx.lifespan_context
    summary = await run_ingestion_pipeline(
        document,
        client=lc["client"],
        database=lc["database"],
        llm=lc["llm"],
        embedding_model=lc["embedding_model"],
    )
    return json.dumps(summary)


@mcp.tool
async def search_web(
    query: str,
    ctx: Context,
    engine: Literal["google", "bing", "yandex"] = "google",
    num_results: int = 10,
    country: str | None = None,
    language: str | None = None,
) -> str:
    """Run an on-demand web search via Bright Data's SERP API.

    Returns SERP results (rank, title, URL, snippet) directly to the caller.
    Does NOT ingest anything into the knowledge graph — call `ingest_url`
    afterwards on URLs you want to keep.

    Args:
        query: The search query.
        engine: Search engine to query. Defaults to "google".
        num_results: Maximum number of organic results to return (default 10).
        country: Optional 2-letter ISO country code for geo-targeting (e.g. "us").
        language: Optional 2-letter language code (e.g. "en").
    """

    try:
        results = await web_search(
            query,
            engine=engine,
            num_results=num_results,
            country=country,
            language=language,
        )
    except ValueError as exc:
        return json.dumps({"error": "invalid_input", "detail": str(exc)})
    except BrightDataConfigurationError as exc:
        return json.dumps({"error": "configuration_error", "detail": str(exc)})
    except BrightDataRequestError as exc:
        return json.dumps({"error": "fetch_failed", "detail": str(exc)})
    except httpx.HTTPStatusError as exc:
        return json.dumps(
            {
                "error": "http_error",
                "detail": f"HTTP {exc.response.status_code} from Bright Data SERP API",
            }
        )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        return json.dumps(
            {
                "error": "network_error",
                "detail": f"Could not reach Bright Data SERP API: {exc}",
            }
        )

    return json.dumps(
        {
            "query": query,
            "engine": engine,
            "results": [r.model_dump() for r in results],
        },
        indent=2,
    )


@mcp.tool
async def ingest_conversation(
    conversation_text: str,
    ctx: Context,
    title: str | None = None,
) -> str:
    """Extract knowledge from a conversation and add it to the knowledge graph.

    Processes conversation text through the extraction pipeline to identify
    people, tasks, episodes, preferences, and relationships.

    Args:
        conversation_text: The full conversation text to process.
        title: Optional title for the conversation document.
    """

    if not conversation_text.strip():
        return json.dumps(
            {"error": "empty_input", "detail": "Conversation text must not be empty."}
        )

    document = await _ingest_conversation(conversation_text, title)

    if document is None:
        return json.dumps({"status": "already_ingested"})

    lc = ctx.lifespan_context
    summary = await run_ingestion_pipeline(
        document,
        client=lc["client"],
        database=lc["database"],
        llm=lc["llm"],
        embedding_model=lc["embedding_model"],
    )
    return json.dumps(summary)
