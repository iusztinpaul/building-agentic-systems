"""The client for ONE **Embedding catalog** model, on any **Serving path**.

Everything model-specific — the Modal app, the prompts, the native and the
effective vector width — comes from the catalog entry (ADR-009 §3); the URL,
the authenticated health warm-up and the served model id come from
:mod:`tree.models.modal_server`. A **Dedicated endpoint** and both fallback
scripts expose the same app name, the same server class and the same **Proxy
token** auth, so this client never reads how the entry is served: moving a
model down the ladder changes YAML, never code.

Dimensions (ADR-009 §3): no request of ours carries an OpenAI ``dimensions``
parameter — vLLM answers 400 unless the model's HF config is flagged
Matryoshka and a managed recipe exposes no such flag. The server is always
asked for its native width; every response is asserted to BE that width,
truncated + L2-renormalised to the effective width with
:func:`tree.models.modal_catalog.truncate_embedding`, and asserted again.
A vector that is not exactly ``dimensions`` wide therefore cannot reach the
mongot vector index — not with a stale catalog entry, not with a recipe that
dropped the model's projection head.

Auth (ADR-009 §4): the **Proxy token**'s two halves joined by a ``.`` are both
the ``Authorization: Bearer`` value of the health warm-up and the ``api_key``
of the ``AsyncOpenAI`` client — Modal documents the joined form as "the same
scheme the OpenAI API uses" (docs/guide/webhook-proxy-auth, read 2026-09-19).
"""

import asyncio
import logging

from openai import AsyncOpenAI

from tree.config.app_config import ModalEmbeddingModelConfig
from tree.models.base import BaseEmbeddingModel, EmbeddingRole
from tree.models.exceptions import ExtractionError, ModelError
from tree.models.modal_catalog import (
    EMBEDDING_SERVER_NAME,
    get_catalog_entry,
    prompt_for,
    truncate_embedding,
)
from tree.models.modal_server import (
    resolve_server_url,
    served_model_id,
    wait_until_healthy,
)
from tree.observability import record_embedding_usage, track

logger = logging.getLogger(__name__)


def _record_modal_usage(model: str, response: object) -> None:
    """Record Modal embedding token usage on the current Opik span.

    Self-hosted, so ``total_cost=0`` — the user explicitly wants the token
    counts logged even without a dollar cost. The OpenAI-compatible response
    carries a ``usage`` object with ``total_tokens``; when it is absent the
    span is recorded without usage. Fully exception-safe — telemetry must never
    break the embed call.

    ``model`` is the catalog ``repo_id``, never the discovered served id: the
    id a managed recipe happens to serve under is an implementation detail,
    while the repo id is what the YAML, the cost tables and
    ``search_embedding_identity`` all name.
    """

    try:
        usage = getattr(response, "usage", None)
        total_tokens = getattr(usage, "total_tokens", None) if usage else None
        record_embedding_usage(
            provider="modal",
            model=model,
            total_tokens=total_tokens,
            total_cost=0.0,
        )
    except Exception as exc:  # noqa: BLE001 — telemetry must never break embed
        logger.debug("Opik Modal usage recording no-op: %s", exc)


def _effective_dimensions(
    entry: ModalEmbeddingModelConfig, requested: int | None
) -> int:
    """The width this model will actually return, or a loud failure.

    ``None`` means "whatever the model serves" — the native width. Anything
    else must be either the native width or a size the entry lists as
    Matryoshka-truncatable; a model cannot invent components it was never
    trained to place, so the alternative to failing here is a silently wrong
    vector space.

    Raises:
        ModelError: ``requested`` is neither the native width nor a listed
            Matryoshka size. The message carries both, so the operator can
            either fix the config or extend the catalog entry.
    """

    if requested is None or requested == entry.native_dimensions:
        return entry.native_dimensions
    if requested in entry.matryoshka_dimensions:
        return requested
    raise ModelError(
        f"{entry.repo_id} cannot produce {requested}-d vectors: native "
        f"{entry.native_dimensions}, matryoshka_dimensions "
        f"{entry.matryoshka_dimensions}. Set models.<block>.dimensions to "
        f"{entry.native_dimensions} or extend the Embedding catalog entry."
    )


