"""MCP tool handlers registered in BOTH **Memory modes** — thin delegation.

The seven tools here (``search_memory``, ``ingest_url``, ``ingest_file``,
``ingest_conversation``, ``search_web``, ``scrape_web``,
``visualize_memory_embeddings``) work with node rows alone, so
:mod:`tree.mcp.server` imports this module unconditionally. The seven tools that
presuppose edges live in :mod:`tree.mcp.graph_tools`, imported only in
``graphrag`` (ADR-006 decision 5).

The **Embedding map** tool is here rather than with the graph tools because
clustering is mode-orthogonal (ADR-007 §8): the map draws points and their
clusters, never edges, so it says exactly as much in ``rag`` as in ``graphrag``.
It delivers through the SAME dual path as the graph tools, which is why this
module imports the MODE-NEUTRAL :mod:`tree.mcp.viz_app` (whose ``ui://`` and
``graphs://`` resources therefore exist in rag mode too) and never
:mod:`tree.mcp.graph_tools`.

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
from fastmcp.apps import AppConfig
from fastmcp.tools import ToolResult
from prefect.exceptions import ObjectNotFound, PrefectHTTPStatusError
from pydantic import BaseModel, Field
from pymongo.errors import PyMongoError

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

# viz_app: the MODE-NEUTRAL MCP App layer (ADR-007 §7) — it registers the
# ``ui://`` and ``graphs://`` resources as a side effect and owns the one
# dual-delivery helper. It imports neither this module nor the graph tools, so
# there is no cycle and rag mode stays free of graph code.
from tree.mcp.viz_app import GRAPH_VIEW_URI, _graph_tool_result
from tree.memory.clustering.store import load_embedding_map
from tree.memory.rag.retrieval import retrieve_parents
from tree.memory.rag.search import SearchUnavailableError
from tree.memory.visualize.embeddings import (
    NO_CLUSTERING_RUN_MESSAGE,
    to_embedding_map_payload,
)
from tree.models.exceptions import ModelError
from tree.online import dispatch_online_pipeline
from tree.observability import (
    track,
    update_current_trace,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# The **Tool error envelope** — ONE failure shape, every tool, both modes
# ---------------------------------------------------------------------------

#: The shared docstring line: tools describe their OWN codes (the full list
#: lives on :class:`ToolErrorEnvelope`), this sentence describes the shape. It
#: is a TEST ANCHOR, not the source of the prose — every envelope-answering
#: tool's docstring must CONTAIN it verbatim (modulo line wrapping), asserted in
#: ``test_error_envelope.py``. It cannot be interpolated: FastMCP reads
#: ``__doc__`` at import time, so editing this string does not edit the
#: docstrings — it turns the test red instead.
ERROR_CONTRACT = (
    "Errors answer ``{error_type, retryable, message}`` — retry only when "
    "``retryable`` is true."
)

#: What "retrieval is DOWN" looks like, as opposed to "retrieval found
#: nothing": the search legs gave up (:class:`SearchUnavailableError`), Mongo is
#: unreachable (:class:`PyMongoError`), or the embedding provider failed
#: (:class:`~tree.models.exceptions.ModelError` — the BASE, since both a
#: Voyage ``ExtractionError`` and a bare ``ModelError`` from an unresolvable
#: Modal endpoint escape ``embed()``). All three are transient infrastructure,
#: so all three answer ``search_unavailable`` with ``retryable=True``.
_RETRIEVAL_UNAVAILABLE = (SearchUnavailableError, PyMongoError, ModelError)


class ToolErrorEnvelope(BaseModel):
    """The ONE failure shape every MCP tool answers with (ADR-008 §2).

    ``retryable`` is the field a model acts on — not the ``error_type`` string:

    * ``true`` — the SAME call may succeed later: transient infrastructure
      (network, provider, Prefect, Mongo). Codes: ``network_error``,
      ``fetch_failed``, ``search_unavailable``, ``storage_unavailable``,
      ``pipeline_unavailable``, ``http_error`` on 429 / 5xx.
    * ``false`` — change the input or stop: validation, configuration,
      unsupported input, a bug. Codes: ``invalid_input``, ``unsupported_url``,
      ``configuration_error``, ``file_error``, ``invalid_state``,
      ``internal_error``, ``http_error`` on any other 4xx.

    Replaces the pre-ADR-008 ``{"error", "detail"}`` outright — no alias.
    """

    error_type: str = Field(description="Stable machine code, e.g. invalid_input.")
    retryable: bool = Field(description="True iff the SAME call may succeed later.")
    message: str = Field(description="One human-readable sentence for the model.")


def tool_error(error_type: str, message: str, *, retryable: bool) -> str:
    """Build the **Tool error envelope** as the JSON string a tool returns.

    The codes and their retryability are enumerated on
    :class:`ToolErrorEnvelope`; the two that are easy to confuse are
    ``search_unavailable`` (the RETRIEVAL legs are down — vector / text /
    embedding provider) and ``storage_unavailable`` (Mongo is unreachable
    outside retrieval: the ingest pre-flight, the review queue).

    ``retryable`` is keyword-only on purpose: it is the field the model acts on,
    and a bare positional ``False`` at a call site reads as noise.
    """

    return ToolErrorEnvelope(
        error_type=error_type, retryable=retryable, message=message
    ).model_dump_json()


#: The ONE message a Mongo outage answers with, wherever it is caught outside
#: retrieval. A model reads it and retries the identical call later.
STORAGE_UNAVAILABLE_MESSAGE = "memory store unreachable — try again"

#: The ONE message a blank free-text query answers with, in BOTH modes. Every
#: search-shaped tool guards on it BEFORE any tracing or retrieval, so a model
#: that sends whitespace gets the same ``invalid_input`` everywhere instead of
#: the embedding provider's raw ``400 Input cannot contain empty strings``.
BLANK_QUERY_MESSAGE = "query must not be empty"


def storage_error(tool: str, exc: Exception) -> str:
    """Map an unreachable Mongo to ``storage_unavailable``, logging the traceback.

    The boundaries ADR-008 §2 does not name — the ingest pre-flight
    ``Document.find_one`` and the three review-queue tools — still talk to
    Mongo, and a :class:`~pymongo.errors.PyMongoError` escaping to FastMCP is a
    protocol error the model cannot read. Deliberately NOT
    ``search_unavailable``: that code means the retrieval legs are down, and a
    model told "search is unavailable" after an ``ingest_url`` call learns the
    wrong thing about the memory.

    Called from INSIDE an ``except`` block, so ``logger.exception`` records the
    traceback.
    """

    logger.exception("%s failed: memory store unreachable: %s", tool, exc)
    return tool_error(
        "storage_unavailable", STORAGE_UNAVAILABLE_MESSAGE, retryable=True
    )


def _http_retryable(status_code: int) -> bool:
    """429 and 5xx come back; every other 4xx needs a different call, not a retry."""

    return status_code == 429 or status_code >= 500


def internal_error(tool: str, exc: Exception) -> str:
    """The last-resort catch-all: an unenumerated failure, as an envelope.

    ADR-008 §2 is "no tool raises through FastMCP any more", not "no tool raises
    at the two named boundaries": a protocol error carries no ``retryable`` and
    no message the model can act on. ``retryable=False`` because an unexpected
    exception is a bug until someone reads the traceback this logs — the model
    must stop, not loop.

    Called from INSIDE an ``except`` block, so ``logger.exception`` records the
    traceback.
    """

    logger.exception("%s failed: %s", tool, exc)
    return tool_error("internal_error", f"{tool} failed: {exc}", retryable=False)


def _retrieval_error(tool: str, exc: Exception) -> str:
    """Map ANY retrieval failure to an envelope, always logging the traceback.

    The retrieval boundary of ADR-008 §2, shared by the rag ``search_memory``
    here and the four graphrag readers in :mod:`tree.mcp.graph_tools`: a dead
    index, an unreachable Mongo or a failing embedding provider must reach the
    model as a JSON answer it can retry — never as an MCP protocol error — and
    everything else as a non-retryable ``internal_error``. One mapping, five
    call sites, so the two codes cannot drift apart per tool.

    Called from INSIDE an ``except`` block, so ``logger.exception`` records the
    traceback whichever branch is taken.
    """

    if isinstance(exc, _RETRIEVAL_UNAVAILABLE):
        logger.exception("%s failed: %s", tool, exc)
        return tool_error(
            "search_unavailable",
            f"Memory search is temporarily unavailable: {exc}",
            retryable=True,
        )
    return internal_error(tool, exc)


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
    The answer is one JSON object —
    ``{"parents": [...], "outcome": "found" | "nothing_found", "search_mode":
    "hybrid" | "text_only" | "vector_only"}``, best match first — where each
    parent carries its ``content``, its ``heading_path``, the ``document`` it
    came from (title, source_uri, date) and the ``matched_children`` that made
    it rank, so you can quote the passage that actually matched.

    ``outcome`` is ``nothing_found`` when an empty memory or an off-topic query
    left no hit above the relevance bar — say so instead of quoting unrelated
    passages. ``search_mode`` is ``hybrid`` when both search legs answered;
    ``text_only`` / ``vector_only`` means one leg was unavailable, so these
    results may be incomplete — worth one caveat line.

    Errors answer ``{error_type, retryable, message}`` — retry only when
    ``retryable`` is true. A blank query is ``invalid_input`` (not retryable)
    rather than the embedding provider's raw ``400 Input cannot contain empty
    strings``; an unreachable index or provider is ``search_unavailable``
    (retryable) — a JSON answer, never a protocol error.

    Args:
        query: Search query text.
        top_k: Maximum number of parent chunks to return (default 10).
    """

    if not query.strip():
        return tool_error("invalid_input", BLANK_QUERY_MESSAGE, retryable=False)

    _set_retrieval_thread(ctx, "search_memory")
    lc = ctx.lifespan_context
    try:
        result = await retrieve_parents(
            client=lc["client"],
            database=lc["database"],
            query=query,
            embedding_model=lc["embedding_model"],
            user_id=lc["user_id"],
            top_k=top_k,
        )
    except Exception as exc:  # noqa: BLE001 — every failure becomes an envelope
        return _retrieval_error("search_memory", exc)
    return result.model_dump_json(indent=2)


