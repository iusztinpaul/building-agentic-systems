"""
Offline end-to-end orchestration — the batch mirror of :mod:`tree.online`.

Sits at the top level because it spans BOTH pipelines: the data step
(``data_etl_coordinator``) and the memory step
(``memory_extract_etl_coordinator``, which owns the trailing index run).
Neither pipeline package may import the other; this module imports both.

:func:`etl_offline` runs the whole chain in ONE flow run: the data coordinator
as an inline subflow (it owns source resolution — same ``source_files`` /
inline ``sources`` / backfill+listen default as always — and the per-platform
worker fan-out), then one extraction coordinator per target user (explicit
``user_id``, or every active user when ``None`` — the nightly-cron semantics).
Worker fan-outs inside each coordinator still run as separate deployment runs;
only the two coordinators execute inline here, so the end-to-end run costs the
same single admission slot a lone coordinator already does.

The standalone ``data-etl-coordinator`` / ``memory-extract-etl-coordinator``
deployments remain the manual single-step entry points; this flow composes
them, it does not replace them.
"""

import logging
from dataclasses import asdict
from typing import Any

from beanie import PydanticObjectId
from prefect import flow

from tree.data.offline_pipeline import data_etl_coordinator, resolve_target_user_ids
from tree.memory.extraction.pipeline import memory_extract_etl_coordinator
from tree.observability import (
    TAG_DATA_PIPELINE,
    TAG_MEMORY_PIPELINE,
    TAG_OFFLINE,
    configure_opik,
    flush_opik,
    span,
)

logger = logging.getLogger(__name__)

# Spans/deployment tags for the end-to-end run: it IS both pipelines, offline.
TAGS_ETL_OFFLINE = [TAG_DATA_PIPELINE, TAG_MEMORY_PIPELINE, TAG_OFFLINE]


@flow(name="etl-offline", log_prints=True)
async def etl_offline(
    user_id: PydanticObjectId | None = None,
    source_files: list[str] | None = None,
    sources: list[dict[str, Any]] | None = None,
    num_shards: int = 1,
) -> dict[str, Any]:
    """Run the FULL offline pipeline: data ingest, then extraction (+ index).

    Phase 1 — data: :func:`data_etl_coordinator` as an inline subflow, passing
    the source selectors through untouched (it owns resolution, so the source
    semantics are identical to a standalone data run).

    Phase 2 — memory: one :func:`memory_extract_etl_coordinator` inline subflow
    per target user (each resolves that user's PENDING documents, fans out
    extraction workers, and fires the trailing index run). Per-user failures
    are isolated: one user's blown extraction is recorded and the others
    proceed — mirroring the shard-failure-isolation convention (#095).

    Observability: owns one span the two coordinators' spans nest under
    (same-process contextvars), so the end-to-end run renders as ONE trace.

    Returns ``{"data": <DataFanOutStats>, "extraction": {user_id: <FanOutStats
    | {"error": ...}>}}`` as plain dicts (JSON-safe for the flow-run result).
    """

    configure_opik()
    try:
        with span("etl-offline", tags=TAGS_ETL_OFFLINE):
            data_stats = await data_etl_coordinator(
                user_id=user_id, source_files=source_files, sources=sources
            )

            extraction: dict[str, Any] = {}
            for uid in await resolve_target_user_ids(user_id):
                try:
                    stats = await memory_extract_etl_coordinator(
                        uid, num_shards=num_shards
                    )
                    extraction[str(uid)] = asdict(stats)
                except Exception as exc:  # noqa: BLE001 — isolate per-user failures.
                    logger.exception("etl-offline: extraction failed for user %s", uid)
                    extraction[str(uid)] = {"error": str(exc)}

            return {"data": asdict(data_stats), "extraction": extraction}
    finally:
        # Fail-open telemetry flush — the worker subprocess exits after the run.
        flush_opik()
