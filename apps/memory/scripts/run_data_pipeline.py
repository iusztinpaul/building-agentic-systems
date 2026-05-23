"""
Trigger the data pipeline via Prefect.

Triggers the ``data-etl-orchestrator`` deployment (#068, ADR-002 §3 amended #066).
Operators always run the ORCHESTRATOR: it reads the configured
``configs/default.yaml`` ``sources.sources`` list, partitions it into
``min(num_shards, N)`` balanced shards, and dispatches one ``data-etl-worker`` run
per shard (a DISTINCT worker deployment — NO recursion). There is NO trailing step:
the data pipeline only produces ``documents``; there is no index. ``num_shards=1``
(the default) dispatches ONE worker run with the full source list; ``> 1`` fans the
sources out across multiple workers. A bare single-shard ingestion is also available
by triggering ``data-etl-worker`` directly (not via this script).

Every deployment registered by ``tree.orchestrator`` requires a
``user_id`` parameter (#020). Pass it via ``--user-id <ObjectId>`` or
the ``USER_ID`` env var (the Makefile wires this for you).

Requires:
    - Prefect server running (make local-start)
    - Workflows served (make memory-serve-workflows)

``--num-shards`` (optional, ``>= 1``) sets the source-shard fan-out width.

Usage:
    make memory-run-data-pipeline USER_ID=507f1f77bcf86cd799439011
    make memory-run-data-pipeline USER_ID=507f... NUM_SHARDS=2
    uv run python scripts/run_data_pipeline.py --user-id 507f1f77bcf86cd799439011
    uv run python scripts/run_data_pipeline.py --user-id 507f... --num-shards 2
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

DEPLOYMENT_NAME = "data-etl-orchestrator/data-etl-orchestrator"
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
                    logger.info("Done. Flow completed successfully.")
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
        "Tenant id (24-char Mongo ObjectId) the data pipeline writes for. "
        "Required; falls back to the ``USER_ID`` env var when omitted."
    ),
)
@click.option(
    "--num-shards",
    default=None,
    type=int,
    help=(
        "Optional source-shard fan-out width (#068). The orchestrator partitions "
        "the configured sources into ``min(num_shards, N)`` shards and dispatches "
        "one ``data-etl-worker`` run per shard (NO trailing index). Omit or 1 → 1 "
        "worker run with all sources. Must be ``>= 1``."
    ),
)
def main(user_id: str | None, num_shards: int | None) -> None:
    """Trigger the data-etl-orchestrator Prefect deployment for ``user_id``."""

    raw = user_id or os.environ.get("USER_ID")
    if not raw:
        logger.error(
            "--user-id is required (or set USER_ID env). No silent fallback "
            "to a default user."
        )
        raise SystemExit(1)

    try:
        parsed = PydanticObjectId(raw)
    except Exception as exc:  # noqa: BLE001 — surface raw input to the user.
        logger.error("--user-id %r is not a valid Mongo ObjectId: %s", raw, exc)
        raise SystemExit(1) from exc

    if num_shards is not None and num_shards < 1:
        logger.error("--num-shards must be >= 1 (got %d)", num_shards)
        raise SystemExit(1)

    asyncio.run(_run(parsed, num_shards))


if __name__ == "__main__":
    main()
