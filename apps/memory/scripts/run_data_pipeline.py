"""
Trigger the Substack RSS ETL pipeline via Prefect.

Requires:
    - Prefect server running (make prefect-server)
    - Workflows served (make serve-workflows)

Usage:
    uv run python scripts/run_data_pipeline.py
    uv run python scripts/run_data_pipeline.py https://www.decodingai.com/feed https://other.substack.com/feed

Reads feed URLs from configs/default.yaml (sources.substack) by default.
CLI arguments override the config.
"""

import asyncio
import logging
import sys

from prefect.client.orchestration import get_client
from prefect.client.schemas.filters import LogFilter, LogFilterFlowRunId

from twin.config.app_config import app_config
from twin.logging import init_logger

init_logger()
logger = logging.getLogger(__name__)

DEPLOYMENT_NAME = (
    "ingest-substack-rss-feed-batch-etl/ingest-substack-rss-feed-batch-etl"
)
POLL_INTERVAL_SECONDS = 2


async def main(feed_urls: list[str]) -> None:
    async with get_client() as client:
        deployment = await client.read_deployment_by_name(DEPLOYMENT_NAME)

        flow_run = await client.create_flow_run_from_deployment(
            deployment_id=deployment.id,
            parameters={"feed_urls": feed_urls},
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
    feed_urls = sys.argv[1:] or app_config.sources.substack

    if not feed_urls:
        logger.error(
            "Usage: uv run python scripts/run_data_pipeline.py [feed_url ...]\n"
            "       Or configure sources.substack in configs/default.yaml"
        )
        sys.exit(1)

    asyncio.run(main(feed_urls))
