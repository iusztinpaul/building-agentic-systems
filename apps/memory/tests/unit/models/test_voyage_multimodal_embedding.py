from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from tree.models.exceptions import ExtractionError, ModelError
from tree.models.voyage_multimodal_embedding import VoyageMultimodalEmbeddingModel


def _mock_aiohttp_response(*, status: int = 200, json_data: dict | None = None):
    """Return a mock aiohttp response context manager."""

    mock_resp = AsyncMock()
    mock_resp.status = status
    mock_resp.json = AsyncMock(return_value=json_data or {})
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)
    return mock_resp


def _mock_aiohttp_session(mock_resp: AsyncMock):
    """Return a mock aiohttp.ClientSession wrapping a mock response."""

    mock_post = MagicMock(return_value=mock_resp)
    mock_session = AsyncMock()
    mock_session.post = mock_post
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    return mock_session, mock_post


@pytest.fixture
def model():
    return VoyageMultimodalEmbeddingModel(
        api_key="test-key",
        model="voyage-multimodal-3",
    )


class TestVoyageMultimodalInit:
    def test_raises_on_empty_api_key(self):
        with pytest.raises(ModelError, match="Voyage API key is required"):
            VoyageMultimodalEmbeddingModel(api_key="")

    def test_stores_config(self):
        m = VoyageMultimodalEmbeddingModel(
            api_key="key",
            model="voyage-multimodal-3.5",
            input_type="document",
            output_dimension=512,
        )

        assert m._model == "voyage-multimodal-3.5"
        assert m._input_type == "document"
        assert m._output_dimension == 512


