"""Unit tests for the pure logic in `tree.data.youtube.youtube`.

Mirrors `tests/unit/data/substack/test_substack.py`. Covers:
- URL/ID helpers (`extract_video_id`, `canonical_video_url`,
  `extract_channel_id_from_rss_url`).
- RSS path: `extract_video_url` resolution order, `feed_entry_to_metadata`
  mapping + tz-awareness invariants, `fetch_feed` HTTP / bozo / empty-feed
  behaviour (mocked httpx + feedparser).
- Single-video path: oEmbed parsing, document assembly from a
  `FetchedTranscript`, and the "missing metadata" fallbacks.
- Load: the branches the integration sequential-rerun tests never reach — the
  `DuplicateKeyError -> None` in-batch collision skip (they short-circuit on
  the `find_one` dedup check before `insert()`) and the replace/skip decision
  table around `ingest_error` and `SourceType.LATENT` (ADR-004 §6).
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import httpx
import pytest
from beanie import PydanticObjectId
from pymongo.errors import DuplicateKeyError
from pytest_mock import MockerFixture

from tree.data.youtube.types import (
    FetchedTranscript,
    TranscriptSegment,
    VideoMetadata,
)
from tree.data.youtube.youtube import (
    INVALID_URL_ERROR,
    build_document,
    build_failure_document,
    canonical_video_url,
    extract_channel_id_from_rss_url,
    extract_video_id,
    extract_video_url,
    feed_entry_to_metadata,
    fetch_feed,
    load_video_document,
    parse_oembed_metadata,
)
from tree.entities.documents import Document, SourceType

VIDEO_ID = "eYaWxljC4sA"
CANONICAL_URL = f"https://www.youtube.com/watch?v={VIDEO_ID}"
_USER_ID = PydanticObjectId("507f1f77bcf86cd799439011")
_LOADER_LOGGER = "tree.data.youtube.youtube"


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


class TestBuildFailureDocument:
    """Persisted ingest-failure rows (ADR-004 §6)."""

    def test_no_transcript_row_keeps_base_metadata_and_drops_content(self) -> None:
        metadata = VideoMetadata(
            video_id=VIDEO_ID, title="An Interesting Video", channel="The Channel"
        )

        doc = build_failure_document(
            source_uri=CANONICAL_URL,
            metadata=metadata,
            ingest_error="no_transcript: brightdata + gemini both returned empty",
            user_id=_USER_ID,
        )

        assert doc.source_type == SourceType.YOUTUBE
        assert doc.source_uri == CANONICAL_URL
        assert doc.title == "An Interesting Video"
        assert doc.authors == ["The Channel"]
        # `content=None` is what keeps the row out of extraction (the existing
        # `{"content": {"$ne": None}}` filter).
        assert doc.content is None
        assert doc.ingest_error == (
            "no_transcript: brightdata + gemini both returned empty"
        )

    def test_no_transcript_row_carries_the_known_publish_date(self) -> None:
        publish = datetime(2024, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
        metadata = VideoMetadata(video_id=VIDEO_ID, publish_date=publish)

        doc = build_failure_document(
            source_uri=CANONICAL_URL,
            metadata=metadata,
            ingest_error="no_transcript: brightdata returned empty; gemini not configured",
            user_id=_USER_ID,
        )

        assert doc.date == publish

    def test_invalid_url_row_is_keyed_on_the_raw_input(self) -> None:
        doc = build_failure_document(
            source_uri="pls transcribe this",
            metadata=None,
            ingest_error=INVALID_URL_ERROR,
            user_id=_USER_ID,
        )

        assert doc.source_uri == "pls transcribe this"
        assert doc.title is None
        assert doc.authors == []
        assert doc.date is None
        assert doc.content is None
        assert doc.ingest_error == "invalid_url: no video id in input"


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

    async def test_new_failure_row_is_not_logged_as_an_ingest(
        self, mocker, caplog
    ) -> None:
        doc = Document(
            source_type=SourceType.YOUTUBE,
            source_uri=CANONICAL_URL,
            user_id=_USER_ID,
            ingest_error=INVALID_URL_ERROR,
        )
        _patch_find_one(mocker, None)
        mocker.patch(f"{_LOADER_LOGGER}.Document.insert", new_callable=mocker.AsyncMock)

        with caplog.at_level(logging.INFO, logger=_LOADER_LOGGER):
            await load_video_document(doc)

        assert "Recorded ingest failure" in caplog.text
        assert "Ingested:" not in caplog.text

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
            "tree.data.youtube.youtube.Document.find_one",
            new_callable=mocker.AsyncMock,
            return_value=None,
        )
        mocker.patch(
            "tree.data.youtube.youtube.Document.insert",
            new_callable=mocker.AsyncMock,
            side_effect=DuplicateKeyError("dup"),
        )

        result = await load_video_document(doc)

        assert result is None


class TestExtractVideoId:
    @pytest.mark.parametrize(
        "url, expected",
        [
            (f"https://www.youtube.com/watch?v={VIDEO_ID}", VIDEO_ID),
            (f"https://youtu.be/{VIDEO_ID}", VIDEO_ID),
            (f"https://www.youtube.com/shorts/{VIDEO_ID}", VIDEO_ID),
            (f"https://m.youtube.com/watch?v={VIDEO_ID}&t=10s", VIDEO_ID),
            (f"https://www.youtube.com/watch?v={VIDEO_ID}&feature=share", VIDEO_ID),
            (VIDEO_ID, VIDEO_ID),
            ("https://example.com/foo", None),
            ("", None),
            ("abcdefghij", None),  # 10 chars, not a valid bare ID
            ("not-a-url", None),
        ],
    )
    def test_resolves_known_shapes(self, url, expected):
        assert extract_video_id(url) == expected


class TestExtractChannelIdFromRssUrl:
    def test_resolves_channel_id(self):
        url = (
            "https://www.youtube.com/feeds/videos.xml"
            "?channel_id=UCkyHDwRWMEluOEYmOGJ_2nw"
        )
        assert extract_channel_id_from_rss_url(url) == "UCkyHDwRWMEluOEYmOGJ_2nw"

    @pytest.mark.parametrize(
        "url",
        [
            f"https://www.youtube.com/watch?v={VIDEO_ID}",
            "https://www.youtube.com/feeds/videos.xml",  # missing channel_id
            "https://example.com/feeds/videos.xml?channel_id=UCfoo",
            "",
        ],
    )
    def test_returns_none_for_non_rss_urls(self, url):
        assert extract_channel_id_from_rss_url(url) is None


class TestCanonicalVideoUrl:
    def test_returns_canonical_form(self):
        assert canonical_video_url(VIDEO_ID) == CANONICAL_URL

    def test_round_trips_from_youtu_be(self):
        # Story: SWE pastes any YouTube URL form and gets the same canonical
        # document. Composing extract_video_id + canonical_video_url is the
        # contract used by Document.source_uri to dedupe URL variants.
        assert (
            canonical_video_url(extract_video_id(f"https://youtu.be/{VIDEO_ID}"))
            == CANONICAL_URL
        )

    def test_round_trips_from_watch_with_query(self):
        assert (
            canonical_video_url(
                extract_video_id(f"https://www.youtube.com/watch?v={VIDEO_ID}&t=42s")
            )
            == CANONICAL_URL
        )


class TestExtractVideoUrl:
    def test_uses_yt_videoid_when_present(self) -> None:
        entry = {"yt_videoid": VIDEO_ID, "link": "https://example.com/wrong"}
        assert extract_video_url(entry) == CANONICAL_URL

    def test_falls_back_to_link_when_yt_videoid_missing(self) -> None:
        entry = {"link": CANONICAL_URL}
        assert extract_video_url(entry) == CANONICAL_URL

    def test_falls_back_to_link_short_url(self) -> None:
        entry = {"link": f"https://youtu.be/{VIDEO_ID}"}
        assert extract_video_url(entry) == CANONICAL_URL

    def test_returns_none_for_unparseable_entry(self) -> None:
        entry = {"link": "https://example.com/not-youtube"}
        assert extract_video_url(entry) is None

    def test_returns_none_for_empty_entry(self) -> None:
        assert extract_video_url({}) is None
        assert extract_video_url({"yt_videoid": "", "link": ""}) is None

    def test_returns_none_for_invalid_yt_videoid(self) -> None:
        # Not 11 chars and link is also invalid → None.
        entry = {"yt_videoid": "tooshort", "link": "https://example.com/x"}
        assert extract_video_url(entry) is None


class TestFeedEntryToMetadata:
    def test_maps_title_channel_and_publish_date(self) -> None:
        entry = {
            "yt_videoid": VIDEO_ID,
            "yt_channelid": "UCkyHDwRWMEluOEYmOGJ_2nw",
            "title": "Hello World Video",
            "author": "Test Channel",
            "published": "2024-01-15T12:00:00+00:00",
            "link": CANONICAL_URL,
        }

        metadata = feed_entry_to_metadata(entry)

        assert metadata.video_id == VIDEO_ID
        assert metadata.title == "Hello World Video"
        assert metadata.channel == "Test Channel"
        assert metadata.channel_id == "UCkyHDwRWMEluOEYmOGJ_2nw"
        assert metadata.publish_date is not None
        assert metadata.publish_date.tzinfo is not None
        assert metadata.publish_date.year == 2024
        assert metadata.publish_date.month == 1
        assert metadata.publish_date.day == 15
        assert metadata.duration_seconds is None

    def test_publish_date_is_tz_aware(self) -> None:
        entry = {
            "yt_videoid": VIDEO_ID,
            "title": "t",
            "author": "a",
            "published": "2024-01-15T12:00:00+00:00",
            "link": CANONICAL_URL,
        }

        metadata = feed_entry_to_metadata(entry)

        assert metadata.publish_date is not None
        assert metadata.publish_date.tzinfo is not None

    def test_missing_published_returns_none_publish_date(self) -> None:
        entry = {
            "yt_videoid": VIDEO_ID,
            "title": "t",
            "author": "a",
            "link": CANONICAL_URL,
        }

        metadata = feed_entry_to_metadata(entry)

        # NOT datetime.now — build_document already has the now-fallback.
        assert metadata.publish_date is None

    def test_invalid_published_returns_none_publish_date(self) -> None:
        entry = {
            "yt_videoid": VIDEO_ID,
            "title": "t",
            "author": "a",
            "published": "not-a-date",
            "link": CANONICAL_URL,
        }

        metadata = feed_entry_to_metadata(entry)

        assert metadata.publish_date is None

    def test_missing_title_and_author_keep_fields_none(self) -> None:
        entry = {
            "yt_videoid": VIDEO_ID,
            "link": CANONICAL_URL,
        }

        metadata = feed_entry_to_metadata(entry)

        assert metadata.title is None
        assert metadata.channel is None
        assert metadata.channel_id is None
        assert metadata.publish_date is None

    def test_falls_back_to_link_for_video_id(self) -> None:
        entry = {
            "title": "t",
            "author": "a",
            "link": CANONICAL_URL,
        }

        metadata = feed_entry_to_metadata(entry)

        assert metadata.video_id == VIDEO_ID

    def test_raises_when_no_resolvable_video_id(self) -> None:
        entry = {
            "title": "t",
            "author": "a",
            "link": "https://example.com/not-youtube",
        }

        with pytest.raises(ValueError, match="no resolvable video id"):
            feed_entry_to_metadata(entry)


class TestFetchFeed:
    def _mock_httpx(self, mocker, mock_response):
        mock_client = mocker.AsyncMock()
        mock_client.__aenter__.return_value = mock_client
        mock_client.get.return_value = mock_response
        mocker.patch(
            "tree.data.youtube.youtube.httpx.AsyncClient",
            return_value=mock_client,
        )
        return mock_client

    async def test_returns_parsed_entries(self, mocker) -> None:
        mock_response = mocker.Mock(text="<atom>mock</atom>")
        mock_client = self._mock_httpx(mocker, mock_response)

        entries = [{"yt_videoid": VIDEO_ID, "title": "Entry 1"}]
        mocker.patch(
            "tree.data.youtube.youtube.feedparser.parse",
            return_value=mocker.Mock(bozo=False, entries=entries),
        )

        result = await fetch_feed(
            "https://www.youtube.com/feeds/videos.xml?channel_id=UC..."
        )

        assert result == entries
        mock_client.get.assert_called_once_with(
            "https://www.youtube.com/feeds/videos.xml?channel_id=UC...",
            follow_redirects=True,
            timeout=30.0,
        )

    async def test_raises_on_http_error(self, mocker) -> None:
        mock_response = mocker.Mock()
        mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "Not Found",
            request=mocker.Mock(),
            response=mocker.Mock(status_code=404),
        )
        self._mock_httpx(mocker, mock_response)

        with pytest.raises(httpx.HTTPStatusError):
            await fetch_feed("https://www.youtube.com/feeds/videos.xml?channel_id=x")

    async def test_raises_on_malformed_feed_without_entries(self, mocker) -> None:
        mock_response = mocker.Mock(text="not xml")
        self._mock_httpx(mocker, mock_response)

        mocker.patch(
            "tree.data.youtube.youtube.feedparser.parse",
            return_value=mocker.Mock(
                bozo=True, entries=[], bozo_exception=Exception("bad")
            ),
        )

        with pytest.raises(ValueError, match="Failed to parse RSS feed"):
            await fetch_feed("https://www.youtube.com/feeds/videos.xml?channel_id=x")

    async def test_bozo_feed_with_entries_returns_entries(self, mocker) -> None:
        mock_response = mocker.Mock(text="<atom>partial</atom>")
        self._mock_httpx(mocker, mock_response)

        entries = [{"yt_videoid": VIDEO_ID, "title": "Recovered"}]
        mocker.patch(
            "tree.data.youtube.youtube.feedparser.parse",
            return_value=mocker.Mock(bozo=True, entries=entries),
        )

        result = await fetch_feed(
            "https://www.youtube.com/feeds/videos.xml?channel_id=x"
        )

        assert result == entries

    async def test_returns_empty_list_for_empty_feed(self, mocker) -> None:
        mock_response = mocker.Mock(text="<atom></atom>")
        self._mock_httpx(mocker, mock_response)

        mocker.patch(
            "tree.data.youtube.youtube.feedparser.parse",
            return_value=mocker.Mock(bozo=False, entries=[]),
        )

        result = await fetch_feed(
            "https://www.youtube.com/feeds/videos.xml?channel_id=x"
        )

        assert result == []
