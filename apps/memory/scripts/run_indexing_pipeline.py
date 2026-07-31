"""
Run the memory INDEXING pipeline (reverse edges, embeddings, search indexes).

A light CLI shim (glue lives in :mod:`tree.cli`) over the
``memory-indexing-etl`` deployment — the single standalone indexing step that
works after either offline or online extraction (it is a global backfill over
unembedded nodes). No modes: indexing has no offline/online split.

Every run is scoped to a ``user_id`` (#020): defaults to the current-session
user; override with ``USER_ID=<ObjectId>`` or ``USER_IDENTIFIER=<handle>``
(the Makefile wires these).

Requires:
    - Prefect server + Mongo running (make local-start)
    - Workflows served (make memory-serve-workflows)

Usage:
    make memory-run-indexing-pipeline                       # current user
    make memory-run-indexing-pipeline USER_IDENTIFIER=paul  # override by handle
    uv run python scripts/run_indexing_pipeline.py --user-identifier paul
"""

import asyncio
import logging

import click

from tree.cli import (
    connect_and_resolve_user,
    trigger_deployment,
    user_options,
    wait_for_flow_run,
)
from tree.logging import init_logger

init_logger()
logger = logging.getLogger(__name__)

DEPLOYMENT_NAME = "memory-indexing-etl/memory-indexing-etl"


async def _run(user_id: str | None, user_identifier: str | None) -> None:
    resolved_user_id = await connect_and_resolve_user(user_id, user_identifier)
    flow_run_id = await trigger_deployment(
        DEPLOYMENT_NAME, {"user_id": str(resolved_user_id)}
    )
    await wait_for_flow_run(flow_run_id)


@click.command()
@user_options
def main(user_id: str | None, user_identifier: str | None) -> None:
    """Trigger the memory-indexing-etl deployment for the resolved user."""

    asyncio.run(_run(user_id, user_identifier))


if __name__ == "__main__":
    main()
