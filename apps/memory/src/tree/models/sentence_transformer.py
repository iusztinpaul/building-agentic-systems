import logging
from typing import Any

from sentence_transformers import SentenceTransformer

from tree.models.base import BaseEmbeddingModel, EmbeddingRole
from tree.models.exceptions import ExtractionError
from tree.observability import track

logger = logging.getLogger(__name__)


class SentenceTransformerEmbeddingModel(BaseEmbeddingModel):
    """In-process embedding model using sentence-transformers."""

    def __init__(
        self,
        model: str = "voyageai/voyage-4-nano",
        dimensions: int = 512,
        device: str = "cpu",
    ) -> None:
        self._dimensions = dimensions
        self._model = SentenceTransformer(
            model,
            trust_remote_code=True,
            device=device,
            truncate_dim=dimensions,
        )
        logger.info("Loaded sentence-transformer model: %s on %s", model, device)

    @property
    def dimensions(self) -> int:
        """Truncated output size (Matryoshka via ``truncate_dim``).

        ``self._dimensions`` reflects the runtime truncation that
        ``embed`` actually returns. Falls back to the model's native
        dimensionality from ``get_sentence_embedding_dimension()`` only
        if no truncation was configured.
        """

        if self._dimensions is not None:
            return self._dimensions
        return int(self._model.get_sentence_embedding_dimension())

    @track(name="sentence-transformer-embed")
    async def embed(
        self, texts: list[str], input_type: EmbeddingRole | None = None
    ) -> list[list[float]]:
        """Encode texts, optionally under an **Embedding role**.

        The role becomes ``encode(prompt_name=...)`` ONLY when the loaded
        model actually defines a non-empty prompt of that name; otherwise the
        call is byte-for-byte today's and the role is silently ignored
        (ADR-009 §5).

        Why non-empty and not just present: sentence-transformers seeds EVERY
        model with ``prompts = {"query": "", "document": ""}``
        (``SentenceTransformer.__init__``, 5.3.0), so membership alone is
        always true; an empty prompt is not a prompt the model defines. Note
        ``encode`` RAISES ``ValueError`` for a ``prompt_name`` missing from
        that dict — which is exactly the "never raises" clause this guard
        keeps us on the right side of.
        """

        if not texts:
            return []

        encode_kwargs: dict[str, Any] = {"normalize_embeddings": True}
        prompts = getattr(self._model, "prompts", None) or {}
        if input_type is not None and prompts.get(input_type):
            encode_kwargs["prompt_name"] = input_type

        try:
            embeddings = self._model.encode(texts, **encode_kwargs)
        except Exception as exc:
            raise ExtractionError(
                f"Sentence-transformer embedding failed: {exc}"
            ) from exc

        return [emb[: self._dimensions].tolist() for emb in embeddings]
