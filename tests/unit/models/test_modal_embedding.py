from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
from openai import AsyncOpenAI

from twin.models.exceptions import ExtractionError, ModelError
from twin.models.modal_embedding import ModalEmbeddingModel


def _make_embedding_response(embeddings: list[list[float]]):
    """Build a mock OpenAI embeddings response."""

    class EmbeddingItem:
        def __init__(self, embedding):
            self.embedding = embedding

    class Response:
        def __init__(self, data):
            self.data = data

    return Response([EmbeddingItem(e) for e in embeddings])


def _mock_aiohttp_session(*, status: int = 200):
    """Return a patched aiohttp.ClientSession context manager."""

    mock_resp = AsyncMock()
    mock_resp.status = status
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)

    mock_get = MagicMock(return_value=mock_resp)
    mock_session = AsyncMock()
    mock_session.get = mock_get
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    return mock_session, mock_get


@pytest.fixture
def mock_modal_function():
    """Mock modal.Function.from_name to return a function with a known web URL."""

    mock_fn = MagicMock()
    mock_fn.get_web_url.aio = AsyncMock(
        return_value="https://test--vllm-embedding-serve.modal.run"
    )
    with patch("twin.models.modal_embedding.modal.Function") as mock_cls:
        mock_cls.from_name.return_value = mock_fn
        yield mock_cls, mock_fn


@pytest.fixture
def initialised_model(mock_modal_function, mocker):
    """A ModalEmbeddingModel with client pre-set (skips lazy init)."""

    m = ModalEmbeddingModel(
        api_key="test-key",
        model="voyageai/voyage-4-nano",
        app_name="test-app",
        function_name="voyageai-voyage-4-nano",
    )
    # Pre-create the client so embed() skips _ensure_initialised.
    m._client = AsyncOpenAI(
        base_url="https://test--vllm-embedding-serve.modal.run/v1",
        api_key="test-key",
    )
    mock_create = AsyncMock()
    mocker.patch.object(m._client.embeddings, "create", mock_create)
    return m, mock_create


class TestModalEmbeddingModelInit:
    def test_raises_on_empty_api_key(self):
        with pytest.raises(ModelError, match="Modal embedding API key is required"):
            ModalEmbeddingModel(api_key="")

    async def test_resolves_web_url_from_modal(self, mock_modal_function):
        mock_cls, mock_fn = mock_modal_function

        m = ModalEmbeddingModel(
            api_key="test-key", app_name="my-app", function_name="my-func"
        )

        # Init is lazy — client is not created yet.
        assert m._client is None

        with patch("aiohttp.ClientSession") as mock_session_cls:
            mock_session, _ = _mock_aiohttp_session()
            mock_session_cls.return_value = mock_session
            await m._ensure_initialised()

        mock_cls.from_name.assert_called_once_with("my-app", "my-func")
        mock_fn.get_web_url.aio.assert_awaited_once()
        assert m._client is not None

    async def test_appends_v1_suffix_to_url(self, mock_modal_function):
        _, mock_fn = mock_modal_function
        mock_fn.get_web_url.aio = AsyncMock(return_value="https://example.modal.run")

        m = ModalEmbeddingModel(api_key="test-key")
        with patch("aiohttp.ClientSession") as mock_session_cls:
            mock_session, _ = _mock_aiohttp_session()
            mock_session_cls.return_value = mock_session
            await m._ensure_initialised()

        base_url = str(m._client.base_url)
        assert base_url.rstrip("/").endswith("/v1")

    async def test_does_not_double_v1_suffix(self, mock_modal_function):
        _, mock_fn = mock_modal_function
        mock_fn.get_web_url.aio = AsyncMock(return_value="https://example.modal.run/v1")

        m = ModalEmbeddingModel(api_key="test-key")
        with patch("aiohttp.ClientSession") as mock_session_cls:
            mock_session, _ = _mock_aiohttp_session()
            mock_session_cls.return_value = mock_session
            await m._ensure_initialised()

        base_url = str(m._client.base_url)
        assert not base_url.rstrip("/").endswith("/v1/v1")

    async def test_raises_model_error_when_modal_lookup_fails(self):
        with patch("twin.models.modal_embedding.modal.Function") as mock_cls:
            mock_cls.from_name.side_effect = RuntimeError("not deployed")

            m = ModalEmbeddingModel(api_key="test-key")
            with pytest.raises(ModelError, match="Failed to resolve Modal web URL"):
                await m._ensure_initialised()

    async def test_raises_model_error_when_url_is_empty(self, mock_modal_function):
        _, mock_fn = mock_modal_function
        mock_fn.get_web_url.aio = AsyncMock(return_value="")

        m = ModalEmbeddingModel(api_key="test-key")
        with pytest.raises(ModelError, match="returned an empty web URL"):
            await m._ensure_initialised()

    async def test_raises_model_error_when_url_is_none(self, mock_modal_function):
        _, mock_fn = mock_modal_function
        mock_fn.get_web_url.aio = AsyncMock(return_value=None)

        m = ModalEmbeddingModel(api_key="test-key")
        with pytest.raises(ModelError, match="returned an empty web URL"):
            await m._ensure_initialised()

    async def test_initialisation_is_idempotent(self, mock_modal_function):
        _, mock_fn = mock_modal_function

        m = ModalEmbeddingModel(api_key="test-key")
        with patch("aiohttp.ClientSession") as mock_session_cls:
            mock_session, _ = _mock_aiohttp_session()
            mock_session_cls.return_value = mock_session
            await m._ensure_initialised()
            await m._ensure_initialised()

        # URL resolved only once despite two calls.
        mock_fn.get_web_url.aio.assert_awaited_once()


