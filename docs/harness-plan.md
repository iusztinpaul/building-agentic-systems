# Tree — Coding-Agent Harness Design Plan

**Tree: Your Rooted Personal Assistant** — a minimal TypeScript coding agent (the `tree` CLI) that pairs with the `tree-memory` MCP server to demonstrate how a harness is actually built. It's the agent half of Tree; `apps/memory/` is the rooted-memory half.

Target: **~2,000 lines of legible TypeScript.** Optimized for teaching, not feature parity.

## Current repository state

The monorepo restructure has landed. The harness placeholder is `apps/harness/.gitkeep` — everything below goes inside `apps/harness/`. Supporting infra is already wired up:

- **Root `Makefile`** delegates: `harness-%: $(MAKE) -C apps/harness $*`. The moment `apps/harness/Makefile` exists, every `make harness-<target>` from the repo root works.
- **`.mcp.json`** registers the `tree-memory` server, spawned via `uv --directory apps/memory run python scripts/serve_mcp.py` with `ENV_FILE_PATH=../../.env`. The harness's MCP client reads this verbatim.
- **`.gitignore`** excludes `apps/harness/node_modules/`, `apps/harness/dist/`, `apps/harness/.bun/`.
- **`.gitattributes`** marks `apps/harness/bun.lock` and `apps/harness/package-lock.json` as `linguist-generated=true`.
- **`.editorconfig`** enforces 2-space indent for `.ts/.tsx/.js/.json/.md/.yaml/.toml`; tabs for `Makefile`.
- **`docs/modern-typescript-stack.md`** is the authoritative stack reference: Bun + Biome + `tsc`, no Turborepo at this size.
- **`CONTRIBUTING.md`** documents the dev workflow.

What doesn't exist yet in `apps/harness/`: `package.json`, `tsconfig.json`, `biome.json`, `bunfig.toml`, `Makefile`, `README.md`, `src/`. Milestone 1 adds the scaffold; subsequent milestones fill in `src/`.

## References studied

- **Claude Code leak (TS + Bun + custom react-reconciler TUI):** single-process loop; async-generator agent loop; Anthropic SDK; JSONL transcripts; real lifecycle hooks; sub-agents.
- **OpenCode (TS + Bun + OpenTUI/Solid.js):** server/client split over HTTP+WS; Effect.js DI; Vercel AI SDK; SQLite via Drizzle.

**Decision:** adopt Claude Code's single-process shape. OpenCode's split is architecture overhead that obscures the teaching point.

## Confirmed decisions

1. **Stack:** TypeScript + Ink + Bun + Biome + `tsc` (per `docs/modern-typescript-stack.md`).
2. **Layout:** flat single TS package at `apps/harness/`; **not** Bun workspaces. Same idiom as Claude Code.
3. **Sessions:** JSONL transcripts under `~/.tree/projects/<cwd-hash>/<session-id>.jsonl`, not SQLite.
4. **Sub-agent tools:** included — a recursive call into the same loop with narrowed context.

The existing `tree-memory` MCP server at `apps/memory/scripts/serve_mcp.py` stays unchanged — the harness just spawns it over stdio.

## Architecture

### Layout

```
apps/harness/
  package.json       # "name": "tree"
  tsconfig.json
  biome.json
  bunfig.toml
  Makefile
  README.md
  src/
    index.tsx        # bin entry — argv, loads settings, mounts <App/>
    app.tsx          # top-level Ink component
    agent/
      loop.ts        # THE STAR — async generator: prompt → stream → tools → repeat
      messages.ts    # Message / ContentBlock types
      client.ts      # thin Anthropic SDK wrapper (streaming)
      prompt.ts      # system prompt assembly (tools, cwd, date)
    tools/
      types.ts       # Tool<Input, Output> + ToolContext
      registry.ts    # name -> Tool map
      bash.ts  read.ts  write.ts  edit.ts  glob.ts  grep.ts
      task.ts        # sub-agent tool (see below)
      todo.ts        # in-memory todo list
    mcp/
      client.ts      # stdio MCP client via @modelcontextprotocol/sdk
      adapter.ts     # wraps each MCP tool as a harness Tool
      config.ts      # loads root .mcp.json
    ui/              # Message/ToolCall/AgentProgress/Input/Spinner/Markdown
    session/         # store.ts / paths.ts / resume.ts → ~/.tree/projects/…
    permissions/     # policy.ts + prompt.tsx
    hooks/           # runner.ts + config.ts
    util/            # log / errors / shortid
```

**Why flat, not Bun workspaces:** for a ~2,000-line teaching codebase, separation belongs in folders, not build graphs. Claude Code itself ships as one package.

