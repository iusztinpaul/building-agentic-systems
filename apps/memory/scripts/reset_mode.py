"""
**Mode reset** for ALL users: the one supported way to switch a deployment
between `rag` and `graphrag` (ADR-006 §5, ADR-012 §3 — no migration, a rebuild).

Drops `memory` + `memory_clusters` and keeps `documents` + `users` (reasons in
`tree.db`), so every Document is pending again and the next pipeline run
rebuilds the memory in the CONFIGURED `memory.mode`. The mode is one per
deployment, so there is no `USER_ID` here.

The four-step order:

    1. set the new mode (YAML, or TREE_MEMORY__MODE exported in the SERVING shell)
    2. make memory-reset-mode CONFIRM=yes
    3. re-serve workflows (make memory-serve-workflows)
    4. make memory-run-pipeline

DESTRUCTIVE, so it is a DRY RUN unless the confirmation token matches: `yes` on
a local target, `prod` when the env target is prod OR the Mongo scheme is
`mongodb+srv` (an Atlas URI is prod data whatever `.env.target` says — and
direnv exports the prod vars into every shell). Either way it first prints the
env target, the redacted host, the database, the configured mode and the row
counts. A dry run exits 1.

Needs no follow-up beyond step 4: `person:self` comes back at the next graphrag
run (`User.ensure_self_person`), Beanie recreates the classic indexes at the
next boot and the indexing run recreates `vector_index`. Prefect's task caches
are left alone ON PURPOSE — no cache key carries the mode, chunking and child
embeddings are identical in both modes (ADR-006 §3), so the rebuild replays
them from cache at no Voyage cost. Do not add a cache purge here.

Usage:
    make memory-reset-mode                    # dry run: what would go?
    make memory-reset-mode CONFIRM=yes        # drop (local target)
    uv run python scripts/reset_mode.py --env-target local --confirm yes
"""

import asyncio
import logging

import click
from pymongo import AsyncMongoClient

from tree.config.settings import settings
from tree.db import ModeResetReport, reset_memory_mode
from tree.logging import init_logger

init_logger()
logger = logging.getLogger(__name__)


def _required_token(env_target: str) -> str:
    if env_target == "prod" or settings.mongo.mongo_scheme == "mongodb+srv":
        return "prod"
    return "yes"


def _log_block(report: ModeResetReport) -> None:
    logger.info(
        "Mode reset — env target: %s (%s) · database: %s · "
        "memory.mode (configured): %s · ALL users",
        report.env_target,
        report.target,
        report.database,
        report.configured_mode,
    )
    for name, rows in report.dropped.items():
        logger.info("  %-16s %6d rows  → DROP", f"{name}:", rows)
    for name, rows in report.kept.items():
        suffix = (
            f" ({report.pending_documents} become pending)"
            if name == "documents"
            else ""
        )
        logger.info("  %-16s %6d rows  → kept%s", f"{name}:", rows, suffix)


async def _run(env_target: str, confirm: str | None) -> None:
    required = _required_token(env_target)
    database = settings.mongo.mongo_initdb_database
    client = AsyncMongoClient(settings.mongo.mongo_uri.get_secret_value())
    try:
        report = await reset_memory_mode(
            client, database, env_target=env_target, dry_run=True
        )
        _log_block(report)

        if confirm != required:
            if required == "prod":
                logger.info("Dry run: nothing dropped.")
                logger.info("Target is prod — re-run with CONFIRM=prod to drop it.")
            else:
                logger.info(
                    "Dry run: nothing dropped. Re-run with CONFIRM=yes to drop "
                    "both collections."
                )
            raise SystemExit(1)

        report = await reset_memory_mode(
            client, database, env_target=env_target, dry_run=False
        )
    finally:
        await client.close()

    logger.info(
        "Next: set memory.mode, re-serve workflows (make memory-serve-workflows), "
        "then make memory-run-pipeline — %d document(s) are pending.",
        report.pending_documents,
    )


@click.command()
@click.option(
    "--env-target",
    type=click.Choice(["local", "prod", "unknown"]),
    default="unknown",
    show_default=True,
    help="The active env target (the Makefile passes it). `unknown` leaves the "
    "prod decision to the Mongo scheme alone.",
)
@click.option(
    "--confirm",
    default=None,
    help="Confirmation token: `yes` on a local target, `prod` on prod. Anything "
    "else is a dry run.",
)
def main(env_target: str, confirm: str | None) -> None:
    """Drop the mode-bound collections for ALL users (dry run without a token)."""

    asyncio.run(_run(env_target, confirm))


if __name__ == "__main__":
    main()
