"""The client for ONE **Embedding catalog** model, on any **Serving path**.

Everything model-specific — the Modal app, the prompts, the native and the
effective vector width — comes from the catalog entry (ADR-009 §3); the URL and
the served model id come from :mod:`tree.models.modal_server`, and the cold
start is waited out by the **Warm gate** this client COMPOSES
(:mod:`tree.models.modal_warmup`). A **Dedicated endpoint** and both fallback
scripts expose the same app name, the same server class and the same **Proxy
token** auth, so this client never reads how the entry is served: moving a
model down the ladder changes YAML, never code.

Warming (ADR-009 §11): a scaled-to-zero Modal server answers ``/health`` with
503 in about a second and boots BECAUSE it is polled, so the gate polls. Its
single-flight body is ALL-OR-NOTHING — URL lookup, health poll, served-model
discovery, client construction — and it runs again, whole, when a call answers
cold (Modal scaled the server back to zero mid-run) or when the instance is
used on a second event loop.

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

import logging

from openai import AsyncOpenAI

from tree.config.app_config import ModalEmbeddingModelConfig, app_config
from tree.models.base import BaseEmbeddingModel, EmbeddingRole
from tree.models.exceptions import ExtractionError, ModelError
from tree.models.modal_catalog import (
    EMBEDDING_SERVER_NAME,
    get_catalog_entry,
    prompt_for,
    truncate_embedding,
)
from tree.models.modal_server import resolve_server_url, served_model_id
from tree.models.modal_warmup import WarmGate, poll_health
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
    polls the container warm through proxy auth and discovers the served model
    id once; every later call reuses them until one answers cold.
    """

    def __init__(
        self,
        proxy_token: str,
        model: str,
        dimensions: int | None = None,
        warmup_deadline_s: float | None = None,
    ) -> None:
        if not proxy_token:
            raise ModelError(
                "Modal proxy token is required. Set MODAL_PROXY_TOKEN_ID and "
                "MODAL_PROXY_TOKEN_SECRET."
            )

        self._proxy_token = proxy_token
        self._entry = get_catalog_entry(model)
        self._dimensions = _effective_dimensions(self._entry, dimensions)
        # Read at CONSTRUCTION, like every other knob this client takes: one
        # budget per server (ADR-009 §11), overridable per process with
        # TREE_MODAL__WARMUP_DEADLINE_S.
        self._warmup_deadline_s = (
            app_config.modal.warmup_deadline_s
            if warmup_deadline_s is None
            else warmup_deadline_s
        )
        # Replaced by the id discovered from /v1/models on the first warm; the
        # repo id is the fallback the discovery itself already applies.
        self._served_model = self._entry.repo_id
        self._client: AsyncOpenAI | None = None
        # One instance is shared by concurrent callers (an MCP tool call and a
        # batch dispatch can embed at the same time), so warming is a critical
        # section, not a check-then-act — and it stays single-flight across a
        # re-warm and an event-loop change.
        self._gate = WarmGate(self._warm, label=self._entry.app_name)

    @property
    def dimensions(self) -> int:
        """Width of every vector ``embed()`` returns — after truncation."""

        return self._dimensions

    @property
    def warm_key(self) -> str:
        """What identifies the SERVER this model talks to (its Modal app).

        The **Pre-warm** warms every DISTINCT Modal app once: two catalog
        models on one app would share a boot, and two instances of the same
        model share a server even though they never share a gate.
        """

        return self._entry.app_name

    async def ensure_warm(self) -> None:
        """Wait until this model's Modal server answers — at most once per
        warm period.

        Public because the **Pre-warm** awaits it directly (duck-typed, so a
        Voyage or Gemini model simply has no such method). Nothing is marked
        warm until the whole body succeeded, so a failed warm is retried in
        full rather than embedding against a dead server.
        """

        await self._gate.ensure_warm()

    async def _warm(self) -> None:
        """Resolve the server, poll it warm, discover its id, build a client.

        The gate's single-flight body, ALL-OR-NOTHING: ``_served_model`` and
        ``_client`` are assigned only after every step succeeded, so a failure
        leaves NOTHING half-built and the next call re-runs the whole sequence.
        It costs one ``get_url``, one ``/v1/models`` GET and one client object
        per cold period — not per call.
        """

        url = await resolve_server_url(self._entry)
        # The poll sends the same `Authorization: Bearer <id>.<secret>` the
        # embeddings call does: Modal's edge checks it before a GPU wakes.
        await poll_health(
            f"{url}/health",
            {"Authorization": f"Bearer {self._proxy_token}"},
            deadline_s=self._warmup_deadline_s,
        )
        served = await served_model_id(
            url, self._proxy_token, default=self._entry.repo_id
        )
        # The Proxy token IS the OpenAI api_key: the client sends it as
        # `Authorization: Bearer <id>.<secret>`.
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

        An empty batch returns ``[]`` without warming anything — the same
        guard :class:`~tree.models.voyage_embedding.VoyageTextEmbeddingModel`
        carries, and here it also keeps an empty call from waking a
        scaled-to-zero GPU container (which the server would answer with an
        empty ``data`` list the OpenAI SDK refuses to parse anyway).

        The call goes through the **Warm gate**: it waits out a cold start
        before the first one, and a container that idled out mid-run costs ONE
        re-warm and ONE retry instead of a failed document.

        Wrapped in an Opik ``llm``-type span; on success the ``usage`` token
        counts are recorded with ``total_cost=0`` (self-hosted). Recording is
        fully fail-open.

        Raises:
            ModelError: the warm failed on something waiting cannot fix (a
                wrong **Proxy token**, an undeployed model).
            ExtractionError: the call failed (retryable), the server stayed
                cold through one re-warm, or it answered with vectors of the
                wrong width.
        """

        if not texts:
            return []

        prompt = prompt_for(self._entry, input_type)
        inputs = [prompt + text for text in texts]
        try:
            # The lambda reads `_client` and `_served_model` at CALL time: a
            # re-warm replaces both, and the retry must use the new ones.
            response = await self._gate.call(
                lambda: self._client.embeddings.create(  # type: ignore[union-attr]
                    input=inputs,
                    model=self._served_model,
                    # Explicit: the SDK otherwise asks for base64, while the
                    # shared smoke test (`modal_server._embed`) asks for float.
                    # One wire contract on every Serving path, and no engine or
                    # recipe has to implement base64 for us.
                    encoding_format="float",
                )
            )
        except ModelError:
            # Everything the gate raises — a bad token (ModelError), a spent
            # deadline or a server still cold after one re-warm
            # (ExtractionError, a subclass) — already says what to do. Wrapping
            # it in "Embedding call failed" would re-type a configuration error
            # as a retryable one.
            raise
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
