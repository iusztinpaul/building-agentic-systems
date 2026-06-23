"""Integration tests for the single-video YouTube ETL (#080 shared bulk core).

Persists real Documents against the local MongoDB fixture (`mongo_client`
from `tests/integration/conftest.py`). Mocks:

- The transcript fetcher (no `youtube-transcript-api`, no Gemini calls), injected
  by patching `tree.data.youtube.youtube_ingest.GeminiTranscriptFetcher` (the
  construction point inside the shared bulk core) — the flows no longer carry a
  `fetcher` arg. Patching the class is REQUIRED because
  `GeminiTranscriptFetcher.__init__` raises without `GOOGLE_API_KEY`.
- The oEmbed HTTP call (no live network to youtube.com).

Mirrors the patterns in `tests/integration/data/substack/test_substack_rss_pipeline.py`.
The batch path now issues ONE bulk `fetch_many(all_urls)` for the whole batch — a
call-count assertion on the fake fetcher guards the #080 fix.
"""

from __future__ import annotations

import logging

import pytest
import tree.data.youtube.youtube_ingest as youtube_ingest
from beanie import PydanticObjectId
from prefect import tags as prefect_tags

from tree.data.youtube.types import (
    FetchedTranscript,
    TranscriptSegment,
    VideoMetadata,
)
from tree.data.youtube.youtube_video_pipeline import (
    ingest_youtube_video,
    ingest_youtube_video_batch,
)
from tree.entities.documents import Document, SourceType

VIDEO_ID = "eYaWxljC4sA"
CANONICAL_URL = f"https://www.youtube.com/watch?v={VIDEO_ID}"
SHORT_URL = f"https://youtu.be/{VIDEO_ID}"

PIPELINE_LOGGER = "tree.data.youtube.youtube_video_pipeline"


