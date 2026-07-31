"""
Run the MEMORY extraction pipeline (``documents`` → knowledge graph).

A light CLI shim (glue lives in :mod:`tree.cli`) over the
``memory-extract-etl-coordinator`` deployment (#067): the coordinator resolves
the doc set, partitions it into ``min(num_shards, N)`` shards, dispatches one
``memory-extract-etl-worker`` run per shard, then fires ONE trailing
``memory-indexing-etl`` run. Two modes select the doc set:

* ``--mode offline`` (default) — batch: every PENDING document for the
  resolved user (optionally narrowed with ``--doc-ids``); ``--num-shards``
  sets the fan-out width.
* ``--mode online`` — realtime: exactly the ``--doc-ids`` you pass (required;
  e.g. the id printed by ``run_data_pipeline.py --mode online``). No shard
  fan-out — a handful of docs needs one worker.

Every run is scoped to a ``user_id`` (#020): defaults to the current-session
user; override with ``USER_ID=<ObjectId>`` or ``USER_IDENTIFIER=<handle>``
(the Makefile wires these).

Requires:
    - Prefect server + Mongo running (make local-start)
    - Workflows served (make memory-serve-workflows)

Usage:
    make memory-run-memory-pipeline                                # offline, all pending docs
    make memory-run-memory-pipeline NUM_SHARDS=4
    make memory-run-memory-pipeline MODE=online DOC_IDS="<id1>,<id2>"
    uv run python scripts/run_memory_pipeline.py --mode online --doc-ids "id1,id2"
"""

import asyncio
import logging
from typing import Any

import click

from tree.cli import (
    MODE_ONLINE,
    connect_and_resolve_user,
    mode_option,
    trigger_deployment,
    user_options,
    wait_for_flow_run,
)
from tree.logging import init_logger

init_logger()
logger = logging.getLogger(__name__)

DEPLOYMENT_NAME = "memory-extract-etl-coordinator/memory-extract-etl-coordinator"


async def _run(
    user_id: str | None,
    user_identifier: str | None,
    document_ids: list[str] | None,
    num_shards: int | None,
) -> None:
    resolved_user_id = await connect_and_resolve_user(user_id, user_identifier)
    parameters: dict[str, Any] = {"user_id": str(resolved_user_id)}
    if document_ids:
        parameters["document_ids"] = document_ids
    if num_shards is not None:
        parameters["num_shards"] = num_shards
    flow_run_id = await trigger_deployment(DEPLOYMENT_NAME, parameters)
    await wait_for_flow_run(flow_run_id)


@click.command()
@mode_option
@user_options
@click.option(
    "--doc-ids",
    default=None,
    help=(
        "Comma-separated document ObjectIds to extract. REQUIRED for --mode "
        "online; optional narrowing for --mode offline (omit → every PENDING "
        "document for the resolved user)."
    ),
)
@click.option(
    "--num-shards",
    default=None,
    type=int,
    help=(
        "[offline] Document-shard fan-out width (#067, ``>= 1``): the coordinator "
        "dispatches one worker run per shard, then indexes once. Omit or 1 → "
        "1 worker run + 1 index run."
    ),
)
def main(
    mode: str,
    user_id: str | None,
    user_identifier: str | None,
    doc_ids: str | None,
    num_shards: int | None,
) -> None:
    """Run the memory extraction pipeline: offline batch or specific online docs."""

    if mode == MODE_ONLINE:
        if not doc_ids:
            raise click.UsageError(
                "--mode online requires --doc-ids '<id>[,<id2>]' (the id printed "
                "by run_data_pipeline.py --mode online)."
            )
        if num_shards is not None:
            raise click.UsageError("--num-shards is an offline-only fan-out knob.")
    if num_shards is not None and num_shards < 1:
        raise click.UsageError(f"--num-shards must be >= 1 (got {num_shards}).")

    parsed_doc_ids: list[str] | None = None
    if doc_ids:
        parsed_doc_ids = [d.strip() for d in doc_ids.split(",") if d.strip()]

    asyncio.run(_run(user_id, user_identifier, parsed_doc_ids, num_shards))


if __name__ == "__main__":
    main()