class TestVoyageMultimodalEmbed:
    async def test_embed_returns_vectors(self, model):
        expected = [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]
        response_data = {
            "data": [
                {"embedding": expected[0]},
                {"embedding": expected[1]},
            ],
            "text_tokens": 10,
            "image_pixels": 0,
            "total_tokens": 10,
        }

        mock_resp = _mock_aiohttp_response(status=200, json_data=response_data)
        mock_session, mock_post = _mock_aiohttp_session(mock_resp)

        with patch("aiohttp.ClientSession") as mock_cls:
            mock_cls.return_value = mock_session
            result = await model.embed(["hello", "world"])

        assert result == expected

    async def test_embed_sends_correct_payload(self, model):
        response_data = {"data": [{"embedding": [0.1]}]}
        mock_resp = _mock_aiohttp_response(status=200, json_data=response_data)
        mock_session, mock_post = _mock_aiohttp_session(mock_resp)

        with patch("aiohttp.ClientSession") as mock_cls:
            mock_cls.return_value = mock_session
            await model.embed(["test text"])

        call_kwargs = mock_post.call_args
        payload = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")

        assert payload["model"] == "voyage-multimodal-3"
        assert payload["inputs"] == [
            {"content": [{"type": "text", "text": "test text"}]}
        ]
        assert payload["truncation"] is True
        assert "input_type" not in payload
        assert "output_dimension" not in payload

    async def test_embed_sends_optional_params(self):
        m = VoyageMultimodalEmbeddingModel(
            api_key="key",
            model="voyage-multimodal-3.5",
            input_type="query",
            output_dimension=256,
        )
        response_data = {"data": [{"embedding": [0.1]}]}
        mock_resp = _mock_aiohttp_response(status=200, json_data=response_data)
        mock_session, mock_post = _mock_aiohttp_session(mock_resp)

        with patch("aiohttp.ClientSession") as mock_cls:
            mock_cls.return_value = mock_session
            await m.embed(["test"])

        call_kwargs = mock_post.call_args
        payload = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")

        assert payload["input_type"] == "query"
        assert payload["output_dimension"] == 256

    async def test_embed_sends_auth_header(self, model):
        response_data = {"data": [{"embedding": [0.1]}]}
        mock_resp = _mock_aiohttp_response(status=200, json_data=response_data)
        mock_session, mock_post = _mock_aiohttp_session(mock_resp)

        with patch("aiohttp.ClientSession") as mock_cls:
            mock_cls.return_value = mock_session
            await model.embed(["test"])

        call_kwargs = mock_post.call_args
        headers = call_kwargs.kwargs.get("headers") or call_kwargs[1].get("headers")

        assert headers["Authorization"] == "Bearer test-key"

    async def test_embed_empty_input(self, model):
        result = await model.embed([])

        assert result == []

    async def test_embed_raises_on_api_error(self, model):
        error_data = {"detail": "Invalid API key"}
        mock_resp = _mock_aiohttp_response(status=401, json_data=error_data)
        mock_session, _ = _mock_aiohttp_session(mock_resp)

        with patch("aiohttp.ClientSession") as mock_cls:
            mock_cls.return_value = mock_session

            with pytest.raises(
                ExtractionError, match="Voyage multimodal API error 401"
            ):
                await model.embed(["test"])

    async def test_embed_400_carries_status_code(self, model):
        # A content rejection (HTTP 400) must carry the structured status so the
        # resilient batcher can decide to bisect-and-skip ONLY on a real 400 —
        # not on a substring of the message.
        error_data = {"detail": "inputs contain invalid elements"}
        mock_resp = _mock_aiohttp_response(status=400, json_data=error_data)
        mock_session, _ = _mock_aiohttp_session(mock_resp)

        with patch("aiohttp.ClientSession") as mock_cls:
            mock_cls.return_value = mock_session

            with pytest.raises(
                ExtractionError, match="Voyage multimodal API error 400"
            ) as excinfo:
                await model.embed(["test"])

        assert excinfo.value.status_code == 400

    async def test_embed_raises_on_missing_data(self, model):
        mock_resp = _mock_aiohttp_response(status=200, json_data={"object": "list"})
        mock_session, _ = _mock_aiohttp_session(mock_resp)

        with patch("aiohttp.ClientSession") as mock_cls:
            mock_cls.return_value = mock_session

            with pytest.raises(ExtractionError, match="unexpected response"):
                await model.embed(["test"])

    async def test_embed_raises_on_connection_error(self, model):
        mock_session = AsyncMock()
        mock_session.post = MagicMock(
            side_effect=aiohttp.ClientError("connection refused")
        )
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession") as mock_cls:
            mock_cls.return_value = mock_session

            with pytest.raises(ExtractionError, match="embedding call failed"):
                await model.embed(["test"])

    async def test_embed_multiple_texts(self, model):
        expected = [[0.1], [0.2], [0.3]]
        response_data = {
            "data": [{"embedding": e} for e in expected],
        }
        mock_resp = _mock_aiohttp_response(status=200, json_data=response_data)
        mock_session, mock_post = _mock_aiohttp_session(mock_resp)

        with patch("aiohttp.ClientSession") as mock_cls:
            mock_cls.return_value = mock_session
            result = await model.embed(["a", "b", "c"])

        assert result == expected
        payload = mock_post.call_args.kwargs.get("json") or mock_post.call_args[1].get(
            "json"
        )
        assert len(payload["inputs"]) == 3


