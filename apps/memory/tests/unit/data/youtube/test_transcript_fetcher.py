import asyncio
import logging
from types import SimpleNamespace

import pytest
from youtube_transcript_api._errors import TranscriptsDisabled

from tree.data.youtube.transcript_fetcher import (
    ChainedTranscriptFetcher,
    YoutubeTranscriptApiFetcher,
)
from tree.data.youtube.types import (
    FetchedTranscript,
    TranscriptSegment,
    VideoMetadata,
)

FETCHER_LOGGER = "tree.data.youtube.transcript_fetcher"

VIDEO_ID_A = "eYaWxljC4sA"
VIDEO_ID_B = "AAAaaaBBBcc"
VIDEO_ID_C = "CCCdddEEEff"


def _fake_raw_transcript(snippets, language_code="en"):
    """Build the duck-typed return value of YouTubeTranscriptApi().fetch."""

    return SimpleNamespace(
        snippets=[SimpleNamespace(text=t, start=s, duration=d) for t, s, d in snippets],
        language_code=language_code,
    )


def _make_fetched(video_id: str, text: str = "hello") -> FetchedTranscript:
    """Helper to build a domain FetchedTranscript for the chain tests."""

    return FetchedTranscript(
        metadata=VideoMetadata(video_id=video_id),
        segments=[
            TranscriptSegment(text=text, start_seconds=0.0, duration_seconds=1.0)
        ],
        language="en",
        plain_text=text,
    )


class _FakeFetcher:
    """Programmable fake `TranscriptFetcher` for chain tests."""

    def __init__(self, mapping: dict[tuple[str, ...], list]) -> None:
        # Keyed by tuple(inputs) so we can assert which subset was passed.
        self._mapping = mapping
        self.calls: list[list[str]] = []

    async def fetch_many(self, video_urls_or_ids):
        self.calls.append(list(video_urls_or_ids))
        key = tuple(video_urls_or_ids)
        if key not in self._mapping:
            raise AssertionError(
                f"Unexpected fake-fetcher call with inputs={video_urls_or_ids}"
            )
        return list(self._mapping[key])


# --- YoutubeTranscriptApiFetcher --------------------------------------------


