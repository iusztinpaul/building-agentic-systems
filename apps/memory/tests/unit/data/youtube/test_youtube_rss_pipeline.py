"""Unit tests for ``tree.data.youtube.youtube_rss_pipeline`` (#080).

The RSS path derives ``VideoMetadata`` from the Atom feed entry (no oEmbed), then runs
the SHARED bulk core (``youtube_ingest._bulk_build_and_load``): ONE ``fetch_many``
per feed → ``build_document`` per slot → ``load_video_document`` per slot. There are no
per-row tasks and no per-feed sub-flow runs. Two skips are preserved: an unresolvable
feed id WARNs + is dropped; a ``None``-transcript slot is dropped by the core with a
WARNING.

The transcript backend is constructed inside the shared core's
``fetch_transcripts_batch`` task, so these tests PATCH
``youtube_ingest.GeminiTranscriptFetcher`` with a fake rather than injecting a fetcher
(the flow no longer carries a ``fetcher`` arg).
"""

from __future__ import annotations

import logging

import tree.data.youtube.youtube_ingest as youtube_ingest
import tree.data.youtube.youtube_rss_pipeline as rss_pipeline
from beanie import PydanticObjectId

from tree.data.youtube.types import (
    FetchedTranscript,
    TranscriptSegment,
    VideoMetadata,
)
from tree.data.youtube.youtube_rss_pipeline import (
    _resolve_feed_items,
    fetch_feed_task,
    ingest_youtube_rss_feed_batch,
)
from tree.entities.documents import Document, SourceType

VIDEO_IDS = ["eYaWxljC4sA", "AAAaaaBBBcc", "ZZZzzzYYYxx"]
FEED_URL = (
    "https://www.youtube.com/feeds/videos.xml?channel_id=UCkyHDwRWMEluOEYmOGJ_2nw"
)


