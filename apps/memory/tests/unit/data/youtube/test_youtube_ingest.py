"""Unit tests for the shared bulk-transcript ingest core (#080).

``tree.data.youtube.youtube_ingest`` is the single tail BOTH YouTube pipelines run:
"(url, metadata) list → ONE bulk ``fetch_many`` → ``build_document`` per non-``None``
slot → ``load_video_document`` per slot (isolated)". These tests prove the bulk-fetch
grain, the ``None``-slot skip + WARNING, the per-element load isolation, and the
ETL-phase task retry metadata.

The transcript backend is now CONSTRUCTED inside ``fetch_transcripts_batch`` rather than
passed in, so the tests PATCH the construction point
(``youtube_ingest.GeminiTranscriptFetcher``) with a fake whose ``fetch_many`` returns
canned transcripts — no ``GOOGLE_API_KEY``, no network, no Gemini.
"""

from __future__ import annotations

import logging

import tree.data.youtube.youtube_ingest as youtube_ingest
from beanie import PydanticObjectId

from tree.data.youtube.types import (
    FetchedTranscript,
    TranscriptSegment,
    VideoMetadata,
)
from tree.data.youtube.youtube_ingest import (
    _bulk_build_and_load,
    build_batch,
    fetch_transcripts_batch,
    load_batch,
)
from tree.entities.documents import Document, SourceType

VIDEO_IDS = ["eYaWxljC4sA", "AAAaaaBBBcc", "ZZZzzzYYYxx"]


