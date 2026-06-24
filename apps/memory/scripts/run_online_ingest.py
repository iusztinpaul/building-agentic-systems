"""
Trigger a one-off ONLINE (realtime) ingestion of a single source.

The CLI counterpart to the MCP ingest tools: routes one source through
``online_ingest`` (the realtime data-layer router) then ``submit_ingestion``
(fires the ``memory-extract-etl-orchestrator`` deployment) — exactly what the MCP
``ingest_url`` / ``ingest_file`` tools do, but from the terminal.

``--source`` is auto-detected at this CLI boundary: an ``http(s)://`` URL routes
through the web/substack/youtube dispatcher; anything else is treated as a local
file path (``.txt`` / ``.md`` / ``.html``). Conversation ingestion stays MCP-only
(pasting a whole conversation on argv is impractical).

Every write is scoped to a ``user_id`` (#020): defaults to the current-session
user; override with ``USER_ID=<ObjectId>`` or ``USER_IDENTIFIER=<handle>`` (the
Makefile wires these). See :mod:`scripts._users` for the resolution precedence.

Requires:
    - Prefect server running (make local-start)
    - Workflows served (make memory-serve-workflows) — for the extraction submit

Usage:
    make memory-run-online-ingest SOURCE="https://www.decodingai.com/p/some-post"
    make memory-run-online-ingest SOURCE="/path/to/notes.md" TITLE="My notes"
    uv run python scripts/run_online_ingest.py --source https://example.com
"""

import asyncio
import json
import logging
from urllib.parse import urlparse

import click

from _users import resolve_user_id
from tree.config.settings import settings
from tree.data.online_pipeline import (
    FileSource,
    OnlineSource,
    UrlSource,
    online_ingest,
)
from tree.db import init_mongodb
from tree.logging import init_logger
from tree.mcp.ingest import submit_ingestion

init_logger()
logger = logging.getLogger(__name__)


def _build_source(source: str, title: str | None) -> OnlineSource:
    """URL → ``UrlSource``; anything else → ``FileSource`` (CLI-boundary detect).

    ``title`` applies only to files (a URL's title comes from the fetched page).
    """

    scheme = urlparse(source).scheme.lower()
    if scheme in {"http", "https"}:
        return UrlSource(uri=source)
    return FileSource(path=source, title=title)


async def _run(
    source: str,
    title: str | None,
    user_id: str | None,
    user_identifier: str | None,
) -> None:
    await init_mongodb(
        settings.mongo.mongo_uri.get_secret_value(),
        settings.mongo.mongo_initdb_database,
    )
    resolved_user_id = await resolve_user_id(user_id, user_identifier)

    online_source = _build_source(source, title)
    logger.info(
        "Online ingest: %s source for user_id=%s (%s)",
        online_source.type,
        resolved_user_id,
        source,
    )

    document = await online_ingest(online_source, resolved_user_id)
    if document is None:
        logger.info("Already ingested (duplicate); nothing submitted: %s", source)
        return

    result = await submit_ingestion(document, user_id=resolved_user_id)
    logger.info("Ingested + submitted extraction:\n%s", json.dumps(result, indent=2))


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
def main(
    source: str,
    title: str | None,
    user_id: str | None,
    user_identifier: str | None,
) -> None:
    """Ingest one URL or file through the realtime online path for the resolved user."""

    asyncio.run(_run(source, title, user_id, user_identifier))


if __name__ == "__main__":
    main()
