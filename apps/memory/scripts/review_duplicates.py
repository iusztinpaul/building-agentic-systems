"""Human-review CLI for flagged SAME_AS pairs.

Usage:
    # List pending pairs.
    uv --directory apps/memory run python scripts/review_duplicates.py list \
        [--entity-type person] [--limit 10]

    # Confirm or reject a specific pair.
    uv --directory apps/memory run python scripts/review_duplicates.py confirm \
        person:alice person:alice s --reviewed-by alice@example.com \
        [--strategy keep_primary|merge_properties|keep_aliases]

    uv --directory apps/memory run python scripts/review_duplicates.py reject \
        person:bob person:bobby --reviewed-by alice@example.com

    # Interactive walk (no subcommand): prompt for reviewer name once, then
    # walk pending pairs one at a time.
    uv --directory apps/memory run python scripts/review_duplicates.py

Calls :func:`tree.logging.init_logger` at module level per project
convention so ``logger.info`` calls in the review module surface.
"""

from __future__ import annotations

import asyncio
import logging

import click

from tree.logging import init_logger

init_logger()

from tree.config.settings import settings  # noqa: E402
from tree.db import init_mongodb  # noqa: E402
from tree.entities.knowledge_graph import NodeType  # noqa: E402
from tree.memory.review import (  # noqa: E402
    MergeStrategy,
    PendingDuplicate,
    ReviewDecision,
    ReviewResult,
    find_pending_duplicates,
    review_duplicate,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------


def _format_pending_row(p: PendingDuplicate) -> str:
    """Render one pending pair as a single-line table row."""

    return (
        f"  [{p.similarity_score:.3f} {p.match_type:<9}] "
        f"{p.source_node_id}  <->  {p.target_node_id}"
        f"   ({p.source_name!r} vs {p.target_name!r})"
    )


def _print_pending(pending: list[PendingDuplicate]) -> None:
    if not pending:
        click.echo("No pending duplicates.")
        return
    click.echo(f"Pending duplicates ({len(pending)}):")
    for p in pending:
        click.echo(_format_pending_row(p))


def _format_review_result(result: ReviewResult) -> str:
    if result.decision is ReviewDecision.CONFIRM:
        return (
            f"CONFIRMED: winner={result.winner_node_id} "
            f"loser={result.loser_node_id} "
            f"strategy={result.applied_strategy} "
            f"edges_transferred={result.edges_transferred} "
            f"edge_id={result.same_as_edge_id}"
        )
    return f"REJECTED: edge_id={result.same_as_edge_id}"


# ---------------------------------------------------------------------------
# Async runners
# ---------------------------------------------------------------------------


async def _list_async(entity_type: NodeType | None, limit: int) -> None:
    client = await init_mongodb(
        settings.mongo.mongo_uri.get_secret_value(),
        settings.mongo.mongo_initdb_database,
    )
    try:
        database = client[settings.mongo.mongo_initdb_database]
        pending = await find_pending_duplicates(
            database, entity_type=entity_type, limit=limit
        )
        _print_pending(pending)
    finally:
        await client.close()


async def _review_async(
    *,
    source_node_id: str,
    target_node_id: str,
    decision: ReviewDecision,
    reviewed_by: str,
    merge_strategy: MergeStrategy,
) -> None:
    client = await init_mongodb(
        settings.mongo.mongo_uri.get_secret_value(),
        settings.mongo.mongo_initdb_database,
    )
    try:
        database = client[settings.mongo.mongo_initdb_database]
        result = await review_duplicate(
            database,
            source_node_id=source_node_id,
            target_node_id=target_node_id,
            decision=decision,
            reviewed_by=reviewed_by,
            merge_strategy=merge_strategy,
        )
        click.echo(_format_review_result(result))
    finally:
        await client.close()


async def _interactive_async(reviewed_by: str, limit: int) -> None:
    client = await init_mongodb(
        settings.mongo.mongo_uri.get_secret_value(),
        settings.mongo.mongo_initdb_database,
    )
    confirmed = 0
    rejected = 0
    skipped = 0
    try:
        database = client[settings.mongo.mongo_initdb_database]
        pending = await find_pending_duplicates(database, limit=limit)
        if not pending:
            click.echo("No pending duplicates. Nothing to review.")
            return

        click.echo(f"Walking {len(pending)} pending pair(s). c/r/s/q.")
        for p in pending:
            click.echo("")
            click.echo(_format_pending_row(p))
            choice = click.prompt(
                "Decision",
                type=click.Choice(["c", "r", "s", "q"], case_sensitive=False),
                default="s",
                show_default=True,
            ).lower()

            if choice == "q":
                click.echo("Quitting.")
                break
            if choice == "s":
                skipped += 1
                continue

            if choice == "c":
                strategy_str = click.prompt(
                    "Merge strategy",
                    type=click.Choice(
                        [s.value for s in MergeStrategy], case_sensitive=False
                    ),
                    default=MergeStrategy.KEEP_PRIMARY.value,
                    show_default=True,
                )
                strategy = MergeStrategy(strategy_str)
                try:
                    result = await review_duplicate(
                        database,
                        source_node_id=p.source_node_id,
                        target_node_id=p.target_node_id,
                        decision=ReviewDecision.CONFIRM,
                        reviewed_by=reviewed_by,
                        merge_strategy=strategy,
                    )
                except ValueError as exc:
                    click.echo(f"  ERROR: {exc}")
                    skipped += 1
                    continue
                click.echo(f"  {_format_review_result(result)}")
                confirmed += 1
                continue

            # choice == "r"
            try:
                result = await review_duplicate(
                    database,
                    source_node_id=p.source_node_id,
                    target_node_id=p.target_node_id,
                    decision=ReviewDecision.REJECT,
                    reviewed_by=reviewed_by,
                    merge_strategy=MergeStrategy.KEEP_PRIMARY,
                )
            except ValueError as exc:
                click.echo(f"  ERROR: {exc}")
                skipped += 1
                continue
            click.echo(f"  {_format_review_result(result)}")
            rejected += 1
    finally:
        await client.close()

    click.echo("")
    click.echo(
        f"Summary: {confirmed} confirmed, {rejected} rejected, {skipped} skipped."
    )


# ---------------------------------------------------------------------------
# Click CLI
# ---------------------------------------------------------------------------


@click.group(invoke_without_command=True)
@click.option(
    "--limit",
    type=int,
    default=50,
    show_default=True,
    help="Maximum number of pending pairs to walk in interactive mode.",
)
@click.pass_context
def main(ctx: click.Context, limit: int) -> None:
    """Review flagged duplicate pairs. Runs the interactive walk by default."""

    if ctx.invoked_subcommand is not None:
        return

    reviewed_by = click.prompt("Reviewer name (email or handle)", type=str)
    asyncio.run(_interactive_async(reviewed_by=reviewed_by, limit=limit))


@main.command("list")
@click.option(
    "--entity-type",
    type=click.Choice([t.value for t in NodeType], case_sensitive=False),
    default=None,
    help="Filter by entity type.",
)
@click.option(
    "--limit",
    type=int,
    default=50,
    show_default=True,
    help="Maximum number of rows to print.",
)
def list_cmd(entity_type: str | None, limit: int) -> None:
    """List pending SAME_AS pairs."""

    entity_filter = NodeType(entity_type) if entity_type else None
    asyncio.run(_list_async(entity_type=entity_filter, limit=limit))


@main.command("confirm")
@click.argument("source")
@click.argument("target")
@click.option(
    "--reviewed-by",
    required=True,
    help="Reviewer identifier (email or handle).",
)
@click.option(
    "--strategy",
    type=click.Choice([s.value for s in MergeStrategy], case_sensitive=False),
    default=MergeStrategy.KEEP_PRIMARY.value,
    show_default=True,
    help="Merge strategy.",
)
def confirm_cmd(source: str, target: str, reviewed_by: str, strategy: str) -> None:
    """Confirm a pending SAME_AS pair as a true duplicate."""

    asyncio.run(
        _review_async(
            source_node_id=source,
            target_node_id=target,
            decision=ReviewDecision.CONFIRM,
            reviewed_by=reviewed_by,
            merge_strategy=MergeStrategy(strategy),
        )
    )


@main.command("reject")
@click.argument("source")
@click.argument("target")
@click.option(
    "--reviewed-by",
    required=True,
    help="Reviewer identifier (email or handle).",
)
def reject_cmd(source: str, target: str, reviewed_by: str) -> None:
    """Reject a pending SAME_AS pair (mark as not-a-duplicate)."""

    asyncio.run(
        _review_async(
            source_node_id=source,
            target_node_id=target,
            decision=ReviewDecision.REJECT,
            reviewed_by=reviewed_by,
            merge_strategy=MergeStrategy.KEEP_PRIMARY,
        )
    )


if __name__ == "__main__":
    main()
