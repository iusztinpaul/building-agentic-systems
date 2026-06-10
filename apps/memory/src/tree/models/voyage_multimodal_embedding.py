"""Embedding model that calls the Voyage AI multimodal embeddings API.

Uses ``aiohttp`` to call ``POST https://ai.mongodb.com/v1/multimodalembeddings``
(the Atlas-hosted Voyage API — Atlas-issued ``al-`` model keys ONLY
authenticate against ``ai.mongodb.com``, not the legacy ``api.voyageai.com``)
directly, wrapping text inputs as multimodal content. Image support can be
added later by extending the ``embed`` method or adding an ``embed_multimodal``
method.

**Rate-limit behavior.** Voyage's free tier is 3 RPM / 10K TPM (see
https://docs.voyageai.com/docs/pricing); the per-task Prefect retry count
(``retries=2`` on ``embed_entities_task``) is not enough to ride out a
60-second rate window on its own. ``embed`` therefore wraps the POST in
an internal exponential-backoff loop that retries on HTTP 429 and fails
fast on every other non-200 status, so Prefect's retry budget isn't
burned on transient rate-limit errors. The schedule is configurable via
``rate_limit_backoff_seconds``; when it runs out, ``embed`` surfaces an
``ExtractionError`` whose message contains the literal anchor
``"rate-limit retries exhausted"`` so operators can grep for it. This
logic was originally implemented on a separate text-only client and
folded into the multimodal client in #038 when the project consolidated
on a single voyage client (``voyage-multimodal-3``).

API docs: https://docs.voyageai.com/docs/multimodal-embeddings
"""

import asyncio
import logging
from typing import Literal

import aiohttp
from prefect.concurrency.asyncio import rate_limit

from tree.config.app_config import app_config
from tree.models.base import BaseEmbeddingModel
from tree.models.exceptions import ExtractionError, ModelError
from tree.observability import record_embedding_usage, track

logger = logging.getLogger(__name__)

_API_URL = "https://ai.mongodb.com/v1/multimodalembeddings"


def _record_voyage_cost(model: str, body: dict) -> None:
    """Record Voyage multimodal usage + manual cost on the current Opik span.

    Fully exception-safe. The multimodal response carries ``usage.total_tokens``
    (text token count); cost is ``total_tokens × price_per_token`` from the YAML
    price map. Models absent from the map record usage with ``total_cost=0``.
    """

    try:
        usage = body.get("usage") or {}
        total_tokens = usage.get("total_tokens")
        cost = (
            app_config.observability.cost_for(model, total_tokens)
            if total_tokens is not None
            else 0.0
        )
        record_embedding_usage(
            provider="voyage",
            model=model,
            total_tokens=total_tokens,
            total_cost=cost,
        )
    except Exception as exc:  # noqa: BLE001 — telemetry must never break embed
        logger.debug("Opik Voyage cost recording no-op: %s", exc)


# The Prefect global concurrency limit (ADR-002 §1) that throttles every real
# Voyage embed POST across separate flow runs. Acquired immediately before each
# real network POST attempt (inside the 429-backoff loop, so a 429-retry
# re-acquires a fresh slot). ``strict=False`` makes a missing limit a no-op, so
# unit tests and fresh dev boxes without a synced ``voyage-embeddings`` GCL
# behave exactly as before. A ``_CachedSingleEmbedding`` cache hit never reaches
# this client, so it is never throttled.
_VOYAGE_EMBED_LIMIT = "voyage-embeddings"

# Default exponential-backoff schedule for HTTP 429 rate limits. Kept
# tight (8 attempts, capped at 60s each) so a real outage still surfaces
# inside a few minutes while comfortably riding out a free-tier 3 RPM
# window. See the module docstring for the rationale.
_DEFAULT_RATE_LIMIT_BACKOFF_SECONDS: tuple[float, ...] = (
    2.0,
    4.0,
    8.0,
    16.0,
    30.0,
    60.0,
    60.0,
    60.0,
)

# Known native output dimensions per Voyage multimodal model id. Used when
# the caller does not request Matryoshka truncation via ``output_dimension``.
# Source: https://docs.voyageai.com/docs/multimodal-embeddings — keep in
# lockstep with the API docs.
_MODEL_NATIVE_DIMENSIONS: dict[str, int] = {
    "voyage-multimodal-3": 1024,
    "voyage-multimodal-3.5": 1024,
}


