import abc
from typing import Any


class BaseLLM(abc.ABC):
    """Async LLM that returns parsed JSON."""

    @abc.abstractmethod
    async def generate_json(
        self, prompt: str, *, system: str | None = None
    ) -> dict[str, Any]:
        """Send a prompt and return the parsed JSON response."""


class BaseEmbeddingModel(abc.ABC):
    """Async embedding model."""

    @abc.abstractmethod
    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Return one embedding vector per input text."""
