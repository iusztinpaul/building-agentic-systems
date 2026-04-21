"""FastMCP server with lifespan for MongoDB + model initialization."""

import logging
from collections.abc import AsyncGenerator
from typing import Any

from fastmcp import FastMCP
from fastmcp.server.lifespan import lifespan

from tree.config.settings import settings
from tree.db import init_mongodb
from tree.models.get_model import get_embedding_model, get_llm

logger = logging.getLogger(__name__)


@lifespan
async def app_lifespan(server: FastMCP) -> AsyncGenerator[dict[str, Any], None]:
    """Initialize MongoDB connection and ML models at startup."""

    database = settings.mongo.mongo_initdb_database
    client = await init_mongodb(
        settings.mongo.mongo_uri.get_secret_value(),
        database,
    )
    llm = get_llm()
    embedding_model = get_embedding_model()

    from tree.memory.indexing.core import ensure_indexes

    await ensure_indexes(client, database)

    logger.info("MCP server ready (database=%s)", database)
    try:
        yield {
            "client": client,
            "database": database,
            "llm": llm,
            "embedding_model": embedding_model,
        }
    finally:
        await client.close()
        logger.info("MCP server shut down")


mcp = FastMCP(
    "Twin Memory",
    instructions=(
        "Query and build a personal knowledge graph of documents, people, tasks, "
        "episodes, and preferences. Use 'query_memory' for flexible natural language "
        "queries. Use 'search_memory' as a reliable fallback for semantic similarity search. "
        "Use 'deep_search_memory' for broad exploration — it saves results to disk and "
        "returns a lightweight index; read individual files for details. "
        "Use 'ingest_url' to add web content, 'ingest_file' for local files, "
        "and 'ingest_conversation' to extract knowledge from conversations."
    ),
    lifespan=app_lifespan,
)

import tree.mcp.tools  # noqa: E402, F401 — registers tools on `mcp`
