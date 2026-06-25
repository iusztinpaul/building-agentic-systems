"""
Trigger a one-off ONLINE (realtime) DATA-pipeline ingestion of a single source.

Routes one source through the realtime data-layer router ``online_ingest`` into the
``documents`` collection ONLY. It does NOT extract or index — that is the memory
pipeline's job; run ``make memory-run-memory-pipeline-extraction-online
DOC_IDS=<id>`` with the printed document id next. (The MCP ingest tools fire
extraction automatically as a realtime convenience; this CLI keeps the data step
separate so the two pipelines stay decoupled.)

``--source`` is auto-detected at this CLI boundary: an ``http(s)://`` URL routes
through the web/substack/youtube dispatcher; anything else is treated as a local
file path (``.txt`` / ``.md`` / ``.html``). Conversation ingestion stays MCP-only
(pasting a whole conversation on argv is impractical).

Every write is scoped to a ``user_id`` (#020): defaults to the current-session
user; override with ``USER_ID=<ObjectId>`` or ``USER_IDENTIFIER=<handle>`` (the
Makefile wires these). See :func:`tree.entities.sessions.resolve_user_id` for the
resolution precedence.

Requires:
    - Prefect server + Mongo running (make local-start)

Usage:
    make memory-run-data-pipeline-online SOURCE="https://www.decodingai.com/p/some-post"
    make memory-run-data-pipeline-online SOURCE="/path/to/notes.md" TITLE="My notes"
    uv run python scripts/run_online_ingest.py --source https://example.com
"""

import asyncio
import logging
from urllib.parse import urlparse

import click

from tree.config.settings import settings
from tree.data.online_pipeline import (
    FileSource,
    OnlineSource,
    UrlSource,
    online_ingest,
)
from tree.db import init_mongodb
from tree.entities.sessions import resolve_user_id
from tree.logging import init_logger
from tree.observability import flush_opik

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
        "Online data ingest: %s source for user_id=%s (%s)",
        online_source.type,
        resolved_user_id,
        source,
    )

    document = await online_ingest(online_source, resolved_user_id)
    # online_ingest owns the Opik span; the CLI process (no MCP/flow lifecycle)
    # flushes it before exit so the trace actually ships.
    flush_opik()
    if document is None:
        logger.info("Already ingested (duplicate): %s", source)
        return

    # Data step only — NOT extracted/indexed. Point the user at the memory step.
    logger.info(
        "Ingested document into `documents` (NOT extracted/indexed):\n"
        "  id         : %s\n"
        "  source_uri : %s\n"
        "Next, extract it into the knowledge graph:\n"
        "  make memory-run-memory-pipeline-extraction-online DOC_IDS=%s",
        document.id,
        document.source_uri,
        document.id,
    )
    # Deliberate machine-readable STDOUT emit (NOT a stray debug print): the bare
    # doc id is the `run-online` chain's contract — logs go to STDERR, so this is
    # the only thing on STDOUT for `make run-online` to capture and chain
    # extraction. A duplicate (document is None) prints nothing here.
    print(document.id)


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
