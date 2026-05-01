"""Integration tests for the ``search_web`` MCP tool.

Exercises the tool end-to-end against the live Bright Data SERP API and the
real MongoDB. Gated on real ``BRIGHTDATA_API_KEY`` / ``BRIGHTDATA_SERP_ZONE``;
the whole module is skipped when those are missing or set to the .env.example
placeholder.

The default-path test (``ingest=False``) verifies the headline contract: a
search must NOT pollute memory. We assert ``documents`` count is unchanged
across the call.
"""

from __future__ import annotations

import json
import os

import pytest

from tree.mcp.tools import search_web

_PLACEHOLDER_VALUES = {"", "your-brightdata-serp-zone", "your-brightdata-api-key"}


def _is_real(value: str | None) -> bool:
    return bool(value) and value not in _PLACEHOLDER_VALUES


_API_KEY = os.environ.get("BRIGHTDATA_API_KEY")
_SERP_ZONE = os.environ.get("BRIGHTDATA_SERP_ZONE")
_LIVE_CREDS_REASON = (
    "BRIGHTDATA_API_KEY / BRIGHTDATA_SERP_ZONE not configured (or set to placeholder)"
)
_skip_without_serp_creds = pytest.mark.skipif(
    not (_is_real(_API_KEY) and _is_real(_SERP_ZONE)),
    reason=_LIVE_CREDS_REASON,
)


def _get_tool_callable():
    """Return the underlying coroutine for the registered ``search_web`` tool.

    FastMCP wraps the function in a ``FunctionTool``; the original coroutine is
    available as the ``.fn`` attribute on the wrapped object.
    """

    return getattr(search_web, "fn", search_web)


class TestSearchWebToolRegistration:
    """Registration checks — no live SERP creds required."""

    async def test_search_web_is_registered_on_mcp(self) -> None:
        """``search_web`` must be discoverable through the FastMCP tool registry.

        This is the same lookup path FastMCP follows when an MCP client lists
        tools over the wire, so it's a faithful end-to-end registration check.
        """

        from fastmcp.tools.function_tool import FunctionTool

        from tree.mcp.server import mcp

        # Server-name sanity: the lifespan-bearing instance is what the harness
        # / Claude Desktop spawn.
        assert mcp.name == "Tree Memory"

        tool = await mcp.get_tool("search_web")
        assert isinstance(tool, FunctionTool), (
            f"search_web is registered but not as a FunctionTool: {type(tool).__name__}"
        )
        # The original coroutine is exposed as `.fn` — same accessor used in
        # the unit tests and elsewhere.
        assert callable(tool.fn)


@_skip_without_serp_creds
class TestSearchWebToolDoesNotPolluteMemory:
    async def test_default_call_does_not_change_documents_count(
        self, make_mcp_ctx, mongo_client
    ) -> None:
        """``search_web`` (default ingest=False) leaves ``documents`` untouched.

        This is the headline contract for the feature: pure search must not
        write to memory. Verified by counting ``documents`` rows before and
        after the call.
        """

        documents_col = mongo_client["integration_tests_twin"]["documents"]
        before = await documents_col.count_documents({})

        ctx = make_mcp_ctx()
        raw = await _get_tool_callable()(
            "openai gpt-4",
            ctx,
            engine="google",
            num_results=3,
        )

        after = await documents_col.count_documents({})

        # Shape: search succeeded.
        payload = json.loads(raw)
        assert payload["query"] == "openai gpt-4"
        assert payload["engine"] == "google"
        assert isinstance(payload["results"], list)
        # The ``ingest`` block must be absent on the default path.
        assert "ingest" not in payload, (
            "default search_web call leaked an ingest block into the response"
        )

        # Headline assertion: memory is untouched.
        assert after == before, (
            f"documents count changed across a default search_web call: "
            f"{before} -> {after}"
        )
