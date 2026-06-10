"""Unit tests for Opik instrumentation on the embedding model clients.

These guard the cost / usage extraction logic that runs INSIDE each client's
``embed`` success path:

* Voyage (text + multimodal): reads ``usage.total_tokens`` from the HTTP
  response and records a manual ``total_cost`` from the YAML price map.
* Modal/vLLM: reads ``response.usage.total_tokens`` and records it with
  ``total_cost=0`` (self-hosted); records without usage when absent.
* All recording is fail-open: a telemetry failure must never break ``embed``.

The Opik SDK boundary is mocked via the ``record_embedding_usage`` symbol each
client imported, so we assert on the structured call rather than the network.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tree.models.modal_embedding import ModalEmbeddingModel
from tree.models.voyage_embedding import VoyageTextEmbeddingModel
from tree.models.voyage_multimodal_embedding import VoyageMultimodalEmbeddingModel


def _mock_aiohttp_response(*, status: int = 200, json_data: dict | None = None):
    mock_resp = AsyncMock()
    mock_resp.status = status
    mock_resp.json = AsyncMock(return_value=json_data or {})
    mock_resp.__aenter__ = AsyncMock(return_value=mock_resp)
    mock_resp.__aexit__ = AsyncMock(return_value=False)
    return mock_resp


def _mock_aiohttp_session(mock_resp: AsyncMock):
    mock_post = MagicMock(return_value=mock_resp)
    mock_session = AsyncMock()
    mock_session.post = mock_post
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)
    return mock_session, mock_post


class TestVoyageTextCostRecording:
    async def test_records_usage_and_nonzero_cost(self, mocker) -> None:
        # Arrange — 1,000,000 tokens of voyage-3.5 at $0.06/1M → $0.06.
        rec = mocker.patch("tree.models.voyage_embedding.record_embedding_usage")
        model = VoyageTextEmbeddingModel(api_key="key", model="voyage-3.5")
        response_data = {
            "data": [{"embedding": [0.1]}],
            "usage": {"total_tokens": 1_000_000},
        }
        mock_resp = _mock_aiohttp_response(status=200, json_data=response_data)
        mock_session, _ = _mock_aiohttp_session(mock_resp)

        # Act
        with patch("aiohttp.ClientSession", return_value=mock_session):
            await model.embed(["hello"])

        # Assert
        rec.assert_called_once()
        kwargs = rec.call_args.kwargs
        assert kwargs["provider"] == "voyage"
        assert kwargs["model"] == "voyage-3.5"
        assert kwargs["total_tokens"] == 1_000_000
        assert kwargs["total_cost"] == pytest.approx(0.06)

    async def test_missing_usage_records_zero_cost(self, mocker) -> None:
        # Arrange — response without a usage block (defensive).
        rec = mocker.patch("tree.models.voyage_embedding.record_embedding_usage")
        model = VoyageTextEmbeddingModel(api_key="key", model="voyage-3.5")
        response_data = {"data": [{"embedding": [0.1]}]}
        mock_resp = _mock_aiohttp_response(status=200, json_data=response_data)
        mock_session, _ = _mock_aiohttp_session(mock_resp)

        # Act
        with patch("aiohttp.ClientSession", return_value=mock_session):
            await model.embed(["hello"])

        # Assert — usage None, cost 0, but the call still recorded.
        kwargs = rec.call_args.kwargs
        assert kwargs["total_tokens"] is None
        assert kwargs["total_cost"] == 0.0

    async def test_recording_failure_does_not_break_embed(self, mocker) -> None:
        # Arrange — telemetry blows up; embed must still return vectors.
        mocker.patch(
            "tree.models.voyage_embedding.record_embedding_usage",
            side_effect=RuntimeError("opik down"),
        )
        model = VoyageTextEmbeddingModel(api_key="key", model="voyage-3.5")
        response_data = {
            "data": [{"embedding": [0.1, 0.2]}],
            "usage": {"total_tokens": 10},
        }
        mock_resp = _mock_aiohttp_response(status=200, json_data=response_data)
        mock_session, _ = _mock_aiohttp_session(mock_resp)

        # Act
        with patch("aiohttp.ClientSession", return_value=mock_session):
            result = await model.embed(["hello"])

        # Assert — embed succeeded despite telemetry failure.
        assert result == [[0.1, 0.2]]


class TestVoyageMultimodalCostRecording:
    async def test_records_usage_and_nonzero_cost(self, mocker) -> None:
        # Arrange — voyage-multimodal-3 at $0.12/1M.
        rec = mocker.patch(
            "tree.models.voyage_multimodal_embedding.record_embedding_usage"
        )
        model = VoyageMultimodalEmbeddingModel(
            api_key="key", model="voyage-multimodal-3"
        )
        response_data = {
            "data": [{"embedding": [0.1]}],
            "usage": {"total_tokens": 500_000},
        }
        mock_resp = _mock_aiohttp_response(status=200, json_data=response_data)
        mock_session, _ = _mock_aiohttp_session(mock_resp)

        # Act
        with patch("aiohttp.ClientSession", return_value=mock_session):
            await model.embed(["hello"])

        # Assert — 500K × $0.12/1M = $0.06.
        kwargs = rec.call_args.kwargs
        assert kwargs["provider"] == "voyage"
        assert kwargs["total_tokens"] == 500_000
        assert kwargs["total_cost"] == pytest.approx(0.06)


class TestModalUsageRecording:
    def _model(self) -> ModalEmbeddingModel:
        m = ModalEmbeddingModel(api_key="key", model="voyageai/voyage-4-nano")
        # Bypass lazy Modal URL resolution + health check.
        m._client = MagicMock()
        m._ensure_initialised = AsyncMock()  # type: ignore[method-assign]
        return m

    async def test_records_token_usage_with_zero_cost(self, mocker) -> None:
        # Arrange — vLLM returns a usage object; self-hosted → cost 0.
        rec = mocker.patch("tree.models.modal_embedding.record_embedding_usage")
        model = self._model()
        usage = MagicMock()
        usage.total_tokens = 42
        response = MagicMock()
        response.usage = usage
        response.data = [MagicMock(embedding=[0.1, 0.2])]
        model._client.embeddings.create = AsyncMock(return_value=response)

        # Act
        await model.embed(["hello"])

        # Assert
        kwargs = rec.call_args.kwargs
        assert kwargs["provider"] == "modal"
        assert kwargs["model"] == "voyageai/voyage-4-nano"
        assert kwargs["total_tokens"] == 42
        assert kwargs["total_cost"] == 0.0

    async def test_records_without_usage_when_absent(self, mocker) -> None:
        # Arrange — response has no usage object.
        rec = mocker.patch("tree.models.modal_embedding.record_embedding_usage")
        model = self._model()
        response = MagicMock()
        response.usage = None
        response.data = [MagicMock(embedding=[0.1])]
        model._client.embeddings.create = AsyncMock(return_value=response)

        # Act
        await model.embed(["hello"])

        # Assert — recorded with total_tokens None (no usage), cost still 0.
        kwargs = rec.call_args.kwargs
        assert kwargs["total_tokens"] is None
        assert kwargs["total_cost"] == 0.0

    async def test_recording_failure_does_not_break_embed(self, mocker) -> None:
        # Arrange
        mocker.patch(
            "tree.models.modal_embedding.record_embedding_usage",
            side_effect=RuntimeError("opik down"),
        )
        model = self._model()
        response = MagicMock()
        response.usage = MagicMock(total_tokens=5)
        response.data = [MagicMock(embedding=[0.9])]
        model._client.embeddings.create = AsyncMock(return_value=response)

        # Act
        result = await model.embed(["hello"])

        # Assert
        assert result == [[0.9]]
