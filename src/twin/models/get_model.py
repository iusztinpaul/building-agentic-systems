import logging

from twin.config.app_config import app_config
from twin.config.settings import settings
from twin.models.base import BaseLLM, BaseEmbeddingModel
from twin.models.fake_model import MockEmbeddingModel
from twin.models.gemini import GeminiEmbeddingModel, GeminiLLM
from twin.models.openai_embedding import OpenAICompatibleEmbeddingModel
from twin.models.sentence_transformer import SentenceTransformerEmbeddingModel

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
    if provider == "voyage":
        return OpenAICompatibleEmbeddingModel(
            base_url=settings.embedding_base_url,
            api_key=settings.embedding_api_key.get_secret_value(),
            model=app_config.models.embedding.model,
            dimensions=app_config.models.embedding.dimensions,
        )
    raise ValueError(f"Unknown embedding provider: {provider}")
