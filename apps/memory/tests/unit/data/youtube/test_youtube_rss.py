"""Unit tests for the pure logic in `tree.data.youtube.youtube_rss`.

Mirrors `tests/unit/data/substack/test_substack.py`. Covers:
- `extract_video_url` resolution order (yt_videoid → link fallback → None).
- `feed_entry_to_metadata` mapping and tz-awareness invariants.
- `fetch_feed` HTTP / bozo / empty-feed behaviour (mocked httpx + feedparser).
"""

from __future__ import annotations

import httpx
import pytest

from tree.data.youtube.youtube_rss import (
    extract_video_url,
    feed_entry_to_metadata,
    fetch_feed,
)

VIDEO_ID = "eYaWxljC4sA"
CANONICAL_URL = f"https://www.youtube.com/watch?v={VIDEO_ID}"


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
            "tree.data.youtube.youtube_rss.httpx.AsyncClient",
            return_value=mock_client,
        )
        return mock_client

    async def test_returns_parsed_entries(self, mocker) -> None:
        mock_response = mocker.Mock(text="<atom>mock</atom>")
        mock_client = self._mock_httpx(mocker, mock_response)

        entries = [{"yt_videoid": VIDEO_ID, "title": "Entry 1"}]
        mocker.patch(
            "tree.data.youtube.youtube_rss.feedparser.parse",
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
            "tree.data.youtube.youtube_rss.feedparser.parse",
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
            "tree.data.youtube.youtube_rss.feedparser.parse",
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
            "tree.data.youtube.youtube_rss.feedparser.parse",
            return_value=mocker.Mock(bozo=False, entries=[]),
        )

        result = await fetch_feed(
            "https://www.youtube.com/feeds/videos.xml?channel_id=x"
        )

        assert result == []
