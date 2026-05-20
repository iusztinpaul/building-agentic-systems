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
    """Factory for embedding model instances.

    #039 split the single ``models.embedding`` config into a transient
    ``resolution_embedding`` and a persisted ``search_embedding``. This
    no-arg factory returns the **search** model so all existing call
    sites stay behavior-identical (the search model is what feeds dedup,
    query, and the persisted node ``embedding`` field). Task #040 grows
    the dual factory entry points that select between the two.
    """

    embedding = app_config.models.search_embedding
    provider = provider or embedding.provider

    if provider == "mock":
        logger.warning("Using mock embedding model (random vectors)")
        return MockEmbeddingModel(
            dimensions=embedding.dimensions,
        )
    if provider == "gemini":
        return GeminiEmbeddingModel(
            api_key=settings.google_api_key.get_secret_value(),
            model=embedding.model,
            dimensions=embedding.dimensions,
        )
    if provider == "sentence-transformers":
        return SentenceTransformerEmbeddingModel(
            model=embedding.model,
            dimensions=embedding.dimensions,
        )
    if provider == "modal":
        return ModalEmbeddingModel(
            api_key=settings.modal_embedding_api_key.get_secret_value(),
            model=embedding.model,
        )
    if provider == "voyage":
        # The project pinned the multimodal model family
        # (``voyage-multimodal-*`` against ``/v1/multimodalembeddings``)
        # as the single Voyage client in #038, so there is only one
        # code path here. Text-only models such as ``voyage-3`` are not
        # supported by the multimodal endpoint (Voyage returns
        # ``HTTP 400: Model voyage-3 is not supported``); operators
        # who flip ``models.search_embedding.model`` to a non-multimodal
        # id will see that error at the first ``embed`` call. The text
        # client added in #037 was removed in the same commit — see
        # ``tracker/038-consolidate-voyage-clients`` for context.
        return VoyageMultimodalEmbeddingModel(
            api_key=settings.voyage_api_key.get_secret_value(),
            model=embedding.model,
            output_dimension=embedding.dimensions,
        )
    raise ValueError(f"Unknown embedding provider: {provider}")
