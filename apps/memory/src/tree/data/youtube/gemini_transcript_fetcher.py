"""YouTube transcript fetcher backed by Gemini 3.5 Flash.

This is the FALLBACK transcript backend (ADR-004,
`docs/adrs/004_brightdata_primary_youtube_transcript_backend.md`):
`brightdata_transcript_fetcher.BrightDataTranscriptFetcher` is primary, and
this fetcher runs only over the slots Bright Data could not transcribe — or
over the whole batch when Bright Data is unavailable or unconfigured. It
sends the canonical YouTube URL straight to Gemini via the multimodal
`Part.from_uri(...)` API and asks the model for a verbatim transcript.

Costs Gemini video tokens per call — the dominant data-pipeline model spend,
and the reason it is second in the chain. Every fallback into this fetcher is
WARNING-logged by the caller (`fetch_transcripts_batch`) with the reason, the
slot count, and the cost consequence, so the spend is never silent.

Returns `None` only on Gemini-side errors (auth, quota, refusal, malformed
response, empty body); a successful Gemini response always yields a
`FetchedTranscript`.

Design notes:
- The model id is hard-coded at the constructor (`gemini-3.5-flash`).
  Surface as a YAML knob in a follow-up task only if a user asks; the v1
  shape is intentionally minimal.
- This fetcher only fills `VideoMetadata.video_id`. Per-source pipelines
  (oEmbed in #003, Atom feed entries in #004) are responsible for title /
  channel enrichment — Gemini may include that prose in the response body,
  but we deliberately don't parse it.
- Per-slot failures return `None`; this layer NEVER emits a WARNING. The
  `None`-slot WARNING still lives in `fetch_transcripts_batch`
  (`tree.data.youtube.youtube_pipeline`), which names the skipped video —
  after #092 it fires for slots the WHOLE chain exhausted, carrying a
  normalized `no_transcript: …` error naming both backends' states.
"""

from __future__ import annotations

import asyncio
import logging

from google import genai
from google.genai.types import Content, Part
from pydantic import SecretStr

from tree.config.settings import settings
from tree.data.youtube.types import (
    FetchedTranscript,
    TranscriptSegment,
    VideoMetadata,
)
from tree.data.youtube.youtube import canonical_video_url, extract_video_id
from tree.observability import track_genai_client

logger = logging.getLogger(__name__)


_DEFAULT_MODEL = "gemini-3.5-flash"

_TRANSCRIPT_PROMPT = (
    "Return the verbatim spoken transcript of this YouTube video in English. "
    "Output transcript text only, one sentence per line, no timestamps, "
    "no commentary, no summary."
)


class GeminiTranscriptFetcher:
    """The fallback YouTube transcript fetcher (ADR-004).

    `BrightDataTranscriptFetcher` is the primary backend; this one runs only
    over the slots it could not transcribe, or over the whole batch when
    Bright Data is unavailable or unconfigured.

    Sends the YouTube video URL directly to Gemini 3.5 Flash via
    ``Part.from_uri(file_uri=<youtube_url>, mime_type="video/*")`` and asks
    the model to return a verbatim transcript.

    Costs Gemini video tokens per call, and every fallback into it is
    WARNING-logged by `fetch_transcripts_batch`. Returns ``None`` only on
    Gemini-side errors (auth, quota, refusal, malformed response, empty
    body); a successful Gemini response always yields a `FetchedTranscript`.

    Metadata note: only ``VideoMetadata.video_id`` is populated here. Other
    fields (title, channel, ...) are filled by per-source pipelines: oEmbed
    in #003 (single-video) and Atom feed entries in #004 (RSS).
    """

    def __init__(
        self,
        *,
        api_key: SecretStr | None = None,
        model: str = _DEFAULT_MODEL,
        concurrency: int = 3,
    ) -> None:
        resolved_key = api_key if api_key is not None else settings.google_api_key
        secret_value = resolved_key.get_secret_value() if resolved_key else ""
        if not secret_value:
            raise RuntimeError("GOOGLE_API_KEY is not configured; see .env.example")

        self.model = model
        self.concurrency = concurrency
        self._semaphore = asyncio.Semaphore(concurrency)
        # Wrap with Opik's genai integration for automatic spans + native Gemini
        # token usage / cost on the transcription calls (video tokens are the main
        # data-pipeline model spend, and these calls were previously untracked). No-op
        # passthrough when Opik is unconfigured — mirrors ``GeminiLLM``; see
        # :func:`tree.observability.track_genai_client`.
        self._client = track_genai_client(genai.Client(api_key=secret_value))

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
            # Unresolvable input: silent at this layer; the user-facing
            # `None`-slot WARNING lives in `fetch_transcripts_batch`.
            logger.debug("Could not resolve video id from input: %r", url_or_id)
            return None

        canonical_url = canonical_video_url(video_id)

        try:
            response = await self._call_gemini(canonical_url)
        except Exception as exc:
            logger.debug(
                "Gemini call failed for %s: %s", video_id, exc.__class__.__name__
            )
            return None

        text = self._extract_text(response)
        if not text or not text.strip():
            logger.debug("Gemini returned empty/whitespace for %s", video_id)
            return None

        return FetchedTranscript(
            metadata=VideoMetadata(video_id=video_id),
            segments=[
                TranscriptSegment(
                    text=text,
                    start_seconds=0.0,
                    duration_seconds=0.0,
                )
            ],
            language="en",
            plain_text=text,
        )

    async def _call_gemini(self, canonical_url: str) -> object:
        """Single Gemini call. Kept thin so tests can patch one method."""

        contents = [
            Content(
                role="user",
                parts=[
                    Part.from_uri(file_uri=canonical_url, mime_type="video/*"),
                    Part(text=_TRANSCRIPT_PROMPT),
                ],
            )
        ]
        return await self._client.aio.models.generate_content(
            model=self.model,
            contents=contents,
        )

    @staticmethod
    def _extract_text(response: object) -> str | None:
        """Best-effort plain-text extraction from a Gemini response.

        Returns ``None`` if the response is malformed or the safety filter
        prevents text extraction.
        """

        try:
            text = getattr(response, "text", None)
        except Exception as exc:
            logger.debug(
                "Gemini response refused text extraction: %s",
                exc.__class__.__name__,
            )
            return None
        if text is None:
            return None
        return str(text)
