"""Embedding model that calls a Modal-hosted vLLM endpoint.

Lazily resolves the Modal web URL and performs a health check on the
first ``embed()`` call to handle cold starts.  Uses the OpenAI
``AsyncOpenAI`` client directly against the vLLM server.
"""

import logging

import modal
from openai import AsyncOpenAI

from twin.models.base import BaseEmbeddingModel
from twin.models.exceptions import ExtractionError, ModelError

logger = logging.getLogger(__name__)

_DEFAULT_APP_NAME = "vllm-embedding-models"
_DEFAULT_FUNCTION_NAME = "voyageai-voyage-4-nano"


class ModalEmbeddingModel(BaseEmbeddingModel):
    """Embedding model backed by a Modal-deployed vLLM server.

    On first ``embed()`` call:
    1. Looks up the deployed Modal function and resolves its public web URL
       (async, avoids blocking the event loop).
    2. Creates an ``AsyncOpenAI`` client pointing at that URL.
    3. Sends a ``GET /health`` request to warm up the container (handles
       Modal's 15-minute scaledown window) and waits for it to be ready.
    """

    def __init__(
        self,
        api_key: str,
        model: str = "voyageai/voyage-4-nano",
        dimensions: int | None = None,
        app_name: str = _DEFAULT_APP_NAME,
        function_name: str = _DEFAULT_FUNCTION_NAME,
        health_timeout: float = 300.0,
    ) -> None:
        if not api_key:
            raise ModelError(
                "Modal embedding API key is required. "
                "Set the MODAL_EMBEDDING_API_KEY environment variable."
            )
        self._api_key = api_key
        self._model_name = model
        self._dimensions = dimensions
        self._app_name = app_name
        self._function_name = function_name
        self._health_timeout = health_timeout
        self._client: AsyncOpenAI | None = None

    # ------------------------------------------------------------------
    # Lazy initialisation (async)
    # ------------------------------------------------------------------

    async def _ensure_initialised(self) -> None:
        """Resolve the Modal URL, build the client, and health-check.

        Called once on the first ``embed()`` invocation; subsequent calls
        are no-ops.
        """

        if self._client is not None:
            return

        base_url = await self._resolve_web_url()
        self._client = AsyncOpenAI(base_url=base_url, api_key=self._api_key)
        logger.info(
            "ModalEmbeddingModel ready: app=%s func=%s url=%s",
            self._app_name,
            self._function_name,
            base_url,
        )

        await self._health_check(base_url)

    # ------------------------------------------------------------------
    # URL resolution
    # ------------------------------------------------------------------

    async def _resolve_web_url(self) -> str:
        """Look up the deployed Modal function and return its web URL."""

        try:
            fn = modal.Function.from_name(self._app_name, self._function_name)
            url: str = await fn.get_web_url.aio()
        except Exception as exc:
            raise ModelError(
                f"Failed to resolve Modal web URL for "
                f"{self._app_name}/{self._function_name}. "
                f"Is the app deployed? Run: make deploy-embedding-model"
            ) from exc

        if not url:
            raise ModelError(
                f"Modal function {self._app_name}/{self._function_name} "
                f"returned an empty web URL. Is it decorated with @modal.web_server?"
            )

        # Ensure the URL ends with /v1 for OpenAI compatibility.
        url = url.rstrip("/")
        if not url.endswith("/v1"):
            url = f"{url}/v1"

        return url

    # ------------------------------------------------------------------
    # Health check / warm-up
    # ------------------------------------------------------------------

    async def _health_check(self, base_url: str) -> None:
        """Send a GET /health to wake up the Modal container."""

        import aiohttp

        # Strip /v1 suffix to get the root URL for the health endpoint.
        health_base = base_url.rstrip("/")
        if health_base.endswith("/v1"):
            health_base = health_base[: -len("/v1")]

        logger.info("Warming up Modal embedding endpoint: %s/health", health_base)
        try:
            timeout = aiohttp.ClientTimeout(total=self._health_timeout)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(f"{health_base}/health") as resp:
                    if resp.status != 200:
                        raise ExtractionError(
                            f"Modal health check returned status {resp.status}"
                        )
        except ExtractionError:
            raise
        except Exception as exc:
            raise ExtractionError(
                f"Modal health check failed: {exc}. "
                f"The endpoint may still be starting up."
            ) from exc

        logger.info("Modal embedding endpoint is healthy")

    # ------------------------------------------------------------------
    # Embedding
    # ------------------------------------------------------------------

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed texts via the Modal-hosted vLLM server."""

        await self._ensure_initialised()
        assert self._client is not None  # guaranteed by _ensure_initialised

        try:
            kwargs: dict[str, object] = {
                "input": texts,
                "model": self._model_name,
            }
            if self._dimensions is not None:
                kwargs["dimensions"] = self._dimensions

            response = await self._client.embeddings.create(**kwargs)  # type: ignore[arg-type]
        except Exception as exc:
            raise ExtractionError(f"Embedding call failed: {exc}") from exc

        return [item.embedding for item in response.data]
