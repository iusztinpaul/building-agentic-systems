"""Unit tests for the pure logic in `tree.data.youtube.youtube_video`.

Covers oEmbed parsing, document assembly from a `FetchedTranscript`, and the
"missing metadata" fallbacks. The dedup/upsert helper (`load_video_document`)
is exercised end-to-end in the integration suite; the branches covered here are
the ones the integration sequential-rerun tests never reach — the
`DuplicateKeyError -> None` in-batch collision skip (they short-circuit on the
`find_one` dedup check before `insert()`) and the replace/skip decision table
around `ingest_error` and `SourceType.LATENT` (ADR-004 §6).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from beanie import PydanticObjectId
from pymongo.errors import DuplicateKeyError
from pytest_mock import MockerFixture

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
_LOADER_LOGGER = "tree.data.youtube.youtube_video"


def _incoming_doc() -> Document:
    """A freshly built YouTube Document, as `build_document` returns it."""

    return Document(
        source_type=SourceType.YOUTUBE,
        source_uri=CANONICAL_URL,
        user_id=_USER_ID,
        content="hello world",
    )


def _existing_doc(
    *, source_type: SourceType, ingest_error: str | None = None
) -> Document:
    """A Document already persisted at `CANONICAL_URL`, with a real id."""

    doc = Document(
        source_type=source_type,
        source_uri=CANONICAL_URL,
        user_id=_USER_ID,
        ingest_error=ingest_error,
    )
    doc.id = PydanticObjectId()
    return doc


def _patch_find_one(mocker: MockerFixture, existing: Document | None):
    return mocker.patch(
        f"{_LOADER_LOGGER}.Document.find_one",
        new_callable=mocker.AsyncMock,
        return_value=existing,
    )


def _patch_replace(mocker: MockerFixture):
    return mocker.patch(
        f"{_LOADER_LOGGER}.Document.replace",
        new_callable=mocker.AsyncMock,
    )


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
    async def test_replaces_existing_errored_row(self, mocker) -> None:
        """ADR-004 §6: a row carrying `ingest_error` is REPLACEABLE on a later
        run exactly like a LATENT row — same `doc.id` reuse + `replace()`.
        """
        existing = _existing_doc(
            source_type=SourceType.YOUTUBE,
            ingest_error="no_transcript: no transcript available",
        )
        doc = _incoming_doc()
        _patch_find_one(mocker, existing)
        replace = _patch_replace(mocker)

        result = await load_video_document(doc)

        assert result is doc
        assert doc.id == existing.id
        replace.assert_awaited_once()

    async def test_warns_with_source_uri_and_prior_error_on_reattempt(
        self, mocker, caplog
    ) -> None:
        prior_error = "no_transcript: no transcript available"
        existing = _existing_doc(
            source_type=SourceType.YOUTUBE, ingest_error=prior_error
        )
        doc = _incoming_doc()
        _patch_find_one(mocker, existing)
        _patch_replace(mocker)

        with caplog.at_level(logging.WARNING, logger=_LOADER_LOGGER):
            await load_video_document(doc)

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert CANONICAL_URL in warnings[0].getMessage()
        assert prior_error in warnings[0].getMessage()

    async def test_skips_existing_non_latent_document_without_error(
        self, mocker
    ) -> None:
        existing = _existing_doc(source_type=SourceType.YOUTUBE)
        doc = _incoming_doc()
        _patch_find_one(mocker, existing)
        replace = _patch_replace(mocker)

        result = await load_video_document(doc)

        assert result is None
        replace.assert_not_awaited()

    async def test_upgrades_latent_document(self, mocker) -> None:
        existing = _existing_doc(source_type=SourceType.LATENT)
        doc = _incoming_doc()
        _patch_find_one(mocker, existing)
        replace = _patch_replace(mocker)

        result = await load_video_document(doc)

        assert result is doc
        assert doc.id == existing.id
        replace.assert_awaited_once()

    async def test_latent_upgrade_does_not_emit_reattempt_warning(
        self, mocker, caplog
    ) -> None:
        existing = _existing_doc(source_type=SourceType.LATENT)
        doc = _incoming_doc()
        _patch_find_one(mocker, existing)
        _patch_replace(mocker)

        with caplog.at_level(logging.WARNING, logger=_LOADER_LOGGER):
            await load_video_document(doc)

        assert [r for r in caplog.records if r.levelno == logging.WARNING] == []

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
