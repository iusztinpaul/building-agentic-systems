"""Unit tests for the `GeminiTranscriptFetcher` paid-fallback transcript fetcher.

These tests are fully mocked: no real Gemini calls, no network. The fetcher
is exercised against a stubbed `google.genai.Client` so that the assertions
focus exclusively on the fetcher's contract (Protocol conformance, order
preservation, error semantics, and call-site shape — an
`aio.interactions.create(...)` call with an agentic `video` input).
"""

from __future__ import annotations

import asyncio
import json
import logging
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import SecretStr

from tree.data.youtube.gemini_transcript_fetcher import GeminiTranscriptFetcher

FETCHER_LOGGER = "tree.data.youtube.gemini_transcript_fetcher"

VIDEO_ID_A = "eYaWxljC4sA"
VIDEO_ID_B = "AAAaaaBBBcc"
VIDEO_ID_C = "CCCdddEEEff"


def _stub_response(text: str, *, status: str = "completed") -> SimpleNamespace:
    """Mimic the relevant attributes of a `google.genai` Interaction."""

    return SimpleNamespace(
        status=status,
        output_text=json.dumps({"transcript": text}),
        errors=None,
        model="gemini-3.8-flash",
        usage=SimpleNamespace(
            total_input_tokens=162,
            total_tool_use_tokens=812,
            total_output_tokens=113,
            total_thought_tokens=593,
            total_tokens=1680,
        ),
    )


def _video_uri(kwargs: dict[str, Any]) -> str:
    """Return the `uri` of the single `video` input of an interactions call."""

    videos = [i for i in kwargs["input"] if i.get("type") == "video"]
    assert len(videos) == 1, f"expected one video input, got {kwargs['input']!r}"
    return videos[0]["uri"]


class _FakeAsyncInteractions:
    """Async-interactions stand-in for `client.aio.interactions`."""

    def __init__(self, side_effect: Any) -> None:
        self._side_effect = side_effect
        self.calls: list[dict[str, Any]] = []
        self.in_flight = 0
        self.max_in_flight = 0
        self._lock = asyncio.Lock()

    async def create(self, **kwargs: Any) -> Any:
        async with self._lock:
            self.in_flight += 1
            if self.in_flight > self.max_in_flight:
                self.max_in_flight = self.in_flight
        try:
            self.calls.append(kwargs)
            # Yield to the loop so siblings can pile up if the semaphore allows.
            await asyncio.sleep(0)
            if callable(self._side_effect):
                value = self._side_effect(**kwargs)
                if asyncio.iscoroutine(value):
                    value = await value
            else:
                value = self._side_effect
            if isinstance(value, BaseException):
                raise value
            return value
        finally:
            async with self._lock:
                self.in_flight -= 1


class _FakeClient:
    """Minimal stand-in for `google.genai.Client` that exposes `aio.interactions`."""

    def __init__(self, side_effect: Any) -> None:
        self.aio = SimpleNamespace(interactions=_FakeAsyncInteractions(side_effect))


def _patch_genai(mocker, side_effect: Any) -> _FakeClient:
    """Patch the `genai.Client` constructor in the fetcher module."""

    fake_client = _FakeClient(side_effect)
    mocker.patch(
        "tree.data.youtube.gemini_transcript_fetcher.genai.Client",
        return_value=fake_client,
    )
    return fake_client


# --- Init guards -------------------------------------------------------------


class TestInit:
    def test_no_key_anywhere_raises(self, mocker):
        # Arrange: settings.google_api_key empty.
        mocker.patch(
            "tree.data.youtube.gemini_transcript_fetcher.settings",
            SimpleNamespace(google_api_key=SecretStr("")),
        )
        mocker.patch(
            "tree.data.youtube.gemini_transcript_fetcher.genai.Client",
            return_value=_FakeClient(_stub_response("ignored")),
        )

        with pytest.raises(RuntimeError, match=".env.example"):
            GeminiTranscriptFetcher()

    def test_explicit_key_succeeds_and_default_model(self, mocker):
        _patch_genai(mocker, _stub_response("ignored"))

        fetcher = GeminiTranscriptFetcher(api_key=SecretStr("test"))

        assert fetcher.model == "gemini-3.8-flash"

    def test_settings_key_used_when_no_explicit_key(self, mocker):
        mocker.patch(
            "tree.data.youtube.gemini_transcript_fetcher.settings",
            SimpleNamespace(google_api_key=SecretStr("from-settings")),
        )
        _patch_genai(mocker, _stub_response("ignored"))

        # Should not raise.
        fetcher = GeminiTranscriptFetcher()

        assert fetcher.model == "gemini-3.8-flash"


