"""
Trigger the Substack RSS ETL pipeline via Prefect.

Requires:
    - Prefect server running (make prefect-server)
    - Workflows served (make serve-workflows)

Usage:
    uv run python scripts/run_data_pipeline.py https://www.decodingai.com/feed
    uv run python scripts/run_data_pipeline.py https://www.decodingai.com/feed https://other.substack.com/feed
"""

import asyncio
import sys

from prefect.client.orchestration import get_client

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
        print(f"Flow run created: {flow_run.id}")
        base_url = str(client.api_url).removesuffix("/api")
        print(f"Track at: {base_url}/runs/flow-run/{flow_run.id}")

        while True:
            run = await client.read_flow_run(flow_run.id)
            if run.state and run.state.is_final():
                if run.state.is_completed():
                    print("\nDone. Flow completed successfully.")
                else:
                    print(f"\nFlow finished with state: {run.state.name}")
                break
            await asyncio.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(
            "Usage: uv run python scripts/run_data_pipeline.py <feed_url> [feed_url ...]"
        )
        sys.exit(1)

    asyncio.run(main(sys.argv[1:]))