if MEMORY_MODE == "rag":
    mcp.tool(search_memory)


# ---------------------------------------------------------------------------
# Embedding map — READS the latest **Clustering run**, never computes it
# ---------------------------------------------------------------------------


@mcp.tool(app=AppConfig(resource_uri=GRAPH_VIEW_URI))
@track(
    tags=TAGS_RETRIEVAL_MCP,
    name="visualize_memory_embeddings",
    create_duplicate_root_span=False,
)
async def visualize_memory_embeddings(
    ctx: Context, hulls: bool = False, as_html_file: bool = False
) -> str | ToolResult:
    """Show the memory's embedding space as a 2D map: every child chunk is a
    point, coloured by its cluster from the latest clustering run, with an
    LLM-written label per cluster. Use when the user wants to *see* what topics
    the memory holds or how it is organised. ``hulls=true`` outlines each
    cluster. If no clustering run exists, this returns a message telling the
    operator which command to run; if the map is stale, the answer starts with
    a warning line.

    Args:
        hulls: Draw a convex hull around each cluster (default off).
        as_html_file: Set true when the user explicitly asks for a downloadable
            / openable HTML file instead of the inline interactive view.
    """

    _set_retrieval_thread(ctx, "visualize_memory_embeddings")
    lc = ctx.lifespan_context
    embedding_map = await load_embedding_map(
        lc["client"], lc["database"], lc["user_id"]
    )
    # Never an empty canvas: a user nobody has clustered gets the command to run
    # (ADR-007 §8). A plain ``str`` — there is no payload to deliver.
    if embedding_map is None:
        return NO_CLUSTERING_RUN_MESSAGE

    payload = to_embedding_map_payload(embedding_map, hulls=hulls)
    summary = payload["summary"]
    warning = payload["warning"]
    if warning:
        # The warning goes FIRST so a model relaying only the opening line
        # still tells the user the map under-reports the corpus.
        summary = f"{warning}\n{summary}"

    return _graph_tool_result(
        ctx, payload, summary, query="embedding-map", as_html_file=as_html_file
    )


