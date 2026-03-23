import logging
from datetime import datetime, timezone

from datasets import load_dataset
from pymongo.errors import DuplicateKeyError

from twin.entities.documents import Document, SourceType

logger = logging.getLogger(__name__)

ARXIV_HF_DATASET = "librarian-bots/arxiv-metadata-snapshot"
ARXIV_BASE_URL = "https://arxiv.org/abs/"


def parse_update_date(date_str: str | None) -> datetime:
    """Parse an arxiv update_date string (YYYY-MM-DD) into a timezone-aware datetime."""

    if date_str:
        try:
            return datetime.strptime(date_str.strip(), "%Y-%m-%d").replace(
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


def extract_document(raw_entry: dict) -> Document:
    """Transform a single raw arxiv dataset entry into a Document."""

    arxiv_id = raw_entry.get("id", "")
    source_uri = f"{ARXIV_BASE_URL}{arxiv_id}" if arxiv_id else ""

    abstract = (raw_entry.get("abstract") or "").strip()
    categories = (raw_entry.get("categories") or "").strip()

    return Document(
        source_type=SourceType.HUGGINGFACE,
        source_uri=source_uri,
        title=(raw_entry.get("title") or "").strip(),
        summary=categories,
        content=abstract,
        authors=parse_authors(raw_entry.get("authors")),
        date=parse_update_date(raw_entry.get("update_date")),
    )


def fetch_dataset(max_samples: int) -> list[dict]:
    """Stream the arxiv dataset from HuggingFace and return up to max_samples entries."""

    logger.info(
        "Streaming arxiv dataset from %s (max_samples=%d)",
        ARXIV_HF_DATASET,
        max_samples,
    )

    ds = load_dataset(ARXIV_HF_DATASET, split="train", streaming=True)

    entries: list[dict] = []
    for i, entry in enumerate(ds):
        if i >= max_samples:
            break
        entries.append(dict(entry))

    logger.info("Fetched %d entries from arxiv dataset", len(entries))

    return entries


async def load_document(doc: Document) -> Document | None:
    """Dedup and persist a single arxiv document.

    Returns the persisted Document, or None if skipped as duplicate.
    """

    existing = await Document.find_one(Document.source_uri == doc.source_uri)
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
