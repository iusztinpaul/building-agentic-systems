import numpy as np
import pytest

from twin.models.exceptions import ExtractionError
from twin.models.sentence_transformer import SentenceTransformerEmbeddingModel


@pytest.fixture
def model(mocker):
    mocker.patch(
        "twin.models.sentence_transformer.SentenceTransformer",
    )
    return SentenceTransformerEmbeddingModel(
        model="voyageai/voyage-4-nano",
        dimensions=4,
    )


class TestSentenceTransformerEmbeddingModel:
    async def test_embed_returns_truncated_vectors(self, model):
        fake_output = np.array([[0.1, 0.2, 0.3, 0.4, 0.5], [0.6, 0.7, 0.8, 0.9, 1.0]])
        model._model.encode.return_value = fake_output

        result = await model.embed(["hello", "world"])

        assert len(result) == 2
        assert result[0] == [0.1, 0.2, 0.3, 0.4]
        assert result[1] == [0.6, 0.7, 0.8, 0.9]
        model._model.encode.assert_called_once_with(
            ["hello", "world"],
            normalize_embeddings=True,
        )

    async def test_embed_empty_input(self, model):
        result = await model.embed([])

        assert result == []
        model._model.encode.assert_not_called()

    async def test_embed_raises_extraction_error_on_failure(self, model):
        model._model.encode.side_effect = RuntimeError("model error")

        with pytest.raises(
            ExtractionError, match="Sentence-transformer embedding failed"
        ):
            await model.embed(["test"])

    async def test_embed_single_text(self, model):
        fake_output = np.array([[0.1, 0.2, 0.3, 0.4]])
        model._model.encode.return_value = fake_output

        result = await model.embed(["single"])

        assert result == [[0.1, 0.2, 0.3, 0.4]]
