import logging

from tree.config.app_config import EmbeddingConfig, app_config
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


def _build_embedding_model(
    cfg: EmbeddingConfig, provider: str | None = None
) -> BaseEmbeddingModel:
    """Build an embedding model from a single ``EmbeddingConfig`` block.

    Holds the per-provider dispatch (mock / gemini / sentence-transformers /
    modal / voyage) in one place so the role-specific getters
    (:func:`get_resolution_embedding_model`, :func:`get_search_embedding_model`)
    never duplicate the ``if``-ladder. ``model`` / ``dimensions`` are read
    straight off ``cfg``; ``provider`` defaults to ``cfg.provider`` but may
    be overridden (the legacy :func:`get_embedding_model` shim forwards its
    optional ``provider`` argument this way, matching the pre-#040 behavior).
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
        return SentenceTransformerEmbeddingModel(
            model=cfg.model,
            dimensions=cfg.dimensions,
        )
    if provider == "modal":
        return ModalEmbeddingModel(
            api_key=settings.modal_embedding_api_key.get_secret_value(),
            model=cfg.model,
        )
    if provider == "voyage":
        # The project pinned the multimodal model family
        # (``voyage-multimodal-*`` against ``/v1/multimodalembeddings``)
        # as the single Voyage client in #038, so there is only one
        # code path here. Text-only models such as ``voyage-3`` are not
        # supported by the multimodal endpoint (Voyage returns
        # ``HTTP 400: Model voyage-3 is not supported``); operators
        # who flip an embedding block's ``model`` to a non-multimodal
        # id will see that error at the first ``embed`` call. The text
        # client added in #037 was removed in the same commit — see
        # ``tracker/038-consolidate-voyage-clients`` for context.
        return VoyageMultimodalEmbeddingModel(
            api_key=settings.voyage_api_key.get_secret_value(),
            model=cfg.model,
            output_dimension=cfg.dimensions,
        )
    raise ValueError(f"Unknown embedding provider: {provider}")


def get_resolution_embedding_model() -> BaseEmbeddingModel:
    """Factory for the resolution embedding model.

    Builds from ``app_config.models.resolution_embedding`` — the
    **transient** embedding used only by resolution's semantic stage
    (computed on the entity name, never persisted, not coupled to the
    live vector index). Swap this YAML block to point resolution at a
    lighter/cheaper model without touching persisted vectors.
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

    #039 split the single ``models.embedding`` config into a transient
    ``resolution_embedding`` and a persisted ``search_embedding``. This
    no-arg factory returns the **search** model so all existing call
    sites stay behavior-identical (the search model is what feeds dedup,
    query, and the persisted node ``embedding`` field).

    New code should call the role-named getter for its job —
    :func:`get_resolution_embedding_model` or
    :func:`get_search_embedding_model`. This shim is retained so #040's
    diff stays reviewable; #041/#043 migrate the call sites.

    The optional ``provider`` override builds the search model under the
    given provider (used by the existing per-provider unit tests).
    """

    return _build_embedding_model(app_config.models.search_embedding, provider=provider)
