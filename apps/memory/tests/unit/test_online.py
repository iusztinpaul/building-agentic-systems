"""Unit tests for ``tree.online`` — the cross-pipeline realtime ingest flow.

Covers the ``online-pipeline`` flow (cold-start init, source coercion, inline
extraction + indexing submit), the edge validator, and ``dispatch_online_pipeline``'s
deployment-first / inline-fallback contract. The DATA-step router itself
(``online_ingest``) is covered in ``tests/unit/data/test_online_pipeline.py``.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from beanie import PydanticObjectId

from tree.data.online_pipeline import FileSource, UrlSource
from tree.online import (
    MAX_SOURCE_PAYLOAD_BYTES,
    online_pipeline,
    dispatch_online_pipeline,
    validate_online_source,
)

_USER_ID = PydanticObjectId("507f1f77bcf86cd799439011")


class TestDataEtlOnlineFlow:
    """``online-pipeline`` — ONE run: data step + inline extraction worker.

    A worker executes it cold, so it must init Mongo itself, coerce the
    JSON-serialized ``source`` dict back to the typed union, run the extraction
    WORKER inline (the fire-and-forget caller never sees the Document), and
    submit the trailing indexing run fire-and-forget.
    """

    @pytest.fixture(autouse=True)
    def _stub_infra(self, mocker) -> None:
        mocker.patch("tree.online.init_mongodb", new_callable=AsyncMock)
        mocker.patch("tree.online.flush_opik")

    async def test_coerces_dict_source_extracts_and_submits_indexing(
        self, mocker
    ) -> None:
        doc = MagicMock()
        doc.id = "68a1"
        mock_ingest = mocker.patch(
            "tree.online.online_ingest",
            new_callable=AsyncMock,
            return_value=doc,
        )
        mock_extract = mocker.patch(
            "tree.online.memory_extract_etl_worker", new_callable=AsyncMock
        )
        mock_run = mocker.patch("tree.online.run_deployment", new_callable=AsyncMock)

        result = await online_pipeline(
            {"type": "url", "uri": "https://example.com"}, _USER_ID
        )

        # The dict round-trips to the typed union before hitting the router;
        # extraction runs INLINE (same run), indexing is submitted (timeout=0).
        assert result == "68a1"
        mock_ingest.assert_awaited_once_with(
            UrlSource(uri="https://example.com"), _USER_ID
        )
        mock_extract.assert_awaited_once_with(user_id=_USER_ID, document_ids=["68a1"])
        mock_run.assert_awaited_once()
        assert mock_run.await_args.args == ("memory-indexing-etl/memory-indexing-etl",)
        assert mock_run.await_args.kwargs["timeout"] == 0

    async def test_duplicate_returns_none_without_extracting(self, mocker) -> None:
        mocker.patch(
            "tree.online.online_ingest",
            new_callable=AsyncMock,
            return_value=None,
        )
        mock_extract = mocker.patch(
            "tree.online.memory_extract_etl_worker", new_callable=AsyncMock
        )

        result = await online_pipeline({"type": "conversation", "text": "hi"}, _USER_ID)

        assert result is None
        mock_extract.assert_not_awaited()

    async def test_run_extraction_false_skips_the_memory_step(self, mocker) -> None:
        doc = MagicMock()
        mocker.patch(
            "tree.online.online_ingest",
            new_callable=AsyncMock,
            return_value=doc,
        )
        mock_extract = mocker.patch(
            "tree.online.memory_extract_etl_worker", new_callable=AsyncMock
        )
        mock_run = mocker.patch("tree.online.run_deployment", new_callable=AsyncMock)

        await online_pipeline(
            FileSource(path="/tmp/x.md", content="text"),
            _USER_ID,
            run_extraction=False,
        )

        mock_extract.assert_not_awaited()
        mock_run.assert_not_awaited()

    async def test_failed_indexing_submit_does_not_fail_the_ingest(
        self, mocker
    ) -> None:
        doc = MagicMock()
        doc.id = "68a1"
        mocker.patch(
            "tree.online.online_ingest", new_callable=AsyncMock, return_value=doc
        )
        mocker.patch("tree.online.memory_extract_etl_worker", new_callable=AsyncMock)
        mocker.patch(
            "tree.online.run_deployment",
            new_callable=AsyncMock,
            side_effect=RuntimeError("prefect down"),
        )

        # Fail-open: the document + graph content are durable; the indexing
        # gap is WARNING-logged and covered by any later indexing run.
        result = await online_pipeline(
            {"type": "url", "uri": "https://example.com"}, _USER_ID
        )

        assert result == "68a1"


class TestValidateOnlineSource:
    """Edge validation run BEFORE a fire-and-forget submit — pure, no I/O."""

    def test_accepts_a_valid_url_source(self) -> None:
        validate_online_source(UrlSource(uri="https://example.com/post"))

    def test_rejects_a_bad_url_scheme(self) -> None:
        with pytest.raises(ValueError, match="Unsupported URL scheme"):
            validate_online_source(UrlSource(uri="ftp://example.com/x"))

    def test_rejects_an_oversized_payload(self) -> None:
        # Flow-run parameters are capped server-side; oversized content must
        # fail synchronously with an actionable message, not a remote 4xx.
        big = FileSource(
            path="/tmp/big.md", content="x" * (MAX_SOURCE_PAYLOAD_BYTES + 1)
        )

        with pytest.raises(ValueError, match="parameter cap"):
            validate_online_source(big)


class TestDispatchOnlineIngest:
    """The caller-edge dispatcher: deployment first, inline flow fallback."""

    async def test_submits_the_deployment_fire_and_forget(self, mocker) -> None:
        flow_run = MagicMock()
        flow_run.id = "run-1"
        mock_run = mocker.patch(
            "tree.online.run_deployment",
            new_callable=AsyncMock,
            return_value=flow_run,
        )
        mock_flow = mocker.patch("tree.online.online_pipeline", new_callable=AsyncMock)

        result = await dispatch_online_pipeline(
            UrlSource(uri="https://example.com"), _USER_ID
        )

        # Fire-and-forget: timeout=0, JSON-serialized source, stringified
        # user_id, and the extraction chain delegated to the worker-side flow.
        assert result == {
            "status": "submitted",
            "flow_run_id": "run-1",
            "mode": "deployment",
        }
        mock_run.assert_awaited_once()
        assert mock_run.await_args.args == ("online-pipeline/online-pipeline",)
        assert mock_run.await_args.kwargs["timeout"] == 0
        assert mock_run.await_args.kwargs["parameters"] == {
            "source": {"type": "url", "uri": "https://example.com"},
            "user_id": str(_USER_ID),
            "run_extraction": True,
            "opik_trace_headers": None,
        }
        mock_flow.assert_not_awaited()

    async def test_runs_the_same_flow_inline_when_deployment_unavailable(
        self, mocker
    ) -> None:
        mocker.patch(
            "tree.online.run_deployment",
            new_callable=AsyncMock,
            side_effect=RuntimeError("deployment not found"),
        )
        mock_flow = mocker.patch(
            "tree.online.online_pipeline",
            new_callable=AsyncMock,
            return_value="68a1",
        )

        result = await dispatch_online_pipeline(
            UrlSource(uri="https://example.com"), _USER_ID, run_extraction=False
        )

        assert result == {
            "status": "ingested",
            "document_id": "68a1",
            "mode": "in_process",
        }
        # The fallback runs the SAME flow with the same knobs — one pipeline,
        # two execution loci.
        mock_flow.assert_awaited_once()
        assert mock_flow.await_args.kwargs["run_extraction"] is False

    async def test_inline_duplicate_reports_already_ingested(self, mocker) -> None:
        mocker.patch(
            "tree.online.run_deployment",
            new_callable=AsyncMock,
            side_effect=RuntimeError("prefect down"),
        )
        mocker.patch(
            "tree.online.online_pipeline",
            new_callable=AsyncMock,
            return_value=None,
        )

        result = await dispatch_online_pipeline(
            UrlSource(uri="https://example.com"), _USER_ID
        )

        assert result == {"status": "already_ingested", "mode": "in_process"}

    async def test_validation_failure_raises_before_any_submit(self, mocker) -> None:
        mock_run = mocker.patch("tree.online.run_deployment", new_callable=AsyncMock)

        with pytest.raises(ValueError, match="Unsupported URL scheme"):
            await dispatch_online_pipeline(UrlSource(uri="notaurl"), _USER_ID)

        mock_run.assert_not_awaited()
