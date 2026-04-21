import logging

from tree.config.app_config import app_config
from tree.config.settings import settings
from tree.models.base import BaseLLM, BaseEmbeddingModel
from tree.models.fake_model import MockEmbeddingModel
from tree.models.gemini import GeminiEmbeddingModel, GeminiLLM
from tree.models.modal_embedding import ModalEmbeddingModel
from tree.models.sentence_transformer import SentenceTransformerEmbeddingModel
from tree.models.voyage_multimodal_embedding import VoyageMultimodalEmbeddingModel

logger = logging.getLogger(__name__)


def get_llm(provider: str | None = None) -> BaseLLM:
    """Factory for LLM instances."""

    provider = provider or app_config.models.llm.provider

    if provider == "gemini":
        return GeminiLLM(
            api_key=settings.google_api_key.get_secret_value(),
            model=app_config.models.llm.model,
        )
    raise ValueError(f"Unknown LLM provider: {provider}")


def get_embedding_model(provider: str | None = None) -> BaseEmbeddingModel:
    """Factory for embedding model instances."""

    provider = provider or app_config.models.embedding.provider

    if provider == "mock":
        logger.warning("Using mock embedding model (random vectors)")
        return MockEmbeddingModel(
            dimensions=app_config.models.embedding.dimensions,
        )
    if provider == "gemini":
        return GeminiEmbeddingModel(
            api_key=settings.google_api_key.get_secret_value(),
            model=app_config.models.embedding.model,
            dimensions=app_config.models.embedding.dimensions,
        )
    if provider == "sentence-transformers":
        return SentenceTransformerEmbeddingModel(
            model=app_config.models.embedding.model,
            dimensions=app_config.models.embedding.dimensions,
        )
    if provider == "modal":
        return ModalEmbeddingModel(
            api_key=settings.modal_embedding_api_key.get_secret_value(),
            model=app_config.models.embedding.model,
        )
    if provider == "voyage":
        return VoyageMultimodalEmbeddingModel(
            api_key=settings.voyage_api_key.get_secret_value(),
            model=app_config.models.embedding.model,
            output_dimension=app_config.models.embedding.dimensions,
        )
    raise ValueError(f"Unknown embedding provider: {provider}")
