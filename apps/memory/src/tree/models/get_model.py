import logging

from tree.config.app_config import EmbeddingConfig, app_config
from tree.config.settings import settings
from tree.models.base import BaseLLM, BaseEmbeddingModel
from tree.models.fake_model import MockEmbeddingModel
from tree.models.gemini import GeminiEmbeddingModel, GeminiLLM
from tree.models.voyage_embedding import VoyageTextEmbeddingModel
from tree.models.voyage_multimodal_embedding import VoyageMultimodalEmbeddingModel

# NOTE: ``sentence_transformers`` (→ transformers/torch/sklearn, ~7s import) and
# ``modal_embedding`` are imported LAZILY inside their dispatch branches below,
# never at module level. The cloud MCP server runs ``voyage``/``gemini`` and must
# bind its port within Horizon's 60s readiness window; dragging torch into every
# boot blew that budget on cold serverless containers and the process was killed
# mid-import. Keeping these provider imports inside their branches means the
# common boot path never pays for them.

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


def _build_embedding_model(
    cfg: EmbeddingConfig, provider: str | None = None
) -> BaseEmbeddingModel:
    """Build an embedding model from a single ``EmbeddingConfig`` block.

    Holds the per-provider dispatch in one place so the role-specific
    getters never duplicate the ``if``-ladder. ``provider`` defaults to
    ``cfg.provider`` but may be overridden.
    """

    provider = provider or cfg.provider

    if provider == "mock":
        logger.warning("Using mock embedding model (random vectors)")
        return MockEmbeddingModel(
            dimensions=cfg.dimensions,
        )
    if provider == "gemini":
        return GeminiEmbeddingModel(
            api_key=settings.google_api_key.get_secret_value(),
            model=cfg.model,
            dimensions=cfg.dimensions,
        )
    if provider == "sentence-transformers":
        # Lazy import: pulls transformers/torch/sklearn (~7s). See module note.
        from tree.models.sentence_transformer import SentenceTransformerEmbeddingModel

        return SentenceTransformerEmbeddingModel(
            model=cfg.model,
            dimensions=cfg.dimensions,
        )
    if provider == "modal":
        # Lazy import: keeps the Modal client off the common boot path.
        from tree.models.modal_embedding import ModalEmbeddingModel

        return ModalEmbeddingModel(
            api_key=settings.modal_embedding_api_key.get_secret_value(),
            model=cfg.model,
        )
    if provider == "voyage":
        # Voyage exposes two endpoints behind the same API: a **text** endpoint
        # at ``/v1/embeddings`` (the ``voyage-3`` family) and a **multimodal**
        # endpoint at ``/v1/multimodalembeddings`` (the ``voyage-multimodal-*``
        # family). They are NOT interchangeable — routing ``voyage-3`` to the
        # multimodal endpoint returns ``HTTP 400: Model voyage-3 is not
        # supported``. Pick the right client by model id; both coexist (#048,
        # a partial revert of #038's multimodal-only consolidation).
        if cfg.model.startswith("voyage-multimodal"):
            return VoyageMultimodalEmbeddingModel(
                api_key=settings.voyage_api_key.get_secret_value(),
                model=cfg.model,
                output_dimension=cfg.dimensions,
            )
        return VoyageTextEmbeddingModel(
            api_key=settings.voyage_api_key.get_secret_value(),
            model=cfg.model,
            output_dimension=cfg.dimensions,
        )
    raise ValueError(f"Unknown embedding provider: {provider}")


def get_resolution_embedding_model() -> BaseEmbeddingModel:
    """Factory for the resolution embedding model.

    Builds from ``app_config.models.resolution_embedding`` — the
    **transient** embedding used only by resolution's semantic stage
    (computed on the entity name, never persisted, not index-coupled).
    Swap this YAML block to point resolution at a lighter model without
    touching persisted vectors.
    """

    return _build_embedding_model(app_config.models.resolution_embedding)


def get_search_embedding_model() -> BaseEmbeddingModel:
    """Factory for the search embedding model.

    Builds from ``app_config.models.search_embedding`` — the **persisted**
    embedding written to the node ``embedding`` field and dimension-coupled
    to the live mongot ``vector_index``. Feeds dedup, query, and search.
    """

    return _build_embedding_model(app_config.models.search_embedding)


def get_embedding_model(provider: str | None = None) -> BaseEmbeddingModel:
    """Factory for embedding model instances (legacy shim).

    Returns the **search** model. New code should call the role-named
    getter for its job — :func:`get_resolution_embedding_model` or
    :func:`get_search_embedding_model`.

    The optional ``provider`` override builds the search model under the
    given provider (used by the per-provider unit tests).
    """

    return _build_embedding_model(app_config.models.search_embedding, provider=provider)
