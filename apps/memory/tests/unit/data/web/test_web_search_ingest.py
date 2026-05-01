"""Unit tests for ``tree.data.web.web_search_ingest.trigger_url_batch_ingest``.

The helper is a thin wrapper around Prefect's async client. We mock the
``get_client`` async-context-manager and assert the helper:
- looks up the right deployment by name,
- creates the flow run with the URLs in the parameters dict,
- returns the flow_run_id + a tracking URL,
- never polls the run's state (fire-and-forget).
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from tree.data.web.web_search_ingest import (
    DEPLOYMENT_NAME,
    trigger_url_batch_ingest,
)


def _make_mock_client(
    *,
    deployment_id: str = "deploy-123",
    flow_run_id: str = "flow-abc-456",
    api_url: str = "http://127.0.0.1:4200/api",
) -> MagicMock:
    """Build a mock Prefect client with the two methods the helper calls."""

    client = MagicMock()
    client.api_url = api_url
    client.read_deployment_by_name = AsyncMock(
        return_value=SimpleNamespace(id=deployment_id)
    )
    client.create_flow_run_from_deployment = AsyncMock(
        return_value=SimpleNamespace(id=flow_run_id)
    )
    # Spy: must NOT be called by the helper.
    client.read_flow_run = AsyncMock()
    return client


def _patch_get_client(mocker, client: MagicMock) -> None:
    @asynccontextmanager
    async def _ctx():
        yield client

    mocker.patch(
        "tree.data.web.web_search_ingest.get_client",
        side_effect=lambda: _ctx(),
    )


class TestTriggerUrlBatchIngest:
    async def test_empty_urls_raises_value_error(self) -> None:
        # Arrange / Act / Assert
        with pytest.raises(ValueError, match="urls must not be empty"):
            await trigger_url_batch_ingest([])

    async def test_returns_flow_run_id_and_tracking_url(self, mocker) -> None:
        # Arrange
        client = _make_mock_client(
            flow_run_id="abcd-1234",
            api_url="http://127.0.0.1:4200/api",
        )
        _patch_get_client(mocker, client)

        # Act
        result = await trigger_url_batch_ingest(["https://a", "https://b"])

        # Assert
        assert result["flow_run_id"] == "abcd-1234"
        assert result["tracking_url"] == "http://127.0.0.1:4200/runs/flow-run/abcd-1234"

    async def test_looks_up_deployment_by_canonical_name(self, mocker) -> None:
        # Arrange
        client = _make_mock_client()
        _patch_get_client(mocker, client)

        # Act
        await trigger_url_batch_ingest(["https://a"])

        # Assert
        client.read_deployment_by_name.assert_awaited_once_with(DEPLOYMENT_NAME)
        assert DEPLOYMENT_NAME == "ingest-web-url-batch-etl/ingest-web-url-batch-etl"

    async def test_passes_urls_in_parameters(self, mocker) -> None:
        # Arrange
        client = _make_mock_client(deployment_id="dep-xyz")
        _patch_get_client(mocker, client)
        urls = ["https://a", "https://b", "https://c"]

        # Act
        await trigger_url_batch_ingest(urls)

        # Assert
        client.create_flow_run_from_deployment.assert_awaited_once()
        kwargs = client.create_flow_run_from_deployment.await_args.kwargs
        assert kwargs["deployment_id"] == "dep-xyz"
        assert kwargs["parameters"] == {"urls": urls}

    async def test_does_not_poll_run_state(self, mocker) -> None:
        """Fire-and-forget: helper must not call ``read_flow_run`` (no polling loop)."""

        # Arrange
        client = _make_mock_client()
        _patch_get_client(mocker, client)

        # Act
        await trigger_url_batch_ingest(["https://a"])

        # Assert
        client.read_flow_run.assert_not_awaited()

    async def test_propagates_client_errors(self, mocker) -> None:
        """Deployment-not-found and connection errors propagate to caller."""

        # Arrange
        client = _make_mock_client()
        client.read_deployment_by_name = AsyncMock(
            side_effect=RuntimeError("deployment not found")
        )
        _patch_get_client(mocker, client)

        # Act / Assert
        with pytest.raises(RuntimeError, match="deployment not found"):
            await trigger_url_batch_ingest(["https://a"])

    async def test_strips_api_suffix_for_tracking_url(self, mocker) -> None:
        # Arrange — api_url ends with /api/, base URL should drop the suffix.
        client = _make_mock_client(
            flow_run_id="run-1",
            api_url="http://prefect.local:4200/api",
        )
        _patch_get_client(mocker, client)

        # Act
        result = await trigger_url_batch_ingest(["https://a"])

        # Assert
        assert result["tracking_url"] == "http://prefect.local:4200/runs/flow-run/run-1"
