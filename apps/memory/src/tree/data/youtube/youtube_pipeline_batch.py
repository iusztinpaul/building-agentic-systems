"""YouTube unified pipeline — ONE batch flow over single videos + channel RSS feeds.

Flattens a YouTube shard (mixed ``YouTubeRssSource`` + ``YouTubeVideoSource``) into a
single ``[(canonical_url, VideoMetadata)]`` list, then runs the SHARED bulk core
(``youtube_pipeline._batch_build_and_load``) ONCE: one ``fetch_many`` transcript fetch
over ALL items (feeds + loose videos), build, load. The only per-kind difference is
the resolve step — RSS metadata comes from the feed, single-video metadata from
oEmbed; once flattened to ``(url, VideoMetadata)`` the two are indistinguishable.

Failure isolation is preserved at the flatten boundary: a feed that fails to fetch is
logged + skipped (its items absent), an oEmbed-failing video is dropped, and the
shared core keeps its per-slot transcript resilience. A loose video whose id cannot
be resolved at all is NOT dropped: it reaches the core as a raw ``invalid_url``
ingest_error row (ADR-004 §6). Feed entries with no resolvable id stay WARNING-only —
there is no stable key to persist them under.
"""

from __future__ import annotations

import logging

from beanie import PydanticObjectId
from prefect import flow, task

from tree.config.sources import (
    YouTubeRssSource,
    YouTubeVideoSource,
)
from tree.config.settings import settings
from tree.data.batch import gather_isolated
from tree.data.youtube.types import VideoMetadata
from tree.data.youtube.youtube import (
    extract_video_url,
    feed_entry_to_metadata,
    fetch_feed,
)
from tree.data.youtube.youtube_pipeline import (
    _batch_build_and_load,
    _partition_video_inputs,
    _resolve_video_item,
)
from tree.db import init_mongodb
from tree.entities.documents import Document

logger = logging.getLogger(__name__)

# A flattened, resolved item ready for the shared bulk core: canonical URL + metadata
# (from the feed for RSS, from oEmbed for a single video).
_ResolvedItem = tuple[str, VideoMetadata]


@task(name="fetch-youtube-rss-feed", retries=3, retry_delay_seconds=5)
async def fetch_rss_feed(feed_url: str) -> list[dict]:
    return await fetch_feed(feed_url)


def _resolve_feed_items(entries: list[dict]) -> list[_ResolvedItem]:
    """Resolve Atom entries to ``(canonical_url, feed VideoMetadata)`` items.

    Skips entries with no resolvable video id (pipeline-layer WARNING). Metadata comes
    from ``feed_entry_to_metadata`` (the feed), NOT oEmbed — the RSS metadata source.
    """

    items: list[_ResolvedItem] = []
    for entry in entries:
        video_url = extract_video_url(entry)
        if video_url is None:
            logger.warning("Skipping entry with no resolvable video id")
            continue
        items.append((video_url, feed_entry_to_metadata(entry)))
    return items


async def _resolve_feed(feed_url: str) -> list[_ResolvedItem]:
    """Fetch + resolve one feed to items — the unit isolated per feed."""

    return _resolve_feed_items(await fetch_rss_feed(feed_url))


@flow(name="ingest-youtube-batch-etl", log_prints=True, validate_parameters=False)
async def ingest_youtube_batch(
    entries: list[YouTubeRssSource | YouTubeVideoSource],
    user_id: PydanticObjectId,
) -> list[Document]:
    """Batch-ingest a YouTube shard (RSS feeds + single videos) via ONE bulk core.

    Flattens both source kinds into one ``[(canonical_url, VideoMetadata)]`` list —
    RSS feeds expand to per-video items (metadata from the feed, isolated per feed),
    single videos resolve via oEmbed (isolated per URL) — then runs the SHARED
    ``_batch_build_and_load`` ONCE, so there is exactly ONE ``fetch_many`` transcript
    fetch over feeds + loose videos combined.
    """

    await init_mongodb(
        settings.mongo.mongo_uri.get_secret_value(),
        settings.mongo.mongo_initdb_database,
    )

    feed_urls = [e.uri for e in entries if isinstance(e, YouTubeRssSource)]
    video_urls = [e.uri for e in entries if isinstance(e, YouTubeVideoSource)]

    items: list[_ResolvedItem] = []
    invalid_inputs: list[str] = []

    # RSS feeds → expand to per-video items, isolated per feed.
    if feed_urls:
        per_feed, failures = await gather_isolated(feed_urls, _resolve_feed)
        if failures:
            logger.warning(
                "Feed resolve failed for %d/%d feeds", failures, len(feed_urls)
            )
        items.extend(item for feed_items in per_feed for item in feed_items)

    # Single videos → oEmbed-resolve, isolated per URL. An input with no
    # resolvable video id never reaches oEmbed: it goes to the core as a RAW
    # ``invalid_url`` failure row (ADR-004 §6).
    if video_urls:
        resolvable, invalid_inputs = _partition_video_inputs(video_urls)
        resolved, failures = await gather_isolated(resolvable, _resolve_video_item)
        if failures:
            logger.warning(
                "oEmbed resolution failed for %d/%d URLs", failures, len(resolvable)
            )
        items.extend(resolved)

    if not items and not invalid_inputs:
        logger.info("YouTube: no resolvable items in shard")
        return []

    ingested = await _batch_build_and_load(
        items, user_id, invalid_inputs=invalid_inputs
    )
    logger.info(
        "YouTube: ingested %d items (%d feeds, %d single videos)",
        len(ingested),
        len(feed_urls),
        len(video_urls),
    )
    return ingested
