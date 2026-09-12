"""
Run the memory INDEXING pipeline (embeddings backfill, search indexes).

A light CLI shim (glue lives in :mod:`tree.cli`) that dispatches the
``offline-pipeline`` flow (:mod:`tree.offline`) with the DATA and EXTRACTION
phases OFF, so the run is the indexing **Offline phase** alone (ADR-007
Decision 5): ONE ``memory-indexing-etl`` subflow per target user. It is a global
backfill over the user's unembedded rows — idempotent and safe to re-run — and
works after either offline or online extraction. No modes: indexing has no
offline/online split.

The command blocks streaming the run's logs and exits non-zero on failure. The
``offline-pipeline`` deployment is core (always registered), and dispatch needs
a reachable Prefect API — there is no in-process fallback, so a submission
failure surfaces here as an error.

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

from tree.cli import connect_and_resolve_user, user_options, wait_for_dispatch
from tree.logging import init_logger
from tree.observability import flush_opik
from tree.offline import dispatch_offline_pipeline

init_logger()
logger = logging.getLogger(__name__)


async def _run(user_id: str | None, user_identifier: str | None) -> None:
    resolved_user_id = await connect_and_resolve_user(user_id, user_identifier)
    result = await dispatch_offline_pipeline(
        user_id=resolved_user_id,
        run_data=False,
        run_extraction=False,
        run_indexing=True,
    )
    await wait_for_dispatch(result)
    # The dispatcher's spans belong to this short-lived process — flush before exit.
    flush_opik()


@click.command()
@user_options
def main(user_id: str | None, user_identifier: str | None) -> None:
    """Run the indexing phase alone, as ONE offline-pipeline run."""

    asyncio.run(_run(user_id, user_identifier))


if __name__ == "__main__":
    main()
