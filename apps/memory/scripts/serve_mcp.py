"""Entry point for the Twin Memory MCP server."""

from tree.logging import init_logger

init_logger()

from tree.mcp.server import mcp  # noqa: E402

if __name__ == "__main__":
    mcp.run()
