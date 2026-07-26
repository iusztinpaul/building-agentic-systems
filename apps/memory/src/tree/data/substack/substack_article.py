import logging
import re
from datetime import datetime, timezone

import httpx
from beanie import PydanticObjectId
from bs4 import BeautifulSoup

from tree.data.substack.substack_rss import (
    html_to_plain_text,
    load_document,
)
from tree.entities.documents import Document, SourceType

logger = logging.getLogger(__name__)

_ISO_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


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
    ``substack_rss.load_document`` consumes. Lives here so the batch path and
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
