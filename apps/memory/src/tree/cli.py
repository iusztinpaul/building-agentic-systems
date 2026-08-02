"""
Shared glue for the entry-point scripts (``apps/memory/scripts/``).

The scripts are extremely light CLI shims over the pipeline modules
(:mod:`tree.online` / :mod:`tree.offline` / the always-registered core
deployments); everything they share lives here so each script is just Click
options + one dispatch call:

* the common ``--mode`` / ``--user-id`` / ``--user-identifier`` Click options,
* tenant resolution (Mongo init + :func:`tree.entities.sessions.resolve_user_id`),
* triggering a core deployment by name,
* streaming a flow run's logs while blocking until it is final (runs execute
  on a worker, so their logs live in Prefect — mirroring them here surfaces
  errors in the operator's terminal instead of only the Prefect UI),
* building a typed :data:`OnlineSource` from a CLI ``--source`` token.

CLI-layer semantics apply throughout: failures ``sys.exit(1)`` rather than
raise, because every caller is a terminal entry point.
"""

import asyncio
import logging
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import click
from beanie import PydanticObjectId
from prefect.client.orchestration import get_client
from prefect.client.schemas.filters import LogFilter, LogFilterFlowRunId

from tree.config.settings import settings
from tree.data.file.file import read_file
from tree.data.online_pipeline import FileSource, OnlineSource, UrlSource
from tree.db import init_mongodb
from tree.entities.sessions import resolve_user_id

logger = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 2

MODE_OFFLINE = "offline"
MODE_ONLINE = "online"


def mode_option(fn: Callable[..., Any]) -> Callable[..., Any]:
    """The shared ``--mode offline|online`` switch (default ``offline``)."""

    return click.option(
        "--mode",
        type=click.Choice([MODE_OFFLINE, MODE_ONLINE]),
        default=MODE_OFFLINE,
        show_default=True,
        help="offline = config-driven batch; online = ONE realtime source/document.",
    )(fn)


def user_options(fn: Callable[..., Any]) -> Callable[..., Any]:
    """The shared tenant-override options (#020); Makefile wires the env vars."""

    fn = click.option(
        "--user-identifier",
        default=None,
        help=(
            "Override the target tenant by stable handle (e.g. email). Defaults to "
            "the current-session user; also reads the ``USER_IDENTIFIER`` env var."
        ),
    )(fn)
    return click.option(
        "--user-id",
        default=None,
        help=(
            "Override the target tenant by Mongo ObjectId. Defaults to the "
            "current-session user; also reads the ``USER_ID`` env var."
        ),
    )(fn)


async def connect_and_resolve_user(
    user_id: str | None, user_identifier: str | None
) -> PydanticObjectId:
    """Init Mongo and resolve the target tenant — the first step of every script."""

    await init_mongodb(
        settings.mongo.mongo_uri.get_secret_value(),
        settings.mongo.mongo_initdb_database,
    )
    return await resolve_user_id(user_id, user_identifier)


def build_online_source(source: str, title: str | None) -> OnlineSource:
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


async def trigger_deployment(name: str, parameters: dict[str, Any]) -> str:
    """Create a flow run from an always-registered deployment; return its id."""

    async with get_client() as client:
        deployment = await client.read_deployment_by_name(name)
        flow_run = await client.create_flow_run_from_deployment(
            deployment_id=deployment.id,
            parameters=parameters,
        )
    logger.info("Flow run created: %s (%s)", flow_run.id, name)
    return str(flow_run.id)


async def wait_for_flow_run(flow_run_id: str) -> None:
    """Stream a flow run's logs and block until it is final; exit 1 on failure."""

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


async def wait_for_dispatch(result: dict[str, Any]) -> None:
    """Block on a ``dispatch_*_pipeline`` result — waiting stays a CLI concern.

    ``mode == "deployment"`` → stream the submitted run to completion;
    ``mode == "in_process"`` → the inline fallback already ran the flow to
    completion in this process, so just report its result.
    """

    if result["mode"] == "deployment":
        logger.info("Submitted flow run %s; waiting for it...", result["flow_run_id"])
        await wait_for_flow_run(result["flow_run_id"])
    else:
        logger.info("Ran in-process (no deployment registered): %s", result)
