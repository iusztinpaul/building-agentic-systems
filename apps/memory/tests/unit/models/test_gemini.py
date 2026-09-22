import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from tree.models.exceptions import ExtractionError
from tree.models.gemini import GeminiEmbeddingModel, GeminiLLM


@pytest.fixture()
def mock_genai_client(mocker) -> MagicMock:
    mock_client = MagicMock()
    mocker.patch("tree.models.gemini.genai.Client", return_value=mock_client)
    return mock_client


class TestGeminiLLM:
    def test_init_sets_model(self, mock_genai_client) -> None:
        llm = GeminiLLM(api_key="fake-key", model="gemini-2.0-flash")

        assert llm._model == "gemini-2.0-flash"

    @pytest.mark.asyncio
    async def test_generate_json_returns_parsed_dict(self, mock_genai_client) -> None:
        expected = {"nodes": [{"name": "AI"}]}
        mock_response = MagicMock()
        mock_response.text = json.dumps(expected)
        mock_genai_client.aio.models.generate_content = AsyncMock(
            return_value=mock_response
        )

        llm = GeminiLLM(api_key="fake-key", model="gemini-2.0-flash")
        result = await llm.generate_json("Extract entities")

        assert result == expected

    @pytest.mark.asyncio
    async def test_generate_json_with_system_instruction(
        self, mock_genai_client
    ) -> None:
        mock_response = MagicMock()
        mock_response.text = '{"ok": true}'
        mock_genai_client.aio.models.generate_content = AsyncMock(
            return_value=mock_response
        )

        llm = GeminiLLM(api_key="fake-key", model="gemini-2.0-flash")
        result = await llm.generate_json("prompt", system="You are helpful")

        assert result == {"ok": True}
        call_kwargs = mock_genai_client.aio.models.generate_content.call_args
        config = call_kwargs.kwargs["config"]
        assert config.system_instruction == "You are helpful"

    @pytest.mark.asyncio
    async def test_generate_json_raises_on_api_error(self, mock_genai_client) -> None:
        mock_genai_client.aio.models.generate_content = AsyncMock(
            side_effect=RuntimeError("API down")
        )

        llm = GeminiLLM(api_key="fake-key", model="gemini-2.0-flash")

        with pytest.raises(ExtractionError, match="Gemini API call failed"):
            await llm.generate_json("prompt")

    @pytest.mark.asyncio
    async def test_generate_json_raises_on_empty_response(
        self, mock_genai_client
    ) -> None:
        mock_response = MagicMock()
        mock_response.text = ""
        mock_genai_client.aio.models.generate_content = AsyncMock(
            return_value=mock_response
        )

        llm = GeminiLLM(api_key="fake-key", model="gemini-2.0-flash")

        with pytest.raises(ExtractionError, match="empty response"):
            await llm.generate_json("prompt")

    @pytest.mark.asyncio
    async def test_generate_json_raises_on_invalid_json(
        self, mock_genai_client
    ) -> None:
        mock_response = MagicMock()
        mock_response.text = "not valid json {{"
        mock_genai_client.aio.models.generate_content = AsyncMock(
            return_value=mock_response
        )

        llm = GeminiLLM(api_key="fake-key", model="gemini-2.0-flash")

        with pytest.raises(ExtractionError, match="invalid JSON"):
            await llm.generate_json("prompt")


class TestGeminiEmbeddingModel:
    def test_init_sets_model_and_dimensions(self, mock_genai_client) -> None:
        model = GeminiEmbeddingModel(
            api_key="fake-key", model="text-embedding-004", dimensions=256
        )

        assert model._model == "text-embedding-004"
        assert model._dimensions == 256

    @pytest.mark.asyncio
    async def test_embed_returns_vectors(self, mock_genai_client) -> None:
        embedding1 = MagicMock()
        embedding1.values = [0.1, 0.2, 0.3]
        embedding2 = MagicMock()
        embedding2.values = [0.4, 0.5, 0.6]
        mock_response = MagicMock()
        mock_response.embeddings = [embedding1, embedding2]
        mock_genai_client.aio.models.embed_content = AsyncMock(
            return_value=mock_response
        )

        model = GeminiEmbeddingModel(
            api_key="fake-key", model="text-embedding-004", dimensions=256
        )
        result = await model.embed(["hello", "world"])

        assert result == [[0.1, 0.2, 0.3], [0.4, 0.5, 0.6]]

    @pytest.mark.asyncio
    async def test_embed_handles_none_values(self, mock_genai_client) -> None:
        embedding = MagicMock()
        embedding.values = None
        mock_response = MagicMock()
        mock_response.embeddings = [embedding]
        mock_genai_client.aio.models.embed_content = AsyncMock(
            return_value=mock_response
        )

        model = GeminiEmbeddingModel(
            api_key="fake-key", model="text-embedding-004", dimensions=256
        )
        result = await model.embed(["hello"])

        assert result == [[]]

    @pytest.mark.asyncio
    async def test_embed_raises_on_api_error(self, mock_genai_client) -> None:
        mock_genai_client.aio.models.embed_content = AsyncMock(
            side_effect=RuntimeError("API down")
        )

        model = GeminiEmbeddingModel(
            api_key="fake-key", model="text-embedding-004", dimensions=256
        )

        with pytest.raises(ExtractionError, match="Gemini embedding call failed"):
            await model.embed(["hello"])