class VoyageMultimodalEmbeddingModel(BaseEmbeddingModel):
    """Embedding model backed by the Voyage AI multimodal embeddings API.

    Each text string is wrapped as a single-element multimodal input::

        {"content": [{"type": "text", "text": "..."}]}

    Supports optional ``input_type`` (``"query"`` / ``"document"``) for
    retrieval-optimised embeddings, and ``output_dimension`` for Matryoshka
    truncation (``voyage-multimodal-3.5`` supports 256, 512, 1024, 2048).

    Calls to :meth:`embed` are wrapped in an exponential-backoff loop that
    retries transient HTTP 429 (rate-limit) responses and fails fast on
    every other non-200 status. The retry schedule is overridable via the
    ``rate_limit_backoff_seconds`` constructor parameter; when the
    schedule runs out, :meth:`embed` raises ``ExtractionError`` with the
    anchor message ``"rate-limit retries exhausted"``.
    """

    def __init__(
        self,
        api_key: str,
        model: str = "voyage-multimodal-3",
        input_type: Literal["query", "document"] | None = None,
        output_dimension: int | None = None,
        truncation: bool = True,
        timeout: float = 120.0,
        rate_limit_backoff_seconds: tuple[float, ...] = (
            _DEFAULT_RATE_LIMIT_BACKOFF_SECONDS
        ),
    ) -> None:
        if not api_key:
            raise ModelError(
                "Voyage API key is required. "
                "Set the VOYAGE_API_KEY environment variable."
            )
        self._api_key = api_key
        self._model = model
        self._input_type = input_type
        self._output_dimension = output_dimension
        self._truncation = truncation
        self._timeout = timeout
        self._rate_limit_backoff_seconds = rate_limit_backoff_seconds

    @property
    def dimensions(self) -> int:
        """Output vector size.

        Uses the caller-supplied Matryoshka ``output_dimension`` when set,
        otherwise the model's native dimensionality from
        ``_MODEL_NATIVE_DIMENSIONS``.
        """

        if self._output_dimension is not None:
            return self._output_dimension
        native = _MODEL_NATIVE_DIMENSIONS.get(self._model)
        if native is None:
            raise ModelError(
                f"VoyageMultimodalEmbeddingModel has no explicit "
                f"`output_dimension` and the native dimension for model "
                f"'{self._model}' is unknown. Add it to "
                f"_MODEL_NATIVE_DIMENSIONS in voyage_multimodal_embedding.py "
                f"or construct the model with an explicit `output_dimension=`."
            )
        return native

    # ------------------------------------------------------------------
    # Embedding
    # ------------------------------------------------------------------

    @track(type="llm", name="voyage-multimodal-embed")
    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed text strings via the Voyage multimodal API.

        Retries transparently on HTTP 429 per
        ``self._rate_limit_backoff_seconds``; fails fast on every other
        non-200 status. Raises ``ExtractionError`` when the backoff
        schedule is exhausted or any non-rate-limit failure occurs.

        Wrapped in an Opik ``llm``-type span; on success records
        ``usage.total_tokens`` and a manual ``total_cost`` (fail-open).
        """

        if not texts:
            return []

        inputs = [{"content": [{"type": "text", "text": t}]} for t in texts]

        payload: dict[str, object] = {
            "model": self._model,
            "inputs": inputs,
            "truncation": self._truncation,
        }
        if self._input_type is not None:
            payload["input_type"] = self._input_type
        if self._output_dimension is not None:
            payload["output_dimension"] = self._output_dimension

        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

        # Exponential-backoff loop for HTTP 429 (rate limit). All other
        # statuses fail fast — they are not transient. Free-tier Voyage
        # is 3 RPM, so even normal extraction runs into 429s; without
        # this retry the Prefect task burns its retry budget and the
        # entire flow fails. See ``tracker/038-consolidate-voyage-clients``
        # for the consolidation that moved this loop here from the
        # deprecated text-only client.
        backoff_iter = iter(self._rate_limit_backoff_seconds)
        while True:
            try:
                # ADR-002 §1: acquire one shared ``voyage-embeddings`` slot per
                # real POST attempt. Placed inside the 429-backoff loop so a
                # 429-retry re-acquires a fresh slot; ``strict=False`` no-ops
                # when the limit is absent. The early ``if not texts: return []``
                # above short-circuits before this, so an empty call never
                # occupies a slot.
                await rate_limit(_VOYAGE_EMBED_LIMIT, occupy=1, strict=False)
                timeout = aiohttp.ClientTimeout(total=self._timeout)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.post(
                        _API_URL, json=payload, headers=headers
                    ) as resp:
                        body = await resp.json()

                        if resp.status == 429:
                            detail = body.get("detail", body)
                            try:
                                sleep_s = next(backoff_iter)
                            except StopIteration:
                                raise ExtractionError(
                                    "Voyage multimodal API error 429: "
                                    f"rate-limit retries exhausted ({detail})",
                                    status_code=resp.status,
                                ) from None
                            logger.warning(
                                "Voyage 429 (rate limit); sleeping %.1fs "
                                "before retry. detail=%s",
                                sleep_s,
                                detail,
                            )
                            await asyncio.sleep(sleep_s)
                            continue

                        if resp.status != 200:
                            detail = body.get("detail", body)
                            raise ExtractionError(
                                f"Voyage multimodal API error {resp.status}: {detail}",
                                status_code=resp.status,
                            )

                        data = body.get("data")
                        if data is None:
                            raise ExtractionError(
                                f"Voyage multimodal API returned unexpected response: {body}"
                            )

                        _record_voyage_cost(self._model, body)
                        return [item["embedding"] for item in data]
            except ExtractionError:
                raise
            except Exception as exc:
                raise ExtractionError(
                    f"Voyage multimodal embedding call failed: {exc}"
                ) from exc
