"""Unit tests for the ``search_web`` MCP tool and its CLI wrapper.

The MCP tool is exercised by calling its underlying coroutine directly with
``tree.data.web.web_serp.search`` mocked — we do not spin up the FastMCP
server here. End-to-end MCP testing lives in the integration suite.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from beanie import PydanticObjectId
from click.testing import CliRunner

from tree.data.web import SearchResult
from tree.data.web.web_unlocker import (
    BrightDataConfigurationError,
    BrightDataRequestError,
)

_USER_ID = PydanticObjectId("507f1f77bcf86cd799439011")


# ---------------------------------------------------------------------------
# MCP tool tests — call the registered tool's underlying function directly.
# ---------------------------------------------------------------------------


def _sample_results() -> list[SearchResult]:
    return [
        SearchResult(
            rank=1,
            title="Knowledge graphs",
            url="https://example.com/kg",
            snippet="An intro",
        ),
        SearchResult(
            rank=2,
            title="Graph DBs",
            url="https://example.com/graphdb",
            snippet="Survey",
        ),
    ]


def _make_ctx() -> MagicMock:
    """Build a mock FastMCP Context.

    The ``search_web`` ingest path reads ``lifespan_context['user_id']`` so we
    stub a real dict with a stable :class:`PydanticObjectId` here. The non-
    ingest path doesn't touch it.
    """

    ctx = MagicMock()
    ctx.lifespan_context = {"user_id": _USER_ID}
    return ctx


def _get_tool_callable():
    """Return the underlying coroutine for the registered ``search_web`` tool.

    FastMCP wraps the function in a ``FunctionTool``; the original coroutine is
    available as the ``.fn`` attribute on the wrapped object.
    """

    from tree.mcp.tools import search_web

    return getattr(search_web, "fn", search_web)


class TestSearchWebMcpTool:
    async def test_returns_json_payload_on_success(self, mocker) -> None:
        # Arrange
        mock_search = AsyncMock(return_value=_sample_results())
        mocker.patch("tree.mcp.tools.web_search", mock_search)
        ctx = _make_ctx()

        # Act
        raw = await _get_tool_callable()(
            "knowledge graphs",
            ctx,
            engine="google",
            num_results=5,
        )

        # Assert
        payload = json.loads(raw)
        assert payload["query"] == "knowledge graphs"
        assert payload["engine"] == "google"
        assert len(payload["results"]) == 2
        first = payload["results"][0]
        assert first["rank"] == 1
        assert first["title"] == "Knowledge graphs"
        assert first["url"] == "https://example.com/kg"
        assert first["snippet"] == "An intro"

        # Confirm the underlying SERP client received the right kwargs.
        mock_search.assert_awaited_once()
        kwargs = mock_search.await_args.kwargs
        assert kwargs["engine"] == "google"
        assert kwargs["num_results"] == 5
        assert kwargs["country"] is None
        assert kwargs["language"] is None

    async def test_passes_locale_kwargs(self, mocker) -> None:
        # Arrange
        mock_search = AsyncMock(return_value=[])
        mocker.patch("tree.mcp.tools.web_search", mock_search)
        ctx = _make_ctx()

        # Act
        raw = await _get_tool_callable()(
            "datenschutz",
            ctx,
            engine="google",
            num_results=10,
            country="de",
            language="de",
        )

        # Assert
        payload = json.loads(raw)
        assert payload["results"] == []

        kwargs = mock_search.await_args.kwargs
        assert kwargs["country"] == "de"
        assert kwargs["language"] == "de"

    async def test_does_not_invoke_ingestion_pipeline(self, mocker) -> None:
        # Arrange
        mock_search = AsyncMock(return_value=_sample_results())
        mocker.patch("tree.mcp.tools.web_search", mock_search)
        # Spy on the ingestion submission — search_web must NEVER call it.
        mock_ingestion = mocker.patch(
            "tree.mcp.tools.submit_ingestion", new_callable=AsyncMock
        )
        ctx = _make_ctx()

        # Act
        await _get_tool_callable()("hello", ctx)

        # Assert
        mock_ingestion.assert_not_awaited()

    async def test_does_not_touch_lifespan_context(self, mocker) -> None:
        """The SERP client doesn't need MongoDB/LLM/embedder; the tool must not access them."""

        # Arrange
        mock_search = AsyncMock(return_value=_sample_results())
        mocker.patch("tree.mcp.tools.web_search", mock_search)
        ctx = MagicMock()
        # Force AttributeError if the tool accesses lifespan_context.
        type(ctx).lifespan_context = property(
            lambda self: (_ for _ in ()).throw(
                AssertionError("search_web must not read lifespan_context")
            )
        )

        # Act
        raw = await _get_tool_callable()("hello", ctx)

        # Assert — if the tool accessed lifespan_context the property raises.
        json.loads(raw)

    async def test_empty_query_returns_invalid_input_error(self, mocker) -> None:
        # Arrange — mimic web_serp's behavior: ValueError on empty query.
        mock_search = AsyncMock(side_effect=ValueError("query must not be empty"))
        mocker.patch("tree.mcp.tools.web_search", mock_search)
        ctx = _make_ctx()

        # Act
        raw = await _get_tool_callable()("", ctx)

        # Assert
        payload = json.loads(raw)
        assert payload["error"] == "invalid_input"
        assert "empty" in payload["detail"].lower()

    async def test_configuration_error_is_serialized(self, mocker) -> None:
        # Arrange
        mock_search = AsyncMock(
            side_effect=BrightDataConfigurationError("BRIGHTDATA_SERP_ZONE is not set")
        )
        mocker.patch("tree.mcp.tools.web_search", mock_search)
        ctx = _make_ctx()

        # Act
        raw = await _get_tool_callable()("anything", ctx)

        # Assert
        payload = json.loads(raw)
        assert payload["error"] == "configuration_error"
        assert "BRIGHTDATA_SERP_ZONE" in payload["detail"]

    async def test_request_error_is_serialized_as_fetch_failed(self, mocker) -> None:
        # Arrange
        mock_search = AsyncMock(
            side_effect=BrightDataRequestError(
                "Bright Data SERP API returned HTTP 503: upstream timeout"
            )
        )
        mocker.patch("tree.mcp.tools.web_search", mock_search)
        ctx = _make_ctx()

        # Act
        raw = await _get_tool_callable()("anything", ctx)

        # Assert
        payload = json.loads(raw)
        assert payload["error"] == "fetch_failed"
        assert "503" in payload["detail"]

    async def test_http_status_error_is_serialized(self, mocker) -> None:
        # Arrange
        request = httpx.Request("POST", "https://api.brightdata.com/request")
        response = httpx.Response(status_code=500, request=request)
        mock_search = AsyncMock(
            side_effect=httpx.HTTPStatusError(
                "boom", request=request, response=response
            )
        )
        mocker.patch("tree.mcp.tools.web_search", mock_search)
        ctx = _make_ctx()

        # Act
        raw = await _get_tool_callable()("anything", ctx)

        # Assert
        payload = json.loads(raw)
        assert payload["error"] == "http_error"
        assert "500" in payload["detail"]

    @pytest.mark.parametrize(
        "exc",
        [
            httpx.ConnectError("connection refused"),
            httpx.TimeoutException("timed out"),
        ],
        ids=["connect-error", "timeout"],
    )
    async def test_network_errors_are_serialized(self, mocker, exc: Exception) -> None:
        # Arrange
        mock_search = AsyncMock(side_effect=exc)
        mocker.patch("tree.mcp.tools.web_search", mock_search)
        ctx = _make_ctx()

        # Act
        raw = await _get_tool_callable()("anything", ctx)

        # Assert
        payload = json.loads(raw)
        assert payload["error"] == "network_error"
        assert payload["detail"]


