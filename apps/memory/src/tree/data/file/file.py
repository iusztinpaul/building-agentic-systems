"""
Core logic for file ingestion.

Persists caller-read file text as Documents. The pipeline itself never
touches the filesystem: files only exist on the CALLER's machine (CLI or
MCP client), so content is read at that edge (via :func:`read_file`) and
passed in as a payload — mirroring the conversation pipeline's
``session_uri`` (identity) / ``conversation_text`` (payload) split.
"""

import logging
from datetime import UTC, datetime
from pathlib import Path

from beanie import PydanticObjectId
from pymongo.errors import DuplicateKeyError

from tree.data.substack.substack import html_to_plain_text
from tree.entities.documents import Document, SourceType

logger = logging.getLogger(__name__)

_SUPPORTED_EXTENSIONS = {".txt", ".md", ".html"}


def read_file(file_path: str) -> str:
    """Read content from a local file — EDGE helper, not called by the pipeline.

    Call this where the file actually lives (CLI script, MCP client) and pass
    the result to :func:`load_file_document` as ``content``.

    Supports .txt, .md (read as-is) and .html (converted to plain text).

    Raises:
        FileNotFoundError: If the file does not exist.
        IsADirectoryError: If the path points to a directory.
        ValueError: If the file extension is not supported.
    """

    path = Path(file_path).resolve()

    if not path.exists():
        raise FileNotFoundError(f"File not found: {path}")
    if path.is_dir():
        raise IsADirectoryError(f"Path is a directory, not a file: {path}")

    suffix = path.suffix.lower()
    if suffix not in _SUPPORTED_EXTENSIONS:
        raise ValueError(
            f"Unsupported file type '{suffix}'. "
            f"Supported: {', '.join(sorted(_SUPPORTED_EXTENSIONS))}"
        )

    raw = path.read_text(encoding="utf-8")

    if suffix == ".html":
        return html_to_plain_text(raw)

    return raw


async def load_file_document(
    file_path: str,
    content: str,
    user_id: PydanticObjectId,
    title: str | None = None,
) -> Document | None:
    """Persist caller-read file ``content`` as a Document for ``user_id``.

    ``file_path`` is identity only — it names the file on the CALLER's
    machine (source_uri + default title) and is never opened here; the
    server may not share a filesystem with the caller. It is NOT resolved
    or canonicalized, so callers should pass absolute paths for stable
    dedup keys.

    Returns the Document, or None if a non-LATENT duplicate already exists.
    Dedup and LATENT promotion are scoped to ``user_id`` so two users can
    ingest the same path independently.

    Raises:
        ValueError: If ``content`` is empty / whitespace-only.
    """

    if not content.strip():
        raise ValueError("File content must not be empty.")

    path = Path(file_path)
    source_uri = f"file://{path}"

    existing = await Document.find_one({"user_id": user_id, "source_uri": source_uri})
    if existing is not None:
        if existing.source_type != SourceType.LATENT:
            logger.info("File already ingested: %s", source_uri)
            return None

        existing.source_type = SourceType.FILE
        existing.title = title or path.name
        existing.content = content
        existing.date = datetime.now(tz=UTC)
        await existing.replace()
        logger.info("Upgraded LATENT document for file: %s", source_uri)
        return existing

    doc = Document(
        source_type=SourceType.FILE,
        source_uri=source_uri,
        user_id=user_id,
        title=title or path.name,
        content=content,
        date=datetime.now(tz=UTC),
    )

    try:
        await doc.insert()
    except DuplicateKeyError:
        logger.info("File already ingested (race condition): %s", source_uri)
        return None

    logger.info("Ingested file document: %s", source_uri)
    return doc
