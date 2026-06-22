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

Every deployment registered by ``tree.orchestrator`` requires a ``user_id``
parameter (#020). It defaults to the current-session user; override with
``USER_ID=<ObjectId>`` or ``USER_IDENTIFIER=<handle>`` (the Makefile wires these
for you). See :mod:`scripts._users` for the resolution precedence.

Requires:
    - Prefect server running (make local-start)
    - Workflows served (make memory-serve-workflows)

``--num-shards`` (optional, ``>= 1``) sets the source-shard fan-out width.

Usage:
    make memory-run-data-pipeline                       # current-session user
    make memory-run-data-pipeline USER_ID=507f...       # override by id
    make memory-run-data-pipeline USER_IDENTIFIER=paul  # override by handle
    make memory-run-data-pipeline NUM_SHARDS=2
    uv run python scripts/run_data_pipeline.py --user-identifier paul --num-shards 2
"""

import asyncio
import logging
import sys

import click
from prefect.client.orchestration import get_client
from prefect.client.schemas.filters import LogFilter, LogFilterFlowRunId

from _users import resolve_user_id
from tree.config.settings import settings
from tree.db import init_mongodb
from tree.logging import init_logger

init_logger()
logger = logging.getLogger(__name__)

DEPLOYMENT_NAME = "data-etl-orchestrator/data-etl-orchestrator"
POLL_INTERVAL_SECONDS = 2


async def _run(
    user_id: str | None, user_identifier: str | None, num_shards: int | None
) -> None:
    await init_mongodb(
        settings.mongo.mongo_uri.get_secret_value(),
        settings.mongo.mongo_initdb_database,
    )
    resolved_user_id = await resolve_user_id(user_id, user_identifier)

    async with get_client() as client:
        deployment = await client.read_deployment_by_name(DEPLOYMENT_NAME)

        parameters: dict[str, object] = {"user_id": str(resolved_user_id)}
        if num_shards is not None:
            parameters["num_shards"] = num_shards

        flow_run = await client.create_flow_run_from_deployment(
            deployment_id=deployment.id,
            parameters=parameters,
        )
        logger.info("Flow run created: %s (user_id=%s)", flow_run.id, resolved_user_id)
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
def main(
    user_id: str | None, user_identifier: str | None, num_shards: int | None
) -> None:
    """Trigger the data-etl-orchestrator deployment for the resolved user."""

    if num_shards is not None and num_shards < 1:
        logger.error("--num-shards must be >= 1 (got %d)", num_shards)
        raise SystemExit(1)

    asyncio.run(_run(user_id, user_identifier, num_shards))


if __name__ == "__main__":
    main()
