"""
Query and visualize the materialized knowledge graph.

Usage:
    # Visualize the entire graph
    uv run python scripts/query_graph.py

    # Query and visualize matching subgraph
    uv run python scripts/query_graph.py --query "What does Paul work on?"

    # Customise output and search parameters
    uv run python scripts/query_graph.py --query "MLOps" --top-k 5 --max-hops 2 -o result.html
"""

import asyncio
import logging

import click

from twin.config.settings import settings
from twin.db import init_mongodb
from twin.memory.query.core import query_memory
from twin.memory.query.visualize import visualize_query_result
from twin.memory.types import QueryResult
from twin.models.get_model import get_embedding_model

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

_KG_COLLECTION = "knowledge_graph"


async def _fetch_full_graph(client, database: str) -> QueryResult:
    """Load the entire materialized knowledge_graph as a QueryResult."""

    db = client[database]
    collection = db[_KG_COLLECTION]

    nodes: list[dict] = []
    async for doc in collection.find({"kind": "node"}):
        nodes.append(doc)

    edges: list[dict] = []
    async for doc in collection.find({"kind": "edge"}):
        edges.append(doc)

    return QueryResult(nodes=nodes, edges=edges)


async def _run(
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
            "Querying graph: %r (top_k=%d, max_hops=%d)", query, top_k, max_hops
        )
        embedding_model = get_embedding_model()
        result = await query_memory(
            client, database, query, embedding_model, top_k=top_k, max_hops=max_hops
        )
    else:
        logger.info("No query provided — loading full graph")
        result = await _fetch_full_graph(client, database)

    if not result.nodes and not result.edges:
        logger.error(
            "No data found. Run the extraction and materialization pipelines first."
        )
        raise SystemExit(1)

    logger.info("Result: %d nodes, %d edges", len(result.nodes), len(result.edges))
    visualize_query_result(result, output, open_browser=not no_open)


@click.command()
@click.option(
    "--query",
    "-q",
    default=None,
    help="Search query. Omit to visualize the full graph.",
)
@click.option(
    "--top-k",
    "-k",
    default=10,
    show_default=True,
    help="Number of seed nodes to retrieve.",
)
@click.option(
    "--max-hops",
    "-h",
    default=3,
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
def main(query: str | None, top_k: int, max_hops: int, output: str, no_open: bool):
    """Query and visualize the knowledge graph."""

    asyncio.run(_run(query, top_k, max_hops, output, no_open))


if __name__ == "__main__":
    main()