def _canonical(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def _make_transcript(
    video_id: str, plain_text: str = "spoken words"
) -> FetchedTranscript:
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
    """Programmable transcript fetcher recording every ``fetch_many`` call.

    Stands in for ``GeminiTranscriptFetcher``: it exposes the same
    ``async def fetch_many(urls) -> list[FetchedTranscript | None]`` contract and is
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
    """Patch the in-task construction point so ``GeminiTranscriptFetcher()`` yields ``fake``.

    Patching the CLASS (not an injected arg) is required because
    ``GeminiTranscriptFetcher.__init__`` raises without ``GOOGLE_API_KEY``; the patch
    replaces it so the task needs no key/network.
    """

    mocker.patch.object(youtube_ingest, "GeminiTranscriptFetcher", return_value=fake)


class TestTaskMetadata:
    """Retry grain lives on the batch ETL-phase tasks (mirrors test_web_pipeline)."""

    def test_fetch_transcripts_batch_retries(self) -> None:
        assert fetch_transcripts_batch.retries == 2
        assert fetch_transcripts_batch.retry_delay_seconds == 5
        assert fetch_transcripts_batch.name == "fetch-youtube-transcripts-batch"

    def test_build_batch_retries(self) -> None:
        assert build_batch.retries == 0
        assert build_batch.name == "build-youtube-batch"

    def test_load_batch_retries(self) -> None:
        assert load_batch.retries == 1
        assert load_batch.retry_delay_seconds == 2
        assert load_batch.name == "load-youtube-batch"


class TestFetchTranscriptsBatch:
    """ONE bulk ``fetch_many`` per batch; ``None`` slots dropped with a WARNING."""

    async def test_one_bulk_fetch_over_all_urls(self, mocker) -> None:
        items = [(_canonical(vid), VideoMetadata(video_id=vid)) for vid in VIDEO_IDS]
        fake = _FakeFetcher({vid: _make_transcript(vid) for vid in VIDEO_IDS})
        _patch_fetcher(mocker, fake)

        resolved = await fetch_transcripts_batch.fn(items)

        # Exactly ONE call to fetch_many, with all 3 canonical URLs.
        assert len(fake.calls) == 1
        assert fake.calls[0] == [_canonical(vid) for vid in VIDEO_IDS]
        assert [url for url, _, _ in resolved] == [_canonical(vid) for vid in VIDEO_IDS]

    async def test_constructs_fetcher_inside_the_task(self, mocker) -> None:
        # The fetcher is built IN the task body, not threaded in as an argument.
        items = [(_canonical(VIDEO_IDS[0]), VideoMetadata(video_id=VIDEO_IDS[0]))]
        fake = _FakeFetcher({VIDEO_IDS[0]: _make_transcript(VIDEO_IDS[0])})
        ctor = mocker.patch.object(
            youtube_ingest, "GeminiTranscriptFetcher", return_value=fake
        )

        await fetch_transcripts_batch.fn(items)

        ctor.assert_called_once_with()

    async def test_none_transcript_slot_dropped_with_warning(
        self, mocker, caplog
    ) -> None:
        items = [(_canonical(vid), VideoMetadata(video_id=vid)) for vid in VIDEO_IDS]
        fake = _FakeFetcher(
            {
                VIDEO_IDS[0]: _make_transcript(VIDEO_IDS[0]),
                VIDEO_IDS[1]: None,  # no transcript for the middle slot
                VIDEO_IDS[2]: _make_transcript(VIDEO_IDS[2]),
            }
        )
        _patch_fetcher(mocker, fake)

        with caplog.at_level(logging.WARNING, logger=youtube_ingest.logger.name):
            resolved = await fetch_transcripts_batch.fn(items)

        # The middle slot is dropped; still ONE bulk fetch.
        assert len(fake.calls) == 1
        assert [url for url, _, _ in resolved] == [
            _canonical(VIDEO_IDS[0]),
            _canonical(VIDEO_IDS[2]),
        ]
        # The dropped slot emits the user-facing WARNING naming the video.
        warnings = [
            r
            for r in caplog.records
            if "No transcript for" in r.getMessage() and r.levelno == logging.WARNING
        ]
        assert len(warnings) == 1
        assert _canonical(VIDEO_IDS[1]) in warnings[0].getMessage()

    async def test_empty_items_no_fetch(self, mocker) -> None:
        ctor = mocker.patch.object(youtube_ingest, "GeminiTranscriptFetcher")

        resolved = await fetch_transcripts_batch.fn([])

        assert resolved == []
        # No fetcher is even constructed for an empty batch.
        ctor.assert_not_called()


class TestBuildBatch:
    """Pure map to Documents via the shared ``build_document``."""

    async def test_builds_one_document_per_slot(self) -> None:
        user_id = PydanticObjectId()
        resolved = [
            (
                _canonical(vid),
                VideoMetadata(video_id=vid, title=f"T {vid}"),
                _make_transcript(vid),
            )
            for vid in VIDEO_IDS
        ]

        docs = await build_batch.fn(resolved, user_id)

        assert len(docs) == 3
        assert [d.source_uri for d in docs] == [_canonical(vid) for vid in VIDEO_IDS]
        assert all(d.source_type == SourceType.YOUTUBE for d in docs)
        assert all(d.user_id == user_id for d in docs)
        # Metadata is honoured: the title from VideoMetadata flows through.
        assert docs[0].title == f"T {VIDEO_IDS[0]}"

    async def test_empty_resolved_returns_empty(self) -> None:
        assert await build_batch.fn([], PydanticObjectId()) == []


class TestLoadBatch:
    """DB Load over a single isolated gather via the shared ``load_video_document``."""

    async def test_returns_persisted_subset_dropping_duplicates(self, mocker) -> None:
        doc_a = _make_doc(VIDEO_IDS[0])
        doc_b = _make_doc(VIDEO_IDS[1])
        load_mock = mocker.patch.object(
            youtube_ingest,
            "load_video_document",
            mocker.AsyncMock(side_effect=[doc_a, None]),
        )

        result = await load_batch.fn([doc_a, doc_b])

        assert result == [doc_a]
        # ONE awaited gather over the doc list: one load per element.
        assert load_mock.await_count == 2

    async def test_isolates_one_element_failure(self, mocker) -> None:
        doc_a = _make_doc(VIDEO_IDS[0])
        doc_b = _make_doc(VIDEO_IDS[1])
        mocker.patch.object(
            youtube_ingest,
            "load_video_document",
            mocker.AsyncMock(side_effect=[doc_a, RuntimeError("bad load")]),
        )

        result = await load_batch.fn([doc_a, doc_b])

        # The failing element is logged + skipped, NOT propagated.
        assert result == [doc_a]

    async def test_empty_batch_returns_empty(self, mocker) -> None:
        load_mock = mocker.patch.object(
            youtube_ingest, "load_video_document", mocker.AsyncMock()
        )

        result = await load_batch.fn([])

        assert result == []
        load_mock.assert_not_awaited()


class TestBulkBuildAndLoad:
    """The shared core: one bulk fetch → build per non-None slot → isolated load."""

    async def test_is_a_plain_function_not_a_flow_or_task(self) -> None:
        # The core carries NO Prefect decorator (no ``.fn`` / ``.name`` attrs).
        assert not hasattr(_bulk_build_and_load, "fn")

    async def test_one_bulk_fetch_builds_per_slot_loads_isolated(self, mocker) -> None:
        user_id = PydanticObjectId()
        items = [(_canonical(vid), VideoMetadata(video_id=vid)) for vid in VIDEO_IDS]
        fake = _FakeFetcher({vid: _make_transcript(vid) for vid in VIDEO_IDS})
        _patch_fetcher(mocker, fake)

        built_docs = [_make_doc(vid) for vid in VIDEO_IDS]
        # Load drops the middle element (duplicate / failure) via side_effect.
        load_mock = mocker.patch.object(
            youtube_ingest,
            "load_video_document",
            mocker.AsyncMock(
                side_effect=[built_docs[0], RuntimeError("dup"), built_docs[2]]
            ),
        )
        # Build is deterministic: return our known docs in order.
        mocker.patch.object(
            youtube_ingest,
            "build_document",
            mocker.Mock(side_effect=built_docs),
        )

        result = await _bulk_build_and_load(items, user_id)

        # ONE bulk transcript fetch over all 3 canonical URLs.
        assert len(fake.calls) == 1
        assert fake.calls[0] == [_canonical(vid) for vid in VIDEO_IDS]
        # Load isolation: the 2 successes returned, the RuntimeError slot dropped.
        assert result == [built_docs[0], built_docs[2]]
        assert load_mock.await_count == 3
