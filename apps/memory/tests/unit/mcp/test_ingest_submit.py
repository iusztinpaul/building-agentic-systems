"""Unit tests for ``submit_ingestion`` — the async-ingestion submit contract.

``submit_ingestion`` is the boundary helper the MCP ingest tools call instead of
running extraction in-process: it fires the extraction-orchestrator deployment
and returns a status the caller surfaces. These tests cover the three return
contracts (submitted / empty-content / Prefect-unreachable), mocking only the
Prefect client boundary (``get_client``).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from beanie import PydanticObjectId

from tree.entities.documents import Document, SourceType
from tree.mcp.ingest import submit_ingestion


def _make_document(*, content: str | None) -> Document:
    return Document(
        user_id=PydanticObjectId(),
        source_type=SourceType.CONVERSATION,
        source_uri="conversation://abc",
        title="t",
        content=content,
    )


def _stub_get_client(mocker, *, deployment_id: str, flow_run_id: str) -> MagicMock:
    """Patch ``tree.mcp.ingest.get_client`` with a fake successful Prefect client."""

    fake_client = MagicMock()
    fake_client.read_deployment_by_name = AsyncMock(
        return_value=MagicMock(id=deployment_id)
    )
    fake_client.create_flow_run_from_deployment = AsyncMock(
        return_value=MagicMock(id=flow_run_id)
    )
    ctx_manager = MagicMock()
    ctx_manager.__aenter__ = AsyncMock(return_value=fake_client)
    ctx_manager.__aexit__ = AsyncMock(return_value=False)
    mocker.patch("tree.mcp.ingest.get_client", return_value=ctx_manager)
    return fake_client


async def test_submitted_returns_flow_run_id_and_scopes_to_document(mocker) -> None:
    # Arrange
    document = _make_document(content="some content")
    user_id = PydanticObjectId()
    client = _stub_get_client(mocker, deployment_id="dep-1", flow_run_id="fr-9")

    # Act
    result = await submit_ingestion(document, user_id=user_id)

    # Assert — submitted, with the flow run id and document echoed back.
    assert result["status"] == "submitted"
    assert result["flow_run_id"] == "fr-9"
    assert result["document_id"] == str(document.id)
    params = client.create_flow_run_from_deployment.await_args.kwargs["parameters"]
    assert params == {"user_id": str(user_id), "document_ids": [str(document.id)]}


async def test_empty_content_is_not_submitted(mocker) -> None:
    # Arrange — a content-less document must never reach Prefect.
    document = _make_document(content=None)
    spy = mocker.patch("tree.mcp.ingest.get_client")

    # Act
    result = await submit_ingestion(document, user_id=PydanticObjectId())

    # Assert
    assert result["status"] == "not_submitted"
    assert result["reason"] == "empty_content"
    spy.assert_not_called()


async def test_prefect_unreachable_returns_not_submitted_with_error(mocker) -> None:
    # Arrange — the Prefect API raising must degrade to a clean status, not raise.
    document = _make_document(content="some content")
    mocker.patch("tree.mcp.ingest.get_client", side_effect=RuntimeError("prefect down"))

    # Act
    result = await submit_ingestion(document, user_id=PydanticObjectId())

    # Assert
    assert result["status"] == "not_submitted"
    assert "prefect down" in result["error"]
    assert result["document_id"] == str(document.id)