# ---------------------------------------------------------------------------
# MCP tool tests — optional ingestion path (`ingest=True`).
# ---------------------------------------------------------------------------


class TestSearchWebIngestPath:
    """Cover the opt-in ingest path added in #008."""

    async def test_default_does_not_emit_ingest_field_or_call_trigger(
        self, mocker
    ) -> None:
        # Arrange
        mock_search = AsyncMock(return_value=_sample_results())
        mocker.patch("tree.mcp.tools.web_search", mock_search)
        mock_trigger = mocker.patch(
            "tree.mcp.tools._trigger_url_batch_ingest", new_callable=AsyncMock
        )
        ctx = _make_ctx()

        # Act
        raw = await _get_tool_callable()("k graphs", ctx)

        # Assert
        payload = json.loads(raw)
        assert "ingest" not in payload
        mock_trigger.assert_not_awaited()

    async def test_ingest_true_fires_deployment_with_all_urls(self, mocker) -> None:
        # Arrange
        mock_search = AsyncMock(return_value=_sample_results())
        mocker.patch("tree.mcp.tools.web_search", mock_search)
        mock_trigger = AsyncMock(
            return_value={
                "flow_run_id": "fr-1",
                "tracking_url": "http://127.0.0.1:4200/runs/flow-run/fr-1",
            }
        )
        mocker.patch("tree.mcp.tools._trigger_url_batch_ingest", mock_trigger)
        ctx = _make_ctx()

        # Act
        raw = await _get_tool_callable()("k graphs", ctx, ingest=True)

        # Assert
        payload = json.loads(raw)
        assert payload["ingest"]["triggered"] is True
        assert payload["ingest"]["flow_run_id"] == "fr-1"
        assert payload["ingest"]["tracking_url"].startswith("http")
        assert payload["ingest"]["urls"] == [
            "https://example.com/kg",
            "https://example.com/graphdb",
        ]
        mock_trigger.assert_awaited_once_with(
            ["https://example.com/kg", "https://example.com/graphdb"], _USER_ID
        )

    async def test_ingest_top_k_truncates_to_first_k_urls(self, mocker) -> None:
        # Arrange
        mock_search = AsyncMock(return_value=_sample_results())
        mocker.patch("tree.mcp.tools.web_search", mock_search)
        mock_trigger = AsyncMock(
            return_value={
                "flow_run_id": "fr-2",
                "tracking_url": "http://x/runs/flow-run/fr-2",
            }
        )
        mocker.patch("tree.mcp.tools._trigger_url_batch_ingest", mock_trigger)
        ctx = _make_ctx()

        # Act
        raw = await _get_tool_callable()("k graphs", ctx, ingest=True, ingest_top_k=1)

        # Assert
        payload = json.loads(raw)
        assert payload["ingest"]["urls"] == ["https://example.com/kg"]
        mock_trigger.assert_awaited_once_with(["https://example.com/kg"], _USER_ID)

    async def test_ingest_urls_overrides_ingest_top_k_and_serp(self, mocker) -> None:
        # Arrange
        mock_search = AsyncMock(return_value=_sample_results())
        mocker.patch("tree.mcp.tools.web_search", mock_search)
        mock_trigger = AsyncMock(
            return_value={
                "flow_run_id": "fr-3",
                "tracking_url": "http://x/runs/flow-run/fr-3",
            }
        )
        mocker.patch("tree.mcp.tools._trigger_url_batch_ingest", mock_trigger)
        ctx = _make_ctx()
        custom_urls = ["https://custom-a", "https://custom-b"]

        # Act
        raw = await _get_tool_callable()(
            "k graphs",
            ctx,
            ingest=True,
            ingest_top_k=99,  # Should be ignored.
            ingest_urls=custom_urls,
        )

        # Assert
        payload = json.loads(raw)
        assert payload["ingest"]["urls"] == custom_urls
        mock_trigger.assert_awaited_once_with(custom_urls, _USER_ID)

    async def test_ingest_false_with_top_k_returns_invalid_input(self, mocker) -> None:
        # Arrange
        mock_search = AsyncMock(return_value=_sample_results())
        mocker.patch("tree.mcp.tools.web_search", mock_search)
        mock_trigger = mocker.patch(
            "tree.mcp.tools._trigger_url_batch_ingest", new_callable=AsyncMock
        )
        ctx = _make_ctx()

        # Act
        raw = await _get_tool_callable()("k graphs", ctx, ingest=False, ingest_top_k=3)

        # Assert
        payload = json.loads(raw)
        assert payload["error"] == "invalid_input"
        assert "ingest=false" in payload["detail"].lower()
        mock_trigger.assert_not_awaited()
        # SERP call should also be skipped — fail fast on misuse.
        mock_search.assert_not_awaited()

    async def test_ingest_false_with_urls_returns_invalid_input(self, mocker) -> None:
        # Arrange
        mock_trigger = mocker.patch(
            "tree.mcp.tools._trigger_url_batch_ingest", new_callable=AsyncMock
        )
        ctx = _make_ctx()

        # Act
        raw = await _get_tool_callable()(
            "k graphs", ctx, ingest=False, ingest_urls=["https://a"]
        )

        # Assert
        payload = json.loads(raw)
        assert payload["error"] == "invalid_input"
        mock_trigger.assert_not_awaited()

    async def test_ingest_true_with_explicit_empty_urls_returns_invalid_input(
        self, mocker
    ) -> None:
        # Arrange
        mock_trigger = mocker.patch(
            "tree.mcp.tools._trigger_url_batch_ingest", new_callable=AsyncMock
        )
        ctx = _make_ctx()

        # Act
        raw = await _get_tool_callable()("k graphs", ctx, ingest=True, ingest_urls=[])

        # Assert
        payload = json.loads(raw)
        assert payload["error"] == "invalid_input"
        assert "ingest_urls is empty" in payload["detail"]
        mock_trigger.assert_not_awaited()

    async def test_ingest_true_with_empty_serp_results_does_not_fire(
        self, mocker
    ) -> None:
        """Empty SERP + ingest=True → search succeeds, ingest is a no-op."""

        # Arrange
        mock_search = AsyncMock(return_value=[])
        mocker.patch("tree.mcp.tools.web_search", mock_search)
        mock_trigger = mocker.patch(
            "tree.mcp.tools._trigger_url_batch_ingest", new_callable=AsyncMock
        )
        ctx = _make_ctx()

        # Act
        raw = await _get_tool_callable()("zzz", ctx, ingest=True)

        # Assert
        payload = json.loads(raw)
        assert payload["results"] == []
        assert payload["ingest"]["triggered"] is False
        assert payload["ingest"]["urls"] == []
        # Detail should NOT pretend SERP failed when SERP wasn't even
        # consulted with this code path; the wording is generic.
        assert "empty SERP" not in payload["ingest"]["detail"]
        mock_trigger.assert_not_awaited()

    @pytest.mark.parametrize("bad_top_k", [0, -1, -5])
    async def test_ingest_top_k_below_one_returns_invalid_input(
        self, mocker, bad_top_k: int
    ) -> None:
        """``ingest_top_k <= 0`` is a user error; reject before SERP call."""

        # Arrange
        mock_search = AsyncMock(return_value=_sample_results())
        mocker.patch("tree.mcp.tools.web_search", mock_search)
        mock_trigger = mocker.patch(
            "tree.mcp.tools._trigger_url_batch_ingest", new_callable=AsyncMock
        )
        ctx = _make_ctx()

        # Act
        raw = await _get_tool_callable()(
            "k graphs", ctx, ingest=True, ingest_top_k=bad_top_k
        )

        # Assert
        payload = json.loads(raw)
        assert payload["error"] == "invalid_input"
        assert "ingest_top_k" in payload["detail"]
        # Don't burn a SERP credit on a misuse.
        mock_search.assert_not_awaited()
        mock_trigger.assert_not_awaited()

    async def test_trigger_failure_degrades_to_search_only_payload(
        self, mocker
    ) -> None:
        """If Prefect lookup raises, return SERP results with `triggered=false`."""

        # Arrange
        mock_search = AsyncMock(return_value=_sample_results())
        mocker.patch("tree.mcp.tools.web_search", mock_search)
        mocker.patch(
            "tree.mcp.tools._trigger_url_batch_ingest",
            AsyncMock(side_effect=RuntimeError("deployment not found")),
        )
        ctx = _make_ctx()

        # Act
        raw = await _get_tool_callable()("k graphs", ctx, ingest=True)

        # Assert
        payload = json.loads(raw)
        assert len(payload["results"]) == 2  # SERP results preserved.
        assert payload["ingest"]["triggered"] is False
        assert "deployment not found" in payload["ingest"]["error"]
        assert "flow_run_id" not in payload["ingest"]


