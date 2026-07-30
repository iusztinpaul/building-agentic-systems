"""Pure YouTube helpers — URL/ID canonicalisation, feed + single-video acquisition, load.

Mirrors ``tree.data.substack.substack`` in shape. Two acquisition paths, one load:

- **URL/ID helpers** (`extract_video_id`, `canonical_video_url`,
  `extract_channel_id_from_rss_url`) — pure functions so every call site
  (pipelines, dispatcher routing, transcript fetchers) accepts user input in any
  common YouTube URL shape and converges on the canonical
  ``https://www.youtube.com/watch?v={id}`` form.
- **RSS path** (`fetch_feed`, `extract_video_url`, `feed_entry_to_metadata`) —
  channel Atom feeds via ``feedparser`` (same error contract as the Substack
  helper); metadata comes from the feed, so the batch pipeline skips per-video
  oEmbed calls.
- **Single-video path** (`fetch_oembed_metadata`, `parse_oembed_metadata`) —
  best-effort HTTP enrichment via the public oEmbed endpoint. Returns ``{}``
  (not an exception) for the "video disables oEmbed" / "404" case so the
  pipeline can still land a Document built from the transcript alone.
- **Shared build + load** (`build_document`, `build_failure_document`,
  `load_video_document`) — the transcript IS the document content; transcripts
  have no ``<a href>`` anchors, so there is no reference extraction. The load
  inlines the dedup-then-insert pattern from
  ``tree.data.substack.substack.load_document`` (the canonical version) —
  intentionally NOT imported, because the transcript path has no references to
  resolve.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import parse_qs, urlparse

import feedparser
import httpx
from beanie import PydanticObjectId
from pymongo.errors import DuplicateKeyError

from tree.data.youtube.types import FetchedTranscript, VideoMetadata
from tree.entities.documents import Document, SourceType

logger = logging.getLogger(__name__)

# YouTube video IDs are exactly 11 chars, drawn from [A-Za-z0-9_-].
_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")

_VIDEO_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "youtu.be",
}

_RSS_HOSTS = {
    "youtube.com",
    "www.youtube.com",
}

_FEED_TIMEOUT_SECONDS = 30.0

_OEMBED_ENDPOINT = "https://www.youtube.com/oembed"
_OEMBED_TIMEOUT_SECONDS = 30.0
_SUMMARY_MAX_CHARS = 280

# The normalized ``ingest_error`` for an input we could not resolve to a video
# id at all (ADR-004 §6). Such a row is keyed on the RAW input string, since no
# canonical URL exists for it.
INVALID_URL_ERROR = "invalid_url: no video id in input"


# --- URL/ID helpers ---------------------------------------------------------


def _normalize_host(host: str | None) -> str:
    return (host or "").lower()


def extract_video_id(url_or_id: str) -> str | None:
    """Resolve any common YouTube URL shape (or a bare 11-char ID) to a video ID.

    Returns the 11-character ID, or `None` if the input cannot be resolved.
    """

    if not url_or_id:
        return None

    # Bare ID: accept as-is.
    if _VIDEO_ID_RE.match(url_or_id):
        return url_or_id

    parsed = urlparse(url_or_id)
    host = _normalize_host(parsed.hostname)
    if host not in _VIDEO_HOSTS:
        return None

    # https://youtu.be/<id>
    if host == "youtu.be":
        candidate = parsed.path.lstrip("/").split("/", 1)[0]
        return candidate if _VIDEO_ID_RE.match(candidate) else None

    # https://(www|m).youtube.com/watch?v=<id>
    if parsed.path == "/watch":
        values = parse_qs(parsed.query).get("v", [])
        if values and _VIDEO_ID_RE.match(values[0]):
            return values[0]
        return None

    # https://(www|m).youtube.com/shorts/<id>
    if parsed.path.startswith("/shorts/"):
        candidate = parsed.path[len("/shorts/") :].split("/", 1)[0]
        return candidate if _VIDEO_ID_RE.match(candidate) else None

    # https://(www|m).youtube.com/embed/<id>
    if parsed.path.startswith("/embed/"):
        candidate = parsed.path[len("/embed/") :].split("/", 1)[0]
        return candidate if _VIDEO_ID_RE.match(candidate) else None

    return None


def extract_channel_id_from_rss_url(url: str) -> str | None:
    """Return the `channel_id` query value from a YouTube channel RSS URL.

    Returns `None` if the URL is not a YouTube channel RSS feed.
    """

    if not url:
        return None

    parsed = urlparse(url)
    if _normalize_host(parsed.hostname) not in _RSS_HOSTS:
        return None
    if parsed.path != "/feeds/videos.xml":
        return None

    values = parse_qs(parsed.query).get("channel_id", [])
    if not values or not values[0]:
        return None
    return values[0]


def canonical_video_url(video_id: str) -> str:
    """Return the canonical `Document.source_uri` form for a video ID.

    The canonical form is the same regardless of which URL shape the user
    pasted, so that `Document.source_uri` upserts deduplicate correctly.
    """

    return f"https://www.youtube.com/watch?v={video_id}"


# --- RSS-feed acquisition ---------------------------------------------------


async def fetch_feed(feed_url: str) -> list[dict]:
    """Fetch and parse a YouTube channel Atom feed, returning raw entries.

    Mirrors `tree.data.substack.substack.fetch_feed`: same error contract
    (HTTP errors propagate, malformed-with-no-entries raises ``ValueError``,
    bozo-with-entries is tolerated). YouTube channel feeds are Atom (not RSS
    2.0); ``feedparser`` handles both, which is why we reuse it instead of
    pulling in a second XML parser.
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


