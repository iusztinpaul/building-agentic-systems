"""Integration tests for the ``scrape_web`` MCP tool.

Exercises the tool end-to-end against the live Bright Data Web Unlocker and
the real MongoDB. Gated on real ``BRIGHTDATA_API_KEY`` /
``BRIGHTDATA_UNLOCKER_ZONE`` (the *Unlocker* zone, distinct from the SERP
zone used by ``search_web``); the live class is skipped when those are
missing or set to the .env.example placeholder.

The headline contract: ``scrape_web`` must NOT pollute memory. We assert the
``documents`` count is unchanged across the call.
"""

from __future__ import annotations

import json
import os

import pytest

from tree.mcp.tools import scrape_web

_PLACEHOLDER_VALUES = {
    "",
    "your-brightdata-unlocker-zone",
    "your-brightdata-api-key",
}


def _is_real(value: str | None) -> bool:
    return bool(value) and value not in _PLACEHOLDER_VALUES


_API_KEY = os.environ.get("BRIGHTDATA_API_KEY")
_UNLOCKER_ZONE = os.environ.get("BRIGHTDATA_UNLOCKER_ZONE")
_LIVE_CREDS_REASON = (
    "BRIGHTDATA_API_KEY / BRIGHTDATA_UNLOCKER_ZONE not configured "
    "(or set to placeholder)"
)
_skip_without_unlocker_creds = pytest.mark.skipif(
    not (_is_real(_API_KEY) and _is_real(_UNLOCKER_ZONE)),
    reason=_LIVE_CREDS_REASON,
)


def _get_tool_callable():
    """Return the underlying coroutine for the registered ``scrape_web`` tool.

    FastMCP wraps the function in a ``FunctionTool``; the original coroutine is
    available as the ``.fn`` attribute on the wrapped object.
    """

    return getattr(scrape_web, "fn", scrape_web)


class TestScrapeWebToolRegistration:
    """Registration checks — no live Unlocker creds required."""

    async def test_scrape_web_is_registered_on_mcp(self) -> None:
        """``scrape_web`` must be discoverable through the FastMCP tool registry.

        Same lookup path FastMCP follows when an MCP client lists tools over
        the wire — a faithful end-to-end registration check.
        """

        from fastmcp.tools.function_tool import FunctionTool

        from tree.mcp.server import mcp

        assert mcp.name == "Tree Memory"

        tool = await mcp.get_tool("scrape_web")
        assert isinstance(tool, FunctionTool), (
            f"scrape_web is registered but not as a FunctionTool: {type(tool).__name__}"
        )
        assert callable(tool.fn)


@_skip_without_unlocker_creds
class TestLiveScrapeWeb:
    async def test_batch_scrape_returns_content_and_does_not_pollute_memory(
        self, make_mcp_ctx, mongo_client
    ) -> None:
        """Two stable URLs scrape successfully; ``documents`` count is unchanged.

        Headline contract: ``scrape_web`` must NOT write to MongoDB. Verified
        by counting ``documents`` rows before and after the call. The two URLs
        are IANA-published references with rock-stable content.
        """

        documents_col = mongo_client["integration_tests_twin"]["documents"]
        before = await documents_col.count_documents({})

        urls = [
            "https://example.com",
            "https://www.iana.org/help/example-domains",
        ]

        ctx = make_mcp_ctx()
        raw = await _get_tool_callable()(urls, ctx)

        after = await documents_col.count_documents({})

        payload = json.loads(raw)
        assert payload["requested"] == 2
        assert payload["succeeded"] >= 1
        assert payload["failed"] == payload["requested"] - payload["succeeded"]
        assert isinstance(payload["results"], list)
        assert len(payload["results"]) == 2

        # Order matches input order — important for the agent's mental model
        # of SERP→scrape flow.
        assert [r["url"] for r in payload["results"]] == urls

        # Each successful result has populated content with the requested
        # data_format.
        successful = [r for r in payload["results"] if r["success"]]
        assert successful, "expected at least one successful scrape"
        for result in successful:
            assert isinstance(result["content"], str) and result["content"], (
                f"successful scrape returned empty content: {result['url']}"
            )
            assert result["length"] > 0
            assert result["data_format"] == "markdown"
            assert result["error"] is None
            assert result["error_type"] is None

        # Headline assertion: memory is untouched.
        assert after == before, (
            f"documents count changed across a default scrape_web call: "
            f"{before} -> {after}"
        )
