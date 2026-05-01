import pytest

from tree.data.youtube.urls import (
    canonical_video_url,
    extract_channel_id_from_rss_url,
    extract_video_id,
    is_youtube_rss_url,
    is_youtube_video_url,
)

VIDEO_ID = "eYaWxljC4sA"


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


class TestIsYoutubeVideoUrl:
    @pytest.mark.parametrize(
        "url, expected",
        [
            (f"https://www.youtube.com/watch?v={VIDEO_ID}", True),
            (f"https://youtu.be/{VIDEO_ID}", True),
            (f"https://m.youtube.com/watch?v={VIDEO_ID}", True),
            (f"https://www.youtube.com/shorts/{VIDEO_ID}", True),
            (
                "https://www.youtube.com/feeds/videos.xml?channel_id=UCfoo",
                False,
            ),
            ("https://example.com/foo", False),
            ("", False),
        ],
    )
    def test_truth_table(self, url, expected):
        assert is_youtube_video_url(url) is expected


class TestIsYoutubeRssUrl:
    @pytest.mark.parametrize(
        "url, expected",
        [
            (
                "https://www.youtube.com/feeds/videos.xml?channel_id=UCfoo",
                True,
            ),
            ("https://www.youtube.com/feeds/videos.xml", False),
            (f"https://www.youtube.com/watch?v={VIDEO_ID}", False),
            ("https://example.com/feeds/videos.xml?channel_id=UCfoo", False),
            ("", False),
        ],
    )
    def test_truth_table(self, url, expected):
        assert is_youtube_rss_url(url) is expected


class TestCanonicalVideoUrl:
    def test_returns_canonical_form(self):
        assert (
            canonical_video_url(VIDEO_ID)
            == f"https://www.youtube.com/watch?v={VIDEO_ID}"
        )

    def test_round_trips_from_youtu_be(self):
        # Story: SWE pastes any YouTube URL form and gets the same canonical
        # document. Composing extract_video_id + canonical_video_url is the
        # contract used by Document.source_uri to dedupe URL variants.
        assert (
            canonical_video_url(extract_video_id(f"https://youtu.be/{VIDEO_ID}"))
            == f"https://www.youtube.com/watch?v={VIDEO_ID}"
        )

    def test_round_trips_from_watch_with_query(self):
        assert (
            canonical_video_url(
                extract_video_id(f"https://www.youtube.com/watch?v={VIDEO_ID}&t=42s")
            )
            == f"https://www.youtube.com/watch?v={VIDEO_ID}"
        )
