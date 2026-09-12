"""MCP tool handlers that only make sense with a knowledge graph.

Imported by :mod:`tree.mcp.server` ONLY when ``app_config.memory.mode ==
"graphrag"`` (ADR-006 decision 5), so every tool registered here — including
``visualize_memory_graph`` — plus the side-effect app module it pulls in
(:mod:`tree.mcp.dashboard_app`) is absent from a rag-mode server. That gating
is the reason this module exists at all: in rag mode the collection holds node
rows only, so ``max_hops``, edge expansion and the dedup review queue have
nothing to operate on, and a registered-but-degraded tool would be worse than no
tool.

The mode-independent six (``search_memory`` in its rag form, the three ingest
tools, ``search_web``, ``scrape_web``) live in :mod:`tree.mcp.tools`, which this
module imports for the shared Opik-threading helper. ``search_memory`` is
declared in BOTH modules — one name, two signatures, exactly one registered per
mode.
"""

import json
import logging
from typing import Any

from bson import json_util
from fastmcp import Context
from pymongo.errors import PyMongoError
from fastmcp.apps import AppConfig
from fastmcp.tools import ToolResult

from tree.config.constants import (
    TAGS_MCP,
    TAGS_RETRIEVAL_MCP,
)
from tree.entities.memory import NodeType

# dashboard_app: side-effect import — registers the custom-HTML dashboard
# (memory_dashboard tool + ui:// resource).
from tree.mcp import dashboard_app  # noqa: F401
from tree.mcp.deep_search import write_deep_search_results

from tree.mcp.server import mcp
from tree.mcp.tools import (
    BLANK_QUERY_MESSAGE,
    _retrieval_error,
    _set_retrieval_thread,
    internal_error,
    storage_error,
    tool_error,
)

# viz_app: the MODE-NEUTRAL MCP App layer (ADR-007 §7) — it registers the
# ``ui://`` and ``graphs://`` resources as a side effect and owns the one
# dual-delivery helper. It does NOT import this module, so there is no cycle.
from tree.mcp.viz_app import GRAPH_VIEW_URI, _graph_tool_result
from tree.memory.graph.nl_query import execute_nl_query
from tree.memory.graph.retrieval import fetch_full_graph
from tree.memory.graph.retrieval import query_memory as structured_query_memory
from tree.memory.visualize.graph import to_graph_payload
from tree.memory.graph.review import (
    MergeStrategy,
    ReviewDecision,
)
from tree.memory.graph.review import (
    find_pending_duplicates as _find_pending_duplicates,
)
from tree.memory.graph.review import (
    review_duplicate as _review_duplicate,
)
from tree.memory.types import QueryResult
from tree.observability import track

logger = logging.getLogger(__name__)


def _serialize(docs: list[dict[str, Any]]) -> str:
    """Serialize MongoDB documents to JSON, stripping embedding fields."""

    cleaned = [{k: v for k, v in doc.items() if k != "embedding"} for doc in docs]
    return json_util.dumps(cleaned, indent=2)


def _dual_graph_result(
    ctx: Context,
    docs: list[dict[str, Any]],
    serialized: str,
    query: str,
) -> str | ToolResult:
    """Answer with the serialized docs AND the graph, on whichever channel fits.

    The ONE visualization seam shared by ``query_memory`` and ``search_memory``
    — both have the same shape (serialized docs + optional graph), so neither
    builds a **Graph payload** nor branches on client capability itself. That
    branching lives once, in :func:`~tree.mcp.viz_app._graph_tool_result`,
    which also serves ``visualize_memory_graph`` (ADR-005, decision 4): inline
    MCP App iframe when the client renders App UIs, else a self-contained file
    under ``.tree/graphs/`` + a ``graphs://`` resource link.

    ``serialized`` is carried VERBATIM into the model-visible text of whatever
    comes back: these tools' contract is answering the user's question, so the
    model must never lose the data to the visualization. The full node/edge
    dump rides ONLY in the ``audience=["user"]`` block the iframe reads.

    Returns the plain ``str`` — never a ``ToolResult`` — when the rows carry no
    ``kind`` field, i.e. an aggregation or a projection dropped it and there is
    nothing to draw.
    """

    nodes = [d for d in docs if d.get("kind") == "node"]
    edges = [d for d in docs if d.get("kind") == "edge"]

    if not nodes and not edges:
        logger.warning(
            "Visualization skipped: no documents have a 'kind' field "
            "(query may have projected it away)."
        )
        return (
            f"{serialized}\n\nVisualization skipped: returned documents "
            "lack 'kind' field."
        )

    payload = to_graph_payload(QueryResult(nodes=nodes, edges=edges))
    summary = (
        f"{serialized}\n\nGraph of these results: "
        f"{len(payload['nodes'])} nodes, {len(payload['edges'])} edges"
    )
    return _graph_tool_result(ctx, payload, summary, query=query)


