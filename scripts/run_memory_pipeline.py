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
import sys

from prefect.client.orchestration import get_client

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
    ids = sys.argv[1:] if len(sys.argv) > 1 else None
    asyncio.run(main(ids))
