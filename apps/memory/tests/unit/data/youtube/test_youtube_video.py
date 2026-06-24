"""Unit tests for the pure logic in `tree.data.youtube.youtube_video`.

Covers oEmbed parsing, document assembly from a `FetchedTranscript`, and the
"missing metadata" fallbacks. The dedup/upsert helper (`load_video_document`)
is exercised end-to-end in the integration suite; the one branch covered here
is the `DuplicateKeyError -> None` in-batch collision skip, which the
integration sequential-rerun tests never reach (they short-circuit on the
`find_one` dedup check before `insert()`).
"""

from __future__ import annotations

from datetime import datetime, timezone

from beanie import PydanticObjectId
from pymongo.errors import DuplicateKeyError

from tree.data.youtube.types import (
    FetchedTranscript,
    TranscriptSegment,
    VideoMetadata,
)
from tree.data.youtube.youtube_video import (
    build_document,
    load_video_document,
    parse_oembed_metadata,
)
from tree.entities.documents import Document, SourceType

VIDEO_ID = "eYaWxljC4sA"
CANONICAL_URL = f"https://www.youtube.com/watch?v={VIDEO_ID}"
_USER_ID = PydanticObjectId("507f1f77bcf86cd799439011")


def _make_transcript(
    *, video_id: str = VIDEO_ID, plain_text: str = "hello world"
) -> FetchedTranscript:
    return FetchedTranscript(
        metadata=VideoMetadata(video_id=video_id),
        segments=[
            TranscriptSegment(text=plain_text, start_seconds=0.0, duration_seconds=1.0)
        ],
        language="en",
        plain_text=plain_text,
    )


class TestParseOembedMetadata:
    def test_happy_path_populates_title_and_channel(self) -> None:
        payload = {
            "title": "An Interesting Video",
            "author_name": "The Channel",
            "author_url": "https://www.youtube.com/@thechannel",
            "type": "video",
        }

        metadata = parse_oembed_metadata(payload, video_id=VIDEO_ID)

        assert metadata.video_id == VIDEO_ID
        assert metadata.title == "An Interesting Video"
        assert metadata.channel == "The Channel"
        # publish_date and duration are NOT in oEmbed → left None in v1.
        assert metadata.publish_date is None
        assert metadata.duration_seconds is None

    def test_empty_payload_returns_only_video_id(self) -> None:
        metadata = parse_oembed_metadata({}, video_id=VIDEO_ID)

        assert metadata.video_id == VIDEO_ID
        assert metadata.title is None
        assert metadata.channel is None
        assert metadata.publish_date is None

    def test_missing_author_keeps_channel_none(self) -> None:
        payload = {"title": "Title only"}

        metadata = parse_oembed_metadata(payload, video_id=VIDEO_ID)

        assert metadata.title == "Title only"
        assert metadata.channel is None


class TestBuildDocument:
    def test_assembles_document_with_youtube_source_type(self) -> None:
        metadata = VideoMetadata(video_id=VIDEO_ID, title="A Video", channel="Channel")
        transcript = _make_transcript(plain_text="line one\nline two")

        doc = build_document(
            video_id=VIDEO_ID,
            metadata=metadata,
            transcript=transcript,
            user_id=_USER_ID,
        )

        assert doc.source_type == SourceType.YOUTUBE
        assert doc.source_uri == CANONICAL_URL
        assert doc.content == "line one\nline two"
        assert doc.title == "A Video"
        assert doc.authors == ["Channel"]

    def test_falls_back_to_video_id_title_when_metadata_missing(self) -> None:
        metadata = VideoMetadata(video_id=VIDEO_ID)
        transcript = _make_transcript(plain_text="some words")

        doc = build_document(
            video_id=VIDEO_ID,
            metadata=metadata,
            transcript=transcript,
            user_id=_USER_ID,
        )

        assert doc.title == f"YouTube video {VIDEO_ID}"
        # No channel → empty authors list (per spec).
        assert doc.authors == []

    def test_summary_falls_back_to_transcript_prefix_when_no_title(self) -> None:
        metadata = VideoMetadata(video_id=VIDEO_ID)
        long_text = "x" * 500
        transcript = _make_transcript(plain_text=long_text)

        doc = build_document(
            video_id=VIDEO_ID,
            metadata=metadata,
            transcript=transcript,
            user_id=_USER_ID,
        )

        # No title → summary uses the transcript prefix (≤ 280 chars per spec).
        assert doc.summary is not None
        assert len(doc.summary) <= 280
        assert doc.summary == long_text[:280]

    def test_summary_uses_title_when_present(self) -> None:
        metadata = VideoMetadata(video_id=VIDEO_ID, title="A Real Title")
        transcript = _make_transcript()

        doc = build_document(
            video_id=VIDEO_ID,
            metadata=metadata,
            transcript=transcript,
            user_id=_USER_ID,
        )

        assert doc.summary == "A Real Title"

    def test_date_is_tz_aware_when_metadata_has_no_publish_date(self) -> None:
        metadata = VideoMetadata(video_id=VIDEO_ID, title="t")
        transcript = _make_transcript()

        doc = build_document(
            video_id=VIDEO_ID,
            metadata=metadata,
            transcript=transcript,
            user_id=_USER_ID,
        )

        assert doc.date is not None
        assert doc.date.tzinfo is not None
        assert doc.date.utcoffset() == timezone.utc.utcoffset(doc.date)

    def test_date_uses_publish_date_when_provided(self) -> None:
        publish = datetime(2024, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
        metadata = VideoMetadata(video_id=VIDEO_ID, title="t", publish_date=publish)
        transcript = _make_transcript()

        doc = build_document(
            video_id=VIDEO_ID,
            metadata=metadata,
            transcript=transcript,
            user_id=_USER_ID,
        )

        assert doc.date == publish


class TestLoadVideoDocument:
    async def test_returns_none_on_duplicate_key_race(self, mocker) -> None:
        """The in-batch collision path: a flattened unified batch can hold the
        same canonical URL twice (e.g. via a feed entry and a single source).
        Both pass `find_one` as "not present", then race on `insert()`; the
        second `insert()` raises `DuplicateKeyError`, which the loader must
        convert into a clean `None` skip rather than propagate.
        """
        doc = Document(
            source_type=SourceType.YOUTUBE,
            source_uri=CANONICAL_URL,
            user_id=_USER_ID,
        )

        # No existing doc → take the insert path (not the dedup early-return).
        mocker.patch(
            "tree.data.youtube.youtube_video.Document.find_one",
            new_callable=mocker.AsyncMock,
            return_value=None,
        )
        mocker.patch(
            "tree.data.youtube.youtube_video.Document.insert",
            new_callable=mocker.AsyncMock,
            side_effect=DuplicateKeyError("dup"),
        )

        result = await load_video_document(doc)

        assert result is None
