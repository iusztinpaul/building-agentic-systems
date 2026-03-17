from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from twin.models.exceptions import ExtractionError, ModelError
from twin.models.voyage_multimodal_embedding import VoyageMultimodalEmbeddingModel


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

    async def test_embed_raises_on_missing_data(self, model):
        mock_resp = _mock_aiohttp_response(status=200, json_data={"object": "list"})
        mock_session, _ = _mock_aiohttp_session(mock_resp)

        with patch("aiohttp.ClientSession") as mock_cls:
            mock_cls.return_value = mock_session

            with pytest.raises(ExtractionError, match="unexpected response"):
                await model.embed(["test"])

    async def test_embed_raises_on_connection_error(self, model):
        mock_session = AsyncMock()
        mock_session.post.side_effect = aiohttp.ClientError("connection refused")
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