# --- Single-video acquisition -----------------------------------------------


async def fetch_oembed_metadata(video_url: str) -> dict[str, Any]:
    """Fetch best-effort oEmbed metadata for a YouTube video URL.

    Returns the parsed JSON payload, or an empty ``dict`` when the video
    disables oEmbed / the endpoint returns a 404. Never raises for
    "metadata-not-available"; only re-raises on non-404 transport errors so
    the pipeline can retry under Prefect's `@task(retries=...)`.
    """

    logger.info("Fetching oEmbed metadata: %s", video_url)

    params = {"url": video_url, "format": "json"}
    async with httpx.AsyncClient() as client:
        response = await client.get(
            _OEMBED_ENDPOINT,
            params=params,
            follow_redirects=True,
            timeout=_OEMBED_TIMEOUT_SECONDS,
        )

    if response.status_code == 404:
        logger.debug(
            "oEmbed returned 404 for %s — proceeding with no metadata", video_url
        )
        return {}

    response.raise_for_status()
    return response.json()


def parse_oembed_metadata(payload: dict[str, Any], *, video_id: str) -> VideoMetadata:
    """Map an oEmbed JSON payload to a partial `VideoMetadata`.

    oEmbed surfaces ``title``, ``author_name`` (channel), and ``author_url``
    (channel URL). It does NOT include publish date or duration — those
    fields stay ``None`` in v1.
    """

    title = payload.get("title") or None
    channel = payload.get("author_name") or None

    return VideoMetadata(
        video_id=video_id,
        title=title,
        channel=channel,
    )


# --- Shared build + load ----------------------------------------------------


def build_document(
    *,
    video_id: str,
    metadata: VideoMetadata,
    transcript: FetchedTranscript,
    user_id: PydanticObjectId,
) -> Document:
    """Assemble a `Document` from oEmbed metadata + a fetched transcript.

    - ``source_type`` is always `SourceType.YOUTUBE`.
    - ``source_uri`` is the canonical ``watch?v=…`` URL (so dedup is stable
      across pasted shapes like ``youtu.be/…`` or ``/shorts/…``).
    - ``content`` is the transcript's ``plain_text`` — the transcript IS the
      document body.
    - ``title`` falls back to ``"YouTube video {video_id}"`` when oEmbed gave
      us nothing.
    - ``summary`` is the title when present; otherwise a leading prefix of
      the transcript (≤ 280 chars).
    - ``date`` is the publish date when known, otherwise "now" in UTC. Always
      tz-aware (the project rule).
    - ``authors`` is ``[channel]`` when known, otherwise empty.
    """

    title = metadata.title or f"YouTube video {video_id}"
    summary = metadata.title or transcript.plain_text[:_SUMMARY_MAX_CHARS]
    authors = [metadata.channel] if metadata.channel else []
    date = metadata.publish_date or datetime.now(tz=timezone.utc)

    return Document(
        source_type=SourceType.YOUTUBE,
        source_uri=canonical_video_url(video_id),
        user_id=user_id,
        title=title,
        summary=summary,
        content=transcript.plain_text,
        authors=authors,
        date=date,
    )