# ---------------------------------------------------------------------------
# Ingestion tools
# ---------------------------------------------------------------------------


async def _ingest(
    source: UrlSource | FileSource | ConversationSource,
    *,
    user_id: PydanticObjectId,
    dup_extra: dict[str, Any],
) -> str:
    """Shared MCP ingest tail: dispatch to the online pipeline, serialize the receipt.

    ``dispatch_online_pipeline`` owns the whole contract — edge validation, the
    pre-flight duplicate lookup and the fire-and-forget ``online-pipeline``
    deployment submit (ONE worker-side run ingests AND extracts) — and answers
    an :class:`~tree.online.IngestReceipt`. Its five fields plus the tool's own
    ``dup_extra`` echo (``url`` / ``file_path``) ARE the tool's answer.

    The dispatch boundary of ADR-008 §2 lives here, and catches exactly two
    Prefect failures — there is NO in-process fallback (ADR-002), so the model
    must be able to tell "come back later" from "an operator has to act":

    * API unreachable (``ConnectError`` / a timeout / a Prefect HTTP status) →
      ``pipeline_unavailable``, retryable;
    * the deployment was never registered (``ObjectNotFound``) →
      ``configuration_error``, NOT retryable — the same call fails identically
      until someone serves the workflows.

    A ``PyMongoError`` from the dispatcher's pre-flight ``Document.find_one`` is
    caught FIRST as ``storage_unavailable`` (retryable): that lookup happens
    before any Prefect call, so an unreachable memory store is neither a
    pipeline nor a validation failure.

    Everything else propagates to the calling tool, which owns its own codes —
    notably the ``ValueError`` that ``validate_online_source`` raises for a bad
    URL (``unsupported_url``) or an oversized payload (``file_error``).
    """

    try:
        receipt = await dispatch_online_pipeline(source, user_id)
    except PyMongoError as exc:
        # FIRST clause: the dispatcher's pre-flight ``Document.find_one`` runs
        # BEFORE any Prefect call, so an unreachable Mongo must not be read as
        # a pipeline problem.
        return storage_error("ingest", exc)
    except ObjectNotFound:
        logger.exception("online-pipeline deployment is not registered")
        return tool_error(
            "configuration_error",
            "The 'online-pipeline/online-pipeline' deployment is not registered "
            "on the Prefect API — run `make memory-serve-workflows` (local) or "
            "deploy it, then retry.",
            retryable=False,
        )
    except (httpx.ConnectError, httpx.TimeoutException, PrefectHTTPStatusError) as exc:
        logger.exception(
            "Prefect API unreachable while dispatching the ingest: %s", exc
        )
        return tool_error(
            "pipeline_unavailable",
            "Prefect API unreachable — try again",
            retryable=True,
        )
    return json.dumps({**receipt.model_dump(), **dup_extra})


