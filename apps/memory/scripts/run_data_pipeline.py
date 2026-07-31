"""
Trigger the data pipeline via Prefect.

Triggers the ``data-etl-coordinator`` deployment (#072, ADR-002 §3 amended #066;
source selection per ADR-003). Operators always run the COORDINATOR: it resolves
its source set, groups it by PLATFORM, and dispatches one ``data-etl-worker`` run
per non-HuggingFace platform bucket present (``substack`` / ``youtube`` /
``custom``) plus ``num_workers`` offset-window runs per ``HuggingFaceDatasetSource``
(the HF fan-out width is declared per-source in YAML, NOT via a global flag). There
is NO trailing step and NO trailing index: the data pipeline only produces
``documents``.

Source selection (freely combinable; pass neither for the default):

* ``--source-file`` (repeatable) — a committed source file under ``sources/``
  (e.g. ``sources/backfill.yaml`` / ``sources/listen.yaml``).
* ``--uri`` (repeatable) — an ad-hoc source URL, optionally suffixed ``=TYPE`` to
  force a type (e.g. ``…/feed=substack_rss``); the type is inferred otherwise.
  ``huggingface_dataset`` is rejected — those need tuning fields a bare URL can't
  carry, so define them in a YAML file and use ``--source-file``.
* Neither flag → the coordinator's default backfill+listen set.

``--uri`` tokens are parsed + built into typed sources up front, so a bad token
(e.g. an explicit ``huggingface_dataset``) fails fast BEFORE any flow is triggered.

Every deployment registered by ``tree.orchestrator`` requires a ``user_id``
parameter (#020). It defaults to the current-session user; override with
``USER_ID=<ObjectId>`` or ``USER_IDENTIFIER=<handle>`` (the Makefile wires these
for you). See :func:`tree.entities.sessions.resolve_user_id` for the resolution precedence.

Requires:
    - Prefect server running (make local-start)
    - Workflows served (make memory-serve-workflows)

Usage:
    make memory-run-data-pipeline-offline                                # default: backfill + listen
    make memory-run-data-pipeline-offline SOURCE_FILE="sources/listen.yaml"
    make memory-run-data-pipeline-offline URI="https://blog.com/feed=substack_rss"
    make memory-run-data-pipeline-offline SOURCE_FILE="sources/backfill.yaml" URI="https://x.com/a https://y.com/feed=substack_rss"
    uv run python scripts/run_data_pipeline.py --user-identifier paul --source-file sources/listen.yaml --uri https://x.com/a
"""

import asyncio
import logging
import sys

import click
from prefect.client.orchestration import get_client
from prefect.client.schemas.filters import LogFilter, LogFilterFlowRunId

from tree.config.settings import settings
from tree.config.sources import build_uri_sources, parse_uri_token
from tree.db import init_mongodb
from tree.entities.sessions import resolve_user_id
from tree.logging import init_logger

init_logger()
logger = logging.getLogger(__name__)

DEPLOYMENT_NAME = "data-etl-coordinator/data-etl-coordinator"
END_TO_END_DEPLOYMENT_NAME = "etl-offline/etl-offline"
POLL_INTERVAL_SECONDS = 2


async def _run(
    user_id: str | None,
    user_identifier: str | None,
    source_files: list[str],
    inline_sources: list[dict[str, object]],
    end_to_end: bool,
    num_shards: int,
) -> None:
    await init_mongodb(
        settings.mongo.mongo_uri.get_secret_value(),
        settings.mongo.mongo_initdb_database,
    )
    resolved_user_id = await resolve_user_id(user_id, user_identifier)

    # Forward only the selectors the operator passed; with neither present the
    # coordinator falls back to its default backfill+listen set.
    parameters: dict[str, object] = {"user_id": str(resolved_user_id)}
    if source_files:
        parameters["source_files"] = source_files
    if inline_sources:
        parameters["sources"] = inline_sources
    if end_to_end:
        parameters["num_shards"] = num_shards

    async with get_client() as client:
        try:
            deployment = await client.read_deployment_by_name(
                END_TO_END_DEPLOYMENT_NAME if end_to_end else DEPLOYMENT_NAME
            )
        except Exception as exc:  # noqa: BLE001 — absent optional deployment.
            if not end_to_end:
                raise
            # etl-offline is an OPTIONAL deployment (free-tier cap); mirror the
            # online dispatcher: run the SAME flow inline in this process.
            logger.warning(
                "etl-offline deployment unavailable (%s: %s); running the flow "
                "in-process instead",
                type(exc).__name__,
                exc,
            )
            from tree.offline import etl_offline

            await etl_offline(
                user_id=resolved_user_id,
                source_files=source_files or None,
                sources=inline_sources or None,
                num_shards=num_shards,
            )
            logger.info("Done. Flow completed successfully (in-process).")
            return

        flow_run = await client.create_flow_run_from_deployment(
            deployment_id=deployment.id,
            parameters=parameters,
        )
        logger.info("Flow run created: %s (user_id=%s)", flow_run.id, resolved_user_id)
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
        "Override the target tenant by Mongo ObjectId. Defaults to the "
        "current-session user; also reads the ``USER_ID`` env var."
    ),
)
@click.option(
    "--user-identifier",
    default=None,
    help=(
        "Override the target tenant by stable handle (e.g. email). Defaults to "
        "the current-session user; also reads the ``USER_IDENTIFIER`` env var."
    ),
)
@click.option(
    "--source-file",
    "source_files",
    multiple=True,
    help=(
        "Repeatable. A committed source file to ingest (e.g. "
        "``sources/backfill.yaml``). Combine freely with ``--uri``; pass neither "
        "to ingest the default backfill+listen set."
    ),
)
@click.option(
    "--uri",
    "uris",
    multiple=True,
    help=(
        "Repeatable. An ad-hoc source URL, optionally suffixed ``=TYPE`` to force a "
        "type (e.g. ``https://blog.com/feed=substack_rss``); inferred otherwise. "
        "Combine freely with ``--source-file``. ``huggingface_dataset`` is rejected "
        "— define HF datasets in a YAML file and use ``--source-file``."
    ),
)
@click.option(
    "--end-to-end",
    is_flag=True,
    default=False,
    help=(
        "Run the FULL offline pipeline (etl-offline: data ingest → extraction → "
        "index) instead of the data step only. Falls back to running the flow "
        "in-process when the optional etl-offline deployment isn't registered."
    ),
)
@click.option(
    "--num-shards",
    default=1,
    help="Extraction fan-out width (end-to-end only; forwarded per user).",
)
def main(
    user_id: str | None,
    user_identifier: str | None,
    source_files: tuple[str, ...],
    uris: tuple[str, ...],
    end_to_end: bool,
    num_shards: int,
) -> None:
    """Trigger the data-etl-coordinator (or end-to-end etl-offline) deployment."""

    # Parse + build inline sources from --uri tokens up front so a bad token
    # (e.g. an explicit huggingface_dataset) fails fast BEFORE any flow runs.
    specs = [parse_uri_token(token) for token in uris]
    inline_sources = [source.model_dump() for source in build_uri_sources(specs)]

    asyncio.run(
        _run(
            user_id=user_id,
            user_identifier=user_identifier,
            source_files=list(source_files),
            inline_sources=inline_sources,
            end_to_end=end_to_end,
            num_shards=num_shards,
        )
    )


if __name__ == "__main__":
    main()
