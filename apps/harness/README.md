# Tree — Coding-Agent Harness

The agent half of **Tree: Your Rooted Personal Assistant**. A minimal TypeScript coding agent (`tree` CLI) that pairs with the `tree-memory` MCP server in `apps/memory/`.

Architecture, rationale, and the seven-milestone roadmap live in [`../../docs/harness-plan.md`](../../docs/harness-plan.md); per-milestone task files are under [`../../docs/tasks/`](../../docs/tasks/).

## Prerequisites

- [Bun](https://bun.sh) ≥ 1.1 (`brew install bun` or `curl -fsSL https://bun.sh/install | bash`)
- `GOOGLE_API_KEY` set in the repo-root `.env` — shared with the memory app
- Optional: `ripgrep` (`brew install ripgrep`) for the `grep` tool
- Optional (for memory tools): `make local-start` + `make memory-build` at the repo root. Without these, the `mcp__tree-memory__*` tools won't be available, but native tools (bash/read/write/edit/...) still work.

### Shared environment

The harness reads the repo-root `.env` (only `GOOGLE_API_KEY` is required) and loads MCP servers from the repo-root `.mcp.json`. The default config ships one server, `tree-memory`, spawned on demand via `uv --directory apps/memory run python scripts/serve_mcp.py`. Add more servers to `.mcp.json` and the harness will pick them up on the next run.

## Quick start

```bash
# install deps
make harness-install

# one-shot CLI (answer comes from the model alone)
PROMPT="what is 2+2?" make harness-run

# interactive Ink REPL (requires a TTY)
make harness-dev

# end-to-end: memory + harness
make local-start                                       # shared infra (from repo root)
make memory-run-all-data-pipelines                     # ingest some data
make memory-run-memory-pipeline-extraction             # extract the graph
make memory-run-memory-pipeline-indexing               # index it
PROMPT="search my memory for knowledge-graph notes" make harness-run
```

In the last command the harness auto-spawns the `tree-memory` MCP server from `.mcp.json` and exposes its six tools (`query_memory`, `search_memory`, `deep_search_memory`, `ingest_url`, `ingest_file`, `ingest_conversation`) to the model.

## Modes

| Invocation | What it does |
|---|---|
| `PROMPT="..." make harness-run` | CLI mode — streams the response to stdout, exits. |
| `make harness-run` (no PROMPT, TTY) | Interactive Ink REPL. |
| `ARGS="--resume" make harness-run` | Lists recent sessions for this cwd and exits. |
| `ARGS="--resume <id-prefix>" PROMPT="..." make harness-run` | Resumes that session, appends a new turn. |
| `ARGS="--continue" PROMPT="..." make harness-run` | Resumes the most-recent session for this cwd. |
| `ARGS="--no-mcp" make harness-run` | Skip the MCP bootstrap (useful when infra is down). |

`ARGS=` passes flags through; `PROMPT=` is the one-shot prompt. They compose.

## Slash commands (interactive REPL)

| Command | Effect |
|---|---|
| `/help` | List all slash commands |
| `/clear` | Reset the in-memory conversation. The session JSONL file is kept; new turns append to it. |
| `/resume` | Print recent sessions with prompt hint (restart with `ARGS="--resume <id>"`) |

## Tools

Native tools (always available): `bash`, `read`, `write`, `edit`, `glob`, `grep`, `todo`, `task`.

MCP tools (on startup) are discovered from the root `.mcp.json`. Each shows up as `mcp__<server>__<name>`; for the default config that means the six `tree-memory` tools:
`query_memory`, `search_memory`, `deep_search_memory`, `ingest_url`, `ingest_file`, `ingest_conversation`.

### Destructive-tool gating

Destructive tools (anything that writes, or that matches the ingest/write/create/delete/… heuristic for MCP tools) go through a permission check. In the Ink REPL a yellow-bordered dialog asks `[y]` once, `[a]` allow this pattern (e.g. `bash:git ` ), `[n]` deny. Patterns last the session. In `--print` mode destructive tools are auto-allowed with `source: "cli-auto"` logged.

## Sessions — JSONL transcripts

Every session writes to `~/.tree/projects/<cwd-hash>/<session-id>.jsonl`. Each line is one of:
- `{ kind: "meta", ts, cwd, sessionId }`
- `{ kind: "message", ts, role, content }` — verbatim from the loop
- `{ kind: "event", ts, name, data }` — permissions, hooks, resumed markers, subagent lifecycle

Sub-agents get nested files at `~/.tree/projects/<cwd-hash>/<parent-id>/<subagent-id>.jsonl`. The `subagent_start` / `subagent_end` events bracket each run with type, description, depth, and final stats.

## Sub-agents

The `task` tool spawns a sub-agent — a recursive call into the same loop with a narrowed tool set and fresh conversation. Three types:

| Type | Tools | Can spawn? |
|---|---|---|
| `general` | all | yes (depth ≤ 2) |
| `explore` | read, glob, grep, todo + read-only MCP tools | no |
| `plan` | read, glob, grep | no |

Limits per sub-agent: **depth ≤ 2**, **5-minute wall-clock**, **30 tool calls**, plus any parent `AbortSignal` propagates down.

## Hooks — shell extensibility

Tree runs shell-exec hooks at four points, configured via `settings.json`:

```json
{
  "hooks": {
    "PreToolUse":       [ { "matcher": "bash:rm ",      "command": "./scripts/deny-rm.sh" } ],
    "PostToolUse":      [ { "matcher": "edit:./src/",   "command": "./scripts/audit-edit.sh" } ],
    "UserPromptSubmit": [ { "command": "./scripts/rewrite-prompt.sh" } ],
    "Stop":             [ { "command": "echo 'session ended' >> ~/tree-activity.log" } ]
  }
}
```

### Protocol

- Hook stdin receives a JSON context. For `PreToolUse`: `{ event, tool, input }`. For `PostToolUse`: adds `result`. For `UserPromptSubmit`: `{ event, prompt }`. For `Stop`: `{ event, reason }`.
- Hook stdout is optionally JSON: `{ "decision": "block", "reason": "..." }` blocks the call; `{ "prompt": "..." }` on `UserPromptSubmit` rewrites the prompt.
- Non-zero exit = block (same as `"decision": "block"`).
- Matcher syntax matches the permission-rule DSL: `"toolName"` or `"toolName:prefix"`. Omit the matcher on `UserPromptSubmit` / `Stop`.
- Each fire is logged as a `{ kind: "event", name: "hook", data: { event, tool, command, exitCode, decision } }` line in the session JSONL.

### Settings merge order

Project (`./.tree/settings.json`) runs first, then user (`~/.tree/settings.json`). Within an event the arrays concatenate, preserving order. No inheritance magic — keep it simple.

### Hook timeout

Each hook has a 5-second wall-clock. Kill signal on timeout; a timed-out hook is treated as a block.

## Layout

```
apps/harness/
  src/
    index.tsx         # bin entry — argv, modes, Ink vs CLI
    app.tsx           # top-level Ink component
    agent/
      loop.ts         # the async-generator agent loop
      subagents.ts    # task registry + spawner factory
    tools/
      types.ts        # Tool + ToolContext + subagent types
      registry.ts     # built-in tools + Gemini tool-schema bridge
      bash/read/write/edit/glob/grep/todo/task.ts
    mcp/
      config.ts       # load root .mcp.json
      client.ts       # stdio transport + tool discovery
      adapter.ts      # wrap MCP tools as harness Tools
    session/          # paths.ts + store.ts + resume.ts
    permissions/      # policy.ts + prompt.tsx
    hooks/            # config.ts + runner.ts
    ui/               # Message/ToolCall/Input/Spinner/Markdown/AgentProgress + slash.ts
    messages.ts       # neutral type vocabulary (Role/ContentBlock/Message/StreamEvent)
    client.ts         # Gemini SDK wrapper (streamText generator)
```

## Make targets (from repo root)

| Target | Effect |
|---|---|
| `make harness-install` | `bun install` |
| `make harness-dev` | `bun --watch` with optional `ARGS=` |
| `make harness-run` | one-shot; set `PROMPT=` and/or `ARGS=` |
| `make harness-typecheck` | `bun tsc --noEmit` |
| `make harness-tests` / `unit-tests` / `integration-tests` | `bun test` across unit + integration suites (128 tests) |
| `make harness-format-fix` / `format-check` | Biome format |
| `make harness-lint-fix` / `lint-check` | Biome check (format + lint + organize-imports) |
| `make harness-build` | single-binary compile to `dist/tree` |
