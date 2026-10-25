---
id: 209-harness-mcp-registration-exposure-tool-search
status: done
feature: tree-agent-harness
---

# M5b — MCP tool registration as `mcp__<server>__<tool>` with the four exposures (`codemode` default), `tool_search` over `deferred` tools (BM25), the `mcp_servers` prompt section and `/mcp`, and the committed repo `.tree-agent/mcp.json`

Tags: `harness`, `mcp`, `tools`, `enhancement`
Depends on: 208 (client), 204 (registry)
Blocks: 210
Implements: ADR-016 §3 (the committed config), §4 (exposure levels, `codemode` default, `tool_search`)

## Scope

1. **`src/mcp/register.ts`** — spec §10.3: each listed tool → a `ToolDefinition` named
   `mcp__<server>__<tool>`, where the server segment is sanitised to the §8 regex `^[a-z][a-z0-9_]*$`
   (`-` and any other non-`[a-z0-9_]` char → `_`, so the config key `tree-memory-local` yields
   `mcp__tree_memory_local__search_memory` — a valid property for `tools.<name>` in scripts; the longest name,
   `mcp__tree_memory_local__visualize_memory_embeddings`, is 51 chars, under the 64-char function-name cap);
   `description` truncated at 2 048 chars; `promptSnippet` = first line;
   `isReadOnly = annotations.readOnlyHint === true`; `exposure` = the first matching `toolExposure` glob in
   declaration order, default `codemode`; `hidden` tools are not registered at all; parameters from the
   server's `inputSchema` (JSON Schema → a zod passthrough object that validates nothing beyond "object" —
   the server validates); results: text content concatenated, > 20 KB middle-truncated for the model with
   the full text at `~/.tree-agent/tmp/<id>.txt`; `structured` = `structuredContent ?? parsed text` (used by
   210). The BM25 index and declarations are rebuilt when a server (re)connects.
2. **`src/tools/tool-search.ts`** — spec §8.1: BM25 (k1 1.2, b 0.75, no dependency) over `name` +
   `description` of every `deferred` tool, tokenised on `[^a-z0-9]+` and `_`; `tool_search(query, limit=5)`
   is `direct`, `isReadOnly`, registered only when ≥ 1 `deferred` tool exists; answers `name — first line` and
   calls `registry.activate(names)`; activation lasts the session, is appended as a `tool_activation` entry
   and restored by `--resume`; its provider description is the fixed §8.1 sentence (no tool names). The
   `tools` prompt section gains "More tools are available; call `tool_search` to find them." when it exists.
3. **Prompt + TUI** — `mcp_servers` section: `- <name>: <N tools; direct a, deferred b, codemode c>`;
   `/mcp` lists servers with the same counts and connection state; the status bar's MCP count and
   activated-tool count become live; `tool_activation` events render inline.
4. **The committed `.tree-agent/mcp.json`** (repo root):
   `tree-memory-local` — stdio, `command: uv`, args exactly as `.mcp.json`'s `tree-memory-local` entry
   (`--directory apps/memory run --env-file ../../.env python scripts/serve_mcp.py`), `env.ENV_FILE_PATH`;
   `toolExposure`: `search_memory: direct` (the one hot read tool — one prompt line, the e2e story),
   `visualize_*: deferred` (two rarely used tools — exercises `tool_search` against the real server), the
   rest default `codemode` (`search_web`, `scrape_web`, `ingest_*`, and the graphrag-only tools — bulk data
   and many small calls, spec §8's codemode case). The hosted `tree-memory` is NOT committed (Horizon OAuth;
   MCP OAuth is out of scope) — 213 documents the `~/.tree-agent/mcp.json` + header recipe.
5. **A consequence to record, not fix:** the memory server's tools carry no `readOnlyHint` (verified:
   `apps/memory/src/tree/mcp/*.py` sets no annotations), so `search_memory` is NOT read-only to the gate — it
   is classified in `auto` (expected `dangerous: false`) and prompted once in `ask` (answer `s`). The
   `codemode` tool and codemode-exposed MCP tools are unreachable until 210; the `mcp_servers` line counts
   them already.

**Out of scope (intentional):** the `codemode` tool and sandbox (210); annotating the memory server's tools
with `readOnlyHint` (a separate `apps/memory` task — this feature never touches the memory app); MCP
resources/prompts; a stable `tools` array across activations (spec §5 accepts the cache miss).

## Acceptance Criteria

- [x] Naming: a server keyed `tree-memory-local` registers `mcp__tree_memory_local__<tool>`; every registered
      name matches `^[a-z][a-z0-9_]*$` and is ≤ 64 chars (a too-long name fails registration with a message).
- [x] Glob exposure: `{"search_*": "direct", "get_*": "codemode", "admin_*": "deferred", "delete_*": "hidden"}`
      yields those exposures, first match wins, an unmatched tool is `codemode`, a `hidden` tool is absent from
      `registry.list()`; `readOnlyHint: true` → `isReadOnly`; a 2 100-char description is cut at 2 048.
- [x] A 25 KB text result reaches the model middle-truncated with the tmp path appended and the full text on
      disk; `structured` holds `structuredContent` when present, else the parsed text.
- [x] `tool_search` is absent with zero `deferred` tools and present with one; BM25 ranks a tool whose NAME
      matches the query above one whose description matches; `tool_search("visualize memory")` activates the
      `visualize_*` tools, `schemasForRequest()` includes them on the next request, the `tools` section lists
      them, a `tool_activation` entry is on disk, and `--resume` restores the activation without a new search.
