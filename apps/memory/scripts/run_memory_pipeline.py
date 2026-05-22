"""
Trigger the memory extraction pipeline via Prefect.

Every Prefect deployment registered by ``tree.orchestrator`` requires a
``user_id`` parameter (#020). Pass it via ``--user-id <ObjectId>`` or
the ``USER_ID`` env var (the Makefile wires this for you).

Requires:
    - Prefect server running (make local-start)
    - Workflows served (make memory-serve-workflows)

``--num-shards`` (optional, ``>= 1``) selects the in-flow document-shard fan-out
(#061): omitted or ``1`` runs the plain WORKER extraction (a single
``memory-extraction-etl`` run, no child runs, no trailing indexing run);
``> 1`` takes the ORCHESTRATOR path — the flow partitions the user's pending docs
into shards, self-dispatches one ``memory-extraction-etl`` child run per shard
(each ``num_shards=1``), then fires a single ``memory-indexing-etl`` run. There is
ONE extraction deployment for both paths.

Usage:
    make memory-run-memory-pipeline-extraction USER_ID=507f1f77bcf86cd799439011
    make memory-run-memory-pipeline-extraction USER_ID=507f... NUM_SHARDS=4
    uv run python scripts/run_memory_pipeline.py --user-id 507f...
    uv run python scripts/run_memory_pipeline.py --user-id 507f... --doc-ids "id1,id2"
    uv run python scripts/run_memory_pipeline.py --user-id 507f... --num-shards 4
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

DEPLOYMENT_NAME = "memory-extraction-etl/memory-extraction-etl"
POLL_INTERVAL_SECONDS = 2


async def _run(
    user_id: PydanticObjectId,
    document_ids: list[str] | None,
    num_shards: int | None,
) -> None:
    async with get_client() as client:
        deployment = await client.read_deployment_by_name(DEPLOYMENT_NAME)

        parameters: dict[str, object] = {"user_id": str(user_id)}
        if document_ids:
            parameters["document_ids"] = document_ids
        if num_shards is not None:
            parameters["num_shards"] = num_shards

        flow_run = await client.create_flow_run_from_deployment(
            deployment_id=deployment.id,
            parameters=parameters,
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
        "Tenant id (24-char Mongo ObjectId). Required; falls back to the "
        "``USER_ID`` env var when omitted."
    ),
)
@click.option(
    "--doc-ids",
    default=None,
    help=(
        "Optional comma-separated list of document ObjectIds to extract. "
        "Omit to extract every PENDING document for ``user_id``."
    ),
)
@click.option(
    "--num-shards",
    default=None,
    type=int,
    help=(
        "Optional document-shard fan-out (#061). Omit or 1 → plain worker "
        "extraction (no child runs, no trailing indexing run). ``> 1`` → the "
        "flow partitions pending docs into shards, self-dispatches one "
        "``memory-extraction-etl`` child run per shard, then indexes once. "
        "Must be ``>= 1``."
    ),
)
def main(user_id: str | None, doc_ids: str | None, num_shards: int | None) -> None:
    """Trigger the memory-extraction-etl Prefect deployment for ``user_id``."""

    raw = user_id or os.environ.get("USER_ID")
    if not raw:
        logger.error(
            "--user-id is required (or set USER_ID env). No silent fallback "
            "to a default user."
        )
        raise SystemExit(1)

    try:
        parsed = PydanticObjectId(raw)
    except Exception as exc:  # noqa: BLE001
        logger.error("--user-id %r is not a valid Mongo ObjectId: %s", raw, exc)
        raise SystemExit(1) from exc

    if num_shards is not None and num_shards < 1:
        logger.error("--num-shards must be >= 1 (got %d)", num_shards)
        raise SystemExit(1)

    parsed_doc_ids: list[str] | None = None
    if doc_ids:
        parsed_doc_ids = [d.strip() for d in doc_ids.split(",") if d.strip()]

    asyncio.run(_run(parsed, parsed_doc_ids, num_shards))


if __name__ == "__main__":
    main()
