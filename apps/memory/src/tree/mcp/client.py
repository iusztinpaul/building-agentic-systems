"""FastMCP client for the cloud-hosted ``tree-memory`` MCP server.

The server is deployed to Prefect Horizon (FastMCP Cloud) and reachable at
``settings.tree_memory_cloud_url`` (``https://<name>.fastmcp.app/mcp``).
Horizon protects it with **Horizon Authentication** (OAuth + organization
membership): clients must be logged into Horizon and a member of the org.
So this helper uses the OAuth flow — ``auth="oauth"`` opens the default
browser on first use, captures the callback, and caches the token for
subsequent calls. There is no static API key for client connections.

Usage::

    from tree.mcp.client import get_cloud_client

    async with get_cloud_client() as client:
        tools = await client.list_tools()
        result = await client.call_tool("query_graph", {"query": "..."})
"""

from __future__ import annotations

from fastmcp import Client

from tree.config.settings import settings


def get_cloud_client() -> Client:
    """Build a :class:`fastmcp.Client` for the cloud ``tree-memory`` server.

    Targets ``settings.tree_memory_cloud_url`` and authenticates with the
    Horizon OAuth flow (``auth="oauth"``): the first call opens a browser to
    log into Horizon, then the token is cached for later calls. The returned
    client is an async context manager — open it with ``async with`` before
    calling tools.

    This is the interactive path. A headless/server-side caller would need
    Horizon's optional *delegated authentication* (a pre-registered OAuth
    provider or per-user API key) instead of the browser flow.
    """

    return Client(settings.tree_memory_cloud_url, auth="oauth")
