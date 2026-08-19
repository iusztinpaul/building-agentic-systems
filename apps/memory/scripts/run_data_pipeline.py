"""
Run the DATA pipeline (sources → ``documents``; NO extraction/indexing).

A light CLI shim (glue lives in :mod:`tree.cli`) with two modes:

* ``--mode offline`` (default) — dispatch the ``offline-pipeline`` flow
  (:mod:`tree.offline`) with extraction OFF over the selected config sources
  (ADR-003): ``--source-file`` (repeatable, e.g. ``sources/listen.yaml``)
  and/or ``--uri`` (repeatable, optionally suffixed ``=TYPE``, e.g.
  ``…/feed=substack_rss``); neither → the default backfill+listen set (the
  data coordinator owns source resolution). ``--uri`` tokens are parsed up
  front so a bad token (e.g. ``huggingface_dataset`` — YAML-only) fails fast
  BEFORE any flow is dispatched.
* ``--mode online`` — ingest ONE realtime ``--source`` (URL or local
  ``.txt``/``.md``/``.html`` file, read here at the edge) by dispatching the
  ``online-pipeline`` flow (:mod:`tree.online`) with extraction OFF — data step
  only, symmetric with offline mode.

Both modes block streaming the run's logs and exit non-zero on failure. The
``offline-pipeline`` / ``online-pipeline`` deployments are core (always
registered), and dispatch needs a reachable Prefect API — there is no
in-process fallback, so a submission failure surfaces here as an error.

Every write is scoped to a ``user_id`` (#020): defaults to the current-session
user; override with ``USER_ID=<ObjectId>`` or ``USER_IDENTIFIER=<handle>``
(the Makefile wires these).

Requires:
    - Prefect server + Mongo running (make local-start)
    - Workflows served (make memory-serve-workflows)

Usage:
    make memory-run-data-pipeline                                  # offline, default sources
    make memory-run-data-pipeline SOURCE_FILE="sources/listen.yaml" URI="https://x.com/a"
    make memory-run-data-pipeline MODE=online SOURCE="https://www.decodingai.com/p/some-post"
    uv run python scripts/run_data_pipeline.py --mode online --source /path/to/notes.md
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
from tree.offline import dispatch_offline_pipeline
from tree.online import dispatch_online_pipeline

init_logger()
logger = logging.getLogger(__name__)


async def _run_offline(
    user_id: str | None,
    user_identifier: str | None,
    source_files: list[str],
    inline_sources: list[dict[str, Any]],
) -> None:
    resolved_user_id = await connect_and_resolve_user(user_id, user_identifier)
    # Forward only the selectors the operator passed; with neither present the
    # data coordinator falls back to its default backfill+listen set.
    result = await dispatch_offline_pipeline(
        user_id=resolved_user_id,
        source_files=source_files or None,
        sources=inline_sources or None,
        run_extraction=False,
    )
    await wait_for_dispatch(result)
    # The dispatcher's spans belong to this short-lived process — flush before exit.
    flush_opik()


async def _run_online(
    user_id: str | None,
    user_identifier: str | None,
    source: str,
    title: str | None,
) -> None:
    resolved_user_id = await connect_and_resolve_user(user_id, user_identifier)
    online_source = build_online_source(source, title)
    try:
        result = await dispatch_online_pipeline(
            online_source, resolved_user_id, run_extraction=False
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
    source: str | None,
    title: str | None,
) -> None:
    """Run the data pipeline (documents only): offline batch or one online source."""

    if mode == MODE_ONLINE:
        if not source:
            raise click.UsageError("--mode online requires --source '<url|path>'.")
        if source_files or uris:
            raise click.UsageError("--source-file/--uri are offline-only selectors.")
        asyncio.run(_run_online(user_id, user_identifier, source, title))
        return

    if source or title:
        raise click.UsageError("--source/--title are online-only (pass --mode online).")
    # Parse + build inline sources from --uri tokens up front so a bad token
    # (e.g. an explicit huggingface_dataset) fails fast BEFORE any flow runs.
    specs = [parse_uri_token(token) for token in uris]
    inline_sources = [s.model_dump() for s in build_uri_sources(specs)]
    asyncio.run(
        _run_offline(user_id, user_identifier, list(source_files), inline_sources)
    )


if __name__ == "__main__":
    main()
