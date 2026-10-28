"""Substack unified pipeline — ONE batch flow over single articles + RSS feeds.

Flattens a Substack shard (mixed ``SubstackRssSource`` + ``SubstackArticleSource``)
into one work list — the resolved RSS ``(Document, raw_entry)`` items first, then the
single-article URLs — and walks it in ``app_config.data.batch_size`` chunks: per chunk
it scrapes ONLY the chunk's article URLs, then loads the chunk via the SHARED
``substack.load_document``. The two kinds acquire content differently — RSS items carry
feed-embedded content (NO scrape), single articles are scraped per chunk — but both
normalize to ``(Document, raw_entry)``: the article path wraps its scraped HTML in a
synthetic feed-entry (``{"content": [{"value": …}]}``) so reference extraction + dedup
in ``load_document`` run identically. Once normalized the load can't tell which kind an
item came from.

Failure isolation is preserved at every boundary: a feed that fails to fetch is
logged + skipped (its items absent), an article that fails to scrape is dropped, and
the load isolates per-item failures. The thin single-article MCP flow
(``substack_pipeline.ingest_substack_article``) and the pure helpers
(``substack``) are unchanged.
"""

from __future__ import annotations

import logging
from itertools import batched

from beanie import PydanticObjectId
from prefect import flow, task

from tree.config.app_config import app_config
from tree.config.sources import (
    SubstackArticleSource,
    SubstackRssSource,
)
from tree.config.settings import settings
from tree.data.batch import gather_isolated
from tree.data.substack.substack import (
    as_feed_entry,
    extract_document,
    fetch_and_extract,
    fetch_feed,
    load_document,
)
from tree.db import init_mongodb
from tree.entities.documents import Document

logger = logging.getLogger(__name__)

# A flattened, normalized item: the built Document + the ``raw_entry`` dict
# ``load_document`` reads for reference extraction (the real feed entry for RSS, a
# synthetic ``{"content": [{"value": body_html}]}`` for a scraped article).
_NormalizedItem = tuple[Document, dict]

# One entry of the chunked work list: an already-resolved RSS item, or an article URL
# still to scrape.
_WorkItem = _NormalizedItem | str


@task(name="fetch-substack-rss-feed", retries=3, retry_delay_seconds=5)
async def fetch_rss_feed(feed_url: str) -> list[dict]:
    return await fetch_feed(feed_url)


@task(name="extract-substack-article-batch", retries=3, retry_delay_seconds=5)
async def extract_article_batch(
    article_urls: list[str], user_id: PydanticObjectId
) -> list[_NormalizedItem]:
    """Scrape one chunk's article URLs into ``(Document, raw_entry)`` items.

    Extract+Transform are FUSED (one scrape yields the Document). A per-URL failure is
    logged + skipped by ``gather_isolated``, NOT propagated; the task hard-fails only on
    a batch-WIDE failure. Tier F (plain ``httpx``, free replay) → ``retries=3`` /
    ``retry_delay_seconds=5`` = a 15 s budget (ADR-002 amendment #096).
    """

    async def _resolve(url: str) -> _NormalizedItem:
        return await _resolve_article(url, user_id)

    resolved, failures = await gather_isolated(article_urls, _resolve)
    if failures:
        logger.warning(
            "Article scrape failed for %d/%d URLs", failures, len(article_urls)
        )
    return resolved


@task(name="load-substack-batch", retries=3, retry_delay_seconds=5)
async def load_batch(items: list[_NormalizedItem]) -> list[Document]:
    """Dedup + persist one chunk of ``(Document, raw_entry)`` items, isolated per item.

    Uniform over both kinds — once normalized the load can't tell an RSS item from a
    scraped article. Duplicates drop out as ``None``; a per-item failure is logged +
    skipped. Retried whole-batch on a batch-WIDE infra failure, safe via the
    ``(user_id, source_uri)`` dedup. Tier F (idempotent Mongo write) → 15 s budget.
    """

    async def _load(item: _NormalizedItem) -> Document | None:
        return await load_document(*item)

    ingested, failures = await gather_isolated(items, _load)
    if failures:
        logger.warning("load_batch: %d/%d items failed", failures, len(items))
    return ingested


