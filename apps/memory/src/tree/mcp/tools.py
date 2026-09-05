"""MCP tool handlers registered in BOTH **Memory modes** — thin delegation.

The six tools here (``search_memory``, ``ingest_url``, ``ingest_file``,
``ingest_conversation``, ``search_web``, ``scrape_web``) work with node rows
alone, so :mod:`tree.mcp.server` imports this module unconditionally. The seven
tools that presuppose edges live in :mod:`tree.mcp.graph_tools`, imported only
in ``graphrag`` (ADR-006 decision 5).

``search_memory`` is the one tool whose SIGNATURE depends on the mode — the rag
version below returns **Parent-document retrieval** results and takes ``top_k``
only; the graphrag version in :mod:`tree.mcp.graph_tools` keeps its graph
expansion knobs. Two functions, one registered name, never both: dead
``max_hops`` / ``visualize`` parameters that a rag server would have to ignore
are exactly what this split avoids.
"""

import asyncio
import json
import logging
from typing import Any, Literal

import httpx
from beanie import PydanticObjectId
from fastmcp import Context

from tree.config.constants import (
    TAGS_INGESTION_MCP,
    TAGS_MCP,
    TAGS_RETRIEVAL_MCP,
)
from tree.data.online_pipeline import (
    ConversationSource,
    FileSource,
    UrlSource,
)
from tree.data.web.web_pipeline import (
    trigger_url_batch_ingest as _trigger_url_batch_ingest,
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
from tree.data.web.web_serp import search as web_search
from tree.data.web.web_unlocker import (
    BrightDataConfigurationError,
    BrightDataRequestError,
)
from tree.mcp.server import MEMORY_MODE, mcp
from tree.memory.rag.retrieval import retrieve_parents
from tree.online import dispatch_online_pipeline
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


# ADR-006 decision 5: rag mode's ``search_memory``. Defined unconditionally (so
# it stays importable and unit-testable in either mode) and REGISTERED just
# below only when the server booted in rag mode — in graphrag the name is taken
# by the five-parameter graph version in :mod:`tree.mcp.graph_tools`.
@track(tags=TAGS_RETRIEVAL_MCP, name="search_memory", create_duplicate_root_span=False)
async def search_memory(query: str, ctx: Context, top_k: int = 10) -> str:
    """Hybrid (vector + text) search over child chunks, returning the distinct
    parent chunks with their document metadata.

    Children are the small embedded search units; parents are what you read.
    The answer is one JSON object — ``{"parents": [...]}``, best match first —
    where each parent carries its ``content``, its ``heading_path``, the
    ``document`` it came from (title, source_uri, date) and the
    ``matched_children`` that made it rank, so you can quote the passage that
    actually matched. An empty memory answers ``{"parents": []}``.

    Args:
        query: Search query text.
        top_k: Maximum number of parent chunks to return (default 10).
    """

    _set_retrieval_thread(ctx, "search_memory")
    lc = ctx.lifespan_context
    result = await retrieve_parents(
        client=lc["client"],
        database=lc["database"],
        query=query,
        embedding_model=lc["embedding_model"],
        user_id=lc["user_id"],
        top_k=top_k,
    )
    return result.model_dump_json(indent=2)


if MEMORY_MODE == "rag":
    mcp.tool(search_memory)


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

    ``dispatch_online_pipeline`` owns the whole contract — edge validation and
    the fire-and-forget ``online-pipeline`` deployment submit (ONE worker-side
    run ingests AND extracts), returning ``{"status": <flow-run state>,
    "flow_run_id": ...}``. There is no in-process path, so a Prefect failure
    raises here and surfaces through each tool's error handling.
    """

    result = await dispatch_online_pipeline(source, user_id)
    return json.dumps({**result, **dup_extra})


@mcp.tool
@track(tags=TAGS_INGESTION_MCP, name="ingest_url", create_duplicate_root_span=False)
async def ingest_url(url: str, ctx: Context) -> str:
    """Fetch a web page and ingest its content into the knowledge graph.

    Async ingestion: SUBMITS ONE ``online-pipeline`` flow run (fetch +
    extraction inline, indexing submitted after) and returns immediately —
    ``{"status": "scheduled", "flow_run_id": ...}``, where ``status`` is the
    new run's Prefect state. It does not wait for the graph to be built, so
    the page is NOT searchable yet when this returns.

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
    inline, indexing submitted after) and returns immediately —
    ``{"status": "scheduled", "flow_run_id": ...}``, the new run's Prefect
    state — so the file is NOT searchable yet when this returns.

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
    worker. Returns ``{"status": "scheduled", "flow_run_id": ...}``, the new
    run's Prefect state; the conversation is NOT searchable yet at that point.

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
