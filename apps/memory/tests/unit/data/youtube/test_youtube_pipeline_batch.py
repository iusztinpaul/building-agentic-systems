"""Unit tests for the unified YouTube pipeline (``ingest_youtube_batch``).

The unified flow FLATTENS a shard's RSS feeds (expanded to per-video items) and single
videos into one ``[(canonical_url, VideoMetadata)]`` list, then runs the SHARED bulk
core ONCE — so there is exactly ONE ``fetch_many`` over feeds + loose videos combined.
Resolve (feed-expand vs oEmbed) and the bulk core are mocked here; their internals are
covered by ``test_youtube_rss.py`` / ``test_youtube_video.py`` / ``test_youtube_ingest.py``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from beanie import PydanticObjectId

import tree.data.youtube.youtube_ingest as youtube_ingest
import tree.data.youtube.youtube_pipeline_batch as yt
from tree.config.app_config import YouTubeRssSource, YouTubeVideoSource
from tree.data.youtube.types import (
    FetchedTranscript,
    TranscriptSegment,
    VideoMetadata,
)

_USER_ID = PydanticObjectId("507f1f77bcf86cd799439011")


def _meta(video_id: str) -> VideoMetadata:
    return VideoMetadata(video_id=video_id)


def _canonical(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


@pytest.fixture(autouse=True)
def _stub_mongo(mocker) -> None:
    mocker.patch(
        "tree.data.youtube.youtube_pipeline_batch.init_mongodb", new_callable=AsyncMock
    )


def _patch_bulk(mocker) -> AsyncMock:
    return mocker.patch.object(
        yt, "_bulk_build_and_load", new_callable=AsyncMock, return_value=["doc"]
    )


async def test_flattens_feeds_and_videos_into_one_bulk_call(mocker) -> None:
    # Two feeds (2 + 1 items) + two single videos → ONE _bulk_build_and_load over the
    # combined 5 items (the single-fetch_many win).
    feed_items = {
        "feed://A": [
            ("https://yt/watch?v=a1", _meta("a1")),
            ("https://yt/watch?v=a2", _meta("a2")),
        ],
        "feed://B": [("https://yt/watch?v=b1", _meta("b1"))],
    }

    async def fake_resolve_feed(feed_url):
        return feed_items[feed_url]

    async def fake_resolve_video(url):
        return (url, _meta(url.rsplit("=", 1)[-1]))

    mocker.patch.object(
        yt, "_resolve_feed", new_callable=AsyncMock, side_effect=fake_resolve_feed
    )
    mocker.patch.object(
        yt,
        "_resolve_video_item",
        new_callable=AsyncMock,
        side_effect=fake_resolve_video,
    )
    bulk = _patch_bulk(mocker)

    entries = [
        YouTubeRssSource(uri="feed://A"),
        YouTubeRssSource(uri="feed://B"),
        YouTubeVideoSource(uri="https://yt/watch?v=v1"),
        YouTubeVideoSource(uri="https://yt/watch?v=v2"),
    ]
    result = await yt.ingest_youtube_batch.fn(entries, _USER_ID)

    bulk.assert_awaited_once()
    items, user_id = bulk.await_args.args
    assert user_id == _USER_ID
    assert len(items) == 5  # 3 from feeds + 2 single videos, in ONE call
    assert result == ["doc"]


async def test_isolates_a_failing_feed(mocker) -> None:
    # One feed raises during resolve → skipped; the other feed's items still flow.
    async def fake_resolve_feed(feed_url):
        if feed_url == "feed://bad":
            raise RuntimeError("feed fetch failed")
        return [("https://yt/watch?v=ok", _meta("ok"))]

    mocker.patch.object(
        yt, "_resolve_feed", new_callable=AsyncMock, side_effect=fake_resolve_feed
    )
    bulk = _patch_bulk(mocker)

    entries = [YouTubeRssSource(uri="feed://bad"), YouTubeRssSource(uri="feed://good")]
    await yt.ingest_youtube_batch.fn(entries, _USER_ID)

    items, _ = bulk.await_args.args
    assert [m.video_id for _u, m in items] == ["ok"]


async def test_no_resolvable_items_skips_bulk(mocker) -> None:
    mocker.patch.object(yt, "_resolve_feed", new_callable=AsyncMock, return_value=[])
    bulk = _patch_bulk(mocker)

    result = await yt.ingest_youtube_batch.fn(
        [YouTubeRssSource(uri="feed://A")], _USER_ID
    )

    assert result == []
    bulk.assert_not_awaited()


# --- Headline: ONE fetch_many over feeds + loose videos (the unification win) ---


class _FakeFetcher:
    """Programmable stand-in for ``GeminiTranscriptFetcher`` recording its calls."""

    def __init__(self, transcripts: dict[str, FetchedTranscript | None]) -> None:
        self.transcripts = transcripts
        self.calls: list[list[str]] = []

    async def fetch_many(self, urls: list[str]) -> list[FetchedTranscript | None]:
        self.calls.append(list(urls))
        out: list[FetchedTranscript | None] = []
        for url in urls:
            match: FetchedTranscript | None = None
            for vid, payload in self.transcripts.items():
                if vid in url:
                    match = payload
                    break
            out.append(match)
        return out


def _transcript(video_id: str) -> FetchedTranscript:
    return FetchedTranscript(
        metadata=_meta(video_id),
        segments=[TranscriptSegment(text="w", start_seconds=0.0, duration_seconds=1.0)],
        language="en",
        plain_text="w",
    )


async def test_one_fetch_many_over_feeds_and_loose_videos(mocker) -> None:
    # The whole point of the unification: a worker with channel feeds + loose videos
    # issues EXACTLY ONE fetch_many over the COMBINED url list (was one-per-feed + one
    # for the loose videos). Uses the REAL _bulk_build_and_load with a fake fetcher.
    feed_vids = ["aaaaaaaaaaa", "bbbbbbbbbbb"]
    loose_vid = "ccccccccccc"

    async def fake_resolve_feed(feed_url):
        return [(_canonical(v), _meta(v)) for v in feed_vids]

    async def fake_resolve_video(url):
        return (url, _meta(url.rsplit("=", 1)[-1]))

    mocker.patch.object(
        yt, "_resolve_feed", new_callable=AsyncMock, side_effect=fake_resolve_feed
    )
    mocker.patch.object(
        yt,
        "_resolve_video_item",
        new_callable=AsyncMock,
        side_effect=fake_resolve_video,
    )
    mocker.patch.object(
        youtube_ingest,
        "load_video_document",
        new_callable=AsyncMock,
        side_effect=lambda d: d,
    )
    fake = _FakeFetcher({v: _transcript(v) for v in [*feed_vids, loose_vid]})
    mocker.patch.object(youtube_ingest, "GeminiTranscriptFetcher", return_value=fake)

    entries = [
        YouTubeRssSource(uri="feed://A"),
        YouTubeVideoSource(uri=_canonical(loose_vid)),
    ]
    result = await yt.ingest_youtube_batch.fn(entries, _USER_ID)

    assert len(fake.calls) == 1  # ONE fetch_many, not per-feed + per-loose-batch
    assert len(fake.calls[0]) == 3  # 2 feed videos + 1 loose video, combined
    assert len(result) == 3
