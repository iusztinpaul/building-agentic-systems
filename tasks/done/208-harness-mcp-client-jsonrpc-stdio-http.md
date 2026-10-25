---
id: 208-harness-mcp-client-jsonrpc-stdio-http
status: done
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

- [x] The config union parses both transports; a server with both `command` and `url`, an unknown
      `toolExposure` value, or a non-URL `url` fails with a message naming the server; user and project files
      merge by name with the project entry winning; `.mcp.json` at the repo root is not opened (spy on `fs`).
- [x] stdio: the handshake against `fake-mcp-server.mjs` completes (`initialize` → `notifications/initialized`
      → `tools/list`), two pages are followed via `nextCursor` and both tools are listed; `callTool` returns the
      echoed args; the child's stderr lands in the logger and not on stdout; `close()` ends the child.
- [x] HTTP: a JSON response and an SSE response both resolve `callTool`; a returned `Mcp-Session-Id` is sent
      on the next request; configured `headers` are present on every POST; the `Accept` header lists both types.
- [x] A server→client request is answered with JSON-RPC `-32601` and does not reject any pending call; a call
      with no answer rejects after the 60 s timeout (fake timers); a dropped stdio transport reconnects on the
      next `callTool`.
- [x] `--no-mcp` → no config file read, no child process spawned.
- [x] `make harness-lint-fix && make harness-lint-check && make harness-typecheck && make harness-tests &&
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

### [SWE] 2026-10-25 02:22 — Implementation

**Files added**
- `apps/harness/src/mcp/config.ts` — the spec §10.1 schema as written (`McpServer`, `McpConfig`, `Exposure`; types `StdioServer`/`HttpServer`). Also `loadMcpConfig({env, projectDir?})`, `parseMcpConfig(raw, source)` and `McpConfigError`.
- `apps/harness/src/mcp/jsonrpc.ts` — `JsonRpcPeer` (`request`, `notify`, `receive`, `close`), the `Transport`/`TransportHandlers` interface, `JsonRpcError{code,data}`, `CALL_TIMEOUT_MS = 60_000` and `METHOD_NOT_FOUND = -32601`.
- `apps/harness/src/mcp/transport-stdio.ts` — `StdioTransport` (it exposes `pid` for tests).
- `apps/harness/src/mcp/transport-http.ts` — `HttpTransport` (it exposes `sessionId`, `setProtocolVersion`) and `ACCEPT`.
- `apps/harness/src/mcp/client.ts` — `McpClient(name, server, {cwd, env, timeoutMs, log, fetch})` with `name`, `server`, `connected`, `tools`, `listTools()`, `callTool(name, args)` and `close()`. Types `McpTool {name, description?, inputSchema, annotations?: Record<string, unknown>}` and `McpCallResult {content[], structuredContent?, isError}`. Also `connectMcpServers(servers, opts)`, `startMcp({env, cwd, trusted})` (type `StartMcp`) and `PROTOCOL_VERSION`.
- `apps/harness/tests/fixtures/fake-mcp-server.mjs` — a plain-Node stdio server. It serves `echo` (page 1, `nextCursor: "p2"`) and `read_note` (page 2, with `annotations.readOnlyHint: true` and `structuredContent`). It logs `recv <method>` to stderr and refuses `tools/list` before `notifications/initialized`. Knobs: `FAKE_MCP_MARKER=<file>` appends the pid on each start, `FAKE_MCP_EXIT_AFTER_CALL=1` exits after one call, `echo {hang:true}` never answers, and `echo {probe:true}` sends `sampling/createMessage` first and answers with the client's reply.
- `apps/harness/tests/helpers/mcp-http.ts` — `startFakeMcpHttp({sessionId?})` runs an `http.createServer` on 127.0.0.1, port 0. `/json` answers with a JSON body. `/sse` answers with an SSE stream: a comment, a `notifications/message`, then the response. Optional `Mcp-Session-Id`, `expire()` (makes the session answer 404) and a request recorder.
- Tests: `apps/harness/tests/mcp/{config,jsonrpc,transport-stdio,transport-http,client}.test.ts`.

**Files changed**
- `src/config/paths.ts` — `projectPaths(cwd)` also returns `mcp: <cwd>/.tree-agent/mcp.json`.
- `src/cli.ts` — `--no-mcp` (in USAGE, `CliArgs.mcp`, `parseArgs`) and the `CliDeps.startMcp` seam (the same pattern as `discoverSkills`). Inside `open()`, after `checkDecisionModel`: `startMcp({env, cwd, trusted: projectTrusted})` when MCP is on. `main()` holds the clients and closes all of them in a `finally`. **Nothing is registered** (task 209 passes the clients to the session).
- `tests/cli.test.ts` (`main (mcp)` and the `resolveMode` flag test), `tests/config/paths.test.ts`.
- No new dependency. `package.json` and `pnpm-lock.yaml` are untouched.

**Bindings / decisions the spec leaves open**
- **One deviation from the "verbatim" schema:** with zod's non-strict objects, `{command, url}` parses as stdio, and `url` is dropped without a word. `.strict()` would break the `.and(toolExposure)`. So `parseMcpConfig` checks the raw entry for both keys before zod runs and reports `set command (stdio) or url (HTTP), not both`. The exported schema stays as the spec writes it.
- Error format: `<file>: server "<name>": <key path> <predicate>`, first problem only. The file is the full path (the story's `mcp.json: server "tree-memory": url must be a valid URL` is its suffix). Predicates: `must be a valid URL`, `must be one of direct, deferred, codemode, hidden` (for an unknown string and for a non-string), `is required`, `must be a/an <type>`, `needs command (stdio) or url (HTTP)`, `must be an object`. A union failure is reported from the branch the entry chose: `url` → HTTP, `command` → stdio.
- Merge: the user file, then the project file (trusted cwd only), combined by server name. The project entry **replaces the user entry whole** (no deep merge of `toolExposure`). A missing file counts as `{}`. Invalid JSON or an invalid file → an `McpConfigError` naming the file → exit 1 (story 5).
- Startup: a config error is fatal (exit 1). A server that fails to spawn, handshake or list gives **one** `log.warn` (`mcp "<name>": …`) and is kept, so a later call retries the handshake. One bad server does not stop the run.
- stdio: `cwd` = the session cwd (the 209 config uses a relative `--directory apps/memory`). `env` = `{...deps.env (process.env in prod), ...server.env}`. The transport watches for `close` (not `exit`), so a reply written just before the child exits still reaches its call. The child's stderr goes to `log.info` as `mcp "<name>": <line>` (hidden at the default `warn`, so the TUI screen stays clean). A child that exits on its own gives one `warn` with `server exited (code N)`. A spawn failure is said once (the rejection). `close()` ends stdin, sends SIGTERM after 1 s and SIGKILL after 3 s. One shared `process.once("exit")` hook kills every live child.
- HTTP: headers = the configured ones + `content-type: application/json` + `accept: application/json, text/event-stream`. They are built with `Headers`, so a configured `Accept` is replaced, not doubled. `Mcp-Session-Id` is stored from any response and echoed. **Added beyond the task text:** `MCP-Protocol-Version: <negotiated>` goes on every request after initialize (a MUST in the 2025-06-18 transports page). A 404 while holding a session clears the id and marks the transport dropped, so the next call re-initialises (the spec says start a new session). `close()` aborts in-flight reads and sends `DELETE` with the session id (best effort, 2 s). A 202 or an empty body delivers nothing. Any non-2xx throws `HTTP <status>: <first 200 chars>`. An unreachable server gives `POST <url>: <cause>`. Header values are never logged.
- JSON-RPC: ids are numbers counting up from 1 per connection. A server→client request (`id` + `method`) gets `-32601 Method not found: <method>`. **The exception is `ping`**, which gets `{}` (MCP requires an answer). A server notification is never answered. A non-object frame, a frame without an id, or an unknown or late id gives a `warn` and is ignored. A timeout rejects with `<label><method> timed out after 60 s` and sends `notifications/cancelled {requestId, reason: "timeout"}`. A transport drop or `close()` rejects every pending call at once and clears their timers.
- Client: `initialize {protocolVersion: "2025-06-18", capabilities: {}, clientInfo: {name: "tree-agent", version: "0.1.0"}}`, then `notifications/initialized`. When the server's `protocolVersion` differs, the client warns and continues. `tools/list` sends no `params` on page 1 and `{cursor}` after that. Tools are deduped by name (first occurrence wins), and a repeated cursor stops the loop with a warn. `callTool` returns the raw `{content, structuredContent?, isError}` and does not interpret `isError`. A JSON-RPC error throws `JsonRpcError`. Results are zod-checked (`unexpected <path>: …`). Concurrent calls on a dropped client share one reconnect. `resources/*` and `prompts/*` are never sent.

**Sources consulted (tech-docs skill: context7 first, then the llms.txt index)**
- context7 `/websites/modelcontextprotocol_io_specification_2025-06-18` (searched via the context7 HTTP API; snippet: DELETE / `Mcp-Session-Id` session termination).
- `https://modelcontextprotocol.io/llms.txt` → `specification/2025-06-18/basic/lifecycle.md` (initialize / initialized, version negotiation), `basic/transports.md` (stdio framing and stderr, streamable HTTP `Accept`, 202 for notifications, SSE responses, `Mcp-Session-Id`, 404 → new session, `MCP-Protocol-Version`, DELETE), `server/tools.md` (`tools/list` + `nextCursor`, `tools/call` `content[]` / `structuredContent` / `isError`, annotations are untrusted) and `server/utilities/pagination.md`.

**Tests**
- Unit: 622 passed, 0 failed, 39 files (`make harness-tests`, live OpenRouter tests included). Before: 548 in 34 files. New: config 17, jsonrpc 12, transport-stdio 8, transport-http 14, client 17, cli +5 (+1 assertion), paths +1.
- Integration: N/A (this app has none).
- Mutation probes (each file restored afterwards): no `-32601` answer → 3 fail; no session-id echo → 5 fail; no reconnect → 2 fail; config also reads `<cwd>/.mcp.json` → 1 fail (the fs spy); no pagination → 5 fail; no timeout → 3 fail; `--no-mcp` ignored → 1 fail; user wins over project → 1 fail; child stderr written to stdout → 4 fail.
- After the full run, `pgrep -fl fake-mcp-server` shows nothing (no orphans).

**Acceptance criteria**
- [x] Config — `tests/mcp/config.test.ts`: `McpConfig schema` (both transports), `parseMcpConfig errors…` (`rejects a url without a scheme with the story's message`, `rejects a server with both command and url`, `rejects an unknown toolExposure value, naming the glob`), `loadMcpConfig > merges user then project by server name, the project entry winning whole`, `> never opens the repo-root .mcp.json (fs spy)`. That test `vi.mock`s every lower-case function of `node:fs` and `node:fs/promises` and asserts the only paths touched are the two `.tree-agent`-level `mcp.json` files, with none ending in `.mcp.json`. Also `> only .mcp.json present → no servers`.
- [x] stdio — `tests/mcp/client.test.ts > McpClient over stdio`: `handshakes initialize → notifications/initialized → tools/list ×2 and lists both tools` (order taken from the server's own `recv` log), `callTool returns the echoed args`, `structuredContent`, `child's stderr goes to the logger at info`, `close() ends the child` (pid gone). Also `tests/mcp/transport-stdio.test.ts > forwards the child's stderr to the logger at info, never to stdout` (spy on `process.stdout.write`) and `close() ends the child`.
- [x] HTTP — `tests/mcp/transport-http.test.ts`: `a JSON response is one frame`, `an SSE response is read with the provider's parser`, `every POST has Accept with both types… and the configured headers`, `stores a returned Mcp-Session-Id and sends it on every later request`, `sends no Mcp-Session-Id when the server returns none`. Also `client.test.ts > McpClient over streamable HTTP` (JSON, and SSE with a session, end to end).
- [x] `-32601` / timeout / reconnect — `jsonrpc.test.ts > answers a server→client request -32601 and leaves the pending call alone`, `client.test.ts > answers a server→client request -32601 and the pending call still resolves` (the fake server echoes the client's reply), `jsonrpc.test.ts > rejects a call with no answer after the 60 s timeout…` (fake timers: still pending at 59 999 ms, rejected at 60 000 ms), `client.test.ts > a dropped child is respawned and re-initialised on the next callTool` (2 pids, `initialize` seen twice).
- [x] `--no-mcp` — `cli.test.ts > main (mcp) > --no-mcp: no mcp.json read (an invalid one passes), no child spawned, no startMcp call`. With the flag: exit 0 despite an invalid `~/.tree-agent/mcp.json`, 0 `startMcp` calls, no pid in the marker. Without it: 1 call and exit 1 naming the file.
- [x] Cadence green (evidence below).

**Evidence**
```
$ make harness-lint-fix && make harness-lint-check && make harness-typecheck && make harness-tests && make pre-commit
Checked 87 files … No fixes applied.        (lint-check)
pnpm exec tsc --noEmit                       (clean)
 Test Files  39 passed (39)
      Tests  622 passed (622)
biome check (harness)....Passed  (prettier/ruff Passed)

# Live stdio smoke (make env-status → local; infra up). Scratch cwd, fake HOME and TREE_AGENT_HOME,
# a user mcp.json naming tree-memory-local with ABSOLUTE --directory/--env-file paths and
# MCP_SKIP_INDEX_BOOTSTRAP=1. Driver: …/scratchpad/smoke/driver/{smoke,call}.ts → startMcp()
tree-memory-local connected: true tools: 8
  - search_memory / visualize_memory_structure / visualize_memory_embeddings / ingest_url /
    ingest_file / search_web / scrape_web / ingest_conversation   (annotations: none on any)
serve_mcp pids while open: 2 (uv + python)   after close: 0   elapsed 5.6 s
callTool("search_memory", {query: "AI agents"}) → isError false, 1 content item,
  structuredContent present, 15 468 chars of text
```

**Notes for the Tester / PA**
- The live smoke confirms what 209 expects: the rag-mode server lists 8 tools, and none carries `annotations` (no `readOnlyHint`).
- The clients are opened in `cli.ts` and only closed there, because registration belongs to 209. Every startup with an `mcp.json` now spawns the servers, even though their tools are not yet reachable by the model. The default repo has no `.tree-agent/mcp.json` until 209, so nothing changes for a plain run.
- In the TUI, a startup `warn` (a server failed) goes to stderr like the existing `trust.ts` warns do.
- `CLIENT_INFO.version` is a literal `0.1.0` (same as `package.json`), not read from the package.
- Line count: about 750 code lines over the 5 files. The ~300 in the spec is the teaching target before 76-column formatting and validation. `agent-loop.ts` is untouched (180).
- Things to probe: a server that prints a banner on stdout before its first frame (warned and skipped); a stdio command that ignores stdin EOF (SIGTERM after 1 s); `toolExposure` with a non-string value; an HTTP server that answers `text/event-stream` and never closes after the response (the call resolves on the frame and the stream stays open until `close()`); `--no-mcp` together with the TUI; an `mcp.json` whose `mcpServers` is an array (`mcpServers must be an object`).

### [Tester] 2026-10-25 02:55 — QA

**Test summary**
- Format / lint / typecheck / pre-commit: PASS (`make harness-lint-fix && make harness-lint-check && make harness-typecheck && make harness-tests && make pre-commit`, exit 0; `git status` unchanged by lint-fix)
- Unit tests: 622 passed / 0 failed (39 files, includes live OpenRouter tests). Integration: N/A (app has none).
- Warnings: 0 test-runner warnings (the `[tree-agent] warn:` lines in the log are asserted-on stderr from other tasks' tests)
- Regression: `agent-loop.ts` 180 lines; TUI boundary grep empty; `pnpm install --frozen-lockfile` "Already up to date"; no `.env*`/spec-doc/package/lockfile change; `pgrep -fl "fake-mcp-server|evil.mjs|serve_mcp"` empty after suite and after all my probes.

**E2E adversarial pass** (QA scratch: `scratchpad/qa-208/`, fake HOME + TREE_AGENT_HOME, port-0 servers, children killed)
- Happy path (CLI, live model): user `mcp.json` naming `fake-mcp-server.mjs` → `tree-agent --cwd w1 "Reply with exactly the word ok"` → stdout `ok`, stderr empty, marker = 1 pid, 0 leftover (PASS). `TREE_AGENT_LOG=info` shows `mcp "fake": recv initialize / notifications/initialized / tools/list ×2` on stderr only (PASS).
- Config (`parseMcpConfig`, CLI): both `command`+`url` → `…mcp.json: server "dual": set command (stdio) or url (HTTP), not both` exit 1; unknown / non-string `toolExposure` → `toolExposure.* must be one of direct, deferred, codemode, hidden`; `url: "tree-memory.fastmcp.app/mcp"` → `server "tree-memory": url must be a valid URL` exit 1; `mcpServers` array/null → `mcpServers must be an object`; missing → `mcpServers is required`; top-level array/null → `must be an object`; string / null entry → `server "a": must be an object`; `{}` → `needs command (stdio) or url (HTTP)`; `env:{K:1}` → `env.K must be a string`; `env` array → `env must be an object`; `args` string → `args must be an array`; `headers.A:1` → `headers.A must be a string` (all PASS). `ftp://h/` is accepted (zod `.url()`, spec-verbatim; see notes).
- Merge/trust: user has `toolExposure {"*":"direct"}`, project same server without it → merged result `toolExposure:{}` (project wins whole; PASS). Malformed project JSON + `--trust` → error names `w2/.tree-agent/mcp.json` exit 1; malformed user JSON → names `h2/mcp.json` exit 1; same malformed project file in an UNTRUSTED cwd → ignored, exit 0 (PASS).
- fs spy (`node --import fsspy.mjs`, patched every `node:fs`/`fs/promises` path-taking function, real CLI runs; cwd holds `.tree-agent/mcp.json` AND `.mcp.json`): untrusted → only `h3/mcp.json`; `--trust` → `h3/mcp.json` + `w3/.tree-agent/mcp.json`; `--trust --no-mcp` → no mcp path at all; `TREE_AGENT_HOME` unset, HOME=h4 → `h4/.tree-agent/mcp.json`; `.mcp.json` never touched in any run (PASS). `TREE_AGENT_HOME` override honoured (all h* runs).
- JSON-RPC over stdio (`evil.mjs`): stdout banner before first frame → warn `not JSON on stdout`, handshake OK; unknown id 9999, no-id frame, server notification, malformed line, `[1,2,3]`, blank line interleaved in `initialize`/`tools/list`/`tools/call` → warns only, every call resolves; server `ping` → client replies `{}`; `sampling/createMessage` + `roots/list` → `-32601`, pending call resolves; 1 MB notification line → call resolves (40 ms); error response with `data` → `JsonRpcError{code:-32000,data:{why,n}}`; two JSON objects on one line → warn `not JSON`, call rejects at the per-call timeout (2 s with `timeoutMs`, 60 s default; no hang beyond the timeout) (all PASS, last = documented behaviour).
- stdio lifecycle: child `exit(3)` mid-call → rejects in 60 ms `server exited (code 3)`, `connected` false; next call respawns (verified by 2 spawns) (PASS). `close()` ×2 OK, call after close → `closed` (PASS). Child ignoring stdin close → `close()` 1010 ms; also ignoring SIGTERM → 3010 ms (SIGKILL) (PASS). `env` merged over `process.env` (child sees `QA_EXTRA=yes`, PATH, HOME) and `cwd` option honoured (`/private/tmp`) (PASS). ENOENT command → `cannot start /no/such/bin: spawn … ENOENT` (PASS). CLI with 4 broken servers (ENOENT, refused HTTP, stdout banner, silent `sleep 300`) → exit 0, one warn each, stdout `ok`, no leftover; startup blocked 60 s by the silent server (see notes).
- Signals on a headless run mid-`bash sleep 25` (server ignoring stdin EOF): one SIGINT → exit 1, 0 leftover; SIGTERM → exit 1, 0 leftover; well-behaved server + double SIGINT → 0 leftover; ignoring-EOF server + DOUBLE SIGINT (300 ms apart, default signal death, no `exit` event) → 1 orphan `evil.mjs` (FAIL-ish edge, see notes).
- HTTP (`http1.mjs`, port 0): `Accept` is exactly `application/json, text/event-stream` even with a configured `Accept: text/plain`; `Authorization`/`X-QA` on `initialize`, `notifications/initialized` and `tools/call`; `Mcp-Session-Id` echoed from the 2nd request on; `MCP-Protocol-Version: 2025-06-18` from `notifications/initialized` on; `close()` sends `DELETE` with the id (PASS). SSE body never closed after the result → `callTool` resolves in 2 ms, `close()` 3 ms (PASS). SSE with comment lines, `event: ping` notification and `id:` → resolves (PASS). 503 → `HTTP 503: upstream down` (PASS). Refused/unsafe port → `POST <url>: <cause>` (PASS). 404 on a live session → that call rejects `session expired (HTTP 404)`, the next call re-initialises (new session, 1 extra `initialize`) and succeeds (PASS; failing call is not retried). Array (batch) body, `<html>` body under `content-type: application/json`, empty 200 → warn (or silence for empty) then the call times out (2 s test timeout; no crash) (documented). `text/html` content-type with a JSON body is parsed as JSON (PASS).
- Timeout: unit test `jsonrpc.test.ts:127-148` (fake timers, pending at 59 999 ms, rejected at 60 000 ms, `notifications/cancelled{requestId,reason:"timeout"}`); mine: `timeoutMs 300`, server answers at 600 ms → rejected at 300 ms, late reply logged `ignored a response to unknown id 2`, no throw, client still `connected`, 2nd call works (PASS).
- `--no-mcp`: CLI fs spy above (no config read, trusted cwd), `markerExists=false`; TUI path through `FakeTerminal` (throwaway vitest, deleted): `--no-mcp` → 0 `startMcp` calls, no pid, exit 0; default → 1 call, 1 pid, exit 0 (PASS).
- Pagination (`page.mjs`): two pages followed (unit + fixture); empty page then a page with a tool → `["only"]`; duplicate name across pages → first wins; cycle `a→b→a` → warn `repeated cursor a`, terminates; malformed tool (no `name`) → whole `listTools` rejects `unexpected tools.0.name: Required`; endlessly NEW cursors → loops forever (no page cap; see notes).
- Live smoke (`make env-status` local, docker infra up): copied SWE driver to `qa-208/smoke`, fake HOME/TREE_AGENT_HOME → `tree-memory-local connected: true tools: 8` (no annotations), `serve_mcp` pids while open 2, after close 0, 5283 ms; `search_memory` → `isError false, 1 content, structured true, 15468 chars` (PASS).
- Mutations (restored byte-identical, md5 verified): (1) drop reconnect (`#peer/#transport = undefined` in `onClose`) → 2 fail (`client.test.ts` stdio respawn, HTTP expired session); (2a) project trust gate dropped in `startMcp` → 1 fail (`cli.test.ts > an untrusted cwd reads the user mcp.json only`); (2b) config also reads `<cwd>/.mcp.json` → 3 fail (`never opens the repo-root .mcp.json (fs spy)`, `only .mcp.json present`, `cli > a trusted cwd … .mcp.json is never parsed`).

**Acceptance criteria**
- [x] PASS — config union both transports, both+command/url / unknown exposure / non-URL named with server, merge project-wins, `.mcp.json` unopened — `tests/mcp/config.test.ts:60-215` (esp. :110, :116, :104, :154, :182) + my CLI fs spy and merge probe.
- [x] PASS — stdio handshake, 2 pages, callTool echo, stderr to logger not stdout, `close()` ends child — `client.test.ts:76-165`, `transport-stdio.test.ts:69,122`; live CLI run (stdout `ok`, stderr only at `TREE_AGENT_LOG=info`).
- [x] PASS — HTTP JSON + SSE resolve, session id sent next, headers on every POST, Accept both types — `transport-http.test.ts:41-115`, `client.test.ts:254-290`; my header audit.
- [x] PASS — server→client request `-32601` without rejecting pending; 60 s timeout (fake timers); dropped stdio reconnects — `jsonrpc.test.ts:77,127`, `client.test.ts:122,167`; mutation 1 kills the reconnect tests.
- [x] PASS — `--no-mcp` → no config read, no spawn — `cli.test.ts main (mcp)`; CLI fs spy; TUI probe.
- [x] PASS — cadence green (above).

**Evidence**
```
$ make harness-lint-fix && make harness-lint-check && make harness-typecheck && make harness-tests && make pre-commit
 Test Files  39 passed (39)
      Tests  622 passed (622)
biome check (harness)....................................................Passed
exit=0
$ (live) node qa-208/smoke/smoke.ts   # fake HOME/TREE_AGENT_HOME
tree-memory-local connected: true tools: 8 … serve_mcp pids while open: 2 / after close: 0 elapsed ms: 5283
```

**Other issues found (non-blocking; orchestrator decides)**
1. Orphan on a double Ctrl+C: Node's default SIGINT death fires no `exit` event, so the `process.once("exit")` kill hook in `transport-stdio.ts:36-44` never runs; a stdio server that ignores stdin EOF survives (`pgrep -fl evil.mjs`). Servers that exit on EOF (the normal case) are fine. Suggest handling SIGINT/SIGTERM in the hook (or documenting it).
2. `listTools` has a repeated-cursor guard but no page cap: a server returning endlessly new cursors loops forever (`client.ts` do/while).
3. A server that never answers `initialize` blocks startup (and the first prompt) 60 s (`hang` probe: `initialize timed out after 60 s`); servers start in parallel so it is 60 s total, not per server. A shorter startup timeout would help 209.
4. `url` accepts any scheme (`ftp://h/` passes `z.string().url()`); spec-verbatim, fails later at fetch.
5. A server named `__proto__` is silently dropped (`mcpServers[name] = …` on a plain object in `parseMcpConfig`); use `Object.create(null)` / a Map. Edge.
6. The call that hits an expired HTTP session / dead child fails; only the next call recovers (matches the AC wording "next callTool"; a single transparent retry of the failing call would match the spec's "start a new session" more closely).
7. Batch-array HTTP bodies, two-objects-per-line and empty 200s for a request leave the call pending until the 60 s timeout (warned, not crashed).
8. Startup warns in the TUI go to stderr over the screen (SWE already noted).

**VERDICT: PASS**

### [SWE] 2026-10-25 02:55 — Fold-ins before commit

Three of the Tester's non-blocking findings (#1–#3) folded in. All changes stay inside this task's files.

**Files modified**
- `src/mcp/transport-stdio.ts`: the shared process hook now also prepends one `SIGINT` and one `SIGTERM` listener. `handleSignal(signal, target = process)` does nothing when another listener is still counted (the headless `once` abort from 204 runs, and `main`'s `finally` closes the clients). When the hook is the only listener left (a second Ctrl+C after the abort's `once` is spent), it sends SIGTERM to every live child, sends SIGKILL to survivors after the 2 s grace, then exits `128 + n` (130 / 143). The `exit` hook stays as the backstop.
- `src/mcp/client.ts`: `MAX_TOOL_PAGES = 100`. Past it, `listTools` warns once (`tools/list stopped after 100 pages`) and returns what it collected. This sits alongside the repeated-cursor guard. `INITIALIZE_TIMEOUT_MS = 10_000`, applied to `initialize` only, with a `McpClientOptions.initializeTimeoutMs` override. `tools/call` keeps 60 s. `connectMcpServers` logs `connecting to N MCP server(s)…` at info before the handshakes (only when N > 0). An initialize timeout is still one warn naming the server, and the client is kept for a lazy retry.
- `src/mcp/jsonrpc.ts`: `request(method, params, { timeoutMs })` is a per-call override of the peer default.
- `tests/fixtures/fake-mcp-server.mjs`: new knob `FAKE_MCP_ENDLESS_PAGES=1` (a new tool and a never-seen cursor on every page).

**Tests (new)**
- `transport-stdio.test.ts > handleSignal …`: `registers one prepended SIGINT and SIGTERM listener for every transport`, `alone (a second Ctrl+C), it kills an EOF-ignoring child, then exits 130`, `SIGTERM alone exits 143`, `with another listener (the headless abort), it leaves the child and the process alone`.
- `client.test.ts`: `tools/list stops after 100 pages of endlessly new cursors, warning once`, `a server silent on initialize: an info line first, one warn after its own timeout, the client kept`, `logs no connecting line when there is no server`.
- `jsonrpc.test.ts`: `a per-call timeout (initialize's 10 s) overrides the 60 s default` (fake timers: initialize still pending at 9 999 ms, rejected at 10 000 ms with `notifications/cancelled`, the concurrent `tools/call` still pending until 60 s). The existing 60 s test is unchanged.
- Mutation probes (restored, md5 verified): no page cap → 1 fail. Initialize without its own timeout → 1 fail. Hook ignoring other listeners → 1 fail. Hook with no children to kill → 2 fail.

**Evidence**
```
$ make harness-lint-fix && make harness-lint-check && make harness-typecheck && make harness-tests && make pre-commit
Checked 87 files … No fixes applied.     tsc --noEmit clean
 Test Files  39 passed (39)
      Tests  630 passed (630)
biome check (harness)....Passed   exit=0
$ pgrep -fl fake-mcp-server   → (none)

# Real CLI, live model, fake HOME/TREE_AGENT_HOME, a user mcp.json naming a stdio server that ignores stdin EOF and SIGINT,
# prompt "run bash sleep 25"; signals sent to the CLI pid at 7 s:
double SIGINT (300 ms apart) → exit 130, server pid gone   (before: orphan)
single SIGINT                → exit 1,   server pid gone   ("run ended: aborted")
single SIGTERM               → exit 1,   server pid gone
TREE_AGENT_LOG=info stderr: "[tree-agent] info: connecting to 1 MCP server(s)…"
```

**Notes**
- On the fallback path the children's exits are not `#closing`, so each one may log `server exited (signal SIGTERM)` at warn while the harness dies. This is intentional: the user asked to leave twice.
- Tester findings #4–#8 are not addressed (out of scope for this fold-in).

### [SWE] 2026-10-25 05:10 — Post-commit fix: initialize timeout → setting, 30 s default

**Why**
The fold-in hard-coded `INITIALIZE_TIMEOUT_MS = 10_000`, which is too short for the real `tree-memory-local` server: it answers `initialize` in about 11 s warm and 17–28 s on slow runs (Python imports, Opik setup). Sessions logged `initialize timed out after 10 s` and started with no memory tools. The wait is now `settings.mcp.initializeTimeoutMs` (positive integer, default 30 000). It is one global value: there is no per-server knob in `mcp.json`. `tools/call` stays at 60 s.

**Files modified**
- `apps/harness/src/config/settings.ts`: new strict `mcp: { initializeTimeoutMs }` section.
- `apps/harness/src/mcp/jsonrpc.ts`: a timed-out call rejects with `JsonRpcTimeoutError` (an `Error` subclass, same message), so the client can tell a timeout from other failures without matching strings.
- `apps/harness/src/mcp/client.ts`: `INITIALIZE_TIMEOUT_MS = 30_000` is the fallback. When `initialize` times out, the error gets ` (settings.json: mcp.initializeTimeoutMs)` appended. This happens in the handshake, so lazy reconnects show the hint too. `StartMcpOptions` gains `initializeTimeoutMs` and `log`, and `startMcp` forwards both to `connectMcpServers`.
- `apps/harness/src/cli.ts`: passes `settings.mcp.initializeTimeoutMs` to `startMcp`.
- `apps/harness/README.md` (213's uncommitted copy, edited in place): new settings row `mcp.initializeTimeoutMs | 30 000`. The MCP timeouts bullet and the memory-server paragraph now say 30 s and name the setting. The classifier's separate "10 s timeout" is unchanged.

**Tests**
- `tests/config/settings.test.ts`: `DEFAULTS` gains `mcp`. New test `reads mcp.initializeTimeoutMs (default = the client's fallback) and rejects 0 and a fraction`. It checks that the default equals `INITIALIZE_TIMEOUT_MS`, that 90 000 is read, that 0, 1.5 and an unknown `mcp.timeoutMs` are rejected, and that the issue names the key path.
- `tests/mcp/client.test.ts` (fake timers, a silent HTTP server via an injected never-answering `fetch`, no real I/O): `initialize waits 30 s by default: pending at 29 999 ms, a warn naming the setting at 30 000 ms` and `a custom initializeTimeoutMs is honoured: 45 s is still pending at 44 999 ms`. The existing silent-stdio warn now ends with the hint. Also new: `startMcp passes initializeTimeoutMs on to every client` and `a tools/call timeout carries no settings hint`.
- `tests/mcp/jsonrpc.test.ts`: the per-call override test now runs at 30 s (`initialize timed out after 30 s`).
- `tests/cli.test.ts`: `passes settings.json mcp.initializeTimeoutMs to startMcp (default 30 000)` checks 30 000, then 90 000 from `settings.json`.
- Red first: 8 failures before the change, for the right reasons (the defaults lacked `mcp`, the old 10 s constant, a missing hint). All green after.

**Evidence**
```
$ make harness-lint-fix && make harness-lint-check && make harness-typecheck && make harness-tests && make pre-commit
Checked 102 files … No fixes applied. Found 1 warning (pre-existing comma operator in tests/compaction, not touched)
tsc --noEmit clean
 Test Files  46 passed (46)
      Tests  855 passed (855)
biome check (harness)....Passed   prettier....Passed

# Live: env local, scratch TREE_AGENT_HOME, `make harness-run PROMPT="Reply with exactly: ok" ARGS="--cwd ../.. --trust --json"`, stderr timestamped
run 1: "connecting to 1 MCP server(s)…" at 0.97 s → server "Processing request of type ListToolsRequest" at 17.60 s  ⇒ handshake ≈ 16.6 s; 0 warns; output "ok"
run 2: 0.96 s → 15.40 s  ⇒ handshake ≈ 14.4 s; 0 warns; output "ok"
run 3 (prompt: call search_memory for 'agents'): tool_call mcp__tree_memory_local__search_memory → tool_result with real parents; answer "10"; 0 warns
run 4 (settings.json {"mcp":{"initializeTimeoutMs":2000}}):
  [tree-agent] warn: mcp "tree-memory-local": initialize timed out after 2 s (settings.json: mcp.initializeTimeoutMs)
```

**Notes**
- Both measured handshakes (14–17 s) would have failed under the old 10 s limit. 30 s gives roughly 2× headroom over the warm case, but a cold `uv` sync can still go over it, and the README says how to raise the limit.

### [Tester] 2026-10-25 05:30 — QA of the post-commit initialize-timeout fix

**Test summary**
- Cadence (`make harness-lint-fix && lint-check && typecheck && tests && make pre-commit`): all green, exit 0. Biome: 1 pre-existing warning (tests/compaction comma operator), 0 new. `git diff` checksum identical before and after lint-fix (nothing reformatted).
- Harness suite: 46 files / 855 tests passed (live OpenRouter tests ran).

**Verified**
- Settings: default 30 000 (`Settings.parse({}).mcp.initializeTimeoutMs`, asserted equal to `INITIALIZE_TIMEOUT_MS` in `tests/config/settings.test.ts`). Through the real CLI with a scratch `TREE_AGENT_HOME`: `0` and `-5` → `mcp.initializeTimeoutMs: Number must be greater than 0`; `1.5` → `Expected integer, received float`; `"30000"` → `Expected number, received string`; `{"mcp":{"timeoutMs":1000}}` → `mcp: Unrecognized key(s) in object: 'timeoutMs'`; `{"mcp":null}` → `Expected object, received null`. Each names the file and key path, exit 1.
- Fake-timer tests: pending at 29 999 ms, warn at 30 000 ms; custom 45 000 pending at 44 999 ms, warn at 45 s. Warn text exact: `[tree-agent] warn: mcp "silent": initialize timed out after 30 s (settings.json: mcp.initializeTimeoutMs)`.
- `tools/call` timeout: still 60 s (`CALL_TIMEOUT_MS`), the test asserts `/^mcp "fake": tools\/call timed out after 0\.2 s$/` (no hint).
- Reconnect (own script, silent stdio server, `initializeTimeoutMs: 300`, two consecutive `callTool`s): both reject with `JsonRpcTimeoutError` `mcp "silent": initialize timed out after 0.3 s (settings.json: mcp.initializeTimeoutMs)`. Two silent servers connect in parallel (1 009 ms for a 1 s limit), both warned.
- `--no-mcp --no-skills` run: prints `ok`, exit 0, no server started.
- Live, repo root via `make harness-run … ARGS="--cwd ../.. --trust --json"`, scratch `TREE_AGENT_HOME`, local infra up: "Reply with exactly: ok" → `ok`, exit 0, 19 s wall, 0 `timed out` lines. The memory prompt (`TREE_AGENT_LOG=info`) → `tool_call mcp__tree_memory_local__search_memory {"query":"agents","top_k":2}`, a real `parents` result, answer `2`, `agent_end completed`, 0 timeout lines.
- Mutation: `INITIALIZE_TIMEOUT_MS = 30_000` → `10_000` in `src/mcp/client.ts` → 3 tests fail (settings default test, jsonrpc per-call test, the 30 s default test). Restored from a saved copy; `shasum` identical before and after (`39c75ca2…`).
- Files changed by A: `src/cli.ts`, `src/config/settings.ts`, `src/mcp/client.ts`, `src/mcp/jsonrpc.ts` + 4 test files. No other `apps/harness/src` file moved.

**Other issues found (non-blocking)**
- `initializeTimeoutMs` has no upper bound. A value above 2 147 483 647 (e.g. `3000000000`) passes validation, Node clamps the timer to 1 ms (`TimeoutOverflowWarning`) and the handshake fails after 48 ms with the misleading `initialize timed out after 3000000 s (settings.json: mcp.initializeTimeoutMs)`. Repro: `/private/tmp/claude-501/-Users-pauliusztin-Documents-01-Projects-AI-Engineer-Handbook-building-agentic-systems-building-agentic-systems/a7886cf7-0496-410f-869e-8174a1590e03/scratchpad/qa-213/overflow.mjs`. Suggest `.max(2_147_483_647)` (the other `*Ms` settings share the gap). Follow-up, not a blocker.
- 213's SWE log still says the fixed 10 s timeout is a BLOCKER and counts 849 tests; superseded by this fix (suggest a one-line addendum there).

**VERDICT: PASS**
- `code-review` plugin is enabled in `.claude/settings.json`, but this Tester run had no tool to invoke it. Substitute: a manual read of the whole Part A diff (`cli.ts`, `settings.ts`, `client.ts`, `jsonrpc.ts`): no defects beyond the upper-bound note above (`startMcp` forwards `env`, `cwd`, `initializeTimeoutMs`, `log` and strips only `trusted`; the rethrown `JsonRpcTimeoutError` keeps the name and message, drops the original stack, harmless).
