from typing import Any

from twin.config.app_config import app_config
from twin.models.base import BaseLLM, BaseEmbeddingModel


class FakeLLM(BaseLLM):
    """Returns canned JSON responses. For testing only."""

    def __init__(self, responses: list[dict[str, Any]] | None = None) -> None:
        self._responses = list(responses) if responses else []
        self._call_count = 0

    async def generate_json(
        self, prompt: str, *, system: str | None = None
    ) -> dict[str, Any]:
        if self._call_count < len(self._responses):
            result = self._responses[self._call_count]
        else:
            result = {"nodes": [], "edges": []}
        self._call_count += 1
        return result

    @property
    def call_count(self) -> int:
        return self._call_count


class FakeEmbeddingModel(BaseEmbeddingModel):
    """Returns zero vectors. For testing only."""

    def __init__(self, dimensions: int | None = None) -> None:
        dimensions = dimensions or app_config.models.embedding.dimensions
        self._dimensions = dimensions

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.0] * self._dimensions for _ in texts]
