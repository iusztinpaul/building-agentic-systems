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
from click.testing import CliRunner

from tree.data.web import SearchResult
from tree.data.web.web_unlocker import (
    BrightDataConfigurationError,
    BrightDataRequestError,
)


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
    """Build a mock FastMCP Context. The tool body must not touch lifespan_context."""

    return MagicMock()


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
        # Spy on the ingestion pipeline — search_web must NEVER call it.
        mock_ingestion = mocker.patch(
            "tree.mcp.tools.run_ingestion_pipeline", new_callable=AsyncMock
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
        for opt in ("--query", "--engine", "--num-results", "--country", "--language"):
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
