"""Unit tests for :class:`tree.models.voyage_embedding.VoyageTextEmbeddingModel`.

The class is the Voyage **text**-embeddings client (``/v1/embeddings``).
These tests mirror the ones for ``VoyageMultimodalEmbeddingModel`` but assert
the text-endpoint payload shape (``input: list[str]``, NOT the multimodal
``inputs: [{content: [...]}]`` shape) and the structured
``ExtractionError.status_code`` discriminator that
``tree.memory.embedding_text._embed_chunk_resilient`` keys off.

Regression context: routing ``voyage-3``/``voyage-3.5`` (the #048 default) to
the multimodal endpoint is rejected with ``HTTP 400: Model voyage-3 is not
supported``. This module pins the text client's contract; the routing fix is
covered in :mod:`tests.unit.models.test_get_model`.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from tree.memory.embedding_text import embed_in_batches
from tree.models.exceptions import ExtractionError, ModelError
from tree.models.voyage_embedding import VoyageTextEmbeddingModel


def _mock_aiohttp_response(*, status: int = 200, json_data: dict | None = None):
    mock_resp = AsyncMock()
    mock_resp.status = status
    mock_resp.json = AsyncMock(return_value=json_data or {})
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)
    return mock_resp


def _mock_aiohttp_session(mock_resp: AsyncMock):
    mock_post = MagicMock(return_value=mock_resp)
    mock_session = AsyncMock()
    mock_session.post = mock_post
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    return mock_session, mock_post


@pytest.fixture
def model() -> VoyageTextEmbeddingModel:
    return VoyageTextEmbeddingModel(api_key="test-key", model="voyage-3.5")


class TestVoyageTextInit:
    def test_raises_on_empty_api_key(self) -> None:
        with pytest.raises(ModelError, match="Voyage API key is required"):
            VoyageTextEmbeddingModel(api_key="")

    def test_defaults_to_voyage_3_5(self) -> None:
        m = VoyageTextEmbeddingModel(api_key="key")

        assert m._model == "voyage-3.5"

    def test_stores_config(self) -> None:
        m = VoyageTextEmbeddingModel(
            api_key="key",
            model="voyage-3",
            input_type="document",
            output_dimension=512,
        )

        assert m._model == "voyage-3"
        assert m._input_type == "document"
        assert m._output_dimension == 512


class TestVoyageTextDimensions:
    def test_native_voyage_3_5_is_1024(self) -> None:
        m = VoyageTextEmbeddingModel(api_key="key", model="voyage-3.5")

        assert m.dimensions == 1024

    def test_native_voyage_3_is_1024(self) -> None:
        m = VoyageTextEmbeddingModel(api_key="key", model="voyage-3")

        assert m.dimensions == 1024

    def test_native_voyage_3_lite_is_512(self) -> None:
        m = VoyageTextEmbeddingModel(api_key="key", model="voyage-3-lite")

        assert m.dimensions == 512

    def test_native_voyage_code_3_is_1024(self) -> None:
        m = VoyageTextEmbeddingModel(api_key="key", model="voyage-code-3")

        assert m.dimensions == 1024

    def test_explicit_output_dimension_wins(self) -> None:
        m = VoyageTextEmbeddingModel(
            api_key="key", model="voyage-3.5", output_dimension=256
        )

        assert m.dimensions == 256

    def test_unknown_model_without_output_dimension_raises(self) -> None:
        m = VoyageTextEmbeddingModel(api_key="key", model="voyage-future-7")

        with pytest.raises(ModelError, match="no explicit `output_dimension`"):
            _ = m.dimensions


class TestVoyageTextEmbed:
    async def test_embed_returns_vectors(self, model: VoyageTextEmbeddingModel) -> None:
        expected = [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]
        response_data = {
            "data": [
                {"embedding": expected[0]},
                {"embedding": expected[1]},
            ]
        }

        mock_resp = _mock_aiohttp_response(status=200, json_data=response_data)
        mock_session, _ = _mock_aiohttp_session(mock_resp)

        with patch("aiohttp.ClientSession") as mock_cls:
            mock_cls.return_value = mock_session
            result = await model.embed(["hello", "world"])

        assert result == expected

    async def test_embed_payload_uses_text_endpoint_shape(
        self, model: VoyageTextEmbeddingModel
    ) -> None:
        """The text endpoint expects ``input: [str, ...]`` (NOT the multimodal
        ``inputs: [{content: [{type, text}]}, ...]`` shape). This is the
        headline bug #048 fixes.
        """

        response_data = {"data": [{"embedding": [0.1]}]}
        mock_resp = _mock_aiohttp_response(status=200, json_data=response_data)
        mock_session, mock_post = _mock_aiohttp_session(mock_resp)

        with patch("aiohttp.ClientSession") as mock_cls:
            mock_cls.return_value = mock_session
            await model.embed(["hello"])

        call_kwargs = mock_post.call_args
        url = call_kwargs.args[0] if call_kwargs.args else call_kwargs.kwargs.get("url")
        payload = call_kwargs.kwargs.get("json") or call_kwargs[1].get("json")

        assert url == "https://api.voyageai.com/v1/embeddings"
        assert payload["model"] == "voyage-3.5"
        assert payload["input"] == ["hello"]
        assert "inputs" not in payload  # not the multimodal shape
        assert payload["truncation"] is True

    async def test_embed_sends_optional_params(self) -> None:
        m = VoyageTextEmbeddingModel(
            api_key="key",
            model="voyage-3.5",
            input_type="query",
            output_dimension=256,
        )
        response_data = {"data": [{"embedding": [0.1]}]}
        mock_resp = _mock_aiohttp_response(status=200, json_data=response_data)
        mock_session, mock_post = _mock_aiohttp_session(mock_resp)

        with patch("aiohttp.ClientSession") as mock_cls:
            mock_cls.return_value = mock_session
            await m.embed(["test"])

        payload = mock_post.call_args.kwargs.get("json") or mock_post.call_args[1].get(
            "json"
        )

        assert payload["input_type"] == "query"
        assert payload["output_dimension"] == 256

    async def test_embed_sends_auth_header(
        self, model: VoyageTextEmbeddingModel
    ) -> None:
        response_data = {"data": [{"embedding": [0.1]}]}
        mock_resp = _mock_aiohttp_response(status=200, json_data=response_data)
        mock_session, mock_post = _mock_aiohttp_session(mock_resp)

        with patch("aiohttp.ClientSession") as mock_cls:
            mock_cls.return_value = mock_session
            await model.embed(["test"])

        headers = mock_post.call_args.kwargs.get("headers") or mock_post.call_args[
            1
        ].get("headers")

        assert headers["Authorization"] == "Bearer test-key"

    async def test_embed_empty_input_makes_no_http_call(
        self, model: VoyageTextEmbeddingModel
    ) -> None:
        mock_resp = _mock_aiohttp_response(status=200, json_data={"data": []})
        mock_session, mock_post = _mock_aiohttp_session(mock_resp)

        with patch("aiohttp.ClientSession") as mock_cls:
            mock_cls.return_value = mock_session
            result = await model.embed([])

        assert result == []
        mock_post.assert_not_called()

    async def test_embed_400_raises_with_status_code(
        self, model: VoyageTextEmbeddingModel
    ) -> None:
        """A content-rejection HTTP 400 must surface ``status_code == 400`` so
        ``_embed_chunk_resilient`` can skip the poison input (not re-raise)."""

        error_data = {"detail": "invalid elements in input"}
        mock_resp = _mock_aiohttp_response(status=400, json_data=error_data)
        mock_session, _ = _mock_aiohttp_session(mock_resp)

        with patch("aiohttp.ClientSession") as mock_cls:
            mock_cls.return_value = mock_session

            with pytest.raises(ExtractionError) as exc_info:
                await model.embed(["test"])

        assert exc_info.value.status_code == 400

    async def test_embed_raises_on_api_error(
        self, model: VoyageTextEmbeddingModel
    ) -> None:
        error_data = {"detail": "Invalid API key"}
        mock_resp = _mock_aiohttp_response(status=401, json_data=error_data)
        mock_session, _ = _mock_aiohttp_session(mock_resp)

        with patch("aiohttp.ClientSession") as mock_cls:
            mock_cls.return_value = mock_session

            with pytest.raises(
                ExtractionError, match="Voyage text-embeddings API error 401"
            ) as exc_info:
                await model.embed(["test"])

        assert exc_info.value.status_code == 401

    async def test_embed_raises_on_missing_data(
        self, model: VoyageTextEmbeddingModel
    ) -> None:
        mock_resp = _mock_aiohttp_response(status=200, json_data={"object": "list"})
        mock_session, _ = _mock_aiohttp_session(mock_resp)

        with patch("aiohttp.ClientSession") as mock_cls:
            mock_cls.return_value = mock_session

            with pytest.raises(ExtractionError, match="unexpected response"):
                await model.embed(["test"])

    async def test_embed_raises_on_connection_error(
        self, model: VoyageTextEmbeddingModel
    ) -> None:
        mock_session = AsyncMock()
        mock_session.post = MagicMock(
            side_effect=aiohttp.ClientError("connection refused")
        )
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)

        with patch("aiohttp.ClientSession") as mock_cls:
            mock_cls.return_value = mock_session

            with pytest.raises(
                ExtractionError, match="Voyage text-embeddings call failed"
            ):
                await model.embed(["test"])

    async def test_embed_retries_on_429_then_succeeds(self, mocker) -> None:
        """HTTP 429 triggers the exponential-backoff loop; the call ultimately
        succeeds when the rate limit clears, riding out Voyage's 3 RPM tier so
        Prefect's per-task ``retries=2`` budget isn't burned on transient 429s.
        """

        mock_sleep = mocker.patch(
            "tree.models.voyage_embedding.asyncio.sleep",
            new_callable=AsyncMock,
        )

        m = VoyageTextEmbeddingModel(
            api_key="key",
            model="voyage-3.5",
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

        with patch("aiohttp.ClientSession") as mock_cls:
            mock_cls.side_effect = sessions
            result = await m.embed(["hello"])

        assert result == [[0.1, 0.2]]
        assert mock_sleep.await_count == 2
        assert mock_sleep.await_args_list[0].args == (0.1,)
        assert mock_sleep.await_args_list[1].args == (0.2,)

    async def test_embed_raises_when_429_backoff_exhausted(self, mocker) -> None:
        """If 429s persist past the backoff schedule, the model surfaces an
        ExtractionError that names the rate-limit cause and carries
        ``status_code == 429`` (so the resilience layer re-raises, never skips).
        """

        mocker.patch(
            "tree.models.voyage_embedding.asyncio.sleep",
            new_callable=AsyncMock,
        )

        m = VoyageTextEmbeddingModel(
            api_key="key",
            model="voyage-3.5",
            rate_limit_backoff_seconds=(0.1, 0.1),  # only 2 retries
        )

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
            ) as exc_info:
                await m.embed(["test"])

        assert exc_info.value.status_code == 429

    async def test_embed_multiple_texts(self, model: VoyageTextEmbeddingModel) -> None:
        expected = [[0.1], [0.2], [0.3]]
        response_data = {"data": [{"embedding": e} for e in expected]}
        mock_resp = _mock_aiohttp_response(status=200, json_data=response_data)
        mock_session, mock_post = _mock_aiohttp_session(mock_resp)

        with patch("aiohttp.ClientSession") as mock_cls:
            mock_cls.return_value = mock_session
            result = await model.embed(["a", "b", "c"])

        assert result == expected
        payload = mock_post.call_args.kwargs.get("json") or mock_post.call_args[1].get(
            "json"
        )
        assert payload["input"] == ["a", "b", "c"]


class TestVoyageTextRateLimitChokepoint:
    """ADR-002 §1 (amended): the shared ``voyage-embeddings`` slot is acquired
    immediately before each real network POST *attempt*, inside the 429-backoff
    ``while True`` loop, so a 429-retry re-acquires a fresh slot.

    The autouse ``_noop_voyage_rate_limit`` conftest fixture stubs
    ``tree.models.voyage_embedding.rate_limit`` so unit boxes don't hit a Prefect
    server; these tests re-patch the same target with a spy to assert the call
    count and arguments.
    """

    async def test_acquires_one_slot_per_successful_post(
        self, mocker, model: VoyageTextEmbeddingModel
    ) -> None:
        # Arrange: a clean 200 — exactly one real POST.
        rate_limit = mocker.patch(
            "tree.models.voyage_embedding.rate_limit", new_callable=AsyncMock
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
            "tree.models.voyage_embedding.asyncio.sleep", new_callable=AsyncMock
        )
        rate_limit = mocker.patch(
            "tree.models.voyage_embedding.rate_limit", new_callable=AsyncMock
        )
        m = VoyageTextEmbeddingModel(
            api_key="key",
            model="voyage-3.5",
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

    async def test_empty_input_acquires_no_slot(
        self, mocker, model: VoyageTextEmbeddingModel
    ) -> None:
        # Arrange: the ``if not texts: return []`` short-circuit fires before any
        # POST and so must NOT acquire a slot.
        rate_limit = mocker.patch(
            "tree.models.voyage_embedding.rate_limit", new_callable=AsyncMock
        )

        # Act
        result = await model.embed([])

        # Assert
        assert result == []
        rate_limit.assert_not_awaited()


class TestVoyageTextComposesWithEmbeddingTextResilience:
    """Composition: the real text client, run through ``embed_in_batches``,
    inherits the ``tree.memory.embedding_text`` resilience layer for free.

    No new code in ``embedding_text.py`` — these tests prove the layer composes
    with the new client via the ``status_code`` discriminator: a content-400 is
    bisected and skipped to an aligned ``[]`` placeholder; a 429 (transient)
    propagates rather than being silently dropped.
    """

    @staticmethod
    def _session_factory(poison: set[str]):
        """Build an ``aiohttp.ClientSession`` side-effect that 400s on any
        request whose ``input`` list contains a poison text, 200s otherwise.

        Returns a callable suitable for ``patch(...).side_effect`` so each
        ``embed`` call (each bisected sub-chunk) gets a fresh session/response.
        """

        def _make_session(*_args, **_kwargs):
            sess = AsyncMock()

            def _post(_url, *, json, headers):  # noqa: A002 - aiohttp kwarg name
                inputs = json["input"]
                if any(t in poison for t in inputs):
                    return _mock_aiohttp_response(
                        status=400,
                        json_data={"detail": "inputs contain invalid elements"},
                    )
                return _mock_aiohttp_response(
                    status=200,
                    json_data={"data": [{"embedding": [0.5, 0.5]} for _ in inputs]},
                )

            sess.post = MagicMock(side_effect=_post)
            sess.__aenter__ = AsyncMock(return_value=sess)
            sess.__aexit__ = AsyncMock(return_value=False)
            return sess

        return _make_session

    async def test_mocked_400_single_input_is_skipped_to_placeholder(self) -> None:
        # Arrange: a real text client whose endpoint 400s on the middle input.
        model = VoyageTextEmbeddingModel(api_key="key", model="voyage-3.5")

        with patch("aiohttp.ClientSession") as mock_cls:
            mock_cls.side_effect = self._session_factory(poison={"bad"})

            # Act: embed_in_batches bisects the failing chunk and skips the
            # poison input (aligned [] placeholder), embedding the rest.
            vectors = await embed_in_batches(["a", "bad", "c"], model, max_inputs=1000)

        # Assert: good inputs embedded, poison skipped — no dropped/shifted slot.
        assert vectors == [[0.5, 0.5], [], [0.5, 0.5]]

    async def test_mocked_429_propagates_as_retry_not_skip(self, mocker) -> None:
        # Arrange: every request 429s; the client exhausts its (short) backoff
        # schedule and raises status_code=429, which embed_in_batches must
        # propagate (transient — never bisect/skip).
        mocker.patch(
            "tree.models.voyage_embedding.asyncio.sleep",
            new_callable=AsyncMock,
        )
        model = VoyageTextEmbeddingModel(
            api_key="key",
            model="voyage-3.5",
            rate_limit_backoff_seconds=(0.01, 0.01),
        )

        def _make_429_session(*_args, **_kwargs):
            sess = AsyncMock()
            sess.post = MagicMock(
                return_value=_mock_aiohttp_response(
                    status=429, json_data={"detail": "throttled"}
                )
            )
            sess.__aenter__ = AsyncMock(return_value=sess)
            sess.__aexit__ = AsyncMock(return_value=False)
            return sess

        with patch("aiohttp.ClientSession") as mock_cls:
            mock_cls.side_effect = _make_429_session

            # Act / Assert: the 429 surfaces through embed_in_batches unchanged.
            with pytest.raises(ExtractionError, match="rate-limit retries exhausted"):
                await embed_in_batches(["a", "b"], model, max_inputs=1000)
