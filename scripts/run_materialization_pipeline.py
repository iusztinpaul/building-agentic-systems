"""
Trigger the memory materialization pipeline via Prefect.

Requires:
    - Prefect server running (make prefect-server)
    - Workflows served (make serve-workflows)

Usage:
    uv run python scripts/run_materialization_pipeline.py
"""

import asyncio

from prefect.client.orchestration import get_client

DEPLOYMENT_NAME = "memory-materialization-etl/memory-materialization-etl"
POLL_INTERVAL_SECONDS = 2


async def main() -> None:
    async with get_client() as client:
        deployment = await client.read_deployment_by_name(DEPLOYMENT_NAME)

        flow_run = await client.create_flow_run_from_deployment(
            deployment_id=deployment.id,
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
    asyncio.run(main())
