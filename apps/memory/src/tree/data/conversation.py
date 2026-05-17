"""
Core logic for conversation ingestion.

Persists raw conversation text as Documents for knowledge graph extraction.
"""

import hashlib
import logging
from datetime import UTC, datetime

from beanie import PydanticObjectId
from pymongo.errors import DuplicateKeyError

from tree.entities.documents import Document, SourceType

logger = logging.getLogger(__name__)


def _content_hash(text: str) -> str:
    """Return a short SHA-256 hex digest of *text*."""
    return hashlib.sha256(text.encode()).hexdigest()[:16]


async def load_conversation_document(
    conversation_text: str,
    user_id: PydanticObjectId,
    title: str | None = None,
) -> Document | None:
    """Persist conversation text as a Document for ``user_id``.

    Uses a content-hash based source_uri so that retries and re-ingestion
    of the same text are idempotent. Dedup is scoped to ``user_id`` so
    two users can ingest the same transcript independently.

    Returns the Document, or None if a duplicate already exists.

    Raises:
        ValueError: If conversation_text is empty or whitespace-only.
    """

    if not conversation_text.strip():
        raise ValueError("Conversation text must not be empty.")

    source_uri = f"conversation://{_content_hash(conversation_text)}"
    now = datetime.now(tz=UTC)

    existing = await Document.find_one({"user_id": user_id, "source_uri": source_uri})
    if existing is not None:
        logger.info("Conversation already ingested: %s", source_uri)
        return None

    doc = Document(
        source_type=SourceType.CONVERSATION,
        source_uri=source_uri,
        user_id=user_id,
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