def _make_transcript(
    *, video_id: str = VIDEO_ID, plain_text: str = "Some spoken words here."
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
    """Programmable stand-in for ``GeminiTranscriptFetcher`` recording every call.

    Same ``async def fetch_many(urls) -> list[FetchedTranscript | None]`` contract,
    swapped in by patching ``youtube_ingest.GeminiTranscriptFetcher`` to return it.
    Returns one element per input slot by substring-matching each URL against the
    supplied ``results_per_id`` map.
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


def _patch_oembed(mocker, payload: dict | None = None, *, status_code: int = 200):
    """Patch `httpx.AsyncClient` in the youtube_video module.

    Returns the mock client so individual tests can assert on calls.
    """

    if payload is None:
        payload = {
            "title": "An Interesting Video",
            "author_name": "The Channel",
            "type": "video",
        }

    mock_response = mocker.Mock()
    mock_response.status_code = status_code
    mock_response.json = mocker.Mock(return_value=payload)
    mock_response.raise_for_status = mocker.Mock()

    mock_client = mocker.AsyncMock()
    mock_client.get.return_value = mock_response
    mock_client.__aenter__ = mocker.AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = mocker.AsyncMock(return_value=False)

    mocker.patch(
        "tree.data.youtube.youtube_video.httpx.AsyncClient",
        return_value=mock_client,
    )
    return mock_client


class TestIngestYoutubeVideoFlow:
    """The thin MCP-only @flow ingests a single video with oEmbed metadata."""

    async def test_ingests_document_via_prefect_flow(
        self, mongo_client, mocker
    ) -> None:
        _patch_oembed(mocker)
        fake = _FakeFetcher({VIDEO_ID: _make_transcript(plain_text="hello transcript")})
        _patch_fetcher(mocker, fake)

        with prefect_tags("tests"):
            result = await ingest_youtube_video(CANONICAL_URL, PydanticObjectId())

        assert result is not None
        assert result.source_type == SourceType.YOUTUBE
        assert result.source_uri == CANONICAL_URL
        assert result.content == "hello transcript"
        assert result.title == "An Interesting Video"
        assert result.authors == ["The Channel"]

        db_doc = await Document.find_one(Document.source_uri == CANONICAL_URL)
        assert db_doc is not None
        assert db_doc.source_type == SourceType.YOUTUBE

    async def test_idempotent_on_rerun(self, mongo_client, mocker) -> None:
        _patch_oembed(mocker)
        fake = _FakeFetcher({VIDEO_ID: _make_transcript()})
        _patch_fetcher(mocker, fake)

        with prefect_tags("tests"):
            user_id = PydanticObjectId()
            first = await ingest_youtube_video(CANONICAL_URL, user_id)
            second = await ingest_youtube_video(CANONICAL_URL, user_id)

        assert first is not None
        assert second is None  # duplicate skipped

        rows = await Document.find(Document.source_type == SourceType.YOUTUBE).to_list()
        assert len(rows) == 1

    async def test_canonicalizes_short_url_to_watch_form(
        self, mongo_client, mocker
    ) -> None:
        _patch_oembed(mocker)
        fake = _FakeFetcher({VIDEO_ID: _make_transcript()})
        _patch_fetcher(mocker, fake)

        with prefect_tags("tests"):
            user_id = PydanticObjectId()
            first = await ingest_youtube_video(SHORT_URL, user_id)
            second = await ingest_youtube_video(CANONICAL_URL, user_id)

        assert first is not None
        # Persists with canonical URL regardless of pasted shape.
        assert first.source_uri == CANONICAL_URL
        # Subsequent ingest of the watch?v=… form is a duplicate.
        assert second is None

        rows = await Document.find(Document.source_type == SourceType.YOUTUBE).to_list()
        assert len(rows) == 1
        assert rows[0].source_uri == CANONICAL_URL

    async def test_upgrades_latent_document(self, mongo_client, mocker) -> None:
        user_id = PydanticObjectId()
        latent = Document(
            source_type=SourceType.LATENT,
            source_uri=CANONICAL_URL,
            user_id=user_id,
        )
        await latent.insert()

        _patch_oembed(mocker)
        fake = _FakeFetcher({VIDEO_ID: _make_transcript(plain_text="transcript text")})
        _patch_fetcher(mocker, fake)

        with prefect_tags("tests"):
            result = await ingest_youtube_video(CANONICAL_URL, user_id)

        assert result is not None
        assert result.id == latent.id
        assert result.source_type == SourceType.YOUTUBE
        assert result.title == "An Interesting Video"

        rows = await Document.find(Document.source_uri == CANONICAL_URL).to_list()
        assert len(rows) == 1
        assert rows[0].source_type == SourceType.YOUTUBE

    async def test_missing_transcript_skips_quietly(
        self, mongo_client, mocker, caplog
    ) -> None:
        # oEmbed should not even be called when the bulk fetch returned None,
        # but patch defensively so a regression doesn't hit the network.
        _patch_oembed(mocker)
        fake = _FakeFetcher({VIDEO_ID: None})
        _patch_fetcher(mocker, fake)

        caplog.set_level(logging.WARNING, logger=PIPELINE_LOGGER)

        with prefect_tags("tests"):
            result = await ingest_youtube_video(CANONICAL_URL, PydanticObjectId())

        assert result is None

        rows = await Document.find().to_list()
        assert rows == []

        # Spec: pipeline emits no redundant WARNING — the bulk core owns the
        # "No transcript for <url>; skipping" warning in fetch_transcripts_batch.
        pipeline_warnings = [
            r
            for r in caplog.records
            if r.name == PIPELINE_LOGGER and r.levelno >= logging.WARNING
        ]
        assert pipeline_warnings == []

    async def test_oembed_404_still_persists_document(
        self, mongo_client, mocker
    ) -> None:
        # Some videos disable oEmbed; we must still land a document with the
        # transcript-derived title fallback.
        _patch_oembed(mocker, payload={}, status_code=404)
        fake = _FakeFetcher({VIDEO_ID: _make_transcript(plain_text="transcript only")})
        _patch_fetcher(mocker, fake)

        with prefect_tags("tests"):
            result = await ingest_youtube_video(CANONICAL_URL, PydanticObjectId())

        assert result is not None
        assert result.title == f"YouTube video {VIDEO_ID}"
        assert result.authors == []
        assert result.content == "transcript only"


class TestIngestYoutubeVideoBatchFlow:
    """The batch path issues ONE bulk transcript fetch for the whole batch."""

    async def test_ingests_multiple_videos_with_one_bulk_fetch(
        self, mongo_client, mocker
    ) -> None:
        _patch_oembed(mocker)
        mocker.patch(
            "tree.data.youtube.youtube_video_pipeline.init_mongodb",
            return_value=mongo_client,
        )

        url_a = "https://www.youtube.com/watch?v=eYaWxljC4sA"
        url_b = "https://www.youtube.com/watch?v=AAAaaaBBBcc"
        fake = _FakeFetcher(
            {
                "eYaWxljC4sA": _make_transcript(video_id="eYaWxljC4sA"),
                "AAAaaaBBBcc": _make_transcript(video_id="AAAaaaBBBcc"),
            }
        )
        _patch_fetcher(mocker, fake)

        with prefect_tags("tests"):
            result = await ingest_youtube_video_batch(
                video_urls=[url_a, url_b], user_id=PydanticObjectId()
            )

        assert len(result) == 2
        sources = sorted(doc.source_uri for doc in result)
        assert sources == sorted([url_a, url_b])

        # #080: ONE bulk fetch_many with BOTH canonical URLs — NOT per-video.
        assert len(fake.calls) == 1
        assert sorted(fake.calls[0]) == sorted([url_a, url_b])

    async def test_batch_handles_missing_transcript_slot(
        self, mongo_client, mocker
    ) -> None:
        _patch_oembed(mocker)
        mocker.patch(
            "tree.data.youtube.youtube_video_pipeline.init_mongodb",
            return_value=mongo_client,
        )

        url_good = "https://www.youtube.com/watch?v=eYaWxljC4sA"
        url_bad = "https://www.youtube.com/watch?v=AAAaaaBBBcc"
        fake = _FakeFetcher(
            {
                "eYaWxljC4sA": _make_transcript(video_id="eYaWxljC4sA"),
                "AAAaaaBBBcc": None,  # bulk fetch exhausted for the second slot
            }
        )
        _patch_fetcher(mocker, fake)

        with prefect_tags("tests"):
            result = await ingest_youtube_video_batch(
                video_urls=[url_good, url_bad],
                user_id=PydanticObjectId(),
            )

        assert len(result) == 1
        assert result[0].source_uri == url_good
        # Still ONE bulk fetch over both URLs.
        assert len(fake.calls) == 1
        assert len(fake.calls[0]) == 2


@pytest.fixture(autouse=True)
def _silence_prefect_log(caplog):
    # Prefect adds its own info-level chatter at flow boundaries; we only
    # care about WARNING-level records on our pipeline logger.
    caplog.set_level(logging.INFO, logger=PIPELINE_LOGGER)
    yield
