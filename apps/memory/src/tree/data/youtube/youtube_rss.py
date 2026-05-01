"""Pure logic for the YouTube channel RSS-feed ETL.

Mirrors `tree.data.substack.substack_rss` in shape, specialised for YouTube:

- `fetch_feed` — `httpx.AsyncClient.get(...)` + `feedparser.parse(...)`, returns
  the list of Atom entries. Same error semantics as the Substack helper:
  malformed feeds with no entries raise `ValueError`; otherwise the entries
  list is returned (even when `bozo` is set, as long as entries were
  recovered).
- `extract_video_url` — pure: derive the canonical
  ``https://www.youtube.com/watch?v={id}`` URL from an Atom entry. Prefers
  ``entry['yt_videoid']`` (feedparser maps ``yt:videoId`` to that attribute);
  falls back to parsing ``entry['link']`` via `extract_video_id`. Returns
  ``None`` when neither resolves — the pipeline layer then logs a WARNING
  and continues to the next entry.
- `feed_entry_to_metadata` — pure: map an Atom entry to a partial
  `VideoMetadata` so the RSS pipeline can skip the per-video oEmbed call
  shipped in #003 (the feed already carries title / channel / publish date).

YouTube channel feeds are Atom (not RSS 2.0); ``feedparser`` handles both,
which is why we reuse it instead of pulling in a second XML parser.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

import feedparser
import httpx

from tree.data.youtube.types import VideoMetadata
from tree.data.youtube.urls import canonical_video_url, extract_video_id

logger = logging.getLogger(__name__)

_FEED_TIMEOUT_SECONDS = 30.0


async def fetch_feed(feed_url: str) -> list[dict]:
    """Fetch and parse a YouTube channel Atom feed, returning raw entries.

    Mirrors `tree.data.substack.substack_rss.fetch_feed`: same error contract
    (HTTP errors propagate, malformed-with-no-entries raises ``ValueError``,
    bozo-with-entries is tolerated).
    """

    logger.info("Fetching RSS feed: %s", feed_url)

    async with httpx.AsyncClient() as client:
        response = await client.get(
            feed_url, follow_redirects=True, timeout=_FEED_TIMEOUT_SECONDS
        )
        response.raise_for_status()

    feed = feedparser.parse(response.text)
    if feed.bozo and not feed.entries:
        raise ValueError(
            f"Failed to parse RSS feed from {feed_url}: {feed.bozo_exception}"
        )

    return list(feed.entries)


def extract_video_url(entry: dict) -> str | None:
    """Get the canonical video URL from a YouTube Atom entry.

    Resolution order:
    1. ``entry['yt_videoid']`` — feedparser maps ``yt:videoId`` to this attr.
    2. ``entry['link']`` — fall back to URL parsing via `extract_video_id`.

    Returns the canonical ``https://www.youtube.com/watch?v={id}`` URL, or
    ``None`` when neither field yields a valid 11-character video ID.
    """

    if not entry:
        return None

    raw_id = entry.get("yt_videoid")
    if raw_id:
        video_id = extract_video_id(raw_id)
        if video_id is not None:
            return canonical_video_url(video_id)

    link = entry.get("link")
    if link:
        video_id = extract_video_id(link)
        if video_id is not None:
            return canonical_video_url(video_id)

    return None


def _parse_published(entry: dict) -> datetime | None:
    """Parse ``entry['published']`` to a tz-aware UTC datetime, or ``None``.

    YouTube Atom feeds publish ISO-8601 timestamps (``2024-01-15T12:00:00+00:00``).
    `parsedate_to_datetime` from `email.utils` only handles RFC-2822 — we try
    `datetime.fromisoformat` first and fall back for safety. Returns ``None``
    when the field is missing or unparseable; `build_document` already has a
    ``datetime.now(tz=UTC)`` fallback for that case.
    """

    published = entry.get("published")
    if not published:
        return None

    try:
        dt = datetime.fromisoformat(published)
    except ValueError, TypeError:
        try:
            dt = parsedate_to_datetime(published)
        except ValueError, TypeError:
            return None

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt


def feed_entry_to_metadata(entry: dict) -> VideoMetadata:
    """Map a YouTube Atom entry to a partial `VideoMetadata`.

    Populates everything the feed surfaces so the RSS pipeline can avoid the
    per-video oEmbed round-trip used by the single-video pipeline (#003):

    - ``title``         ← ``entry['title']``
    - ``channel``       ← ``entry['author']``
    - ``channel_id``    ← ``entry.get('yt_channelid')`` (optional)
    - ``publish_date``  ← ``entry['published']`` parsed as tz-aware UTC
    - ``duration_seconds`` ← ``None`` (not in the feed)

    Requires ``yt_videoid`` (or a parseable ``link``) — caller filters out
    entries with no resolvable video id BEFORE invoking this function.
    """

    raw_id = entry.get("yt_videoid")
    video_id: str | None
    if raw_id:
        video_id = extract_video_id(raw_id)
    else:
        video_id = extract_video_id(entry.get("link", ""))
    if video_id is None:
        raise ValueError("Atom entry has no resolvable video id")

    title = entry.get("title") or None
    channel = entry.get("author") or None
    channel_id = entry.get("yt_channelid") or None
    publish_date = _parse_published(entry)

    return VideoMetadata(
        video_id=video_id,
        title=title,
        channel=channel,
        channel_id=channel_id,
        publish_date=publish_date,
    )
