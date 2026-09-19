import numpy as np
import pytest

from tree.models.exceptions import ExtractionError
from tree.models.sentence_transformer import SentenceTransformerEmbeddingModel


@pytest.fixture
def model(mocker):
    mocker.patch(
        "tree.models.sentence_transformer.SentenceTransformer",
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


class TestPromptName:
    """The **Embedding role** becomes ``prompt_name`` ONLY for a role the
    loaded model actually defines a prompt for (ADR-009 §5).

    Verified against the pinned sentence-transformers 5.3.0 (read 2026-09-19,
    ``SentenceTransformer.py``): ``encode(prompt_name=...)`` raises
    ``ValueError`` for a name absent from ``model.prompts``, and every model
    is seeded with ``prompts = {"query": "", "document": ""}`` at
    construction — so membership alone is always true and the gate must be the
    prompt STRING being non-empty ("defines that prompt", glossary).
    """

    def _encode_kwargs(self, model) -> dict:
        return model._model.encode.call_args.kwargs

    async def test_defined_role_is_passed_as_prompt_name(self, model):
        model._model.prompts = {"query": "Q: "}
        model._model.encode.return_value = np.array([[0.1, 0.2, 0.3, 0.4]])

        await model.embed(["text"], input_type="query")

        assert self._encode_kwargs(model)["prompt_name"] == "query"

    async def test_undefined_role_sends_no_prompt_name(self, model):
        """User story 2: ``all-MiniLM-L6-v2`` defines no ``document`` prompt —
        encode exactly as before, no error."""

        model._model.prompts = {"query": "Q: "}
        model._model.encode.return_value = np.array([[0.1, 0.2, 0.3, 0.4]])

        await model.embed(["text"], input_type="document")

        assert "prompt_name" not in self._encode_kwargs(model)

    async def test_no_role_sends_no_prompt_name(self, model):
        model._model.prompts = {"query": "Q: "}
        model._model.encode.return_value = np.array([[0.1, 0.2, 0.3, 0.4]])

        await model.embed(["text"], input_type=None)

        assert "prompt_name" not in self._encode_kwargs(model)

    @pytest.mark.parametrize("role", ["query", "document"])
    async def test_empty_seeded_prompts_send_no_prompt_name(self, model, role):
        """sentence-transformers seeds ``{"query": "", "document": ""}`` on
        EVERY model; an empty prompt is not a defined prompt."""

        model._model.prompts = {"query": "", "document": ""}
        model._model.encode.return_value = np.array([[0.1, 0.2, 0.3, 0.4]])

        await model.embed(["text"], input_type=role)

        assert "prompt_name" not in self._encode_kwargs(model)

    async def test_model_without_prompts_attribute_sends_no_prompt_name(self, model):
        model._model.prompts = None
        model._model.encode.return_value = np.array([[0.1, 0.2, 0.3, 0.4]])

        await model.embed(["text"], input_type="query")

        assert "prompt_name" not in self._encode_kwargs(model)
