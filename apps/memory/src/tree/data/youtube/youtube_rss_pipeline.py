"""YouTube RSS-feed leaf pipeline — shared bulk core + feed metadata (#080).

The RSS path derives ``VideoMetadata`` from the Atom feed entry itself
(``feed_entry_to_metadata`` — the feed already carries title / channel / publish
date, so there is NO per-video oEmbed round-trip), then runs the SHARED
bulk-transcript core (``tree.data.youtube.youtube_ingest._bulk_build_and_load``):
ONE bulk ``fetch_many(all_urls)`` per feed → ``build_document`` per slot →
``load_video_document`` per slot. The bulk fetch + build + load are identical to the
direct-video path — only the metadata SOURCE differs (feed here, oEmbed there).

Per feed the batch flow runs ``fetch_feed_task`` (Extract, ``retries=2``) then routes
the resolved ``(canonical_url, feed metadata)`` items through the shared core's
``fetch_transcripts_batch`` → ``build_batch`` → ``load_batch`` ETL-phase tasks. There
are no per-row tasks and no per-feed sub-flow runs.

Two skip behaviours are preserved exactly:

- An Atom entry with no resolvable video id is skipped with a pipeline-layer WARNING
  (a feed-parsing problem the transcript fetch never sees).
- A missing transcript (the bulk fetch returns ``None`` for that slot) is skipped inside
  the shared core's ``fetch_transcripts_batch`` with a per-slot WARNING.

Transcripts are fetched via Gemini inside the shared core's ``fetch_transcripts_batch``
task, which constructs the ``GeminiTranscriptFetcher`` itself — no fetcher is threaded
through this flow, so task inputs stay fully serializable.
"""

from __future__ import annotations

import asyncio
import logging

from beanie import PydanticObjectId
from prefect import flow, task

from tree.config.settings import settings
from tree.data.youtube.types import VideoMetadata
from tree.data.youtube.youtube_ingest import _bulk_build_and_load
from tree.data.youtube.youtube_rss import (
    extract_video_url,
    feed_entry_to_metadata,
    fetch_feed,
)
from tree.db import init_mongodb
from tree.entities.documents import Document

logger = logging.getLogger(__name__)


@task(name="fetch-youtube-rss-feed", retries=2, retry_delay_seconds=5)
async def fetch_feed_task(feed_url: str) -> list[dict]:
    return await fetch_feed(feed_url)


def _resolve_feed_items(
    entries: list[dict],
) -> list[tuple[str, VideoMetadata]]:
    """Resolve Atom entries to ``(canonical_url, feed VideoMetadata)`` items.

    Skips entries with no resolvable video id, emitting the pipeline-layer WARNING
    (preserved behaviour). Metadata comes from ``feed_entry_to_metadata`` (the feed),
    NOT oEmbed — the RSS metadata source.
    """

    items: list[tuple[str, VideoMetadata]] = []
    for entry in entries:
        video_url = extract_video_url(entry)
        if video_url is None:
            logger.warning("Skipping entry with no resolvable video id")
            continue
        items.append((video_url, feed_entry_to_metadata(entry)))
    return items


async def _ingest_one_feed(
    feed_url: str,
    user_id: PydanticObjectId,
) -> list[Document]:
    """Ingest a single feed through the shared bulk core (plain async, NO sub-flow).

    ``fetch_feed_task`` (Extract) → resolve ``(canonical_url, feed metadata)`` items →
    the SHARED ``_bulk_build_and_load`` (ONE bulk ``fetch_many`` for the feed + build +
    load). Folded into the batch loop — there is no per-feed sub-flow run.
    """

    entries = await fetch_feed_task(feed_url)
    items = _resolve_feed_items(entries)
    if not items:
        logger.info("Ingested 0 new videos from %s", feed_url)
        return []

    ingested = await _bulk_build_and_load(items, user_id)
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
) -> list[Document]:
    """Batch-ingest a list of YouTube channel feeds via the shared bulk core.

    Brings up the Mongo connection once, then runs the per-feed body (one feed fetch +
    ONE bulk ``fetch_many`` + build + load) for every feed under
    ``asyncio.gather(return_exceptions=True)`` so one bad feed is logged + skipped and
    never sinks the others. No per-feed sub-flow runs — the per-feed body is inlined.
    """

    await init_mongodb(
        settings.mongo.mongo_uri.get_secret_value(),
        settings.mongo.mongo_initdb_database,
    )

    results = await asyncio.gather(
        *[_ingest_one_feed(feed_url, user_id) for feed_url in feed_urls],
        return_exceptions=True,
    )

    all_ingested: list[Document] = []
    for feed_url, result in zip(feed_urls, results, strict=True):
        if isinstance(result, BaseException):
            logger.warning(
                "Failed to ingest feed %s; skipping", feed_url, exc_info=result
            )
            continue
        all_ingested.extend(result)

    return all_ingested