@mcp.tool
@track(tags=TAGS_INGESTION_MCP, name="ingest_url", create_duplicate_root_span=False)
async def ingest_url(url: str, ctx: Context) -> str:
    """Fetch a web page and ingest its content into memory.

    Answers an **Ingest receipt** — ``{"source_uri", "duplicate",
    "document_id", "flow_run_id", "status"}``. ``source_uri`` is the memory's
    natural key for the source. ``duplicate: true`` means it was ALREADY in
    memory at submit time: ``document_id`` names the existing Document,
    ``flow_run_id`` is null, ``status`` is ``"duplicate"`` and nothing was
    re-ingested — tell the user it is already known. ``duplicate: false`` means
    ONE ``online-pipeline`` flow run was submitted (``flow_run_id`` set,
    ``status`` its Prefect state, normally ``"scheduled"``): ingestion +
    extraction run out-of-band, so the source is NOT searchable yet when this
    returns. The answer also echoes the ``url`` you passed;
    ``source_uri`` may differ from it (a YouTube link canonicalises to
    ``https://www.youtube.com/watch?v=<id>``).

    Errors answer ``{error_type, retryable, message}`` — retry only when
    ``retryable`` is true. A Prefect API that is down is
    ``pipeline_unavailable`` and worth retrying; a non-http(s) link is
    ``unsupported_url`` and is not.

    Args:
        url: The web URL to fetch and ingest.
    """

    lc = ctx.lifespan_context
    try:
        return await _ingest(
            UrlSource(uri=url), user_id=lc["user_id"], dup_extra={"url": url}
        )
    except ValueError as exc:
        return tool_error("unsupported_url", str(exc), retryable=False)
    except BrightDataConfigurationError as exc:
        return tool_error("configuration_error", str(exc), retryable=False)
    except BrightDataRequestError as exc:
        return tool_error("fetch_failed", str(exc), retryable=True)
    except httpx.HTTPStatusError as exc:
        status_code = exc.response.status_code
        return tool_error(
            "http_error",
            f"HTTP {status_code}: {url}",
            retryable=_http_retryable(status_code),
        )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        return tool_error(
            "network_error", f"Could not reach {url}: {exc}", retryable=True
        )
    except Exception as exc:  # noqa: BLE001 — no tool raises through FastMCP
        return internal_error("ingest_url", exc)


