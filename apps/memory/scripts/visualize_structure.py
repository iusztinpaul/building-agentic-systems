"""
Render the **Memory structure** of one user as an interactive HTML file.

Reads ``memory.mode`` ONCE at start (ADR-006 decision 5) and draws what that
mode stores, through the ONE **Graph renderer** (ADR-005):

* ``rag`` — the ``document`` → **Parent chunk** → **Child chunk** tree. rag
  stores no edge rows, so the ``part_of`` edges are synthesised from each chunk
  row's ``parent_id`` at read time and never written
  (:mod:`tree.memory.rag.structure`). With no query: the most-recent documents
  and every chunk they own, in ``.tree/graphs/structure-<stamp>.html``. With a
  query: the parents **Parent-document retrieval** returns, their documents and
  ALL their children.
* ``graphrag`` — the knowledge graph: the **Full graph** with no query
  (``graph-<stamp>.html``), the expanded subgraph around the search seeds with
  one.

Either mode, no query: the 250 most-recent documents are embedded and the
``Documents`` slider shows 100 (``--max-docs`` embeds fewer); a query view ranks
its documents by relevance and shows them all. An empty memory, a query that
finds nothing and both search legs down each print ONE line and exit 1 without
writing a file. Text instead of a picture: ``make memory-search``.

Every read is scoped to a single ``user_id`` (#020). It defaults to the
current-session user; override with ``USER_ID=<ObjectId>`` or
``USER_IDENTIFIER=<handle>`` (the Makefile wires these for you). See
:func:`tree.entities.sessions.resolve_user_id` for the resolution precedence.

Usage:
    # The current user's memory structure: 250 most-recent documents embedded,
    # 100 shown by the Documents slider
    make memory-visualize-structure

    # Embed only the 50 most-recent documents
    make memory-visualize-structure MAX_DOCS=50

    # Narrow to a query's results (relevance-ranked)
    make memory-visualize-structure QUERY="What does Paul work on?"

    # Direct invocation, pinned output file, no browser
    uv run python scripts/visualize_structure.py --query "MLOps" -o /tmp/mlops.html --no-open
"""

import asyncio
import logging

import click

from tree.cli import user_options
from tree.config.app_config import app_config
from tree.config.settings import settings
from tree.db import init_mongodb
from tree.entities.sessions import resolve_user_id
from tree.logging import init_logger
from tree.memory.graph.retrieval import fetch_full_graph, query_memory
from tree.memory.rag.retrieval import retrieve_parents
from tree.memory.rag.search import SearchUnavailableError
from tree.memory.rag.structure import (
    EMPTY_MEMORY_MESSAGE,
    NO_RESULTS_MESSAGE,
    fetch_rag_structure,
    fetch_retrieval_structure,
)
from tree.memory.rag.types import MemoryStructure
from tree.memory.visualize.graph import visualize_query_result
from tree.models.get_model import get_embedding_model

init_logger()
logger = logging.getLogger(__name__)

# BOTH legs down (``SearchUnavailableError``) is an operator-facing outcome, not
# a bug: ONE line and exit 1 instead of a traceback (the same line
# ``search_memory.py`` prints).
SEARCH_UNAVAILABLE_LINE = "Search unavailable: {message} — retryable"


async def _read_structure(
    mode: str,
    user_id: str | None,
    user_identifier: str | None,
    query: str | None,
    top_k: int,
    max_hops: int,
    max_docs: int,
) -> MemoryStructure:
    """Read the rows the mode draws; an empty structure means nothing to draw."""

    client = await init_mongodb(
        settings.mongo.mongo_uri.get_secret_value(),
        settings.mongo.mongo_initdb_database,
    )
    database = settings.mongo.mongo_initdb_database
    resolved = await resolve_user_id(user_id, user_identifier)

    # No pre-warm here, on purpose (ADR-009 §11): a script issues ONE query, so
    # the Warm gate's warm-at-use pays the cold start exactly once.
    if mode == "rag":
        if not query:
            return await fetch_rag_structure(
                client, database, resolved, max_docs=max_docs
            )
        logger.info(
            "Retrieving parents for user_id=%s: %r (top_k=%d)", resolved, query, top_k
        )
        retrieval = await retrieve_parents(
            client, database, query, get_embedding_model(), resolved, top_k=top_k
        )
        if retrieval.outcome == "nothing_found" or not retrieval.parents:
            return MemoryStructure()
        return await fetch_retrieval_structure(client, database, resolved, retrieval)

    if query:
        logger.info(
            "Querying graph for user_id=%s: %r (top_k=%d, max_hops=%d)",
            resolved,
            query,
            top_k,
            max_hops,
        )
        return await query_memory(
            client,
            database,
            query,
            get_embedding_model(),
            resolved,
            top_k=top_k,
            max_hops=max_hops,
        )
    logger.info("No query provided — loading full graph for user_id=%s", resolved)
    return await fetch_full_graph(client, database, resolved, max_docs=max_docs)


async def _run(
    user_id: str | None,
    user_identifier: str | None,
    query: str | None,
    top_k: int,
    max_hops: int,
    max_docs: int,
    output: str | None,
    no_open: bool,
) -> None:
    mode = app_config.memory.mode
    try:
        result = await _read_structure(
            mode, user_id, user_identifier, query, top_k, max_hops, max_docs
        )
    except SearchUnavailableError as exc:
        click.echo(SEARCH_UNAVAILABLE_LINE.format(message=exc))
        raise SystemExit(1) from exc

    if not result.nodes and not result.edges:
        click.echo(
            NO_RESULTS_MESSAGE.format(query=query) if query else EMPTY_MEMORY_MESSAGE
        )
        raise SystemExit(1)

    logger.info("Result: %d nodes, %d edges", len(result.nodes), len(result.edges))
    path = visualize_query_result(
        result,
        output,
        open_browser=not no_open,
        query=query or ("structure" if mode == "rag" else "graph"),
        document_order="relevance" if query else "recency",
    )
    click.echo(f"Wrote {path}")


@click.command()
@user_options
@click.option(
    "--query",
    "-q",
    default=None,
    help="Search query. Omit to draw the most-recent documents' structure.",
)
@click.option(
    "--top-k",
    "-k",
    type=click.IntRange(min=1),
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
    "--max-docs",
    type=click.IntRange(min=1),
    default=app_config.query.full_graph_max_docs,
    show_default=True,
    help=(
        "Most-recent documents embedded with no --query; the browser's "
        "Documents slider reveals up to this many. Ignored with --query."
    ),
)
@click.option(
    "--output",
    "-o",
    default=None,
    help=(
        "Output HTML file. Defaults to "
        ".tree/graphs/<slug>-<UTC-stamp>.html under the repo root."
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
    max_docs: int,
    output: str | None,
    no_open: bool,
) -> None:
    """Render the resolved user's memory structure, in the configured memory mode."""

    asyncio.run(
        _run(
            user_id, user_identifier, query, top_k, max_hops, max_docs, output, no_open
        )
    )


if __name__ == "__main__":
    main()
