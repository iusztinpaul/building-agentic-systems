"""Unit tests for the ``BaseEmbeddingModel.dimensions`` property surface.

Every concrete embedding model must report a positive integer dimensionality
so ``ensure_indexes`` can drive the Atlas vector index's ``numDimensions``
from the live model instance (see ``tree.memory.indexing.core``). These
tests pin the property down per subclass without touching any external
network/process.
"""

from unittest.mock import MagicMock

import pytest

from tree.models.base import BaseEmbeddingModel
from tree.models.exceptions import ModelError
from tree.models.fake_model import FakeEmbeddingModel, MockEmbeddingModel
from tree.models.gemini import GeminiEmbeddingModel
from tree.models.modal_embedding import ModalEmbeddingModel
from tree.models.sentence_transformer import SentenceTransformerEmbeddingModel
from tree.models.voyage_multimodal_embedding import VoyageMultimodalEmbeddingModel


class TestBaseDeclaresDimensions:
    def test_dimensions_is_a_property_descriptor(self) -> None:
        """``BaseEmbeddingModel.dimensions`` must be exposed as a property
        descriptor (not a method) — every concrete implementation accesses
        it as ``model.dimensions``."""

        descriptor = BaseEmbeddingModel.__dict__["dimensions"]
        assert isinstance(descriptor, property)

    def test_cannot_instantiate_base_without_dimensions(self) -> None:
        """An abstract subclass that omits ``dimensions`` must remain
        abstract."""

        class _NoDimensions(BaseEmbeddingModel):
            async def embed(self, texts):  # type: ignore[override]
                return []

        with pytest.raises(TypeError):
            _NoDimensions()  # type: ignore[abstract]


class TestFakeEmbeddingModelDimensions:
    def test_default_matches_app_config(self) -> None:
        model = FakeEmbeddingModel()
        assert isinstance(model.dimensions, int)
        assert model.dimensions > 0

    def test_explicit_dimensions(self) -> None:
        model = FakeEmbeddingModel(dimensions=16)
        assert model.dimensions == 16

    async def test_embed_vector_length_matches_property(self) -> None:
        model = FakeEmbeddingModel(dimensions=12)
        vectors = await model.embed(["hello"])
        assert len(vectors[0]) == model.dimensions == 12


class TestMockEmbeddingModelDimensions:
    def test_constructor_configurable(self) -> None:
        assert MockEmbeddingModel(dimensions=16).dimensions == 16

    async def test_embed_returns_vectors_of_configured_length(self) -> None:
        model = MockEmbeddingModel(dimensions=16)
        vectors = await model.embed(["foo"])
        assert len(vectors) == 1
        assert len(vectors[0]) == 16


class TestGeminiEmbeddingModelDimensions:
    def test_returns_configured_dimensions(self, mocker) -> None:
        mocker.patch("tree.models.gemini.genai.Client", return_value=MagicMock())
        model = GeminiEmbeddingModel(
            api_key="fake", model="text-embedding-004", dimensions=256
        )
        assert model.dimensions == 256


class TestSentenceTransformerDimensions:
    def test_returns_truncated_dimensions(self, mocker) -> None:
        mocker.patch("tree.models.sentence_transformer.SentenceTransformer")
        model = SentenceTransformerEmbeddingModel(
            model="voyageai/voyage-4-nano", dimensions=384
        )
        assert model.dimensions == 384


class TestModalEmbeddingDimensions:
    def test_returns_explicit_dimensions(self) -> None:
        model = ModalEmbeddingModel(
            api_key="fake",
            model="voyageai/voyage-4-nano",
            dimensions=512,
        )
        assert model.dimensions == 512

    def test_falls_back_to_known_native_dimensions(self) -> None:
        model = ModalEmbeddingModel(
            api_key="fake",
            model="voyageai/voyage-4-nano",
            dimensions=None,
        )
        # Documented in modal_embedding._MODEL_NATIVE_DIMENSIONS.
        assert model.dimensions == 1024

    def test_raises_for_unknown_model_without_dimensions(self) -> None:
        model = ModalEmbeddingModel(
            api_key="fake",
            model="some/unknown-model",
            dimensions=None,
        )
        with pytest.raises(ModelError, match="native dimension"):
            _ = model.dimensions


class TestVoyageMultimodalDimensions:
    def test_returns_explicit_output_dimension(self) -> None:
        model = VoyageMultimodalEmbeddingModel(
            api_key="fake",
            model="voyage-multimodal-3.5",
            output_dimension=512,
        )
        assert model.dimensions == 512

    def test_falls_back_to_known_native_dimensions(self) -> None:
        model = VoyageMultimodalEmbeddingModel(
            api_key="fake",
            model="voyage-multimodal-3",
        )
        assert model.dimensions == 1024

    def test_raises_for_unknown_model_without_output_dimension(self) -> None:
        model = VoyageMultimodalEmbeddingModel(
            api_key="fake",
            model="voyage-multimodal-future",
        )
        with pytest.raises(ModelError, match="native dimension"):
            _ = model.dimensions