# ---------------------------------------------------------------------------
# CLI tests — exercise scripts/search_web.py wiring with the SERP call mocked.
# ---------------------------------------------------------------------------


@pytest.fixture
def cli_main():
    """Import the CLI command lazily so mocks above don't bleed into module load."""

    import scripts.search_web as cli_module

    return cli_module.main


class TestSearchWebCli:
    def test_help_lists_all_options(self, cli_main) -> None:
        # Arrange
        runner = CliRunner()

        # Act
        result = runner.invoke(cli_main, ["--help"])

        # Assert
        assert result.exit_code == 0
        for opt in (
            "--query",
            "--engine",
            "--num-results",
            "--country",
            "--language",
            "--ingest",
            "--ingest-top-k",
            "--ingest-urls",
        ):
            assert opt in result.output

    def test_runs_search_and_exits_zero_on_success(self, mocker, cli_main) -> None:
        # Arrange
        mock_search = AsyncMock(return_value=_sample_results())
        mocker.patch("scripts.search_web.web_search", mock_search)
        runner = CliRunner()

        # Act
        result = runner.invoke(
            cli_main,
            ["--query", "knowledge graphs", "--num-results", "5"],
        )

        # Assert
        assert result.exit_code == 0, result.output
        mock_search.assert_awaited_once()
        kwargs = mock_search.await_args.kwargs
        assert kwargs["engine"] == "google"
        assert kwargs["num_results"] == 5
        assert kwargs["country"] is None
        assert kwargs["language"] is None

    def test_passes_locale_options(self, mocker, cli_main) -> None:
        # Arrange
        mock_search = AsyncMock(return_value=[])
        mocker.patch("scripts.search_web.web_search", mock_search)
        runner = CliRunner()

        # Act
        result = runner.invoke(
            cli_main,
            [
                "--query",
                "datenschutz",
                "--engine",
                "google",
                "--country",
                "de",
                "--language",
                "de",
            ],
        )

        # Assert
        assert result.exit_code == 0, result.output
        kwargs = mock_search.await_args.kwargs
        assert kwargs["country"] == "de"
        assert kwargs["language"] == "de"

    def test_empty_query_exits_one(self, mocker, cli_main) -> None:
        # Arrange — let web_search raise the ValueError that the real client raises.
        mock_search = AsyncMock(side_effect=ValueError("query must not be empty"))
        mocker.patch("scripts.search_web.web_search", mock_search)
        runner = CliRunner()

        # Act
        result = runner.invoke(cli_main, ["--query", ""])

        # Assert
        assert result.exit_code == 1

    def test_configuration_error_exits_one(self, mocker, cli_main) -> None:
        # Arrange
        mock_search = AsyncMock(
            side_effect=BrightDataConfigurationError("BRIGHTDATA_SERP_ZONE is not set")
        )
        mocker.patch("scripts.search_web.web_search", mock_search)
        runner = CliRunner()

        # Act
        result = runner.invoke(cli_main, ["--query", "anything"])

        # Assert
        assert result.exit_code == 1

    def test_request_error_exits_one(self, mocker, cli_main) -> None:
        # Arrange
        mock_search = AsyncMock(
            side_effect=BrightDataRequestError("HTTP 503: upstream")
        )
        mocker.patch("scripts.search_web.web_search", mock_search)
        runner = CliRunner()

        # Act
        result = runner.invoke(cli_main, ["--query", "anything"])

        # Assert
        assert result.exit_code == 1

    def test_missing_query_argument_exits_nonzero(self, cli_main) -> None:
        # Arrange — Click rejects missing required option with exit code 2.
        runner = CliRunner()

        # Act
        result = runner.invoke(cli_main, [])

        # Assert
        assert result.exit_code != 0

    def test_ingest_flag_fires_trigger_with_top_k(self, mocker, cli_main) -> None:
        # Arrange
        mock_search = AsyncMock(return_value=_sample_results())
        mocker.patch("scripts.search_web.web_search", mock_search)
        mock_trigger = AsyncMock(
            return_value={
                "flow_run_id": "fr-cli",
                "tracking_url": "http://127.0.0.1:4200/runs/flow-run/fr-cli",
            }
        )
        mocker.patch("scripts.search_web.trigger_url_batch_ingest", mock_trigger)
        runner = CliRunner()

        # Act
        result = runner.invoke(
            cli_main,
            [
                "--query",
                "k graphs",
                "--ingest",
                "--ingest-top-k",
                "1",
                "--user-id",
                str(_USER_ID),
            ],
        )

        # Assert
        assert result.exit_code == 0, result.output
        mock_trigger.assert_awaited_once_with(["https://example.com/kg"], _USER_ID)

    def test_ingest_urls_overrides_top_k(self, mocker, cli_main) -> None:
        # Arrange
        mock_search = AsyncMock(return_value=_sample_results())
        mocker.patch("scripts.search_web.web_search", mock_search)
        mock_trigger = AsyncMock(
            return_value={
                "flow_run_id": "fr-cli",
                "tracking_url": "http://x/runs/flow-run/fr-cli",
            }
        )
        mocker.patch("scripts.search_web.trigger_url_batch_ingest", mock_trigger)
        runner = CliRunner()

        # Act
        result = runner.invoke(
            cli_main,
            [
                "--query",
                "k graphs",
                "--ingest",
                "--ingest-top-k",
                "99",
                "--ingest-urls",
                "https://a, https://b",
                "--user-id",
                str(_USER_ID),
            ],
        )

        # Assert
        assert result.exit_code == 0, result.output
        mock_trigger.assert_awaited_once_with(["https://a", "https://b"], _USER_ID)

    def test_ingest_top_k_without_ingest_flag_exits_one(self, mocker, cli_main) -> None:
        # Arrange — search must NOT be called when validation fails.
        mock_search = AsyncMock(return_value=_sample_results())
        mocker.patch("scripts.search_web.web_search", mock_search)
        mock_trigger = mocker.patch(
            "scripts.search_web.trigger_url_batch_ingest", new_callable=AsyncMock
        )
        runner = CliRunner()

        # Act
        result = runner.invoke(
            cli_main,
            ["--query", "k graphs", "--ingest-top-k", "1"],
        )

        # Assert
        assert result.exit_code == 1
        mock_trigger.assert_not_awaited()
        mock_search.assert_not_awaited()

    @pytest.mark.parametrize("bad_top_k", ["0", "-1"])
    def test_ingest_top_k_below_one_exits_one(
        self, mocker, cli_main, bad_top_k: str
    ) -> None:
        """``--ingest-top-k 0`` (or negative) is a user error."""

        # Arrange — search/trigger must not run when validation fails.
        mock_search = AsyncMock(return_value=_sample_results())
        mocker.patch("scripts.search_web.web_search", mock_search)
        mock_trigger = mocker.patch(
            "scripts.search_web.trigger_url_batch_ingest", new_callable=AsyncMock
        )
        runner = CliRunner()

        # Act
        result = runner.invoke(
            cli_main,
            ["--query", "k graphs", "--ingest", "--ingest-top-k", bad_top_k],
        )

        # Assert
        assert result.exit_code == 1
        mock_trigger.assert_not_awaited()
        mock_search.assert_not_awaited()

    def test_ingest_trigger_failure_still_exits_zero(self, mocker, cli_main) -> None:
        """Search succeeded; ingestion is best-effort — never fail the CLI for it."""

        # Arrange
        mock_search = AsyncMock(return_value=_sample_results())
        mocker.patch("scripts.search_web.web_search", mock_search)
        mocker.patch(
            "scripts.search_web.trigger_url_batch_ingest",
            AsyncMock(side_effect=RuntimeError("workflows not served")),
        )
        runner = CliRunner()

        # Act
        result = runner.invoke(
            cli_main,
            ["--query", "k graphs", "--ingest", "--user-id", str(_USER_ID)],
        )

        # Assert
        assert result.exit_code == 0, result.output
