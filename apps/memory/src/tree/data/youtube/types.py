"""Module-local data types for the YouTube data layer.

These types describe the contract between transcript fetchers and downstream
ETL pipelines (#003 single-video, #004 RSS). They live in a per-module
`types.py` per `CLAUDE.md`'s "loose clean architecture" rule — they are not
shared across apps, only across the `tree.data.youtube` submodules.
"""

from datetime import datetime

from pydantic import BaseModel, field_validator


class VideoMetadata(BaseModel):
    """Metadata describing a single YouTube video.

    `video_id` is always the bare 11-character YouTube identifier — never the
    full URL. All other fields are optional: the sole
    `GeminiTranscriptFetcher` only populates `video_id`, and downstream
    pipelines enrich the rest from feed entries (#004) or oEmbed (#003).
    """

    video_id: str
    title: str | None = None
    channel: str | None = None
    channel_id: str | None = None
    publish_date: datetime | None = None
    duration_seconds: int | None = None
    description: str | None = None

    @field_validator("publish_date")
    @classmethod
    def _publish_date_must_be_tz_aware(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("publish_date must be timezone-aware (UTC)")
        return value


def merge_video_metadata(base: VideoMetadata, override: VideoMetadata) -> VideoMetadata:
    """Merge two metadata records — every non-``None`` `override` field wins.

    The ONE merge both transcript branches run (ADR-004, Decision 5). `base` is
    the caller's metadata (oEmbed for a single video, the Atom feed entry for
    RSS); `override` is whatever the fetched transcript carries. A Bright Data
    record fills title / channel / channel_id / publish_date / duration /
    description, so those win; wherever the record is null the base survives.
    The Gemini branch's transcript metadata carries only `video_id`, so the same
    call leaves the base intact there — no branch-specific logic.

    Pure: returns a NEW `VideoMetadata`, mutating neither argument.
    """

    return base.model_copy(
        update={
            field: value
            for field, value in override.model_dump().items()
            if value is not None
        }
    )


class TranscriptSegment(BaseModel):
    """A single transcript segment as returned by the underlying backend."""

    text: str
    start_seconds: float
    duration_seconds: float


class FetchedTranscript(BaseModel):
    """A successfully fetched transcript for a single video.

    `segments` may be empty if the backend returned a transcript with no
    snippets. `plain_text` is the canonical newline-joined rendering used by
    downstream chunking / extraction.
    """

    metadata: VideoMetadata
    segments: list[TranscriptSegment]
    language: str | None = None
    plain_text: str
