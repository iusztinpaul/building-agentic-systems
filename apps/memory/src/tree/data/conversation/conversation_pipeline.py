"""
Prefect flow for conversation ingestion.

A thin wrapper around conversation.py that adds retries and logging.
"""

import logging
from datetime import datetime

from beanie import PydanticObjectId
from prefect import flow

from tree.data.conversation.conversation import load_conversation_document
from tree.entities.documents import Document
from tree.config.constants import TAGS_DATA_ONLINE
from tree.observability import (
    configure_opik,
    flush_opik,
    pipeline_metadata,
    span,
)

logger = logging.getLogger(__name__)

# Online data ingest (conversation variant of ``online_ingest``) — pipeline-identity
# tags shared 1:1 with this flow's Prefect flow-run tags.
_CONVERSATION_TAGS = TAGS_DATA_ONLINE
_CONVERSATION_METADATA = pipeline_metadata("conversation")


# Tier F — free replay: the body is ONE idempotent Mongo write (deduped on
# ``(user_id, source_type, source_uri)``), so retries live on the FLOW and there
# are NO tasks (ADR-002 amendment #097, superseding #096 rule 3b). Accepted cost:
# a retried attempt emits its own Opik trace — a trace per real retry is signal.
@flow(
    name="ingest-conversation-etl",
    log_prints=True,
    retries=3,
    retry_delay_seconds=5,
)
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
    caller (MCP lifespan, coordinator, or batch flow).

    Observability: configures Opik at entry (subprocess-safe) and owns one
    trace per attempt.
    """

    configure_opik()
    try:
        with span(
            "ingest-conversation-etl",
            tags=_CONVERSATION_TAGS,
            metadata=_CONVERSATION_METADATA,
        ):
            result = await load_conversation_document(
                conversation_text,
                user_id,
                title=title,
                session_uri=session_uri,
                session_started_at=session_started_at,
            )
    finally:
        # Flush batched Opik telemetry (fail-open; no-op without OPIK_API_KEY).
        flush_opik()

    if result is None:
        logger.info("Conversation already ingested, skipping.")
        return None

    logger.info("Ingested conversation: %s", result.source_uri)
    return result
