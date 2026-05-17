"""Prefect pipeline wrappers around the YouTube RSS-feed ETL.

Mirrors `tree.data.substack.substack_rss_pipeline` line-for-line — same
``@task`` / ``@flow`` shape, same ``init_mongodb`` boundary in the batch
flow, same ``log_prints=True``.

Differences vs the Substack RSS pipeline are forced by YouTube semantics:

- We perform a single bulk transcript fetch via
  `TranscriptFetcher.fetch_many(...)` over all canonical URLs from the feed,
  rather than one HTTP call per entry. The chain wrapper handles per-slot
  primary→Gemini fallback transparently.
- We skip the oEmbed round-trip used by #003 because Atom feed entries
  already carry title / channel / publish_date. One feed fetch + one bulk
  transcript fetch instead of (1 + 2N) HTTP calls.
- One missing transcript (chain returns ``None`` for that slot) does NOT
  sink the batch; the chain wrapper has already emitted the WARNING. We do
  emit a pipeline-layer WARNING for the *unrelated* class of failure where
  an Atom entry has no resolvable video id (a feed-parsing problem the
  chain never sees).
"""

from __future__ import annotations

import asyncio
import logging

from beanie import PydanticObjectId
from prefect import flow, task

from tree.config.settings import settings
from tree.data.youtube.transcript_fetcher import TranscriptFetcher
from tree.data.youtube.urls import extract_video_id
from tree.data.youtube.youtube_rss import (
    extract_video_url,
    feed_entry_to_metadata,
    fetch_feed,
)
from tree.data.youtube.youtube_video import build_document, load_video_document
from tree.data.youtube.youtube_video_pipeline import _default_chained_fetcher
from tree.db import init_mongodb
from tree.entities.documents import Document

logger = logging.getLogger(__name__)


@task(name="fetch-youtube-rss-feed", retries=2, retry_delay_seconds=5)
async def fetch_feed_task(feed_url: str) -> list[dict]:
    return await fetch_feed(feed_url)


@task(name="load-youtube-rss-document", retries=1, retry_delay_seconds=2)
async def load_video_task(doc: Document) -> Document | None:
    return await load_video_document(doc)


@flow(name="ingest-youtube-rss-feed-etl", log_prints=True, validate_parameters=False)
async def ingest_youtube_rss_feed(
    feed_url: str,
    user_id: PydanticObjectId,
    fetcher: TranscriptFetcher | None = None,
) -> list[Document]:
    """Ingest every video referenced by a YouTube channel RSS feed.

    Steps:
    1. ``fetch_feed_task(feed_url)`` → Atom entries.
    2. For each entry, derive ``(canonical_video_url, VideoMetadata)`` from
       feed-side fields. Entries with no resolvable video id are skipped with
       a pipeline-layer WARNING.
    3. ``fetcher.fetch_many([canonical_url, ...])`` — ONE bulk call.
    4. For each ``(video_url, metadata, transcript)``: if the transcript is
       ``None`` (chain exhausted), continue silently — the chain wrapper has
       already warned. Otherwise build the `Document` and persist it.
    5. Return the list of newly-persisted (or upgraded-from-LATENT) Documents.
    """

    fetcher = fetcher or _default_chained_fetcher()

    entries = await fetch_feed_task(feed_url)

    resolved: list[tuple[str, dict]] = []
    for entry in entries:
        video_url = extract_video_url(entry)
        if video_url is None:
            logger.warning("Skipping entry with no resolvable video id")
            continue
        resolved.append((video_url, entry))

    if not resolved:
        logger.info("Ingested 0 new videos from %s", feed_url)
        return []

    video_urls = [url for url, _ in resolved]
    transcripts = await fetcher.fetch_many(video_urls)

    ingested: list[Document] = []
    for (video_url, entry), transcript in zip(resolved, transcripts):
        if transcript is None:
            # Chain wrapper has already emitted the user-facing WARNING.
            continue

        # `extract_video_url` already validated the id; this is just the
        # bare 11-char form for `build_document`.
        video_id = extract_video_id(video_url)
        if video_id is None:  # pragma: no cover — defensive
            continue

        metadata = feed_entry_to_metadata(entry)
        doc = build_document(
            video_id=video_id,
            metadata=metadata,
            transcript=transcript,
            user_id=user_id,
        )
        result = await load_video_task(doc)
        if result is not None:
            ingested.append(result)

    logger.info("Ingested %d new videos from %s", len(ingested), feed_url)
    return ingested


@flow(
    name="ingest-youtube-rss-feed-batch-etl",
    log_prints=True,
    validate_parameters=False,
)
async def ingest_youtube_rss_feed_batch(
    feed_urls: list[str],
    user_id: PydanticObjectId,
    fetcher: TranscriptFetcher | None = None,
) -> list[Document]:
    """Batch-ingest a list of YouTube channel feeds.

    Mirrors `ingest_substack_rss_feed_batch`: bring up the Mongo connection
    once at the flow boundary, then `asyncio.gather` over per-feed ingests.
    """

    await init_mongodb(
        settings.mongo.mongo_uri.get_secret_value(),
        settings.mongo.mongo_initdb_database,
    )

    fetcher = fetcher or _default_chained_fetcher()

    results = await asyncio.gather(
        *[
            ingest_youtube_rss_feed(feed_url, user_id, fetcher=fetcher)
            for feed_url in feed_urls
        ]
    )

    return [doc for docs in results for doc in docs]
