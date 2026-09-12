"""MCP layer — the ONLY harness↔memory boundary (ADR-008 §5).

Deliberately EMPTY: re-exporting ``mcp`` here would import ``tree.mcp.server``
(and Mongo, Prefect and Opik with it) for every importer of any submodule —
including the pure-client **Session-end hook** (``tree.mcp.hooks``), which must
stay stdlib + ``fastmcp`` + ``pydantic`` at runtime, not just in its own source.
Import the server explicitly: ``from tree.mcp.server import mcp``.
"""
