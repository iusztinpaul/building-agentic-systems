"""Unit tests for the unified YouTube pipeline (``ingest_youtube_batch``).

The unified flow FLATTENS a shard's RSS feeds (expanded to per-video items) and single
videos into one ``[(canonical_url, VideoMetadata)]`` list, then runs the SHARED bulk
core once per ``app_config.data.batch_size`` chunk — one ``fetch_many`` per chunk over
feeds + loose videos combined. Resolve (feed-expand vs oEmbed) and the bulk core are
mocked here; their internals are covered by ``test_youtube.py`` /
``test_youtube_pipeline_bulk.py``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from beanie import PydanticObjectId
from pydantic import SecretStr

import tree.data.youtube.youtube_pipeline as youtube_pipeline
import tree.data.youtube.youtube_pipeline_batch as yt
from tree.config.app_config import DataConfig, app_config
from tree.config.sources import (
    YouTubeRssSource,
    YouTubeVideoSource,
)
from tree.config.settings import settings
from tree.data.batch import track_item_failures
from tree.data.web.web_scraper_api import BrightDataTimeoutError
from tree.data.youtube.types import (
    FetchedTranscript,
    TranscriptSegment,
    VideoMetadata,
)
from tree.data.youtube.youtube import INVALID_URL_ERROR
from tree.entities.documents import Document

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
        yt, "_batch_build_and_load", new_callable=AsyncMock, return_value=["doc"]
    )


async def test_flattens_feeds_and_videos_into_one_bulk_call(mocker) -> None:
    # Two feeds (2 + 1 items) + two single videos fit one default-size chunk → ONE
    # _batch_build_and_load over the combined 5 items.
    feed_items = {
        "feed://A": [
            (_canonical("aaaaaaaaaa1"), _meta("aaaaaaaaaa1")),
            (_canonical("aaaaaaaaaa2"), _meta("aaaaaaaaaa2")),
        ],
        "feed://B": [(_canonical("bbbbbbbbbb1"), _meta("bbbbbbbbbb1"))],
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
        YouTubeVideoSource(uri=_canonical("vvvvvvvvvv1")),
        YouTubeVideoSource(uri=_canonical("vvvvvvvvvv2")),
    ]
    result = await yt.ingest_youtube_batch.fn(entries, _USER_ID)

    bulk.assert_awaited_once()
    items, user_id = bulk.await_args.args
    assert user_id == _USER_ID
    assert len(items) == 5  # 3 from feeds + 2 single videos, in one chunk
    assert result == ["doc"]


async def test_unresolvable_loose_video_is_passed_as_an_invalid_input(mocker) -> None:
    # ADR-004 §6: a loose video whose id cannot be resolved never reaches the
    # oEmbed resolve — it flows to the core as a RAW input for an
    # ``invalid_url`` ingest_error row.
    resolve = mocker.patch.object(
        yt, "_resolve_video_item", new_callable=AsyncMock, return_value=None
    )
    bulk = _patch_bulk(mocker)

    await yt.ingest_youtube_batch.fn(
        [YouTubeVideoSource(uri="https://example.com/not-youtube")], _USER_ID
    )

    resolve.assert_not_awaited()
    items, _ = bulk.await_args.args
    assert items == []
    assert bulk.await_args.kwargs["invalid_inputs"] == [
        "https://example.com/not-youtube"
    ]


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


# --- Headline: feeds + loose videos share each chunk's fetch_many ---


class _FakeFetcher:
    """Programmable stand-in for either transcript fetcher, recording its calls."""

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
    # that fit one chunk issues EXACTLY ONE fetch_many over the COMBINED url list (was
    # one-per-feed + one for the loose videos). Uses the REAL _batch_build_and_load
    # with a fake PRIMARY (Bright Data) fetcher — no live call, Gemini never needed.
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
        youtube_pipeline,
        "load_video_document",
        new_callable=AsyncMock,
        side_effect=lambda d: d,
    )
    mocker.patch.object(settings, "brightdata_api_key", SecretStr("bd-key"))
    mocker.patch.object(settings, "google_api_key", SecretStr("gemini-key"))
    fake = _FakeFetcher({v: _transcript(v) for v in [*feed_vids, loose_vid]})
    mocker.patch.object(
        youtube_pipeline, "BrightDataTranscriptFetcher", return_value=fake
    )
    gemini_ctor = mocker.patch.object(youtube_pipeline, "GeminiTranscriptFetcher")

    entries = [
        YouTubeRssSource(uri="feed://A"),
        YouTubeVideoSource(uri=_canonical(loose_vid)),
    ]
    result = await yt.ingest_youtube_batch.fn(entries, _USER_ID)

    assert len(fake.calls) == 1  # ONE fetch_many, not per-feed + per-loose-batch
    assert len(fake.calls[0]) == 3  # 2 feed videos + 1 loose video, combined
    assert len(result) == 3
    gemini_ctor.assert_not_called()  # the primary covered every slot


# --- Chunked streaming: one bulk core run per ``data.batch_size`` chunk ---


def _video_ids(count: int) -> list[str]:
    return [f"vid{i:08d}" for i in range(count)]


class _FailingChunkFetcher(_FakeFetcher):
    """A Bright Data stand-in that times out on any chunk holding ``poison``."""

    def __init__(
        self, transcripts: dict[str, FetchedTranscript | None], *, poison: str
    ) -> None:
        super().__init__(transcripts)
        self.poison = poison

    async def fetch_many(self, urls: list[str]) -> list[FetchedTranscript | None]:
        if any(self.poison in url for url in urls):
            self.calls.append(list(urls))
            raise BrightDataTimeoutError("collection not ready")
        return await super().fetch_many(urls)


@pytest.fixture
def _batch_size_two(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app_config, "data", DataConfig(batch_size=2))


@pytest.fixture
def _real_core(mocker) -> None:
    """Run the REAL bulk core: oEmbed resolve echoes the URL, load echoes the doc."""

    mocker.patch.object(
        yt,
        "_resolve_video_item",
        new_callable=AsyncMock,
        side_effect=lambda url: (url, _meta(url.rsplit("=", 1)[-1])),
    )
    mocker.patch.object(settings, "brightdata_api_key", SecretStr("bd-key"))
    mocker.patch.object(settings, "google_api_key", SecretStr("gemini-key"))


def _patch_load(mocker) -> AsyncMock:
    return mocker.patch.object(
        youtube_pipeline,
        "load_video_document",
        new_callable=AsyncMock,
        side_effect=lambda doc: doc,
    )


@pytest.mark.usefixtures("_batch_size_two", "_real_core")
async def test_transcripts_are_fetched_and_loaded_once_per_chunk(mocker) -> None:
    ids = _video_ids(5)
    fake = _FakeFetcher({v: _transcript(v) for v in ids})
    mocker.patch.object(
        youtube_pipeline, "BrightDataTranscriptFetcher", return_value=fake
    )
    load = mocker.patch.object(
        youtube_pipeline,
        "load_batch",
        new_callable=AsyncMock,
        side_effect=lambda docs: list(docs),
    )

    result = await yt.ingest_youtube_batch.fn(
        [YouTubeVideoSource(uri=_canonical(v)) for v in ids], _USER_ID
    )

    assert [len(urls) for urls in fake.calls] == [2, 2, 1]
    assert [len(call.args[0]) for call in load.await_args_list] == [2, 2, 1]
    assert [doc.source_uri for doc in result] == [_canonical(v) for v in ids]


@pytest.mark.usefixtures("_real_core")
async def test_chunked_result_equals_the_single_chunk_result(
    mocker, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = _video_ids(5)
    mocker.patch.object(
        youtube_pipeline,
        "BrightDataTranscriptFetcher",
        side_effect=lambda: _FakeFetcher({v: _transcript(v) for v in ids}),
    )
    _patch_load(mocker)
    entries = [YouTubeVideoSource(uri=_canonical(v)) for v in ids]

    monkeypatch.setattr(app_config, "data", DataConfig(batch_size=2))
    chunked = await yt.ingest_youtube_batch.fn(entries, _USER_ID)
    monkeypatch.setattr(app_config, "data", DataConfig(batch_size=500))
    single = await yt.ingest_youtube_batch.fn(entries, _USER_ID)

    assert [doc.source_uri for doc in chunked] == [doc.source_uri for doc in single]
    assert len(chunked) == 5


@pytest.mark.usefixtures("_batch_size_two", "_real_core")
async def test_invalid_url_rows_are_written_exactly_once_across_chunks(
    mocker,
) -> None:
    ids = _video_ids(5)
    invalid = ["https://example.com/not-youtube", "not a url"]
    mocker.patch.object(
        youtube_pipeline,
        "BrightDataTranscriptFetcher",
        return_value=_FakeFetcher({v: _transcript(v) for v in ids}),
    )
    load = _patch_load(mocker)

    await yt.ingest_youtube_batch.fn(
        [
            *(YouTubeVideoSource(uri=_canonical(v)) for v in ids),
            *(YouTubeVideoSource(uri=raw) for raw in invalid),
        ],
        _USER_ID,
    )

    loaded: list[Document] = [call.args[0] for call in load.await_args_list]
    invalid_rows = [d.source_uri for d in loaded if d.ingest_error == INVALID_URL_ERROR]
    assert sorted(invalid_rows) == sorted(invalid)


@pytest.mark.usefixtures("_batch_size_two", "_real_core")
async def test_a_bright_data_failure_falls_back_only_its_chunk(mocker) -> None:
    # Chunks [v0, v1 | v2, v3 | v4]: Bright Data times out on chunk 2 only, so only
    # v2 + v3 go to Gemini; chunks 1 and 3 keep their Bright Data transcripts.
    ids = _video_ids(5)
    brightdata = _FailingChunkFetcher({v: _transcript(v) for v in ids}, poison=ids[2])
    gemini = _FakeFetcher({v: _transcript(v) for v in ids})
    mocker.patch.object(
        youtube_pipeline, "BrightDataTranscriptFetcher", return_value=brightdata
    )
    mocker.patch.object(
        youtube_pipeline, "GeminiTranscriptFetcher", return_value=gemini
    )
    _patch_load(mocker)

    result = await yt.ingest_youtube_batch.fn(
        [YouTubeVideoSource(uri=_canonical(v)) for v in ids], _USER_ID
    )

    assert [len(urls) for urls in brightdata.calls] == [2, 2, 1]
    assert gemini.calls == [[_canonical(ids[2]), _canonical(ids[3])]]
    assert len(result) == 5


@pytest.mark.usefixtures("_batch_size_two", "_real_core")
async def test_a_failure_in_one_chunk_does_not_stop_the_next(mocker) -> None:
    # Chunks [v0, v1 | v2, v3 | v4]: one load fails in chunk 1, one in chunk 2.
    ids = _video_ids(5)
    failing = {_canonical(ids[0]), _canonical(ids[2])}
    mocker.patch.object(
        youtube_pipeline,
        "BrightDataTranscriptFetcher",
        return_value=_FakeFetcher({v: _transcript(v) for v in ids}),
    )

    async def fake_load(doc: Document) -> Document:
        if doc.source_uri in failing:
            raise RuntimeError(f"load failed: {doc.source_uri}")
        return doc

    mocker.patch.object(
        youtube_pipeline,
        "load_video_document",
        new_callable=AsyncMock,
        side_effect=fake_load,
    )

    with track_item_failures() as tally:
        result = await yt.ingest_youtube_batch.fn(
            [YouTubeVideoSource(uri=_canonical(v)) for v in ids], _USER_ID
        )

    assert [doc.source_uri for doc in result] == [
        _canonical(v) for v in (ids[1], ids[3], ids[4])
    ]
    assert tally.count == 2


@pytest.mark.usefixtures("_batch_size_two")
async def test_invalid_inputs_ride_only_the_first_chunk(mocker) -> None:
    ids = _video_ids(3)
    mocker.patch.object(
        yt,
        "_resolve_video_item",
        new_callable=AsyncMock,
        side_effect=lambda url: (url, _meta(url.rsplit("=", 1)[-1])),
    )
    bulk = _patch_bulk(mocker)

    await yt.ingest_youtube_batch.fn(
        [
            *(YouTubeVideoSource(uri=_canonical(v)) for v in ids),
            YouTubeVideoSource(uri="not a url"),
        ],
        _USER_ID,
    )

    assert [call.kwargs["invalid_inputs"] for call in bulk.await_args_list] == [
        ["not a url"],
        None,
    ]
