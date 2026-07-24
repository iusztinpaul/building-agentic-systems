"""YouTube transcript fetcher backed by Bright Data's Web Scraper API.

`BrightDataTranscriptFetcher` collects the real caption transcript (plain text
plus timestamped segments) AND rich metadata (title, channel, channel id,
publish date, duration, description) from the pre-built YouTube dataset
``gd_lk56epmy2i5g7lzu0k``, at ~$0.70/1,000 records — orders of magnitude
cheaper than Gemini video tokens. It is the PRIMARY backend; the Gemini
fetcher is the fallback (ADR-004).

Design notes:
- COMPLETELY separate from `GeminiTranscriptFetcher`: no base class, no
  inheritance. The two share only the contract
  ``async fetch_many(list[str]) -> list[FetchedTranscript | None]``. A formal
  interface waits for a real third backend (ADR-004, Decision 4).
- ONE collection per batch: every resolvable input goes into a single
  `collect(...)` call, the one thin seam unit tests patch.
- Per-slot misses (unresolvable input, absent record, transcript-less record)
  return `None` in that slot. Batch-WIDE client errors PROPAGATE — the
  fallback chain in #092 distinguishes "this video has no transcript" from
  "the whole collection failed", and flattening them to `None`s would erase
  that distinction.
- This layer NEVER emits a WARNING (mirrors the Gemini fetcher): the
  user-facing warnings live in the pipeline layer.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from pydantic import SecretStr

from tree.config.app_config import app_config
from tree.config.settings import settings
from tree.data.web.web_scraper_api import collect
from tree.data.web.web_unlocker import BrightDataConfigurationError
from tree.data.youtube.types import (
    FetchedTranscript,
    TranscriptSegment,
    VideoMetadata,
)
from tree.data.youtube.urls import canonical_video_url, extract_video_id

logger = logging.getLogger(__name__)


# Bright Data's pre-built YouTube scraper. API identity, not tuning — so a
# constant here rather than a YAML knob (ADR-004, Decision 7).
_YOUTUBE_DATASET_ID = "gd_lk56epmy2i5g7lzu0k"

# `formatted_transcript` offsets are milliseconds; `TranscriptSegment` is
# seconds.
_MILLISECONDS_PER_SECOND = 1000.0


class BrightDataTranscriptFetcher:
    """The primary YouTube transcript fetcher (Bright Data Web Scraper API).

    One `fetch_many` call triggers exactly ONE Bright Data collection over all
    resolvable inputs and returns a list aligned to the input order.
    """

    def __init__(
        self,
        *,
        api_key: SecretStr | None = None,
        timeout_seconds: float | None = None,
        poll_interval_seconds: float | None = None,
    ) -> None:
        """Guard the credential up-front and resolve the timing knobs.

        The key is only a construction-time guard (mirroring the Gemini
        fetcher's raise-on-missing-key shape): the request itself is
        authenticated inside `collect`, which reads the same
        ``BRIGHTDATA_API_KEY``. Raising here keeps an unconfigured backend from
        being constructed at all — #092 never builds one without credentials.
        """

        resolved_key = api_key if api_key is not None else settings.brightdata_api_key
        if not (resolved_key and resolved_key.get_secret_value()):
            raise BrightDataConfigurationError(
                "BRIGHTDATA_API_KEY is not set; see .env.example"
            )

        self.timeout_seconds = (
            timeout_seconds
            if timeout_seconds is not None
            else app_config.youtube.brightdata_timeout_seconds
        )
        self.poll_interval_seconds = (
            poll_interval_seconds
            if poll_interval_seconds is not None
            else app_config.youtube.brightdata_poll_interval_seconds
        )

    async def fetch_many(
        self, video_urls_or_ids: list[str]
    ) -> list[FetchedTranscript | None]:
        """Fetch transcripts for a batch, preserving input order.

        Raises:
            BrightDataConfigurationError, BrightDataRequestError,
            BrightDataTimeoutError: Batch-wide failures, propagated verbatim so
                the caller can fall the WHOLE batch back to Gemini.
        """

        if not video_urls_or_ids:
            return []

        slot_ids = [self._resolve(item) for item in video_urls_or_ids]

        # De-duplicated so a repeated video is collected (and billed) once, and
        # unresolvable inputs are never submitted at all — invalid inputs are
        # billable too.
        collectable_ids = list(dict.fromkeys(i for i in slot_ids if i is not None))
        if not collectable_ids:
            return [None] * len(slot_ids)

        records = await collect(
            _YOUTUBE_DATASET_ID,
            [{"url": canonical_video_url(video_id)} for video_id in collectable_ids],
            timeout_seconds=self.timeout_seconds,
            poll_interval_seconds=self.poll_interval_seconds,
        )
        records_by_id = self._index_by_video_id(records)

        return [
            self._to_transcript(records_by_id.get(slot_id), slot_id)
            if slot_id is not None
            else None
            for slot_id in slot_ids
        ]

    @staticmethod
    def _resolve(url_or_id: str) -> str | None:
        """Resolve one input to a video id, `None` (debug-logged) if it can't."""

        video_id = extract_video_id(url_or_id)
        if video_id is None:
            # Silent at this layer; the user-facing WARNING lives in the
            # pipeline (mirrors the Gemini fetcher).
            logger.debug("Could not resolve video id from input: %r", url_or_id)
        return video_id

    @staticmethod
    def _index_by_video_id(records: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        """Key records by video id — they come back in an arbitrary order."""

        indexed: dict[str, dict[str, Any]] = {}
        for record in records:
            source_url = record.get("url") or (record.get("input") or {}).get("url")
            video_id = extract_video_id(source_url) if source_url else None
            if video_id is not None:
                indexed.setdefault(video_id, record)
        return indexed

    @classmethod
    def _to_transcript(
        cls, record: dict[str, Any] | None, video_id: str
    ) -> FetchedTranscript | None:
        """Map one record to a `FetchedTranscript`, `None` if it has no transcript."""

        if record is None:
            logger.debug("Bright Data returned no record for %s", video_id)
            return None

        plain_text = record.get("transcript")
        if not isinstance(plain_text, str) or not plain_text.strip():
            logger.debug("Bright Data record for %s has no transcript", video_id)
            return None

        return FetchedTranscript(
            metadata=cls._to_metadata(record, video_id),
            segments=cls._to_segments(record.get("formatted_transcript")),
            # ONLY `transcription_language` — `transcript_language` is the list
            # of languages YouTube OFFERS, not the one this transcript is in.
            language=cls._non_empty_str(record.get("transcription_language")),
            plain_text=plain_text,
        )

    @classmethod
    def _to_metadata(cls, record: dict[str, Any], video_id: str) -> VideoMetadata:
        """Map a record's metadata fields, falling back to the slot's video id."""

        return VideoMetadata(
            video_id=cls._non_empty_str(record.get("video_id")) or video_id,
            title=cls._non_empty_str(record.get("title")),
            # `handle_name` is the display name ("Rick Astley"), `youtuber` the
            # "@handle" — the display name matches what oEmbed/Atom put in
            # `channel`, so it wins.
            channel=cls._non_empty_str(record.get("handle_name"))
            or cls._non_empty_str(record.get("youtuber")),
            channel_id=cls._non_empty_str(record.get("youtuber_id")),
            publish_date=cls._to_utc_datetime(record.get("date_posted")),
            duration_seconds=cls._to_int(record.get("video_length")),
            description=cls._non_empty_str(record.get("description")),
        )

    @classmethod
    def _to_segments(cls, raw_segments: Any) -> list[TranscriptSegment]:
        """Map `formatted_transcript` entries, converting milliseconds to seconds."""

        if not isinstance(raw_segments, list):
            return []

        return [
            TranscriptSegment(
                text=entry["text"],
                start_seconds=cls._ms_to_seconds(entry.get("start_time")),
                duration_seconds=cls._ms_to_seconds(entry.get("duration")),
            )
            for entry in raw_segments
            if isinstance(entry, dict) and isinstance(entry.get("text"), str)
        ]

    @staticmethod
    def _ms_to_seconds(value: Any) -> float:
        """Milliseconds to seconds; a missing/non-numeric offset becomes 0.0."""

        if isinstance(value, bool) or not isinstance(value, int | float):
            return 0.0
        return value / _MILLISECONDS_PER_SECOND

    @staticmethod
    def _non_empty_str(value: Any) -> str | None:
        """The value when it is a non-blank string, else `None`."""

        if isinstance(value, str) and value.strip():
            return value
        return None

    @staticmethod
    def _to_int(value: Any) -> int | None:
        """The value as an int when it is numeric, else `None`."""

        if isinstance(value, bool) or not isinstance(value, int | float):
            return None
        return int(value)

    @staticmethod
    def _to_utc_datetime(value: Any) -> datetime | None:
        """Parse an ISO-8601 timestamp to a tz-aware UTC datetime.

        Unparseable input yields `None`; a naive timestamp is read as UTC. The
        project forbids naive datetimes downstream, so this never returns one.
        """

        if not isinstance(value, str) or not value.strip():
            return None

        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            logger.debug("Unparseable date_posted: %r", value)
            return None

        if parsed.tzinfo is None:
            return parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
