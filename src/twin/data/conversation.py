"""
Core logic for conversation ingestion.

Persists raw conversation text as Documents for knowledge graph extraction.
"""

import logging
from datetime import UTC, datetime
from uuid import uuid4

from twin.entities.documents import Document, SourceType

logger = logging.getLogger(__name__)


async def load_conversation_document(
    conversation_text: str,
    title: str | None = None,
) -> Document:
    """Persist conversation text as a Document.

    Each conversation is always treated as unique (UUID-based source_uri).

    Raises:
        ValueError: If conversation_text is empty or whitespace-only.
    """

    if not conversation_text.strip():
        raise ValueError("Conversation text must not be empty.")

    source_uri = f"conversation://{uuid4()}"
    now = datetime.now(tz=UTC)

    doc = Document(
        source_type=SourceType.CONVERSATION,
        source_uri=source_uri,
        title=title or f"Conversation {now.isoformat()}",
        content=conversation_text,
        date=now,
    )
    await doc.insert()

    logger.info("Ingested conversation document: %s", source_uri)
    return doc
