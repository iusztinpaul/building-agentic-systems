import logging

from tree.config.app_config import EmbeddingConfig, app_config
from tree.config.settings import settings
from tree.models.base import BaseLLM, BaseEmbeddingModel
from tree.models.fake_model import MockEmbeddingModel
from tree.models.gemini import GeminiEmbeddingModel, GeminiLLM
from tree.models.voyage_embedding import VoyageTextEmbeddingModel
from tree.models.voyage_multimodal_embedding import VoyageMultimodalEmbeddingModel

# NOTE: ``sentence_transformers`` (→ transformers/torch/sklearn, ~7s import) and
# the two Modal clients (``modal_embedding`` / ``modal_llm``) are imported
# LAZILY inside their dispatch branches below, never at module level. The cloud MCP server runs ``voyage``/``gemini`` and must
# bind its port within Horizon's 60s readiness window; dragging torch into every
# boot blew that budget on cold serverless containers and the process was killed
# mid-import. Keeping these provider imports inside their branches means the
# common boot path never pays for them.

logger = logging.getLogger(__name__)


def get_llm(provider: str | None = None) -> BaseLLM:
    provider = provider or app_config.models.llm.provider

    if provider == "gemini":
        return GeminiLLM(
            api_key=settings.google_api_key.get_secret_value(),
            model=app_config.models.llm.model,
        )
    if provider == "modal":
        # Lazy import: keeps the Modal SDK off the common boot path. See the
        # module note — the client pulls `modal`, which the MCP cold boot must
        # not pay for.
        from tree.models.modal_catalog import modal_proxy_bearer
        from tree.models.modal_llm import ModalLLM

        return ModalLLM(
            proxy_token=modal_proxy_bearer(),
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
        # Lazy import: keeps the Modal SDK off the common boot path. See the
        # module note — the client pulls `modal`, which the MCP cold boot must
        # not pay for.
        from tree.models.modal_catalog import modal_proxy_bearer
        from tree.models.modal_embedding import ModalEmbeddingModel

        return ModalEmbeddingModel(
            proxy_token=modal_proxy_bearer(),
            model=cfg.model,
            dimensions=cfg.dimensions,
        )
    if provider == "voyage":
        # Voyage exposes two endpoints behind the same API: a **text** endpoint
        # at ``/v1/embeddings`` (the ``voyage-4`` / legacy ``voyage-3``
        # families) and a **multimodal** endpoint at
        # ``/v1/multimodalembeddings`` (the ``voyage-multimodal-*`` family).
        # They are NOT interchangeable — routing ``voyage-4`` to the multimodal
        # endpoint returns ``HTTP 400: Model voyage-4 is not supported``. Pick
        # the right client by model id; both coexist (#048, a partial revert of
        # #038's multimodal-only consolidation).
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


def search_embedding_identity() -> str:
    """Identity of the persisted embedding space: ``provider:model:dims:role``.

    ADR-009 decision 6: the 90-day Prefect ``INPUTS`` cache on
    ``embed-children`` / ``embed-entities`` was keyed on the text list alone,
    so re-extracting an already-seen document after a model swap replayed
    vectors from the OLD embedding space. Both tasks take this string as an
    input, which puts it in the cache key — a model, dimension or **Embedding
    role** change is a cache MISS instead of a silent replay.

    The role is the literal ``document`` rather than an argument: BOTH cached
    tasks embed vectors that are PERSISTED, and a persisted vector is always
    ``document`` (ADR-009 decision 5, forced by "dedup vector == persisted
    vector"). Nothing role-less or ``query``-shaped is cached, so there is no
    second identity to render. Pinning it here is also what retires the
    role-less 3-part keys: a vector cached before roles existed can never be
    replayed into a ``document`` corpus.

    Read from ``app_config`` at CALL time (never a module constant): Prefect
    re-imports this module inside flow-run subprocesses, and a
    ``TREE_MODELS__SEARCH_EMBEDDING__MODEL=voyage-3.5`` override must move the
    identity with it. With the shipped defaults the value is
    ``"voyage:voyage-4:1024:document"``.
    """

    cfg = app_config.models.search_embedding
    return f"{cfg.provider}:{cfg.model}:{cfg.dimensions}:document"


def get_embedding_model(provider: str | None = None) -> BaseEmbeddingModel:
    """Factory for embedding model instances (legacy shim).

    Returns the **search** model. New code should call the role-named
    getter for its job — :func:`get_resolution_embedding_model` or
    :func:`get_search_embedding_model`.

    The optional ``provider`` override builds the search model under the
    given provider (used by the per-provider unit tests).
    """

    return _build_embedding_model(app_config.models.search_embedding, provider=provider)
