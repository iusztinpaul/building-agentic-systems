"""Prefect pipeline wrappers around the single-video YouTube ETL.

Mirrors `tree.data.substack.substack_article_pipeline` line-for-line — same
``@task`` / ``@flow`` shape, same ``init_mongodb`` boundary in the batch
flow, same ``log_prints=True`` so the orchestrator log stream picks up
inner ``print(...)`` traffic from third-party libs.

The fetcher is the swap point. Production calls
``ingest_youtube_video(url)`` and gets the default chained
``[YoutubeTranscriptApiFetcher, GeminiTranscriptFetcher]`` (free primary +
paid fallback). Tests inject a fake `TranscriptFetcher` to avoid network
and Gemini auth.
"""

from __future__ import annotations

import asyncio
import logging

from prefect import flow, task

from tree.config.settings import settings
from tree.data.youtube.transcript_fetcher import (
    ChainedTranscriptFetcher,
    GeminiTranscriptFetcher,
    TranscriptFetcher,
    YoutubeTranscriptApiFetcher,
)
from tree.data.youtube.urls import canonical_video_url, extract_video_id
from tree.data.youtube.youtube_video import (
    build_document,
    fetch_oembed_metadata,
    load_video_document,
    parse_oembed_metadata,
)
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


@task(name="fetch-youtube-video", retries=2, retry_delay_seconds=5)
async def fetch_video_task(
    video_url: str, fetcher: TranscriptFetcher
) -> tuple[Document, str] | None:
    """Resolve, transcribe, enrich, and assemble — return (doc, video_id) or None.

    Returns ``None`` when:
    - The input URL cannot be resolved to a video ID, OR
    - The fetcher chain returns ``None`` for the slot (no transcript even
      after the Gemini fallback). The chain has already emitted the
      user-facing WARNING; this layer logs nothing extra.
    """

    video_id = extract_video_id(video_url)
    if video_id is None:
        logger.warning("Could not resolve video id from input: %s", video_url)
        return None

    canonical_url = canonical_video_url(video_id)

    transcripts = await fetcher.fetch_many([canonical_url])
    transcript = transcripts[0] if transcripts else None
    if transcript is None:
        # Chain has already warned; no redundant pipeline-layer warning.
        return None

    payload = await fetch_oembed_metadata(canonical_url)
    metadata = parse_oembed_metadata(payload, video_id=video_id)

    doc = build_document(video_id=video_id, metadata=metadata, transcript=transcript)
    return doc, video_id


@task(name="load-youtube-video-document", retries=1, retry_delay_seconds=2)
async def load_video_task(doc: Document) -> Document | None:
    return await load_video_document(doc)


@flow(name="ingest-youtube-video-etl", log_prints=True, validate_parameters=False)
async def ingest_youtube_video(
    video_url: str, fetcher: TranscriptFetcher | None = None
) -> Document | None:
    """Ingest a single YouTube video URL into the documents collection.

    The ``fetcher`` argument is the swap point: tests inject a fake
    `TranscriptFetcher` to avoid network + Gemini auth; production callers
    omit it and get the default chained primary+Gemini implementation.
    """

    fetcher = fetcher or _default_chained_fetcher()

    fetched = await fetch_video_task(video_url, fetcher)
    if fetched is None:
        return None

    doc, _ = fetched
    return await load_video_task(doc)


@flow(
    name="ingest-youtube-video-batch-etl",
    log_prints=True,
    validate_parameters=False,
)
async def ingest_youtube_video_batch(
    video_urls: list[str], fetcher: TranscriptFetcher | None = None
) -> list[Document]:
    """Batch-ingest a list of video URLs.

    Mirrors `ingest_substack_article_batch`: bring up the Mongo connection
    once at the flow boundary, then `asyncio.gather` over individual ingests.
    """

    await init_mongodb(
        settings.mongo.mongo_uri.get_secret_value(),
        settings.mongo.mongo_initdb_database,
    )

    fetcher = fetcher or _default_chained_fetcher()

    results = await asyncio.gather(
        *[ingest_youtube_video(url, fetcher=fetcher) for url in video_urls]
    )

    ingested = [doc for doc in results if doc is not None]
    logger.info("Ingested %d videos out of %d URLs", len(ingested), len(video_urls))

    return ingested
