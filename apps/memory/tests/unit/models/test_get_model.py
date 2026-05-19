from unittest.mock import MagicMock

import pytest

from tree.models.fake_model import MockEmbeddingModel
from tree.models.gemini import GeminiEmbeddingModel, GeminiLLM
from tree.models.get_model import get_embedding_model, get_llm
from tree.models.modal_embedding import ModalEmbeddingModel
from tree.models.sentence_transformer import SentenceTransformerEmbeddingModel
from tree.models.voyage_embedding import VoyageEmbeddingModel
from tree.models.voyage_multimodal_embedding import VoyageMultimodalEmbeddingModel


@pytest.fixture(autouse=True)
def _mock_settings(mocker) -> None:
    mock_settings = MagicMock()
    mock_settings.google_api_key.get_secret_value.return_value = "fake-google-key"
    mock_settings.modal_embedding_api_key.get_secret_value.return_value = (
        "fake-modal-key"
    )
    mock_settings.voyage_api_key.get_secret_value.return_value = "fake-voyage-key"
    mocker.patch("tree.models.get_model.settings", mock_settings)


@pytest.fixture(autouse=True)
def _mock_app_config(mocker) -> None:
    mock_config = MagicMock()
    mock_config.models.llm.provider = "gemini"
    mock_config.models.llm.model = "gemini-2.0-flash"
    mock_config.models.embedding.provider = "mock"
    mock_config.models.embedding.model = "text-embedding-004"
    mock_config.models.embedding.dimensions = 256
    mocker.patch("tree.models.get_model.app_config", mock_config)


class TestGetLLM:
    def test_returns_gemini_llm_by_default(self) -> None:
        result = get_llm()

        assert isinstance(result, GeminiLLM)

    def test_returns_gemini_llm_with_explicit_provider(self) -> None:
        result = get_llm(provider="gemini")

        assert isinstance(result, GeminiLLM)

    def test_raises_for_unknown_provider(self) -> None:
        with pytest.raises(ValueError, match="Unknown LLM provider: unknown"):
            get_llm(provider="unknown")


class TestGetEmbeddingModel:
    def test_returns_mock_by_default(self) -> None:
        result = get_embedding_model()

        assert isinstance(result, MockEmbeddingModel)

    def test_returns_mock_with_explicit_provider(self) -> None:
        result = get_embedding_model(provider="mock")

        assert isinstance(result, MockEmbeddingModel)

    def test_returns_gemini_embedding(self) -> None:
        result = get_embedding_model(provider="gemini")

        assert isinstance(result, GeminiEmbeddingModel)

    def test_returns_sentence_transformers(self, mocker) -> None:
        mocker.patch(
            "tree.models.sentence_transformer.SentenceTransformer",
        )

        result = get_embedding_model(provider="sentence-transformers")

        assert isinstance(result, SentenceTransformerEmbeddingModel)

    def test_returns_modal_embedding(self) -> None:
        result = get_embedding_model(provider="modal")

        assert isinstance(result, ModalEmbeddingModel)

    def test_returns_voyage_text_embedding_by_default(self, mocker) -> None:
        """The post-#034 YAML default is ``voyage / voyage-3 / 1024``; ``voyage-3``
        is a text-only model and MUST be routed through the
        ``/v1/embeddings`` client, not the multimodal one.

        Regression test for #037 — the multimodal endpoint returns
        ``HTTP 400: Model voyage-3 is not supported``, which crashed the
        memory-extraction flow under the post-#034 defaults.
        """

        # _mock_app_config sets model="text-embedding-004"; override to
        # voyage-3 so the routing branch is exercised.
        mocker.patch(
            "tree.models.get_model.app_config.models.embedding.model", "voyage-3"
        )
        mocker.patch(
            "tree.models.get_model.app_config.models.embedding.dimensions", 1024
        )

        result = get_embedding_model(provider="voyage")

        assert isinstance(result, VoyageEmbeddingModel)
        assert not isinstance(result, VoyageMultimodalEmbeddingModel)

    def test_returns_voyage_multimodal_for_multimodal_model(self, mocker) -> None:
        """``voyage-multimodal-3`` / ``voyage-multimodal-3.5`` still go
        to the multimodal client at ``/v1/multimodalembeddings``."""

        mocker.patch(
            "tree.models.get_model.app_config.models.embedding.model",
            "voyage-multimodal-3",
        )
        mocker.patch(
            "tree.models.get_model.app_config.models.embedding.dimensions", 1024
        )

        result = get_embedding_model(provider="voyage")

        assert isinstance(result, VoyageMultimodalEmbeddingModel)

    def test_raises_for_unknown_provider(self) -> None:
        with pytest.raises(ValueError, match="Unknown embedding provider: unknown"):
            get_embedding_model(provider="unknown")
