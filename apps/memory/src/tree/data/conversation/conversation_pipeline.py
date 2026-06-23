"""
Prefect tasks and flow for conversation ingestion.

Thin wrappers around conversation.py that add retries and logging.
"""

import logging
from datetime import datetime

from beanie import PydanticObjectId
from prefect import flow, task

from tree.data.conversation.conversation import load_conversation_document
from tree.entities.documents import Document
from tree.observability import (
    TAGS_INGESTION_BATCH,
    configure_opik,
    flush_opik,
    get_distributed_trace_headers,
    pipeline_metadata,
    span,
    tracked_span,
)

logger = logging.getLogger(__name__)

# Four-tag family: offline Prefect ingestion = ``["ingestion", "batch"]``. The
# former ``"conversation-pipeline"`` pipeline-name tag is now span metadata.
_CONVERSATION_TAGS = TAGS_INGESTION_BATCH
_CONVERSATION_METADATA = pipeline_metadata("conversation")


@tracked_span("load_conversation_document_task", tags=_CONVERSATION_TAGS)
async def _load_conversation_document(
    conversation_text: str,
    user_id: PydanticObjectId,
    title: str | None = None,
    session_uri: str | None = None,
    session_started_at: datetime | None = None,
    opik_trace_headers: dict[str, str] | None = None,
) -> Document | None:
    return await load_conversation_document(
        conversation_text,
        user_id,
        title=title,
        session_uri=session_uri,
        session_started_at=session_started_at,
    )


load_conversation_document_task = task(
    _load_conversation_document,
    name="load-conversation-document",
    retries=1,
    retry_delay_seconds=2,
)


@flow(name="ingest-conversation-etl", log_prints=True)
async def ingest_conversation(
    conversation_text: str,
    user_id: PydanticObjectId,
    title: str | None = None,
    session_uri: str | None = None,
    session_started_at: datetime | None = None,
) -> Document | None:
    """Ingest conversation text as a Document for ``user_id``.

    Forwards optional ``session_uri`` / ``session_started_at`` to
    :func:`tree.data.conversation.conversation.load_conversation_document`. See that
    function for the ``source_uri`` derivation rule and the
    ``metadata["session_started_at"]`` storage convention.

    Returns ``None`` if the conversation was already ingested
    (idempotent). Assumes MongoDB/Beanie is already initialised by the
    caller (MCP lifespan, orchestrator, or batch flow).

    Observability: configures Opik at entry (subprocess-safe) and owns ONE
    trace; the task span nests under it via the forwarded distributed headers.
    """

    configure_opik()
    try:
        with span(
            "ingest-conversation-etl",
            tags=_CONVERSATION_TAGS,
            metadata=_CONVERSATION_METADATA,
        ):
            headers = get_distributed_trace_headers()
            result = await load_conversation_document_task(
                conversation_text,
                user_id,
                title=title,
                session_uri=session_uri,
                session_started_at=session_started_at,
                opik_trace_headers=headers,
            )
    finally:
        # Flush batched Opik telemetry (fail-open; no-op without OPIK_API_KEY).
        flush_opik()

    if result is None:
        logger.info("Conversation already ingested, skipping.")
        return None

    logger.info("Ingested conversation: %s", result.source_uri)
    return result
