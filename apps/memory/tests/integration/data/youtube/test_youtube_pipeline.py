"""Integration tests for the single-video YouTube ETL (#080 shared bulk core).

Persists real Documents against the local MongoDB fixture (`mongo_client`
from `tests/integration/conftest.py`). Mocks:

- BOTH transcript backends (no Bright Data collection, no Gemini call), injected
  by patching `tree.data.youtube.youtube_ingest.BrightDataTranscriptFetcher` and
  `...GeminiTranscriptFetcher` — the construction points inside the shared bulk
  core; the flows carry no `fetcher` arg. Patching the classes is REQUIRED
  because both constructors raise without their API key, and it is what keeps
  this suite from ever billing a live backend (ADR-004, Decision 8).
- The oEmbed HTTP call (no live network to youtube.com).

Covers both the thin single-video MCP flow (`ingest_youtube_video`) and the unified
batch (`youtube_pipeline_batch.ingest_youtube_batch`), which issues ONE bulk
`fetch_many(all_urls)` over the whole shard — a call-count assertion on the fake
PRIMARY fetcher guards that — plus the persisted `ingest_error` rows (#092).
"""

from __future__ import annotations

import logging

import pytest
import tree.data.youtube.youtube_ingest as youtube_ingest
from beanie import PydanticObjectId
from prefect import tags as prefect_tags
from pydantic import SecretStr

from tree.config.settings import settings
from tree.data.youtube.types import (
    FetchedTranscript,
    TranscriptSegment,
    VideoMetadata,
)
from tree.config.sources import YouTubeVideoSource
from tree.data.youtube.youtube_pipeline_batch import ingest_youtube_batch
from tree.data.youtube.youtube_pipeline import ingest_youtube_video
from tree.entities.documents import Document, SourceType

VIDEO_ID = "eYaWxljC4sA"
CANONICAL_URL = f"https://www.youtube.com/watch?v={VIDEO_ID}"
SHORT_URL = f"https://youtu.be/{VIDEO_ID}"

