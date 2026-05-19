"""Embedding model that calls the Voyage AI **text** embeddings API.

Uses ``aiohttp`` to call ``POST https://api.voyageai.com/v1/embeddings``
for text-only embedding models such as ``voyage-3``, ``voyage-3-large``,
``voyage-3-lite``, ``voyage-3.5``, ``voyage-3.5-lite``, ``voyage-code-3``,
``voyage-finance-2``, etc.

Distinct from :class:`tree.models.voyage_multimodal_embedding.VoyageMultimodalEmbeddingModel`,
which targets ``/v1/multimodalembeddings`` and only accepts the
``voyage-multimodal-*`` model family. Routing ``voyage-3`` (the project's
post-#034 default) to the multimodal endpoint returns ``HTTP 400: Model
voyage-3 is not supported.`` — see ``tracker/037-fresh-deploy-e2e-acceptance``
for the regression that motivated this split.

API docs: https://docs.voyageai.com/docs/embeddings
"""

import asyncio
import logging
from typing import Literal

import aiohttp

from tree.models.base import BaseEmbeddingModel
from tree.models.exceptions import ExtractionError, ModelError

logger = logging.getLogger(__name__)

_API_URL = "https://api.voyageai.com/v1/embeddings"

# Default exponential-backoff schedule for HTTP 429 rate limits.
# Voyage's free tier is 3 RPM / 10K TPM (see
# https://docs.voyageai.com/docs/pricing); the per-task Prefect retry
# count (``retries=2`` on ``embed_entities_task``) is not enough to ride
# out a 60-second rate window on its own. These sleeps run **inside**
# the embed call so the Prefect task itself doesn't burn its retry
# budget on transient 429s — kept tight (8 attempts, capped at 60s
# each) so a real outage still surfaces inside a few minutes.
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

# Known native output dimensions per Voyage **text** model id. Used when
# the caller does not request Matryoshka truncation via ``output_dimension``.
# Keep in lockstep with https://docs.voyageai.com/docs/embeddings.
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


class VoyageEmbeddingModel(BaseEmbeddingModel):
    """Embedding model backed by the Voyage AI **text** embeddings API.

    Supports optional ``input_type`` (``"query"`` / ``"document"``) for
    retrieval-optimised embeddings and ``output_dimension`` for
    Matryoshka truncation when the model supports it (e.g. ``voyage-3.5``).
    """

    def __init__(
        self,
        api_key: str,
        model: str = "voyage-3",
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
                f"VoyageEmbeddingModel has no explicit `output_dimension` "
                f"and the native dimension for model '{self._model}' is "
                f"unknown. Add it to _MODEL_NATIVE_DIMENSIONS in "
                f"voyage_embedding.py or construct the model with an "
                f"explicit `output_dimension=`."
            )
        return native

    # ------------------------------------------------------------------
    # Embedding
    # ------------------------------------------------------------------

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed text strings via the Voyage **text** embeddings API."""

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
        # statuses fail fast — they are not transient. Free-tier Voyage
        # is 3 RPM, so even normal extraction runs into 429s; without
        # this retry the Prefect task burns its retry budget and the
        # entire flow fails. See ``tracker/037-fresh-deploy-e2e-acceptance``.
        backoff_iter = iter(self._rate_limit_backoff_seconds)
        try:
            while True:
                try:
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
                                        f"rate-limit retries exhausted ({detail})"
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
                                    f"{resp.status}: {detail}"
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
        except ExtractionError:
            raise
