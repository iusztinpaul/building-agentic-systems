"""
Trigger the arxiv dataset ETL pipeline via Prefect.

Requires:
    - Prefect server running (make local-start)
    - Workflows served (make serve-workflows)

Usage:
    uv run python scripts/run_arxiv_data_pipeline.py
    uv run python scripts/run_arxiv_data_pipeline.py --max-samples 100

Reads max_samples from configs/default.yaml (sources.huggingface_arxiv_dataset.max_samples) by default.
CLI argument overrides the config.
"""

import asyncio
import logging
import sys

import click
from prefect.client.orchestration import get_client
from prefect.client.schemas.filters import LogFilter, LogFilterFlowRunId

from twin.config.app_config import app_config

DEPLOYMENT_NAME = "ingest-arxiv-dataset-etl/ingest-arxiv-dataset-etl"
POLL_INTERVAL_SECONDS = 2


async def main(max_samples: int) -> None:
    async with get_client() as client:
        deployment = await client.read_deployment_by_name(DEPLOYMENT_NAME)

        flow_run = await client.create_flow_run_from_deployment(
            deployment_id=deployment.id,
            parameters={"max_samples": max_samples},
        )
        print(f"Flow run created: {flow_run.id}")
        base_url = str(client.api_url).rstrip("/").removesuffix("/api")
        print(f"Track at: {base_url}/runs/flow-run/{flow_run.id}")

        log_filter = LogFilter(flow_run_id=LogFilterFlowRunId(any_=[flow_run.id]))
        log_offset = 0

        while True:
            logs = await client.read_logs(
                log_filter=log_filter, offset=log_offset, limit=100
            )
            for log in logs:
                print(
                    f"{log.timestamp:%Y-%m-%d %H:%M:%S} | {logging.getLevelName(log.level):7s} | {log.message}"
                )
            log_offset += len(logs)

            run = await client.read_flow_run(flow_run.id)
            if run.state and run.state.is_final():
                if run.state.is_completed():
                    print("\nDone. Flow completed successfully.")
                else:
                    print(
                        f"\nFlow finished with state: {run.state.name}",
                        file=sys.stderr,
                    )
                    sys.exit(1)
                break
            await asyncio.sleep(POLL_INTERVAL_SECONDS)


@click.command()
@click.option(
    "--max-samples",
    type=int,
    default=None,
    help="Maximum number of samples to ingest. Defaults to config value.",
)
def cli(max_samples: int | None) -> None:
    if max_samples is None:
        max_samples = app_config.sources.huggingface_arxiv_dataset.max_samples

    asyncio.run(main(max_samples))


if __name__ == "__main__":
    cli()