PIPELINE_LOGGER = "tree.data.youtube.youtube_pipeline"


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
    """Programmable stand-in for either transcript backend, recording every call.

    Same ``async def fetch_many(urls) -> list[FetchedTranscript | None]`` contract,
    swapped in by patching the backend's construction point in ``youtube_ingest``.
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


def _patch_fetcher(mocker, fake: _FakeFetcher) -> _FakeFetcher:
    """Patch BOTH construction points: ``fake`` is the PRIMARY (Bright Data) backend.

    Patching the classes (not injected args) sidesteps the API-key guards in both
    constructors — no key, no network, no billed call. The Gemini fallback is
    wired to an always-empty fake so a transcript-less primary slot exhausts the
    chain deterministically; the returned fake is that fallback, so a test can
    assert on what Gemini was asked for.
    """

    gemini = _FakeFetcher({})
    mocker.patch.object(
        youtube_ingest, "BrightDataTranscriptFetcher", return_value=fake
    )
    mocker.patch.object(youtube_ingest, "GeminiTranscriptFetcher", return_value=gemini)
    mocker.patch.object(settings, "brightdata_api_key", SecretStr("bd-key"))
    mocker.patch.object(settings, "google_api_key", SecretStr("gemini-key"))
    return gemini


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

    async def test_exhausted_transcript_persists_an_ingest_error_row(
        self, mongo_client, mocker, caplog
    ) -> None:
        _patch_oembed(mocker)
        fake = _FakeFetcher({VIDEO_ID: None})
        gemini = _patch_fetcher(mocker, fake)  # the fallback is empty too

        caplog.set_level(logging.WARNING, logger=PIPELINE_LOGGER)

        with prefect_tags("tests"):
            result = await ingest_youtube_video(CANONICAL_URL, PydanticObjectId())

        # Nothing was INGESTED …
        assert result is None
        # … but the failure is persisted as inspectable data (ADR-004 §6).
        rows = await Document.find().to_list()
        assert len(rows) == 1
        assert rows[0].source_uri == CANONICAL_URL
        assert rows[0].source_type == SourceType.YOUTUBE
        assert rows[0].content is None
        assert rows[0].ingest_error == (
            "no_transcript: brightdata + gemini both returned empty"
        )
        # The fallback ran over exactly the transcript-less slot.
        assert gemini.calls == [[CANONICAL_URL]]

        # Spec: pipeline emits no redundant WARNING — the bulk core owns the
        # "No transcript for <url>" warning in fetch_transcripts_batch.
        pipeline_warnings = [
            r
            for r in caplog.records
            if r.name == PIPELINE_LOGGER and r.levelno >= logging.WARNING
        ]
        assert pipeline_warnings == []

    async def test_later_success_replaces_a_persisted_failure_row(
        self, mongo_client, mocker
    ) -> None:
        # #089 replace-on-retry, end-to-end: the failure row a first run leaves
        # behind is REPLACED by the transcript a later run gets.
        _patch_oembed(mocker)
        user_id = PydanticObjectId()

        _patch_fetcher(mocker, _FakeFetcher({VIDEO_ID: None}))
        with prefect_tags("tests"):
            first = await ingest_youtube_video(CANONICAL_URL, user_id)

        _patch_fetcher(mocker, _FakeFetcher({VIDEO_ID: _make_transcript()}))
        with prefect_tags("tests"):
            second = await ingest_youtube_video(CANONICAL_URL, user_id)

        assert first is None
        assert second is not None

        rows = await Document.find(Document.source_uri == CANONICAL_URL).to_list()
        assert len(rows) == 1  # replaced in place, not duplicated
        assert rows[0].ingest_error is None
        assert rows[0].content == "Some spoken words here."

    async def test_unresolvable_input_persists_an_invalid_url_row(
        self, mongo_client, mocker
    ) -> None:
        _patch_oembed(mocker)
        _patch_fetcher(mocker, _FakeFetcher({}))
        raw_input = "https://example.com/not-a-youtube-video"

        with prefect_tags("tests"):
            result = await ingest_youtube_video(raw_input, PydanticObjectId())

        assert result is None

        rows = await Document.find().to_list()
        assert len(rows) == 1
        # Keyed on the RAW input: there is no canonical URL for it.
        assert rows[0].source_uri == raw_input
        assert rows[0].content is None
        assert rows[0].ingest_error == "invalid_url: no video id in input"

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


class TestIngestYoutubeBatchFlow:
    """The unified batch path issues ONE bulk transcript fetch for the whole shard."""

    async def test_ingests_multiple_videos_with_one_bulk_fetch(
        self, mongo_client, mocker
    ) -> None:
        _patch_oembed(mocker)
        mocker.patch(
            "tree.data.youtube.youtube_pipeline_batch.init_mongodb",
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
            result = await ingest_youtube_batch(
                [YouTubeVideoSource(uri=url_a), YouTubeVideoSource(uri=url_b)],
                PydanticObjectId(),
            )

        assert len(result) == 2
        sources = sorted(doc.source_uri for doc in result)
        assert sources == sorted([url_a, url_b])

        # ONE bulk fetch_many with BOTH canonical URLs — NOT per-video.
        assert len(fake.calls) == 1
        assert sorted(fake.calls[0]) == sorted([url_a, url_b])

    async def test_batch_handles_missing_transcript_slot(
        self, mongo_client, mocker
    ) -> None:
        _patch_oembed(mocker)
        mocker.patch(
            "tree.data.youtube.youtube_pipeline_batch.init_mongodb",
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
            result = await ingest_youtube_batch(
                [YouTubeVideoSource(uri=url_good), YouTubeVideoSource(uri=url_bad)],
                PydanticObjectId(),
            )

        assert len(result) == 1
        assert result[0].source_uri == url_good
        # Still ONE bulk fetch over both URLs.
        assert len(fake.calls) == 1
        assert len(fake.calls[0]) == 2

        # The exhausted slot lands as a failure row — the batch survives it.
        rows = await Document.find(Document.source_uri == url_bad).to_list()
        assert len(rows) == 1
        assert rows[0].content is None
        assert rows[0].ingest_error is not None
        assert rows[0].ingest_error.startswith("no_transcript: ")


@pytest.fixture(autouse=True)
def _silence_prefect_log(caplog):
    # Prefect adds its own info-level chatter at flow boundaries; we only
    # care about WARNING-level records on our pipeline logger.
    caplog.set_level(logging.INFO, logger=PIPELINE_LOGGER)
    yield
