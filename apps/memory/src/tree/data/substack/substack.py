"""Pure Substack helpers — RSS-feed and single-article acquisition + the shared load.

Two acquisition paths, one load: RSS entries carry feed-embedded content (no scrape),
single articles are scraped and parsed from page HTML. Both normalize to
``(Document, raw_entry)`` — the article path wraps its scraped body HTML in a
synthetic feed-entry (:func:`as_feed_entry`) — so :func:`load_document` dedups,
resolves references, and persists identically for both.
"""

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

_ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


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


def entry_content_html(raw_entry: dict) -> str:
    """Body HTML of a feed entry (a real feedparser one or a synthetic article one).

    Returns ``""`` when the entry carries no content — including when ``content``
    is present but EMPTY, which a bare ``entry["content"][0]`` would IndexError on.
    """

    content = raw_entry.get("content") or [{}]

    return content[0].get("value", "")


def extract_document(raw_entry: dict, user_id: PydanticObjectId) -> Document:
    content_html = entry_content_html(raw_entry)
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

    content_html = entry_content_html(raw_entry)
    ref_uris = [
        uri for uri in extract_references(content_html) if uri != doc.source_uri
    ]
    doc.references = await resolve_references(ref_uris, doc.user_id)

    if existing:
        doc.id = existing.id
        await doc.replace()
        logger.info("Upgraded latent document: %s", doc.source_uri)
    else:
        try:
            await doc.insert()
        except DuplicateKeyError:
            # Concurrent insert of the same (user_id, source_type, source_uri) — e.g.
            # the same article resolved from both a feed and a single source in one
            # flattened batch. The unique index lets one win; this attempt is a clean
            # skip, not a failure.
            logger.debug("Skipping concurrent duplicate: %s", doc.source_uri)
            return None
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


async def fetch_article(article_url: str) -> str:
    """Fetch the full HTML of a single Substack article page."""

    logger.info("Fetching Substack article: %s", article_url)

    async with httpx.AsyncClient() as client:
        response = await client.get(article_url, follow_redirects=True, timeout=30)
        response.raise_for_status()

    return response.text


def _extract_meta(soup: BeautifulSoup, property_name: str) -> str:
    """Extract content from an Open Graph or generic meta tag."""

    tag = soup.find("meta", attrs={"property": property_name})
    if tag and tag.get("content"):
        return tag["content"]

    tag = soup.find("meta", attrs={"name": property_name})
    if tag and tag.get("content"):
        return tag["content"]

    return ""


def _parse_iso_utc(value: str) -> datetime | None:
    """Parse an ISO-8601 string to a tz-aware UTC datetime, or ``None`` if it isn't one.

    A naive timestamp is assumed UTC — the project accepts no naive datetimes.
    """

    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _parse_article_date(soup: BeautifulSoup) -> datetime:
    """Extract the publication date from meta tags or time elements."""

    candidates = [
        _extract_meta(soup, "article:published_time"),
        _extract_meta(soup, "og:article:published_time"),
    ]

    time_tag = soup.find("time")
    if time_tag:
        candidates.append(time_tag.get("datetime", ""))
        text = time_tag.get_text(strip=True)
        if _ISO_DATE_RE.match(text):
            candidates.append(text)

    for candidate in candidates:
        parsed = _parse_iso_utc(candidate)
        if parsed:
            return parsed

    return datetime.now(tz=timezone.utc)


def _extract_article_body(soup: BeautifulSoup) -> str:
    """Extract the main article body HTML from a Substack page."""

    body = soup.find("div", class_="body")
    if body:
        return str(body)

    body = soup.find("article")
    if body:
        return str(body)

    return ""


def extract_document_from_html(
    html: str, article_url: str, user_id: PydanticObjectId
) -> tuple[Document, str]:
    """Parse a Substack article HTML page into a Document plus its raw body HTML.

    Returns the body HTML alongside the Document because every caller needs both
    and this is the only place the page is parsed — the body is what
    :func:`load_article_document` feeds to reference extraction.
    """

    soup = BeautifulSoup(html, "html.parser")

    title = _extract_meta(soup, "og:title") or _extract_meta(soup, "twitter:title")
    if not title:
        title_tag = soup.find("title")
        title = title_tag.get_text(strip=True) if title_tag else ""

    summary = (
        _extract_meta(soup, "og:description")
        or _extract_meta(soup, "twitter:description")
        or _extract_meta(soup, "description")
    )

    # ``_extract_meta`` already falls back from property= to name=, so a bare
    # name="author" tag is covered by the first term.
    author = (
        _extract_meta(soup, "author")
        or _extract_meta(soup, "article:author")
        or "Unknown"
    )

    body_html = _extract_article_body(soup)

    doc = Document(
        source_type=SourceType.SUBSTACK,
        source_uri=article_url,
        user_id=user_id,
        title=title,
        summary=summary or title,
        content=html_to_plain_text(body_html),
        authors=[author],
        date=_parse_article_date(soup),
    )

    return doc, body_html


def as_feed_entry(body_html: str) -> dict:
    """Wrap scraped body HTML in the synthetic feed-entry shape ``load_document`` reads.

    The article path has no feed entry, so it fakes the one field
    :func:`load_document` consumes. Lives here so the batch path and
    :func:`load_article_document` can't drift on the shape.
    """

    return {"content": [{"value": body_html}]}


async def load_article_document(doc: Document, body_html: str) -> Document | None:
    """Resolve references and persist a single article document.

    Delegates to the shared load_document helper, passing a synthetic
    raw_entry so reference extraction works identically to the RSS path.
    """

    return await load_document(doc, as_feed_entry(body_html))


async def fetch_and_extract(
    article_url: str, user_id: PydanticObjectId
) -> tuple[Document, str]:
    """Fetch an article and extract a Document plus the raw body HTML."""

    html = await fetch_article(article_url)

    return extract_document_from_html(html, article_url, user_id)
