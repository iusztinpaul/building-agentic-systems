"""
Trigger the unified data pipeline ETL via Prefect.

Walks every entry in ``configs/default.yaml`` ``sources.sources`` and
dispatches each one to the appropriate sub-flow (Substack RSS / article
batches, HuggingFace arxiv, web URLs).

Every deployment registered by ``tree.orchestrator`` requires a
``user_id`` parameter (#020). Pass it via ``--user-id <ObjectId>`` or
the ``USER_ID`` env var (the Makefile wires this for you).

Requires:
    - Prefect server running (make local-start)
    - Workflows served (make memory-serve-workflows)

Usage:
    make memory-run-data-pipeline USER_ID=507f1f77bcf86cd799439011
    uv run python scripts/run_data_pipeline.py --user-id 507f1f77bcf86cd799439011
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

DEPLOYMENT_NAME = "data-pipeline-etl/data-pipeline-etl"
POLL_INTERVAL_SECONDS = 2


async def _run(user_id: PydanticObjectId) -> None:
    async with get_client() as client:
        deployment = await client.read_deployment_by_name(DEPLOYMENT_NAME)

        flow_run = await client.create_flow_run_from_deployment(
            deployment_id=deployment.id,
            parameters={"user_id": str(user_id)},
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
def main(user_id: str | None) -> None:
    """Trigger the data-pipeline-etl Prefect deployment for ``user_id``."""

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

    asyncio.run(_run(parsed))


if __name__ == "__main__":
    main()
