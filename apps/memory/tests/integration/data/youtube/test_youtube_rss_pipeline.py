"""Integration tests for the YouTube RSS-feed ETL.

Persists real `Document`s against the local MongoDB fixture (`mongo_client`
from `tests/integration/conftest.py`). Mocks:

- `httpx.AsyncClient` and `feedparser.parse` for the feed fetch.
- The `TranscriptFetcher` (no `youtube-transcript-api`, no Gemini calls) —
  injected via the flow's `fetcher=` kwarg.

Mirrors patterns from
`tests/integration/data/substack/test_substack_rss_pipeline.py` and
`tests/integration/data/youtube/test_youtube_video_pipeline.py`.
"""

from __future__ import annotations

import logging

import pytest
from beanie import PydanticObjectId
from prefect import tags as prefect_tags

from tree.data.youtube.types import (
    FetchedTranscript,
    TranscriptSegment,
    VideoMetadata,
)
from tree.data.youtube.youtube_rss_pipeline import (
    ingest_youtube_rss_feed,
    ingest_youtube_rss_feed_batch,
)
from tree.entities.documents import Document, SourceType

PIPELINE_LOGGER = "tree.data.youtube.youtube_rss_pipeline"

FEED_URL = (
    "https://www.youtube.com/feeds/videos.xml?channel_id=UCkyHDwRWMEluOEYmOGJ_2nw"
)
FEED_URL_B = "https://www.youtube.com/feeds/videos.xml?channel_id=UC_other_channel"

VIDEO_IDS = ["eYaWxljC4sA", "AAAaaaBBBcc", "ZZZzzzYYYxx"]

FAKE_FEED_ENTRIES = [
    {
        "yt_videoid": vid,
        "yt_channelid": "UCkyHDwRWMEluOEYmOGJ_2nw",
        "title": f"Test Video {i}",
        "author": "Test Channel",
        "published": "2024-01-15T12:00:00+00:00",
        "link": f"https://www.youtube.com/watch?v={vid}",
    }
    for i, vid in enumerate(VIDEO_IDS)
]


def _make_transcript(
    *, video_id: str, plain_text: str = "spoken words"
) -> FetchedTranscript:
    return FetchedTranscript(
        metadata=VideoMetadata(video_id=video_id),
        segments=[
            TranscriptSegment(text=plain_text, start_seconds=0.0, duration_seconds=1.0)
        ],
        language="en",
        plain_text=plain_text,
    )


