"""
**Embedding reset** for ONE user: empty every persisted vector so the indexing
phase re-embeds it with the CURRENT `models.search_embedding`.

The migration for any change that makes stored vectors incomparable with new
ones — a different embedding model (voyage-3.5 -> voyage-4) or a different
**Embedding role** rule — because `memory` rows carry no model stamp
(ADR-009 §7). It touches exactly the rows the backfill refills (child chunks +
LLM-extractable entity nodes) and clears the children's `cluster_id` / `viz`, so
the **Embedding map** warns instead of drawing old-space coordinates.

DESTRUCTIVE, so it is a DRY RUN unless you pass `CONFIRM=yes` — on whichever
environment is active, production included (`make env-status`). The dry run
writes nothing and exits 1; nothing is lost either way, but re-embedding a large
tenant costs real Voyage tokens and leaves search on the text leg until the
indexing run finishes.

The three-step sequence, per user:

    make memory-reset-embeddings                  # dry run: how many rows?
    make memory-reset-embeddings CONFIRM=yes      # empty them
    make memory-run-indexing-pipeline             # re-embed
    make memory-run-clustering-pipeline           # only if you use the map

Usage:
    make memory-reset-embeddings
    make memory-reset-embeddings CONFIRM=yes USER_IDENTIFIER=paul
    uv run python scripts/reset_embeddings.py --yes --user-id 66f...
"""

import asyncio
import logging

import click

from tree.cli import user_options
from tree.config.settings import settings
from tree.db import init_mongodb
from tree.entities.sessions import resolve_user_id
from tree.logging import init_logger
from tree.memory.rag.indexing import reset_embeddings

init_logger()
logger = logging.getLogger(__name__)


async def _run(user_id: str | None, user_identifier: str | None, yes: bool) -> None:
    client = await init_mongodb(
        settings.mongo.mongo_uri.get_secret_value(),
        settings.mongo.mongo_initdb_database,
    )
    database = settings.mongo.mongo_initdb_database
    resolved_user_id = await resolve_user_id(user_id, user_identifier)

    rows = await reset_embeddings(client, database, resolved_user_id, dry_run=not yes)

    if not yes:
        logger.info(
            "Nothing written. Re-run with CONFIRM=yes to reset %d row(s).", rows
        )
        raise SystemExit(1)

    logger.info(
        "Next: make memory-run-indexing-pipeline (then "
        "make memory-run-clustering-pipeline if you use the Embedding map)."
    )


@click.command()
@user_options
@click.option(
    "--yes",
    is_flag=True,
    default=False,
    help="Actually empty the vectors. Without it the command only counts them.",
)
def main(user_id: str | None, user_identifier: str | None, yes: bool) -> None:
    """Empty the resolved user's persisted embeddings (dry run without --yes)."""

    asyncio.run(_run(user_id, user_identifier, yes))


if __name__ == "__main__":
    main()