@mcp.tool(app=AppConfig(resource_uri=GRAPH_VIEW_URI))
async def visualize_memory_graph(
    ctx: Context,
    query: str = "",
    top_k: int = 15,
    max_hops: int = 2,
    as_html_file: bool = False,
) -> str | ToolResult:
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

    Errors answer ``{error_type, retryable, message}`` — retry only when
    ``retryable`` is true.
    """

    lc = ctx.lifespan_context
    try:
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
            # The full-graph read hits the SAME Mongo, so it is inside the same
            # guard: "the database is down" must not differ by argument.
            result = await fetch_full_graph(
                client=lc["client"],
                database=lc["database"],
                user_id=lc["user_id"],
            )
            label = "your full memory"
    except Exception as exc:  # noqa: BLE001 — every failure becomes an envelope
        return _retrieval_error("visualize_memory_graph", exc)

    payload = to_graph_payload(result)
    n_nodes, n_edges = len(payload["nodes"]), len(payload["edges"])
    summary = f"Knowledge graph for {label}: {n_nodes} nodes, {n_edges} edges"

    return _graph_tool_result(
        ctx, payload, summary, query=query, as_html_file=as_html_file
    )


@mcp.tool(app=AppConfig(resource_uri=GRAPH_VIEW_URI))
@track(tags=TAGS_RETRIEVAL_MCP, name="query_memory", create_duplicate_root_span=False)
async def query_memory(
    query: str,
    ctx: Context,
    visualize: bool = False,
    max_results: int = 10,
) -> str | ToolResult:
    """Query the knowledge graph using natural language.

    Dynamically translates the query into a MongoDB aggregation pipeline.
    Supports hybrid search (vector + text), graph traversals, filters,
    and aggregations.

    The answer always carries the serialized results. With ``visualize`` the
    same graph view as ``visualize_memory_graph`` comes along: inline when the
    client renders MCP App UIs, otherwise a self-contained HTML file plus a
    ``graphs://`` resource link — do NOT re-author the HTML yourself. If that
    path exists locally just share it; if the server is remote (cloud), read
    the linked resource and save its text as a local ``.html`` file.

    Args:
        query: Natural language question about the knowledge graph.
        visualize: If true, also render an interactive HTML graph visualization.
        max_results: Maximum number of documents to return (default 10).

    Errors answer ``{error_type, retryable, message}`` — retry only when
    ``retryable`` is true. A blank query is ``invalid_input`` (not retryable):
    it would otherwise reach the embedding provider as an empty string.
    """

    if not query.strip():
        return tool_error("invalid_input", BLANK_QUERY_MESSAGE, retryable=False)

    _set_retrieval_thread(ctx, "query_memory")
    lc = ctx.lifespan_context
    try:
        results = await execute_nl_query(
            client=lc["client"],
            database=lc["database"],
            query=query,
            llm=lc["llm"],
            embedding_model=lc["embedding_model"],
            user_id=lc["user_id"],
            max_results=max_results,
        )
    except Exception as exc:  # noqa: BLE001 — every failure becomes an envelope
        return _retrieval_error("query_memory", exc)
    output = _serialize(results)

    if visualize and results:
        return _dual_graph_result(ctx, results, output, query)

    return output


@mcp.tool(app=AppConfig(resource_uri=GRAPH_VIEW_URI))
@track(tags=TAGS_RETRIEVAL_MCP, name="search_memory", create_duplicate_root_span=False)
async def search_memory(
    query: str,
    ctx: Context,
    top_k: int = 10,
    max_hops: int = 1,
    max_results: int = 10,
    visualize: bool = False,
) -> str | ToolResult:
    """Search the knowledge graph using semantic + text search with graph expansion.

    Uses vector similarity + text search with RRF fusion to find seed nodes,
    then expands the graph around them. Reliable fallback for semantic similarity.

    The answer always carries the serialized results. With ``visualize`` the
    same graph view as ``visualize_memory_graph`` comes along: inline when the
    client renders MCP App UIs, otherwise a self-contained HTML file plus a
    ``graphs://`` resource link — do NOT re-author the HTML yourself. If that
    path exists locally just share it; if the server is remote (cloud), read
    the linked resource and save its text as a local ``.html`` file.

    Args:
        query: Search query text.
        top_k: Number of seed nodes to retrieve.
        max_hops: Maximum hops for graph expansion.
        max_results: Maximum total documents (nodes + edges) to return (default 10).
        visualize: If true, also render an interactive HTML graph visualization.

    Errors answer ``{error_type, retryable, message}`` — retry only when
    ``retryable`` is true. A blank query is ``invalid_input`` (not retryable) —
    the SAME guard, message and code as the rag ``search_memory``, so a model
    sees one behaviour across both **Memory mode**s.
    """

    if not query.strip():
        return tool_error("invalid_input", BLANK_QUERY_MESSAGE, retryable=False)

    _set_retrieval_thread(ctx, "search_memory")
    lc = ctx.lifespan_context
    try:
        result = await structured_query_memory(
            client=lc["client"],
            database=lc["database"],
            query=query,
            embedding_model=lc["embedding_model"],
            user_id=lc["user_id"],
            top_k=top_k,
            max_hops=max_hops,
        )
    except Exception as exc:  # noqa: BLE001 — every failure becomes an envelope
        return _retrieval_error("search_memory", exc)
    docs = result.nodes + result.edges
    if len(docs) > max_results:
        docs = docs[:max_results]
    output = _serialize(docs)

    if visualize and docs:
        return _dual_graph_result(ctx, docs, output, query)

    return output


@mcp.tool
@track(
    tags=TAGS_RETRIEVAL_MCP, name="deep_search_memory", create_duplicate_root_span=False
)
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

    Errors answer ``{error_type, retryable, message}`` — retry only when
    ``retryable`` is true. An empty memory is NOT an error: it answers the
    plain string "No results found."; a BLANK query is ``invalid_input``.
    """

    if not query.strip():
        return tool_error("invalid_input", BLANK_QUERY_MESSAGE, retryable=False)

    _set_retrieval_thread(ctx, "deep_search_memory")
    lc = ctx.lifespan_context
    try:
        result = await structured_query_memory(
            client=lc["client"],
            database=lc["database"],
            query=query,
            embedding_model=lc["embedding_model"],
            user_id=lc["user_id"],
            top_k=top_k,
            max_hops=max_hops,
        )
    except Exception as exc:  # noqa: BLE001 — every failure becomes an envelope
        return _retrieval_error("deep_search_memory", exc)

    if not result.nodes and not result.edges:
        return "No results found."

    _, index_yaml = write_deep_search_results(query, result, session_id)

    return index_yaml