class _FakeFetcher:
    """Programmable `TranscriptFetcher` for integration tests.

    Returns one element per input slot, mirroring the real chain contract.
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
            # Simple substring match — feed URLs always contain the video id.
            match: FetchedTranscript | None = None
            for vid, payload in self._results_per_id.items():
                if vid in url:
                    match = payload
                    break
            results.append(match)
        return results


def _patch_feed(mocker, entries: list[dict]) -> object:
    """Patch the feed fetch path. Returns the mock httpx client for assertions."""

    mock_response = mocker.Mock()
    mock_response.status_code = 200
    mock_response.text = "<atom>mock</atom>"
    mock_response.raise_for_status = mocker.Mock()

    mock_client = mocker.AsyncMock()
    mock_client.get.return_value = mock_response
    mock_client.__aenter__ = mocker.AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = mocker.AsyncMock(return_value=False)

    mocker.patch(
        "tree.data.youtube.youtube_rss.httpx.AsyncClient",
        return_value=mock_client,
    )

    class _FakeFeed:
        bozo = False
        bozo_exception = None

    feed = _FakeFeed()
    feed.entries = entries  # type: ignore[attr-defined]
    mocker.patch(
        "tree.data.youtube.youtube_rss.feedparser.parse",
        return_value=feed,
    )

    return mock_client


class TestIngestYoutubeRssFeedFlow:
    async def test_ingests_videos_via_prefect_flow(
        self, mongo_client, mocker, caplog
    ) -> None:
        # Fail loudly if the pipeline ever tries to call the youtube_video
        # oEmbed path — the RSS pipeline must use feed-side metadata.
        oembed_spy = mocker.patch(
            "tree.data.youtube.youtube_video.httpx.AsyncClient",
            side_effect=AssertionError("oEmbed must NOT be called for RSS ingest"),
        )

        feed_client = _patch_feed(mocker, FAKE_FEED_ENTRIES)

        fake = _FakeFetcher({vid: _make_transcript(video_id=vid) for vid in VIDEO_IDS})

        caplog.set_level(logging.WARNING, logger=PIPELINE_LOGGER)

        with prefect_tags("tests"):
            result = await ingest_youtube_rss_feed(
                FEED_URL, PydanticObjectId(), fetcher=fake
            )

        assert len(result) == 3
        for doc in result:
            assert doc.source_type == SourceType.YOUTUBE
            assert doc.id is not None
            assert doc.title.startswith("Test Video")
            # Channel propagated from feed-side `author`.
            assert doc.authors == ["Test Channel"]

        # Bulk fetch — exactly one call to `fetch_many` with all 3 URLs.
        assert len(fake.calls) == 1
        assert len(fake.calls[0]) == 3

        # Feed fetched once. oEmbed never called.
        assert feed_client.get.call_count == 1
        oembed_spy.assert_not_called()

        # No pipeline-layer WARNINGs in the happy path.
        pipeline_warnings = [
            r
            for r in caplog.records
            if r.name == PIPELINE_LOGGER and r.levelno >= logging.WARNING
        ]
        assert pipeline_warnings == []

        db_docs = await Document.find(
            Document.source_type == SourceType.YOUTUBE
        ).to_list()
        assert len(db_docs) == 3

    async def test_idempotent_on_rerun(self, mongo_client, mocker) -> None:
        mocker.patch(
            "tree.data.youtube.youtube_video.httpx.AsyncClient",
            side_effect=AssertionError("oEmbed must NOT be called for RSS ingest"),
        )

        _patch_feed(mocker, FAKE_FEED_ENTRIES)

        fake = _FakeFetcher({vid: _make_transcript(video_id=vid) for vid in VIDEO_IDS})

        user_id = PydanticObjectId()
        with prefect_tags("tests"):
            first = await ingest_youtube_rss_feed(FEED_URL, user_id, fetcher=fake)
        assert len(first) == 3

        _patch_feed(mocker, FAKE_FEED_ENTRIES)

        with prefect_tags("tests"):
            second = await ingest_youtube_rss_feed(FEED_URL, user_id, fetcher=fake)
        assert len(second) == 0

        db_docs = await Document.find(
            Document.source_type == SourceType.YOUTUBE
        ).to_list()
        assert len(db_docs) == 3

    @pytest.mark.slow
    async def test_upgrades_latent_document(self, mongo_client, mocker) -> None:
        user_id = PydanticObjectId()
        canonical = f"https://www.youtube.com/watch?v={VIDEO_IDS[0]}"
        latent = Document(
            source_type=SourceType.LATENT,
            source_uri=canonical,
            user_id=user_id,
        )
        await latent.insert()

        mocker.patch(
            "tree.data.youtube.youtube_video.httpx.AsyncClient",
            side_effect=AssertionError("oEmbed must NOT be called for RSS ingest"),
        )

        _patch_feed(mocker, [FAKE_FEED_ENTRIES[0]])
        fake = _FakeFetcher({VIDEO_IDS[0]: _make_transcript(video_id=VIDEO_IDS[0])})

        with prefect_tags("tests"):
            result = await ingest_youtube_rss_feed(FEED_URL, user_id, fetcher=fake)

        assert len(result) == 1
        assert result[0].id == latent.id
        assert result[0].source_type == SourceType.YOUTUBE
        assert result[0].title == "Test Video 0"

        rows = await Document.find(Document.source_uri == canonical).to_list()
        assert len(rows) == 1
        assert rows[0].source_type == SourceType.YOUTUBE

    async def test_chain_exhausted_slot_skips_silently(
        self, mongo_client, mocker, caplog
    ) -> None:
        """Middle slot returns None (chain exhausted). 2 docs persist; the
        pipeline emits NO WARNING of its own — the chain owns that warning."""

        mocker.patch(
            "tree.data.youtube.youtube_video.httpx.AsyncClient",
            side_effect=AssertionError("oEmbed must NOT be called for RSS ingest"),
        )

        _patch_feed(mocker, FAKE_FEED_ENTRIES)

        fake = _FakeFetcher(
            {
                VIDEO_IDS[0]: _make_transcript(video_id=VIDEO_IDS[0]),
                VIDEO_IDS[1]: None,  # chain exhausted on slot 2
                VIDEO_IDS[2]: _make_transcript(video_id=VIDEO_IDS[2]),
            }
        )

        caplog.set_level(logging.WARNING, logger=PIPELINE_LOGGER)

        with prefect_tags("tests"):
            result = await ingest_youtube_rss_feed(
                FEED_URL, PydanticObjectId(), fetcher=fake
            )

        assert len(result) == 2
        persisted_ids = sorted(doc.source_uri for doc in result)
        assert persisted_ids == sorted(
            [
                f"https://www.youtube.com/watch?v={VIDEO_IDS[0]}",
                f"https://www.youtube.com/watch?v={VIDEO_IDS[2]}",
            ]
        )

        # Spec: pipeline-layer logger emits NO WARNING for the missing slot.
        pipeline_warnings = [
            r
            for r in caplog.records
            if r.name == PIPELINE_LOGGER and r.levelno >= logging.WARNING
        ]
        assert pipeline_warnings == []

    async def test_unresolvable_entry_is_skipped_with_warning(
        self, mongo_client, mocker, caplog
    ) -> None:
        """An Atom entry with no resolvable video id is skipped with a
        pipeline-layer WARNING, while remaining entries still ingest."""

        mocker.patch(
            "tree.data.youtube.youtube_video.httpx.AsyncClient",
            side_effect=AssertionError("oEmbed must NOT be called for RSS ingest"),
        )

        bad_entry = {
            "title": "Bad Entry",
            "author": "Test Channel",
            "published": "2024-01-15T12:00:00+00:00",
            "link": "https://example.com/not-youtube",
        }
        entries = [
            FAKE_FEED_ENTRIES[0],
            bad_entry,
            FAKE_FEED_ENTRIES[2],
        ]
        _patch_feed(mocker, entries)

        fake = _FakeFetcher(
            {
                VIDEO_IDS[0]: _make_transcript(video_id=VIDEO_IDS[0]),
                VIDEO_IDS[2]: _make_transcript(video_id=VIDEO_IDS[2]),
            }
        )

        caplog.set_level(logging.WARNING, logger=PIPELINE_LOGGER)

        with prefect_tags("tests"):
            result = await ingest_youtube_rss_feed(
                FEED_URL, PydanticObjectId(), fetcher=fake
            )

        assert len(result) == 2

        skip_warnings = [
            r
            for r in caplog.records
            if r.name == PIPELINE_LOGGER
            and r.levelno == logging.WARNING
            and "no resolvable video id" in r.getMessage()
        ]
        assert len(skip_warnings) == 1

        # The bulk fetcher was called with exactly the 2 valid URLs.
        assert len(fake.calls) == 1
        assert len(fake.calls[0]) == 2

    async def test_uses_feed_metadata_no_oembed_call(
        self, mongo_client, mocker
    ) -> None:
        """Acceptance criterion: title/channel/publish_date come from the
        Atom entry; the oEmbed endpoint is never hit."""

        oembed_spy = mocker.patch(
            "tree.data.youtube.youtube_video.httpx.AsyncClient",
            side_effect=AssertionError("oEmbed must NOT be called for RSS ingest"),
        )

        feed_client = _patch_feed(mocker, [FAKE_FEED_ENTRIES[0]])

        fake = _FakeFetcher({VIDEO_IDS[0]: _make_transcript(video_id=VIDEO_IDS[0])})

        with prefect_tags("tests"):
            result = await ingest_youtube_rss_feed(
                FEED_URL, PydanticObjectId(), fetcher=fake
            )

        assert len(result) == 1
        doc = result[0]
        assert doc.title == "Test Video 0"
        assert doc.authors == ["Test Channel"]
        assert doc.date is not None
        assert doc.date.year == 2024
        assert doc.date.month == 1
        assert doc.date.day == 15

        # Exactly one HTTP call total: the feed itself. Zero oEmbed calls.
        assert feed_client.get.call_count == 1
        oembed_spy.assert_not_called()


class TestIngestYoutubeRssFeedBatchFlow:
    async def test_batch_combines_results_and_inits_mongo_once(
        self, mongo_client, mocker
    ) -> None:
        mocker.patch(
            "tree.data.youtube.youtube_video.httpx.AsyncClient",
            side_effect=AssertionError("oEmbed must NOT be called for RSS ingest"),
        )

        _patch_feed(mocker, FAKE_FEED_ENTRIES[:1])

        init_mongo_spy = mocker.patch(
            "tree.data.youtube.youtube_rss_pipeline.init_mongodb",
            return_value=mongo_client,
        )

        fake = _FakeFetcher({VIDEO_IDS[0]: _make_transcript(video_id=VIDEO_IDS[0])})

        with prefect_tags("tests"):
            result = await ingest_youtube_rss_feed_batch(
                feed_urls=[FEED_URL, FEED_URL_B],
                user_id=PydanticObjectId(),
                fetcher=fake,
            )

        # Both feeds return the same single entry → second is a duplicate.
        assert len(result) == 1
        assert init_mongo_spy.call_count == 1


@pytest.fixture(autouse=True)
def _silence_prefect_log(caplog):
    # Prefect adds info-level chatter at flow boundaries; tests only assert
    # on WARNING-level records on our pipeline logger.
    caplog.set_level(logging.INFO, logger=PIPELINE_LOGGER)
    yield
