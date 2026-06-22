import logging
from collections.abc import Generator
from datetime import datetime, timezone

import httpx
from beanie import PydanticObjectId
from bs4 import BeautifulSoup
from datasets import load_dataset
from pymongo.errors import DuplicateKeyError

from tree.entities.documents import Document, SourceType

logger = logging.getLogger(__name__)

ARXIV_HF_DATASET = "librarian-bots/arxiv-metadata-snapshot"
ARXIV_BASE_URL = "https://arxiv.org/abs/"
ARXIV_HTML_URL = "https://arxiv.org/html/"


def parse_update_date(date_value: str | datetime | None) -> datetime:
    """Parse an arxiv update_date string (YYYY-MM-DD) or datetime into a timezone-aware datetime."""

    if isinstance(date_value, datetime):
        if date_value.tzinfo is None:
            return date_value.replace(tzinfo=timezone.utc)
        return date_value

    if date_value:
        try:
            return datetime.strptime(date_value.strip(), "%Y-%m-%d").replace(
                tzinfo=timezone.utc
            )
        except ValueError, TypeError:
            pass

    return datetime.now(tz=timezone.utc)


def parse_authors(authors_str: str | None) -> list[str]:
    """Parse the arxiv authors string into a list of individual author names."""

    if not authors_str:
        return ["Unknown"]

    authors = [a.strip() for a in authors_str.split(",") if a.strip()]

    return authors if authors else ["Unknown"]


def extract_document(raw_entry: dict, user_id: PydanticObjectId) -> Document | None:
    """Transform a single raw arxiv dataset entry into a Document.

    Returns None if the entry has no ID.
    """

    arxiv_id = raw_entry.get("id", "")
    if not arxiv_id:
        logger.warning(
            "Skipping arxiv entry with missing or empty ID: %s",
            raw_entry.get("title", "<no title>"),
        )
        return None

    source_uri = f"{ARXIV_BASE_URL}{arxiv_id}"

    abstract = (raw_entry.get("abstract") or "").strip()

    return Document(
        source_type=SourceType.HUGGINGFACE,
        source_uri=source_uri,
        user_id=user_id,
        title=(raw_entry.get("title") or "").strip(),
        summary=abstract,
        content="",
        authors=parse_authors(raw_entry.get("authors")),
        date=parse_update_date(raw_entry.get("update_date")),
    )


def fetch_dataset_batches(
    max_samples: int, batch_size: int, offset: int | None = None
) -> Generator[list[dict], None, None]:
    """Stream the arxiv dataset from HuggingFace and yield batches of entries.

    Yields lists of up to ``batch_size`` entries, stopping after ``max_samples`` total.

    ``offset`` selects a disjoint window of the stream (#071): when it is a positive
    int the first ``offset`` rows are discarded via ``IterableDataset.skip(offset)``
    BEFORE iteration, so the yielded rows cover exactly ``[offset, offset + max_samples)``
    (``max_samples`` is counted WITHIN the post-skip window). A falsy ``offset``
    (``None`` or ``0``) applies NO skip — byte-for-byte today's single-run ingest. A
    negative ``offset`` is rejected (``.skip`` is never called with a negative value).
    """

    if offset is not None and offset < 0:
        raise ValueError(f"offset must be >= 0, got {offset}")

    logger.info(
        "Streaming arxiv dataset from %s (offset=%s, max_samples=%d, batch_size=%d)",
        ARXIV_HF_DATASET,
        offset,
        max_samples,
        batch_size,
    )

    ds = load_dataset(ARXIV_HF_DATASET, split="train", streaming=True)

    if offset:
        ds = ds.skip(offset)

    batch: list[dict] = []
    count = 0
    for entry in ds:
        if count >= max_samples:
            break
        batch.append(dict(entry))
        count += 1
        if len(batch) >= batch_size:
            logger.info(
                "Yielding batch of %d entries (total so far: %d)", len(batch), count
            )
            yield batch
            batch = []

    if batch:
        logger.info("Yielding final batch of %d entries (total: %d)", len(batch), count)
        yield batch

    logger.info(
        "Finished streaming. Window [offset=%s, count=%d] → rows [%d, %d)",
        offset,
        count,
        offset or 0,
        (offset or 0) + count,
    )


async def fetch_paper_content(source_uri: str) -> str:
    """Fetch the full paper content from the arXiv HTML endpoint.

    Returns the extracted text or an empty string if the paper
    is not available in HTML format.
    """

    arxiv_id = source_uri.removeprefix(ARXIV_BASE_URL)
    if not arxiv_id:
        return ""

    url = f"{ARXIV_HTML_URL}{arxiv_id}"
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=30) as client:
            response = await client.get(url)
        if response.status_code != 200:
            logger.debug(
                "HTML not available for %s (status %d)", arxiv_id, response.status_code
            )
            return ""

        soup = BeautifulSoup(response.text, "html.parser")
        article = (
            soup.find("article")
            or soup.find("main")
            or soup.find("div", class_="ltx_page_content")
        )
        if not article:
            logger.debug("No article element found for %s", arxiv_id)
            return ""

        text = article.get_text(separator="\n", strip=True)
        logger.info("Fetched full content for %s (%d chars)", arxiv_id, len(text))
        return text

    except httpx.HTTPError:
        logger.warning("Failed to fetch HTML for %s", arxiv_id, exc_info=True)
        return ""


async def load_document(doc: Document) -> Document | None:
    """Dedup and persist a single arxiv document (scoped to ``doc.user_id``).

    Returns the persisted Document, or None if skipped as duplicate.
    """

    existing = await Document.find_one(
        {"user_id": doc.user_id, "source_uri": doc.source_uri}
    )
    if existing and existing.source_type != SourceType.LATENT:
        logger.debug("Skipping duplicate: %s", doc.source_uri)
        return None

    if existing:
        doc.id = existing.id
        await doc.replace()
        logger.info("Upgraded latent document: %s", doc.source_uri)
    else:
        try:
            await doc.insert()
            logger.info("Ingested: %s", doc.source_uri)
        except DuplicateKeyError:
            logger.debug("Duplicate key on insert, skipping: %s", doc.source_uri)
            return None

    return doc
