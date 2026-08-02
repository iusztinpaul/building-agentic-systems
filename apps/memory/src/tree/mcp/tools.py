"""MCP tool handlers — thin delegation to business logic."""

import asyncio
import json
import logging
from typing import Any, Literal

import httpx
from beanie import PydanticObjectId
from bson import json_util
from fastmcp import Context

from tree.data.online_pipeline import (
    ConversationSource,
    FileSource,
    UrlSource,
)
from tree.data.web.web_scrape import (
    DEFAULT_MAX_CHARS as _SCRAPE_DEFAULT_MAX_CHARS,
)
from tree.data.web.web_scrape import (
    MAX_URLS_PER_CALL as _SCRAPE_MAX_URLS_PER_CALL,
)
from tree.data.web.web_scrape import (
    scrape_one as _scrape_one,
)
from tree.data.web.web_pipeline import (
    trigger_url_batch_ingest as _trigger_url_batch_ingest,
)
from tree.data.web.web_serp import search as web_search
from tree.data.web.web_unlocker import (
    BrightDataConfigurationError,
    BrightDataRequestError,
)
from tree.entities.knowledge_graph import NodeType

# graph_app / dashboard_app: side-effect imports — register the read-only
# Sigma graph MCP App (visualize_memory_graph tool + ui:// resource) and the
# custom-HTML dashboard (memory_dashboard tool + ui:// resource).
from tree.mcp import dashboard_app, graph_app  # noqa: F401
from tree.mcp.deep_search import write_deep_search_results
from tree.mcp.server import mcp
from tree.online import dispatch_online_pipeline
from tree.memory.query.core import query_memory as structured_query_memory
from tree.memory.query.nl_query import execute_nl_query
from tree.memory.query.visualize import build_networkx_graph, render_html
from tree.memory.review import (
    MergeStrategy,
    ReviewDecision,
)
from tree.memory.review import (
    find_pending_duplicates as _find_pending_duplicates,
)
from tree.memory.review import (
    review_duplicate as _review_duplicate,
)
from tree.memory.types import QueryResult
from tree.config.constants import (
    TAGS_INGESTION_MCP,
    TAGS_MCP,
    TAGS_RETRIEVAL_MCP,
)
from tree.observability import (
    track,
    update_current_trace,
)

logger = logging.getLogger(__name__)


def _set_retrieval_thread(ctx: Context, tool: str) -> None:
    """Group this tool's trace under the server-instance thread, fail-open.

    Reads the lifespan ``thread_id`` (a server-instance UUID minted at boot —
    one harness session = one thread) and tags the current Opik trace with it
    plus ``user_id`` metadata. No-ops when Opik is unconfigured or the context
    lacks a thread_id.
    """

    try:
        lc = ctx.lifespan_context
        update_current_trace(
            thread_id=lc.get("thread_id"),
            metadata={"user_id": str(lc.get("user_id")), "tool": tool},
        )
    except Exception as exc:  # noqa: BLE001 — telemetry must never break the tool
        logger.debug("Opik retrieval-thread tagging no-op: %s", exc)


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
@track(tags=TAGS_RETRIEVAL_MCP, name="query_memory", create_duplicate_root_span=False)
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

    _set_retrieval_thread(ctx, "query_memory")
    lc = ctx.lifespan_context
    results = await execute_nl_query(
        client=lc["client"],
        database=lc["database"],
        query=query,
        llm=lc["llm"],
        embedding_model=lc["embedding_model"],
        user_id=lc["user_id"],
        max_results=max_results,
    )
    output = _serialize(results)

    if visualize and results:
        output += _visualize(results)

    return output


