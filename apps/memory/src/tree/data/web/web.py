"""Web ingestion core: fetch a URL via Bright Data Web Unlocker, build a Document, persist it.

Mirrors the layered structure of ``tree.data.substack.substack_article`` (extraction
helpers + ``fetch_and_extract_*`` + ``load_*_document``) and the LATENT-promotion
pattern from ``tree.data.file.load_file_document``.

Persistence rules:

- ``find_one`` first; if a non-LATENT duplicate exists, return ``None``.
- If a ``LATENT`` document exists, upgrade it in place (replace) to ``WEB``.
- Otherwise insert; on a ``DuplicateKeyError`` race, return ``None``.
- Never use ``replace_one(upsert=True)`` — that would silently overwrite documents
  promoted by other pipelines.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from urllib.parse import urlparse

from beanie import PydanticObjectId
from pymongo.errors import DuplicateKeyError

from tree.data.web.web_unlocker import fetch_url
from tree.entities.documents import Document, SourceType

logger = logging.getLogger(__name__)

_SUMMARY_MAX_CHARS = 300
_H1_RE = re.compile(r"^\s*#\s+(.+?)\s*$", re.MULTILINE)
_WHITESPACE_RE = re.compile(r"\s+")


def _derive_title(markdown: str, url: str) -> str:
    """Pick a human-readable title for the document.

    Order of preference:
      1. The first markdown ``# H1`` heading.
      2. The URL's last non-empty path segment, hyphens/underscores replaced
         with spaces, then title-cased.
      3. The URL host (title-cased) when the URL has no path segments.
    """

    match = _H1_RE.search(markdown)
    if match:
        title = match.group(1).strip()
        # Strip any extra leading hashes left over from "## " edge cases.
        title = title.lstrip("#").strip()
        if title:
            return title

    parsed = urlparse(url)
    segments = [seg for seg in parsed.path.split("/") if seg]
    if segments:
        tail = segments[-1]
        return tail.replace("-", " ").replace("_", " ").title()

    return parsed.netloc.title() if parsed.netloc else url


def _derive_summary(markdown: str) -> str:
    """First 300 chars of the body, single-line, whitespace-collapsed."""

    if not markdown:
        return ""

    collapsed = _WHITESPACE_RE.sub(" ", markdown).strip()
    return collapsed[:_SUMMARY_MAX_CHARS]


async def fetch_and_extract_web(url: str, user_id: PydanticObjectId) -> Document:
    """Fetch ``url`` via Bright Data Web Unlocker (markdown) and build a Document.

    The returned Document has ``source_type=SourceType.WEB``, ``source_uri=url``,
    a derived ``title`` (first H1 → URL path tail), a 300-char ``summary``,
    the verbatim markdown ``content``, ``authors=["Unknown"]``, and a
    timezone-aware UTC ``date``. The document is **not** persisted.
    """

    markdown = await fetch_url(url, data_format="markdown")

    title = _derive_title(markdown, url)
    summary = _derive_summary(markdown)

    return Document(
        source_type=SourceType.WEB,
        source_uri=url,
        user_id=user_id,
        title=title,
        summary=summary,
        content=markdown,
        authors=["Unknown"],
        date=datetime.now(tz=UTC),
    )


async def load_web_document(doc: Document) -> Document | None:
    """Persist a single web Document with idempotent upsert semantics
    (scoped to ``doc.user_id``).

    Returns the persisted Document, or ``None`` if a non-LATENT duplicate
    already exists or a concurrent insert wins the race.
    """

    existing = await Document.find_one(
        {"user_id": doc.user_id, "source_uri": doc.source_uri}
    )
    if existing is not None:
        if existing.source_type != SourceType.LATENT:
            logger.info("Web URL already ingested: %s", doc.source_uri)
            return None

        existing.source_type = SourceType.WEB
        existing.title = doc.title
        existing.summary = doc.summary
        existing.content = doc.content
        existing.authors = doc.authors
        existing.date = datetime.now(tz=UTC)
        await existing.replace()
        logger.info("Upgraded LATENT document for web URL: %s", doc.source_uri)
        return existing

    try:
        await doc.insert()
    except DuplicateKeyError:
        logger.info("Web URL already ingested (race condition): %s", doc.source_uri)
        return None

    logger.info("Ingested web document: %s", doc.source_uri)
    return doc
