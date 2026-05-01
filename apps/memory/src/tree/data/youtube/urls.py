"""URL/ID helpers for YouTube videos and channel RSS feeds.

Pure functions only — no I/O, no logging side-effects. These helpers exist so
that every YouTube call site (single-video pipeline, RSS pipeline, dispatcher
routing, the transcript fetcher itself) accepts user input in any of the
common YouTube URL shapes and converges on the canonical
`https://www.youtube.com/watch?v={id}` form.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

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


def is_youtube_video_url(url: str) -> bool:
    """True for any URL shape that resolves to a single YouTube video."""

    if not url:
        return False
    parsed = urlparse(url)
    host = _normalize_host(parsed.hostname)
    if host not in _VIDEO_HOSTS:
        return False
    if host == "youtu.be":
        return bool(parsed.path.lstrip("/"))
    return (
        parsed.path == "/watch"
        or parsed.path.startswith("/shorts/")
        or parsed.path.startswith("/embed/")
    )


def is_youtube_rss_url(url: str) -> bool:
    """True for `https://(www.)youtube.com/feeds/videos.xml?channel_id=...`."""

    return extract_channel_id_from_rss_url(url) is not None


def canonical_video_url(video_id: str) -> str:
    """Return the canonical `Document.source_uri` form for a video ID.

    The canonical form is the same regardless of which URL shape the user
    pasted, so that `Document.source_uri` upserts deduplicate correctly.
    """

    return f"https://www.youtube.com/watch?v={video_id}"