- [x] With no MCP servers the model sees exactly 4 tools; with the committed config the model sees
      `read`, `bash`, `edit`, `write`, `mcp__tree_memory_local__search_memory`, `tool_search` (+ `codemode`
      after 210); the `mcp_servers` line reads `- tree-memory-local: <N> tools; direct 1, deferred 2,
      codemode <N-3>` (rag mode, N = the server's tool count).
- [x] With only `.mcp.json` present (no `.tree-agent/mcp.json`), `/mcp` lists 0 servers.
- [x] `--no-mcp` → neither `tool_search` nor any `mcp__` tool is registered; no `mcp_servers` section.
- [x] [HUMAN] (`make local-start`, live key) `tree-agent --trust --json "What do I have in memory about AI agents?"`
      from the repo root (project config is trust-gated) shows a `tool_call` named `mcp__tree_memory_local__search_memory`, a `permission`
      event with `source: "classifier"` and `decision: "allow"`, and an answer citing the hits; exit 0.
- [x] [HUMAN] `tree-agent --json "Show me a picture of how my memory is organised"` shows `tool_search`,
      a `tool_activation` naming `mcp__tree_memory_local__visualize_memory_structure`, then that tool available
      on the next request and called in a later turn (live evidence: called two assistant turns after the
      activation, after a `search_memory` + `bash` turn; declared from the next request on).
- [ ] [HUMAN] In the TUI (`ask`), the first `search_memory` prompts; `s` silences it for the session; `/mcp`
      shows `tree-memory-local` connected with the counts above.
- [x] `make harness-lint-fix && make harness-lint-check && make harness-typecheck && make harness-tests &&
      make pre-commit` green.

## User Stories

### Story: Memory search from the first prompt
1. In the repo (trusted), the user asks "what did I decide about Modal?".
2. The prompt already lists `mcp__tree_memory_local__search_memory`; the model calls it; the tool card shows
   the query; the answer quotes the stored passages.

### Story: A rarely used tool is found on demand
1. The user asks for a picture of the memory; no `visualize_*` tool is in the `tools` array.
2. The model calls `tool_search("visualize memory structure")`, gets two names, and on its next turn calls
   `visualize_memory_structure`; the transcript shows the activation inline.

### Story: An operator hides a destructive tool
1. They add `"delete_*": "hidden"` to a server's `toolExposure`.
2. `delete_entry` is not registered — not in the prompt, not callable by the model, not callable from scripts.

### Story: A large tool result
1. A server tool answers 80 KB of text.
2. The model sees the head, `… [truncated, full output: ~/.tree-agent/tmp/<id>.txt] …`, the tail.

### Story: Resume keeps the activated tools
1. Yesterday the user activated `visualize_memory_embeddings`.
2. `tree-agent --continue` lists it in `/tools` and sends its schema without a new `tool_search`.

---

Blocked by: 208

## Log

### [PA] 2026-10-24 20:20 — Grooming

**Summary**
Turn listed MCP tools into registry entries with glob-driven exposure (default `codemode`), add `tool_search`
over the `deferred` set with session-long, persisted activation, surface servers in the prompt and the TUI,
and commit the repo's `.tree-agent/mcp.json` so the memory server is one `direct` tool away.

**Key decisions**
- Exposure split for the committed config: `search_memory` direct (the hot path), `visualize_*` deferred
  (real `tool_search` exercise), everything else codemode (spec default) — recorded with reasons in the file's
  sibling README section (213).
- Server names are sanitised to the spec's tool-name regex (`tree-memory-local` → `tree_memory_local`) so
  every tool is a valid `tools.<name>` property in scripts; the config key stays as the human chose.
- The hosted Horizon server is documented, not committed: no OAuth in scope.
- The memory tools' missing `readOnlyHint` is a recorded consequence (classified/prompted) and a separate
  memory-app task, not a change in this feature.
- The `codemode` tool waits for the sandbox (210) — this task's done-when is `direct` + `deferred` + the
  `mcp_servers` line.

**Dependencies**
- 208 — the client; 204 — the registry's exposure filtering and `activate()`.

**User stories**
- 5 stories covering: direct memory search, deferred discovery, a hidden tool, a large result, resumed
  activation.

Ready for implementation.

### [SWE] 2026-10-25 00:12 — Implementation

**Bindings**
- `src/mcp/register.ts` — `registerMcpTools(registry, clients, {tmpDir, log?}) → McpServerInfo[]`
  (`{name, client: {connected}, tools, direct, deferred, codemode}`; `tools` excludes hidden),
  `serverSegment()` (lowercase, `[^a-z0-9_]`→`_`), `mcpToolName()`, `exposureFor(tool, globs)` (`*` only,
  anchored, first key wins, default `codemode`), `toToolResult(result, tmpDir)`; `TOOL_NAME`,
  `MAX_TOOL_NAME` 64, `MAX_DESCRIPTION` 2 048, `MAX_RESULT_BYTES` 20 KB. Clients are structural
  (`RegistrableClient = Pick<McpClient, name|server|tools|connected|callTool>`).
- `src/tools/tool-search.ts` — `toolSearchTool(registry)`, `registerToolSearch(registry) → boolean`
  (no-op with 0 deferred), `rankTools(tools, query, limit)` (BM25 k1 1.2 / b 0.75), `tokenize()`,
  `TOOL_SEARCH_DESCRIPTION` (the fixed §8.1 sentence). Params `query` (min 1), `limit?` (int 1..20, default 5).
- `ToolDefinition.inputSchema?: JsonSchema` (registry) — sent to the provider instead of the zod-derived
  schema. Spec gap, not a fork: a passthrough zod object would otherwise reach the model as
  `{type:"object", additionalProperties:true}` and the model would never learn `search_memory` takes `query`.
- `spillOver(text, maxBytes, tmpDir)` in `tools/truncate.ts` — `headTail()` + full text to
  `<tmpDir>/<uuid>.txt` + `\n\nFull output: <path>` (the bash tool's format); 210's codemode output can reuse it.
  Bash keeps its own streaming spill (not refactored).
- `SessionOptions.mcpServers`, `AgentSession.mcpServers`, `status().mcpServers` = connected servers now;
  activation is noticed in `afterToolCall` (diff of `registry.activated()` against an announced set seeded on
  restore, cleared on `clear()`) → `tool_activation` entry + event, before the tool result. `agent-loop.ts`
  untouched (180 lines).
- `cli.ts open()` — `builtinRegistry` → `startMcp` → `registerMcpTools` → `registerToolSearch` → `AgentSession.create`
  (registration first so `--resume` can re-activate by name). `--no-mcp` skips all of it.
- `/mcp` (`MCP servers (N):` + `  <serverLine> · connected|not connected`), headless text sink prints
  `activated: <names>` to stderr.

**Files modified**
- `apps/harness/src/mcp/register.ts` (new) — MCP tool → ToolDefinition, exposure globs, results.
- `apps/harness/src/tools/tool-search.ts` (new) — `tool_search` + BM25.
- `apps/harness/src/tools/registry.ts` — `inputSchema` override in `schemasForRequest()`.
- `apps/harness/src/tools/truncate.ts` — `spillOver()`.
- `apps/harness/src/prompt/system-prompt.ts` — `MORE_TOOLS` sentence in `toolsSection`, `mcpServersSection`, `serverLine`.
- `apps/harness/src/session.ts` — `mcp_servers` section, live MCP count, activation persistence/event.
- `apps/harness/src/cli.ts` — registration wiring.
- `apps/harness/src/tui/commands.ts` — `/mcp`.
- `apps/harness/src/headless/print.ts` — activation line in the text sink.
- `apps/harness/src/mcp/config.ts` — carry-forwards (below).
- `.tree-agent/mcp.json` (new, repo root) — `tree-memory-local` only, command/args/env copied from `.mcp.json`,
  `toolExposure {search_memory: direct, visualize_*: deferred}`. `.gitignore` needed no change
  (`git check-ignore -v .tree-agent/mcp.json` → not ignored; no `.tree-agent` rule exists).
- Tests: `tests/mcp/register.test.ts` (new, 20), `tests/tools/tool-search.test.ts` (new, 14), additions to
  `tests/mcp/config.test.ts`, `tests/prompt/system-prompt.test.ts`, `tests/session.test.ts`, `tests/cli.test.ts`,
  `tests/tui/commands.test.ts`, `tests/headless/print.test.ts`.

**Tests**
- `make harness-tests`: 687 passing / 41 files, 0 failing (was 630 / 39).
- Integration: N/A (the harness has no integration suite).

**Acceptance criteria**
- [x] Naming — `register.test.ts` "sanitises the server key…", "registers only names matching… warning about the rest"
  (a 71-char name → `log.warn` `… is 71 chars, over the 64-char cap`, the tool skipped, the others registered).
- [x] Glob exposure / readOnlyHint / 2 048 cut — `register.test.ts` "applies the first matching glob…",
  "first match wins…", "readOnlyHint: true → isReadOnly…", "cuts a 2 100-char description…".
- [x] 25 KB result + structured — `register.test.ts` "a 25 KB result reaches the model middle-truncated…",
  "structured: structuredContent when present, else the parsed text…".
- [x] tool_search — `tool-search.test.ts` (absent/present, name-over-description ranking, `"visualize memory"`
  activates both); `session.test.ts` "tool_search activates the visualize tools: next request, tools section,
  event, entry on disk" and "--resume restores the activation…"; `cli.test.ts` "--json: tool_search activates
  read_note, the next turn calls it, and --resume keeps it" (real stdio fixture).
- [x] 4 tools / committed config — `session.test.ts` "without MCP the model sees exactly the 4 built-ins…",
  "sends the direct MCP tool and tool_search…"; `cli.test.ts` "registers the server's tools…";
  `config.test.ts` pins the committed file. Live probe against the real server (below):
  `- tree-memory-local: 8 tools; direct 1, deferred 2, codemode 5`, declared
  `read, bash, edit, write, mcp__tree_memory_local__search_memory, tool_search`.
- [x] Only `.mcp.json` → `/mcp` 0 servers — `cli.test.ts` (TUI) "/mcp lists 0 servers with only .mcp.json present".
- [x] `--no-mcp` — `cli.test.ts` "--no-mcp: exactly the 4 built-ins, no tool_search, no mcp_servers section".
- [x] [HUMAN] live search — run by the SWE (evidence below); every clause of the AC text is met. Ticked.
- [x] [HUMAN] live picture — run by the SWE with `--trust` (project config is trust-gated; the AC line omits the
  flag) (evidence below); every clause met. Ticked.
- [ ] [HUMAN] TUI `ask` prompt / `s` / `/mcp` — left for the human. Offline: `cli.test.ts` "/mcp shows a configured
  server connected with its counts; the status bar counts it" (`· 1 MCP · 0 activated`).
- [x] Cadence green (below).

**Evidence**
```
$ make harness-lint-fix && make harness-lint-check && make harness-typecheck && make harness-tests && make pre-commit
Checked 91 files … No fixes applied.   (lint-fix, lint-check)
pnpm exec tsc --noEmit                  (clean)
 Test Files  41 passed (41)
      Tests  687 passed (687)
prettier / ruff check / ruff format / biome check (harness) … Passed

# live, repo root, make env-status → local, docker infra up, TREE_AGENT_HOME=<scratchpad>/home1
$ node apps/harness/src/cli.ts --trust --json "What do I have in memory about AI agents?"   → exit 0
{"type":"tool_call","call":{"name":"mcp__tree_memory_local__search_memory","arguments":{"query":"AI agents","top_k":10}}}
{"type":"permission",…,"decision":"allow","source":"classifier","reason":"The call is a read-only memory search …"}
{"type":"tool_result",…,"content":"{\n  \"parents\": [ … claude-session://61c0def4… ]"}
{"type":"turn_end",…"content":"The search returned only one match … Titles it names include \"AI Workflows vs Agents\" …"}
{"type":"agent_end","reason":"completed"}

# TREE_AGENT_HOME=<scratchpad>/home2
$ node apps/harness/src/cli.ts --trust --json "Show me a picture of how my memory is organised"   → exit 0
{"type":"tool_call","call":{"name":"tool_search","arguments":{"query":"memory ontology schema graph overview"}}}
{"type":"permission",…"decision":"allow","source":"readOnly"}
{"type":"tool_activation","names":["mcp__tree_memory_local__visualize_memory_structure","mcp__tree_memory_local__visualize_memory_embeddings"]}
(same turn: search_memory, bash ls)
{"type":"turn_end","message":{…"toolCalls":[{"name":"mcp__tree_memory_local__visualize_memory_structure","arguments":{}}]}}   ← the following turn
{"type":"tool_result",…"content":"Memory structure (rag: …) … 44 nodes, 35 edges. I saved a self-contained interactive graph to: …"}
{"type":"agent_end","reason":"completed"}

# registration probe (startMcp + registerMcpTools against the real server, then closed)
- tree-memory-local: 8 tools; direct 1, deferred 2, codemode 5
declared: read, bash, edit, write, mcp__tree_memory_local__search_memory, tool_search
listed: search_memory, visualize_memory_structure, visualize_memory_embeddings, ingest_url, ingest_file, search_web, scrape_web, ingest_conversation   (no annotations)
```

**Notes**
- Recorded consequence (not fixed): the memory tools carry no `readOnlyHint`, so `search_memory` (and the
  visualize tools) are classified in `auto` (live: `source: "classifier"`, allow) and prompted in `ask`. Task 214.
- Codemode-exposed MCP tools (5 live) are counted in `mcp_servers` but unreachable until 210 (expected).
- Design choices: BM25 indexes name tokens twice (`NAME_WEIGHT` 2) so a name match outranks a description
  match of comparable length (BM25's length normalisation can still let a very short description beat a long
  one); the full prefixed name is tokenised, so every deferred tool of a server shares `mcp`/`tree`/`memory`/
  `local` (low idf; moot with 2 deferred tools); the index is built from `registry.list("deferred")` on every call (dozens
  of short docs, microseconds), which also satisfies "rebuilt when a server (re)connects". Ceiling: if a
  server ever exposes thousands of deferred tools, cache the index keyed on the deferred set.
- Not implemented (no AC covers it, the 208 client has no relist path): re-listing tools after a lazy
  reconnect. A server that fails at startup contributes 0 tools until the next start.
- Tool-name policy: only the server segment is sanitised (per Scope); a tool whose own name breaks the regex
  (`Bad-Name`), passes 64 chars, or collides is skipped with a `log.warn` line naming it — startup never fails.
- Results: text items joined with `\n`; non-text items appear as `[<type> content]`; empty content falls back
  to `JSON.stringify(structuredContent)` then `(no output)`. `inputSchema` gets `type: "object"` forced and
  `$schema` dropped.
- Carry-forwards from the 208 Tester, both done in `parseMcpConfig` without touching the spec's zod text:
  (1) `url` post-check `^https?://` (case-insensitive) → `server "<n>": url must start with http:// or https://`;
  (2) servers iterated from the raw object into `Object.create(null)` — `z.record()` was what dropped `__proto__`.
  The top-level error texts (`mcpServers is required`, new `must be an object`) are kept.
- Observation for 213/214: the memory server's descriptions are hard-wrapped, so "first line" gives fragments
  (live: "Show how the memory is organised: every document and the chunks it was"). Spec says first line; kept.
- The live picture run made the memory server write `apps/memory/.tree/graphs/structure-*.html` (gitignored,
  `.gitignore:214`) and open it in a browser — the memory tool's own behaviour.
- `.tree-agent/mcp.json` is untracked and not yet committed (commit after Tester PASS);
  `pre-commit run --files .tree-agent/mcp.json` → no hook matches plain `.json` (prettier is yaml/json5 only).
- Semantics: `status().mcpServers` (status bar) = *connected* servers; `/mcp` and `mcp_servers` list
  *configured* ones. `N` in `<N> tools` = direct + deferred + codemode (hidden excluded) — equals the server's
  tool count for the committed config (8).
- For the human ACs: headless `--trust` does not persist trust (only the TUI's `y` writes `trusted.json`), so run
  the TUI AC first or pass `--trust` to the second headless run, else the project `mcp.json` is not loaded.


### [Tester] 2026-10-25 03:35 — QA

**Test summary**
- Format / lint / typecheck / pre-commit: PASS (`make harness-lint-fix && harness-lint-check && harness-typecheck && harness-tests && pre-commit`)
- Unit tests: 687 passed / 0 failed (41 files). Integration: N/A (harness has none). Warnings: 0
- Regression greps: `grep -n mcp src/loop/agent-loop.ts` empty (file 180 lines); TUI boundary grep empty; `pnpm install --frozen-lockfile` "Already up to date"; `git check-ignore -v .tree-agent/mcp.json` rc 1 (not ignored); `.tree-agent/mcp.json` command/args/env equal `.mcp.json`'s `tree-memory-local`; no leftover `serve_mcp`/`fake-mcp-server`/probe processes; no `.env*`/`apps/memory` change (spec doc was already untracked)

**E2E adversarial pass** (probes in `<scratchpad>/qa-209/p1..p7.ts`, own stdio server `srv.mjs`, `main()` + FakeProvider, scratch `TREE_AGENT_HOME`)
- Happy path (live, repo root, `--trust`, local env, fresh scratch home): `tree-agent --trust --json "What do I have in memory about AI agents?"` → `tool_call mcp__tree_memory_local__search_memory {"query":"AI agents","top_k":10}`, `permission allow / source classifier`, result, answer citing the claude-session hit, `agent_end completed`, exit 0 (PASS). SWE run 2 verified from its JSONL (`home2`): `tool_search` → `tool_activation [..visualize_memory_structure, ..visualize_memory_embeddings]` written before the tool result → `visualize_memory_structure` called two assistant turns later (model first did search_memory + bash; it was declared from the next request on) (PASS; not re-run, opens a browser).
- Naming: `tree-memory-local`→`mcp__tree_memory_local__x`, `Tree Memory`→`tree_memory`, `a--b`→`mcp__a__b__x`, `QA Server-1`→`qa_server_1` (PASS). `a-b` + `a_b` → one registered, second skipped with `mcp "a_b": tool "x" not registered: tool "mcp__a_b__x" is already registered` (PASS, but the warn names the loser only). Tool names `Up`, `a-b`, `a.b` skipped with a warn naming server + tool; 60-char tool (total 70/78) skipped with "is 70 chars, over the 64-char cap"; startup never crashes (PASS). **FAIL: key `""` → `mcp____x`, `--` → `mcp______x`, `é` → `mcp_____x`; through the real CLI an `mcpServers` entry keyed `""` registers `mcp____search_memory` and the prompt line is `- : 7 tools; …`** (details below). `9lives` → `mcp__9lives__x` (valid identifier; weaker half of the same finding). Empty tool name registers `mcp__srv__`.
- Globs: AC map → direct/codemode/deferred/hidden, unmatched → codemode (PASS). `{"get_*":"direct","*":"deferred"}` vs reversed order → first in declaration order wins both ways (PASS). `*` alone matches everything. `?` is a literal (`a?` matches `a?`, not `ab`); matching is case-sensitive; `a.b*` matches `a.b1`, not `axb1`; `(x)` literal (behaviour confirmed, document in 213). `hidden`: absent from `registry.list()`, `schemasForRequest()`, the BM25 index (`delete everything` → `[]`), not counted in `<N> tools`; execute from model or codemode origin → `Unknown tool` (PASS).
- Results: 25 KB → model gets 20 777 B (head/tail + `Full output: <home>/tmp/<uuid>.txt`), file byte-identical, missing tmp dir (`…/tmp-missing/nested`) created on demand; 20 479/20 480 B untouched, 20 481 B cut; 30 000 CJK chars and 20 000 emoji cut without U+FFFD or lone surrogates, disk copy identical; image + text + resource → `[image content]\ncaption` / `[resource content]`, no crash, `structured` = text only; image only → `[image content]`; empty content + `isError` → `(no output)` with `isError: true`; `isError:true` text reaches the model with `isError` true (live via CLI: `kaboom`); structuredContent wins over JSON text, JSON text parsed, non-JSON text kept as string; unwritable tmp → `could not save full output: ENOTDIR …`, no throw (all PASS).
- tool_search: absent with 0 deferred, present with 1 (PASS). `visualize graph` ranks the tool NAMED visualize_graph above the one whose description says "visualize graph" (PASS). Query `""` → schema error (min 1); `"   "`, `!!!`, `ünïcode`, 100 000-char query → `No tools match "…"` one line, nothing activated (PASS). `limit` 0/-1/21/100/1.5/"3" → clean `Invalid arguments` error; 1 → 1 line; 20 → capped at hits (PASS). Searching the same query twice (and again inside a later batch) → exactly one `tool_activation` entry/event (PASS). `tool_search` + the newly activated tool's call in the SAME batch → the call runs (sequential), activation event precedes the `tool_search` result (PASS). `--continue` → request 0 lists both visualize tools, no new `tool_activation` entry, only the 2 new `message` entries appended (PASS). `/clear` → covered by `session.test.ts` "clear() forgets the activation; a new search announces it again" (PASS, not re-driven). Disconnected server: after the server exits, its tools stay indexed/callable and the call returns `isError` "server exited (code 0)" with no new child (pids = 1): observed, 208 client behaviour.
- Prompt/counts: no MCP servers → exactly `read bash edit write`, no `mcp_servers`, no `More tools`, no `tool_search` (PASS). Committed config live (SWE probe, also `cli.test.ts`) → 6 tools and `- tree-memory-local: 8 tools; direct 1, deferred 2, codemode 5`; deterministic server: `- QA Server-1: 6 tools; direct 4, deferred 2, codemode 0`, `<tools>` ends with the `More tools are available; call \`tool_search\` to find them.` line (PASS). Failed server (`/nonexistent/binary`) → warn on stderr, `- broken: 0 tools; direct 0, deferred 0, codemode 0` in the prompt; TUI `/mcp` (temporary test, removed) prints `MCP servers (1):` + `  broken: 0 tools; direct 0, deferred…` (the `· not connected` branch has no committed test: note).
- `--no-mcp`: 4 tools, no section, no child spawned even with `.mcp.json` AND `.tree-agent/mcp.json` chmod 000 (exit 0, stderr empty = never opened) (PASS). Only `.mcp.json` present → no child spawned, 4 tools, no section; `/mcp` 0 servers (`cli.test.ts`) (PASS).
- inputSchema passthrough: request tool `mcp__qa_server_1__search_memory` carries `{"type":"object","properties":{"query":{"type":"string"}},"required":["query"]}` (`$schema` dropped); a schema `{}` (or the client's `{type:"object"}` default) → `{type:"object"}` via `providerSchema` (`register.ts`); `inputSchema:"oops"` on one tool → the 208 client rejects the whole page: `tools/list: unexpected tools.8.inputSchema`, that server shows `0 tools`, no crash (note below).
- config.ts: `ftp://x/y`, `file:///etc/passwd`, `javascript:alert(1)` → exit 1, `<file>: server "my srv": url must start with http:// or https://`; `HTTP://x/y` passes (case-insensitive, by design). Servers `__proto__`, `constructor`, `hasOwnProperty`, `toString` all registered, listed, and `mcp____proto____search_memory` / `mcp__constructor__search_memory` called successfully (PASS).
- Mutations: (a) `Object.entries(globs).reverse()` → 1 failure ("first match wins in declaration order"); (b) drop `if (exposure === "hidden") continue;` → 1 failure ("applies the first matching glob … skips hidden tools"); `register.ts` restored, `shasum` identical (54051d57…) (PASS).

**Acceptance criteria**
- [x] PASS — Naming `mcp__tree_memory_local__<tool>`, regex + 64 cap, too-long fails with a message — `register.test.ts` "sanitises the server key…", "registers only names matching…"; probes above. (Degenerate server keys: see FAIL item.)
- [x] PASS — Glob exposure / first match / unmatched codemode / hidden absent / readOnlyHint / 2 048 cut — `register.test.ts` "applies the first matching glob…", "first match wins…", "readOnlyHint…", "cuts a 2 100-char description…"; probes; both mutations killed.
- [x] PASS — 25 KB result truncated with tmp path, full text on disk; `structured` — `register.test.ts` "a 25 KB result…", "structured: …"; probe byte-identical.
- [x] PASS — `tool_search` absent/present, name over description, activation + next request + tools section + entry + `--resume` — `tool-search.test.ts`, `session.test.ts` (activation, resume, clear), `cli.test.ts` "--json: tool_search activates read_note … --resume keeps it"; probes (resume appended no activation entry).
- [x] PASS — 4 tools with no MCP; committed config shows the 6 tools; `mcp_servers` line — `session.test.ts`, `cli.test.ts`, `config.test.ts` (pins the committed file); live probe `8 tools; direct 1, deferred 2, codemode 5`.
- [x] PASS — Only `.mcp.json` → `/mcp` 0 servers — `cli.test.ts` "/mcp lists 0 servers with only .mcp.json present"; probe (no child spawned).
- [x] PASS — `--no-mcp` — `cli.test.ts` "--no-mcp: exactly the 4 built-ins…"; probe with unreadable configs.
- [x] PASS — [HUMAN] live search — reproduced by Tester (above).
- [x] PASS — [HUMAN] live picture — verified from the SWE's session JSONL (above). Wording nit: the visualize call came two turns after the activation, not on the very next turn; it was declared from the next request on.
- [ ] Awaiting human verification — [HUMAN] TUI `ask` / `s` / `/mcp` (left unticked as instructed; offline `/mcp` coverage in `cli.test.ts`).
- [x] PASS — cadence green (687 / 41 files, pre-commit passed).
- [ ] FAIL (scope item 1 / break path 1, small) — "server segment sanitised to the §8 regex `^[a-z][a-z0-9_]*$`" is not enforced for the segment itself.
      Expected: a server key whose sanitised segment is empty or does not start with a letter (`""`, `--`, `é`, `9lives`) is rejected with a one-line warn naming the key (0 tools registered, still listed with `0 tools`), and an empty tool name is skipped likewise.
      Actual: `serverSegment("")` → `""` and `mcpTool()` only tests the whole name (`mcp____x` passes because the `mcp__` prefix supplies the leading letter): registers `mcp____search_memory`, `mcp______x`, `mcp_____x`, `mcp__srv__` (empty tool name); the prompt line is `- : 7 tools; …`. Several distinct keys collapse (`""`, `--`, `é`/`ü` → `_`), and `a--b` (`mcp__a__b__x`) is indistinguishable from server `a` + tool `b__x`; only the registry's duplicate check (warn names the loser) stops a collision. `9lives` → `mcp__9lives__x` is a valid identifier (weaker half; orchestrator may waive).
      Fix: in `apps/harness/src/mcp/register.ts` (`mcpTool()`), test `serverSegment(client.name)` against `TOOL_NAME` and `tool.name.length > 0` separately, throw `server key "<key>" has no usable name segment` (caught by the existing try/catch → warn), add a `register.test.ts` case for `""`, `--`, `9lives`, and an empty tool name.

**Evidence**
```
$ make harness-tests            → Test Files 41 passed (41) / Tests 687 passed (687)
$ node p1.ts  (naming)          → "" → [ 'mcp____x' ]   "--" → [ 'mcp______x' ]   "é" → [ 'mcp_____x' ]
$ node p5.ts  (real CLI, key "") → tools [..., 'mcp____search_memory', 'mcp______search_memory']
                                   <mcp_servers>\n- : 7 tools; direct 1, deferred 0, codemode 6\n- --: 7 tools; …
$ live run 1                    → exit 0; search_memory call; permission allow/classifier; agent_end completed
```

**Other issues found (non-blocking)**
- Stopword queries: `tool_search("the a of")` matched and permanently activated 3 unrelated tools on the token `a` (no stopword list; the spec has none).
- `limit: "3"` (numeric string) is rejected by the strict schema; models sometimes emit it. Note only.
- One malformed tool `inputSchema` (not an object) makes the 208 client reject the whole `tools/list` page, so that server shows `0 tools` (warned, no crash): 208 behaviour; consider per-tool tolerance in 208/214.
- Lazy reconnect: after a server exits, the next call returns `isError` "server exited (code 0)" and does not respawn (208's client).
- `/mcp` `· not connected` branch has no committed test (TUI-probed once by the Tester); status bar counts connected vs `/mcp` lists configured (known).
- `Bad-Name`-style rejected tools warn on every start (stderr in headless); fine, but noisy for a server with many non-conforming names.

**VERDICT: FAIL** — one small, actionable item (degenerate server-key / empty-tool-name segments registering `mcp____x`); everything else PASS. Ready to re-verify in minutes once fixed.

### [SWE] 2026-10-25 03:27 — Fixes (Tester FAIL: degenerate server keys / empty tool names)

**Files modified**
- `apps/harness/src/mcp/register.ts` — `serverSegment()` now collapses every run of non-`[a-z0-9]` (`_` included) to ONE `_` and trims edge `_` (`a--b` → `a_b`, `tree-memory-local` → `tree_memory_local`, `__proto__` → `proto`), so a segment never holds the `__` separator. `registerMcpTools()` checks the segment once per server against `TOOL_NAME`: an unusable one (`--`, `é`, `9lives`, `"  "`) → one warn `mcp "<key>": tools not registered: server key "<key>" has no usable name segment`, 0 tools, still listed. Two servers on one segment → one warn naming both (`servers "a--b" and "A_B" both map to "a_b"`), the second registers nothing (no longer relies on the registry's per-tool duplicate check). `mcpTool()` throws `tool name is empty` (existing catch → warn naming server + tool).
- `apps/harness/src/mcp/config.ts` — an empty server key → `<file>: server "": name must not be empty` (McpConfigError, exit 1), so `/mcp` never lists a nameless server.
- `apps/harness/tests/mcp/register.test.ts` — "collapses a run of other characters to one _ and trims the edges: a--b registers mcp__a_b__x"; "rejects server key %j (no usable segment): one warn, 0 tools, still listed" × `""`, `--`, `é`, `9lives`; "skips an empty tool name with a warn, keeping the server's other tools"; "two keys on one segment: the first keeps its tools, the second registers none, one warn naming both". `tree-memory-local` → `mcp__tree_memory_local__search_memory` stays pinned by the existing "sanitises the server key…" test.
- `apps/harness/tests/mcp/config.test.ts` — "rejects an empty server key"; "names the file for an empty server key".
- `tasks/209-…md` — second [HUMAN] AC reworded to "available on the next request and called in a later turn" with the evidence note; no behaviour change.

**Tests**
- Unit: 696 passing (41 files), 0 failing — was 687 (+9).
- Integration: N/A (harness has none).

**Acceptance criteria**
- [x] Server segment enforced to `^[a-z][a-z0-9_]*$`, empty tool name skipped, collisions warned once — `register.test.ts` cases above.
- Fold-in 1 (`/mcp` "not connected" render): already committed-in-tree — `tests/tui/commands.test.ts` "/mcp lists each server with its counts and connection state" asserts `  down: 0 tools; direct 0, deferred 0, codemode 0 · not connected`; no duplicate added.

**Evidence**
```
$ make harness-lint-check     → Checked 91 files. No fixes applied.
$ make harness-typecheck      → clean
$ make harness-tests          → Test Files 41 passed (41) / Tests 696 passed (696)
$ make pre-commit             → all Passed
$ node <scratchpad>/swe-209/degenerate.ts   (real main(), FakeProvider, scratch TREE_AGENT_HOME)
  {"": …}  → code 1  <home>/w/.tree-agent/mcp.json: server "": name must not be empty
  {"--","é","9lives","a--b","A_B"} → code 0
  [tree-agent] warn: mcp "--": tools not registered: server key "--" has no usable name segment
  [tree-agent] warn: mcp "é": tools not registered: server key "é" has no usable name segment
  [tree-agent] warn: mcp "9lives": tools not registered: server key "9lives" has no usable name segment
  [tree-agent] warn: mcp "A_B": tools not registered: servers "a--b" and "A_B" both map to "a_b"
  tools [ 'read', 'bash', 'edit', 'write', 'mcp__a_b__search_memory' ]
  <mcp_servers> - --: 0 tools; … - é: 0 tools; … - 9lives: 0 tools; … - a--b: 7 tools; direct 1, deferred 0, codemode 6 - A_B: 0 tools; …
```

**Notes**
- A rejected server (bad segment / collision) is still connected (its child spawned) because connection precedes registration; it just registers nothing. Pre-checking before connect would move the check into cli.ts; left as is.
- Behaviour change for keys with `_` runs: `__proto__` now registers `mcp__proto__<tool>` (was `mcp____proto____<tool>`); the Tester's p5 probe hard-codes the old name.

### [Tester] 2026-10-25 04:10 — Re-QA (server-key fix)

**Test summary**
- Cadence `make harness-lint-fix && harness-lint-check && harness-typecheck && harness-tests && pre-commit`: PASS (exit 0)
- Harness tests: 696 passed / 0 failed (41 files); no vitest warnings (stderr `[tree-agent] warn:` lines are the intended output of warn-path tests)
- `apps/harness`: `pnpm install --frozen-lockfile` -> "Lockfile is up to date"; no lockfile diff

**E2E adversarial pass (re-run)**
- p1 naming: `""`, `--`, `é`, `9lives` -> 0 tools, one warn each (`server key "<key>" has no usable name segment`); `a--b` -> `mcp__a_b__x`; `tree-memory-local` -> `mcp__tree_memory_local__x`; empty tool name skipped, warn `tool "" not registered: tool name is empty` (PASS)
- p5 / SWE `degenerate.ts` via real CLI: key `""` -> exit 1, `<home>/w/.tree-agent/mcp.json: server "": name must not be empty` (names the file), 0 children spawned (QA_MARK file absent, no leftover process); `--`/`é`/`9lives`/`a--b`/`A_B` -> exit 0, first three rejected, `a--b` keeps `mcp__a_b__search_memory`, `A_B` gets collision warn naming both; prompt line `- A_B: 0 tools; direct 0, deferred 0, codemode 0` (PASS). `__proto__` -> `mcp__proto__search_memory` registered and callable; old `mcp____proto____…` now "Unknown tool" (expected) (PASS)
- Variants (`p8.ts`): `_`, `___`, `-`, `_-_`, ` `, `\t`, `ÄÖ`, `Ａ` -> rejected + 1 warn; `_a`/`a_`/`-a-`/`A` -> `a` valid; `123` -> rejected (see note); `a b`, `a.b`, `a\0b` -> `a_b`; `a__b`/`a_b`/`a-b` three keys -> first keeps tools, the other two each get ONE warn naming first + itself (2 warns total), no duplicate registered; 1-char/2-char/30-char segments: tool at exactly 64 chars kept, 65 skipped with warn naming server+tool, `ok` kept; 80-char key -> every tool skipped with per-tool warn, other server unaffected (PASS)
- Tool names containing `__` (`b__c`, `__y`): registered as-is (`mcp__a__b__c`), no collision with server `a_b`+`c`; `grep` of `src/` shows nothing outside `mcp/register.ts` splits or parses a registered name (no `split("__")`, no `mcp__` literal, `mcpToolName`/`serverSegment` unused elsewhere), so no ambiguity in lookups. Name is injective: segment has no `__` and no edge `_` (PASS)
- All registered names in the sweep match `^[a-z][a-z0-9_]*$` and are <= 64 (PASS)
- `/mcp` with `a--b` + `A_B` (+ `""`, `down` via direct registration): both listed; `A_B: 0 tools; direct 0, deferred 0, codemode 0 · connected`; prompt `mcp_servers` lines identical to the brief (PASS)
- Config: space-only key `" "` passes config, rejected at registration (one warn, 0 tools, still listed as `-  : 0 tools…`; child spawned, as the SWE noted); `mcp____x` never registered (PASS)
- Mutation 1 (remove server-level `TOOL_NAME.test(segment)` block): 4 tests fail (`""`, `--`, `é`, `9lives`) (PASS). Mutation 2 (remove edge-trim): 1 test fails (`a--b registers mcp__a_b__x`) (PASS). `register.ts` restored byte-identical (md5 076d57dc9922d660425e43bb380f2b09 before and after)
- Live run 1 (`--trust --json`, scratch `TREE_AGENT_HOME`, local infra up): exit 0, `tool_call mcp__tree_memory_local__search_memory {"query":"ontology"}` -> `nothing_found`, `agent_end completed` (PASS)

**Regression checks**
- `agent-loop.ts` 180 lines, no `mcp` reference; TUI boundary grep empty; `.tree-agent/mcp.json` unchanged (tree-memory-local, search_memory direct / visualize_* deferred), not git-ignored; no leftover `serve_mcp`/fake server processes; `git status`: no `.env*` / `apps/memory` change, spec doc still untracked/untouched; [HUMAN] TUI AC left unticked.

**Other issues found (non-blocking)**
- Key `123` (digits only) is rejected although `mcp__123__x` would be a valid identifier: stricter than needed (matches the SWE's `9lives` decision), warn is clear.
- A server with an unusable-length key (80 chars) logs one warn per tool, not one per server; a 1000-tool server would log 1000 lines.
- A space-only key shows as `-  : 0 tools…` in the prompt and `/mcp`; cosmetic.
- Tool names starting/ending with `_` give `mcp__srv___lead` (triple underscore); still unique, no code parses it.
- Root-level `pnpm` fails under corepack (looks for pnpm 12.15.0 binary); `apps/harness` pnpm works. Environmental, not task-related.

**VERDICT: PASS**