### Agent loop

`apps/harness/src/agent/loop.ts` — one async generator. Yields typed events:

```
assistant_text | tool_use | tool_result | done | error
```

`app.tsx` consumes the generator and drives Ink. The teaching payoff: the loop is identical whether the consumer is CLI (`--print`) or TUI. Distilled from Claude Code's `src/query.ts` (~1,730 lines → ~200 here). Preserves: streaming state machine, partial tool-call JSON assembly, abort via `AbortSignal`, permission callout, termination when the model stops calling tools.

### Tool system

Each tool is one file exporting `{ name, description, schema (zod), call(input, ctx), isReadOnly?, isDestructive? }`. `registry.ts` builds a `Map<string, Tool>`. Destructive tools trigger a permission prompt; read-only tools do not.

Native v1 set: `bash`, `read`, `write`, `edit`, `glob`, `grep`, `task`, `todo`. MCP-discovered tools register alongside with **`mcp__tree-memory__<tool>`** namespacing (the prefix matches the server ID in `.mcp.json`).

### Sub-agents (`task` tool)

**A sub-agent is a recursive call to the same loop with narrowed context.** No separate runtime, no separate prompt machinery.

- **MCP connections:** shared via `ToolContext`. Avoids duplicate Python processes.
- **Transcript:** fresh; persisted at `~/.tree/projects/<hash>/<parent-session>/<subagent-id>.jsonl`.
- **Registered agent types:**
  - `general` — full tool access (including `task` for one more level of depth)
  - `explore` — read-only: `read`, `glob`, `grep`, `bash` (read-only commands)
  - `plan` — design-only: `read`, `glob`, `grep`
- **Tool schema:** `{ subagent_type, description, prompt }`. No per-call tool overrides in v1.
- **UI streaming:** parent holds `Map<subagentId, SubagentState>`; each event updates an indented `<AgentProgress/>` block. Final summary replaces the block.
- **Return to parent LLM:**
  ```ts
  type SubagentResult = {
    summary: string;
    tool_uses: number;
    duration_ms: number;
    subagent_id: string;
  };
  ```
  Only `summary` lands in the parent's `tool_result`.
- **Recursion:** allowed for `general` only, **max depth 2**. Enforced via `depth` on `ToolContext`.
- **Limits:** 5-min wall-clock timeout (`AbortSignal`), 30 tool calls per sub-agent, cumulative token budget, depth 2.

### Session persistence — JSONL

One file per session at `~/.tree/projects/<cwd-hash>/<session-id>.jsonl`, appended after every message. `--resume` lists recent sessions; `--continue` picks the most recent for the current cwd.

### Permissions

Three modes (`default` / `auto-accept` / `bypass`) + allow/deny rules (e.g., `Bash(git:*)`, `Edit(./src/**)`). Stored in `~/.tree/settings.json` (user) and `./.tree/settings.json` (project override — `.gitignore` locally if it contains secrets). Permission prompt is an Ink dialog surfaced inline in the message stream.

### Hooks

Shell-executed hooks configured in `settings.json`:
- `PreToolUse` — can deny a tool call via non-zero exit
- `PostToolUse` — observation only
- `UserPromptSubmit` — can mutate the prompt via stdout JSON
- `Stop` — cleanup

Exit code + JSON stdout contract — same as Claude Code.

## Makefile integration

Root `Makefile` already has `harness-%: $(MAKE) -C apps/harness $*`. The app's own Makefile:

```make
# apps/harness/Makefile

install:
	bun install

dev:
	bun --watch run src/index.tsx

run:
	bun run src/index.tsx $(if $(PROMPT),--print "$(PROMPT)",)

typecheck:
	bun tsc --noEmit

test:
	bun test

format:
	bunx biome format --write src

lint:
	bunx biome check src

build:
	bun build src/index.tsx --compile --outfile dist/tree

clean:
	rm -rf dist node_modules
```

Shared root `.env` is propagated — `ANTHROPIC_API_KEY` lives in the root `.env` (added to `.env.example` when Milestone 1 lands) and is read by the harness like `MONGO_*` is read by memory.

## Integration with existing repo plumbing

Land alongside the relevant milestones:

- **Root `tests:` aggregate** — extend at Milestone 2:
  ```make
  tests:
  	$(MAKE) memory-tests
  	$(MAKE) harness-test
  ```
