---
id: 208-harness-mcp-client-jsonrpc-stdio-http
status: pending
feature: tree-agent-harness
---

# M5a — the hand-rolled MCP client: `mcp.json` schema (user + project, merged), JSON-RPC 2.0 framing, stdio and streamable-HTTP transports, the `initialize` → `tools/list` → `tools/call` handshake, `--no-mcp`

Tags: `harness`, `mcp`, `enhancement`
Depends on: 203 (`sse.ts` is reused for SSE response bodies), 204 (trust gates the project `mcp.json`)
Blocks: 209
Implements: ADR-016 §3 (hand-rolled client; `.tree-agent/mcp.json` as the only MCP source)

## Scope

1. **`src/mcp/config.ts`** — spec §10.1 zod schema verbatim (stdio `{command, args, env}` | HTTP
   `{url, headers}`, each with `toolExposure: Record<glob, Exposure>`); sources: `~/.tree-agent/mcp.json`
   then `.tree-agent/mcp.json` (project, trust-gated), merged by server name with the project winning; the
   repo-root `.mcp.json` (Claude Code's format) is never read. Validation errors name the server and the key.
2. **`src/mcp/jsonrpc.ts`** — request ids, the pending map, result/error correlation, notifications; a
   server→client request (sampling, roots, …) is answered `-32601` and ignored; a per-call timeout of 60 s.
3. **`src/mcp/transport-stdio.ts`** — spawn `command args` with `env` merged over `process.env`, one JSON
   object per line on stdin/stdout, stderr forwarded to the harness logger (never to the user's stdout),
   child killed on `close()`/process exit.
4. **`src/mcp/transport-http.ts`** — `POST url` with `Accept: application/json, text/event-stream` and the
   configured `headers`; a JSON body is one response; an SSE body is read with `provider/sse.ts`;
   `Mcp-Session-Id` is stored when returned and echoed on every later request.
5. **`src/mcp/client.ts`** — `initialize` (protocol `2025-06-18`, capabilities `{}`), `notifications/initialized`,
   `listTools()` following `nextCursor`, `callTool(name, args)`; connect lazily: list tools at startup,
   reconnect on first call if the transport dropped; `resources`/`prompts` never requested.
6. **`--no-mcp`** skips the module entirely (no config read, no spawn).
7. **Test fixtures** — `tests/fixtures/fake-mcp-server.mjs` (a stdio server in plain Node: two tools, one
   with `annotations.readOnlyHint: true`, `tools/list` paginated over two pages, `tools/call` echoing args, one
   tool answering `structuredContent`) and an in-process `http.createServer` fake answering JSON on one route
   and SSE on another, with and without `Mcp-Session-Id`.

**Out of scope (intentional):** registering tools into the registry, exposures, `tool_search` (209); OAuth,
the legacy SSE transport, MCP resources/prompts, server→client requests (spec non-goals); reading `.mcp.json`
(ADR-016 §3 — the two files have different schemas and ownership).

## Acceptance Criteria

- [ ] The config union parses both transports; a server with both `command` and `url`, an unknown
      `toolExposure` value, or a non-URL `url` fails with a message naming the server; user and project files
      merge by name with the project entry winning; `.mcp.json` at the repo root is not opened (spy on `fs`).
- [ ] stdio: the handshake against `fake-mcp-server.mjs` completes (`initialize` → `notifications/initialized`
      → `tools/list`), two pages are followed via `nextCursor` and both tools are listed; `callTool` returns the
      echoed args; the child's stderr lands in the logger and not on stdout; `close()` ends the child.
- [ ] HTTP: a JSON response and an SSE response both resolve `callTool`; a returned `Mcp-Session-Id` is sent
      on the next request; configured `headers` are present on every POST; the `Accept` header lists both types.
- [ ] A server→client request is answered with JSON-RPC `-32601` and does not reject any pending call; a call
      with no answer rejects after the 60 s timeout (fake timers); a dropped stdio transport reconnects on the
      next `callTool`.
- [ ] `--no-mcp` → no config file read, no child process spawned.
- [ ] `make harness-lint-fix && make harness-lint-check && make harness-typecheck && make harness-tests &&
      make pre-commit` green.

## User Stories

### Story: The client connects to a stdio server at startup
1. `.tree-agent/mcp.json` names a stdio server; the directory is trusted.
2. On start the client spawns it, completes the handshake, lists its tools; the child's log lines go to the
   harness log, the transcript stays clean.

### Story: A streamable-HTTP server with a session id
1. `~/.tree-agent/mcp.json` names `{url, headers: {Authorization: "Bearer …"}}`.
2. The first POST gets `Mcp-Session-Id: abc`; the next `tools/call` carries it; an SSE-framed answer is parsed
   with the same SSE code the provider uses.

### Story: A server pages its tool list
1. `tools/list` answers 50 tools and `nextCursor: "p2"`.
2. The client requests page 2 and merges; the final list has every tool once.

### Story: The server crashes mid-session
1. The stdio child exits after the first call.
2. The next `callTool` respawns and re-initialises transparently; the call succeeds.

### Story: A mis-typed config
1. A server entry has `"url": "tree-memory.fastmcp.app/mcp"` (no scheme).
2. Startup prints `mcp.json: server "tree-memory": url must be a valid URL` and exits 1.

---

Blocked by: 204

## Log

### [PA] 2026-10-24 20:14 — Grooming

**Summary**
A ~300-line MCP client without the SDK: config schema with glob exposures, JSON-RPC framing, stdio and
streamable-HTTP transports (the latter reusing the provider's SSE parser), the three-call handshake with
pagination and lazy reconnect, and the fixtures later tasks test against.

**Key decisions**
- `.tree-agent/mcp.json` only; the Claude Code `.mcp.json` is never parsed (different schema, different
  owner — ADR-016 §3).
- Registration is split into 209 so this task's tests need no registry or prompt.
- Fixtures are plain Node scripts, no MCP SDK in dev deps either.

**Dependencies**
- 203 — `sse.ts`; 204 — trust gating of the project file.

**User stories**
- 5 stories covering: stdio startup, HTTP with a session id, pagination, a crashed child, a bad URL.

Ready for implementation.
