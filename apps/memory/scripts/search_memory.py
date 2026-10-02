"""
Search one user's memory and print the ranked **Parent chunk**s as text.

Runs **Parent-document retrieval** (``retrieve_parents``) — Chapter 4's
retrieval — and prints one block per parent: score, title — heading path, a
300-char excerpt and the matched-children count. MODE-NEUTRAL: there is no mode
branch, because parents and children are the same rows in ``rag`` and
``graphrag`` (ADR-006 §3), so the command answers identically in both. It writes
no file; the picture of the same query is ``make memory-visualize-structure
QUERY=…``.

Every read is scoped to a single ``user_id`` (#020). It defaults to the
current-session user; override with ``USER_ID=<ObjectId>`` or
``USER_IDENTIFIER=<handle>`` (the Makefile wires these for you).

Usage:
    make memory-search QUERY="What does Paul work on?"
    make memory-search QUERY="MLOps" TOP_K=5 USER_IDENTIFIER=paul
    uv run python scripts/search_memory.py --query "MLOps" --top-k 5
"""

import asyncio
import logging
import textwrap

import click

from tree.cli import user_options
from tree.config.app_config import app_config
from tree.config.settings import settings
from tree.db import init_mongodb
from tree.entities.sessions import resolve_user_id
from tree.logging import init_logger
from tree.memory.rag.retrieval import retrieve_parents
from tree.memory.rag.search import SearchUnavailableError
from tree.memory.rag.types import RetrievalResult, RetrievedParent
from tree.models.get_model import get_embedding_model

init_logger()
logger = logging.getLogger(__name__)

# The one caveat line a degraded query prints before its results (ADR-008 §3):
# a text_only / vector_only answer ranked half the index, so "nothing relevant"
# is not a safe read of a short list. Printed FIRST, and also above
# "No results." — degraded-and-empty is exactly when the operator needs it.
DEGRADED_SEARCH_CAVEAT = (
    "Search ran {mode} — the other leg was unavailable; results may miss matches."
)

# BOTH legs down (``SearchUnavailableError``) is an operator-facing outcome, not
# a bug: the terminal gets the MCP tools' ``retryable`` hint as ONE line and
# exit 1, instead of a traceback that reads like a crash. No envelope JSON —
# that shape is for the model (ADR-008 §2), not for a human at a prompt.
SEARCH_UNAVAILABLE_LINE = "Search unavailable: {message} — retryable"

# How much of a parent to show per hit. A parent is ~4096 tokens; the terminal
# is a ranking view, not a reader, so it prints an excerpt and the operator
# opens the source if the hit looks right.
_EXCERPT_CHARS = 300


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
    user_id: str | None, user_identifier: str | None, query: str, top_k: int
) -> None:
    client = await init_mongodb(
        settings.mongo.mongo_uri.get_secret_value(),
        settings.mongo.mongo_initdb_database,
    )
    database = settings.mongo.mongo_initdb_database
    resolved = await resolve_user_id(user_id, user_identifier)

    # No pre-warm here, on purpose (ADR-009 §11): a script issues ONE query, so
    # the Warm gate's warm-at-use already pays the cold start exactly once.
    logger.info(
        "Retrieving parents for user_id=%s: %r (top_k=%d)", resolved, query, top_k
    )
    try:
        result = await retrieve_parents(
            client, database, query, get_embedding_model(), resolved, top_k=top_k
        )
    except SearchUnavailableError as exc:
        click.echo(SEARCH_UNAVAILABLE_LINE.format(message=exc))
        raise SystemExit(1) from exc
    _print_parents(result)


@click.command()
@user_options
@click.option("--query", "-q", required=True, help="Search query.")
@click.option(
    "--top-k",
    "-k",
    type=click.IntRange(min=1),
    default=app_config.query.top_k,
    show_default=True,
    help="Maximum number of parent chunks to print.",
)
def main(
    user_id: str | None, user_identifier: str | None, query: str, top_k: int
) -> None:
    """Print the resolved user's best-matching parent chunks for a query."""

    asyncio.run(_run(user_id, user_identifier, query, top_k))


if __name__ == "__main__":
    main()
