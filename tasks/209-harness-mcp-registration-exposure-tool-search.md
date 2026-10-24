---
id: 209-harness-mcp-registration-exposure-tool-search
status: pending
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

- [ ] Naming: a server keyed `tree-memory-local` registers `mcp__tree_memory_local__<tool>`; every registered
      name matches `^[a-z][a-z0-9_]*$` and is ≤ 64 chars (a too-long name fails registration with a message).
- [ ] Glob exposure: `{"search_*": "direct", "get_*": "codemode", "admin_*": "deferred", "delete_*": "hidden"}`
      yields those exposures, first match wins, an unmatched tool is `codemode`, a `hidden` tool is absent from
      `registry.list()`; `readOnlyHint: true` → `isReadOnly`; a 2 100-char description is cut at 2 048.
- [ ] A 25 KB text result reaches the model middle-truncated with the tmp path appended and the full text on
      disk; `structured` holds `structuredContent` when present, else the parsed text.
- [ ] `tool_search` is absent with zero `deferred` tools and present with one; BM25 ranks a tool whose NAME
      matches the query above one whose description matches; `tool_search("visualize memory")` activates the
      `visualize_*` tools, `schemasForRequest()` includes them on the next request, the `tools` section lists
      them, a `tool_activation` entry is on disk, and `--resume` restores the activation without a new search.
- [ ] With no MCP servers the model sees exactly 4 tools; with the committed config the model sees
      `read`, `bash`, `edit`, `write`, `mcp__tree_memory_local__search_memory`, `tool_search` (+ `codemode`
      after 210); the `mcp_servers` line reads `- tree-memory-local: <N> tools; direct 1, deferred 2,
      codemode <N-3>` (rag mode, N = the server's tool count).
- [ ] With only `.mcp.json` present (no `.tree-agent/mcp.json`), `/mcp` lists 0 servers.
- [ ] `--no-mcp` → neither `tool_search` nor any `mcp__` tool is registered; no `mcp_servers` section.
- [ ] [HUMAN] (`make local-start`, live key) `tree-agent --trust --json "What do I have in memory about AI agents?"`
      from the repo root (project config is trust-gated) shows a `tool_call` named `mcp__tree_memory_local__search_memory`, a `permission`
      event with `source: "classifier"` and `decision: "allow"`, and an answer citing the hits; exit 0.
- [ ] [HUMAN] `tree-agent --json "Show me a picture of how my memory is organised"` shows `tool_search`,
      a `tool_activation` naming `mcp__tree_memory_local__visualize_memory_structure`, then that tool called on
      the following turn.
- [ ] [HUMAN] In the TUI (`ask`), the first `search_memory` prompts; `s` silences it for the session; `/mcp`
      shows `tree-memory-local` connected with the counts above.
- [ ] `make harness-lint-fix && make harness-lint-check && make harness-typecheck && make harness-tests &&
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
