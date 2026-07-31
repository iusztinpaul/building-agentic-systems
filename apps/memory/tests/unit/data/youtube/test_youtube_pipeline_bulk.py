"""Unit tests for the shared bulk-transcript ingest core (#080, rewired by #092).

``tree.data.youtube.youtube_pipeline`` is the single tail BOTH YouTube pipelines run:
"(url, metadata) list → Bright Data bulk fetch → Gemini fallback over the missing
slots → build → load". These tests prove the fallback chain order and grain, the
batch-wide fallback triggers, the up-front credential gate, the cost WARNINGs, the
metadata merge, and the persisted ``ingest_error`` rows.

NO live call is ever made: both backends are patched at their in-task CONSTRUCTION
point (``youtube_pipeline.BrightDataTranscriptFetcher`` /
``youtube_pipeline.GeminiTranscriptFetcher``) — the thin seam per fetcher, required
because both constructors raise without their API key. Credential PRESENCE is
faked by patching ``settings`` so the suite behaves identically with or without
keys in the operator's ``.env``. ``TestBrightDataTransportFailure`` is the one
exception: it patches a DEEPER seam (``httpx.AsyncClient`` inside
``web_scraper_api``) because the behaviour it proves — a transport failure
reaching the fallback chain as a typed error — lives below the fetcher.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
import tree.data.youtube.youtube_pipeline as youtube_pipeline
from beanie import PydanticObjectId
from pydantic import SecretStr

from tree.config.settings import settings
from tree.data.web.web_scraper_api import BrightDataTimeoutError
from tree.data.web.web_unlocker import (
    BrightDataConfigurationError,
    BrightDataRequestError,
)
from tree.data.youtube.types import (
    FetchedTranscript,
    TranscriptSegment,
    VideoMetadata,
)
from tree.data.youtube.youtube_pipeline import (
    _bulk_build_and_load,
    build_batch,
    fetch_transcripts_batch,
    load_batch,
)
from tree.entities.documents import Document, SourceType

VIDEO_IDS = ["eYaWxljC4sA", "AAAaaaBBBcc", "ZZZzzzYYYxx"]
PUBLISH_DATE = datetime(2024, 3, 1, 12, 0, tzinfo=UTC)

_INGEST_LOGGER = youtube_pipeline.logger.name


def _canonical(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def _make_transcript(
    video_id: str,
    plain_text: str = "spoken words",
    metadata: VideoMetadata | None = None,
) -> FetchedTranscript:
    return FetchedTranscript(
        metadata=metadata or VideoMetadata(video_id=video_id),
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


def _items(video_ids: list[str] = VIDEO_IDS) -> list[tuple[str, VideoMetadata]]:
    return [(_canonical(vid), VideoMetadata(video_id=vid)) for vid in video_ids]


class _FakeFetcher:
    """Programmable transcript fetcher recording every ``fetch_many`` call.

    Stands in for either backend: same
    ``async def fetch_many(urls) -> list[FetchedTranscript | None]`` contract,
    swapped in by patching the construction point in ``youtube_pipeline``. Raises
    ``error`` instead of answering when one is supplied (the batch-WIDE failure
    shape Bright Data propagates).
    """

    def __init__(
        self,
        results_per_id: dict[str, FetchedTranscript | None] | None = None,
        *,
        error: BaseException | None = None,
    ) -> None:
        self._results_per_id = results_per_id or {}
        self._error = error
        self.calls: list[list[str]] = []

    async def fetch_many(
        self, video_urls_or_ids: list[str]
    ) -> list[FetchedTranscript | None]:
        self.calls.append(list(video_urls_or_ids))
        if self._error is not None:
            raise self._error
        results: list[FetchedTranscript | None] = []
        for url in video_urls_or_ids:
            match: FetchedTranscript | None = None
            for vid, payload in self._results_per_id.items():
                if vid in url:
                    match = payload
                    break
            results.append(match)
        return results


def _configure_backends(
    mocker, *, brightdata: bool = True, gemini: bool = True
) -> None:
    """Fake credential PRESENCE — the only backend switch (ADR-004, Decision 7)."""

    mocker.patch.object(
        settings, "brightdata_api_key", SecretStr("bd-key" if brightdata else "")
    )
    mocker.patch.object(
        settings, "google_api_key", SecretStr("gemini-key" if gemini else "")
    )


def _patch_failing_httpx_client(mocker, error: Exception) -> None:
    """Make every Web Scraper API request fail with ``error``, at the HTTP seam."""

    client = AsyncMock()
    client.post = AsyncMock(side_effect=error)
    client.get = AsyncMock(side_effect=error)

    client_cm = MagicMock()
    client_cm.__aenter__ = AsyncMock(return_value=client)
    client_cm.__aexit__ = AsyncMock(return_value=None)

    mocker.patch(
        "tree.data.web.web_scraper_api.httpx.AsyncClient", return_value=client_cm
    )


def _patch_fetchers(
    mocker,
    brightdata: _FakeFetcher | None = None,
    gemini: _FakeFetcher | None = None,
):
    """Patch BOTH in-task construction points; return the two ctor mocks."""

    brightdata_ctor = mocker.patch.object(
        youtube_pipeline, "BrightDataTranscriptFetcher", return_value=brightdata
    )
    gemini_ctor = mocker.patch.object(
        youtube_pipeline, "GeminiTranscriptFetcher", return_value=gemini
    )
    return brightdata_ctor, gemini_ctor


class TestTaskMetadata:
    """Retry grain lives on the batch ETL-phase tasks (mirrors test_web_pipeline)."""

    def test_fetch_transcripts_batch_retries(self) -> None:
        # Tier B — CAPPED at 2: ~173 s per Bright Data collection plus per-record
        # billing, so 5 retries would be ~15 min and 5 paid collections.
        assert fetch_transcripts_batch.retries == 2
        assert fetch_transcripts_batch.retry_delay_seconds == 5
        assert fetch_transcripts_batch.name == "fetch-youtube-transcripts-batch"

    def test_build_batch_retries(self) -> None:
        # Tier D — pure build, no I/O: a retry reproduces the failure exactly.
        assert build_batch.retries == 0
        assert build_batch.name == "build-youtube-batch"

    def test_load_batch_retries(self) -> None:
        # Tier F (idempotent Mongo write) → 3 x 5 s = 15 s (ADR-002 #096).
        assert load_batch.retries == 3
        assert load_batch.retry_delay_seconds == 5
        assert load_batch.name == "load-youtube-batch"


class TestCredentialGate:
    """Neither backend configured is a hard, up-front failure (ADR-004, §7)."""

    async def test_raises_naming_both_env_vars_and_the_example_file(
        self, mocker
    ) -> None:
        _configure_backends(mocker, brightdata=False, gemini=False)
        _patch_fetchers(mocker)

        with pytest.raises(RuntimeError) as excinfo:
            await fetch_transcripts_batch.fn(_items())

        message = str(excinfo.value)
        assert "BRIGHTDATA_API_KEY" in message
        assert "GOOGLE_API_KEY" in message
        assert ".env.example" in message

    async def test_raises_before_any_fetcher_is_constructed(self, mocker) -> None:
        _configure_backends(mocker, brightdata=False, gemini=False)
        brightdata_ctor, gemini_ctor = _patch_fetchers(mocker)

        with pytest.raises(RuntimeError):
            await fetch_transcripts_batch.fn(_items())

        # No billable call can have happened: neither backend even exists.
        brightdata_ctor.assert_not_called()
        gemini_ctor.assert_not_called()

    async def test_brightdata_only_setup_does_not_raise(self, mocker) -> None:
        _configure_backends(mocker, brightdata=True, gemini=False)
        brightdata = _FakeFetcher({vid: _make_transcript(vid) for vid in VIDEO_IDS})
        _, gemini_ctor = _patch_fetchers(mocker, brightdata=brightdata)

        transcribed, failed = await fetch_transcripts_batch.fn(_items())

        assert len(transcribed) == 3
        assert failed == []
        # The unavailable backend is NEVER constructed (its ctor would raise).
        gemini_ctor.assert_not_called()


class TestFallbackChain:
    """Bright Data primary over ALL urls; Gemini second over ONLY the misses."""

    async def test_brightdata_runs_first_over_every_url(self, mocker) -> None:
        _configure_backends(mocker)
        brightdata = _FakeFetcher({vid: _make_transcript(vid) for vid in VIDEO_IDS})
        _, gemini_ctor = _patch_fetchers(mocker, brightdata=brightdata)

        transcribed, failed = await fetch_transcripts_batch.fn(_items())

        assert len(brightdata.calls) == 1
        assert brightdata.calls[0] == [_canonical(vid) for vid in VIDEO_IDS]
        assert [url for url, _, _ in transcribed] == [
            _canonical(vid) for vid in VIDEO_IDS
        ]
        assert failed == []
        # Nothing missing → the paid fallback is never even constructed.
        gemini_ctor.assert_not_called()

    async def test_fetchers_are_constructed_inside_the_task(self, mocker) -> None:
        _configure_backends(mocker)
        brightdata = _FakeFetcher({VIDEO_IDS[0]: None})
        gemini = _FakeFetcher({VIDEO_IDS[0]: _make_transcript(VIDEO_IDS[0])})
        brightdata_ctor, gemini_ctor = _patch_fetchers(mocker, brightdata, gemini)

        await fetch_transcripts_batch.fn(_items([VIDEO_IDS[0]]))

        # No fetcher is ever a task ARGUMENT: both are built with no inputs here.
        brightdata_ctor.assert_called_once_with()
        gemini_ctor.assert_called_once_with()

    async def test_gemini_receives_exactly_the_transcript_less_subset(
        self, mocker
    ) -> None:
        _configure_backends(mocker)
        brightdata = _FakeFetcher(
            {
                VIDEO_IDS[0]: _make_transcript(VIDEO_IDS[0]),
                VIDEO_IDS[1]: None,  # only this slot is transcript-less
                VIDEO_IDS[2]: _make_transcript(VIDEO_IDS[2]),
            }
        )
        gemini = _FakeFetcher({VIDEO_IDS[1]: _make_transcript(VIDEO_IDS[1])})
        _patch_fetchers(mocker, brightdata, gemini)

        transcribed, failed = await fetch_transcripts_batch.fn(_items())

        assert len(gemini.calls) == 1  # ONE second bulk fetch
        assert gemini.calls[0] == [_canonical(VIDEO_IDS[1])]
        assert [url for url, _, _ in transcribed] == [
            _canonical(vid) for vid in VIDEO_IDS
        ]
        assert failed == []

    @pytest.mark.parametrize(
        ("error", "reason"),
        [
            (BrightDataConfigurationError("no key"), "brightdata_not_configured"),
            (BrightDataRequestError("trigger rejected"), "brightdata_request_error"),
            (BrightDataTimeoutError("still running"), "brightdata_timeout"),
        ],
    )
    async def test_batch_wide_trigger_sends_the_whole_batch_to_gemini(
        self, mocker, caplog, error: Exception, reason: str
    ) -> None:
        _configure_backends(mocker)
        brightdata = _FakeFetcher(error=error)
        gemini = _FakeFetcher({vid: _make_transcript(vid) for vid in VIDEO_IDS})
        _patch_fetchers(mocker, brightdata, gemini)

        with caplog.at_level(logging.WARNING, logger=_INGEST_LOGGER):
            transcribed, failed = await fetch_transcripts_batch.fn(_items())

        # The WHOLE batch falls back; the task does not fail.
        assert gemini.calls == [[_canonical(vid) for vid in VIDEO_IDS]]
        assert len(transcribed) == 3
        assert failed == []
        assert f"reason={reason}" in caplog.text

    async def test_missing_brightdata_credentials_send_the_whole_batch_to_gemini(
        self, mocker
    ) -> None:
        _configure_backends(mocker, brightdata=False, gemini=True)
        gemini = _FakeFetcher({vid: _make_transcript(vid) for vid in VIDEO_IDS})
        brightdata_ctor, _ = _patch_fetchers(mocker, gemini=gemini)

        transcribed, failed = await fetch_transcripts_batch.fn(_items())

        brightdata_ctor.assert_not_called()
        assert gemini.calls == [[_canonical(vid) for vid in VIDEO_IDS]]
        assert len(transcribed) == 3
        assert failed == []

    async def test_empty_batch_constructs_no_fetcher(self, mocker) -> None:
        _configure_backends(mocker)
        brightdata_ctor, gemini_ctor = _patch_fetchers(mocker)

        transcribed, failed = await fetch_transcripts_batch.fn([])

        assert (transcribed, failed) == ([], [])
        brightdata_ctor.assert_not_called()
        gemini_ctor.assert_not_called()


class TestBrightDataTransportFailure:
    """A Bright Data OUTAGE falls back like a poll timeout does (#094).

    The only test in this module that runs the REAL
    ``BrightDataTranscriptFetcher`` + ``collect``: the failure it proves is a
    typing question at the client's HTTP seam, so injecting it at the fetcher
    construction point (as every other test here does) would assume away the
    very thing under test. Still fully mocked — the injection point is
    ``httpx.AsyncClient`` inside ``web_scraper_api``, so no request leaves the
    process.
    """

    @pytest.mark.parametrize(
        "error_type",
        [httpx.ConnectError, httpx.TimeoutException],
        ids=["ConnectError", "TimeoutException"],
    )
    async def test_transport_failure_sends_the_whole_batch_to_gemini(
        self, mocker, caplog, error_type: type[httpx.TransportError]
    ) -> None:
        _configure_backends(mocker)
        _patch_failing_httpx_client(mocker, error_type("bright data is unreachable"))
        gemini = _FakeFetcher({vid: _make_transcript(vid) for vid in VIDEO_IDS})
        mocker.patch.object(
            youtube_pipeline, "GeminiTranscriptFetcher", return_value=gemini
        )

        with caplog.at_level(logging.WARNING, logger=_INGEST_LOGGER):
            transcribed, failed = await fetch_transcripts_batch.fn(_items())

        # The task does NOT fail: the whole batch reaches the paid fallback.
        assert gemini.calls == [[_canonical(vid) for vid in VIDEO_IDS]]
        assert len(transcribed) == 3
        assert failed == []
        assert "reason=brightdata_request_error" in caplog.text
        assert "consumes Gemini tokens and incurs API cost" in caplog.text


class TestGeminiFallbackFailure:
    """An UNEXPECTED Gemini exception must not discard paid Bright Data work (#095).

    Before #095 anything escaping the fallback ``fetch_many`` failed the task, and
    ``retries=2`` then re-ran — and RE-BILLED — the Bright Data collection whose
    transcripts were already in hand. The chain now absorbs it exactly like a
    batch-wide Bright Data failure: successes land, the un-rescued slots become
    ``no_transcript:`` rows, and the failure is a WARNING.
    """

    @pytest.mark.parametrize(
        "error",
        [RuntimeError("gemini 500"), ValueError("unexpected response shape")],
        ids=["RuntimeError", "ValueError"],
    )
    async def test_brightdata_transcripts_survive_a_gemini_exception(
        self, mocker, error: Exception
    ) -> None:
        _configure_backends(mocker)
        brightdata = _FakeFetcher(
            {
                VIDEO_IDS[0]: _make_transcript(VIDEO_IDS[0]),
                VIDEO_IDS[1]: None,
                VIDEO_IDS[2]: None,
            }
        )
        _patch_fetchers(mocker, brightdata, _FakeFetcher(error=error))

        transcribed, failed = await fetch_transcripts_batch.fn(_items())

        # The task completes: the paid transcript is kept, only the misses fail.
        assert [url for url, _, _ in transcribed] == [_canonical(VIDEO_IDS[0])]
        assert [url for url, _, _ in failed] == [
            _canonical(VIDEO_IDS[1]),
            _canonical(VIDEO_IDS[2]),
        ]

    async def test_un_rescued_slots_name_the_failed_gemini_call(self, mocker) -> None:
        _configure_backends(mocker)
        brightdata = _FakeFetcher({VIDEO_IDS[0]: None})
        _patch_fetchers(mocker, brightdata, _FakeFetcher(error=RuntimeError("boom")))

        _, failed = await fetch_transcripts_batch.fn(_items([VIDEO_IDS[0]]))

        error = failed[0][2]
        assert error == (
            "no_transcript: brightdata returned empty; gemini unavailable "
            "(fetch failed)"
        )
        # Normalized: a code + a message, never a raw exception dump.
        assert "RuntimeError" not in error
        assert "Traceback" not in error

    async def test_warns_naming_the_failure_and_the_slot_count(
        self, mocker, caplog
    ) -> None:
        _configure_backends(mocker)
        brightdata = _FakeFetcher(
            {
                VIDEO_IDS[0]: _make_transcript(VIDEO_IDS[0]),
                VIDEO_IDS[1]: None,
                VIDEO_IDS[2]: None,
            }
        )
        _patch_fetchers(mocker, brightdata, _FakeFetcher(error=RuntimeError("boom")))

        with caplog.at_level(logging.WARNING, logger=_INGEST_LOGGER):
            await fetch_transcripts_batch.fn(_items())

        failures = [
            record.getMessage()
            for record in caplog.records
            if record.levelno == logging.WARNING
            and "Gemini fallback failed" in record.getMessage()
        ]
        assert len(failures) == 1
        assert "2/3 videos" in failures[0]
        assert "RuntimeError" in failures[0]
        assert "boom" in failures[0]
        assert "ingest_error" in failures[0]

    @pytest.mark.parametrize(
        "error",
        [asyncio.CancelledError(), KeyboardInterrupt()],
        ids=["CancelledError", "KeyboardInterrupt"],
    )
    async def test_base_exceptions_still_propagate(
        self, mocker, error: BaseException
    ) -> None:
        """Only ``Exception`` is absorbed: cancellation/interrupt must NOT be.

        ``asyncio.CancelledError`` inherits from ``BaseException`` (3.8+), so a
        cancelled batch still cancels instead of quietly writing failure rows.
        """

        _configure_backends(mocker)
        brightdata = _FakeFetcher({VIDEO_IDS[0]: None})
        _patch_fetchers(mocker, brightdata, _FakeFetcher(error=error))

        with pytest.raises(type(error)):
            await fetch_transcripts_batch.fn(_items([VIDEO_IDS[0]]))

    async def test_batch_wide_brightdata_failure_plus_dead_gemini_names_both(
        self, mocker
    ) -> None:
        _configure_backends(mocker)
        brightdata = _FakeFetcher(error=BrightDataTimeoutError("still running"))
        _patch_fetchers(mocker, brightdata, _FakeFetcher(error=RuntimeError("boom")))

        transcribed, failed = await fetch_transcripts_batch.fn(_items([VIDEO_IDS[0]]))

        assert transcribed == []
        assert failed[0][2] == (
            "no_transcript: brightdata unavailable (poll timeout); "
            "gemini unavailable (fetch failed)"
        )


class TestFallbackWarnings:
    """Every fallback is an explicit, costed WARNING (ADR-004, Decision 3)."""

    async def test_warning_names_reason_slot_count_and_gemini_cost(
        self, mocker, caplog
    ) -> None:
        _configure_backends(mocker)
        brightdata = _FakeFetcher(
            {
                VIDEO_IDS[0]: _make_transcript(VIDEO_IDS[0]),
                VIDEO_IDS[1]: None,
                VIDEO_IDS[2]: _make_transcript(VIDEO_IDS[2]),
            }
        )
        gemini = _FakeFetcher({VIDEO_IDS[1]: _make_transcript(VIDEO_IDS[1])})
        _patch_fetchers(mocker, brightdata, gemini)

        with caplog.at_level(logging.WARNING, logger=_INGEST_LOGGER):
            await fetch_transcripts_batch.fn(_items())

        fallback_warnings = [
            record.getMessage()
            for record in caplog.records
            if record.levelno == logging.WARNING
            and "Falling back to Gemini" in record.getMessage()
        ]
        assert len(fallback_warnings) == 1
        message = fallback_warnings[0]
        assert "1/3 videos" in message
        assert "reason=no_brightdata_transcript" in message
        assert "consumes Gemini tokens and incurs API cost" in message

    async def test_warns_per_batch_when_gemini_cannot_rescue_the_misses(
        self, mocker, caplog
    ) -> None:
        _configure_backends(mocker, brightdata=True, gemini=False)
        brightdata = _FakeFetcher(
            {
                VIDEO_IDS[0]: _make_transcript(VIDEO_IDS[0]),
                VIDEO_IDS[1]: None,
                VIDEO_IDS[2]: None,
            }
        )
        _patch_fetchers(mocker, brightdata=brightdata)

        with caplog.at_level(logging.WARNING, logger=_INGEST_LOGGER):
            await fetch_transcripts_batch.fn(_items())

        no_fallback = [
            record.getMessage()
            for record in caplog.records
            if record.levelno == logging.WARNING
            and "No Gemini fallback" in record.getMessage()
        ]
        assert len(no_fallback) == 1
        assert "2/3 videos" in no_fallback[0]
        assert "GOOGLE_API_KEY" in no_fallback[0]

    async def test_exhausted_slot_warns_naming_the_video(self, mocker, caplog) -> None:
        _configure_backends(mocker)
        brightdata = _FakeFetcher({vid: None for vid in VIDEO_IDS})
        gemini = _FakeFetcher(
            {
                VIDEO_IDS[0]: _make_transcript(VIDEO_IDS[0]),
                VIDEO_IDS[1]: None,
                VIDEO_IDS[2]: _make_transcript(VIDEO_IDS[2]),
            }
        )
        _patch_fetchers(mocker, brightdata, gemini)

        with caplog.at_level(logging.WARNING, logger=_INGEST_LOGGER):
            await fetch_transcripts_batch.fn(_items())

        exhausted = [
            record.getMessage()
            for record in caplog.records
            if record.levelno == logging.WARNING
            and "No transcript for" in record.getMessage()
        ]
        assert len(exhausted) == 1
        assert _canonical(VIDEO_IDS[1]) in exhausted[0]


class TestNoTranscriptErrorStrings:
    """Normalized ``code: message`` strings naming the chain that actually ran."""

    async def test_both_backends_empty(self, mocker) -> None:
        _configure_backends(mocker)
        brightdata = _FakeFetcher({VIDEO_IDS[0]: None})
        gemini = _FakeFetcher({VIDEO_IDS[0]: None})
        _patch_fetchers(mocker, brightdata, gemini)

        transcribed, failed = await fetch_transcripts_batch.fn(_items([VIDEO_IDS[0]]))

        assert transcribed == []
        url, metadata, error = failed[0]
        assert url == _canonical(VIDEO_IDS[0])
        assert metadata is not None and metadata.video_id == VIDEO_IDS[0]
        assert error == "no_transcript: brightdata + gemini both returned empty"

    async def test_brightdata_only_setup_names_the_absent_fallback(
        self, mocker
    ) -> None:
        _configure_backends(mocker, brightdata=True, gemini=False)
        brightdata = _FakeFetcher({VIDEO_IDS[0]: None})
        _patch_fetchers(mocker, brightdata=brightdata)

        _, failed = await fetch_transcripts_batch.fn(_items([VIDEO_IDS[0]]))

        assert failed[0][2] == (
            "no_transcript: brightdata returned empty; gemini not configured"
        )

    async def test_gemini_only_setup_names_the_absent_primary(self, mocker) -> None:
        _configure_backends(mocker, brightdata=False, gemini=True)
        gemini = _FakeFetcher({VIDEO_IDS[0]: None})
        _patch_fetchers(mocker, gemini=gemini)

        _, failed = await fetch_transcripts_batch.fn(_items([VIDEO_IDS[0]]))

        assert failed[0][2] == (
            "no_transcript: brightdata not configured; gemini returned empty"
        )

    async def test_batch_wide_timeout_names_the_poll_timeout(self, mocker) -> None:
        _configure_backends(mocker)
        brightdata = _FakeFetcher(error=BrightDataTimeoutError("still running"))
        gemini = _FakeFetcher({VIDEO_IDS[0]: None})
        _patch_fetchers(mocker, brightdata, gemini)

        _, failed = await fetch_transcripts_batch.fn(_items([VIDEO_IDS[0]]))

        error = failed[0][2]
        assert error == (
            "no_transcript: brightdata unavailable (poll timeout); "
            "gemini returned empty"
        )
        # Normalized: a code + a message, never a raw exception dump.
        assert "BrightDataTimeoutError" not in error
        assert "Traceback" not in error

    async def test_one_dead_video_does_not_sink_the_batch(self, mocker) -> None:
        _configure_backends(mocker)
        brightdata = _FakeFetcher(
            {
                VIDEO_IDS[0]: _make_transcript(VIDEO_IDS[0]),
                VIDEO_IDS[1]: None,
                VIDEO_IDS[2]: _make_transcript(VIDEO_IDS[2]),
            }
        )
        gemini = _FakeFetcher({VIDEO_IDS[1]: None})
        _patch_fetchers(mocker, brightdata, gemini)

        transcribed, failed = await fetch_transcripts_batch.fn(_items())

        assert [url for url, _, _ in transcribed] == [
            _canonical(VIDEO_IDS[0]),
            _canonical(VIDEO_IDS[2]),
        ]
        assert [url for url, _, _ in failed] == [_canonical(VIDEO_IDS[1])]


class TestBuildBatch:
    """Pure map to Documents: merged metadata for hits, error rows for misses."""

    async def test_builds_one_document_per_transcribed_slot(self) -> None:
        user_id = PydanticObjectId()
        transcribed = [
            (
                _canonical(vid),
                VideoMetadata(video_id=vid, title=f"T {vid}"),
                _make_transcript(vid),
            )
            for vid in VIDEO_IDS
        ]

        docs = await build_batch.fn(transcribed, [], user_id)

        assert len(docs) == 3
        assert [d.source_uri for d in docs] == [_canonical(vid) for vid in VIDEO_IDS]
        assert all(d.source_type == SourceType.YOUTUBE for d in docs)
        assert all(d.user_id == user_id for d in docs)
        assert all(d.ingest_error is None for d in docs)
        assert docs[0].title == f"T {VIDEO_IDS[0]}"

    async def test_brightdata_metadata_wins_over_the_base(self) -> None:
        base = VideoMetadata(video_id=VIDEO_IDS[0], title="oEmbed title", channel="oE")
        record_metadata = VideoMetadata(
            video_id=VIDEO_IDS[0],
            title="Bright Data title",
            channel="Bright Data channel",
        )
        transcribed = [
            (
                _canonical(VIDEO_IDS[0]),
                base,
                _make_transcript(VIDEO_IDS[0], metadata=record_metadata),
            )
        ]

        docs = await build_batch.fn(transcribed, [], PydanticObjectId())

        assert docs[0].title == "Bright Data title"
        assert docs[0].authors == ["Bright Data channel"]

    async def test_base_metadata_survives_where_brightdata_is_null(self) -> None:
        base = VideoMetadata(video_id=VIDEO_IDS[0], title="oEmbed title", channel="oE")
        record_metadata = VideoMetadata(
            video_id=VIDEO_IDS[0], publish_date=PUBLISH_DATE
        )
        transcribed = [
            (
                _canonical(VIDEO_IDS[0]),
                base,
                _make_transcript(VIDEO_IDS[0], metadata=record_metadata),
            )
        ]

        docs = await build_batch.fn(transcribed, [], PydanticObjectId())

        assert docs[0].title == "oEmbed title"
        assert docs[0].authors == ["oE"]

    async def test_document_date_is_the_real_publish_date_not_ingest_time(self) -> None:
        # The ADR-004 §5 side effect: Bright Data's `date_posted` reaches
        # `build_document`, so `date` stops falling back to `now(UTC)`.
        base = VideoMetadata(video_id=VIDEO_IDS[0], title="oEmbed title")
        record_metadata = VideoMetadata(
            video_id=VIDEO_IDS[0], publish_date=PUBLISH_DATE
        )
        transcribed = [
            (
                _canonical(VIDEO_IDS[0]),
                base,
                _make_transcript(VIDEO_IDS[0], metadata=record_metadata),
            )
        ]

        docs = await build_batch.fn(transcribed, [], PydanticObjectId())

        assert docs[0].date == PUBLISH_DATE
        assert docs[0].date.tzinfo is not None

    async def test_gemini_branch_leaves_base_metadata_intact(self) -> None:
        # The Gemini transcript carries ONLY `video_id`, so the feed/oEmbed base
        # must reach the Document untouched.
        base = VideoMetadata(
            video_id=VIDEO_IDS[0],
            title="Feed title",
            channel="Feed channel",
            publish_date=PUBLISH_DATE,
        )
        transcribed = [(_canonical(VIDEO_IDS[0]), base, _make_transcript(VIDEO_IDS[0]))]

        docs = await build_batch.fn(transcribed, [], PydanticObjectId())

        assert docs[0].title == "Feed title"
        assert docs[0].authors == ["Feed channel"]
        assert docs[0].date == PUBLISH_DATE

    async def test_exhausted_slot_becomes_a_no_transcript_row(self) -> None:
        user_id = PydanticObjectId()
        failed = [
            (
                _canonical(VIDEO_IDS[0]),
                VideoMetadata(video_id=VIDEO_IDS[0], title="Feed title", channel="C"),
                "no_transcript: brightdata + gemini both returned empty",
            )
        ]

        docs = await build_batch.fn([], failed, user_id)

        assert len(docs) == 1
        row = docs[0]
        assert row.source_type == SourceType.YOUTUBE
        assert row.source_uri == _canonical(VIDEO_IDS[0])
        assert row.content is None
        assert row.title == "Feed title"
        assert row.authors == ["C"]
        assert row.ingest_error == (
            "no_transcript: brightdata + gemini both returned empty"
        )

    async def test_unresolvable_input_row_keeps_the_raw_source_uri(self) -> None:
        raw_input = "https://example.com/not-a-video"
        failed = [(raw_input, None, "invalid_url: no video id in input")]

        docs = await build_batch.fn([], failed, PydanticObjectId())

        assert docs[0].source_uri == raw_input
        assert docs[0].content is None
        assert docs[0].ingest_error == "invalid_url: no video id in input"

    async def test_empty_batch_returns_empty(self) -> None:
        assert await build_batch.fn([], [], PydanticObjectId()) == []


class TestLoadBatch:
    """DB Load over a single isolated gather via the shared ``load_video_document``."""

    async def test_returns_persisted_subset_dropping_duplicates(self, mocker) -> None:
        doc_a = _make_doc(VIDEO_IDS[0])
        doc_b = _make_doc(VIDEO_IDS[1])
        load_mock = mocker.patch.object(
            youtube_pipeline,
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
            youtube_pipeline,
            "load_video_document",
            mocker.AsyncMock(side_effect=[doc_a, RuntimeError("bad load")]),
        )

        result = await load_batch.fn([doc_a, doc_b])

        # The failing element is logged + skipped, NOT propagated.
        assert result == [doc_a]

    async def test_empty_batch_returns_empty(self, mocker) -> None:
        load_mock = mocker.patch.object(
            youtube_pipeline, "load_video_document", mocker.AsyncMock()
        )

        result = await load_batch.fn([])

        assert result == []
        load_mock.assert_not_awaited()

    async def test_success_replaces_a_previously_errored_row(
        self, mocker, caplog
    ) -> None:
        # #089 semantics, end-to-end through the real ``load_video_document``: an
        # errored row is replaceable, and the re-attempt is a WARNING.
        user_id = PydanticObjectId()
        existing = Document(
            source_type=SourceType.YOUTUBE,
            source_uri=_canonical(VIDEO_IDS[0]),
            user_id=user_id,
            ingest_error="no_transcript: brightdata + gemini both returned empty",
        )
        existing.id = PydanticObjectId()
        mocker.patch(
            "tree.data.youtube.youtube.Document.find_one",
            new_callable=mocker.AsyncMock,
            return_value=existing,
        )
        replace = mocker.patch(
            "tree.data.youtube.youtube.Document.replace",
            new_callable=mocker.AsyncMock,
        )
        incoming = _make_doc(VIDEO_IDS[0])
        incoming.user_id = user_id

        with caplog.at_level(logging.WARNING, logger="tree.data.youtube.youtube"):
            result = await load_batch.fn([incoming])

        assert result == [incoming]
        assert incoming.id == existing.id
        replace.assert_awaited_once()
        assert "Re-attempting previously failed ingest" in caplog.text


class TestBulkBuildAndLoad:
    """The shared core: fallback chain → build (+ error rows) → isolated load."""

    async def test_is_a_plain_function_not_a_flow_or_task(self) -> None:
        # The core carries NO Prefect decorator (no ``.fn`` / ``.name`` attrs).
        assert not hasattr(_bulk_build_and_load, "fn")

    async def test_one_bulk_fetch_builds_per_slot_loads_isolated(self, mocker) -> None:
        _configure_backends(mocker)
        user_id = PydanticObjectId()
        brightdata = _FakeFetcher({vid: _make_transcript(vid) for vid in VIDEO_IDS})
        _patch_fetchers(mocker, brightdata=brightdata)

        built_docs = [_make_doc(vid) for vid in VIDEO_IDS]
        load_mock = mocker.patch.object(
            youtube_pipeline,
            "load_video_document",
            mocker.AsyncMock(
                side_effect=[built_docs[0], RuntimeError("dup"), built_docs[2]]
            ),
        )
        mocker.patch.object(
            youtube_pipeline, "build_document", mocker.Mock(side_effect=built_docs)
        )

        result = await _bulk_build_and_load(_items(), user_id)

        assert len(brightdata.calls) == 1
        assert brightdata.calls[0] == [_canonical(vid) for vid in VIDEO_IDS]
        # Load isolation: the 2 successes returned, the RuntimeError slot dropped.
        assert result == [built_docs[0], built_docs[2]]
        assert load_mock.await_count == 3

    async def test_persists_failure_rows_but_returns_only_ingested_documents(
        self, mocker
    ) -> None:
        _configure_backends(mocker, brightdata=True, gemini=False)
        brightdata = _FakeFetcher(
            {VIDEO_IDS[0]: _make_transcript(VIDEO_IDS[0]), VIDEO_IDS[1]: None}
        )
        _patch_fetchers(mocker, brightdata=brightdata)
        load_mock = mocker.patch.object(
            youtube_pipeline,
            "load_video_document",
            mocker.AsyncMock(side_effect=lambda doc: doc),
        )

        result = await _bulk_build_and_load(_items(VIDEO_IDS[:2]), PydanticObjectId())

        # BOTH documents go through the normal load path…
        loaded = [call.args[0] for call in load_mock.await_args_list]
        assert len(loaded) == 2
        failure_rows = [doc for doc in loaded if doc.ingest_error is not None]
        assert len(failure_rows) == 1
        assert failure_rows[0].source_uri == _canonical(VIDEO_IDS[1])
        # …but only the real ingest is reported as ingested.
        assert [doc.source_uri for doc in result] == [_canonical(VIDEO_IDS[0])]

    async def test_gemini_exception_still_persists_the_brightdata_documents(
        self, mocker
    ) -> None:
        """#095 end-to-end: a dead Gemini costs the batch nothing already paid for."""

        _configure_backends(mocker)
        brightdata = _FakeFetcher(
            {VIDEO_IDS[0]: _make_transcript(VIDEO_IDS[0]), VIDEO_IDS[1]: None}
        )
        _patch_fetchers(mocker, brightdata, _FakeFetcher(error=RuntimeError("boom")))
        load_mock = mocker.patch.object(
            youtube_pipeline,
            "load_video_document",
            mocker.AsyncMock(side_effect=lambda doc: doc),
        )

        result = await _bulk_build_and_load(_items(VIDEO_IDS[:2]), PydanticObjectId())

        loaded = [call.args[0] for call in load_mock.await_args_list]
        # The Bright Data transcript is ingested; the un-rescued slot is a row.
        assert [doc.source_uri for doc in result] == [_canonical(VIDEO_IDS[0])]
        failure_rows = [doc for doc in loaded if doc.ingest_error is not None]
        assert [doc.source_uri for doc in failure_rows] == [_canonical(VIDEO_IDS[1])]
        assert failure_rows[0].ingest_error == (
            "no_transcript: brightdata returned empty; gemini unavailable "
            "(fetch failed)"
        )

    async def test_invalid_inputs_become_rows_keyed_on_the_raw_input(
        self, mocker
    ) -> None:
        _configure_backends(mocker)
        brightdata_ctor, gemini_ctor = _patch_fetchers(mocker)
        load_mock = mocker.patch.object(
            youtube_pipeline,
            "load_video_document",
            mocker.AsyncMock(side_effect=lambda doc: doc),
        )

        result = await _bulk_build_and_load(
            [], PydanticObjectId(), invalid_inputs=["not a youtube url"]
        )

        assert result == []
        row = load_mock.await_args_list[0].args[0]
        assert row.source_uri == "not a youtube url"
        assert row.source_type == SourceType.YOUTUBE
        assert row.content is None
        assert row.ingest_error == "invalid_url: no video id in input"
        # An unresolvable input costs nothing: no backend is constructed.
        brightdata_ctor.assert_not_called()
        gemini_ctor.assert_not_called()