async def _resolve_feed(
    feed_url: str, user_id: PydanticObjectId
) -> list[_NormalizedItem]:
    """Expand one feed to ``(Document, raw_entry)`` items from feed-embedded content.

    NO re-scrape — ``extract_document`` builds the Document from the feed entry, which
    doubles as the ``raw_entry`` ``load_document`` reads. Isolated per feed.
    """

    entries = await fetch_rss_feed(feed_url)
    return [(extract_document(entry, user_id), entry) for entry in entries]


async def _resolve_article(url: str, user_id: PydanticObjectId) -> _NormalizedItem:
    """Scrape one article and wrap its HTML as a synthetic feed-entry. Isolated per URL."""

    doc, body_html = await fetch_and_extract(url, user_id)
    return doc, as_feed_entry(body_html)


@flow(name="ingest-substack-batch-etl", log_prints=True, validate_parameters=False)
async def ingest_substack_batch(
    entries: list[SubstackRssSource | SubstackArticleSource],
    user_id: PydanticObjectId,
) -> list[Document]:
    """Batch-ingest a Substack shard (RSS feeds + single articles) chunk by chunk.

    Resolves every feed up front from feed-embedded content (no scrape, isolated per
    feed), then walks the work list — RSS items first, then article URLs — in
    ``app_config.data.batch_size`` chunks. Per chunk, ONLY its article URLs are scraped
    (isolated per URL, wrapped in a synthetic feed-entry) and the chunk's items are
    loaded through the SHARED ``load_document``, so the load path is identical for both
    kinds and RSS items are never re-scraped.

    Batch-grain ETL tasks (ADR-002 amendment #096): ``extract-substack-article-batch``
    and ``load-substack-batch`` wrap the per-chunk scrape and load so both get retries,
    matching web. They run once per chunk — never per row — so the task-run count grows
    with ⌈items / batch_size⌉, not with the item count.
    """

    await init_mongodb(
        settings.mongo.mongo_uri.get_secret_value(),
        settings.mongo.mongo_initdb_database,
    )

    feed_urls = [e.uri for e in entries if isinstance(e, SubstackRssSource)]
    article_urls = [e.uri for e in entries if isinstance(e, SubstackArticleSource)]

    work: list[_WorkItem] = []

    # RSS feeds → expand from feed-embedded content (no scrape), isolated per feed.
    if feed_urls:

        async def _resolve_feed_items(feed_url: str) -> list[_NormalizedItem]:
            return await _resolve_feed(feed_url, user_id)

        per_feed, failures = await gather_isolated(feed_urls, _resolve_feed_items)
        if failures:
            logger.warning(
                "Feed resolve failed for %d/%d feeds", failures, len(feed_urls)
            )
        work.extend(item for feed_items in per_feed for item in feed_items)

    # Single articles → scraped per chunk below, isolated per URL.
    work.extend(article_urls)

    if not work:
        logger.info("Substack: no resolvable items in shard")
        return []

    ingested: list[Document] = []
    for chunk in batched(work, app_config.data.batch_size):
        # RSS items precede article URLs in ``work``, so the chunk's load order
        # (RSS items, then scraped articles) matches the unchunked order.
        items = [item for item in chunk if not isinstance(item, str)]
        chunk_urls = [item for item in chunk if isinstance(item, str)]
        if chunk_urls:
            items.extend(await extract_article_batch(chunk_urls, user_id))
        # Uniform load over the chunk — duplicates drop as None, per-item failures
        # are isolated.
        ingested.extend(await load_batch(items))
        logger.info("Substack chunk processed: %d ingested so far", len(ingested))

    logger.info(
        "Substack: ingested %d items (%d feeds, %d single articles)",
        len(ingested),
        len(feed_urls),
        len(article_urls),
    )
    return ingested
