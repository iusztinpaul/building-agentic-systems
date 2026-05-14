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

    @property
    @abc.abstractmethod
    def dimensions(self) -> int:
        """Size of the vector each ``embed(...)[i]`` call yields.

        Surfaces the contract every downstream component depends on
        (vector-index ``numDimensions``, schema validation, dedup
        thresholds). Implementations return a positive integer matching
        the model's wire output.
        """

    @abc.abstractmethod
    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Return one embedding vector per input text."""
