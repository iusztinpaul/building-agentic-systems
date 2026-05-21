"""
Trigger the scheduled dream-consolidation fan-out via Prefect.

The ``dream-consolidation-etl`` deployment serves the parent flow
``dream_consolidation_all_users`` (#052). Unlike the per-tenant pipelines
it takes NO ``user_id`` — the parent flow enumerates active users itself
and fans out one per-user dream run each (tenant-scoped watermark + cost).

Whether each per-user run mutates the graph or only reports is controlled
by ``app_config.dream.dry_run`` (YAML), not a CLI flag.

Requires:
    - Prefect server running (make local-start)
    - Workflows served (make memory-serve-workflows)

Usage:
    make memory-run-dream-consolidation
    uv run python scripts/run_dream_consolidation.py
"""

import asyncio
import logging
import sys

import click
from prefect.client.orchestration import get_client
from prefect.client.schemas.filters import LogFilter, LogFilterFlowRunId

from tree.logging import init_logger

init_logger()
logger = logging.getLogger(__name__)

DEPLOYMENT_NAME = "dream-consolidation-all-users/dream-consolidation-etl"
POLL_INTERVAL_SECONDS = 2


async def _run() -> None:
    async with get_client() as client:
        deployment = await client.read_deployment_by_name(DEPLOYMENT_NAME)

        flow_run = await client.create_flow_run_from_deployment(
            deployment_id=deployment.id,
        )
        logger.info("Flow run created: %s", flow_run.id)
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
def main() -> None:
    """Trigger the dream-consolidation-etl Prefect deployment (all users)."""

    asyncio.run(_run())


if __name__ == "__main__":
    main()