# ---------------------------------------------------------------------------
# Human-review tools (flagged SAME_AS pairs)
# ---------------------------------------------------------------------------


def _serialize_pending_duplicate(p: Any) -> dict[str, Any]:
    return {
        "source_node_id": p.source_node_id,
        "target_node_id": p.target_node_id,
        "source_name": p.source_name,
        "target_name": p.target_name,
        "entity_type": p.entity_type.value,
        "similarity_score": p.similarity_score,
        "match_type": p.match_type,
        "flagged_at": p.flagged_at.isoformat(),
        "edge_id": p.edge_id,
    }


def _serialize_review_result(r: Any) -> dict[str, Any]:
    return {
        "decision": r.decision.value,
        "winner_node_id": r.winner_node_id,
        "loser_node_id": r.loser_node_id,
        "applied_strategy": (
            r.applied_strategy.value if r.applied_strategy is not None else None
        ),
        "edges_transferred": r.edges_transferred,
        "same_as_edge_id": r.same_as_edge_id,
    }


@mcp.tool(name="review_list_pending")
# Review tools are human-in-the-loop curation of the dedup queue, not user-facing
# retrieval. Per the four-tag family they carry only ``mcp`` (no ``retrieval`` /
# ``ingestion``) even though list reads and confirm/reject mutate the graph — the
# curation surface is its own thing, kept off the read/write spend dashboards.
@track(tags=TAGS_MCP, name="review_list_pending", create_duplicate_root_span=False)
async def review_list_pending(
    ctx: Context,
    entity_type: str | None = None,
    limit: int = 50,
) -> str:
    """List pending SAME_AS pairs awaiting human review.

    Returned in descending order of similarity score so the highest-
    confidence candidates surface first. The optional ``entity_type``
    filter restricts results to pairs whose source node has that type
    (e.g. ``"person"``).

    Args:
        entity_type: Optional NodeType value (e.g. "person", "task",
            "preference"). ``None`` returns pairs of every
            type.
        limit: Maximum number of pairs to return (default 50).

    Errors answer ``{error_type, retryable, message}`` — retry only when
    ``retryable`` is true.
    """

    try:
        type_filter = NodeType(entity_type) if entity_type else None
    except ValueError as exc:
        return tool_error("invalid_input", str(exc), retryable=False)

    lc = ctx.lifespan_context
    database = lc["client"][lc["database"]]
    try:
        pending = await _find_pending_duplicates(
            database,
            user_id=lc["user_id"],
            entity_type=type_filter,
            limit=limit,
        )
    except PyMongoError as exc:
        return storage_error("review_list_pending", exc)
    except Exception as exc:  # noqa: BLE001 — no tool raises through FastMCP
        return internal_error("review_list_pending", exc)
    return json.dumps([_serialize_pending_duplicate(p) for p in pending], indent=2)


