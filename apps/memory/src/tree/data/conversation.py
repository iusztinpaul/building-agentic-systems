"""
Core logic for conversation ingestion.

Persists raw conversation text as Documents for knowledge graph extraction.
"""

import hashlib
import logging
from datetime import UTC, datetime

from pymongo.errors import DuplicateKeyError

from tree.entities.documents import Document, SourceType

logger = logging.getLogger(__name__)


def _content_hash(text: str) -> str:
    """Return a short SHA-256 hex digest of *text*."""
    return hashlib.sha256(text.encode()).hexdigest()[:16]


async def load_conversation_document(
    conversation_text: str,
    title: str | None = None,
) -> Document | None:
    """Persist conversation text as a Document.

    Uses a content-hash based source_uri so that retries and re-ingestion
    of the same text are idempotent.

    Returns the Document, or None if a duplicate already exists.

    Raises:
        ValueError: If conversation_text is empty or whitespace-only.
    """

    if not conversation_text.strip():
        raise ValueError("Conversation text must not be empty.")

    source_uri = f"conversation://{_content_hash(conversation_text)}"
    now = datetime.now(tz=UTC)

    existing = await Document.find_one(Document.source_uri == source_uri)
    if existing is not None:
        logger.info("Conversation already ingested: %s", source_uri)
        return None

    doc = Document(
        source_type=SourceType.CONVERSATION,
        source_uri=source_uri,
        title=title or f"Conversation {now.isoformat()}",
        content=conversation_text,
        date=now,
    )

    try:
        await doc.insert()
    except DuplicateKeyError:
        logger.info("Conversation already ingested (race condition): %s", source_uri)
        return None

    logger.info("Ingested conversation document: %s", source_uri)
    return doc
