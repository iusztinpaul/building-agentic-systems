"""
Query and visualize the materialized knowledge graph.

Every read is scoped to a single ``user_id`` (#020). It defaults to the
current-session user; override with ``USER_ID=<ObjectId>`` or
``USER_IDENTIFIER=<handle>`` (the Makefile wires these for you). See
:func:`tree.entities.sessions.resolve_user_id` for the resolution precedence.

Usage:
    # Visualize the entire graph for the current-session user
    make memory-query-graph

    # Query and visualize matching subgraph
    make memory-query-graph QUERY="What does Paul work on?"

    # Override the user by id or handle
    make memory-query-graph USER_IDENTIFIER=paul QUERY="MLOps"

    # Direct invocation
    uv run python scripts/query_graph.py --user-identifier paul --query "MLOps" --top-k 5
"""

import asyncio
import logging

import click

from tree.entities.sessions import resolve_user_id
from tree.config.app_config import app_config
from tree.config.settings import settings
from tree.db import init_mongodb
from tree.memory.query.core import fetch_full_graph, query_memory
from tree.memory.query.visualize import visualize_query_result
from tree.models.get_model import get_embedding_model

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


async def _run(
    user_id: str | None,
    user_identifier: str | None,
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
    user_id = await resolve_user_id(user_id, user_identifier)

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
        result = await fetch_full_graph(client, database, user_id)

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
        "Override the tenant whose KG to query by Mongo ObjectId. Defaults to "
        "the current-session user; also reads the ``USER_ID`` env var."
    ),
)
@click.option(
    "--user-identifier",
    default=None,
    help=(
        "Override the tenant by stable handle (e.g. email). Defaults to the "
        "current-session user; also reads the ``USER_IDENTIFIER`` env var."
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
    user_identifier: str | None,
    query: str | None,
    top_k: int,
    max_hops: int,
    output: str,
    no_open: bool,
) -> None:
    """Query and visualize the knowledge graph for the resolved user."""

    asyncio.run(_run(user_id, user_identifier, query, top_k, max_hops, output, no_open))


if __name__ == "__main__":
    main()