@mcp.tool
@track(tags=TAGS_INGESTION_MCP, name="ingest_file", create_duplicate_root_span=False)
async def ingest_file(
    file_path: str,
    content: str,
    ctx: Context,
    title: str | None = None,
) -> str:
    """Ingest a file's text content into memory.

    The server never opens ``file_path`` — it may not share a filesystem with
    you. Read the file YOURSELF and pass its text as ``content``.

    Answers an **Ingest receipt** — ``{"source_uri", "duplicate",
    "document_id", "flow_run_id", "status"}``. ``source_uri`` is the memory's
    natural key for the source. ``duplicate: true`` means it was ALREADY in
    memory at submit time: ``document_id`` names the existing Document,
    ``flow_run_id`` is null, ``status`` is ``"duplicate"`` and nothing was
    re-ingested — tell the user it is already known. ``duplicate: false`` means
    ONE ``online-pipeline`` flow run was submitted (``flow_run_id`` set,
    ``status`` its Prefect state, normally ``"scheduled"``): ingestion +
    extraction run out-of-band, so the source is NOT searchable yet when this
    returns. The answer also echoes the ``file_path`` you passed
    (``source_uri`` is ``file://<path>``).

    Errors answer ``{error_type, retryable, message}`` — retry only when
    ``retryable`` is true.

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
        return tool_error("file_error", str(exc), retryable=False)
    except Exception as exc:  # noqa: BLE001 — no tool raises through FastMCP
        return internal_error("ingest_file", exc)


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
    By default, does NOT ingest anything into memory — call
    `ingest_url` afterwards on URLs you want to keep, or call `search_web`
    with `ingest=true` for ingestion.

    Errors answer ``{error_type, retryable, message}`` — retry only when
    ``retryable`` is true. (The nested ``ingest`` block of a SUCCESSFUL answer
    is a sub-result, not an envelope: it reports ``triggered`` plus, on a
    failed trigger, its own ``error`` text.)

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
        return tool_error(
            "invalid_input",
            "ingest_urls/ingest_top_k passed but ingest=false",
            retryable=False,
        )

    if ingest and ingest_urls is not None and len(ingest_urls) == 0:
        return tool_error("invalid_input", "ingest_urls is empty", retryable=False)

    if ingest_top_k is not None and ingest_top_k < 1:
        return tool_error(
            "invalid_input",
            f"ingest_top_k must be >= 1 (got {ingest_top_k}); omit it to "
            "ingest all SERP results",
            retryable=False,
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
        return tool_error("invalid_input", str(exc), retryable=False)
    except BrightDataConfigurationError as exc:
        return tool_error("configuration_error", str(exc), retryable=False)
    except BrightDataRequestError as exc:
        return tool_error("fetch_failed", str(exc), retryable=True)
    except httpx.HTTPStatusError as exc:
        status_code = exc.response.status_code
        return tool_error(
            "http_error",
            f"HTTP {status_code} from Bright Data SERP API",
            retryable=_http_retryable(status_code),
        )
    except (httpx.ConnectError, httpx.TimeoutException) as exc:
        return tool_error(
            "network_error",
            f"Could not reach Bright Data SERP API: {exc}",
            retryable=True,
        )
    except Exception as exc:  # noqa: BLE001 — no tool raises through FastMCP
        return internal_error("search_web", exc)

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

    Errors answer ``{error_type, retryable, message}`` — retry only when
    ``retryable`` is true. Per-URL failures are NOT errors: they come back
    inside ``results`` with ``success: false``.

    Args:
        urls: List of absolute http:// or https:// URLs. Max 5 per call.
        data_format: ``"markdown"`` (default, best for LLM input) or
            ``"html"``.
        max_chars: Per-URL truncation cap. Default 30000 (~7-8K tokens).
            Pass ``None`` to disable truncation.
        timeout_seconds: Per-URL HTTP timeout passed to httpx.
    """

    if not urls:
        return tool_error("invalid_input", "urls is empty", retryable=False)

    if len(urls) > _SCRAPE_MAX_URLS_PER_CALL:
        return tool_error(
            "invalid_input",
            f"max {_SCRAPE_MAX_URLS_PER_CALL} urls per call (got {len(urls)})",
            retryable=False,
        )

    if max_chars is not None and max_chars < 1:
        return tool_error(
            "invalid_input", "max_chars must be >= 1 or None", retryable=False
        )

    try:
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
    except Exception as exc:  # noqa: BLE001 — no tool raises through FastMCP
        # ``_scrape_one`` answers per-URL failures in its own result dict, so
        # reaching here means the fan-out itself broke, not one bad URL.
        return internal_error("scrape_web", exc)

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
    """Extract knowledge from a conversation and add it to memory.

    Answers an **Ingest receipt** — ``{"source_uri", "duplicate",
    "document_id", "flow_run_id", "status"}``. ``source_uri`` is the memory's
    natural key for the source. ``duplicate: true`` means it was ALREADY in
    memory at submit time: ``document_id`` names the existing Document,
    ``flow_run_id`` is null, ``status`` is ``"duplicate"`` and nothing was
    re-ingested — tell the user it is already known. ``duplicate: false`` means
    ONE ``online-pipeline`` flow run was submitted (``flow_run_id`` set,
    ``status`` its Prefect state, normally ``"scheduled"``): ingestion +
    extraction run out-of-band, so the source is NOT searchable yet when this
    returns. The chunk rows (and, in ``graphrag``, the people /
    tasks / preferences and their relationships) are written out-of-band by a
    worker.

    Errors answer ``{error_type, retryable, message}`` — retry only when
    ``retryable`` is true.

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
        return tool_error(
            "invalid_input", "Conversation text must not be empty.", retryable=False
        )

    parsed_session_started_at = None
    if session_started_at is not None:
        from datetime import datetime as _dt

        try:
            parsed_session_started_at = _dt.fromisoformat(
                session_started_at.replace("Z", "+00:00")
            )
        except ValueError as exc:
            return tool_error(
                "invalid_input",
                "session_started_at must be an ISO-8601 datetime string "
                f"(e.g. '2026-05-17T14:30:00Z'); got {session_started_at!r}: {exc}",
                retryable=False,
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
        return tool_error("invalid_input", str(exc), retryable=False)
    except Exception as exc:  # noqa: BLE001 — no tool raises through FastMCP
        return internal_error("ingest_conversation", exc)
