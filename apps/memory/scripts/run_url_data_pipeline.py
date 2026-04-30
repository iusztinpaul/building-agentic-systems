"""
Trigger the web URL data pipeline individually via Prefect.

Routes the URL through the dispatcher (specialized pipelines win when they
match; otherwise falls back to Bright Data Web Unlocker via the
``ingest-web-url-etl`` deployment).

Requires:
    - Prefect server running (make local-start)
    - Workflows served (make serve-workflows)

Usage:
    uv run python scripts/run_url_data_pipeline.py "https://example.com/post"
"""

import asyncio
import logging
import sys

from prefect.client.orchestration import get_client
from prefect.client.schemas.filters import LogFilter, LogFilterFlowRunId

from tree.logging import init_logger

init_logger()
logger = logging.getLogger(__name__)

DEPLOYMENT_NAME = "ingest-web-url-etl/ingest-web-url-etl"
POLL_INTERVAL_SECONDS = 2


async def main() -> None:
    if len(sys.argv) < 2 or not sys.argv[1].strip():
        logger.error("USAGE: run_url_data_pipeline.py <url>")
        sys.exit(1)

    url = sys.argv[1]

    async with get_client() as client:
        deployment = await client.read_deployment_by_name(DEPLOYMENT_NAME)

        flow_run = await client.create_flow_run_from_deployment(
            deployment_id=deployment.id,
            parameters={"url": url},
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
                    logger.info("Done. URL data pipeline completed successfully.")
                else:
                    logger.error("Flow finished with state: %s", run.state.name)
                    sys.exit(1)
                break
            await asyncio.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    asyncio.run(main())
