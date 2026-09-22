import abc
from typing import Any, Literal

# The **Embedding role**: which side of retrieval a text is on (ADR-009 §5).
# ``"query"`` is a user question, ``"document"`` is a text that gets persisted
# and later retrieved; ``None`` means symmetric — no role at all.
EmbeddingRole = Literal["query", "document"]


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
    async def embed(
        self, texts: list[str], input_type: EmbeddingRole | None = None
    ) -> list[list[float]]:
        """Return one embedding vector per input text.

        ``input_type`` is the **Embedding role** (ADR-009 §5) — a retrieval
        HINT, never a correctness input:

        - ``None`` (the default) is SYMMETRIC: no provider-side prompt, no
          role field on the wire. Both sides of the comparison are the same
          kind of text (entity name vs entity name).
        - A provider that CANNOT honour a role IGNORES it — it never raises.
          Callers therefore pass the role blind, without asking which model is
          configured.

        The role is per CALL, never per instance: one model object serves both
        the indexing path (``"document"``) and the query path (``"query"``).
        """
