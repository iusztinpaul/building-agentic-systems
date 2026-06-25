"""Unit tests for ``tree.data.youtube.youtube_pipeline`` — the ONLINE single-video path.

After the platform unification this module holds only the single-video resolve
(``_resolve_video_item``, also reused by the unified batch) and the thin MCP-only @flow
``ingest_youtube_video`` (+ its plain async core ``_ingest_youtube_video_one``). The
batch flow moved to ``youtube_pipeline_batch.ingest_youtube_batch`` (tested in
``test_youtube_pipeline_batch.py``).
"""

from __future__ import annotations

import tree.data.youtube.youtube_pipeline as video_pipeline
from beanie import PydanticObjectId

from tree.data.youtube.youtube_pipeline import (
    _ingest_youtube_video_one,
    ingest_youtube_video,
)
from tree.entities.documents import Document, SourceType

VIDEO_IDS = ["eYaWxljC4sA", "AAAaaaBBBcc"]


def _canonical(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


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


class TestFlowMetadata:
    def test_thin_flow_name(self) -> None:
        assert ingest_youtube_video.name == "ingest-youtube-video-etl"

    def test_per_row_tasks_are_gone(self) -> None:
        assert not hasattr(video_pipeline, "fetch_video_task")
        assert not hasattr(video_pipeline, "load_video_task")

    def test_thin_flow_signature_has_no_fetcher(self) -> None:
        import inspect

        params = list(inspect.signature(ingest_youtube_video).parameters)
        assert params == ["video_url", "user_id"]


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

        result = await _ingest_youtube_video_one(_canonical(VIDEO_IDS[0]), user_id)

        assert result is doc
        # oEmbed metadata IS the direct-video metadata source.
        oembed_mock.assert_awaited_once_with(_canonical(VIDEO_IDS[0]))
        # The core is called with a SINGLE-item list carrying the oEmbed metadata.
        core_mock.assert_awaited_once()
        items_arg, user_arg = core_mock.await_args.args
        assert len(items_arg) == 1
        url, metadata = items_arg[0]
        assert url == _canonical(VIDEO_IDS[0])
        assert metadata.title == "T"
        assert metadata.channel == "C"
        assert user_arg == user_id

    async def test_unresolvable_url_returns_none_without_core_call(
        self, mocker
    ) -> None:
        core_mock = mocker.patch.object(
            video_pipeline, "_bulk_build_and_load", mocker.AsyncMock()
        )

        result = await _ingest_youtube_video_one(
            "https://example.com/not-youtube", PydanticObjectId()
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
            _canonical(VIDEO_IDS[0]), PydanticObjectId()
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

        result = await ingest_youtube_video.fn(_canonical(VIDEO_IDS[0]), user_id)

        assert result is doc
        core_mock.assert_awaited_once_with(_canonical(VIDEO_IDS[0]), user_id)
