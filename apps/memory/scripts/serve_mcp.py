"""Entry point for the Tree Memory MCP server.

The server pins no user: each call resolves its **Request user** — over stdio
from ``TREE_USER_IDENTIFIER`` in the process env, over HTTP from the
``horizon-actor-email`` header (``tree.mcp.request_user``, ADR-014).

Usage:
    make memory-serve-mcp
    make memory-serve-mcp TRANSPORT=streamable-http
    uv run python scripts/serve_mcp.py --transport http
"""

from __future__ import annotations

import click

from tree.logging import init_logger

init_logger()


@click.command()
@click.option(
    "--transport",
    type=click.Choice(["stdio", "http", "sse", "streamable-http"]),
    default=None,
    help="Transport protocol to use. Defaults to FastMCP's stdio.",
)
def main(transport: str | None) -> None:
    """Start the MCP server."""

    # Imported after ``init_logger()`` so the server's import-time logs land
    # in the configured handlers.
    from tree.mcp.server import mcp

    if transport:
        mcp.run(transport=transport)
    else:
        mcp.run()


if __name__ == "__main__":
    main()
