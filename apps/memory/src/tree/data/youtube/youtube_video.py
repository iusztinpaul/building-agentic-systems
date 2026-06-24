"""Pure logic for the single-video YouTube ETL.

Mirrors `tree.data.substack.substack_article` in shape:

- `fetch_oembed_metadata` — best-effort HTTP enrichment via the public oEmbed
  endpoint. Returns `{}` (not an exception) for the "video disables oEmbed" /
  "404" case so that the pipeline can still land a Document built from the
  transcript alone.
- `parse_oembed_metadata` — pure dict → `VideoMetadata` mapping.
- `build_document` — assembles a `Document` from a `VideoMetadata` and a
  `FetchedTranscript`. The transcript IS the document content; transcripts
  have no `<a href>` anchors, so there is no reference extraction.
- `load_video_document` — minimal dedup + insert/replace. Inlines the
  ten-line dedup-then-insert pattern from
  `tree.data.substack.substack_rss.load_document` (the canonical version) —
  intentionally NOT imported, because the transcript path has no references
  to resolve.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import httpx
from beanie import PydanticObjectId
from pymongo.errors import DuplicateKeyError

from tree.data.youtube.types import FetchedTranscript, VideoMetadata
from tree.data.youtube.urls import canonical_video_url
from tree.entities.documents import Document, SourceType

logger = logging.getLogger(__name__)

_OEMBED_ENDPOINT = "https://www.youtube.com/oembed"
_OEMBED_TIMEOUT_SECONDS = 30.0
_SUMMARY_MAX_CHARS = 280


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


async def load_video_document(doc: Document) -> Document | None:
    """Dedup and persist a single YouTube `Document`.

    Mirrors the dedup-then-insert/replace path from
    `tree.data.substack.substack_rss.load_document` — that is the canonical
    version. Intentionally **not** imported, because:

    - Transcripts contain no anchor tags → no references to resolve, so the
      reference-extraction loop would be a no-op.
    - Substack's ``load_document`` requires a synthetic ``raw_entry`` dict;
      passing a fake one would obscure that we deliberately skip references.

    Returns the persisted Document, or ``None`` when an existing non-LATENT
    document already lives at the same canonical URL.
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
        except DuplicateKeyError:
            # Concurrent insert of the same (user_id, source_type, source_uri) — e.g.
            # the same video resolved from both a feed and a single source in one
            # flattened batch. The unique index lets one win; this attempt is a
            # clean skip, not a failure.
            logger.debug("Skipping concurrent duplicate: %s", doc.source_uri)
            return None
        logger.info("Ingested: %s", doc.source_uri)

    return doc
