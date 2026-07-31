"""
Run the FULL pipeline end-to-end (data ingest → extraction → index).

A light CLI shim over the two cross-pipeline glue modules — everything is
controlled there, this script only picks the dispatcher:

* ``--mode offline`` (default) → :func:`tree.offline.dispatch_offline_ingest`:
  ONE ``etl-offline`` flow run over the selected config sources
  (``--source-file``/``--uri``; neither → the default backfill+listen set),
  with ``--num-shards`` extraction fan-out per user.
* ``--mode online`` → :func:`tree.online.dispatch_online_ingest`: ONE
  ``etl-online`` flow run for ONE ``--source`` (URL or local file, read here
  at the edge) that ingests AND runs extraction inline, then submits the
  trailing indexing run. A duplicate source skips extraction.

Both dispatchers submit their deployment fire-and-forget; this CLI then blocks
streaming the run's logs (exit non-zero on failure) — waiting is a caller
concern. Where the optional deployment isn't registered (e.g. free-tier
Prefect Cloud), the dispatcher runs the SAME flow in-process instead and the
script reports the synchronous result.

Every write is scoped to a ``user_id`` (#020): defaults to the current-session
user; override with ``USER_ID=<ObjectId>`` or ``USER_IDENTIFIER=<handle>``
(the Makefile wires these).

Requires:
    - Prefect server + Mongo running (make local-start)
    - Workflows served (make memory-serve-workflows)

Usage:
    make memory-run-pipeline                                       # offline, default sources
    make memory-run-pipeline SOURCE_FILE="sources/listen.yaml" NUM_SHARDS=2
    make memory-run-pipeline MODE=online SOURCE="https://www.decodingai.com/p/some-post"
    uv run python scripts/run_pipeline.py --mode online --source /path/to/notes.md
"""

import asyncio
import logging
import sys
from typing import Any

import click

from tree.cli import (
    MODE_ONLINE,
    build_online_source,
    connect_and_resolve_user,
    mode_option,
    user_options,
    wait_for_dispatch,
)
from tree.config.sources import build_uri_sources, parse_uri_token
from tree.logging import init_logger
from tree.observability import flush_opik
from tree.offline import dispatch_offline_ingest
from tree.online import dispatch_online_ingest

init_logger()
logger = logging.getLogger(__name__)


async def _run_offline(
    user_id: str | None,
    user_identifier: str | None,
    source_files: list[str],
    inline_sources: list[dict[str, Any]],
    num_shards: int,
) -> None:
    resolved_user_id = await connect_and_resolve_user(user_id, user_identifier)
    result = await dispatch_offline_ingest(
        user_id=resolved_user_id,
        source_files=source_files or None,
        sources=inline_sources or None,
        num_shards=num_shards,
    )
    await wait_for_dispatch(result)


async def _run_online(
    user_id: str | None,
    user_identifier: str | None,
    source: str,
    title: str | None,
) -> None:
    resolved_user_id = await connect_and_resolve_user(user_id, user_identifier)
    online_source = build_online_source(source, title)
    try:
        result = await dispatch_online_ingest(
            online_source, resolved_user_id, run_extraction=True
        )
    except ValueError as exc:
        logger.error("Invalid source: %s", exc)
        sys.exit(1)
    await wait_for_dispatch(result)
    # The dispatcher's spans belong to this short-lived process — flush before exit.
    flush_opik()


@click.command()
@mode_option
@user_options
@click.option(
    "--source-file",
    "source_files",
    multiple=True,
    help=(
        "[offline] Repeatable. A committed source file to ingest (e.g. "
        "``sources/backfill.yaml``). Combine freely with ``--uri``; pass neither "
        "to ingest the default backfill+listen set."
    ),
)
@click.option(
    "--uri",
    "uris",
    multiple=True,
    help=(
        "[offline] Repeatable. An ad-hoc source URL, optionally suffixed ``=TYPE`` "
        "(e.g. ``https://blog.com/feed=substack_rss``); inferred otherwise. "
        "``huggingface_dataset`` is rejected — define those in a YAML file."
    ),
)
@click.option(
    "--num-shards",
    default=1,
    show_default=True,
    help="[offline] Extraction fan-out width (forwarded per user; ``>= 1``).",
)
@click.option(
    "--source",
    default=None,
    help="[online] The ONE source to ingest: http(s) URL or local file path.",
)
@click.option(
    "--title",
    default=None,
    help="[online] Optional title override (local files only).",
)
def main(
    mode: str,
    user_id: str | None,
    user_identifier: str | None,
    source_files: tuple[str, ...],
    uris: tuple[str, ...],
    num_shards: int,
    source: str | None,
    title: str | None,
) -> None:
    """Run the full pipeline end-to-end: offline batch or one online source."""

    if mode == MODE_ONLINE:
        if not source:
            raise click.UsageError("--mode online requires --source '<url|path>'.")
        if source_files or uris:
            raise click.UsageError("--source-file/--uri are offline-only selectors.")
        asyncio.run(_run_online(user_id, user_identifier, source, title))
        return

    if source or title:
        raise click.UsageError("--source/--title are online-only (pass --mode online).")
    if num_shards < 1:
        raise click.UsageError(f"--num-shards must be >= 1 (got {num_shards}).")
    # Parse + build inline sources from --uri tokens up front so a bad token
    # (e.g. an explicit huggingface_dataset) fails fast BEFORE any flow runs.
    specs = [parse_uri_token(token) for token in uris]
    inline_sources = [s.model_dump() for s in build_uri_sources(specs)]
    asyncio.run(
        _run_offline(
            user_id, user_identifier, list(source_files), inline_sources, num_shards
        )
    )


if __name__ == "__main__":
    main()
