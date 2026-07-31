"""
Trigger a one-off ONLINE (realtime) ingestion of a single source.

Glue over the realtime dispatcher ``dispatch_online_ingest``: it submits the
``etl-online`` Prefect deployment (a worker runs the data step and — with
``--run-extraction`` — runs the extraction worker inline and submits the
trailing indexing run), then blocks
streaming the run's logs until it reaches a final state, exiting non-zero on
failure. Where the deployment is not registered (e.g. free-tier Prefect Cloud),
the dispatcher runs the SAME flow in-process instead and the script simply
reports the synchronous result.

``--source`` is auto-detected at this CLI boundary: an ``http(s)://`` URL routes
through the web/substack/youtube dispatcher; anything else is treated as a local
file path (``.txt`` / ``.md`` / ``.html``). Files are READ HERE, at the edge
where they exist. Conversation ingestion stays MCP-only (pasting a whole
conversation on argv is impractical).

Every write is scoped to a ``user_id`` (#020): defaults to the current-session
user; override with ``USER_ID=<ObjectId>`` or ``USER_IDENTIFIER=<handle>`` (the
Makefile wires these). See :func:`tree.entities.sessions.resolve_user_id` for the
resolution precedence.

Requires:
    - Prefect server + Mongo running (make local-start)
    - A served worker (make memory-serve-workflows) for deployment mode

Usage:
    make memory-run-data-pipeline-online SOURCE="https://www.decodingai.com/p/some-post"
    make memory-run-online SOURCE="/path/to/notes.md" TITLE="My notes"
    uv run python scripts/run_online_ingest.py --source https://example.com
"""

import asyncio
import logging
import sys
from pathlib import Path
from urllib.parse import urlparse

import click
from prefect.client.orchestration import get_client
from prefect.client.schemas.filters import LogFilter, LogFilterFlowRunId

from tree.config.settings import settings
from tree.data.file.file import read_file
from tree.data.online_pipeline import (
    FileSource,
    OnlineSource,
    UrlSource,
)
from tree.db import init_mongodb
from tree.online import dispatch_online_ingest
from tree.entities.sessions import resolve_user_id
from tree.logging import init_logger
from tree.observability import flush_opik

init_logger()
logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 2


def _build_source(source: str, title: str | None) -> OnlineSource:
    """URL → ``UrlSource``; anything else → ``FileSource`` (CLI-boundary detect).

    Files are READ HERE, at the edge where they exist — the pipeline only
    carries the text payload (``FileSource.content``); ``path`` is identity.
    Resolved to an absolute path so the dedup key (source_uri) is stable
    regardless of the CLI's working directory.

    ``title`` applies only to files (a URL's title comes from the fetched page).
    """

    scheme = urlparse(source).scheme.lower()
    if scheme in {"http", "https"}:
        return UrlSource(uri=source)
    path = str(Path(source).resolve())
    return FileSource(path=path, content=read_file(path), title=title)


async def _wait_for_flow_run(flow_run_id: str) -> None:
    """Stream a flow run's logs and block until it is final; exit 1 on failure.

    The same poll loop as ``run_data_pipeline.py``: the run executes on a worker,
    so its logs live in Prefect — mirror them here so errors surface in THIS
    terminal instead of only in the Prefect UI.
    """

    async with get_client() as client:
        base_url = str(client.api_url).rstrip("/").removesuffix("/api")
        logger.info("Track at: %s/runs/flow-run/%s", base_url, flow_run_id)

        log_filter = LogFilter(flow_run_id=LogFilterFlowRunId(any_=[flow_run_id]))
        log_offset = 0

        while True:
            logs = await client.read_logs(
                log_filter=log_filter, offset=log_offset, limit=100
            )
            for log in logs:
                logger.info(
                    "%s | %s | %s",
                    f"{log.timestamp:%Y-%m-%d %H:%M:%S}",
                    f"{logging.getLevelName(log.level):7s}",
                    log.message,
                )
            log_offset += len(logs)

            run = await client.read_flow_run(flow_run_id)
            if run.state and run.state.is_final():
                if run.state.is_completed():
                    logger.info("Done. Flow completed successfully.")
                else:
                    logger.error("Flow finished with state: %s", run.state.name)
                    sys.exit(1)
                break
            await asyncio.sleep(POLL_INTERVAL_SECONDS)


async def _run(
    source: str,
    title: str | None,
    user_id: str | None,
    user_identifier: str | None,
    run_extraction: bool,
) -> None:
    await init_mongodb(
        settings.mongo.mongo_uri.get_secret_value(),
        settings.mongo.mongo_initdb_database,
    )
    resolved_user_id = await resolve_user_id(user_id, user_identifier)

    online_source = _build_source(source, title)
    logger.info(
        "Online ingest: %s source for user_id=%s (%s, run_extraction=%s)",
        online_source.type,
        resolved_user_id,
        source,
        run_extraction,
    )

    try:
        result = await dispatch_online_ingest(
            online_source, resolved_user_id, run_extraction=run_extraction
        )
    except ValueError as exc:
        logger.error("Invalid source: %s", exc)
        sys.exit(1)

    if result["mode"] == "deployment":
        logger.info("Submitted flow run %s; waiting for it...", result["flow_run_id"])
        await _wait_for_flow_run(result["flow_run_id"])
    else:
        # Inline fallback already ran the flow to completion in this process.
        logger.info("Ran in-process (no deployment registered): %s", result)
    # The dispatcher's spans belong to this short-lived process — flush before exit.
    flush_opik()


@click.command()
@click.option(
    "--source",
    required=True,
    help="URL (http/https) or local file path (.txt/.md/.html) to ingest.",
)
@click.option(
    "--title",
    default=None,
    help="Optional title override (local files only; URLs use the page title).",
)
@click.option(
    "--user-id",
    default=None,
    help=(
        "Override the target tenant by Mongo ObjectId. Defaults to the "
        "current-session user; also reads the ``USER_ID`` env var."
    ),
)
@click.option(
    "--user-identifier",
    default=None,
    help=(
        "Override the target tenant by stable handle (e.g. email). Defaults to "
        "the current-session user; also reads the ``USER_IDENTIFIER`` env var."
    ),
)
@click.option(
    "--run-extraction",
    is_flag=True,
    default=False,
    help=(
        "Also run the memory-extraction step (+ submit the trailing indexing "
        "run) in the same flow run — the full online pipeline. Off = data step only."
    ),
)
def main(
    source: str,
    title: str | None,
    user_id: str | None,
    user_identifier: str | None,
    run_extraction: bool,
) -> None:
    """Ingest one URL or file through the realtime online pipeline for the resolved user."""

    asyncio.run(_run(source, title, user_id, user_identifier, run_extraction))


if __name__ == "__main__":
    main()