- **Root `pre-commit`** — currently Python-only. Keep Biome out of the hook chain; run `make harness-format` / `make harness-lint` locally and in CI.
- **CI** — add a parallel `harness` job with `working-directory: apps/harness`: `oven-sh/setup-bun`, `bun install`, `bunx biome check src`, `bun tsc --noEmit`, `bun test`.
- **`.env.example`** — add `ANTHROPIC_API_KEY=your-anthropic-api-key` at Milestone 1.
- **`.git-blame-ignore-revs`** — append the Milestone 1 scaffold commit hash.

## Milestones — 7 chapters, ~2,000 cumulative lines

1. **Walking skeleton.** One file; no Ink; no tools. CLI takes a prompt, calls Claude with streaming, prints text. Defines `Message` / `ContentBlock` types. Adds the `apps/harness/` scaffold (`package.json`, `tsconfig.json`, `biome.json`, `bunfig.toml`, `Makefile`, `README.md`). ~150 lines.
2. **Tool calling + registry.** `bash`, `read`, `write`, `edit`, `glob`, `grep`. Agent loop as async generator. Still CLI-only. ~400 cumulative.
3. **Ink TUI.** Port the same loop into `<App/>`. Message list, input line, markdown, spinner. The loop is unchanged. ~800 cumulative.
4. **Permissions + JSON sessions.** Permission prompts for destructive tools; JSONL writer under `~/.tree/projects/`; `--resume` / `--continue`. ~1,200 cumulative.
5. **MCP client + Tree memory integration.** Stdio MCP client, reads root `.mcp.json`, adapter registers MCP tools. First demo: the TS harness calls the Python FastMCP tools via the `tree-memory` server (tools surface as `mcp__tree-memory__*`). ~1,500 cumulative.
6. **Sub-agents (`task` tool).** Recursive loop reuse, narrowed tools, fresh transcript. Inline streaming. Depth 2 + 5-min timeout + 30-call cap. ~1,800 cumulative.
7. **Hooks + polish.** `PreToolUse` / `PostToolUse` / `UserPromptSubmit` / `Stop`. Slash commands (`/help`, `/clear`, `/resume`). Error surfacing. README. ~2,000 cumulative.

**Skills deferred to an appendix.** The "extensibility without changing the harness" lesson is delivered by hooks + MCP with less code.

## Critical reference files (Claude Code leak)

Paths under `/Users/pauliusztin/Documents/01-Projects/claude-code-leaks/claude-code-1/` (machine-local):

- `src/query.ts` — agent-loop state machine.
- `src/Tool.ts` — tool interface contract.
- `src/tools/AgentTool/builtInAgents.ts` — sub-agent registry shape.
- `src/tools/AgentTool/runAgent.ts` — sub-agent lifecycle.
- `src/tools/AgentTool/AgentTool.tsx` — sub-agent UI streaming.
- `src/services/mcp/client.ts` + `src/tools/MCPTool/MCPTool.ts` — MCP client + adapter.
- `src/history.ts` — JSONL transcript format, `--resume`.
- `src/entrypoints/cli.tsx` — argv / setup reference.

In this repo (do **not** modify from the harness side):

- `apps/memory/scripts/serve_mcp.py` — MCP stdio entrypoint the harness spawns.
- `.mcp.json` — `mcpServers` config shape (note `ENV_FILE_PATH=../../.env` passthrough relative to `apps/memory/`).
- `apps/memory/src/tree/memory/` — the knowledge-graph memory module whose tools surface through MCP.

## Verification plan

Per milestone:

1. **Skeleton:** `make harness-install && PROMPT="hello" make harness-run` streams a Claude response.
2. **Tools:** `PROMPT="list files in apps/memory/src/tree" make harness-run` uses `glob` and returns a list.
3. **Ink TUI:** `make harness-dev` — type a prompt, see message history.
4. **Permissions + sessions:** destructive bash fires permission dialog; `~/.tree/projects/*/*.jsonl` exists; `--resume` lists the session.
5. **MCP:** `make local-start` (MongoDB + mongot + Prefect), populate the graph (`make memory-run-data-pipeline` → `make memory-run-memory-pipeline-extraction` → `make memory-run-memory-pipeline-indexing`), then `make harness-dev` → ask a memory question → verify a `mcp__tree-memory__*` tool is called.
6. **Sub-agents:** parent asked to "use an `explore` sub-agent to summarize `apps/memory/src/tree/memory/`" — verify nested UI block, fresh sub-agent transcript file, final summary stitched into parent.
7. **Hooks:** add a `PreToolUse` hook in `./.tree/settings.json` that blocks `bash(rm:*)`; verify denial.

Cross-cutting: `make harness-lint`, `make harness-format`, `make harness-typecheck` (`bun tsc --noEmit`), `make harness-test` all pass. `make harness-build` produces `apps/harness/dist/tree`. Root aggregates updated once the harness lands.