@mcp.tool(name="review_confirm")
@track(tags=TAGS_MCP, name="review_confirm", create_duplicate_root_span=False)
async def review_confirm(
    source_node_id: str,
    target_node_id: str,
    reviewed_by: str,
    ctx: Context,
    merge_strategy: str = "keep_primary",
) -> str:
    """Confirm a pending SAME_AS pair as a true duplicate.

    Merges the loser into the winner using the same algorithm the
    auto-merge surface would have used. Older ``created_at`` wins; ties
    broken by higher ``confidence``; final tie broken by lexicographic
    ``_id``. All non-SAME_AS edges incident to the loser are re-keyed to
    the winner; the loser is tombstoned (excluded from future dedup
    searches) but retained as an audit trail.

    Args:
        source_node_id: One endpoint of the SAME_AS edge.
        target_node_id: The other endpoint.
        reviewed_by: Reviewer identifier (email, agent handle, etc.) —
            persisted on the audit edge.
        merge_strategy: ``"keep_primary"`` (default),
            ``"merge_properties"``, or ``"keep_aliases"``.

    Errors answer ``{error_type, retryable, message}`` — retry only when
    ``retryable`` is true.
    """

    try:
        strategy = MergeStrategy(merge_strategy)
    except ValueError as exc:
        return tool_error("invalid_input", str(exc), retryable=False)

    lc = ctx.lifespan_context
    database = lc["client"][lc["database"]]
    try:
        result = await _review_duplicate(
            database,
            user_id=lc["user_id"],
            source_node_id=source_node_id,
            target_node_id=target_node_id,
            decision=ReviewDecision.CONFIRM,
            reviewed_by=reviewed_by,
            merge_strategy=strategy,
        )
    except PyMongoError as exc:
        return storage_error("review_confirm", exc)
    except ValueError as exc:
        return tool_error("invalid_state", str(exc), retryable=False)
    except Exception as exc:  # noqa: BLE001 — no tool raises through FastMCP
        return internal_error("review_confirm", exc)

    return json.dumps(_serialize_review_result(result), indent=2)


@mcp.tool(name="review_reject")
@track(tags=TAGS_MCP, name="review_reject", create_duplicate_root_span=False)
async def review_reject(
    source_node_id: str,
    target_node_id: str,
    reviewed_by: str,
    ctx: Context,
) -> str:
    """Reject a pending SAME_AS pair as a false positive.

    Marks the audit edge ``status="rejected"`` without touching either
    node. Future ``dedupe_entity`` runs filter out the rejected pair via
    the reject-pair ``$lookup``, so the same pair is never re-flagged.

    Args:
        source_node_id: One endpoint of the SAME_AS edge.
        target_node_id: The other endpoint.
        reviewed_by: Reviewer identifier (email, agent handle, etc.).

    Errors answer ``{error_type, retryable, message}`` — retry only when
    ``retryable`` is true.
    """

    lc = ctx.lifespan_context
    database = lc["client"][lc["database"]]
    try:
        result = await _review_duplicate(
            database,
            user_id=lc["user_id"],
            source_node_id=source_node_id,
            target_node_id=target_node_id,
            decision=ReviewDecision.REJECT,
            reviewed_by=reviewed_by,
            merge_strategy=MergeStrategy.KEEP_PRIMARY,
        )
    except PyMongoError as exc:
        return storage_error("review_reject", exc)
    except ValueError as exc:
        return tool_error("invalid_state", str(exc), retryable=False)
    except Exception as exc:  # noqa: BLE001 — no tool raises through FastMCP
        return internal_error("review_reject", exc)

    return json.dumps(_serialize_review_result(result), indent=2)
