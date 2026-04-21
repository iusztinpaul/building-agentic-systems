import logging
import re
from datetime import datetime, timezone

import httpx
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


def _parse_article_date(soup: BeautifulSoup) -> datetime:
    """Extract the publication date from meta tags or time elements."""

    for attr in ("article:published_time", "og:article:published_time"):
        value = _extract_meta(soup, attr)
        if value:
            try:
                dt = datetime.fromisoformat(value)
                return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
            except ValueError:
                pass

    time_tag = soup.find("time")
    if time_tag:
        dt_attr = time_tag.get("datetime", "")
        if dt_attr:
            try:
                dt = datetime.fromisoformat(dt_attr)
                return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
            except ValueError:
                pass

        text = time_tag.get_text(strip=True)
        if _ISO_DATE_RE.match(text):
            try:
                dt = datetime.fromisoformat(text)
                return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
            except ValueError:
                pass

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


def extract_document_from_html(html: str, article_url: str) -> Document:
    """Parse a Substack article HTML page into a Document."""

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

    author = _extract_meta(soup, "author") or _extract_meta(soup, "article:author")
    if not author:
        author_tag = soup.find("meta", attrs={"name": "author"})
        author = (
            author_tag["content"]
            if author_tag and author_tag.get("content")
            else "Unknown"
        )

    body_html = _extract_article_body(soup)
    content = html_to_plain_text(body_html)

    date = _parse_article_date(soup)

    return Document(
        source_type=SourceType.SUBSTACK,
        source_uri=article_url,
        title=title,
        summary=summary or title,
        content=content,
        authors=[author],
        date=date,
    )


async def load_article_document(doc: Document, body_html: str) -> Document | None:
    """Resolve references and persist a single article document.

    Delegates to the shared load_document helper, passing a synthetic
    raw_entry so reference extraction works identically to the RSS path.
    """

    synthetic_entry = {"content": [{"value": body_html}]}
    return await load_document(doc, synthetic_entry)


async def fetch_and_extract(article_url: str) -> tuple[Document, str]:
    """Fetch an article and extract a Document plus the raw body HTML."""

    html = await fetch_article(article_url)
    soup = BeautifulSoup(html, "html.parser")
    body_html = _extract_article_body(soup)
    doc = extract_document_from_html(html, article_url)

    return doc, body_html
