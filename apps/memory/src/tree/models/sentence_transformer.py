import logging

from sentence_transformers import SentenceTransformer

from tree.models.base import BaseEmbeddingModel
from tree.models.exceptions import ExtractionError

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

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []

        try:
            embeddings = self._model.encode(
                texts,
                normalize_embeddings=True,
            )
        except Exception as exc:
            raise ExtractionError(
                f"Sentence-transformer embedding failed: {exc}"
            ) from exc

        return [emb[: self._dimensions].tolist() for emb in embeddings]
