import json
import logging
from typing import Any

from google import genai
from google.genai.types import GenerateContentConfig

from twin.config.app_config import app_config
from twin.models.base import BaseLLM, BaseEmbeddingModel
from twin.models.exceptions import ExtractionError

logger = logging.getLogger(__name__)


class GeminiLLM(BaseLLM):
    """Gemini LLM via the google-genai SDK (async)."""

    def __init__(self, api_key: str, model: str | None = None) -> None:
        model = model or app_config.models.llm.model
        self._client = genai.Client(api_key=api_key)
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
        model = model or app_config.models.embedding.model
        dimensions = dimensions or app_config.models.embedding.dimensions
        self._client = genai.Client(api_key=api_key)
        self._model = model
        self._dimensions = dimensions

    async def embed(self, texts: list[str]) -> list[list[float]]:
        try:
            response = await self._client.aio.models.embed_content(
                model=self._model,
                contents=texts,
                config={
                    "output_dimensionality": self._dimensions,
                },
            )
        except Exception as exc:
            raise ExtractionError(f"Gemini embedding call failed: {exc}") from exc

        return [e.values or [] for e in response.embeddings]
