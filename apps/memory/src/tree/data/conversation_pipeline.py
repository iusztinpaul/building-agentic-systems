"""
Prefect tasks and flow for conversation ingestion.

Thin wrappers around conversation.py that add retries and logging.
"""

import logging
from datetime import datetime

from beanie import PydanticObjectId
from prefect import flow, task

from tree.data.conversation import load_conversation_document
from tree.entities.documents import Document

logger = logging.getLogger(__name__)


@task(name="load-conversation-document", retries=1, retry_delay_seconds=2)
async def load_conversation_document_task(
    conversation_text: str,
    user_id: PydanticObjectId,
    title: str | None = None,
    session_uri: str | None = None,
    session_started_at: datetime | None = None,
) -> Document | None:
    return await load_conversation_document(
        conversation_text,
        user_id,
        title=title,
        session_uri=session_uri,
        session_started_at=session_started_at,
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
    :func:`tree.data.conversation.load_conversation_document`. See that
    function for the ``source_uri`` derivation rule and the
    ``metadata["session_started_at"]`` storage convention.

    Returns ``None`` if the conversation was already ingested
    (idempotent). Assumes MongoDB/Beanie is already initialised by the
    caller (MCP lifespan, orchestrator, or batch flow).
    """

    result = await load_conversation_document_task(
        conversation_text,
        user_id,
        title=title,
        session_uri=session_uri,
        session_started_at=session_started_at,
    )
    if result is None:
        logger.info("Conversation already ingested, skipping.")
        return None

    logger.info("Ingested conversation: %s", result.source_uri)
    return result
