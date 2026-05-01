"""Swappable transcript-fetcher interface and ships-by-default implementations.

This module is the sole seam between YouTube ETL pipelines and any concrete
transcript backend. Pipelines (#003 single-video, #004 RSS) call a single
`TranscriptFetcher.fetch_many(...)` method; the chain wrapper transparently
advances slot-by-slot from the primary backend (free, lightweight) to any
fallback backends (e.g. the Gemini-backed fetcher in #002).

Design notes:
- Implementations MUST return one element per input slot, in the same order
  as the input list, with `None` indicating "this fetcher could not produce a
  transcript for that slot." This is what makes the chain wrapper's
  slot-by-slot advance possible.
- Per-video failures (missing transcript, unsupported video, unresolvable
  input) MUST NOT raise — they are returned as `None`. The chain wrapper
  alone owns the user-facing WARNING when it advances to the next fetcher.
- Catastrophic backend failures (auth error, malformed call, full-batch
  network outage) MAY raise; the chain wrapper does not catch those.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Protocol

from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api._errors import (
    NoTranscriptFound,
    TranscriptsDisabled,
    VideoUnavailable,
)

from tree.data.youtube.gemini_transcript_fetcher import GeminiTranscriptFetcher
from tree.data.youtube.types import (
    FetchedTranscript,
    TranscriptSegment,
    VideoMetadata,
)
from tree.data.youtube.urls import canonical_video_url, extract_video_id

__all__ = [
    "ChainedTranscriptFetcher",
    "GeminiTranscriptFetcher",
    "TranscriptFetcher",
    "YoutubeTranscriptApiFetcher",
]

logger = logging.getLogger(__name__)


class TranscriptFetcher(Protocol):
    """Swappable backend interface for fetching YouTube transcripts.

    Implementations must:
    - Return one element per input, preserving input order.
    - Return `None` for any slot they cannot transcribe (NEVER raise per-slot).
    - Be safe to call with arbitrary user-pasted URL shapes; non-YouTube /
      malformed inputs become `None` slots in the output.
    """

    async def fetch_many(
        self, video_urls_or_ids: list[str]
    ) -> list[FetchedTranscript | None]: ...


class YoutubeTranscriptApiFetcher:
    """Primary transcript fetcher backed by the `youtube-transcript-api` package.

    Lightweight: no API key, no paid call. Intended as the first element of a
    `ChainedTranscriptFetcher`.

    Metadata note: this fetcher only populates `VideoMetadata.video_id`.
    Real video metadata (title, channel, publish_date, ...) is enriched by
    the per-source pipeline:
    - The RSS pipeline (#004) reads it from feed entries.
    - The single-video pipeline (#003) reads it from the YouTube oEmbed
      endpoint (`https://www.youtube.com/oembed?url=...&format=json`,
      sync HTTP via `httpx.AsyncClient`, no API key).

    `proxy_config` is reserved as an extension point for the Webshare
    rotating-proxy integration; it is not consumed in v1.

    `languages` defaults to `("en",)` — the human-approved default. Not
    surfaced in YAML config in v1.
    """

    def __init__(
        self,
        languages: tuple[str, ...] = ("en",),
        proxy_config: object | None = None,
        concurrency: int = 5,
    ) -> None:
        self.languages = languages
        # Reserved; not consumed in v1. Stored on the instance so that future
        # subclasses / Webshare integration can pick it up without changing
        # the constructor signature.
        self.proxy_config = proxy_config
        self.concurrency = concurrency
        self._semaphore = asyncio.Semaphore(concurrency)

    async def fetch_many(
        self, video_urls_or_ids: list[str]
    ) -> list[FetchedTranscript | None]:
        if not video_urls_or_ids:
            return []

        async def _one(item: str) -> FetchedTranscript | None:
            async with self._semaphore:
                return await self._fetch_one(item)

        return await asyncio.gather(*(_one(item) for item in video_urls_or_ids))

    async def _fetch_one(self, url_or_id: str) -> FetchedTranscript | None:
        video_id = extract_video_id(url_or_id)
        if video_id is None:
            # Unresolvable input: silent at this layer; the chain wrapper /
            # caller decides the user-facing message.
            logger.debug("Could not resolve video id from input: %r", url_or_id)
            return None

        try:
            raw = await asyncio.to_thread(self._call_api, video_id)
        except (TranscriptsDisabled, NoTranscriptFound, VideoUnavailable) as exc:
            logger.debug(
                "youtube-transcript-api could not transcribe %s: %s",
                video_id,
                exc.__class__.__name__,
            )
            return None

        return self._to_fetched_transcript(video_id, raw)

    def _call_api(self, video_id: str) -> object:
        """Sync entry-point. Kept thin so tests can patch a single method."""

        return YouTubeTranscriptApi().fetch(video_id, languages=list(self.languages))

    @staticmethod
    def _to_fetched_transcript(video_id: str, raw: object) -> FetchedTranscript:
        # `youtube-transcript-api` >= 1.x returns a `FetchedTranscript`
        # object with `.snippets` (each snippet has `.text`, `.start`,
        # `.duration`) plus `.language_code`. We adapt to our domain type.
        snippets = list(getattr(raw, "snippets", []) or [])
        segments = [
            TranscriptSegment(
                text=getattr(s, "text", ""),
                start_seconds=float(getattr(s, "start", 0.0)),
                duration_seconds=float(getattr(s, "duration", 0.0)),
            )
            for s in snippets
        ]
        plain_text = "\n".join(seg.text for seg in segments)
        language = getattr(raw, "language_code", None)

        return FetchedTranscript(
            metadata=VideoMetadata(video_id=video_id),
            segments=segments,
            language=language,
            plain_text=plain_text,
        )


class ChainedTranscriptFetcher:
    """Composite fetcher that advances slot-by-slot through an ordered chain.

    Given fetchers `[primary, fallback_1, fallback_2, ...]`:

    1. Call `primary.fetch_many(video_urls_or_ids)` once on the full input.
    2. For each subsequent fetcher, gather only the inputs whose previous
       output was `None`, call the fetcher on that subset, and merge the
       results back into the original slot positions.
    3. Each time a slot advances from one fetcher to the next, log a WARNING
       naming the input and the fallback fetcher class.
    4. After the last fetcher, any remaining `None` slot gets a final
       "All transcript fetchers exhausted for ..." WARNING.

    The merged output preserves input order and length. Callers continue to
    interpret `None` as "no transcript available; skip this video."
    """

    def __init__(self, fetchers: list[TranscriptFetcher]) -> None:
        if not fetchers:
            raise ValueError("ChainedTranscriptFetcher needs at least one fetcher")
        self._fetchers = fetchers

    async def fetch_many(
        self, video_urls_or_ids: list[str]
    ) -> list[FetchedTranscript | None]:
        if not video_urls_or_ids:
            return []

        results: list[FetchedTranscript | None] = await self._fetchers[0].fetch_many(
            video_urls_or_ids
        )

        for prev_idx, fetcher in enumerate(self._fetchers[1:], start=1):
            pending_indices = [i for i, r in enumerate(results) if r is None]
            if not pending_indices:
                break
            pending_inputs = [video_urls_or_ids[i] for i in pending_indices]

            prev_fetcher = self._fetchers[prev_idx - 1]
            for inp in pending_inputs:
                logger.warning(
                    "%s returned no transcript for %s; falling back to %s",
                    prev_fetcher.__class__.__name__,
                    self._display_label(inp),
                    fetcher.__class__.__name__,
                )

            sub_results = await fetcher.fetch_many(pending_inputs)
            for slot_idx, sub_result in zip(pending_indices, sub_results):
                results[slot_idx] = sub_result

        for i, r in enumerate(results):
            if r is None:
                logger.warning(
                    "All transcript fetchers exhausted for %s; skipping",
                    self._display_label(video_urls_or_ids[i]),
                )

        return results

    @staticmethod
    def _display_label(url_or_id: str) -> str:
        """Best-effort canonical label for log messages."""

        video_id = extract_video_id(url_or_id)
        if video_id is None:
            return url_or_id
        return canonical_video_url(video_id)
