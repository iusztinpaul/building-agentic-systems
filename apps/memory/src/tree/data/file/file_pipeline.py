"""
Prefect flow for file ingestion.

A thin wrapper around file.py that adds retries and logging.
"""

import logging

from beanie import PydanticObjectId
from prefect import flow

from tree.data.file.file import load_file_document
from tree.entities.documents import Document
from tree.observability import (
    TAGS_DATA_ONLINE,
    configure_opik,
    flush_opik,
    pipeline_metadata,
    span,
)

logger = logging.getLogger(__name__)

# Online data ingest (file variant of ``online_ingest``) — pipeline-identity tags
# shared 1:1 with this flow's Prefect flow-run tags.
_FILE_TAGS = TAGS_DATA_ONLINE
_FILE_METADATA = pipeline_metadata("file")


# Tier F — free replay: the body is ONE idempotent Mongo write (deduped on
# ``(user_id, source_type, source_uri)``), so retries live on the FLOW and there
# are NO tasks (ADR-002 amendment #097, superseding #096 rule 3b). Accepted cost:
# a retried attempt emits its own Opik trace — a trace per real retry is signal.
@flow(name="ingest-file-etl", log_prints=True, retries=3, retry_delay_seconds=5)
async def ingest_file(
    file_path: str,
    content: str,
    user_id: PydanticObjectId,
    title: str | None = None,
) -> Document | None:
    """Ingest caller-read file ``content`` as a Document for ``user_id``.

    ``file_path`` is identity only (source_uri + default title) — the file
    is read at the caller's edge, never here (the flow may not share a
    filesystem with the file's machine).

    Assumes MongoDB/Beanie is already initialised by the caller
    (MCP lifespan, coordinator, or batch flow).

    Observability: configures Opik at entry (subprocess-safe) and owns one
    trace per attempt.
    """

    configure_opik()
    try:
        with span("ingest-file-etl", tags=_FILE_TAGS, metadata=_FILE_METADATA):
            result = await load_file_document(file_path, content, user_id, title)
    finally:
        # Flush batched Opik telemetry (fail-open; no-op without OPIK_API_KEY).
        flush_opik()

    if result:
        logger.info("Ingested file: %s", file_path)
    else:
        logger.info("Skipped duplicate file: %s", file_path)

    return result
