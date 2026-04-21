"""
Trigger the memory extraction pipeline via Prefect.

Requires:
    - Prefect server running (make prefect-server)
    - Workflows served (make serve-workflows)

Usage:
    uv run python scripts/run_memory_pipeline.py
    uv run python scripts/run_memory_pipeline.py 507f1f77bcf86cd799439011 507f1f77bcf86cd799439012
"""

import asyncio
import logging
import sys

from prefect.client.orchestration import get_client
from prefect.client.schemas.filters import LogFilter, LogFilterFlowRunId

from twin.logging import init_logger

init_logger()
logger = logging.getLogger(__name__)

DEPLOYMENT_NAME = "memory-extraction-etl/memory-extraction-etl"
POLL_INTERVAL_SECONDS = 2


async def main(document_ids: list[str] | None = None) -> None:
    async with get_client() as client:
        deployment = await client.read_deployment_by_name(DEPLOYMENT_NAME)

        parameters = {}
        if document_ids:
            parameters["document_ids"] = document_ids

        flow_run = await client.create_flow_run_from_deployment(
            deployment_id=deployment.id,
            parameters=parameters,
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


if __name__ == "__main__":
    ids = sys.argv[1:] if len(sys.argv) > 1 else None
    asyncio.run(main(ids))