class TestYoutubeTranscriptApiFetcher:
    async def test_happy_path_two_videos_in_order(self, mocker):
        # Arrange
        fetcher = YoutubeTranscriptApiFetcher()
        side_effect_by_id = {
            VIDEO_ID_A: _fake_raw_transcript(
                [("hello", 0.0, 1.5), ("world", 1.5, 2.0)]
            ),
            VIDEO_ID_B: _fake_raw_transcript([("foo", 0.0, 1.0)]),
        }

        def _fake_call(video_id):
            return side_effect_by_id[video_id]

        mocker.patch.object(fetcher, "_call_api", side_effect=_fake_call)

        # Act
        results = await fetcher.fetch_many([VIDEO_ID_A, VIDEO_ID_B])

        # Assert
        assert len(results) == 2
        assert results[0] is not None and results[1] is not None
        assert results[0].metadata.video_id == VIDEO_ID_A
        assert results[1].metadata.video_id == VIDEO_ID_B
        assert results[0].metadata.title is None  # primary leaves enrichment to ETL
        assert results[0].plain_text == "hello\nworld"
        assert results[1].plain_text == "foo"
        assert results[0].language == "en"

    async def test_missing_transcript_returns_none_silently(self, mocker, caplog):
        # Arrange
        fetcher = YoutubeTranscriptApiFetcher()
        mocker.patch.object(
            fetcher,
            "_call_api",
            side_effect=TranscriptsDisabled(VIDEO_ID_A),
        )

        # Act
        with caplog.at_level(logging.DEBUG, logger=FETCHER_LOGGER):
            results = await fetcher.fetch_many([VIDEO_ID_A])

        # Assert
        assert results == [None]
        warnings = [
            r
            for r in caplog.records
            if r.name == FETCHER_LOGGER and r.levelno >= logging.WARNING
        ]
        assert warnings == [], (
            "Primary fetcher must not emit WARNING on per-slot failure; "
            "the chain wrapper owns the user-facing warning."
        )

    async def test_unresolvable_input_returns_none_silently(self, mocker, caplog):
        # Arrange
        fetcher = YoutubeTranscriptApiFetcher()
        spy = mocker.patch.object(fetcher, "_call_api")

        # Act
        with caplog.at_level(logging.DEBUG, logger=FETCHER_LOGGER):
            results = await fetcher.fetch_many(["not-a-url"])

        # Assert
        assert results == [None]
        spy.assert_not_called()  # never even reach the backend
        warnings = [
            r
            for r in caplog.records
            if r.name == FETCHER_LOGGER and r.levelno >= logging.WARNING
        ]
        assert warnings == []

    async def test_order_preservation_with_middle_failure(self, mocker):
        # Arrange
        fetcher = YoutubeTranscriptApiFetcher()
        good_a = _fake_raw_transcript([("a", 0.0, 1.0)])
        good_c = _fake_raw_transcript([("c", 0.0, 1.0)])

        def _fake_call(video_id):
            if video_id == VIDEO_ID_A:
                return good_a
            if video_id == VIDEO_ID_C:
                return good_c
            raise TranscriptsDisabled(video_id)

        mocker.patch.object(fetcher, "_call_api", side_effect=_fake_call)

        # Act
        results = await fetcher.fetch_many([VIDEO_ID_A, VIDEO_ID_B, VIDEO_ID_C])

        # Assert
        assert len(results) == 3
        assert results[0] is not None
        assert results[1] is None
        assert results[2] is not None
        assert results[0].metadata.video_id == VIDEO_ID_A
        assert results[2].metadata.video_id == VIDEO_ID_C

    async def test_languages_default_is_english(self):
        assert YoutubeTranscriptApiFetcher().languages == ("en",)

    async def test_concurrency_does_not_deadlock(self, mocker):
        # Arrange — 10 fast calls with concurrency=2; should still complete.
        fetcher = YoutubeTranscriptApiFetcher(concurrency=2)

        async def _fast_to_thread(func, *args, **kwargs):
            return func(*args, **kwargs)

        mocker.patch("asyncio.to_thread", side_effect=_fast_to_thread)
        mocker.patch.object(
            fetcher,
            "_call_api",
            side_effect=lambda vid: _fake_raw_transcript([("ok", 0.0, 1.0)]),
        )

        # Act — produce 10 valid bare IDs.
        ids = [f"AAAAAAAAAA{i}" for i in range(10)]
        results = await asyncio.wait_for(fetcher.fetch_many(ids), timeout=5.0)

        # Assert
        assert len(results) == 10
        assert all(r is not None for r in results)


# --- ChainedTranscriptFetcher -----------------------------------------------