class TestTaskType:
    """The **Embedding role** maps to Gemini's ``task_type`` (ADR-009 §5).

    Source (read 2026-09-19): https://ai.google.dev/gemini-api/docs/embeddings
    — ``RETRIEVAL_QUERY`` / ``RETRIEVAL_DOCUMENT`` are supported task types for
    ``gemini-embedding-001``, while "you cannot use the ``task_type`` field for
    the ``gemini-embedding-2`` model". ``google-genai`` 1.65.0 types
    ``EmbedContentConfig.task_type`` as a free-form ``Optional[str]``.

    The field is nonetheless sent for EVERY model id, ``gemini-embedding-2``
    included. Verified live on 2026-09-19: that model ACCEPTS the field and
    IGNORES it (the ``RETRIEVAL_QUERY`` and role-less vectors are identical,
    cosine 1.000000) — the server already implements "cannot honour → ignore,
    never raise", so a client-side list of supporting ids would buy nothing
    and would silently drop the role on the next id Google ships
    (``gemini-embedding-2-preview`` is already live). Do NOT "fix" this back
    into a guard without re-running that check.
    """

    async def _config_for(self, mock_genai_client, *, model: str, **kwargs) -> dict:
        embedding = MagicMock()
        embedding.values = [0.1]
        response = MagicMock()
        response.embeddings = [embedding]
        mock_genai_client.aio.models.embed_content = AsyncMock(return_value=response)

        gemini = GeminiEmbeddingModel(api_key="fake-key", model=model, dimensions=256)
        await gemini.embed(["hello"], **kwargs)

        return mock_genai_client.aio.models.embed_content.call_args.kwargs["config"]

    @pytest.mark.parametrize("model", ["gemini-embedding-001", "gemini-embedding-2"])
    @pytest.mark.parametrize(
        "role,expected",
        [("query", "RETRIEVAL_QUERY"), ("document", "RETRIEVAL_DOCUMENT")],
    )
    async def test_role_maps_to_retrieval_task_type(
        self, mock_genai_client, model: str, role: str, expected: str
    ) -> None:
        config = await self._config_for(mock_genai_client, model=model, input_type=role)

        assert config["task_type"] == expected
        assert config["output_dimensionality"] == 256

    async def test_no_role_omits_the_task_type_key(self, mock_genai_client) -> None:
        config = await self._config_for(
            mock_genai_client, model="gemini-embedding-001", input_type=None
        )

        assert "task_type" not in config
        assert config["output_dimensionality"] == 256

    async def test_default_call_omits_the_task_type_key(
        self, mock_genai_client
    ) -> None:
        """User story 3: a caller that passes no role sends today's config."""

        config = await self._config_for(mock_genai_client, model="gemini-embedding-001")

        assert "task_type" not in config
        assert config["output_dimensionality"] == 256

    async def test_unknown_role_is_ignored(self, mock_genai_client) -> None:
        """A role outside ``{"query", "document", None}`` must be IGNORED, not
        raised on (ADR-009 §5: "a provider that cannot honour a role ignores
        it — it never raises"). Reachable from an operator override or a role
        this provider has no mapping for yet; a bare ``KeyError`` there would
        fail an ingestion run over a retrieval HINT.
        """

        unknown: Any = "clustering"

        config = await self._config_for(
            mock_genai_client, model="gemini-embedding-001", input_type=unknown
        )

        assert "task_type" not in config
        assert config["output_dimensionality"] == 256

    async def test_output_dimensionality_survives_a_role(
        self, mock_genai_client
    ) -> None:
        """The Matryoshka truncation is dimension-coupled to the live vector
        index — a role must never displace it."""

        config = await self._config_for(
            mock_genai_client, model="gemini-embedding-001", input_type="document"
        )

        assert config == {
            "output_dimensionality": 256,
            "task_type": "RETRIEVAL_DOCUMENT",
        }
