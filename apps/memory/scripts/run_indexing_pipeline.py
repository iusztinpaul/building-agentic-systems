"""
Run the memory INDEXING pipeline (reverse edges, embeddings, search indexes).

A light CLI shim (glue lives in :mod:`tree.cli`) over the ``memory_indexing``
flow. Since ``free-tier-deployments`` indexing is a FLOW, not a deployment: this
command runs it IN THE OPERATOR'S OWN PROCESS (an inline flow run) rather than
submitting it to a Prefect worker, so no ``serve`` process is required and the
command blocks on the real work. It is a global backfill over the user's
unembedded nodes — idempotent and safe to re-run — and works after either
offline or online extraction. No modes: indexing has no offline/online split.

Every run is scoped to a ``user_id`` (#020): defaults to the current-session
user; override with ``USER_ID=<ObjectId>`` or ``USER_IDENTIFIER=<handle>``
(the Makefile wires these).

Requires:
    - Prefect server + Mongo running (make local-start)

Usage:
    make memory-run-indexing-pipeline                       # current user
    make memory-run-indexing-pipeline USER_IDENTIFIER=paul  # override by handle
    uv run python scripts/run_indexing_pipeline.py --user-identifier paul
"""

import asyncio
import logging

import click

from tree.cli import connect_and_resolve_user, user_options
from tree.logging import init_logger
from tree.memory.indexing.pipeline import memory_indexing

init_logger()
logger = logging.getLogger(__name__)


async def _run(user_id: str | None, user_identifier: str | None) -> None:
    resolved_user_id = await connect_and_resolve_user(user_id, user_identifier)
    await memory_indexing(user_id=resolved_user_id)


@click.command()
@user_options
def main(user_id: str | None, user_identifier: str | None) -> None:
    """Run the memory_indexing flow in-process for the resolved user."""

    asyncio.run(_run(user_id, user_identifier))


if __name__ == "__main__":
    main()