def _canonical(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def _feed_entry(video_id: str, index: int) -> dict:
    return {
        "yt_videoid": video_id,
        "yt_channelid": "UCkyHDwRWMEluOEYmOGJ_2nw",
        "title": f"Test Video {index}",
        "author": "Test Channel",
        "published": "2024-01-15T12:00:00+00:00",
        "link": _canonical(video_id),
    }


def _make_transcript(video_id: str) -> FetchedTranscript:
    return FetchedTranscript(
        metadata=VideoMetadata(video_id=video_id),
        segments=[
            TranscriptSegment(text="words", start_seconds=0.0, duration_seconds=1.0)
        ],
        language="en",
        plain_text="words",
    )


def _make_doc(video_id: str) -> Document:
    return Document(
        source_type=SourceType.YOUTUBE,
        source_uri=_canonical(video_id),
        user_id=PydanticObjectId(),
        title=f"Video {video_id}",
        summary="Summary",
        content="Body",
        authors=["Test Channel"],
    )


class _FakeFetcher:
    """Programmable stand-in for ``GeminiTranscriptFetcher``.

    Same ``async def fetch_many(urls) -> list[FetchedTranscript | None]`` contract,
    swapped in by patching ``youtube_ingest.GeminiTranscriptFetcher`` to return it.
    """

    def __init__(self, results_per_id: dict[str, FetchedTranscript | None]) -> None:
        self._results_per_id = results_per_id
        self.calls: list[list[str]] = []

    async def fetch_many(
        self, video_urls_or_ids: list[str]
    ) -> list[FetchedTranscript | None]:
        self.calls.append(list(video_urls_or_ids))
        results: list[FetchedTranscript | None] = []
        for url in video_urls_or_ids:
            match: FetchedTranscript | None = None
            for vid, payload in self._results_per_id.items():
                if vid in url:
                    match = payload
                    break
            results.append(match)
        return results


def _patch_fetcher(mocker, fake: _FakeFetcher) -> None:
    """Patch the construction point so ``GeminiTranscriptFetcher()`` yields ``fake``.

    Patching the class (not an injected arg) sidesteps the ``GOOGLE_API_KEY`` guard in
    ``GeminiTranscriptFetcher.__init__`` — no key/network needed.
    """

    mocker.patch.object(youtube_ingest, "GeminiTranscriptFetcher", return_value=fake)


class TestTaskAndFlowMetadata:
    """Retry grain on ``fetch_feed_task``; per-row load task removed."""

    def test_fetch_feed_task_retries(self) -> None:
        assert fetch_feed_task.retries == 2
        assert fetch_feed_task.retry_delay_seconds == 5
        assert fetch_feed_task.name == "fetch-youtube-rss-feed"

    def test_batch_flow_name(self) -> None:
        assert ingest_youtube_rss_feed_batch.name == "ingest-youtube-rss-feed-batch-etl"

    def test_per_row_load_task_is_gone(self) -> None:
        assert not hasattr(rss_pipeline, "load_video_task")

    def test_non_batch_feed_flow_is_gone(self) -> None:
        # The per-feed sub-flow collapsed into ``_ingest_one_feed`` (a plain core).
        assert not hasattr(rss_pipeline, "ingest_youtube_rss_feed")

    def test_batch_flow_signature_has_no_fetcher(self) -> None:
        import inspect

        params = list(inspect.signature(ingest_youtube_rss_feed_batch).parameters)
        assert params == ["feed_urls", "user_id"]


class TestResolveFeedItems:
    """Feed metadata source; unresolvable ids WARN + drop."""

    def test_maps_entries_to_feed_metadata(self) -> None:
        entries = [_feed_entry(vid, i) for i, vid in enumerate(VIDEO_IDS)]

        items = _resolve_feed_items(entries)

        assert [url for url, _ in items] == [_canonical(vid) for vid in VIDEO_IDS]
        # Metadata comes from the feed: title + channel propagate.
        url0, meta0 = items[0]
        assert meta0.title == "Test Video 0"
        assert meta0.channel == "Test Channel"
        assert meta0.publish_date is not None

    def test_unresolvable_entry_warns_and_is_dropped(self, caplog) -> None:
        good = _feed_entry(VIDEO_IDS[0], 0)
        bad = {"title": "Bad", "author": "C", "link": "https://example.com/x"}

        with caplog.at_level(logging.WARNING, logger=rss_pipeline.logger.name):
            items = _resolve_feed_items([good, bad, _feed_entry(VIDEO_IDS[2], 2)])

        assert [url for url, _ in items] == [
            _canonical(VIDEO_IDS[0]),
            _canonical(VIDEO_IDS[2]),
        ]
        warnings = [
            r
            for r in caplog.records
            if "no resolvable video id" in r.getMessage()
            and r.levelno == logging.WARNING
        ]
        assert len(warnings) == 1


class TestIngestYoutubeRssFeedBatch:
    """ONE bulk fetch per feed with feed metadata; no oEmbed."""

    async def test_one_bulk_fetch_per_feed_with_feed_metadata(self, mocker) -> None:
        mocker.patch.object(rss_pipeline, "init_mongodb", mocker.AsyncMock())
        mocker.patch.object(
            rss_pipeline,
            "fetch_feed",
            mocker.AsyncMock(
                return_value=[_feed_entry(vid, i) for i, vid in enumerate(VIDEO_IDS)]
            ),
        )
        # Fail loudly if the RSS path ever reaches into the oEmbed metadata fetch.
        oembed_spy = mocker.patch(
            "tree.data.youtube.youtube_video.fetch_oembed_metadata",
            side_effect=AssertionError("oEmbed must NOT be called for RSS ingest"),
        )
        mocker.patch.object(
            youtube_ingest,
            "load_video_document",
            mocker.AsyncMock(side_effect=lambda doc: doc),
        )
        fake = _FakeFetcher({vid: _make_transcript(vid) for vid in VIDEO_IDS})
        _patch_fetcher(mocker, fake)

        result = await ingest_youtube_rss_feed_batch.fn([FEED_URL], PydanticObjectId())

        # ONE bulk fetch_many over all 3 feed URLs; no oEmbed call.
        assert len(fake.calls) == 1
        assert len(fake.calls[0]) == 3
        oembed_spy.assert_not_called()
        # 3 Documents persist, carrying feed-side titles/authors.
        assert len(result) == 3
        assert all(d.title.startswith("Test Video") for d in result)
        assert all(d.authors == ["Test Channel"] for d in result)

    async def test_missing_transcript_slot_isolated(self, mocker) -> None:
        mocker.patch.object(rss_pipeline, "init_mongodb", mocker.AsyncMock())
        mocker.patch.object(
            rss_pipeline,
            "fetch_feed",
            mocker.AsyncMock(
                return_value=[_feed_entry(vid, i) for i, vid in enumerate(VIDEO_IDS)]
            ),
        )
        mocker.patch.object(
            youtube_ingest,
            "load_video_document",
            mocker.AsyncMock(side_effect=lambda doc: doc),
        )
        # Middle slot has no transcript (fetch returns None).
        results = {vid: _make_transcript(vid) for vid in VIDEO_IDS}
        results[VIDEO_IDS[1]] = None
        fake = _FakeFetcher(results)
        _patch_fetcher(mocker, fake)

        result = await ingest_youtube_rss_feed_batch.fn([FEED_URL], PydanticObjectId())

        # Still ONE bulk fetch; the missing slot dropped, the other 2 persist.
        assert len(fake.calls) == 1
        assert len(result) == 2
        assert _canonical(VIDEO_IDS[1]) not in {d.source_uri for d in result}

    async def test_inits_mongo_once_for_multiple_feeds(self, mocker) -> None:
        init_spy = mocker.patch.object(rss_pipeline, "init_mongodb", mocker.AsyncMock())
        mocker.patch.object(
            rss_pipeline,
            "fetch_feed",
            mocker.AsyncMock(return_value=[_feed_entry(VIDEO_IDS[0], 0)]),
        )
        mocker.patch.object(
            youtube_ingest,
            "load_video_document",
            mocker.AsyncMock(side_effect=lambda doc: doc),
        )
        fake = _FakeFetcher({VIDEO_IDS[0]: _make_transcript(VIDEO_IDS[0])})
        _patch_fetcher(mocker, fake)

        await ingest_youtube_rss_feed_batch.fn(
            [FEED_URL, FEED_URL + "&x=2"], PydanticObjectId()
        )

        # Mongo initialised once at the flow boundary; one fetch_many PER feed.
        assert init_spy.await_count == 1
        assert len(fake.calls) == 2

    async def test_one_bad_feed_does_not_sink_the_others(self, mocker) -> None:
        mocker.patch.object(rss_pipeline, "init_mongodb", mocker.AsyncMock())

        async def _fetch(feed_url: str) -> list[dict]:
            if "bad" in feed_url:
                raise RuntimeError("feed fetch failed")
            return [_feed_entry(VIDEO_IDS[0], 0)]

        mocker.patch.object(
            rss_pipeline, "fetch_feed", mocker.AsyncMock(side_effect=_fetch)
        )
        mocker.patch.object(
            youtube_ingest,
            "load_video_document",
            mocker.AsyncMock(side_effect=lambda doc: doc),
        )
        fake = _FakeFetcher({VIDEO_IDS[0]: _make_transcript(VIDEO_IDS[0])})
        _patch_fetcher(mocker, fake)

        result = await ingest_youtube_rss_feed_batch.fn(
            [FEED_URL, "https://www.youtube.com/feeds/videos.xml?channel_id=bad"],
            PydanticObjectId(),
        )

        # The good feed still ingests its one doc despite the bad feed raising.
        assert len(result) == 1
        assert result[0].source_uri == _canonical(VIDEO_IDS[0])
