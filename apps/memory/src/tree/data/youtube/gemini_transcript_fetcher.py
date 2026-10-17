"""YouTube transcript fetcher backed by Gemini 3.8 Flash agentic video processing."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from google import genai
from pydantic import BaseModel, SecretStr, ValidationError

from tree.config.settings import settings
from tree.data.youtube.types import (
    FetchedTranscript,
    TranscriptSegment,
    VideoMetadata,
)
from tree.data.youtube.youtube import canonical_video_url, extract_video_id
from tree.observability import track, update_current_span

logger = logging.getLogger(__name__)


# Agentic video processing runs only on Gemini 3.8 Flash, 3.6 Flash and
# 3.5 Flash-Lite (https://ai.google.dev/gemini-api/docs/video-understanding,
# read 2026-10-17). ``gemini-3.7-flash`` is still accepted but the API serves
# it as ``gemini-3.8-flash`` (verified live 2026-10-17), so we pin the latter.
_DEFAULT_MODEL = "gemini-3.8-flash"

_TRANSCRIPT_PROMPT = (
    "Return the verbatim spoken transcript of this YouTube video in English "
    "as the `transcript` field of a JSON object: one sentence per line, "
    "no timestamps, no commentary, no summary."
)


class _TranscriptResponse(BaseModel):
    """The structured output Gemini must return for one video.

    Agentic processing returns its working notes in the answer when the
    answer is plain text (``thought / Let's see: / 00:01 "All right..."``
    instead of the transcript). Measured live
    on a 19s clip, 2026-10-17: static processing 0/8 leaked, agentic plain
    text 6/16, agentic with this schema 0/12. So the schema stays, even
    though plain text looks simpler.
    """

    transcript: str


class GeminiTranscriptFetcher:
    """The fallback YouTube transcript fetcher (ADR-004).

    `BrightDataTranscriptFetcher` is the primary backend; this one runs only
    over the slots it could not transcribe, or over the whole batch when
    Bright Data is unavailable or unconfigured.

    Sends the YouTube video URL to Gemini 3.8 Flash through the Interactions
    API as a ``{"type": "video", "processing": "agentic"}`` input and asks the
    model to return a verbatim transcript. On an 18 min video agentic
    processing measured 14,906 tokens vs 111,810 for the default fixed 1 FPS
    (static) sampling; on a 19s clip it costs ~50% more. Interactions are
    not stored server-side (``store=False``).

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
        # Not wrapped with ``track_genai_client``: Opik's genai integration only
        # patches ``models.generate_content`` (checked up to opik 2.2.103), so it
        # never sees ``interactions.create``. ``_call_gemini`` opens its own
        # Opik span and records the token usage instead — video tokens are the
        # main data-pipeline model spend.
        self._client = genai.Client(api_key=secret_value)

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

    @track(type="llm", name="gemini-agentic-video-transcript")
    async def _call_gemini(self, canonical_url: str) -> Any:
        """Single Gemini call. Kept thin so tests can patch one method."""

        interaction = await self._client.aio.interactions.create(
            model=self.model,
            input=[
                {"type": "video", "uri": canonical_url, "processing": "agentic"},
                {"type": "text", "text": _TRANSCRIPT_PROMPT},
            ],
            response_mime_type="application/json",
            response_format=_TranscriptResponse.model_json_schema(),
            store=False,
        )
        _record_interaction_usage(interaction)
        return interaction

    @staticmethod
    def _extract_text(response: Any) -> str | None:
        """Best-effort transcript extraction from a Gemini interaction.

        Returns ``None`` if the interaction did not complete or the response
        is not a valid ``_TranscriptResponse`` JSON object.
        """

        try:
            status = getattr(response, "status", None)
            if status != "completed":
                logger.debug(
                    "Gemini interaction ended with status %r: %s",
                    status,
                    getattr(response, "errors", None),
                )
                return None
            text = getattr(response, "output_text", None)
        except Exception as exc:
            logger.debug(
                "Gemini response refused text extraction: %s",
                exc.__class__.__name__,
            )
            return None
        if text is None:
            return None
        try:
            return _TranscriptResponse.model_validate_json(text).transcript
        except ValidationError as exc:
            logger.debug(
                "Gemini returned a malformed transcript object: %s",
                exc.error_count(),
            )
            return None


def _record_interaction_usage(interaction: Any) -> None:
    """Record the interaction's token usage on the current Opik span, fail-open.

    In agentic mode the video tokens are reported under ``tool_use`` (the
    model's own processing calls), so they count as prompt tokens; thought
    tokens count as completion tokens. E.g. a 19s clip measured 162 input +
    812 tool-use prompt tokens and 113 output + 593 thought completion tokens.
    """

    try:
        usage = interaction.usage
        if usage is None:
            return
        update_current_span(
            provider="google_ai",
            model=interaction.model,
            usage={
                "prompt_tokens": (usage.total_input_tokens or 0)
                + (usage.total_tool_use_tokens or 0),
                "completion_tokens": (usage.total_output_tokens or 0)
                + (usage.total_thought_tokens or 0),
                "total_tokens": usage.total_tokens or 0,
            },
        )
    except Exception as exc:  # noqa: BLE001 — telemetry must never break the call
        logger.debug("Opik Gemini usage recording no-op: %s", exc)