class TestVoyageMultimodalRateLimitRetry:
    """The 429 exponential-backoff loop was folded into this client from
    the deprecated text-only client in #038. The tests below mirror the
    coverage that used to live in ``test_voyage_embedding.py``.
    """

    async def test_embed_retries_on_429_then_succeeds(self, mocker) -> None:
        """HTTP 429 should trigger the exponential-backoff loop and the
        call should ultimately succeed when the rate limit clears.

        Regression test for the Voyage free-tier 3 RPM / 10K TPM limit
        tripping the extraction flow — the model retries transparently
        so Prefect's per-task ``retries=2`` budget isn't burned on
        transient rate-limit errors.
        """

        # Patch asyncio.sleep so the test runs instantly.
        mock_sleep = mocker.patch(
            "tree.models.voyage_multimodal_embedding.asyncio.sleep",
            new_callable=AsyncMock,
        )

        m = VoyageMultimodalEmbeddingModel(
            api_key="key",
            model="voyage-multimodal-3",
            rate_limit_backoff_seconds=(0.1, 0.2, 0.4),
        )

        responses = [
            _mock_aiohttp_response(status=429, json_data={"detail": "throttled"}),
            _mock_aiohttp_response(status=429, json_data={"detail": "throttled"}),
            _mock_aiohttp_response(
                status=200, json_data={"data": [{"embedding": [0.1, 0.2]}]}
            ),
        ]

        # Each `session.post(...)` returns a *fresh* response context,
        # so swap mock_resp per attempt.
        def _make_session_for_call(call_idx: int):
            sess = AsyncMock()
            sess.post = MagicMock(return_value=responses[call_idx])
            sess.__aenter__ = AsyncMock(return_value=sess)
            sess.__aexit__ = AsyncMock(return_value=False)
            return sess

        sessions = [_make_session_for_call(i) for i in range(3)]

        with patch("aiohttp.ClientSession") as mock_cls:
            mock_cls.side_effect = sessions
            result = await m.embed(["hello"])

        assert result == [[0.1, 0.2]]
        # Two sleeps for the two 429s.
        assert mock_sleep.await_count == 2
        # First sleep is the first backoff value (0.1), second is 0.2.
        assert mock_sleep.await_args_list[0].args == (0.1,)
        assert mock_sleep.await_args_list[1].args == (0.2,)

    async def test_embed_raises_when_429_backoff_exhausted(self, mocker) -> None:
        """If 429s persist past the backoff schedule, the model must
        surface an ExtractionError that names the rate-limit cause —
        not a misleading "connection failed" or hang."""

        mocker.patch(
            "tree.models.voyage_multimodal_embedding.asyncio.sleep",
            new_callable=AsyncMock,
        )

        m = VoyageMultimodalEmbeddingModel(
            api_key="key",
            model="voyage-multimodal-3",
            rate_limit_backoff_seconds=(0.1, 0.1),  # only 2 retries
        )

        # Three consecutive 429s — one initial + two retries exhausts.
        def _new_429_session(_call_idx: int):
            resp = _mock_aiohttp_response(
                status=429, json_data={"detail": "still throttled"}
            )
            sess = AsyncMock()
            sess.post = MagicMock(return_value=resp)
            sess.__aenter__ = AsyncMock(return_value=sess)
            sess.__aexit__ = AsyncMock(return_value=False)
            return sess

        sessions = [_new_429_session(i) for i in range(5)]

        with patch("aiohttp.ClientSession") as mock_cls:
            mock_cls.side_effect = sessions
            with pytest.raises(
                ExtractionError,
                match="rate-limit retries exhausted",
            ) as excinfo:
                await m.embed(["test"])

        # The exhausted-429 raise must carry the structured HTTP status so
        # callers branch on it instead of the message (a 429 body can contain
        # the digit-run "400"); the resilient batcher uses this to NOT skip 429s.
        assert excinfo.value.status_code == 429

    async def test_embed_fails_fast_on_non_429_5xx(self, mocker) -> None:
        """Non-429 errors (e.g. 500) must fail fast — they are not
        transient. The model must raise ``ExtractionError`` without
        sleeping or retrying.
        """

        mock_sleep = mocker.patch(
            "tree.models.voyage_multimodal_embedding.asyncio.sleep",
            new_callable=AsyncMock,
        )

        m = VoyageMultimodalEmbeddingModel(
            api_key="key",
            model="voyage-multimodal-3",
            # Generous schedule — the test asserts we don't use any of it.
            rate_limit_backoff_seconds=(0.1, 0.1, 0.1),
        )

        mock_resp = _mock_aiohttp_response(
            status=500, json_data={"detail": "internal error"}
        )
        mock_session, _ = _mock_aiohttp_session(mock_resp)

        with patch("aiohttp.ClientSession") as mock_cls:
            mock_cls.return_value = mock_session
            with pytest.raises(
                ExtractionError, match="Voyage multimodal API error 500"
            ) as excinfo:
                await m.embed(["test"])

        assert mock_sleep.await_count == 0
        # 5xx is transient: structured status must be carried so the resilient
        # batcher re-raises (never skips) on server errors.
        assert excinfo.value.status_code == 500

    async def test_embed_fails_fast_on_non_429_4xx(self, mocker) -> None:
        """4xx other than 429 (e.g. 401 invalid key) must also fail fast."""

        mock_sleep = mocker.patch(
            "tree.models.voyage_multimodal_embedding.asyncio.sleep",
            new_callable=AsyncMock,
        )

        m = VoyageMultimodalEmbeddingModel(
            api_key="key",
            model="voyage-multimodal-3",
        )

        mock_resp = _mock_aiohttp_response(
            status=401, json_data={"detail": "Invalid API key"}
        )
        mock_session, _ = _mock_aiohttp_session(mock_resp)

        with patch("aiohttp.ClientSession") as mock_cls:
            mock_cls.return_value = mock_session
            with pytest.raises(
                ExtractionError, match="Voyage multimodal API error 401"
            ):
                await m.embed(["test"])

        assert mock_sleep.await_count == 0


