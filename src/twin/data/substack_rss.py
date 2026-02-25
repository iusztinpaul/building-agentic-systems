import asyncio
import logging
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import feedparser
import httpx
from bs4 import BeautifulSoup
from prefect import flow, task
from pymongo.errors import DuplicateKeyError

from twin.config.settings import settings
from twin.data.core.base import BaseETL
from twin.db import init_mongodb
from twin.entities.documents import Document, SourceType

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
        )

    async def _resolve_references(self, uris: list[str]) -> list[Document]:
        """Find or create Documents for each reference URI."""

        ref_docs: list[Document] = []
        for uri in uris:
            existing = await Document.find_one(Document.source_uri == uri)
            if existing:
                ref_docs.append(existing)
                continue

            try:
                latent_doc = Document(source_type=SourceType.LATENT, source_uri=uri)
                await latent_doc.insert()
                ref_docs.append(latent_doc)
                logger.debug("Created latent document: %s", uri)
            except DuplicateKeyError:
                existing = await Document.find_one(Document.source_uri == uri)
                if existing:
                    ref_docs.append(existing)

        return ref_docs

    async def _load_one(self, doc: Document, raw_entry: dict) -> Document | None:
        """Dedup, resolve references, and persist a single document.

        Returns the persisted Document, or None if skipped as duplicate.
        """

        existing = await Document.find_one(Document.source_uri == doc.source_uri)
        if existing and existing.source_type != SourceType.LATENT:
            logger.debug("Skipping duplicate: %s", doc.source_uri)
            return None

        content_html = raw_entry.get("content", [{}])[0].get("value", "")
        ref_uris = [
            uri for uri in _extract_references(content_html) if uri != doc.source_uri
        ]
        doc.references = await self._resolve_references(ref_uris)

        if existing:
            doc.id = existing.id
            await doc.replace()
            logger.info("Upgraded latent document: %s", doc.source_uri)
        else:
            await doc.insert()
            logger.info("Ingested: %s", doc.source_uri)

        return doc

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

        documents = await asyncio.gather(
            *[self.extract_one(entry) for entry in feed.entries]
        )

        ingested: list[Document] = []
        for doc, entry in zip(documents, feed.entries):
            result = await self._load_one(doc, entry)
            if result:
                ingested.append(result)

        logger.info("Ingested %d new documents from %s", len(ingested), source_uri)

        return ingested

    async def run_batch(self, source_uris: list[str]) -> list[Document]:
        results = await asyncio.gather(
            *[self.run(source_uri) for source_uri in source_uris]
        )

        return [doc for docs in results for doc in docs]


# --- Prefect workflows ---

_etl = SubstackRSSFeedETL()


@task(name="extract-substack-entry", retries=2, retry_delay_seconds=1.0)
async def extract_substack_entry(raw_entry: dict) -> Document:
    return await _etl.extract_one(raw_entry)


@flow(name="ingest-substack-feed", log_prints=True)
async def ingest_substack_feed(source_uri: str) -> list[Document]:
    return await _etl.run(source_uri)


@flow(name="run-substack-rss-etl", log_prints=True)
async def run_substack_rss_etl(feed_urls: list[str]) -> list[Document]:
    await init_mongodb(settings.mongo.mongo_uri, settings.mongo.mongo_initdb_database)

    return await _etl.run_batch(feed_urls)
