"""Embedding model that calls the Voyage AI **text** embeddings API.

Uses ``aiohttp`` to call ``POST https://api.voyageai.com/v1/embeddings`` for
text-only embedding models such as ``voyage-3``, ``voyage-3.5``,
``voyage-3-lite``, ``voyage-code-3``, etc.

Distinct from
:class:`tree.models.voyage_multimodal_embedding.VoyageMultimodalEmbeddingModel`,
which targets ``/v1/multimodalembeddings`` and only accepts the
``voyage-multimodal-*`` model family. Routing a text model id such as
``voyage-3`` to the multimodal endpoint returns ``HTTP 400: Model voyage-3 is
not supported.`` — the headline regression this client guards against. The two
clients coexist; :func:`tree.models.get_model._build_embedding_model` routes by
model id (``voyage-multimodal-*`` → multimodal, everything else → this client).

**Rate-limit behavior.** Voyage's free tier is 3 RPM / 10K TPM (see
https://docs.voyageai.com/docs/pricing); the per-task Prefect retry count
(``retries=2`` on ``embed_entities_task``) is not enough to ride out a
60-second rate window on its own. :meth:`embed` therefore wraps the POST in an
internal exponential-backoff loop that retries on HTTP 429 and fails fast on
every other non-200 status, so Prefect's retry budget isn't burned on transient
rate-limit errors. The schedule is configurable via
``rate_limit_backoff_seconds``; when it runs out, :meth:`embed` surfaces an
``ExtractionError`` whose message contains the literal anchor
``"rate-limit retries exhausted"`` so operators can grep for it.

**``status_code`` discriminator.** Both the 429 path and every non-200 raise
carry ``ExtractionError.status_code`` (the underlying HTTP status). This is the
contract :func:`tree.memory.embedding_text._embed_chunk_resilient` keys off to
distinguish a content-rejection 400 (skip the poison input) from a transient
429/5xx (re-raise, never silently drop a vector).

The batching / sanitization / bisect skip-and-continue layer lives at
``tree.memory.embedding_text`` and wraps any ``BaseEmbeddingModel``; it is NOT
duplicated here. This client only owns the per-request HTTP call, the 429
backoff loop, and the ``status_code`` contract.

API docs: https://docs.voyageai.com/docs/embeddings
API ref: https://docs.voyageai.com/reference/embeddings-api
"""

import asyncio
import logging
from typing import Literal

import aiohttp
from prefect.concurrency.asyncio import rate_limit

from tree.models.base import BaseEmbeddingModel
from tree.models.exceptions import ExtractionError, ModelError

logger = logging.getLogger(__name__)

_API_URL = "https://api.voyageai.com/v1/embeddings"

# The Prefect global concurrency limit (ADR-002 §1) that throttles every real
# Voyage embed POST across separate flow runs. Acquired immediately before each
# real network POST attempt (inside the 429-backoff loop, so a 429-retry
# re-acquires a fresh slot). ``strict=False`` makes a missing limit a no-op, so
# unit tests and fresh dev boxes without a synced ``voyage-embeddings`` GCL
# behave exactly as before. A ``_CachedSingleEmbedding`` cache hit never reaches
# this client, so it is never throttled.
_VOYAGE_EMBED_LIMIT = "voyage-embeddings"

# Default exponential-backoff schedule for HTTP 429 rate limits. Kept tight
# (8 attempts, capped at 60s each) so a real outage still surfaces inside a few
# minutes while comfortably riding out a free-tier 3 RPM window. Mirrors the
# multimodal client's schedule. See the module docstring for the rationale.
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

# Known native output dimensions per Voyage **text** model id. Used when the
# caller does not request Matryoshka truncation via ``output_dimension``.
# Source: https://docs.voyageai.com/docs/embeddings — keep in lockstep with
# the API docs.
_MODEL_NATIVE_DIMENSIONS: dict[str, int] = {
    "voyage-3": 1024,
    "voyage-3.5": 1024,
    "voyage-3.5-lite": 1024,
    "voyage-3-large": 1024,
    "voyage-3-lite": 512,
    "voyage-code-3": 1024,
    "voyage-finance-2": 1024,
    "voyage-law-2": 1024,
}


class VoyageTextEmbeddingModel(BaseEmbeddingModel):
    """Embedding model backed by the Voyage AI **text** embeddings API.

    Each text string is passed flat in the ``input`` list::

        {"input": ["..."], "model": "voyage-3.5", ...}

    This is DIFFERENT from the multimodal client's nested
    ``{"inputs": [{"content": [{"type": "text", "text": "..."}]}]}`` shape;
    sending the multimodal shape to ``/v1/embeddings`` (or a text model to the
    multimodal endpoint) 400s.

    Supports optional ``input_type`` (``"query"`` / ``"document"``) for
    retrieval-optimised embeddings, and ``output_dimension`` for Matryoshka
    truncation when the model supports it (e.g. ``voyage-3.5``).

    Calls to :meth:`embed` are wrapped in an exponential-backoff loop that
    retries transient HTTP 429 (rate-limit) responses and fails fast on every
    other non-200 status. The retry schedule is overridable via the
    ``rate_limit_backoff_seconds`` constructor parameter; when the schedule runs
    out, :meth:`embed` raises ``ExtractionError`` with the anchor message
    ``"rate-limit retries exhausted"``. Every error raise carries the structured
    ``status_code`` so ``_embed_chunk_resilient`` can branch 400-vs-429.
    """

    def __init__(
        self,
        api_key: str,
        model: str = "voyage-3.5",
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
                f"VoyageTextEmbeddingModel has no explicit `output_dimension` "
                f"and the native dimension for model '{self._model}' is "
                f"unknown. Add it to _MODEL_NATIVE_DIMENSIONS in "
                f"voyage_embedding.py or construct the model with an explicit "
                f"`output_dimension=`."
            )
        return native

    # ------------------------------------------------------------------
    # Embedding
    # ------------------------------------------------------------------

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed text strings via the Voyage **text** embeddings API.

        Retries transparently on HTTP 429 per
        ``self._rate_limit_backoff_seconds``; fails fast on every other
        non-200 status. Raises ``ExtractionError`` (carrying ``status_code``)
        when the backoff schedule is exhausted or any non-rate-limit failure
        occurs.
        """

        if not texts:
            return []

        payload: dict[str, object] = {
            "model": self._model,
            "input": texts,
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
        # statuses fail fast — they are not transient. Free-tier Voyage is
        # 3 RPM, so even normal extraction runs into 429s; without this retry
        # the Prefect task burns its retry budget and the entire flow fails.
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
                                    "Voyage text-embeddings API error 429: "
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
                                f"Voyage text-embeddings API error "
                                f"{resp.status}: {detail}",
                                status_code=resp.status,
                            )

                        data = body.get("data")
                        if data is None:
                            raise ExtractionError(
                                f"Voyage text-embeddings API returned "
                                f"unexpected response: {body}"
                            )

                        return [item["embedding"] for item in data]
            except ExtractionError:
                raise
            except Exception as exc:
                raise ExtractionError(
                    f"Voyage text-embeddings call failed: {exc}"
                ) from exc
