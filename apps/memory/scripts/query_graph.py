"""
Query the memory — parent-document search in ``rag``, graph search in ``graphrag``.

Reads ``memory.mode`` ONCE at start (ADR-006 decision 5) and branches:

* ``graphrag`` (default) — unchanged: query → seeds → graph expansion → HTML
  graph; no query → the whole graph.
* ``rag`` — there are no edges, so there is nothing to draw. With a query the
  command prints the ranked **Parent chunk**s as text and writes no file;
  without one it refuses and exits 1.

Every read is scoped to a single ``user_id`` (#020). It defaults to the
current-session user; override with ``USER_ID=<ObjectId>`` or
``USER_IDENTIFIER=<handle>`` (the Makefile wires these for you). See
:func:`tree.entities.sessions.resolve_user_id` for the resolution precedence.

Usage:
    # Visualize the entire graph for the current-session user (graphrag)
    make memory-query-graph

    # Query: a subgraph in graphrag, ranked parent chunks in rag
    make memory-query-graph QUERY="What does Paul work on?"

    # Override the user by id or handle
    make memory-query-graph USER_IDENTIFIER=paul QUERY="MLOps"

    # Direct invocation
    uv run python scripts/query_graph.py --user-identifier paul --query "MLOps" --top-k 5

    # Pin the output file (graphrag only; default: .tree/graphs/<slug>-<stamp>.html)
    uv run python scripts/query_graph.py --query "MLOps" -o /tmp/mlops.html --no-open
"""

import asyncio
import logging
import textwrap

import click

from tree.config.app_config import app_config
from tree.config.settings import settings
from tree.db import init_mongodb
from tree.entities.sessions import resolve_user_id
from tree.logging import init_logger
from tree.memory.graph.retrieval import fetch_full_graph, query_memory
from tree.memory.visualize.graph import visualize_query_result
from tree.memory.rag.retrieval import retrieve_parents
from tree.memory.rag.types import RetrievalResult, RetrievedParent
from tree.models.get_model import get_embedding_model

init_logger()
logger = logging.getLogger(__name__)

RAG_FULL_GRAPH_UNAVAILABLE = (
    "Full-graph visualization is unavailable in rag mode (memory.mode=rag): "
    'there are no edges. Pass QUERY="..." for parent-document search or switch '
    "to graphrag."
)

# The one caveat line a degraded query prints before its results (ADR-008 §3):
# a text_only / vector_only answer ranked half the index, so "nothing relevant"
# is not a safe read of a short list. Printed FIRST, and also above
# "No results." — degraded-and-empty is exactly when the operator needs it.
DEGRADED_SEARCH_CAVEAT = (
    "Search ran {mode} — the other leg was unavailable; results may miss matches."
)

# How much of a parent to show per hit. A parent is ~4096 tokens; the terminal
# is a ranking view, not a reader, so it prints an excerpt and the operator
# opens the source if the hit looks right.
_EXCERPT_CHARS = 300


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------


def _format_parent_block(parent: RetrievedParent) -> str:
    """Render one retrieved parent as a score / title / heading-path block."""

    title = parent.document.title or parent.document.source_uri or "(untitled)"
    heading = " > ".join(parent.heading_path)
    header = f"[{parent.score:.3f}] {title}"
    if heading:
        header = f"{header} — {heading}"
    excerpt = textwrap.indent(parent.content[:_EXCERPT_CHARS], "    ")
    return f"{header}\n{excerpt}\n    matched children: {len(parent.matched_children)}"


def _print_parents(result: RetrievalResult) -> None:
    if result.search_mode != "hybrid":
        click.echo(DEGRADED_SEARCH_CAVEAT.format(mode=result.search_mode))
    if not result.parents:
        click.echo("No results.")
        return
    for parent in result.parents:
        click.echo(_format_parent_block(parent))
        click.echo("")


async def _run(
    user_id: str | None,
    user_identifier: str | None,
    query: str | None,
    top_k: int,
    max_hops: int,
    output: str | None,
    no_open: bool,
) -> None:
    mode = app_config.memory.mode
    if mode == "rag" and not query:
        click.echo(RAG_FULL_GRAPH_UNAVAILABLE)
        raise SystemExit(1)

    client = await init_mongodb(
        settings.mongo.mongo_uri.get_secret_value(),
        settings.mongo.mongo_initdb_database,
    )
    database = settings.mongo.mongo_initdb_database
    user_id = await resolve_user_id(user_id, user_identifier)

    if mode == "rag":
        logger.info(
            "Retrieving parents for user_id=%s: %r (top_k=%d)", user_id, query, top_k
        )
        result = await retrieve_parents(
            client,
            database,
            query,
            get_embedding_model(),
            user_id,
            top_k=top_k,
        )
        _print_parents(result)
        return

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
    visualize_query_result(result, output, open_browser=not no_open, query=query or "")


@click.command()
@click.option(
    "--user-id",
    default=None,
    help=(
        "Override the tenant whose memory to query by Mongo ObjectId. Defaults "
        "to the current-session user; also reads the ``USER_ID`` env var."
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
    help="Search query. Omit to visualize the full graph (graphrag only).",
)
@click.option(
    "--top-k",
    "-k",
    default=app_config.query.top_k,
    show_default=True,
    help="Number of seed nodes (graphrag) or parent chunks (rag) to retrieve.",
)
@click.option(
    "--max-hops",
    "-h",
    default=app_config.query.max_hops,
    show_default=True,
    help="Max hops for graph expansion. Ignored in rag mode (no edges).",
)
@click.option(
    "--output",
    "-o",
    default=None,
    help=(
        "Output HTML file (graphrag only). Defaults to "
        ".tree/graphs/<query-slug>-<UTC-stamp>.html under the repo root."
    ),
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
    output: str | None,
    no_open: bool,
) -> None:
    """Query the memory for the resolved user, in the configured memory mode."""

    asyncio.run(_run(user_id, user_identifier, query, top_k, max_hops, output, no_open))


if __name__ == "__main__":
    main()
