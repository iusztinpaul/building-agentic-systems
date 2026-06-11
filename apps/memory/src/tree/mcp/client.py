"""FastMCP client for the cloud-hosted ``tree-memory`` MCP server.

The server is deployed to Prefect Horizon (FastMCP Cloud) and reachable at
``settings.tree_memory_cloud_url`` (``https://<name>.fastmcp.app/mcp``).
Horizon secures the endpoint; programmatic clients authenticate with the
``PREFECT_HORIZON_API_KEY`` bearer token. The interactive snippet on the
Horizon dashboard uses OAuth instead — this helper is the headless path.

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

    Reads the endpoint and bearer token from :data:`tree.config.settings`.
    A plain-string ``auth`` is sent by FastMCP as an ``Authorization: Bearer``
    header. The returned client is an async context manager — open it with
    ``async with`` before calling tools.

    Raises:
        RuntimeError: if ``PREFECT_HORIZON_API_KEY`` is unset (placeholder
            local config), so failures surface as a clear config error rather
            than an opaque 401 from the cloud.
    """

    token = settings.prefect_horizon_api_key.get_secret_value()
    if not token:
        raise RuntimeError(
            "PREFECT_HORIZON_API_KEY is not set — required to authenticate "
            "against the cloud tree-memory MCP server. Set it in .env (grab "
            "the value from the Prefect Horizon dashboard's Connect tab)."
        )

    return Client(settings.tree_memory_cloud_url, auth=token)
