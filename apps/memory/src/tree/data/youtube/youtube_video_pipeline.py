"""Single-video YouTube leaf pipeline — shared bulk core + thin MCP flow (#080).

The direct-video path enriches videos via oEmbed CONCURRENTLY (the batch resolves all
URLs at once via ``tree.data.batch.gather_isolated``), then runs the SHARED
bulk-transcript core (``tree.data.youtube.youtube_ingest._bulk_build_and_load``):
ONE bulk ``fetch_many(all_urls)`` for the whole batch → ``build_document`` per slot
→ ``load_video_document`` per slot. This is the #080 fix — the batch previously did a
PER-VIDEO ``fetch_many([url])`` inside per-URL sub-flows.

The per-item sub-flow's body is demoted to the plain async core
``_ingest_youtube_video_one``; ``ingest_youtube_video`` remains a THIN @flow wrapper
used ONLY by the MCP URL router (``tree.data.ingest._ingest_youtube_video``) so a
single-URL ingest still gets its own Prefect flow run + Opik trace. The BATCH path
calls the shared core directly — NEVER the thin wrapper (no per-item sub-flow runs).

Transcripts are fetched via Gemini (``Part.from_uri`` — server-side fetch, no YouTube
IP block) inside the shared core's ``fetch_transcripts_batch`` task, which constructs the
``GeminiTranscriptFetcher`` itself. No fetcher is threaded through these flows, so task
inputs stay fully serializable.
"""

from __future__ import annotations

import logging

from beanie import PydanticObjectId
from prefect import flow

from tree.config.settings import settings
from tree.data.batch import gather_isolated
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

    ingested = await _bulk_build_and_load([resolved], user_id)
    return ingested[0] if ingested else None


@flow(name="ingest-youtube-video-etl", log_prints=True, validate_parameters=False)
async def ingest_youtube_video(
    video_url: str,
    user_id: PydanticObjectId,
) -> Document | None:
    """Thin MCP-only @flow: ingest ONE YouTube video via the core.

    The MCP ``ingest_url`` router (``tree.data.ingest._ingest_youtube_video``) calls
    this so single-URL ingest still gets its own Prefect flow run + Opik trace. The
    BATCH path does NOT call this — it runs the shared bulk core directly.
    """

    return await _ingest_youtube_video_one(video_url, user_id)


@flow(
    name="ingest-youtube-video-batch-etl",
    log_prints=True,
    validate_parameters=False,
)
async def ingest_youtube_video_batch(
    video_urls: list[str],
    user_id: PydanticObjectId,
) -> list[Document]:
    """Batch-ingest a list of video URLs via ONE bulk transcript fetch.

    Brings up the Mongo connection once, resolves all URLs CONCURRENTLY → ``[(canonical_url,
    oEmbed metadata), ...]`` via the shared ``gather_isolated`` helper (one oEmbed failure is
    isolated + skipped instead of sinking the batch; unresolvable ids return ``None`` and are
    dropped), then runs the SHARED bulk core ONCE so there is exactly ONE
    ``fetch_many(all_urls)`` for the whole batch (the #080 fix — previously per-video
    ``fetch_many([url])`` inside per-URL sub-flows). The batch path NEVER calls the
    thin ``ingest_youtube_video`` flow, so it produces no per-item sub-flow runs.
    """

    await init_mongodb(
        settings.mongo.mongo_uri.get_secret_value(),
        settings.mongo.mongo_initdb_database,
    )

    items, failures = await gather_isolated(video_urls, _resolve_video_item)
    if failures:
        logger.warning(
            "oEmbed resolution failed for %d/%d URLs", failures, len(video_urls)
        )

    ingested = await _bulk_build_and_load(items, user_id)
    logger.info("Ingested %d videos out of %d URLs", len(ingested), len(video_urls))

    return ingested
