"""
Core logic for file ingestion.

Reads local files (.txt, .md, .html) and persists them as Documents.
"""

import logging
from datetime import UTC, datetime
from pathlib import Path

from pymongo.errors import DuplicateKeyError

from tree.data.substack.substack_rss import html_to_plain_text
from tree.entities.documents import Document, SourceType

logger = logging.getLogger(__name__)

_SUPPORTED_EXTENSIONS = {".txt", ".md", ".html"}


def read_file(file_path: str) -> str:
    """Read content from a local file.

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
    title: str | None = None,
) -> Document | None:
    """Read a file and persist it as a Document.

    Returns the Document, or None if a non-LATENT duplicate already exists.
    """

    path = Path(file_path).resolve()
    content = read_file(str(path))
    source_uri = f"file://{path}"

    existing = await Document.find_one(Document.source_uri == source_uri)
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
