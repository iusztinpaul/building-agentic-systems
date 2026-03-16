import logging

from openai import AsyncOpenAI

from twin.models.base import BaseEmbeddingModel
from twin.models.exceptions import ExtractionError

logger = logging.getLogger(__name__)


class OpenAICompatibleEmbeddingModel(BaseEmbeddingModel):
    """Embedding model via any OpenAI-compatible API (vLLM, Modal, Voyage AI, etc.)."""

    def __init__(
        self,
        base_url: str,
        api_key: str = "EMPTY",
        model: str = "voyageai/voyage-4-nano",
        dimensions: int = 512,
    ) -> None:
        self._client = AsyncOpenAI(base_url=base_url, api_key=api_key)
        self._model = model
        self._dimensions = dimensions

    async def embed(self, texts: list[str]) -> list[list[float]]:
        try:
            response = await self._client.embeddings.create(
                input=texts,
                model=self._model,
                dimensions=self._dimensions,
            )
        except Exception as exc:
            raise ExtractionError(f"Embedding call failed: {exc}") from exc

        return [item.embedding for item in response.data]
