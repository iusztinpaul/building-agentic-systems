"""Unit tests for ``tree.online`` — the cross-pipeline realtime ingest flow.

Covers the ``online-pipeline`` flow (cold-start init, source coercion, inline
extraction + inline indexing), the edge validator, and
``dispatch_online_pipeline``'s single-path contract: submit the core deployment
fire-and-forget, report the new run's own state, let failures propagate. The
DATA-step router itself (``online_ingest``) is covered in
``tests/unit/data/test_online_pipeline.py``.
"""

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from beanie import PydanticObjectId
from prefect.client.schemas.objects import State, StateType

from tree.data.online_pipeline import FileSource, UrlSource
from tree.entities.documents import SourceType
from tree.online import (
    MAX_SOURCE_PAYLOAD_BYTES,
    IngestReceipt,
    online_pipeline,
    dispatch_online_pipeline,
    validate_online_source,
)

_USER_ID = PydanticObjectId("507f1f77bcf86cd799439011")


def _flow_run(
    run_id: str = "run-1", state_type: StateType = StateType.SCHEDULED
) -> SimpleNamespace:
    """A just-CREATED flow run, as ``run_deployment(timeout=0)`` returns it."""

    return SimpleNamespace(id=run_id, state=State(type=state_type))


class TestDataEtlOnlineFlow:
    """``online-pipeline`` — ONE run: data step + inline extraction worker.

    A worker executes it cold, so it must init Mongo itself, coerce the
    JSON-serialized ``source`` dict back to the typed union, run the extraction
    WORKER inline (the fire-and-forget caller never sees the Document), and run
    the trailing ``memory_indexing`` flow inline too — since
    ``free-tier-deployments`` indexing is a subflow, not a submitted deployment run.
    """

    @pytest.fixture(autouse=True)
    def _stub_infra(self, mocker) -> None:
        mocker.patch("tree.online.init_mongodb", new_callable=AsyncMock)
        mocker.patch("tree.online.flush_opik")

    @pytest.fixture
    def mock_indexing(self, mocker) -> AsyncMock:
        """Stub the INLINE ``memory_indexing`` subflow (no Mongo, no Prefect run)."""

        return mocker.patch("tree.online.memory_indexing", new_callable=AsyncMock)

    async def test_coerces_dict_source_extracts_and_indexes_inline(
        self, mocker, mock_indexing
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
        # extraction AND indexing both run INLINE in this same run — no dispatch.
        assert result == "68a1"
        mock_ingest.assert_awaited_once_with(
            UrlSource(uri="https://example.com"), _USER_ID
        )
        mock_extract.assert_awaited_once_with(user_id=_USER_ID, document_ids=["68a1"])
        mock_indexing.assert_awaited_once_with(user_id=_USER_ID)
        mock_run.assert_not_awaited()

    async def test_duplicate_returns_none_without_extracting(
        self, mocker, mock_indexing
    ) -> None:
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
        mock_indexing.assert_not_awaited()

    async def test_run_extraction_false_skips_the_memory_step(
        self, mocker, mock_indexing
    ) -> None:
        doc = MagicMock()
        mocker.patch(
            "tree.online.online_ingest",
            new_callable=AsyncMock,
            return_value=doc,
        )
        mock_extract = mocker.patch(
            "tree.online.memory_extract_etl_worker", new_callable=AsyncMock
        )
        await online_pipeline(
            FileSource(path="/tmp/x.md", content="text"),
            _USER_ID,
            run_extraction=False,
        )

        mock_extract.assert_not_awaited()
        mock_indexing.assert_not_awaited()

    async def test_failed_inline_indexing_does_not_fail_the_ingest(
        self, mocker, mock_indexing
    ) -> None:
        doc = MagicMock()
        doc.id = "68a1"
        mocker.patch(
            "tree.online.online_ingest", new_callable=AsyncMock, return_value=doc
        )
        mocker.patch("tree.online.memory_extract_etl_worker", new_callable=AsyncMock)
        mock_indexing.side_effect = RuntimeError("mongot down")

        # Fail-open (unchanged by going inline): the document + graph content are
        # durable; the indexing gap is WARNING-logged and covered by any later
        # indexing run.
        result = await online_pipeline(
            {"type": "url", "uri": "https://example.com"}, _USER_ID
        )

        assert result == "68a1"
        mock_indexing.assert_awaited_once()


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


def _existing_document(
    source_type: SourceType = SourceType.WEB, doc_id: str = "507f1f77bcf86cd799439012"
) -> SimpleNamespace:
    """A Document row as the pre-flight ``find_one`` hands it back."""

    return SimpleNamespace(id=PydanticObjectId(doc_id), source_type=source_type)


class TestDispatchOnlineIngest:
    """The caller-edge dispatcher: one pre-flight lookup, then ONE submit path.

    ``online-pipeline`` is always registered, so dispatch simply requires a
    reachable Prefect API: no in-process fallback, and submission failures
    reach the caller instead of turning into a silent blocking local run. The
    **Ingest receipt** it answers carries the Document's natural key plus the
    duplicate verdict known at submit time (ADR-008 §1).
    """

    @pytest.fixture
    def mock_find_one(self, mocker) -> AsyncMock:
        """Pre-flight duplicate lookup — a MISS by default (nothing ingested yet)."""

        return mocker.patch(
            "tree.online.Document.find_one", new_callable=AsyncMock, return_value=None
        )

    @pytest.fixture(autouse=True)
    def _no_existing_document(self, mock_find_one) -> None:
        """Every test in this class needs the lookup stubbed, hit or miss."""

    async def test_submits_the_deployment_fire_and_forget(
        self, mocker, mock_find_one
    ) -> None:
        mock_run = mocker.patch(
            "tree.online.run_deployment",
            new_callable=AsyncMock,
            return_value=_flow_run(),
        )
        mock_flow = mocker.patch("tree.online.online_pipeline", new_callable=AsyncMock)

        result = await dispatch_online_pipeline(
            UrlSource(uri="https://example.com"), _USER_ID
        )

        # Fire-and-forget: timeout=0, JSON-serialized source, stringified
        # user_id, and the extraction chain delegated to the worker-side flow.
        assert result == IngestReceipt(
            source_uri="https://example.com",
            duplicate=False,
            document_id=None,
            flow_run_id="run-1",
            status="scheduled",
        )
        mock_find_one.assert_awaited_once_with(
            {"user_id": _USER_ID, "source_uri": "https://example.com"}
        )
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

    @pytest.mark.parametrize(
        ("state_type", "expected"),
        [(StateType.SCHEDULED, "scheduled"), (StateType.PENDING, "pending")],
    )
    async def test_status_reports_the_flow_runs_own_state(
        self, mocker, state_type: StateType, expected: str
    ) -> None:
        mocker.patch(
            "tree.online.run_deployment",
            new_callable=AsyncMock,
            return_value=_flow_run(state_type=state_type),
        )

        result = await dispatch_online_pipeline(
            UrlSource(uri="https://example.com"), _USER_ID
        )

        # The status is Prefect's own state for a dispatched run; the ingest
        # outcome lives in ``duplicate``.
        assert result.status == expected
        assert result.duplicate is False

    async def test_submission_failure_propagates(self, mocker) -> None:
        mocker.patch(
            "tree.online.run_deployment",
            new_callable=AsyncMock,
            side_effect=RuntimeError("Failed to reach API"),
        )
        mock_flow = mocker.patch("tree.online.online_pipeline", new_callable=AsyncMock)

        # No fallback swallows it: an unreachable API, a missing deployment or
        # bad parameters must surface to the caller.
        with pytest.raises(RuntimeError, match="Failed to reach API"):
            await dispatch_online_pipeline(
                UrlSource(uri="https://example.com"), _USER_ID
            )

        mock_flow.assert_not_awaited()

    async def test_validation_failure_raises_before_any_submit(self, mocker) -> None:
        mock_run = mocker.patch("tree.online.run_deployment", new_callable=AsyncMock)

        with pytest.raises(ValueError, match="Unsupported URL scheme"):
            await dispatch_online_pipeline(UrlSource(uri="notaurl"), _USER_ID)

        mock_run.assert_not_awaited()

    async def test_duplicate_short_circuits_without_dispatch(
        self, mocker, mock_find_one
    ) -> None:
        mock_find_one.return_value = _existing_document()
        mock_run = mocker.patch("tree.online.run_deployment", new_callable=AsyncMock)

        result = await dispatch_online_pipeline(
            UrlSource(uri="https://example.com/post"), _USER_ID
        )

        # A submit-time duplicate answers with the id it already has and
        # dispatches NOTHING — no flow run, no worker time.
        assert result == IngestReceipt(
            source_uri="https://example.com/post",
            duplicate=True,
            document_id="507f1f77bcf86cd799439012",
            flow_run_id=None,
            status="duplicate",
        )
        mock_run.assert_not_awaited()

    async def test_latent_row_is_not_a_duplicate(self, mocker, mock_find_one) -> None:
        # A LATENT row is a placeholder the leaf UPGRADES in place, so the
        # source is still new: dispatch it and keep ``document_id`` unset.
        mock_find_one.return_value = _existing_document(SourceType.LATENT)
        mock_run = mocker.patch(
            "tree.online.run_deployment",
            new_callable=AsyncMock,
            return_value=_flow_run(),
        )

        result = await dispatch_online_pipeline(
            FileSource(path="/tmp/notes.md", content="body"), _USER_ID
        )

        assert result == IngestReceipt(
            source_uri="file:///tmp/notes.md",
            duplicate=False,
            document_id=None,
            flow_run_id="run-1",
            status="scheduled",
        )
        mock_run.assert_awaited_once()

    async def test_duplicate_is_logged_with_its_source_uri(
        self, mocker, mock_find_one, caplog
    ) -> None:
        mock_find_one.return_value = _existing_document()
        mocker.patch("tree.online.run_deployment", new_callable=AsyncMock)

        with caplog.at_level(logging.INFO, logger="tree.online"):
            await dispatch_online_pipeline(
                UrlSource(uri="https://example.com/post"), _USER_ID
            )

        assert "https://example.com/post" in caplog.text
