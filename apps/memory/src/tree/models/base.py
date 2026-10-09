import abc
from typing import Any, Literal

from tree.models.exceptions import ExtractionError

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


def vectors_in_input_order(
    indexed: list[tuple[int, list[float]]], n_inputs: int, *, provider: str
) -> list[list[float]]:
    """Return a response's vectors in input order — exactly one per input.

    Providers tag each vector with the ``index`` of its input; downstream code
    pairs vectors with inputs by position, so a short response would drop the
    tail rows' vectors and a reordered one would write vectors onto the wrong
    rows. Both are provider bugs, not transient failures: raise
    ``ExtractionError`` WITHOUT ``status_code``, so ``_embed_chunk_resilient``
    re-raises it instead of bisecting it into skipped ``[]`` rows.
    """

    ordered = sorted(indexed, key=lambda pair: pair[0])
    indices = [index for index, _ in ordered]
    if indices != list(range(n_inputs)):
        shown = ", ".join(map(str, indices[:10])) + (", …" if len(indices) > 10 else "")
        raise ExtractionError(
            f"{provider} returned indices [{shown}] for {n_inputs} inputs "
            f"(expected 0..{n_inputs - 1}); this batch's vectors were discarded."
        )
    return [vector for _, vector in ordered]
