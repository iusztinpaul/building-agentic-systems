"""
Prefect tasks and flow for file ingestion.

Thin wrappers around file.py that add retries and logging.
"""

import logging

from beanie import PydanticObjectId
from prefect import flow, task

from tree.data.file.file import load_file_document
from tree.entities.documents import Document
from tree.observability import (
    TAGS_DATA_ONLINE,
    configure_opik,
    flush_opik,
    get_distributed_trace_headers,
    pipeline_metadata,
    span,
    tracked_span,
)

logger = logging.getLogger(__name__)

# Online data ingest (file variant of ``online_ingest``) — pipeline-identity tags
# shared 1:1 with this flow's Prefect flow-run tags.
_FILE_TAGS = TAGS_DATA_ONLINE
_FILE_METADATA = pipeline_metadata("file")


@tracked_span("load_file_document_task", tags=_FILE_TAGS)
async def _load_file_document(
    file_path: str,
    content: str,
    user_id: PydanticObjectId,
    title: str | None = None,
    opik_trace_headers: dict[str, str] | None = None,
) -> Document | None:
    return await load_file_document(file_path, content, user_id, title)


load_file_document_task = task(
    _load_file_document,
    name="load-file-document",
    retries=3,
    retry_delay_seconds=5,
)


@flow(name="ingest-file-etl", log_prints=True)
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

    Observability: configures Opik at entry (subprocess-safe) and owns ONE
    trace; the task span nests under it via the forwarded distributed headers.

    NO flow-level ``retries`` (ADR-002 amendment #096, rules 3b + 5): the retry lives on
    ``load_file_document_task``. A flow retry would re-run this body and emit ONE TRACE
    PER ATTEMPT, breaking the "owns ONE trace" contract above — and would stack on the
    task's own retries.
    """

    configure_opik()
    try:
        with span("ingest-file-etl", tags=_FILE_TAGS, metadata=_FILE_METADATA):
            headers = get_distributed_trace_headers()
            result = await load_file_document_task(
                file_path, content, user_id, title, opik_trace_headers=headers
            )
    finally:
        # Flush batched Opik telemetry (fail-open; no-op without OPIK_API_KEY).
        flush_opik()

    if result:
        logger.info("Ingested file: %s", file_path)
    else:
        logger.info("Skipped duplicate file: %s", file_path)

    return result
