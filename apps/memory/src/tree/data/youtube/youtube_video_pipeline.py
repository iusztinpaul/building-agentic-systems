"""Single-video YouTube leaf pipeline — shared bulk core + thin MCP flow (#080).

The direct-video path enriches per video via oEmbed, then runs the SHARED
bulk-transcript core (``tree.data.youtube.youtube_ingest._bulk_build_and_load``):
ONE ``fetcher.fetch_many(all_urls)`` for the whole batch → ``build_document`` per slot
→ ``load_video_document`` per slot. This is the #080 fix — the batch previously did a
PER-VIDEO ``fetch_many([url])`` inside per-URL sub-flows.

The per-item sub-flow's body is demoted to the plain async core
``_ingest_youtube_video_one``; ``ingest_youtube_video`` remains a THIN @flow wrapper
used ONLY by the MCP URL router (``tree.data.ingest._ingest_youtube_video``) so a
single-URL ingest still gets its own Prefect flow run + Opik trace. The BATCH path
calls the shared core directly — NEVER the thin wrapper (no per-item sub-flow runs).

The ``fetcher`` argument is the swap point: production omits it and gets the default
chained ``[YoutubeTranscriptApiFetcher, GeminiTranscriptFetcher]`` (free primary + paid
fallback); tests inject a fake `TranscriptFetcher` to avoid network and Gemini auth.
"""

from __future__ import annotations

import logging

from beanie import PydanticObjectId
from prefect import flow

from tree.config.settings import settings
from tree.data.youtube.transcript_fetcher import (
    ChainedTranscriptFetcher,
    GeminiTranscriptFetcher,
    TranscriptFetcher,
    YoutubeTranscriptApiFetcher,
)
from tree.data.youtube.urls import canonical_video_url, extract_video_id
from tree.data.youtube.youtube_ingest import _bulk_build_and_load
from tree.data.youtube.youtube_video import (
    fetch_oembed_metadata,
    parse_oembed_metadata,
)
from tree.data.youtube.types import VideoMetadata
from tree.db import init_mongodb
from tree.entities.documents import Document

logger = logging.getLogger(__name__)


def _default_chained_fetcher() -> TranscriptFetcher:
    """Build the default chain: free primary + paid Gemini fallback.

    Lazy module-level helper so tests can inject a fake fetcher without
    triggering the `GeminiTranscriptFetcher.__init__` guard (which requires
    ``GOOGLE_API_KEY``).
    """

    return ChainedTranscriptFetcher(
        fetchers=[
            YoutubeTranscriptApiFetcher(),
            GeminiTranscriptFetcher(),
        ]
    )


async def _resolve_video_item(
    video_url: str,
) -> tuple[str, VideoMetadata] | None:
    """Resolve a pasted URL to ``(canonical_url, oEmbed VideoMetadata)`` or ``None``.

    Returns ``None`` (with a WARNING) when the input cannot be resolved to a video id.
    Otherwise canonicalises the URL and enriches metadata via the per-video oEmbed
    round-trip (``fetch_oembed_metadata`` + ``parse_oembed_metadata``) — the
    direct-video metadata source, kept distinct from the RSS feed-metadata path.
    """

    video_id = extract_video_id(video_url)
    if video_id is None:
        logger.warning("Could not resolve video id from input: %s", video_url)
        return None

    canonical_url = canonical_video_url(video_id)
    payload = await fetch_oembed_metadata(canonical_url)
    metadata = parse_oembed_metadata(payload, video_id=video_id)
    return canonical_url, metadata


async def _ingest_youtube_video_one(
    video_url: str,
    user_id: PydanticObjectId,
    fetcher: TranscriptFetcher,
) -> Document | None:
    """Ingest a SINGLE video via the shared bulk core (plain async core, NO decorators).

    Resolves the id → canonical URL → per-video oEmbed metadata, then runs the SHARED
    ``_bulk_build_and_load`` over the one-item list (the single-item bulk fetch + build
    + load). Shared by the thin MCP flow; the batch path calls the shared core directly
    with the whole URL list instead. Returns the persisted Document, or ``None`` for an
    unresolvable id / missing transcript / duplicate.
    """

    resolved = await _resolve_video_item(video_url)
    if resolved is None:
        return None

    ingested = await _bulk_build_and_load([resolved], user_id, fetcher)
    return ingested[0] if ingested else None


@flow(name="ingest-youtube-video-etl", log_prints=True, validate_parameters=False)
async def ingest_youtube_video(
    video_url: str,
    user_id: PydanticObjectId,
    fetcher: TranscriptFetcher | None = None,
) -> Document | None:
    """Thin MCP-only @flow: ingest ONE YouTube video via the core.

    The MCP ``ingest_url`` router (``tree.data.ingest._ingest_youtube_video``) calls
    this so single-URL ingest still gets its own Prefect flow run + Opik trace. The
    BATCH path does NOT call this — it runs the shared bulk core directly.
    """

    fetcher = fetcher or _default_chained_fetcher()
    return await _ingest_youtube_video_one(video_url, user_id, fetcher)


@flow(
    name="ingest-youtube-video-batch-etl",
    log_prints=True,
    validate_parameters=False,
)
async def ingest_youtube_video_batch(
    video_urls: list[str],
    user_id: PydanticObjectId,
    fetcher: TranscriptFetcher | None = None,
) -> list[Document]:
    """Batch-ingest a list of video URLs via ONE bulk transcript fetch.

    Brings up the Mongo connection once, resolves each URL → ``(canonical_url, oEmbed
    metadata)``, then runs the SHARED bulk core ONCE so there is exactly ONE
    ``fetch_many(all_urls)`` for the whole batch (the #080 fix — previously per-video
    ``fetch_many([url])`` inside per-URL sub-flows). The batch path NEVER calls the
    thin ``ingest_youtube_video`` flow, so it produces no per-item sub-flow runs.
    """

    await init_mongodb(
        settings.mongo.mongo_uri.get_secret_value(),
        settings.mongo.mongo_initdb_database,
    )

    fetcher = fetcher or _default_chained_fetcher()

    items: list[tuple[str, VideoMetadata]] = []
    for video_url in video_urls:
        resolved = await _resolve_video_item(video_url)
        if resolved is not None:
            items.append(resolved)

    ingested = await _bulk_build_and_load(items, user_id, fetcher)
    logger.info("Ingested %d videos out of %d URLs", len(ingested), len(video_urls))

    return ingested