class TestVoyageMultimodalRateLimitChokepoint:
    """ADR-002 §1 (amended): the shared ``voyage-embeddings`` slot is acquired
    immediately before each real network POST *attempt*, inside the 429-backoff
    ``while True`` loop, so a 429-retry re-acquires a fresh slot.

    The autouse ``_noop_voyage_rate_limit`` conftest fixture stubs
    ``tree.models.voyage_multimodal_embedding.rate_limit`` so unit boxes don't
    hit a Prefect server; these tests re-patch the same target with a spy to
    assert the call count and arguments.
    """

    async def test_acquires_one_slot_per_successful_post(self, mocker, model) -> None:
        # Arrange: a clean 200 — exactly one real POST.
        rate_limit = mocker.patch(
            "tree.models.voyage_multimodal_embedding.rate_limit",
            new_callable=AsyncMock,
        )
        response_data = {"data": [{"embedding": [0.1]}]}
        mock_resp = _mock_aiohttp_response(status=200, json_data=response_data)
        mock_session, _ = _mock_aiohttp_session(mock_resp)

        with patch("aiohttp.ClientSession") as mock_cls:
            mock_cls.return_value = mock_session
            await model.embed(["hello"])

        # Assert: one POST -> one slot, with the documented args.
        rate_limit.assert_awaited_once_with("voyage-embeddings", occupy=1, strict=False)

    async def test_429_retry_reacquires_a_fresh_slot(self, mocker) -> None:
        # Arrange: 429, 429, then 200 — three real POST attempts, so the slot
        # must be acquired three times (the proactive limiter is per-attempt).
        mocker.patch(
            "tree.models.voyage_multimodal_embedding.asyncio.sleep",
            new_callable=AsyncMock,
        )
        rate_limit = mocker.patch(
            "tree.models.voyage_multimodal_embedding.rate_limit",
            new_callable=AsyncMock,
        )
        m = VoyageMultimodalEmbeddingModel(
            api_key="key",
            model="voyage-multimodal-3",
            rate_limit_backoff_seconds=(0.1, 0.2, 0.4),
        )
        responses = [
            _mock_aiohttp_response(status=429, json_data={"detail": "throttled"}),
            _mock_aiohttp_response(status=429, json_data={"detail": "throttled"}),
            _mock_aiohttp_response(
                status=200, json_data={"data": [{"embedding": [0.1, 0.2]}]}
            ),
        ]

        def _make_session_for_call(call_idx: int):
            sess = AsyncMock()
            sess.post = MagicMock(return_value=responses[call_idx])
            sess.__aenter__ = AsyncMock(return_value=sess)
            sess.__aexit__ = AsyncMock(return_value=False)
            return sess

        sessions = [_make_session_for_call(i) for i in range(3)]

        # Act
        with patch("aiohttp.ClientSession") as mock_cls:
            mock_cls.side_effect = sessions
            result = await m.embed(["hello"])

        # Assert: one slot per real POST attempt (incl. each 429 retry).
        assert result == [[0.1, 0.2]]
        assert rate_limit.await_count == 3
        for call in rate_limit.await_args_list:
            assert call.args == ("voyage-embeddings",)
            assert call.kwargs == {"occupy": 1, "strict": False}

    async def test_empty_input_acquires_no_slot(self, mocker, model) -> None:
        # Arrange: the ``if not texts: return []`` short-circuit fires before any
        # POST and so must NOT acquire a slot.
        rate_limit = mocker.patch(
            "tree.models.voyage_multimodal_embedding.rate_limit",
            new_callable=AsyncMock,
        )

        # Act
        result = await model.embed([])

        # Assert
        assert result == []
        rate_limit.assert_not_awaited()