def build_failure_document(
    *,
    source_uri: str,
    metadata: VideoMetadata | None,
    ingest_error: str,
    user_id: PydanticObjectId,
) -> Document:
    """Assemble a persisted ingest-FAILURE row for a video we could not ingest.

    Failures are data, not scrolled-away WARNINGs (ADR-004 §6): the row keeps
    whatever base metadata exists (title, channel, publish date) and carries

    - ``content=None`` — which the extraction pipelines already exclude via
      ``{"content": {"$ne": None}}``, so no downstream change is needed, and
    - a NORMALIZED ``ingest_error`` (``"<code>: <message>"``, never a raw
      exception dump) naming what actually happened.

    ``source_uri`` is the canonical ``watch?v=…`` URL when the video resolved
    (so a later successful run replaces this row), or the RAW input string when
    it did not — ``metadata`` is ``None`` in exactly that case.

    The row goes through the normal `load_video_document` path, so #089's
    replace-on-retry semantics apply: the next run re-attempts it.
    """

    return Document(
        source_type=SourceType.YOUTUBE,
        source_uri=source_uri,
        user_id=user_id,
        title=metadata.title if metadata else None,
        content=None,
        authors=[metadata.channel] if metadata and metadata.channel else [],
        date=metadata.publish_date if metadata else None,
        ingest_error=ingest_error,
    )


async def load_video_document(doc: Document) -> Document | None:
    """Dedup and persist a single YouTube `Document`.

    Mirrors the dedup-then-insert/replace path from
    `tree.data.substack.substack.load_document` — that is the canonical
    version. Intentionally **not** imported, because:

    - Transcripts contain no anchor tags → no references to resolve, so the
      reference-extraction loop would be a no-op.
    - Substack's ``load_document`` requires a synthetic ``raw_entry`` dict;
      passing a fake one would obscure that we deliberately skip references.

    Returns the persisted Document, or ``None`` when an existing successfully
    ingested non-LATENT document already lives at the same canonical URL.

    Two kinds of existing row are REPLACEABLE rather than a skip:

    - a `SourceType.LATENT` placeholder (a reference discovered before its
      own ingestion), and
    - a row carrying an `ingest_error` — a persisted ingest failure. Every
      later run re-attempts it (no attempt cap, per ADR-004 §6) and logs a
      WARNING naming the prior error, so a permanently failing video is
      visible instead of silently retried forever.
    """

    existing = await Document.find_one(
        {"user_id": doc.user_id, "source_uri": doc.source_uri}
    )
    if (
        existing
        and existing.source_type != SourceType.LATENT
        and existing.ingest_error is None
    ):
        logger.debug("Skipping duplicate: %s", doc.source_uri)
        return None

    if existing:
        if existing.ingest_error is not None:
            logger.warning(
                "Re-attempting previously failed ingest: %s (prior error: %s)",
                doc.source_uri,
                existing.ingest_error,
            )
            outcome = "Replaced errored document"
        else:
            outcome = "Upgraded latent document"
        doc.id = existing.id
        await doc.replace()
        logger.info("%s: %s", outcome, doc.source_uri)
    else:
        try:
            await doc.insert()
        except DuplicateKeyError:
            # Concurrent insert of the same (user_id, source_type, source_uri) — e.g.
            # the same video resolved from both a feed and a single source in one
            # flattened batch. The unique index lets one win; this attempt is a
            # clean skip, not a failure.
            logger.debug("Skipping concurrent duplicate: %s", doc.source_uri)
            return None
        if doc.ingest_error is not None:
            # A failure row is NOT an ingest; saying "Ingested" here would read as
            # a success in the logs for a document with no content at all.
            logger.info(
                "Recorded ingest failure: %s (%s)", doc.source_uri, doc.ingest_error
            )
        else:
            logger.info("Ingested: %s", doc.source_uri)

    return doc
