"""
Core logic for conversation ingestion.

Persists raw conversation text as Documents for knowledge graph extraction.
"""

import hashlib
import logging
from datetime import UTC, datetime
from typing import Any

from beanie import PydanticObjectId
from pymongo.errors import DuplicateKeyError

from tree.entities.documents import Document, SourceType

logger = logging.getLogger(__name__)


def _content_hash(text: str) -> str:
    """Return a short SHA-256 hex digest of *text*."""
    return hashlib.sha256(text.encode()).hexdigest()[:16]


def _normalize_session_started_at(value: datetime) -> datetime:
    """Validate that *value* is a tz-aware ``datetime`` in UTC.

    Per project convention (``CLAUDE.md``: "All the dates are timezone
    aware (UTC by default). We don't accept any naive datetime
    objects."), naive datetimes are rejected with ``ValueError`` rather
    than silently coerced. Non-UTC tz-aware datetimes are converted to
    UTC for canonical storage.
    """

    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError(
            "session_started_at must be timezone-aware (UTC); got a naive datetime."
        )
    if value.tzinfo is not UTC:
        return value.astimezone(UTC)
    return value


async def load_conversation_document(
    conversation_text: str,
    user_id: PydanticObjectId,
    title: str | None = None,
    session_uri: str | None = None,
    session_started_at: datetime | None = None,
) -> Document | None:
    """Persist conversation text as a Document for ``user_id``.

    ``source_uri`` rule:

    * If ``session_uri`` is provided (non-empty), it is used verbatim as
      the Document's ``source_uri``. The caller is responsible for it
      being a stable, opaque, schemed string — e.g.
      ``"claude-session://abc123"``, ``"mcp-session://..."``,
      ``"openai-thread://thread_..."``. No validation is performed
      beyond rejecting empty / whitespace-only values.
    * Otherwise, ``source_uri`` falls back to
      ``f"conversation://{_content_hash(text)}"`` — the Phase-1
      content-hash behavior, preserved for callers that have not been
      updated to propagate a session id.

    Dedup is scoped to ``user_id`` via the
    ``(user_id, source_type, source_uri)`` compound unique index, so two
    users can ingest the same transcript independently and two callers
    passing distinct ``session_uri``s on byte-identical text produce two
    distinct Documents.

    ``session_started_at``, if provided, MUST be timezone-aware. It is
    canonicalized to UTC and stored in ``Document.metadata
    ["session_started_at"]`` as a tz-aware ``datetime``.

    Returns the Document, or ``None`` if a duplicate already exists for
    this ``(user_id, source_uri)``.

    Raises:
        ValueError: If ``conversation_text`` is empty / whitespace-only,
            if ``session_uri`` is supplied but empty / whitespace-only,
            or if ``session_started_at`` is a naive datetime.
    """

    if not conversation_text.strip():
        raise ValueError("Conversation text must not be empty.")

    if session_uri is not None and not session_uri.strip():
        raise ValueError(
            "session_uri must not be empty when supplied; pass None to fall "
            "back to the content-hash source_uri."
        )

    if session_uri is not None:
        source_uri = session_uri
    else:
        source_uri = f"conversation://{_content_hash(conversation_text)}"

    metadata: dict[str, Any] = {}
    if session_started_at is not None:
        metadata["session_started_at"] = _normalize_session_started_at(
            session_started_at
        )

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
        metadata=metadata,
    )

    try:
        await doc.insert()
    except DuplicateKeyError:
        logger.info("Conversation already ingested (race condition): %s", source_uri)
        return None

    logger.info("Ingested conversation document: %s", source_uri)
    return doc
