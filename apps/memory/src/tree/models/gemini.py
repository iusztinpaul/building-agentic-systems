import json
import logging
from typing import Any

from google import genai
from google.genai.types import GenerateContentConfig

from tree.config.app_config import app_config
from tree.models.base import BaseLLM, BaseEmbeddingModel, EmbeddingRole
from tree.models.exceptions import ExtractionError
from tree.observability import track_genai_client

logger = logging.getLogger(__name__)

# The **Embedding role** → Gemini's ``task_type`` (ADR-009 §5). Gemini has a
# richer taxonomy (CLASSIFICATION, CLUSTERING, QUESTION_ANSWERING, …); the two
# retrieval ones are the only ones the role expresses.
# Verified on the pinned ``google-genai`` 1.65.0, where
# ``EmbedContentConfig.task_type`` is a free-form ``Optional[str]``, and
# against https://ai.google.dev/gemini-api/docs/embeddings (read 2026-09-19).
_ROLE_TO_TASK_TYPE: dict[EmbeddingRole, str] = {
    "query": "RETRIEVAL_QUERY",
    "document": "RETRIEVAL_DOCUMENT",
}


class GeminiLLM(BaseLLM):
    """Gemini LLM via the google-genai SDK (async)."""

    def __init__(self, api_key: str, model: str | None = None) -> None:
        model = model or app_config.models.llm.model
        # Wrap with Opik's genai integration for automatic spans + native
        # Gemini token usage / cost. No-op passthrough when Opik is unconfigured
        # (see :func:`tree.observability.track_genai_client`).
        self._client = track_genai_client(genai.Client(api_key=api_key))
        self._model = model

    async def generate_json(
        self, prompt: str, *, system: str | None = None
    ) -> dict[str, Any]:
        config = GenerateContentConfig(
            response_mime_type="application/json",
        )
        if system:
            config.system_instruction = system

        try:
            response = await self._client.aio.models.generate_content(
                model=self._model,
                contents=prompt,
                config=config,
            )
        except Exception as exc:
            raise ExtractionError(f"Gemini API call failed: {exc}") from exc

        text = response.text
        if not text:
            raise ExtractionError("Gemini returned an empty response")

        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise ExtractionError(
                f"Gemini returned invalid JSON: {text[:200]}"
            ) from exc


class GeminiEmbeddingModel(BaseEmbeddingModel):
    """Gemini embedding model via the google-genai SDK (async)."""

    def __init__(
        self,
        api_key: str,
        model: str | None = None,
        dimensions: int | None = None,
    ) -> None:
        model = model or app_config.models.search_embedding.model
        dimensions = dimensions or app_config.models.search_embedding.dimensions
        # Wrap with Opik's genai integration (no-op passthrough without a key).
        self._client = track_genai_client(genai.Client(api_key=api_key))
        self._model = model
        self._dimensions = dimensions

    @property
    def dimensions(self) -> int:
        """Configured output dimensionality (passed to the API as
        ``output_dimensionality``). Gemini text-embedding models support
        Matryoshka truncation, so the wire vector exactly matches the
        value supplied at construction time."""

        return self._dimensions

    async def embed(
        self, texts: list[str], input_type: EmbeddingRole | None = None
    ) -> list[list[float]]:
        """Embed texts, optionally under an **Embedding role**.

        ``"query"`` → ``task_type="RETRIEVAL_QUERY"``, ``"document"`` →
        ``"RETRIEVAL_DOCUMENT"``; ``None`` leaves the key out of the config.

        Sent unconditionally, for every model id. The docs say the field is
        unusable on ``gemini-embedding-2`` ("include the task as an
        instruction in your prompt" instead), but the API ACCEPTS and IGNORES
        it there rather than erroring — verified live 2026-09-19: the
        ``RETRIEVAL_QUERY`` and role-less vectors are identical (cosine
        1.000000). So the server already implements ADR-009 §5's "a provider
        that cannot honour a role ignores it, never raises", and a client-side
        list of supporting model ids would only add a thing to forget to
        update — it would silently drop the role on the next model id Google
        ships (``gemini-embedding-2-preview`` is already live).
        """

        config: dict[str, Any] = {"output_dimensionality": self._dimensions}
        if input_type is not None:
            config["task_type"] = _ROLE_TO_TASK_TYPE[input_type]

        try:
            response = await self._client.aio.models.embed_content(
                model=self._model,
                contents=texts,
                config=config,
            )
        except Exception as exc:
            raise ExtractionError(f"Gemini embedding call failed: {exc}") from exc

        return [e.values or [] for e in response.embeddings]
