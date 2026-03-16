from unittest.mock import AsyncMock

import pytest

from twin.models.exceptions import ExtractionError
from twin.models.openai_embedding import OpenAICompatibleEmbeddingModel


def _make_embedding_response(embeddings: list[list[float]]):
    """Build a mock OpenAI embeddings response."""

    class EmbeddingItem:
        def __init__(self, embedding):
            self.embedding = embedding

    class Response:
        def __init__(self, data):
            self.data = data

    return Response([EmbeddingItem(e) for e in embeddings])


@pytest.fixture
def model(mocker):
    m = OpenAICompatibleEmbeddingModel(
        base_url="http://localhost:8000/v1",
        api_key="EMPTY",
        model="voyageai/voyage-4-nano",
        dimensions=512,
    )
    mock_create = AsyncMock()
    mocker.patch.object(m._client.embeddings, "create", mock_create)
    return m, mock_create


class TestOpenAICompatibleEmbeddingModel:
    async def test_embed_returns_vectors(self, model):
        m, mock_create = model
        expected = [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]
        mock_create.return_value = _make_embedding_response(expected)

        result = await m.embed(["hello", "world"])

        assert result == expected
        mock_create.assert_awaited_once_with(
            input=["hello", "world"],
            model="voyageai/voyage-4-nano",
            dimensions=512,
        )

    async def test_embed_empty_input(self, model):
        m, mock_create = model
        mock_create.return_value = _make_embedding_response([])

        result = await m.embed([])

        assert result == []

    async def test_embed_raises_extraction_error_on_failure(self, model):
        m, mock_create = model
        mock_create.side_effect = RuntimeError("connection refused")

        with pytest.raises(ExtractionError, match="Embedding call failed"):
            await m.embed(["test"])

    async def test_embed_single_text(self, model):
        m, mock_create = model
        expected = [[0.1, 0.2]]
        mock_create.return_value = _make_embedding_response(expected)

        result = await m.embed(["single"])

        assert result == expected
