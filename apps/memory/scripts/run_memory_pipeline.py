"""
Run the MEMORY pipeline (``documents`` → the ``memory`` collection).

What lands there depends on ``memory.mode``: document / **Parent chunk** /
**Child chunk** rows in ``rag``, those plus entity nodes and edges in
``graphrag``.

A light CLI shim (glue lives in :mod:`tree.cli`) that dispatches the
``offline-pipeline`` flow (:mod:`tree.offline`) with the DATA phase OFF, so the
run is the extraction phase plus the indexing phase. Inside it, the extraction
coordinator (#067) resolves the doc set, partitions it into
``min(num_shards, N)`` shards and dispatches one ``memory-extract-etl-worker``
run per shard; then the indexing phase runs once for the user. Two modes select
the doc set:

* ``--mode offline`` (default) — batch: every PENDING document for the
  resolved user (optionally narrowed with ``--doc-ids``); ``--num-shards``
  sets the fan-out width.
* ``--mode online`` — realtime: exactly the documents you select with
  ``--doc-ids`` (e.g. the id printed by ``run_data_pipeline.py --mode online``)
  and/or ``--source-uris`` (the ``source_uri`` an **Ingest receipt** carries,
  resolved to ids by the flow) — one of the two is required. No shard fan-out —
  a handful of docs needs one worker.

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
    make memory-run-memory-pipeline                                # offline, all pending docs
    make memory-run-memory-pipeline NUM_SHARDS=4
    make memory-run-memory-pipeline MODE=online DOC_IDS="<id1>,<id2>"
    make memory-run-memory-pipeline MODE=online SOURCE_URIS="<uri>[,<uri2>]"
    uv run python scripts/run_memory_pipeline.py --mode online --doc-ids "id1,id2"
"""

import asyncio
import logging

import click

from tree.cli import (
    MODE_ONLINE,
    connect_and_resolve_user,
    mode_option,
    user_options,
    wait_for_dispatch,
)
from tree.logging import init_logger
from tree.observability import flush_opik
from tree.offline import dispatch_offline_pipeline

init_logger()
logger = logging.getLogger(__name__)


async def _run(
    user_id: str | None,
    user_identifier: str | None,
    document_ids: list[str] | None,
    source_uris: list[str] | None,
    num_shards: int | None,
) -> None:
    resolved_user_id = await connect_and_resolve_user(user_id, user_identifier)
    result = await dispatch_offline_pipeline(
        user_id=resolved_user_id,
        document_ids=document_ids,
        source_uris=source_uris,
        num_shards=num_shards if num_shards is not None else 1,
        run_data=False,
    )
    await wait_for_dispatch(result)
    # The dispatcher's spans belong to this short-lived process — flush before exit.
    flush_opik()


def _parse_csv(raw: str | None) -> list[str] | None:
    """Split a comma-separated option into entries; ``None`` when unset/empty."""

    if not raw:
        return None
    return [entry.strip() for entry in raw.split(",") if entry.strip()] or None


@click.command()
@mode_option
@user_options
@click.option(
    "--doc-ids",
    default=None,
    help=(
        "Comma-separated document ObjectIds to extract. REQUIRED for --mode "
        "online unless --source-uris is given; optional narrowing for --mode "
        "offline (omit → every PENDING document for the resolved user)."
    ),
)
@click.option(
    "--source-uris",
    default=None,
    help=(
        "Comma-separated source_uris to extract — the natural key an ingest "
        "receipt carries (e.g. the canonical YouTube URL, file:///path). "
        "Resolved to this user's document ids by the flow; an unknown URI fails "
        "the run. Satisfies --mode online in place of --doc-ids, and may be "
        "combined with it."
    ),
)
@click.option(
    "--num-shards",
    default=None,
    type=int,
    help=(
        "[offline] Document-shard fan-out width (#067, ``>= 1``): the coordinator "
        "dispatches one worker run per shard; the indexing phase then runs once "
        "for the user. Omit or 1 → 1 worker run + 1 indexing subflow."
    ),
)
def main(
    mode: str,
    user_id: str | None,
    user_identifier: str | None,
    doc_ids: str | None,
    source_uris: str | None,
    num_shards: int | None,
) -> None:
    """Run the memory extraction pipeline: offline batch or specific online docs."""

    if mode == MODE_ONLINE:
        if not doc_ids and not source_uris:
            raise click.UsageError(
                "--mode online requires --doc-ids '<id>[,<id2>]' (the id printed "
                "by run_data_pipeline.py --mode online) or --source-uris "
                "'<uri>[,<uri2>]' (the source_uri an ingest receipt carries)."
            )
        if num_shards is not None:
            raise click.UsageError("--num-shards is an offline-only fan-out knob.")
    if num_shards is not None and num_shards < 1:
        raise click.UsageError(f"--num-shards must be >= 1 (got {num_shards}).")

    asyncio.run(
        _run(
            user_id,
            user_identifier,
            _parse_csv(doc_ids),
            _parse_csv(source_uris),
            num_shards,
        )
    )


if __name__ == "__main__":
    main()