@mcp.tool
@track(tags=TAGS_RETRIEVAL_MCP, name="search_memory", create_duplicate_root_span=False)
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

    _set_retrieval_thread(ctx, "search_memory")
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
    docs = result.nodes + result.edges
    if len(docs) > max_results:
        docs = docs[:max_results]
    output = _serialize(docs)

    if visualize and docs:
        output += _visualize(docs)

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
    """

    _set_retrieval_thread(ctx, "deep_search_memory")
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

    if not result.nodes and not result.edges:
        return "No results found."

    _, index_yaml = write_deep_search_results(query, result, session_id)

    return index_yaml


# ---------------------------------------------------------------------------
# Ingestion tools
# ---------------------------------------------------------------------------


async def _ingest(
    source: UrlSource | FileSource | ConversationSource,
    *,
    user_id: PydanticObjectId,
    dup_extra: dict[str, Any],
) -> str:
    """Shared MCP ingest tail: dispatch to the online pipeline, serialize to JSON.

    ``dispatch_online_pipeline`` owns the whole contract — edge validation, the
    fire-and-forget ``online-pipeline`` deployment submit (ONE worker-side run
    ingests AND extracts), and the inline-flow fallback when no deployment is
    registered. The result's ``mode`` field says which path ran.
    """

    result = await dispatch_online_pipeline(source, user_id)
    return json.dumps({**result, **dup_extra})


@mcp.tool
@track(tags=TAGS_INGESTION_MCP, name="ingest_url", create_duplicate_root_span=False)
async def ingest_url(url: str, ctx: Context) -> str:
    """Fetch a web page and ingest its content into the knowledge graph.

    Async ingestion: SUBMITS ONE ``online-pipeline`` flow run (fetch +
    extraction inline, indexing submitted after) and returns immediately —
    ``{"status": "submitted", "flow_run_id": ..., "mode": "deployment"}``. It
    does not wait for the graph to be built. Without a registered deployment the
    same pipeline runs in-process instead (``"mode": "in_process"``, returning
    ``ingested``/``already_ingested`` synchronously).

    Args:
        url: The web URL to fetch and ingest.
    """

    lc = ctx.lifespan_context
    try:
        return await _ingest(
            UrlSource(uri=url), user_id=lc["user_id"], dup_extra={"url": url}
        )
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


@mcp.tool
@track(tags=TAGS_INGESTION_MCP, name="ingest_file", create_duplicate_root_span=False)
async def ingest_file(
    file_path: str,
    content: str,
    ctx: Context,
    title: str | None = None,
) -> str:
    """Ingest a file's text content into the knowledge graph.

    The server never opens ``file_path`` — it may not share a filesystem with
    you. Read the file YOURSELF and pass its text as ``content``. Async
    ingestion: SUBMITS ONE ``online-pipeline`` flow run (document + extraction
    inline, indexing submitted after) and returns immediately
    (``{"status": "submitted", "mode": "deployment"}``); without a registered
    deployment the same pipeline runs in-process (``"mode": "in_process"``).

    Args:
        file_path: Absolute path of the file on YOUR machine. Identity only:
            it becomes the dedup key (source_uri) and default title, so always
            pass the same absolute form for the same file.
        content: The file's text, read by you (convert non-text formats to
            plain text/markdown first).
        title: Optional title override. Defaults to the filename.
    """

    lc = ctx.lifespan_context
    try:
        return await _ingest(
            FileSource(path=file_path, content=content, title=title),
            user_id=lc["user_id"],
            dup_extra={"file_path": file_path},
        )
    except ValueError as exc:
        return json.dumps({"error": "file_error", "detail": str(exc)})


@mcp.tool
@track(tags=TAGS_INGESTION_MCP, name="search_web", create_duplicate_root_span=False)
async def search_web(
    query: str,
    ctx: Context,
    engine: Literal["google", "bing", "yandex"] = "google",
    num_results: int = 10,
    country: str | None = None,
    language: str | None = None,
    ingest: bool = False,
    ingest_top_k: int | None = None,
    ingest_urls: list[str] | None = None,
) -> str:
    """Run an on-demand web search via Bright Data's SERP API.

    Returns SERP results (rank, title, URL, snippet) directly to the caller.
    By default, does NOT ingest anything into the knowledge graph — call
    `ingest_url` afterwards on URLs you want to keep, or call `search_web`
    with `ingest=true` for ingestion.

    Args:
        query: The search query.
        engine: Search engine to query. Defaults to "google".
        num_results: Maximum number of organic results to return (default 10).
        country: Optional 2-letter ISO country code for geo-targeting (e.g. "us").
        language: Optional 2-letter language code (e.g. "en").
        ingest: If true, fire-and-forget the `ingest-web-url-batch-etl`
            Prefect deployment with the selected URLs. Default false.
        ingest_top_k: When `ingest=true`, ingest only the first K URLs from
            the SERP results. Ignored if `ingest_urls` is provided.
        ingest_urls: When `ingest=true`, ingest exactly these URLs (overrides
            `ingest_top_k` and the SERP results).
    """

    # Validate ingestion flags BEFORE the SERP call. Misuse is a user error;
    # don't burn a SERP credit just to reject the request.
    if not ingest and (ingest_top_k is not None or ingest_urls is not None):
        return json.dumps(
            {
                "error": "invalid_input",
                "detail": "ingest_urls/ingest_top_k passed but ingest=false",
            }
        )

    if ingest and ingest_urls is not None and len(ingest_urls) == 0:
        return json.dumps({"error": "invalid_input", "detail": "ingest_urls is empty"})

    if ingest_top_k is not None and ingest_top_k < 1:
        return json.dumps(
            {
                "error": "invalid_input",
                "detail": (
                    f"ingest_top_k must be >= 1 (got {ingest_top_k}); omit it to "
                    "ingest all SERP results"
                ),
            }
        )

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

    payload: dict[str, Any] = {
        "query": query,
        "engine": engine,
        "results": [r.model_dump() for r in results],
    }

    if ingest:
        lc = ctx.lifespan_context
        payload["ingest"] = await _build_ingest_block(
            results, ingest_top_k, ingest_urls, user_id=lc["user_id"]
        )

    return json.dumps(payload, indent=2)


async def _build_ingest_block(
    results: list[Any],
    ingest_top_k: int | None,
    ingest_urls: list[str] | None,
    *,
    user_id: PydanticObjectId,
) -> dict[str, Any]:
    """Select URLs and trigger the batch ingest deployment. Always returns a dict."""

    # URL selection. ingest_urls (explicit) wins; else top-k of SERP; else all SERP URLs.
    if ingest_urls is not None:
        selected: list[str] = list(ingest_urls)
    elif ingest_top_k is not None:
        selected = [r.url for r in results[:ingest_top_k]]
    else:
        selected = [r.url for r in results]

    if not selected:
        # No URLs to ingest (e.g. empty SERP, or top-k slice of an empty list).
        # The search itself still succeeded.
        return {
            "triggered": False,
            "urls": [],
            "detail": "no urls to ingest",
        }

    try:
        trigger = await _trigger_url_batch_ingest(selected, user_id)
    except Exception as exc:  # noqa: BLE001 — best-effort: never propagate.
        logger.warning("Failed to trigger ingest-web-url-batch-etl: %s", exc)
        return {
            "triggered": False,
            "urls": selected,
            "error": str(exc),
        }

    return {
        "triggered": True,
        "urls": selected,
        "flow_run_id": trigger["flow_run_id"],
        "tracking_url": trigger["tracking_url"],
    }


@mcp.tool
@track(tags=TAGS_MCP, name="scrape_web", create_duplicate_root_span=False)
async def scrape_web(
    urls: list[str],
    ctx: Context,
    data_format: Literal["markdown", "html"] = "markdown",
    max_chars: int | None = _SCRAPE_DEFAULT_MAX_CHARS,
    timeout_seconds: float = 60.0,
) -> str:
    """Fetch the rendered content of one or more URLs without ingesting.

    Returns markdown (or HTML) for each URL directly to the caller. Does NOT
    write to MongoDB and does NOT trigger memory extraction. Pair with
    ``search_web`` to read SERP results inline; call ``ingest_url``
    afterwards on whichever URLs are worth keeping.

    Args:
        urls: List of absolute http:// or https:// URLs. Max 5 per call.
        data_format: ``"markdown"`` (default, best for LLM input) or
            ``"html"``.
        max_chars: Per-URL truncation cap. Default 30000 (~7-8K tokens).
            Pass ``None`` to disable truncation.
        timeout_seconds: Per-URL HTTP timeout passed to httpx.
    """

    if not urls:
        return json.dumps({"error": "invalid_input", "detail": "urls is empty"})

    if len(urls) > _SCRAPE_MAX_URLS_PER_CALL:
        return json.dumps(
            {
                "error": "invalid_input",
                "detail": (
                    f"max {_SCRAPE_MAX_URLS_PER_CALL} urls per call (got {len(urls)})"
                ),
            }
        )

    if max_chars is not None and max_chars < 1:
        return json.dumps(
            {
                "error": "invalid_input",
                "detail": "max_chars must be >= 1 or None",
            }
        )

    results = await asyncio.gather(
        *[
            _scrape_one(
                u,
                data_format=data_format,
                max_chars=max_chars,
                timeout_seconds=timeout_seconds,
            )
            for u in urls
        ]
    )

    succeeded = sum(1 for r in results if r["success"])

    payload: dict[str, Any] = {
        "requested": len(urls),
        "succeeded": succeeded,
        "failed": len(urls) - succeeded,
        "results": results,
    }

    return json.dumps(payload, indent=2)


@mcp.tool
@track(
    tags=TAGS_INGESTION_MCP,
    name="ingest_conversation",
    create_duplicate_root_span=False,
)
async def ingest_conversation(
    conversation_text: str,
    ctx: Context,
    title: str | None = None,
    session_uri: str | None = None,
    session_started_at: str | None = None,
) -> str:
    """Extract knowledge from a conversation and add it to the knowledge graph.

    Async ingestion: SUBMITS ONE ``online-pipeline`` flow run (document +
    extraction inline, indexing submitted after) and returns immediately —
    people, tasks, preferences, and relationships are built out-of-band by a
    worker. Returns ``{"status": "submitted", "mode": "deployment"}``; without a
    registered deployment the same pipeline runs in-process
    (``"mode": "in_process"``).

    Args:
        conversation_text: The full conversation text to process.
        title: Optional title for the conversation document.
        session_uri: Optional caller-supplied stable session identifier
            (e.g. ``"claude-session://abc"``, ``"openai-thread://..."``).
            When provided, becomes the Document's ``source_uri``
            verbatim — so two callers passing the same ``session_uri``
            dedupe to a single Document even if the text changes between
            calls. When omitted, falls back to a content-hash
            ``conversation://`` URI (Phase-1 behavior).
        session_started_at: Optional ISO-8601 UTC timestamp marking when
            the session began (e.g. ``"2026-05-17T14:30:00Z"``). Stored
            on ``Document.metadata["session_started_at"]``. Must be
            timezone-aware; naive timestamps are rejected.
    """

    if not conversation_text.strip():
        return json.dumps(
            {"error": "empty_input", "detail": "Conversation text must not be empty."}
        )

    parsed_session_started_at = None
    if session_started_at is not None:
        from datetime import datetime as _dt

        try:
            parsed_session_started_at = _dt.fromisoformat(
                session_started_at.replace("Z", "+00:00")
            )
        except ValueError as exc:
            return json.dumps(
                {
                    "error": "invalid_input",
                    "detail": (
                        "session_started_at must be an ISO-8601 datetime string "
                        f"(e.g. '2026-05-17T14:30:00Z'); got {session_started_at!r}: {exc}"
                    ),
                }
            )

    lc = ctx.lifespan_context
    try:
        return await _ingest(
            ConversationSource(
                text=conversation_text,
                title=title,
                session_uri=session_uri,
                session_started_at=parsed_session_started_at,
            ),
            user_id=lc["user_id"],
            dup_extra={},
        )
    except ValueError as exc:
        return json.dumps({"error": "invalid_input", "detail": str(exc)})


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
    """

    try:
        type_filter = NodeType(entity_type) if entity_type else None
    except ValueError as exc:
        return json.dumps({"error": "invalid_input", "detail": str(exc)})

    lc = ctx.lifespan_context
    database = lc["client"][lc["database"]]
    pending = await _find_pending_duplicates(
        database,
        user_id=lc["user_id"],
        entity_type=type_filter,
        limit=limit,
    )
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
    """

    try:
        strategy = MergeStrategy(merge_strategy)
    except ValueError as exc:
        return json.dumps({"error": "invalid_input", "detail": str(exc)})

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
    except ValueError as exc:
        return json.dumps({"error": "invalid_state", "detail": str(exc)})

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
    except ValueError as exc:
        return json.dumps({"error": "invalid_state", "detail": str(exc)})

    return json.dumps(_serialize_review_result(result), indent=2)
