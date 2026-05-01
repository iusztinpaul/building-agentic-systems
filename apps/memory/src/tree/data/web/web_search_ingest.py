"""Fire-and-forget trigger for the ``ingest-web-url-batch-etl`` deployment.

Companion helper for the ``search_web`` MCP tool's optional ingestion path.
Mirrors the trigger pattern in ``apps/memory/scripts/run_url_data_pipeline.py``
(lines 39-48) but **without** the polling/log-streaming loop — search_web is a
sub-5s tool and must not block on a multi-minute batch ingest.

Pure orchestration — no MongoDB, no Bright Data, no business logic. The
deployment must already be served (``make memory-serve-workflows``) for the
trigger to succeed.
"""

from __future__ import annotations

import logging

from prefect.client.orchestration import get_client

logger = logging.getLogger(__name__)

DEPLOYMENT_NAME = "ingest-web-url-batch-etl/ingest-web-url-batch-etl"


async def trigger_url_batch_ingest(urls: list[str]) -> dict[str, str]:
    """Fire the ``ingest-web-url-batch-etl`` deployment with the given URLs.

    Looks up the deployment by name, creates a flow run with
    ``parameters={"urls": urls}``, and returns immediately. Does NOT wait for
    the run to finish.

    Args:
        urls: Non-empty list of URLs to ingest. The deployment validates the
            URL strings itself; this helper does not.

    Returns:
        A dict with two string keys:
            - ``flow_run_id`` — the Prefect flow-run UUID as a string.
            - ``tracking_url`` — a human-readable URL the caller can open to
              follow the run in the Prefect UI.

    Raises:
        ValueError: If ``urls`` is empty.
        Exception: Any error raised by the Prefect client (deployment not
            found, connection refused, etc.) propagates to the caller. The
            ``search_web`` tool catches these and degrades gracefully.
    """

    if not urls:
        raise ValueError("urls must not be empty")

    async with get_client() as client:
        deployment = await client.read_deployment_by_name(DEPLOYMENT_NAME)

        flow_run = await client.create_flow_run_from_deployment(
            deployment_id=deployment.id,
            parameters={"urls": urls},
        )
        flow_run_id = str(flow_run.id)
        base_url = str(client.api_url).rstrip("/").removesuffix("/api")
        tracking_url = f"{base_url}/runs/flow-run/{flow_run_id}"

        logger.info(
            "Triggered ingest-web-url-batch-etl flow run %s for %d URL(s)",
            flow_run_id,
            len(urls),
        )

    return {"flow_run_id": flow_run_id, "tracking_url": tracking_url}