class TestChainedTranscriptFetcher:
    def test_empty_chain_raises(self):
        with pytest.raises(ValueError):
            ChainedTranscriptFetcher(fetchers=[])

    async def test_primary_success_no_fallback_call_no_warning(self, caplog):
        # Arrange
        t_a = _make_fetched(VIDEO_ID_A, "a")
        t_b = _make_fetched(VIDEO_ID_B, "b")
        primary = _FakeFetcher({(VIDEO_ID_A, VIDEO_ID_B): [t_a, t_b]})
        chain = ChainedTranscriptFetcher([primary])

        # Act
        with caplog.at_level(logging.WARNING, logger=FETCHER_LOGGER):
            results = await chain.fetch_many([VIDEO_ID_A, VIDEO_ID_B])

        # Assert
        assert results == [t_a, t_b]
        assert primary.calls == [[VIDEO_ID_A, VIDEO_ID_B]]
        assert [r for r in caplog.records if r.name == FETCHER_LOGGER] == []

    async def test_primary_none_then_fallback_success(self, caplog):
        # Arrange
        t_a = _make_fetched(VIDEO_ID_A, "a")
        t_c = _make_fetched(VIDEO_ID_C, "c")
        t_b_fallback = _make_fetched(VIDEO_ID_B, "b-via-fallback")
        primary = _FakeFetcher({(VIDEO_ID_A, VIDEO_ID_B, VIDEO_ID_C): [t_a, None, t_c]})
        fallback = _FakeFetcher({(VIDEO_ID_B,): [t_b_fallback]})
        chain = ChainedTranscriptFetcher([primary, fallback])

        # Act
        with caplog.at_level(logging.WARNING, logger=FETCHER_LOGGER):
            results = await chain.fetch_many([VIDEO_ID_A, VIDEO_ID_B, VIDEO_ID_C])

        # Assert
        assert results == [t_a, t_b_fallback, t_c]
        assert fallback.calls == [[VIDEO_ID_B]]

        warnings = [
            r
            for r in caplog.records
            if r.name == FETCHER_LOGGER and r.levelno == logging.WARNING
        ]
        assert len(warnings) == 1
        msg = warnings[0].getMessage()
        assert VIDEO_ID_B in msg
        assert "_FakeFetcher" in msg  # fallback class name
        assert "exhausted" not in msg.lower()

    async def test_primary_none_fallback_none_emits_advance_and_exhausted(self, caplog):
        # Arrange
        primary = _FakeFetcher({(VIDEO_ID_A,): [None]})
        fallback = _FakeFetcher({(VIDEO_ID_A,): [None]})
        chain = ChainedTranscriptFetcher([primary, fallback])

        # Act
        with caplog.at_level(logging.WARNING, logger=FETCHER_LOGGER):
            results = await chain.fetch_many([VIDEO_ID_A])

        # Assert
        assert results == [None]
        warnings = [
            r.getMessage()
            for r in caplog.records
            if r.name == FETCHER_LOGGER and r.levelno == logging.WARNING
        ]
        assert len(warnings) == 2
        assert any("falling back to" in w for w in warnings)
        assert any("exhausted" in w.lower() for w in warnings)

    async def test_single_element_chain_only_emits_exhausted_warning(self, caplog):
        # Transitional case: until #002 ships the Gemini fetcher, ETLs
        # construct a single-element chain. The exhausted-warning still
        # fires; no intermediate "falling back" warning, since there is no
        # next fetcher to advance to.
        primary = _FakeFetcher({(VIDEO_ID_A,): [None]})
        chain = ChainedTranscriptFetcher([primary])

        with caplog.at_level(logging.WARNING, logger=FETCHER_LOGGER):
            results = await chain.fetch_many([VIDEO_ID_A])

        assert results == [None]
        warnings = [
            r.getMessage()
            for r in caplog.records
            if r.name == FETCHER_LOGGER and r.levelno == logging.WARNING
        ]
        assert len(warnings) == 1
        assert "exhausted" in warnings[0].lower()
        assert "falling back" not in warnings[0].lower()

    async def test_order_preservation_across_fallback_merge(self, caplog):
        # 5 inputs, primary returns [T, None, T, None, T];
        # fallback returns [T, None] for the two None slots.
        t_p0 = _make_fetched(VIDEO_ID_A, "p0")
        t_p2 = _make_fetched(VIDEO_ID_A, "p2")
        t_p4 = _make_fetched(VIDEO_ID_A, "p4")
        t_fb_for_idx1 = _make_fetched(VIDEO_ID_B, "fb-1")

        inputs = [VIDEO_ID_A, VIDEO_ID_B, VIDEO_ID_C, "noise-1", "noise-2"]
        primary = _FakeFetcher({tuple(inputs): [t_p0, None, t_p2, None, t_p4]})
        # Fallback receives only the two None slots: [VIDEO_ID_B, "noise-1"]
        fallback = _FakeFetcher({(VIDEO_ID_B, "noise-1"): [t_fb_for_idx1, None]})
        chain = ChainedTranscriptFetcher([primary, fallback])

        with caplog.at_level(logging.WARNING, logger=FETCHER_LOGGER):
            results = await chain.fetch_many(inputs)

        assert results == [t_p0, t_fb_for_idx1, t_p2, None, t_p4]
        assert fallback.calls == [[VIDEO_ID_B, "noise-1"]]

        warnings = [
            r.getMessage()
            for r in caplog.records
            if r.name == FETCHER_LOGGER and r.levelno == logging.WARNING
        ]
        # 2 advance-to-fallback warnings (idx 1, idx 3) + 1 final
        # exhausted warning (idx 3 still None after fallback).
        assert sum("falling back" in w for w in warnings) == 2
        assert sum("exhausted" in w.lower() for w in warnings) == 1
