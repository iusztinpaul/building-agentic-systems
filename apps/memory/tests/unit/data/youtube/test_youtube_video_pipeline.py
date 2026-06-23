"""Unit tests for ``tree.data.youtube.youtube_video_pipeline`` (#080).

The direct-video path enriches per video via oEmbed, then runs the SHARED bulk core
(``youtube_ingest._bulk_build_and_load``): ONE ``fetch_many(all_urls)`` for the whole
batch → ``build_document`` per slot → ``load_video_document`` per slot. The per-item
sub-flow's body is demoted to the plain async core ``_ingest_youtube_video_one``;
``ingest_youtube_video`` remains a THIN @flow wrapper used ONLY by the MCP URL router.
The BATCH path calls the shared core directly — it MUST NOT invoke the thin wrapper,
and it must issue EXACTLY ONE bulk transcript fetch (the #080 regression fix).
"""

from __future__ import annotations

import tree.data.youtube.youtube_ingest as youtube_ingest
import tree.data.youtube.youtube_video_pipeline as video_pipeline
from beanie import PydanticObjectId

from tree.data.youtube.types import (
    FetchedTranscript,
    TranscriptSegment,
    VideoMetadata,
)
from tree.data.youtube.youtube_video_pipeline import (
    _ingest_youtube_video_one,
    ingest_youtube_video,
    ingest_youtube_video_batch,
)
from tree.entities.documents import Document, SourceType

VIDEO_IDS = ["eYaWxljC4sA", "AAAaaaBBBcc", "ZZZzzzYYYxx", "DDDdddEEEff", "GGGggHHHiij"]


