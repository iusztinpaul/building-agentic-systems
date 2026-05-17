"""
Query and visualize the materialized knowledge graph.

Every read is scoped to a single ``user_id`` (#020). Pass it via
``--user-id <ObjectId>`` or the ``USER_ID`` env var (the Makefile wires
this for you).

Usage:
    # Visualize the entire graph for a user
    make memory-query-graph USER_ID=507f1f77bcf86cd799439011

    # Query and visualize matching subgraph
    make memory-query-graph USER_ID=507f... QUERY="What does Paul work on?"

    # Direct invocation
    uv run python scripts/query_graph.py --user-id 507f... --query "MLOps" --top-k 5
"""

import asyncio
import logging
import os

import click
from beanie import PydanticObjectId

from tree.config.app_config import app_config
from tree.config.settings import settings
from tree.db import init_mongodb
from tree.memory.query.core import query_memory
from tree.memory.query.visualize import visualize_query_result
from tree.memory.types import QueryResult
from tree.models.get_model import get_embedding_model

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

_KG_COLLECTION = "knowledge_graph"


async def _fetch_full_graph(
    client, database: str, user_id: PydanticObjectId
) -> QueryResult:
    """Load the entire materialized knowledge_graph for ``user_id``."""

    db = client[database]
    collection = db[_KG_COLLECTION]

    nodes: list[dict] = []
    async for doc in collection.find({"user_id": user_id, "kind": "node"}):
        nodes.append(doc)

    edges: list[dict] = []
    async for doc in collection.find({"user_id": user_id, "kind": "edge"}):
        edges.append(doc)

    return QueryResult(nodes=nodes, edges=edges)


async def _run(
    user_id: PydanticObjectId,
    query: str | None,
    top_k: int,
    max_hops: int,
    output: str,
    no_open: bool,
) -> None:
    client = await init_mongodb(
        settings.mongo.mongo_uri.get_secret_value(),
        settings.mongo.mongo_initdb_database,
    )
    database = settings.mongo.mongo_initdb_database

    if query:
        logger.info(
            "Querying graph for user_id=%s: %r (top_k=%d, max_hops=%d)",
            user_id,
            query,
            top_k,
            max_hops,
        )
        embedding_model = get_embedding_model()
        result = await query_memory(
            client,
            database,
            query,
            embedding_model,
            user_id,
            top_k=top_k,
            max_hops=max_hops,
        )
    else:
        logger.info("No query provided — loading full graph for user_id=%s", user_id)
        result = await _fetch_full_graph(client, database, user_id)

    if not result.nodes and not result.edges:
        logger.error(
            "No data found for user_id=%s. Run the extraction and indexing "
            "pipelines first.",
            user_id,
        )
        raise SystemExit(1)

    logger.info("Result: %d nodes, %d edges", len(result.nodes), len(result.edges))
    visualize_query_result(result, output, open_browser=not no_open)


@click.command()
@click.option(
    "--user-id",
    default=None,
    help=(
        "Tenant id (24-char Mongo ObjectId) whose KG to query. Required; "
        "falls back to the ``USER_ID`` env var when omitted."
    ),
)
@click.option(
    "--query",
    "-q",
    default=None,
    help="Search query. Omit to visualize the full graph for ``--user-id``.",
)
@click.option(
    "--top-k",
    "-k",
    default=app_config.query.top_k,
    show_default=True,
    help="Number of seed nodes to retrieve.",
)
@click.option(
    "--max-hops",
    "-h",
    default=app_config.query.max_hops,
    show_default=True,
    help="Max hops for graph expansion.",
)
@click.option(
    "--output",
    "-o",
    default="knowledge_graph.html",
    show_default=True,
    help="Output HTML file.",
)
@click.option(
    "--no-open",
    is_flag=True,
    default=False,
    help="Don't open the browser automatically.",
)
def main(
    user_id: str | None,
    query: str | None,
    top_k: int,
    max_hops: int,
    output: str,
    no_open: bool,
) -> None:
    """Query and visualize the knowledge graph for one ``user_id``."""

    raw = user_id or os.environ.get("USER_ID")
    if not raw:
        logger.error(
            "--user-id is required (or set USER_ID env). No silent fallback "
            "to a default user."
        )
        raise SystemExit(1)

    try:
        parsed = PydanticObjectId(raw)
    except Exception as exc:  # noqa: BLE001
        logger.error("--user-id %r is not a valid Mongo ObjectId: %s", raw, exc)
        raise SystemExit(1) from exc

    asyncio.run(_run(parsed, query, top_k, max_hops, output, no_open))


if __name__ == "__main__":
    main()
