"""Entry point for the Twin Memory MCP server."""

from twin.logging import init_logger

init_logger()

from twin.mcp.server import mcp  # noqa: E402

if __name__ == "__main__":
    mcp.run()
