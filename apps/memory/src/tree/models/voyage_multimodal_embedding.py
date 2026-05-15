"""Embedding model that calls the Voyage AI multimodal embeddings API.

Uses ``aiohttp`` to call ``POST https://api.voyageai.com/v1/multimodalembeddings``
directly, wrapping text inputs as multimodal content.  Image support can be
added later by extending the ``embed`` method or adding an ``embed_multimodal``
method.

API docs: https://docs.voyageai.com/docs/multimodal-embeddings
"""

import logging
from typing import Literal

import aiohttp

from tree.models.base import BaseEmbeddingModel
from tree.models.exceptions import ExtractionError, ModelError

logger = logging.getLogger(__name__)

_API_URL = "https://api.voyageai.com/v1/multimodalembeddings"

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
    """

    def __init__(
        self,
        api_key: str,
        model: str = "voyage-multimodal-3",
        input_type: Literal["query", "document"] | None = None,
        output_dimension: int | None = None,
        truncation: bool = True,
        timeout: float = 120.0,
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

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed text strings via the Voyage multimodal API."""

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

        try:
            timeout = aiohttp.ClientTimeout(total=self._timeout)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(
                    _API_URL, json=payload, headers=headers
                ) as resp:
                    body = await resp.json()

                    if resp.status != 200:
                        detail = body.get("detail", body)
                        raise ExtractionError(
                            f"Voyage multimodal API error {resp.status}: {detail}"
                        )

                    data = body.get("data")
                    if data is None:
                        raise ExtractionError(
                            f"Voyage multimodal API returned unexpected response: {body}"
                        )

                    return [item["embedding"] for item in data]
        except ExtractionError:
            raise
        except Exception as exc:
            raise ExtractionError(
                f"Voyage multimodal embedding call failed: {exc}"
            ) from exc
