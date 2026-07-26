"""Single-video YouTube path — oEmbed resolve + thin MCP flow.

The direct-video resolve ``_resolve_video_item`` enriches one URL via oEmbed
(``fetch_oembed_metadata`` + ``parse_oembed_metadata``) into ``(canonical_url,
VideoMetadata)`` — the direct-video metadata source, reused by the unified batch
(``youtube_pipeline_batch.ingest_youtube_batch``) for its single-video entries.

``_ingest_youtube_video_one`` is the plain async core (resolve → shared
``youtube_ingest._bulk_build_and_load``); ``ingest_youtube_video`` is a THIN @flow
wrapper used ONLY by the MCP URL router
(``tree.data.online_pipeline._ingest_youtube_video``), so a single-URL ingest still
gets its own Prefect flow run + Opik trace.
"""

from __future__ import annotations

import logging

from beanie import PydanticObjectId
from prefect import flow

from tree.data.youtube.types import VideoMetadata
from tree.data.youtube.urls import canonical_video_url, extract_video_id
from tree.data.youtube.youtube_ingest import _bulk_build_and_load
from tree.data.youtube.youtube_video import (
    fetch_oembed_metadata,
    parse_oembed_metadata,
)
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


def _partition_video_inputs(video_urls: list[str]) -> tuple[list[str], list[str]]:
    """Split loose video inputs into resolvable URLs and RAW unresolvable ones.

    The same "can this be resolved to a video id?" decision `_resolve_video_item`
    makes, hoisted so the BATCH path can hand its unresolvable inputs to the core
    as ``invalid_url`` ingest_error rows (ADR-004 §6) instead of dropping them —
    a `None` from an isolated gather carries no clue about WHICH input it was.
    """

    resolvable: list[str] = []
    invalid: list[str] = []
    for video_url in video_urls:
        if extract_video_id(video_url) is None:
            logger.warning("Could not resolve video id from input: %s", video_url)
            invalid.append(video_url)
        else:
            resolvable.append(video_url)
    return resolvable, invalid


async def _ingest_youtube_video_one(
    video_url: str,
    user_id: PydanticObjectId,
) -> Document | None:
    """Ingest a SINGLE video via the shared bulk core (plain async core, NO decorators).

    Resolves the id → canonical URL → per-video oEmbed metadata, then runs the SHARED
    ``_bulk_build_and_load`` over the one-item list (the transcript fallback chain +
    build + load). Shared by the thin MCP flow; the batch path calls the shared core
    directly with the whole URL list instead. Returns the persisted Document, or
    ``None`` for an unresolvable id / missing transcript / duplicate.

    An unresolvable input still runs the core — over ZERO items and the raw string as
    an ``invalid_url`` failure row, so the attempt is persisted, inspectable data
    rather than a WARNING that scrolls away. No transcript backend is touched.
    """

    resolved = await _resolve_video_item(video_url)
    if resolved is None:
        await _bulk_build_and_load([], user_id, invalid_inputs=[video_url])
        return None

    ingested = await _bulk_build_and_load([resolved], user_id)
    return ingested[0] if ingested else None


@flow(name="ingest-youtube-video-etl", log_prints=True, validate_parameters=False)
async def ingest_youtube_video(
    video_url: str,
    user_id: PydanticObjectId,
) -> Document | None:
    """Thin MCP-only @flow: ingest ONE YouTube video via the core.

    The MCP ``ingest_url`` router (``tree.data.online_pipeline._ingest_youtube_video``) calls
    this so single-URL ingest still gets its own Prefect flow run + Opik trace. The
    BATCH path does NOT call this — it runs the shared bulk core directly.

    NO ``retries`` here, deliberately (ADR-002 amendment #096, rules 3c + 5): the core
    delegates to ``_bulk_build_and_load``, whose tasks already retry
    (``fetch-youtube-transcripts-batch`` 2, ``load-youtube-batch`` 3). Adding a flow
    retry would STACK on those AND replay the billable Bright Data collection
    (~173 s + per-record billing) that #095 works to never pay twice.
    """

    return await _ingest_youtube_video_one(video_url, user_id)
