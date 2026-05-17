"""
Prefect tasks and flow for conversation ingestion.

Thin wrappers around conversation.py that add retries and logging.
"""

import logging

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
) -> Document | None:
    return await load_conversation_document(conversation_text, user_id, title)


@flow(name="ingest-conversation-etl", log_prints=True)
async def ingest_conversation(
    conversation_text: str,
    user_id: PydanticObjectId,
    title: str | None = None,
) -> Document | None:
    """Ingest conversation text as a Document for ``user_id``.

    Returns None if the conversation was already ingested (idempotent).
    Assumes MongoDB/Beanie is already initialised by the caller
    (MCP lifespan, orchestrator, or batch flow).
    """

    result = await load_conversation_document_task(conversation_text, user_id, title)
    if result is None:
        logger.info("Conversation already ingested, skipping.")
        return None

    logger.info("Ingested conversation: %s", result.source_uri)
    return result
