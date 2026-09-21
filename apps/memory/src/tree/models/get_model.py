import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Literal

from tree.config.app_config import EmbeddingConfig, app_config
from tree.config.settings import settings
from tree.models.base import BaseLLM, BaseEmbeddingModel
from tree.models.fake_model import MockEmbeddingModel
from tree.models.gemini import GeminiEmbeddingModel, GeminiLLM
from tree.models.voyage_embedding import VoyageTextEmbeddingModel
from tree.models.voyage_multimodal_embedding import VoyageMultimodalEmbeddingModel

# NOTE: ``sentence_transformers`` (→ transformers/torch/sklearn, ~7s import) and
# the two Modal clients (``modal_embedding`` / ``modal_llm``) are imported
# LAZILY inside their dispatch branches below, never at module level. The cloud
# MCP server runs ``voyage``/``gemini`` and must bind its port within Horizon's
# 60s readiness window; dragging torch into every boot blew that budget on cold
# serverless containers and the process was killed mid-import. Keeping these
# provider imports inside their branches means the common boot path never pays
# for them.

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


def llm_identity() -> str:
    """Identity of the LLM behind the cached LLM tasks: ``provider:model``.

    Decision 6's rule ("caches carry the identity of the model that produced
    them") applied to the ``models.llm`` switch of ADR-009 decision 10. The
    ``INPUTS`` caches on ``llm-extract-entities`` (30 days) and
    ``summarise-cluster`` (90 days) named nothing about the LLM, so after a
    ``gemini`` -> ``modal`` flip a document / cluster already seen inside the
    window REPLAYED the previous model's JSON and the new model was never
    called. Both tasks take this string as an input, which puts it in the key
    — a provider or model-id change is a cache MISS instead of a silent replay.

    Two parts only, unlike :func:`search_embedding_identity`: an LLM has no
    dimensions and no **Embedding role**.

    Read from ``app_config`` at CALL time (never a module constant): Prefect
    re-imports this module inside flow-run subprocesses, and a
    ``TREE_MODELS__LLM__MODEL=gemini-2.5-flash`` override must move the
    identity with it. With the shipped defaults the value is
    ``"gemini:gemini-3.1-flash-lite"``.
    """

    cfg = app_config.models.llm
    return f"{cfg.provider}:{cfg.model}"


def get_embedding_model(provider: str | None = None) -> BaseEmbeddingModel:
    """Factory for embedding model instances (legacy shim).

    Returns the **search** model. New code should call the role-named
    getter for its job — :func:`get_resolution_embedding_model` or
    :func:`get_search_embedding_model`.

    The optional ``provider`` override builds the search model under the
    given provider (used by the per-provider unit tests).
    """

    return _build_embedding_model(app_config.models.search_embedding, provider=provider)


def modal_backed_models(
    *blocks: Literal["llm", "resolution_embedding", "search_embedding"],
) -> list[object]:
    """Build the models of ``blocks`` that are served by Modal — and only those.

    The caller-side gate in front of the **Pre-warm**: it reads
    ``app_config.models.<block>.provider`` BEFORE calling that block's factory,
    so a provider with nothing to warm is never CONSTRUCTED for the pre-warm's
    sake. The seams used to build every block and throw the instance away;
    under ``sentence-transformers`` that is a ``SentenceTransformer(...)`` —
    a torch weight load, seconds plus memory — paid by every run, including a
    fully cached one that would otherwise build no model at all (#149 QA).

    ``== "modal"`` IS the rule: the two Modal clients are the only models with
    an ``ensure_warm``, so no capability registry and no new config key. A
    second warmable provider would make one worth having; one does not.

    Read at CALL time (never a module constant), like
    :func:`search_embedding_identity`: Prefect re-imports this module inside
    flow-run subprocesses and a ``TREE_MODELS__LLM__PROVIDER=modal`` override
    must move the gate with it. The factories are looked up in the module
    globals for the same reason.

    The returned instances are THROW-AWAYS: what the later Prefect tasks
    inherit is the warm server, not the object (ADR-009 §11).
    """

    factories: dict[str, Callable[[], object]] = {
        "llm": get_llm,
        "resolution_embedding": get_resolution_embedding_model,
        "search_embedding": get_search_embedding_model,
    }

    return [
        factories[block]()
        for block in blocks
        if getattr(app_config.models, block).provider == "modal"
    ]


async def prewarm_models(*models: object) -> None:
    """Warm every DISTINCT Modal server behind ``models``, concurrently.

    The **Pre-warm** of ADR-009 §11, ported from the `pulse` codebase: it runs
    at the top of a run that fans out over Modal-backed models — before
    document 1 — so a cold start is paid ONCE, by this call, instead of by each
    of the N model instances the Prefect tasks build later. What is shared is
    the warm SERVER, not the instance: every later instance's first poll is a
    single GET answering 200.

    DUCK-TYPED on ``ensure_warm``, which only the two Modal clients have — for
    Voyage, Gemini, sentence-transformers and the mock this is a no-op that
    awaits nothing, creates no task and logs nothing. DEDUPED on ``warm_key``
    (the Modal app name): the resolution and the search embedding may be the
    same app, and one boot serves both.

    FAIL-FAST WITH AN EXPLICIT CANCEL. ``asyncio.gather`` propagates the first
    failure but leaves its siblings RUNNING, so a 403 on one server would
    otherwise wait out the other's 600 s poll — and leave that poll knocking on
    the loop the run is about to use. The ``finally`` cancels every task and
    reaps it inside this call. The first exception propagates unchanged
    (``ModelError`` for a 4xx, ``ExtractionError`` for a spent deadline), which
    is what fails the run with ZERO documents attempted.

    IDEMPOTENT: on an already-warm gate each ``ensure_warm`` returns without a
    request, so a flow-level retry re-runs this harmlessly. Deliberately NOT a
    Prefect task — a cached "warm" is the "warm at t0" fallacy, task retries
    would multiply the poller's deadline, and it must raise OUTSIDE per-document
    error handling.

    No ``modal`` import lives here or anywhere on this module's import path:
    the MCP boot must not pay for the Modal SDK.
    """

    warms: dict[object, Callable[[], Awaitable[None]]] = {}
    for model in models:
        warm = getattr(model, "ensure_warm", None)
        if warm is None:
            continue
        warms.setdefault(getattr(model, "warm_key", id(model)), warm)

    if not warms:
        return

    logger.info(
        "Pre-warming %d Modal server(s): %s",
        len(warms),
        ", ".join(str(key) for key in warms),
    )

    # Built INCREMENTALLY, inside the ``try``: a duck whose ``ensure_warm`` is
    # not a coroutine function makes ``create_task`` raise, and a list
    # comprehension outside the ``try`` would let that ``TypeError`` escape with
    # an EARLIER model's real poll still scheduled (#149 QA). The ``finally``
    # reaps whatever was appended before the raise; the ``TypeError`` itself
    # propagates unchanged.
    tasks: list[asyncio.Task[None]] = []
    try:
        for warm in warms.values():
            tasks.append(asyncio.create_task(warm()))
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
        # Awaited so a cancelled poll is actually reaped here, not left pending
        # on the loop the pipeline is about to fan out on.
        await asyncio.gather(*tasks, return_exceptions=True)