# --- Happy path & call-site shape -------------------------------------------


class TestFetchMany:
    async def test_happy_path_single_video(self, mocker):
        client = _patch_genai(mocker, _stub_response("hello\nworld"))
        fetcher = GeminiTranscriptFetcher(api_key=SecretStr("test"))

        results = await fetcher.fetch_many([VIDEO_ID_A])

        assert len(results) == 1
        transcript = results[0]
        assert transcript is not None
        assert transcript.metadata.video_id == VIDEO_ID_A
        assert transcript.metadata.title is None
        assert transcript.language == "en"
        assert transcript.plain_text == "hello\nworld"
        assert len(transcript.segments) == 1
        assert transcript.segments[0].text == "hello\nworld"
        assert transcript.segments[0].start_seconds == 0.0
        assert transcript.segments[0].duration_seconds == 0.0

        # Verify the call shape: one agentic video input + the text prompt,
        # not stored server-side.
        assert len(client.aio.interactions.calls) == 1
        call = client.aio.interactions.calls[0]
        assert call["model"] == "gemini-3.8-flash"
        assert call["store"] is False
        assert call["response_mime_type"] == "application/json"
        assert call["response_format"]["required"] == ["transcript"]
        video, prompt = call["input"]
        assert video == {
            "type": "video",
            "uri": "https://www.youtube.com/watch?v=" + VIDEO_ID_A,
            "processing": "agentic",
        }
        assert prompt["type"] == "text"
        assert "transcript" in prompt["text"]

    async def test_records_token_usage_on_opik_span(self, mocker):
        # Opik's genai integration never sees `interactions.create`, so the
        # fetcher records usage itself: video (tool-use) tokens count as
        # prompt tokens, thought tokens as completion tokens.
        _patch_genai(mocker, _stub_response("hello"))
        update_span = mocker.patch(
            "tree.data.youtube.gemini_transcript_fetcher.update_current_span"
        )
        fetcher = GeminiTranscriptFetcher(api_key=SecretStr("test"))

        await fetcher.fetch_many([VIDEO_ID_A])

        update_span.assert_called_once_with(
            provider="google_ai",
            model="gemini-3.8-flash",
            usage={
                "prompt_tokens": 162 + 812,
                "completion_tokens": 113 + 593,
                "total_tokens": 1680,
            },
        )

    async def test_order_preservation_with_distinct_text_per_id(self, mocker):
        ids = [VIDEO_ID_A, VIDEO_ID_B, VIDEO_ID_C]
        text_for = {
            "https://www.youtube.com/watch?v=" + VIDEO_ID_A: "alpha-text",
            "https://www.youtube.com/watch?v=" + VIDEO_ID_B: "bravo-text",
            "https://www.youtube.com/watch?v=" + VIDEO_ID_C: "charlie-text",
        }

        def _side_effect(**kwargs: Any) -> Any:
            return _stub_response(text_for[_video_uri(kwargs)])

        _patch_genai(mocker, _side_effect)
        fetcher = GeminiTranscriptFetcher(api_key=SecretStr("test"))

        results = await fetcher.fetch_many(ids)

        assert len(results) == 3
        assert [r.plain_text for r in results] == [
            "alpha-text",
            "bravo-text",
            "charlie-text",
        ]
        assert [r.metadata.video_id for r in results] == ids

    async def test_empty_response_returns_none(self, mocker, caplog):
        _patch_genai(mocker, _stub_response(""))
        fetcher = GeminiTranscriptFetcher(api_key=SecretStr("test"))

        with caplog.at_level(logging.DEBUG, logger=FETCHER_LOGGER):
            results = await fetcher.fetch_many([VIDEO_ID_A])

        assert results == [None]
        warnings = [
            r
            for r in caplog.records
            if r.name == FETCHER_LOGGER and r.levelno >= logging.WARNING
        ]
        assert warnings == [], (
            "Gemini fetcher must not emit WARNING on per-slot failure; "
            "the chain wrapper owns the user-facing warning."
        )

    async def test_whitespace_only_response_returns_none(self, mocker):
        _patch_genai(mocker, _stub_response("   \n  \t\n"))
        fetcher = GeminiTranscriptFetcher(api_key=SecretStr("test"))

        results = await fetcher.fetch_many([VIDEO_ID_A])

        assert results == [None]

    async def test_api_error_returns_none_no_exception_escapes(self, mocker, caplog):
        # First call raises (after 1 retry, also raises); second call succeeds.
        ok = _stub_response("ok-text")

        def _side_effect(**kwargs: Any) -> Any:
            if VIDEO_ID_A in _video_uri(kwargs):
                return Exception("rate limited")
            return ok

        _patch_genai(mocker, _side_effect)
        fetcher = GeminiTranscriptFetcher(api_key=SecretStr("test"))

        with caplog.at_level(logging.DEBUG, logger=FETCHER_LOGGER):
            results = await fetcher.fetch_many([VIDEO_ID_A, VIDEO_ID_B])

        assert results[0] is None
        assert results[1] is not None
        assert results[1].plain_text == "ok-text"
        warnings = [
            r
            for r in caplog.records
            if r.name == FETCHER_LOGGER and r.levelno >= logging.WARNING
        ]
        assert warnings == []

    async def test_unresolvable_input_returns_none_without_calling_gemini(
        self, mocker, caplog
    ):
        client = _patch_genai(mocker, _stub_response("never-used"))
        fetcher = GeminiTranscriptFetcher(api_key=SecretStr("test"))

        with caplog.at_level(logging.DEBUG, logger=FETCHER_LOGGER):
            results = await fetcher.fetch_many(["not-a-youtube-url"])

        assert results == [None]
        assert client.aio.interactions.calls == []
        warnings = [
            r
            for r in caplog.records
            if r.name == FETCHER_LOGGER and r.levelno >= logging.WARNING
        ]
        assert warnings == []

    @pytest.mark.parametrize(
        "output_text",
        [
            "thought\nLet's see:\n00:01 hello",  # leaked notes, not JSON
            '{"text": "hello"}',  # wrong field
        ],
    )
    async def test_malformed_transcript_object_returns_none(self, mocker, output_text):
        response = _stub_response("ignored")
        response.output_text = output_text
        _patch_genai(mocker, response)
        fetcher = GeminiTranscriptFetcher(api_key=SecretStr("test"))

        results = await fetcher.fetch_many([VIDEO_ID_A])

        assert results == [None]

    async def test_incomplete_interaction_returns_none(self, mocker, caplog):
        # A failed / cancelled interaction must not be read as a transcript,
        # even when it carries partial output text.
        _patch_genai(mocker, _stub_response("partial", status="failed"))
        fetcher = GeminiTranscriptFetcher(api_key=SecretStr("test"))

        with caplog.at_level(logging.DEBUG, logger=FETCHER_LOGGER):
            results = await fetcher.fetch_many([VIDEO_ID_A])

        assert results == [None]
        assert any("status 'failed'" in r.getMessage() for r in caplog.records)

    async def test_refusal_safety_block_returns_none(self, mocker):
        # Simulate a response object whose `.output_text` access raises.
        class _RefusalResponse:
            status = "completed"
            usage = None

            @property
            def output_text(self) -> str:
                raise ValueError("blocked by safety filter")

        _patch_genai(mocker, _RefusalResponse())
        fetcher = GeminiTranscriptFetcher(api_key=SecretStr("test"))

        results = await fetcher.fetch_many([VIDEO_ID_A])

        assert results == [None]

    async def test_concurrency_semaphore_caps_in_flight(self, mocker):
        # 10 inputs with concurrency=2 → max in-flight must never exceed 2.
        async def _slow_side_effect(**kwargs: Any) -> Any:
            await asyncio.sleep(0.02)
            return _stub_response("ok")

        client = _patch_genai(mocker, _slow_side_effect)
        # Patch create to use our slow side effect directly (the
        # _FakeAsyncInteractions already supports awaitable side effects via
        # callable that returns a coroutine; here we set it on the instance).
        client.aio.interactions._side_effect = _slow_side_effect

        fetcher = GeminiTranscriptFetcher(api_key=SecretStr("test"), concurrency=2)

        ids = [f"AAAAAAAAAA{i}" for i in range(10)]

        await asyncio.wait_for(fetcher.fetch_many(ids), timeout=5.0)

        assert client.aio.interactions.max_in_flight <= 2
        assert client.aio.interactions.max_in_flight >= 1
        assert len(client.aio.interactions.calls) == 10

    async def test_empty_input_list(self, mocker):
        client = _patch_genai(mocker, _stub_response("never"))
        fetcher = GeminiTranscriptFetcher(api_key=SecretStr("test"))

        results = await fetcher.fetch_many([])

        assert results == []
        assert client.aio.interactions.calls == []
