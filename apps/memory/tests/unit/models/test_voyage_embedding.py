"""Unit tests for :class:`tree.models.voyage_embedding.VoyageEmbeddingModel`.

The class is the Voyage **text**-embeddings client (``/v1/embeddings``).
These tests mirror the ones for ``VoyageMultimodalEmbeddingModel`` but
assert the text-endpoint payload shape (``input: list[str]``, not
``inputs: [{content: [...]}]``).

Regression context: prior to #037 the project routed every voyage
provider through ``VoyageMultimodalEmbeddingModel``; ``voyage-3``
(the post-#034 YAML default) is rejected by the multimodal endpoint
with ``HTTP 400: Model voyage-3 is not supported``. This module
demonstrates the contract of the new text client; the routing fix is
covered in :mod:`tests.unit.models.test_get_model`.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from tree.models.exceptions import ExtractionError, ModelError
from tree.models.voyage_embedding import VoyageEmbeddingModel


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
def model() -> VoyageEmbeddingModel:
    return VoyageEmbeddingModel(api_key="test-key", model="voyage-3")


class TestVoyageInit:
    def test_raises_on_empty_api_key(self) -> None:
        with pytest.raises(ModelError, match="Voyage API key is required"):
            VoyageEmbeddingModel(api_key="")

    def test_stores_config(self) -> None:
        m = VoyageEmbeddingModel(
            api_key="key",
            model="voyage-3.5",
            input_type="document",
            output_dimension=512,
        )

        assert m._model == "voyage-3.5"
        assert m._input_type == "document"
        assert m._output_dimension == 512


class TestVoyageDimensions:
    def test_native_voyage_3_is_1024(self) -> None:
        m = VoyageEmbeddingModel(api_key="key", model="voyage-3")

        assert m.dimensions == 1024

    def test_native_voyage_3_lite_is_512(self) -> None:
        m = VoyageEmbeddingModel(api_key="key", model="voyage-3-lite")

        assert m.dimensions == 512

    def test_explicit_output_dimension_wins(self) -> None:
        m = VoyageEmbeddingModel(api_key="key", model="voyage-3", output_dimension=256)

        assert m.dimensions == 256

    def test_unknown_model_without_output_dimension_raises(self) -> None:
        m = VoyageEmbeddingModel(api_key="key", model="voyage-future-7")

        with pytest.raises(ModelError, match="no explicit `output_dimension`"):
            _ = m.dimensions


class TestVoyageEmbed:
    async def test_embed_returns_vectors(self, model: VoyageEmbeddingModel) -> None:
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
        self, model: VoyageEmbeddingModel
    ) -> None:
        """The text endpoint expects ``input: [str, ...]`` (NOT the
        multimodal ``inputs: [{content: [{type, text}]}, ...]`` shape).
        Regression test for the #037 routing bug.
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
        assert payload["model"] == "voyage-3"
        assert payload["input"] == ["hello"]
        assert "inputs" not in payload  # not the multimodal shape
        assert payload["truncation"] is True

    async def test_embed_sends_optional_params(self) -> None:
        m = VoyageEmbeddingModel(
            api_key="key",
            model="voyage-3",
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

    async def test_embed_sends_auth_header(self, model: VoyageEmbeddingModel) -> None:
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

    async def test_embed_empty_input(self, model: VoyageEmbeddingModel) -> None:
        result = await model.embed([])

        assert result == []

    async def test_embed_raises_on_api_error(self, model: VoyageEmbeddingModel) -> None:
        error_data = {"detail": "Invalid API key"}
        mock_resp = _mock_aiohttp_response(status=401, json_data=error_data)
        mock_session, _ = _mock_aiohttp_session(mock_resp)

        with patch("aiohttp.ClientSession") as mock_cls:
            mock_cls.return_value = mock_session

            with pytest.raises(
                ExtractionError, match="Voyage text-embeddings API error 401"
            ):
                await model.embed(["test"])

    async def test_embed_raises_on_missing_data(
        self, model: VoyageEmbeddingModel
    ) -> None:
        mock_resp = _mock_aiohttp_response(status=200, json_data={"object": "list"})
        mock_session, _ = _mock_aiohttp_session(mock_resp)

        with patch("aiohttp.ClientSession") as mock_cls:
            mock_cls.return_value = mock_session

            with pytest.raises(ExtractionError, match="unexpected response"):
                await model.embed(["test"])

    async def test_embed_raises_on_connection_error(
        self, model: VoyageEmbeddingModel
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
        """HTTP 429 should trigger the exponential-backoff loop and the
        call should ultimately succeed when the rate limit clears.

        Regression test for the Voyage free-tier 3 RPM / 10K TPM limit
        kept tripping the extraction flow even after the routing fix —
        the model retries transparently so Prefect's per-task
        ``retries=2`` budget isn't burned on transient rate-limit errors.
        """

        # Patch asyncio.sleep so the test runs instantly.
        mock_sleep = mocker.patch(
            "tree.models.voyage_embedding.asyncio.sleep",
            new_callable=AsyncMock,
        )

        m = VoyageEmbeddingModel(
            api_key="key",
            model="voyage-3",
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
        post_calls = []

        def _make_session_for_call(call_idx: int):
            sess = AsyncMock()
            sess.post = MagicMock(return_value=responses[call_idx])
            sess.__aenter__ = AsyncMock(return_value=sess)
            sess.__aexit__ = AsyncMock(return_value=False)
            post_calls.append(sess.post)
            return sess

        sessions = [_make_session_for_call(i) for i in range(3)]

        with patch("aiohttp.ClientSession") as mock_cls:
            mock_cls.side_effect = sessions
            result = await m.embed(["hello"])

        assert result == [[0.1, 0.2]]
        # Two sleeps for the two 429s.
        assert mock_sleep.await_count == 2
        # First sleep is the first backoff value (0.1).
        assert mock_sleep.await_args_list[0].args == (0.1,)
        assert mock_sleep.await_args_list[1].args == (0.2,)

    async def test_embed_raises_when_429_backoff_exhausted(self, mocker) -> None:
        """If 429s persist past the backoff schedule, the model must
        surface an ExtractionError that names the rate-limit cause —
        not a misleading "connection failed" or hang."""

        mocker.patch(
            "tree.models.voyage_embedding.asyncio.sleep",
            new_callable=AsyncMock,
        )

        m = VoyageEmbeddingModel(
            api_key="key",
            model="voyage-3",
            rate_limit_backoff_seconds=(0.1, 0.1),  # only 2 retries
        )

        # Three consecutive 429s — one initial + two retries.
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
            ):
                await m.embed(["test"])

    async def test_embed_multiple_texts(self, model: VoyageEmbeddingModel) -> None:
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
