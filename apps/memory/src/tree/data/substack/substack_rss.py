import logging
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import feedparser
import httpx
from beanie import PydanticObjectId
from bs4 import BeautifulSoup
from pymongo.errors import DuplicateKeyError

from tree.entities.documents import Document, SourceType

logger = logging.getLogger(__name__)

URL_PATTERN = re.compile(r"https?://[^\s\"'>]+")

_BLOCK_TAGS = {"p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "tr", "blockquote"}


def html_to_plain_text(html: str) -> str:
    if not html:
        return ""

    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all(_BLOCK_TAGS):
        tag.insert_before("\n")
        tag.append("\n")
    for br in soup.find_all("br"):
        br.replace_with("\n")

    text = soup.get_text()
    lines = [line.strip() for line in text.splitlines()]

    return "\n".join(line for line in lines if line)


def extract_references(html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    urls: list[str] = []
    for tag in soup.find_all("a", href=True):
        href = tag["href"]
        if URL_PATTERN.match(href):
            urls.append(href)

    return list(dict.fromkeys(urls))


def parse_date(entry: dict) -> datetime:
    published = entry.get("published", "")
    if published:
        try:
            return parsedate_to_datetime(published)
        except ValueError, TypeError:
            pass

    return datetime.now(tz=timezone.utc)


def extract_document(raw_entry: dict, user_id: PydanticObjectId) -> Document:
    content_html = raw_entry.get("content", [{}])[0].get("value", "")
    if not content_html:
        content_html = raw_entry.get("summary", "")

    return Document(
        source_type=SourceType.SUBSTACK,
        source_uri=raw_entry.get("link", ""),
        user_id=user_id,
        title=raw_entry.get("title", ""),
        summary=raw_entry.get("summary", raw_entry.get("title", "")),
        content=html_to_plain_text(content_html),
        authors=[raw_entry.get("author", "Unknown")],
        date=parse_date(raw_entry),
    )


async def resolve_references(
    uris: list[str], user_id: PydanticObjectId
) -> list[Document]:
    """Find or create LATENT Documents for each reference URI under ``user_id``."""

    ref_docs: list[Document] = []
    for uri in uris:
        existing = await Document.find_one({"user_id": user_id, "source_uri": uri})
        if existing:
            ref_docs.append(existing)
            continue

        try:
            latent_doc = Document(
                source_type=SourceType.LATENT,
                source_uri=uri,
                user_id=user_id,
            )
            await latent_doc.insert()
            ref_docs.append(latent_doc)
            logger.debug("Created latent document: %s", uri)
        except DuplicateKeyError:
            existing = await Document.find_one({"user_id": user_id, "source_uri": uri})
            if existing:
                ref_docs.append(existing)

    return ref_docs


async def load_document(doc: Document, raw_entry: dict) -> Document | None:
    """Dedup, resolve references, and persist a single document.

    ``doc.user_id`` is the tenant under which dedup runs; reference URIs
    are scoped to the same user.

    Returns the persisted Document, or None if skipped as duplicate.
    """

    existing = await Document.find_one(
        {"user_id": doc.user_id, "source_uri": doc.source_uri}
    )
    if existing and existing.source_type != SourceType.LATENT:
        logger.debug("Skipping duplicate: %s", doc.source_uri)
        return None

    content_html = raw_entry.get("content", [{}])[0].get("value", "")
    ref_uris = [
        uri for uri in extract_references(content_html) if uri != doc.source_uri
    ]
    doc.references = await resolve_references(ref_uris, doc.user_id)

    if existing:
        doc.id = existing.id
        await doc.replace()
        logger.info("Upgraded latent document: %s", doc.source_uri)
    else:
        await doc.insert()
        logger.info("Ingested: %s", doc.source_uri)

    return doc


async def fetch_feed(source_uri: str) -> list[dict]:
    """Fetch and parse an RSS feed, returning raw entries."""

    logger.info("Fetching RSS feed: %s", source_uri)

    async with httpx.AsyncClient() as client:
        response = await client.get(source_uri, follow_redirects=True, timeout=30)
        response.raise_for_status()

    feed = feedparser.parse(response.text)
    if feed.bozo and not feed.entries:
        raise ValueError(
            f"Failed to parse RSS feed from {source_uri}: {feed.bozo_exception}"
        )

    return list(feed.entries)
