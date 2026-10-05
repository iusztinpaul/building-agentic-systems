"""FastMCP server with lifespan for MongoDB + model initialization.

The server pins NO user. Every tool resolves the **Request user** per call
through :func:`tree.mcp.request_user.resolve_request_user` (ADR-014 §1–§3).

The **Memory mode**, by contrast, IS pinned (ADR-006 decision 5): read
ONCE at import into :data:`MEMORY_MODE`, never per request. It decides
BOTH which tool modules get imported at the bottom of this file (the
six graph tools live in ``tree.mcp.graph_tools`` and are never
imported in ``rag`` mode) and which ``instructions`` the server
advertises.
"""

import logging
import sys
import uuid
from collections.abc import AsyncGenerator
from typing import Any, Literal

from fastmcp import FastMCP
from fastmcp.server.lifespan import lifespan

from tree.config.app_config import app_config
from tree.config.settings import settings
from tree.db import init_mongodb
from tree.memory.rag.indexing import (
    assert_settings_match_live_vector_index,
    ensure_indexes,
)
from tree.models.get_model import get_embedding_model, get_llm
from tree.observability import configure_opik, flush_opik

logger = logging.getLogger(__name__)


# The ONE read of the **Memory mode** on the server side (ADR-006 decision 5),
# pinned at import: a running server serves one mode. Flip it with ``TREE_MEMORY__MODE=rag`` and restart — there is no
# per-request switch, so no tool ever has to ask which mode it is in.
MEMORY_MODE: Literal["rag", "graphrag"] = app_config.memory.mode


@lifespan
async def app_lifespan(server: FastMCP) -> AsyncGenerator[dict[str, Any], None]:
    """Initialize MongoDB and the ML models at startup (no user — ADR-014 §1).

    Also runs :func:`assert_settings_match_live_vector_index` so an
    embedding-dimension drift (settings vs the live Atlas Vector Search
    index) surfaces at boot rather than at first vector query.
    """

    # Configure Opik observability once at boot (no-op without OPIK_API_KEY).
    configure_opik()

    database = settings.mongo.mongo_initdb_database
    client = await init_mongodb(
        settings.mongo.mongo_uri.get_secret_value(),
        database,
    )
    # Built, never pre-warmed (ADR-009 §11): this process lives for hours while
    # a Modal container idles out after ~5 minutes, so warming a GPU here bills
    # an afternoon of nothing and spends Horizon's 60 s readiness window on a
    # boot. The Warm gate warms at use instead — the first query after a quiet
    # period waits out the cold start.
    llm = get_llm()
    embedding_model = get_embedding_model()

    # Server-instance thread id: one harness session = one long-lived MCP server
    # process = one Opik thread. FastMCP's Context does not expose a stable
    # per-session id we can rely on across tool calls, so retrieval tools group
    # their traces under this UUID (mirrored onto the lifespan context). See the
    # SWE report for the thread-identity decision.
    thread_id = f"mcp-session-{uuid.uuid4()}"

    # Ensure indexes (creates ``vector_index`` if absent), then assert
    # the live index dimension matches ``app_config.models.search_embedding.dimensions``. The
    # assertion is the loud-fail gate the plan calls for.
    #
    # On a serverless host (Prefect Horizon / Lambda) this boot work — creating
    # the Atlas vector index and polling mongot until it syncs — can run for
    # minutes and blocks port-8081 readiness past the 60s window, so the server
    # never starts. ``MCP_SKIP_INDEX_BOOTSTRAP=true`` skips it: indexes are
    # created by the indexing pipeline, and the MCP server only queries.
    if settings.mcp_skip_index_bootstrap:
        logger.info(
            "MCP_SKIP_INDEX_BOOTSTRAP set — skipping ensure_indexes + "
            "vector-index assertion (fast serverless boot)."
        )
    else:
        await ensure_indexes(client, database, embedding_model=embedding_model)
        await assert_settings_match_live_vector_index(client, database)

    logger.info("MCP server ready (database=%s, thread_id=%s)", database, thread_id)
    try:
        yield {
            "client": client,
            "database": database,
            "llm": llm,
            "embedding_model": embedding_model,
            "thread_id": thread_id,
        }
    finally:
        await client.close()
        # Flush batched Opik telemetry before the process exits (fail-open).
        flush_opik()
        logger.info("MCP server shut down")