class ModalEmbeddingModel(BaseEmbeddingModel):
    """Embedding model served on Modal, resolved from the Embedding catalog.

    Construction is offline and total: an unknown model id, a missing **Proxy
    token** and a width the model cannot produce all fail HERE, before a
    single network call is made. The first ``embed()`` then resolves the URL,
    warms the container through proxy auth and discovers the served model id
    once; every later call reuses them.
    """

    def __init__(
        self,
        proxy_token: str,
        model: str,
        dimensions: int | None = None,
        health_timeout: float = 300.0,
    ) -> None:
        if not proxy_token:
            raise ModelError(
                "Modal proxy token is required. Set MODAL_PROXY_TOKEN_ID and "
                "MODAL_PROXY_TOKEN_SECRET."
            )

        self._proxy_token = proxy_token
        self._entry = get_catalog_entry(model)
        self._dimensions = _effective_dimensions(self._entry, dimensions)
        self._health_timeout = health_timeout
        # Replaced by the id discovered from /v1/models on the first embed();
        # the repo id is the fallback the discovery itself already applies.
        self._served_model = self._entry.repo_id
        self._client: AsyncOpenAI | None = None
        # One instance is shared by concurrent callers (an MCP tool call and a
        # batch dispatch can embed at the same time), so the initialisation is
        # a critical section, not a check-then-act.
        self._init_lock = asyncio.Lock()

    @property
    def dimensions(self) -> int:
        """Width of every vector ``embed()`` returns — after truncation."""

        return self._dimensions

    async def _ensure_initialised(self) -> None:
        """Resolve the server, warm it, discover its model id, build a client.

        Runs EXACTLY once per instance, however many callers embed at the same
        time: the whole sequence is held under ``_init_lock`` and the
        already-initialised check is made both before the lock (the cheap
        common case) and inside it (the race). ``wait_until_healthy`` is one
        long-held request that wakes a scaled-to-zero GPU container, so N
        racing first calls would mean N wake-ups on a billed endpoint plus N
        orphaned HTTP clients.

        A failure leaves NOTHING half-built — ``_client`` and the served id are
        assigned only after every step succeeded — so the next call retries the
        whole sequence. A container that timed out is worth a second attempt,
        and nothing here is a mutation.
        """

        if self._client is not None:
            return

        async with self._init_lock:
            if self._client is not None:
                return

            url = await resolve_server_url(self._entry)
            await wait_until_healthy(url, self._proxy_token, self._health_timeout)
            served = await served_model_id(
                url, self._proxy_token, default=self._entry.repo_id
            )
            # The Proxy token IS the OpenAI api_key: the client sends it as
            # `Authorization: Bearer <id>.<secret>`, which is what Modal's edge
            # checks before a GPU container wakes.
            client = AsyncOpenAI(base_url=f"{url}/v1", api_key=self._proxy_token)

            self._served_model = served
            self._client = client

            truncated = (
                ""
                if self._dimensions == self._entry.native_dimensions
                else " (truncated client-side)"
            )
            logger.info(
                "ModalEmbeddingModel ready: app=%s server=%s served_model=%s "
                "native=%d dimensions=%d%s url=%s/v1",
                self._entry.app_name,
                EMBEDDING_SERVER_NAME,
                served,
                self._entry.native_dimensions,
                self._dimensions,
                truncated,
                url,
            )

    @track(type="llm", name="modal-embed")
    async def embed(
        self, texts: list[str], input_type: EmbeddingRole | None = None
    ) -> list[list[float]]:
        """Embed ``texts`` on whichever Serving path serves this model.

        ``input_type`` — the **Embedding role** — becomes a client-side prefix
        from the catalog entry (ADR-009 §5): OpenAI-compatible
        ``/v1/embeddings`` has no role field on any path. ``None``, or a model
        that defines no prompt for the role, leaves the texts untouched.

        An empty batch returns ``[]`` without initialising anything — the same
        guard :class:`~tree.models.voyage_embedding.VoyageTextEmbeddingModel`
        carries, and here it also keeps an empty call from waking a
        scaled-to-zero GPU container (which the server would answer with an
        empty ``data`` list the OpenAI SDK refuses to parse anyway).

        Wrapped in an Opik ``llm``-type span; on success the ``usage`` token
        counts are recorded with ``total_cost=0`` (self-hosted). Recording is
        fully fail-open.

        Raises:
            ExtractionError: the call failed (retryable), or the server
                answered with vectors of the wrong width.
        """

        if not texts:
            return []

        await self._ensure_initialised()
        assert self._client is not None  # guaranteed by _ensure_initialised

        prompt = prompt_for(self._entry, input_type)
        try:
            response = await self._client.embeddings.create(
                input=[prompt + text for text in texts],
                model=self._served_model,
                # Explicit: the SDK otherwise asks for base64, while the shared
                # smoke test (`modal_server._embed`) asks for float. One wire
                # contract on all three Serving paths, and no engine or recipe
                # has to implement base64 for us.
                encoding_format="float",
            )
        except Exception as exc:
            raise ExtractionError(f"Embedding call failed: {exc}") from exc

        _record_modal_usage(self._entry.repo_id, response)
        return self._checked_vectors([item.embedding for item in response.data])

    def _checked_vectors(self, vectors: list[list[float]]) -> list[list[float]]:
        """Assert the wire width, truncate, assert the returned width.

        The lesson of #138: the catalog was wrong about a model once
        (``hidden_size`` read where the projection head's width was meant), and
        a wrong entry here would write vectors of another width into a
        1024-d index. Asserting BOTH ends means the only outcomes are the exact
        width the memory asked for and an exception.
        """

        native = self._entry.native_dimensions
        for vector in vectors:
            if len(vector) != native:
                raise ExtractionError(
                    f"{self._entry.repo_id} returned {len(vector)}-d vectors "
                    f"but its Embedding catalog entry says native_dimensions "
                    f"{native} — fix modal.embedding_models (or the Serving "
                    "path dropped the model's projection head). No vector was "
                    "returned."
                )

        if self._dimensions != native:
            vectors = [
                truncate_embedding(vector, self._dimensions) for vector in vectors
            ]

        for vector in vectors:
            if len(vector) != self._dimensions:
                raise ExtractionError(
                    f"{self._entry.repo_id} returned {len(vector)}-d vectors "
                    f"after client-side truncation to {self._dimensions} — no "
                    "vector was returned."
                )
        return vectors