class TestModalEmbeddingModelHealthCheck:
    async def test_health_check_called_during_init(self, mock_modal_function):
        m = ModalEmbeddingModel(api_key="test-key")

        with patch("aiohttp.ClientSession") as mock_session_cls:
            mock_session, mock_get = _mock_aiohttp_session()
            mock_session_cls.return_value = mock_session
            await m._ensure_initialised()

            mock_get.assert_called_once()
            call_url = mock_get.call_args[0][0]
            assert "/health" in call_url

    async def test_health_check_raises_on_non_200(self, mock_modal_function):
        m = ModalEmbeddingModel(api_key="test-key")

        with patch("aiohttp.ClientSession") as mock_session_cls:
            mock_session, _ = _mock_aiohttp_session(status=503)
            mock_session_cls.return_value = mock_session

            with pytest.raises(
                ExtractionError, match="health check returned status 503"
            ):
                await m._ensure_initialised()

    async def test_health_check_raises_on_connection_error(self, mock_modal_function):
        m = ModalEmbeddingModel(api_key="test-key")

        with patch("aiohttp.ClientSession") as mock_session_cls:
            mock_session = AsyncMock()
            mock_session.get.side_effect = aiohttp.ClientError("connection refused")
            mock_session.__aenter__ = AsyncMock(return_value=mock_session)
            mock_session.__aexit__ = AsyncMock(return_value=False)
            mock_session_cls.return_value = mock_session

            with pytest.raises(ExtractionError, match="Modal health check failed"):
                await m._ensure_initialised()


class TestModalEmbeddingModelEmbed:
    async def test_embed_returns_vectors(self, initialised_model):
        m, mock_create = initialised_model
        expected = [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]
        mock_create.return_value = _make_embedding_response(expected)

        result = await m.embed(["hello", "world"])

        assert result == expected
        mock_create.assert_awaited_once_with(
            input=["hello", "world"],
            model="voyageai/voyage-4-nano",
        )

    async def test_embed_empty_input(self, initialised_model):
        m, mock_create = initialised_model
        mock_create.return_value = _make_embedding_response([])

        result = await m.embed([])

        assert result == []

    async def test_embed_delegates_to_openai_client(self, initialised_model):
        m, mock_create = initialised_model
        expected = [[0.1, 0.2]]
        mock_create.return_value = _make_embedding_response(expected)

        result = await m.embed(["single"])

        assert result == expected
        mock_create.assert_awaited_once()

    async def test_embed_propagates_extraction_error(self, initialised_model):
        m, mock_create = initialised_model
        mock_create.side_effect = RuntimeError("timeout")

        with pytest.raises(ExtractionError, match="Embedding call failed"):
            await m.embed(["test"])

    async def test_embed_triggers_lazy_init(self, mock_modal_function):
        m = ModalEmbeddingModel(api_key="test-key")
        assert m._client is None

        with patch("aiohttp.ClientSession") as mock_session_cls:
            mock_session, _ = _mock_aiohttp_session()
            mock_session_cls.return_value = mock_session

            # embed() triggers _ensure_initialised which creates the client.
            # The actual embedding call will fail because the client
            # points at a fake URL, but we just verify init happened.
            try:
                await m.embed(["test"])
            except Exception:
                pass

        assert m._client is not None