# One instructions text per mode: the model must never be told about a tool this
# server does not register. The rag text therefore names ONLY the eight tools
# ``tree.mcp.tools`` registers (the **Embedding map** tool included — clustering
# is mode-orthogonal — and the **Memory structure** tree) — no ``query_memory``,
# no ``deep_search_memory``, no graph vocabulary at all.
_GRAPHRAG_INSTRUCTIONS = (
    "Query and build a personal knowledge graph of documents, people, tasks, "
    "and preferences. Use 'query_memory' for flexible natural language "
    "queries. Use 'search_memory' as a reliable fallback for semantic similarity search. "
    "Use 'deep_search_memory' for broad exploration — it saves results to disk and "
    "returns a lightweight index; read individual files for details. "
    "Use 'search_web' for on-demand web searches that don't write to memory. "
    "Use 'ingest_url' to add web content, 'ingest_file' for local files, "
    "and 'ingest_conversation' to extract knowledge from conversations. "
    "Use 'visualize_memory_structure' to show the knowledge graph (documents, "
    "chunks, entities and their relations) as an interactive view, optionally "
    "narrowed to a query. "
    "Use 'visualize_memory_embeddings' to show a 2D map of the memory's topics "
    "(clusters of chunk embeddings) when the user asks what the memory holds."
)

_RAG_INSTRUCTIONS = (
    "Search and grow a personal memory of documents, files, and conversations. "
    "Use 'search_memory' to retrieve the passages that answer a question — it "
    "returns whole parent chunks with their document metadata, best match first. "
    "Use 'search_web' for on-demand web searches and 'scrape_web' to read specific "
    "pages inline; neither writes to memory. "
    "Use 'ingest_url' to add web content, 'ingest_file' for local files, "
    "and 'ingest_conversation' to store what a conversation established. "
    "Use 'visualize_memory_structure' to show how the memory is organised — "
    "documents and the chunks they split into — optionally narrowed to a query. "
    "Use 'visualize_memory_embeddings' to show a 2D map of the memory's topics "
    "(clusters of chunk embeddings) when the user asks what the memory holds."
)

mcp = FastMCP(
    "Tree Memory",
    instructions=(
        _GRAPHRAG_INSTRUCTIONS if MEMORY_MODE == "graphrag" else _RAG_INSTRUCTIONS
    ),
    lifespan=app_lifespan,
)

# FastMCP Cloud (Prefect Horizon) loads the configured entrypoint
# ``…/server.py:mcp`` BY FILE PATH, so this module runs under the name ``server``
# rather than the canonical package name ``tree.mcp.server``. The tool modules
# imported just below register on ``mcp`` via ``from tree.mcp.server import mcp``;
# without this alias that import would execute a SECOND, fresh copy of this file
# as ``tree.mcp.server`` and register all 14 tools on a DIFFERENT ``FastMCP``
# instance than the one Horizon serves — the deployed server then advertises 0
# tools (it always worked locally, where every caller already reaches this module
# through the package import). Aliasing this module as ``tree.mcp.server`` makes
# those imports resolve back to THIS instance. ``setdefault`` is a no-op on the
# normal package import path, where ``tree.mcp.server`` is already registered.
sys.modules.setdefault("tree.mcp.server", sys.modules[__name__])

# The tool set IS the mode (ADR-006 decision 5). ``tools`` holds the eight tools
# both modes serve; the six graph tools live behind this ``if`` so that in rag
# mode ``tree.mcp.graph_tools`` — and, through it, ``dashboard_app`` — never
# even reach ``sys.modules``. A graph tool called against a rag server gets the
# standard "unknown tool" error rather than a half-working path.
import tree.mcp.tools  # noqa: E402, F401 — registers tools on `mcp`

if MEMORY_MODE == "graphrag":
    import tree.mcp.graph_tools  # noqa: E402, F401 — registers graph tools on `mcp`
