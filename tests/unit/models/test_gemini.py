import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from twin.models.exceptions import ExtractionError
from twin.models.gemini import GeminiEmbeddingModel, GeminiLLM


@pytest.fixture()
def mock_genai_client(mocker) -> MagicMock:
    mock_client = MagicMock()
    mocker.patch("twin.models.gemini.genai.Client", return_value=mock_client)
    return mock_client


class TestGeminiLLM:
    def test_init_sets_model(self, mock_genai_client) -> None:
        llm = GeminiLLM(api_key="fake-key", model="gemini-2.0-flash")

        assert llm._model == "gemini-2.0-flash"

    @pytest.mark.asyncio
    async def test_generate_json_returns_parsed_dict(self, mock_genai_client) -> None:
        expected = {"nodes": [{"name": "AI"}]}
        mock_response = MagicMock()
        mock_response.text = json.dumps(expected)
        mock_genai_client.aio.models.generate_content = AsyncMock(
            return_value=mock_response
        )

        llm = GeminiLLM(api_key="fake-key", model="gemini-2.0-flash")
        result = await llm.generate_json("Extract entities")

        assert result == expected

    @pytest.mark.asyncio
    async def test_generate_json_with_system_instruction(
        self, mock_genai_client
    ) -> None:
        mock_response = MagicMock()
        mock_response.text = '{"ok": true}'
        mock_genai_client.aio.models.generate_content = AsyncMock(
            return_value=mock_response
        )

        llm = GeminiLLM(api_key="fake-key", model="gemini-2.0-flash")
        result = await llm.generate_json("prompt", system="You are helpful")

        assert result == {"ok": True}
        call_kwargs = mock_genai_client.aio.models.generate_content.call_args
        config = call_kwargs.kwargs["config"]
        assert config.system_instruction == "You are helpful"

    @pytest.mark.asyncio
    async def test_generate_json_raises_on_api_error(self, mock_genai_client) -> None:
        mock_genai_client.aio.models.generate_content = AsyncMock(
            side_effect=RuntimeError("API down")
        )

        llm = GeminiLLM(api_key="fake-key", model="gemini-2.0-flash")

        with pytest.raises(ExtractionError, match="Gemini API call failed"):
            await llm.generate_json("prompt")

    @pytest.mark.asyncio
    async def test_generate_json_raises_on_empty_response(
        self, mock_genai_client
    ) -> None:
        mock_response = MagicMock()
        mock_response.text = ""
        mock_genai_client.aio.models.generate_content = AsyncMock(
            return_value=mock_response
        )

        llm = GeminiLLM(api_key="fake-key", model="gemini-2.0-flash")

        with pytest.raises(ExtractionError, match="empty response"):
            await llm.generate_json("prompt")

    @pytest.mark.asyncio
    async def test_generate_json_raises_on_invalid_json(
        self, mock_genai_client
    ) -> None:
        mock_response = MagicMock()
        mock_response.text = "not valid json {{"
        mock_genai_client.aio.models.generate_content = AsyncMock(
            return_value=mock_response
        )

        llm = GeminiLLM(api_key="fake-key", model="gemini-2.0-flash")

        with pytest.raises(ExtractionError, match="invalid JSON"):
            await llm.generate_json("prompt")


class TestGeminiEmbeddingModel:
    def test_init_sets_model_and_dimensions(self, mock_genai_client) -> None:
        model = GeminiEmbeddingModel(
            api_key="fake-key", model="text-embedding-004", dimensions=256
        )

        assert model._model == "text-embedding-004"
        assert model._dimensions == 256

    @pytest.mark.asyncio
    async def test_embed_returns_vectors(self, mock_genai_client) -> None:
        embedding1 = MagicMock()
        embedding1.values = [0.1, 0.2, 0.3]
        embedding2 = MagicMock()
        embedding2.values = [0.4, 0.5, 0.6]
        mock_response = MagicMock()
        mock_response.embeddings = [embedding1, embedding2]
        mock_genai_client.aio.models.embed_content = AsyncMock(
            return_value=mock_response
        )

        model = GeminiEmbeddingModel(
            api_key="fake-key", model="text-embedding-004", dimensions=256
        )
        result = await model.embed(["hello", "world"])

        assert result == [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]

    @pytest.mark.asyncio
    async def test_embed_handles_none_values(self, mock_genai_client) -> None:
        embedding = MagicMock()
        embedding.values = None
        mock_response = MagicMock()
        mock_response.embeddings = [embedding]
        mock_genai_client.aio.models.embed_content = AsyncMock(
            return_value=mock_response
        )

        model = GeminiEmbeddingModel(
            api_key="fake-key", model="text-embedding-004", dimensions=256
        )
        result = await model.embed(["hello"])

        assert result == [[]]

    @pytest.mark.asyncio
    async def test_embed_raises_on_api_error(self, mock_genai_client) -> None:
        mock_genai_client.aio.models.embed_content = AsyncMock(
            side_effect=RuntimeError("API down")
        )

        model = GeminiEmbeddingModel(
            api_key="fake-key", model="text-embedding-004", dimensions=256
        )

        with pytest.raises(ExtractionError, match="Gemini embedding call failed"):
            await model.embed(["hello"])
