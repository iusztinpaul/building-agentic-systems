"""Unit tests for `BrightDataTranscriptFetcher`.

Fully mocked: the single `collect(...)` seam is patched in every test, so this
suite NEVER calls Bright Data (or Gemini) live — ADR-004, Decision 8.

The record→types mapping is asserted against
`fixtures/brightdata_youtube_snapshot.json`, a REAL snapshot captured once from
the `gd_lk56epmy2i5g7lzu0k` dataset. That fixture is the canary for a vendor
schema change: if Bright Data renames `formatted_transcript` or switches its
millisecond units, these assertions fail instead of the mapping silently
degrading.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from pydantic import SecretStr

from tree.data.web.web_scraper_api import BrightDataTimeoutError
from tree.data.web.web_unlocker import (
    BrightDataConfigurationError,
    BrightDataRequestError,
)
from tree.data.youtube.brightdata_transcript_fetcher import (
    _YOUTUBE_DATASET_ID,
    BrightDataTranscriptFetcher,
)

FETCHER_MODULE = "tree.data.youtube.brightdata_transcript_fetcher"
FETCHER_LOGGER = FETCHER_MODULE

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "brightdata_youtube_snapshot.json"

VIDEO_ID_A = "dQw4w9WgXcQ"
VIDEO_ID_B = "AAAaaaBBBcc"
VIDEO_ID_C = "CCCdddEEEff"


def _canonical(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def _record(video_id: str, *, transcript: str | None = "line one", **overrides: Any):
    """A minimal Bright Data YouTube record, shaped like the real one."""

    record: dict[str, Any] = {
        "url": _canonical(video_id),
        "video_id": video_id,
        "transcript": transcript,
        "formatted_transcript": [
            {"start_time": 1360, "end_time": 3040, "duration": 1680, "text": "line one"}
        ],
        "input": {"url": _canonical(video_id)},
    }
    record.update(overrides)
    return record


def _patch_collect(
    mocker, *, records: list[dict[str, Any]] | None = None, side_effect: Any = None
) -> AsyncMock:
    """Patch the ONE Bright Data seam the fetcher calls."""

    return mocker.patch(
        f"{FETCHER_MODULE}.collect",
        new_callable=AsyncMock,
        return_value=records if records is not None else [],
        side_effect=side_effect,
    )


def _warnings(caplog) -> list[logging.LogRecord]:
    return [
        record
        for record in caplog.records
        if record.name == FETCHER_LOGGER and record.levelno >= logging.WARNING
    ]


@pytest.fixture
def snapshot_record() -> dict[str, Any]:
    """The single REAL captured record for `dQw4w9WgXcQ`."""

    records = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    return records[0]


@pytest.fixture
def fetcher() -> BrightDataTranscriptFetcher:
    return BrightDataTranscriptFetcher(api_key=SecretStr("test-key"))


# --- Init guards -------------------------------------------------------------


class TestInit:
    def test_missing_api_key_raises_configuration_error(self, mocker) -> None:
        # Arrange
        mocker.patch(
            f"{FETCHER_MODULE}.settings",
            SimpleNamespace(brightdata_api_key=SecretStr("")),
        )

        # Act & Assert
        with pytest.raises(BrightDataConfigurationError, match="BRIGHTDATA_API_KEY"):
            BrightDataTranscriptFetcher()

    def test_api_key_resolves_from_settings(self, mocker) -> None:
        # Arrange
        mocker.patch(
            f"{FETCHER_MODULE}.settings",
            SimpleNamespace(brightdata_api_key=SecretStr("from-settings")),
        )

        # Act
        fetcher = BrightDataTranscriptFetcher()

        # Assert
        assert fetcher.timeout_seconds > 0

    def test_timing_knobs_default_from_app_config(self, mocker) -> None:
        # Arrange
        mocker.patch(
            f"{FETCHER_MODULE}.app_config",
            SimpleNamespace(
                youtube=SimpleNamespace(
                    brightdata_timeout_seconds=123.0,
                    brightdata_poll_interval_seconds=7.0,
                )
            ),
        )

        # Act
        fetcher = BrightDataTranscriptFetcher(api_key=SecretStr("test-key"))

        # Assert
        assert fetcher.timeout_seconds == 123.0
        assert fetcher.poll_interval_seconds == 7.0

    def test_explicit_timing_knobs_win_over_app_config(self) -> None:
        # Arrange & Act
        fetcher = BrightDataTranscriptFetcher(
            api_key=SecretStr("test-key"),
            timeout_seconds=42.0,
            poll_interval_seconds=1.5,
        )

        # Assert
        assert fetcher.timeout_seconds == 42.0
        assert fetcher.poll_interval_seconds == 1.5


# --- Collection shape --------------------------------------------------------


class TestCollectionShape:
    async def test_empty_input_returns_empty_without_collecting(
        self, mocker, fetcher
    ) -> None:
        # Arrange
        collect_mock = _patch_collect(mocker)

        # Act
        results = await fetcher.fetch_many([])

        # Assert
        assert results == []
        collect_mock.assert_not_awaited()

    async def test_issues_exactly_one_collection_for_all_resolvable_slots(
        self, mocker, fetcher
    ) -> None:
        # Arrange
        collect_mock = _patch_collect(
            mocker,
            records=[_record(VIDEO_ID_A), _record(VIDEO_ID_B), _record(VIDEO_ID_C)],
        )

        # Act
        await fetcher.fetch_many(
            [VIDEO_ID_A, f"https://youtu.be/{VIDEO_ID_B}", _canonical(VIDEO_ID_C)]
        )

        # Assert
        collect_mock.assert_awaited_once()
        call = collect_mock.await_args
        assert call.args[0] == _YOUTUBE_DATASET_ID
        assert call.args[1] == [
            {"url": _canonical(VIDEO_ID_A)},
            {"url": _canonical(VIDEO_ID_B)},
            {"url": _canonical(VIDEO_ID_C)},
        ]

    async def test_passes_configured_timing_knobs_to_collect(self, mocker) -> None:
        # Arrange
        fetcher = BrightDataTranscriptFetcher(
            api_key=SecretStr("test-key"),
            timeout_seconds=42.0,
            poll_interval_seconds=1.5,
        )
        collect_mock = _patch_collect(mocker, records=[_record(VIDEO_ID_A)])

        # Act
        await fetcher.fetch_many([VIDEO_ID_A])

        # Assert
        assert collect_mock.await_args.kwargs == {
            "timeout_seconds": 42.0,
            "poll_interval_seconds": 1.5,
        }

    async def test_duplicate_inputs_are_submitted_once_and_fill_both_slots(
        self, mocker, fetcher
    ) -> None:
        # Arrange — a repeated video is billed once, not twice.
        collect_mock = _patch_collect(mocker, records=[_record(VIDEO_ID_A)])

        # Act
        results = await fetcher.fetch_many([VIDEO_ID_A, _canonical(VIDEO_ID_A)])

        # Assert
        assert collect_mock.await_args.args[1] == [{"url": _canonical(VIDEO_ID_A)}]
        assert [result.metadata.video_id for result in results] == [
            VIDEO_ID_A,
            VIDEO_ID_A,
        ]

    async def test_records_are_realigned_to_input_order(self, mocker, fetcher) -> None:
        # Arrange — Bright Data returns records in an arbitrary order.
        _patch_collect(
            mocker,
            records=[
                _record(VIDEO_ID_C, transcript="charlie"),
                _record(VIDEO_ID_A, transcript="alpha"),
                _record(VIDEO_ID_B, transcript="bravo"),
            ],
        )

        # Act
        results = await fetcher.fetch_many([VIDEO_ID_A, VIDEO_ID_B, VIDEO_ID_C])

        # Assert
        assert [result.plain_text for result in results] == [
            "alpha",
            "bravo",
            "charlie",
        ]
        assert [result.metadata.video_id for result in results] == [
            VIDEO_ID_A,
            VIDEO_ID_B,
            VIDEO_ID_C,
        ]

    async def test_record_is_matched_by_input_url_when_url_is_absent(
        self, mocker, fetcher
    ) -> None:
        # Arrange
        record = _record(VIDEO_ID_A, transcript="alpha")
        del record["url"]
        _patch_collect(mocker, records=[record])

        # Act
        results = await fetcher.fetch_many([VIDEO_ID_A])

        # Assert
        assert results[0].plain_text == "alpha"


# --- Per-slot misses ---------------------------------------------------------


class TestPerSlotMisses:
    async def test_unresolvable_input_is_never_submitted(
        self, mocker, fetcher, caplog
    ) -> None:
        # Arrange — invalid inputs are billable, so they must not be collected.
        collect_mock = _patch_collect(mocker, records=[_record(VIDEO_ID_A)])

        # Act
        with caplog.at_level(logging.DEBUG, logger=FETCHER_LOGGER):
            results = await fetcher.fetch_many(["not-a-youtube-url", VIDEO_ID_A])

        # Assert
        assert results[0] is None
        assert results[1] is not None
        assert collect_mock.await_args.args[1] == [{"url": _canonical(VIDEO_ID_A)}]
        assert _warnings(caplog) == []

    async def test_all_inputs_unresolvable_skips_collection_entirely(
        self, mocker, fetcher
    ) -> None:
        # Arrange
        collect_mock = _patch_collect(mocker)

        # Act
        results = await fetcher.fetch_many(["not-a-youtube-url", ""])

        # Assert
        assert results == [None, None]
        collect_mock.assert_not_awaited()

    async def test_absent_record_yields_none_slot(self, mocker, fetcher) -> None:
        # Arrange — Bright Data returned nothing for the second video.
        _patch_collect(mocker, records=[_record(VIDEO_ID_A)])

        # Act
        results = await fetcher.fetch_many([VIDEO_ID_A, VIDEO_ID_B])

        # Assert
        assert results[0] is not None
        assert results[1] is None

    @pytest.mark.parametrize(
        "transcript",
        [None, "", "   \n\t "],
        ids=["missing", "empty", "whitespace"],
    )
    async def test_transcript_less_record_yields_none_slot(
        self, mocker, fetcher, caplog, transcript: str | None
    ) -> None:
        # Arrange
        _patch_collect(mocker, records=[_record(VIDEO_ID_A, transcript=transcript)])

        # Act
        with caplog.at_level(logging.DEBUG, logger=FETCHER_LOGGER):
            results = await fetcher.fetch_many([VIDEO_ID_A])

        # Assert
        assert results == [None]
        assert _warnings(caplog) == []


# --- Batch-wide failures -----------------------------------------------------


class TestBatchWideFailures:
    @pytest.mark.parametrize(
        "error",
        [
            BrightDataConfigurationError("BRIGHTDATA_API_KEY is not set"),
            BrightDataRequestError("HTTP 429"),
            BrightDataTimeoutError("snapshot sd_1 still running"),
        ],
        ids=["configuration", "request", "timeout"],
    )
    async def test_client_errors_propagate_instead_of_flattening_to_none(
        self, mocker, fetcher, error: Exception
    ) -> None:
        # Arrange — #092 distinguishes batch-wide triggers from per-slot misses,
        # so these must NOT be swallowed into all-`None` results.
        _patch_collect(mocker, side_effect=error)

        # Act & Assert
        with pytest.raises(type(error)):
            await fetcher.fetch_many([VIDEO_ID_A, VIDEO_ID_B])


# --- Record → types mapping (against the REAL captured snapshot) -------------


class TestRecordMapping:
    async def test_plain_text_is_the_record_transcript(
        self, mocker, fetcher, snapshot_record
    ) -> None:
        # Arrange
        _patch_collect(mocker, records=[snapshot_record])

        # Act
        results = await fetcher.fetch_many([VIDEO_ID_A])

        # Assert
        assert results[0].plain_text == snapshot_record["transcript"]

    async def test_segments_convert_milliseconds_to_seconds(
        self, mocker, fetcher, snapshot_record
    ) -> None:
        # Arrange
        _patch_collect(mocker, records=[snapshot_record])

        # Act
        results = await fetcher.fetch_many([VIDEO_ID_A])

        # Assert
        segments = results[0].segments
        assert len(segments) == len(snapshot_record["formatted_transcript"])
        assert segments[0].start_seconds == 1.36
        assert segments[0].duration_seconds == 1.68
        assert segments[0].text == "[♪♪♪]"

    async def test_metadata_is_mapped_from_the_record(
        self, mocker, fetcher, snapshot_record
    ) -> None:
        # Arrange
        _patch_collect(mocker, records=[snapshot_record])

        # Act
        results = await fetcher.fetch_many([VIDEO_ID_A])

        # Assert
        metadata = results[0].metadata
        assert metadata.video_id == VIDEO_ID_A
        assert metadata.title == snapshot_record["title"]
        assert metadata.channel == "Rick Astley"
        assert metadata.channel_id == "UCuAXFkgsw1L7xaCfnd5JJOw"
        assert metadata.duration_seconds == 213
        assert metadata.description == snapshot_record["description"]

    async def test_publish_date_is_tz_aware_utc(
        self, mocker, fetcher, snapshot_record
    ) -> None:
        # Arrange
        _patch_collect(mocker, records=[snapshot_record])

        # Act
        results = await fetcher.fetch_many([VIDEO_ID_A])

        # Assert
        publish_date = results[0].metadata.publish_date
        assert publish_date == datetime(2009, 10, 25, 6, 57, 33, tzinfo=UTC)
        assert publish_date.tzinfo is not None

    async def test_language_is_none_when_transcription_language_is_null(
        self, mocker, fetcher, snapshot_record
    ) -> None:
        # Arrange — the record's `transcript_language` lists 6 OFFERED languages;
        # it must never be mistaken for the transcript's own language.
        _patch_collect(mocker, records=[snapshot_record])
        assert len(snapshot_record["transcript_language"]) > 1

        # Act
        results = await fetcher.fetch_many([VIDEO_ID_A])

        # Assert
        assert results[0].language is None

    async def test_language_comes_from_transcription_language(
        self, mocker, fetcher, snapshot_record
    ) -> None:
        # Arrange
        _patch_collect(
            mocker, records=[{**snapshot_record, "transcription_language": "English"}]
        )

        # Act
        results = await fetcher.fetch_many([VIDEO_ID_A])

        # Assert
        assert results[0].language == "English"

    @pytest.mark.parametrize(
        "raw_date",
        [None, "", "not-a-date"],
        ids=["missing", "empty", "garbage"],
    )
    async def test_unparseable_date_posted_yields_no_publish_date(
        self, mocker, fetcher, raw_date: str | None
    ) -> None:
        # Arrange
        _patch_collect(mocker, records=[_record(VIDEO_ID_A, date_posted=raw_date)])

        # Act
        results = await fetcher.fetch_many([VIDEO_ID_A])

        # Assert
        assert results[0].metadata.publish_date is None

    async def test_naive_date_posted_is_read_as_utc(self, mocker, fetcher) -> None:
        # Arrange — the project forbids naive datetimes anywhere downstream.
        _patch_collect(
            mocker, records=[_record(VIDEO_ID_A, date_posted="2009-10-25T06:57:33")]
        )

        # Act
        results = await fetcher.fetch_many([VIDEO_ID_A])

        # Assert
        assert results[0].metadata.publish_date == datetime(
            2009, 10, 25, 6, 57, 33, tzinfo=UTC
        )

    async def test_channel_falls_back_to_handle_when_display_name_is_absent(
        self, mocker, fetcher
    ) -> None:
        # Arrange
        _patch_collect(
            mocker,
            records=[
                _record(VIDEO_ID_A, handle_name=None, youtuber="@RickAstleyYT"),
            ],
        )

        # Act
        results = await fetcher.fetch_many([VIDEO_ID_A])

        # Assert
        assert results[0].metadata.channel == "@RickAstleyYT"

    async def test_missing_formatted_transcript_yields_empty_segments(
        self, mocker, fetcher
    ) -> None:
        # Arrange
        _patch_collect(
            mocker,
            records=[
                _record(VIDEO_ID_A, transcript="plain", formatted_transcript=None)
            ],
        )

        # Act
        results = await fetcher.fetch_many([VIDEO_ID_A])

        # Assert
        assert results[0].plain_text == "plain"
        assert results[0].segments == []
