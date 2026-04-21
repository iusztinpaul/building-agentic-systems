# Milestone 5 — MCP Client + Tree Memory

## Goal

Stdio MCP client reads root `.mcp.json`, adapter registers MCP tools under `mcp__<server>__<tool>` namespacing. First end-to-end demo: the harness calls Tree memory's FastMCP tools.

## Depends on

- M2 (tool registry).
- A populated `tree` MongoDB via `make memory-run-arxiv-data-pipeline` → `...-extraction` → `...-indexing`.

## Files

New:
- `src/mcp/config.ts` — load root `.mcp.json`; resolve `ENV_FILE_PATH` relative to server cwd. ~30 lines.
- `src/mcp/client.ts` — spawn stdio transport, connect MCP SDK client, list tools. ~80 lines.
- `src/mcp/adapter.ts` — wrap each MCP tool as a harness `Tool`; namespace as `mcp__<server>__<tool>`. ~60 lines.

Modified:
- `src/index.ts` / `src/app.tsx` — boot MCP clients at startup, merge into tool registry.
- `src/agent/loop.ts` — pass `mcpClients` through `ToolContext` so sub-agents (M6) can share connections.

## New dependencies

- `@modelcontextprotocol/sdk`.

## LOC budget

+~300 (cumulative ~1500).

## Verification

1. `make local-start` + populate `tree` DB via memory pipelines.
2. `make harness-dev` — harness lists `mcp__tree-memory__*` tools in its registry on startup.
3. "What's in the knowledge graph?" → `mcp__tree-memory__search_memory` fires and returns hits.
4. "Ingest this conversation" → `mcp__tree-memory__ingest_conversation` fires.
5. Kill the MCP server process → harness surfaces a clean error rather than hanging.

## Out of scope

- Sub-agent sharing of MCP handles — plumbing lands here but proven at M6.
- Multiple concurrent MCP servers — code path handles the list, but only `tree-memory` is registered.
