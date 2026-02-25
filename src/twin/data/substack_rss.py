import logging
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import feedparser
import httpx
from bs4 import BeautifulSoup

from twin.data.core.base import BaseETL
from twin.entities.documents import SourceType
from twin.entities.documents import Document

logger = logging.getLogger(__name__)

URL_PATTERN = re.compile(r"https?://[^\s\"'>]+")


_BLOCK_TAGS = {"p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "tr", "blockquote"}


def _html_to_plain_text(html: str) -> str:
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


def _extract_references(html: str) -> list[str]:
    soup = BeautifulSoup(html, "html.parser")
    urls: list[str] = []
    for tag in soup.find_all("a", href=True):
        href = tag["href"]
        if URL_PATTERN.match(href):
            urls.append(href)

    return list(dict.fromkeys(urls))


def _parse_date(entry: dict) -> datetime:
    published = entry.get("published", "")
    if published:
        try:
            return parsedate_to_datetime(published)
        except ValueError, TypeError:
            pass

    return datetime.now(tz=timezone.utc)


class SubstackRSSFeedETL(BaseETL):
    async def extract_one(self, raw_entry: dict) -> Document:
        content_html = raw_entry.get("content", [{}])[0].get("value", "")
        if not content_html:
            content_html = raw_entry.get("summary", "")

        return Document(
            source_type=SourceType.SUBSTACK,
            source_uri=raw_entry.get("link", ""),
            title=raw_entry.get("title", ""),
            summary=raw_entry.get("summary", raw_entry.get("title", "")),
            content=_html_to_plain_text(content_html),
            authors=[raw_entry.get("author", "Unknown")],
            date=_parse_date(raw_entry),
            references=_extract_references(content_html),
        )

    async def run(self, source_uri: str) -> list[Document]:
        logger.info("Fetching RSS feed: %s", source_uri)

        async with httpx.AsyncClient() as client:
            response = await client.get(source_uri, follow_redirects=True, timeout=30)
            response.raise_for_status()

        feed = feedparser.parse(response.text)
        if feed.bozo and not feed.entries:
            raise ValueError(
                f"Failed to parse RSS feed from {source_uri}: {feed.bozo_exception}"
            )

        documents: list[Document] = []
        for entry in feed.entries:
            doc = await self.extract_one(entry)

            exists = await Document.find(Document.source_uri == doc.source_uri).count()
            if exists:
                logger.debug("Skipping duplicate: %s", doc.source_uri)
                continue

            await doc.insert()
            documents.append(doc)
            logger.info("Ingested: %s", doc.source_uri)

        logger.info("Ingested %d new documents from %s", len(documents), source_uri)

        return documents

    async def run_batch(self, source_uris: list[str]) -> list[Document]:
        all_documents: list[Document] = []
        for uri in source_uris:
            docs = await self.run(uri)
            all_documents.extend(docs)

        return all_documents