def _canonical(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def _make_transcript(video_id: str, plain_text: str = "words") -> FetchedTranscript:
    return FetchedTranscript(
        metadata=VideoMetadata(video_id=video_id),
        segments=[
            TranscriptSegment(text=plain_text, start_seconds=0.0, duration_seconds=1.0)
        ],
        language="en",
        plain_text=plain_text,
    )


def _make_doc(video_id: str) -> Document:
    return Document(
        source_type=SourceType.YOUTUBE,
        source_uri=_canonical(video_id),
        user_id=PydanticObjectId(),
        title=f"Video {video_id}",
        summary="Summary",
        content="Body",
        authors=["Channel"],
    )


class _FakeFetcher:
    """Programmable `TranscriptFetcher` recording every ``fetch_many`` call."""

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


class TestTaskAndFlowMetadata:
    """Per-row tasks are gone; the flow names are the stable seams."""

    def test_thin_flow_name(self) -> None:
        assert ingest_youtube_video.name == "ingest-youtube-video-etl"

    def test_batch_flow_name(self) -> None:
        assert ingest_youtube_video_batch.name == "ingest-youtube-video-batch-etl"

    def test_per_row_tasks_are_gone(self) -> None:
        assert not hasattr(video_pipeline, "fetch_video_task")
        assert not hasattr(video_pipeline, "load_video_task")

    def test_batch_flow_signature_unchanged(self) -> None:
        import inspect

        params = list(inspect.signature(ingest_youtube_video_batch).parameters)
        assert params == ["video_urls", "user_id", "fetcher"]


class TestIngestOne:
    """The plain async core: oEmbed metadata then the shared bulk core."""

    async def test_is_a_plain_function_not_a_flow_or_task(self) -> None:
        assert not hasattr(_ingest_youtube_video_one, "fn")

    async def test_resolves_oembed_metadata_then_calls_shared_core(
        self, mocker
    ) -> None:
        user_id = PydanticObjectId()
        doc = _make_doc(VIDEO_IDS[0])

        oembed_mock = mocker.patch.object(
            video_pipeline,
            "fetch_oembed_metadata",
            mocker.AsyncMock(return_value={"title": "T", "author_name": "C"}),
        )
        core_mock = mocker.patch.object(
            video_pipeline,
            "_bulk_build_and_load",
            mocker.AsyncMock(return_value=[doc]),
        )
        fake = _FakeFetcher({})

        result = await _ingest_youtube_video_one(
            _canonical(VIDEO_IDS[0]), user_id, fake
        )

        assert result is doc
        # oEmbed metadata IS the direct-video metadata source.
        oembed_mock.assert_awaited_once_with(_canonical(VIDEO_IDS[0]))
        # The core is called with a SINGLE-item list carrying the oEmbed metadata.
        core_mock.assert_awaited_once()
        items_arg, user_arg, fetcher_arg = core_mock.await_args.args
        assert len(items_arg) == 1
        url, metadata = items_arg[0]
        assert url == _canonical(VIDEO_IDS[0])
        assert metadata.title == "T"
        assert metadata.channel == "C"
        assert user_arg == user_id
        assert fetcher_arg is fake

    async def test_unresolvable_url_returns_none_without_core_call(
        self, mocker
    ) -> None:
        core_mock = mocker.patch.object(
            video_pipeline, "_bulk_build_and_load", mocker.AsyncMock()
        )

        result = await _ingest_youtube_video_one(
            "https://example.com/not-youtube", PydanticObjectId(), _FakeFetcher({})
        )

        assert result is None
        core_mock.assert_not_awaited()

    async def test_returns_none_when_core_yields_nothing(self, mocker) -> None:
        mocker.patch.object(
            video_pipeline,
            "fetch_oembed_metadata",
            mocker.AsyncMock(return_value={}),
        )
        mocker.patch.object(
            video_pipeline,
            "_bulk_build_and_load",
            mocker.AsyncMock(return_value=[]),
        )

        result = await _ingest_youtube_video_one(
            _canonical(VIDEO_IDS[0]), PydanticObjectId(), _FakeFetcher({})
        )

        assert result is None


class TestThinFlow:
    """The thin MCP-only @flow delegates to the core."""

    async def test_delegates_to_core(self, mocker) -> None:
        doc = _make_doc(VIDEO_IDS[0])
        core_mock = mocker.patch.object(
            video_pipeline,
            "_ingest_youtube_video_one",
            mocker.AsyncMock(return_value=doc),
        )
        user_id = PydanticObjectId()
        fake = _FakeFetcher({})

        result = await ingest_youtube_video.fn(
            _canonical(VIDEO_IDS[0]), user_id, fetcher=fake
        )

        assert result is doc
        core_mock.assert_awaited_once_with(_canonical(VIDEO_IDS[0]), user_id, fake)


class TestIngestYoutubeVideoBatch:
    """ONE bulk fetch for the whole batch; the thin flow is NEVER invoked."""

    async def test_one_bulk_fetch_over_all_canonical_urls(self, mocker) -> None:
        # The headline #080 assertion: a batch of 5 URLs → ONE fetch_many(5 urls),
        # NOT 5 calls of fetch_many([url]).
        mocker.patch.object(video_pipeline, "init_mongodb", mocker.AsyncMock())
        mocker.patch.object(
            video_pipeline,
            "fetch_oembed_metadata",
            mocker.AsyncMock(return_value={"title": "T", "author_name": "C"}),
        )
        # Stub the DB Load to echo each built doc (no Mongo write in a unit test).
        mocker.patch.object(
            youtube_ingest,
            "load_video_document",
            mocker.AsyncMock(side_effect=lambda doc: doc),
        )
        fake = _FakeFetcher({vid: _make_transcript(vid) for vid in VIDEO_IDS})

        result = await ingest_youtube_video_batch.fn(
            [_canonical(vid) for vid in VIDEO_IDS], PydanticObjectId(), fetcher=fake
        )

        # Exactly ONE bulk transcript fetch with all 5 canonical URLs.
        assert len(fake.calls) == 1
        assert fake.calls[0] == [_canonical(vid) for vid in VIDEO_IDS]
        # All 5 Documents built + loaded.
        assert len(result) == 5
        assert sorted(d.source_uri for d in result) == sorted(
            _canonical(vid) for vid in VIDEO_IDS
        )

    async def test_does_not_call_thin_flow(self, mocker) -> None:
        mocker.patch.object(video_pipeline, "init_mongodb", mocker.AsyncMock())
        mocker.patch.object(
            video_pipeline,
            "fetch_oembed_metadata",
            mocker.AsyncMock(return_value={}),
        )
        mocker.patch.object(
            youtube_ingest,
            "load_video_document",
            mocker.AsyncMock(side_effect=lambda doc: doc),
        )
        thin_spy = mocker.patch.object(
            video_pipeline, "ingest_youtube_video", mocker.AsyncMock()
        )
        fake = _FakeFetcher({vid: _make_transcript(vid) for vid in VIDEO_IDS})

        await ingest_youtube_video_batch.fn(
            [_canonical(vid) for vid in VIDEO_IDS], PydanticObjectId(), fetcher=fake
        )

        # No per-item sub-flow runs: the batch path never calls the thin wrapper.
        thin_spy.assert_not_awaited()

    async def test_missing_transcript_slot_isolated(self, mocker) -> None:
        mocker.patch.object(video_pipeline, "init_mongodb", mocker.AsyncMock())
        mocker.patch.object(
            video_pipeline,
            "fetch_oembed_metadata",
            mocker.AsyncMock(return_value={}),
        )
        mocker.patch.object(
            youtube_ingest,
            "load_video_document",
            mocker.AsyncMock(side_effect=lambda doc: doc),
        )
        # The middle of 5 videos has no transcript (chain returns None).
        results = {vid: _make_transcript(vid) for vid in VIDEO_IDS}
        results[VIDEO_IDS[2]] = None
        fake = _FakeFetcher(results)

        result = await ingest_youtube_video_batch.fn(
            [_canonical(vid) for vid in VIDEO_IDS], PydanticObjectId(), fetcher=fake
        )

        # Still ONE bulk fetch; the missing slot is skipped, the other 4 persist.
        assert len(fake.calls) == 1
        assert len(result) == 4
        assert _canonical(VIDEO_IDS[2]) not in {d.source_uri for d in result}
