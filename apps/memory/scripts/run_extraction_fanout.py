"""
Trigger the document-shard memory-extraction fan-out via Prefect (#056).

The parent flow ``memory-extraction-fanout-etl`` partitions ONE user's pending
documents into shards, launches one ``memory-extraction-etl`` child run per shard
concurrently (capped by ``serve(global_limit=...)`` + the shared
``voyage-embeddings`` rate limit), then fires a SINGLE ``memory-indexing-etl`` run
after every shard settles.

Pass the tenant via ``--user-id <ObjectId>`` or the ``USER_ID`` env var (the
Makefile wires this for you). ``--num-shards`` overrides
``app_config.concurrency.fanout_max_parallel``.

Requires:
    - Prefect server running (make local-start)
    - The voyage-embeddings limit synced (make memory-sync-concurrency-limits)
    - Workflows served (make memory-serve-workflows)

Usage:
    make memory-run-memory-pipeline-extraction-fanout USER_ID=507f... NUM_SHARDS=4
    uv run python scripts/run_extraction_fanout.py --user-id 507f... --num-shards 4
"""

import asyncio
import logging
import os
import sys

import click
from beanie import PydanticObjectId
from prefect.client.orchestration import get_client
from prefect.client.schemas.filters import LogFilter, LogFilterFlowRunId

from tree.logging import init_logger

init_logger()
logger = logging.getLogger(__name__)

DEPLOYMENT_NAME = "memory-extraction-fanout-etl/memory-extraction-fanout-etl"
POLL_INTERVAL_SECONDS = 2


async def _run(user_id: PydanticObjectId, num_shards: int | None) -> None:
    async with get_client() as client:
        deployment = await client.read_deployment_by_name(DEPLOYMENT_NAME)

        parameters: dict[str, object] = {"user_id": str(user_id)}
        if num_shards is not None:
            parameters["num_shards"] = num_shards

        flow_run = await client.create_flow_run_from_deployment(
            deployment_id=deployment.id,
            parameters=parameters,
        )
        logger.info("Flow run created: %s (user_id=%s)", flow_run.id, user_id)
        base_url = str(client.api_url).rstrip("/").removesuffix("/api")
        logger.info("Track at: %s/runs/flow-run/%s", base_url, flow_run.id)

        log_filter = LogFilter(flow_run_id=LogFilterFlowRunId(any_=[flow_run.id]))
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

            run = await client.read_flow_run(flow_run.id)
            if run.state and run.state.is_final():
                if run.state.is_completed():
                    logger.info("Done. Fan-out parent flow completed successfully.")
                else:
                    logger.error("Flow finished with state: %s", run.state.name)
                    sys.exit(1)
                break
            await asyncio.sleep(POLL_INTERVAL_SECONDS)


@click.command()
@click.option(
    "--user-id",
    default=None,
    help=(
        "Tenant id (24-char Mongo ObjectId). Required; falls back to the "
        "``USER_ID`` env var when omitted."
    ),
)
@click.option(
    "--num-shards",
    default=None,
    type=int,
    help=(
        "How many document-shards to fan out. Defaults to "
        "``app_config.concurrency.fanout_max_parallel``."
    ),
)
def main(user_id: str | None, num_shards: int | None) -> None:
    """Trigger the memory-extraction-fanout-etl Prefect deployment for ``user_id``."""

    raw = user_id or os.environ.get("USER_ID")
    if not raw:
        logger.error(
            "--user-id is required (or set USER_ID env). No silent fallback "
            "to a default user."
        )
        raise SystemExit(1)

    try:
        parsed = PydanticObjectId(raw)
    except Exception as exc:  # noqa: BLE001
        logger.error("--user-id %r is not a valid Mongo ObjectId: %s", raw, exc)
        raise SystemExit(1) from exc

    if num_shards is not None and num_shards < 1:
        logger.error("--num-shards must be >= 1 (got %d)", num_shards)
        raise SystemExit(1)

    asyncio.run(_run(parsed, num_shards))


if __name__ == "__main__":
    main()
