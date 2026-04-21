# Minimal Coding-Agent Harness — Design Plan

## Context

Companion to the Twin memory MCP server (`apps/memory`). The harness is a minimal TypeScript coding agent — the kind of thing Claude Code and OpenCode are, stripped down to the core ideas — that connects to the Twin memory MCP and demonstrates how a harness is actually built. It's the other half of the book: memory on one side, the agent that queries it on the other.

Target: **~2,000 lines of legible TypeScript.** Optimized for teaching, not feature parity.

## Reference harnesses studied

- **Claude Code (leaked source, TS + Bun + custom react-reconciler TUI):** single-process model; agent loop as one async generator; direct Anthropic SDK; JSONL session transcripts; real lifecycle hooks; rich `AgentTool` sub-agents.
- **OpenCode (TS + Bun + OpenTUI/Solid.js):** server/client split over HTTP+WS; Effect.js dependency injection; Vercel AI SDK multi-provider; SQLite via Drizzle.

**Decision:** adopt Claude Code's single-process shape. OpenCode's client/server split is architecture overhead that obscures the ideas the book is teaching.

## Confirmed decisions

1. **Stack:** TypeScript + Ink + Bun — same family as the reference harnesses; rich ecosystem; Ink is React for the terminal.
2. **Layout:** monorepo. Harness lives at `apps/harness/` alongside `apps/memory/`.
3. **Sessions:** JSON transcripts (Claude Code style), not SQLite.
4. **Sub-agent tools:** included — a recursive call into the same loop with narrowed context.

The existing Twin memory MCP server at `apps/memory/scripts/serve_mcp.py` stays unchanged. The harness just spawns it over stdio.

## Architecture

### Layout

`apps/harness/` is a single flat TS package (not Bun workspaces):

```
apps/harness/
  package.json
  tsconfig.json
  biome.json
  bunfig.toml
  README.md
  src/
    index.tsx              # bin entry — argv, loads settings, mounts <App/>
    app.tsx                # top-level Ink component (message list + input)
    agent/
      loop.ts              # THE STAR — async generator: prompt → stream → tools → repeat
      messages.ts          # Message / ContentBlock types + reducers
      client.ts            # thin Anthropic SDK wrapper (streaming)
      prompt.ts            # system prompt assembly (tools, cwd, date)
    tools/
      types.ts             # Tool<Input, Output> + ToolContext
      registry.ts          # name -> Tool map; schema export for the API
      bash.ts
      read.ts
      write.ts
      edit.ts
      glob.ts
      grep.ts
      task.ts              # sub-agent tool (see below)
      todo.ts              # in-memory todo list (planning primitive)
    mcp/
      client.ts            # stdio MCP client via @modelcontextprotocol/sdk
      adapter.ts           # wraps each MCP tool as a harness Tool
      config.ts            # loads .mcp.json
    ui/
      Message.tsx
      ToolCall.tsx         # collapsible tool invocation block
      AgentProgress.tsx    # inline sub-agent progress block
      Input.tsx            # prompt line + slash command parser
      Spinner.tsx
      Markdown.tsx         # tiny markdown -> Ink Text renderer
    session/
      store.ts             # JSONL transcript writer
      paths.ts             # ~/.harness/projects/<hash>/<session-id>.jsonl
      resume.ts            # --resume / --continue
    permissions/
      policy.ts            # allow / deny / ask per tool call
      prompt.tsx           # Ink permission dialog
    hooks/
      runner.ts            # shell-exec PreToolUse / PostToolUse / Stop / UserPromptSubmit
      config.ts            # reads settings.json hooks block
    util/
      log.ts
      errors.ts
      shortid.ts
```

**Why flat, not Bun workspaces:** workspaces add a root `package.json`, `packages/*`, cross-workspace imports, and a mental model readers must absorb before they see an agent loop. For a ~2,000-line teaching codebase, separation belongs in folders, not build graphs. Claude Code itself ships as one package.

### Agent loop

`apps/harness/src/agent/loop.ts` — a single async generator. Yields typed events:

```
assistant_text | tool_use | tool_result | done | error
```

`app.tsx` consumes the generator and drives Ink state. The teaching payoff: the loop is identical whether the consumer is CLI (`--print`) or TUI.

Distilled from Claude Code's `src/query.ts` (1,730 lines → ~200 lines here). Preserves: streaming state machine, partial tool-call JSON assembly, abort via `AbortSignal`, permission check callout, termination when the model stops calling tools.

### Tool system

Each tool is one file exporting `{ name, description, schema (zod), call(input, ctx), isReadOnly?, isDestructive? }`. `registry.ts` builds a `Map<string, Tool>`. Destructive tools trigger a permission prompt; read-only tools do not.

Native v1 set: `bash`, `read`, `write`, `edit`, `glob`, `grep`, `task`, `todo`. MCP-discovered tools register alongside with `mcp__<server>__<tool>` namespacing.

### Sub-agents (`task` tool)

Core principle: **a sub-agent is a recursive call to the same loop with narrowed context.** No separate runtime, no separate prompt machinery — reusing the loop is the lesson.

- **MCP connections:** shared. Sub-agents receive the parent's MCP client handles via `ToolContext`. Avoids spawning duplicate Python processes and cuts latency.
- **Transcript:** fresh. Each sub-agent starts with its own `Message[]` containing its system prompt + the parent-supplied `prompt`. Persisted separately at `~/.harness/projects/<hash>/<parent-session>/<subagent-id>.jsonl` for debugging but not stitched into parent history.
- **Registered agent types** (explicit registry, not per-call overrides):
  - `general` — full tool access (including `task` for one more level of depth)
  - `explore` — read-only: `read`, `glob`, `grep`, `bash` (read-only commands)
  - `plan` — design-only: `read`, `glob`, `grep`
- **Tool schema:** `{ subagent_type, description, prompt }`. Per-call tool overrides explicitly out of v1 — keeps the API surface teachable.
- **Streaming to parent UI:** parent holds `Map<subagentId, SubagentState>`; each yielded sub-agent event updates an indented collapsible `<AgentProgress/>` block. Final message replaces the block with the summary.
- **Return value to parent LLM:**
  ```ts
  type SubagentResult = {
    summary: string;
    tool_uses: number;
    duration_ms: number;
    subagent_id: string;
  };
  ```
  Only `summary` lands in the parent's `tool_result`; the rest feeds UI / logs.
- **Recursion:** allowed for `general` only, **max depth 2**. `explore` and `plan` cannot spawn sub-agents. Enforced via a `depth` counter on `ToolContext`.
- **Limits (single `SubagentLimits` object):** 5-minute wall-clock timeout via `AbortSignal`, max 30 tool calls per sub-agent, cumulative token budget passed through, max depth 2.

### Session persistence — JSONL

Claude Code style. One file per session at `~/.harness/projects/<cwd-hash>/<session-id>.jsonl`, appended after every message. `--resume` lists recent sessions; `--continue` picks the most recent for the current cwd.

### Permissions

Three modes (`default` / `auto-accept` / `bypass`) plus allow / deny rules matched by pattern (e.g., `Bash(git:*)`, `Edit(./src/**)`). Stored in `~/.harness/settings.json` and `./.harness/settings.json` (project override). Permission prompt is an Ink dialog surfaced inline in the message stream.

### Hooks

Shell-executed hooks configured in `settings.json`:

- `PreToolUse` — can deny a tool call via non-zero exit
- `PostToolUse` — observation only
- `UserPromptSubmit` — can mutate the prompt via stdout JSON
- `Stop` — cleanup

Exit code + JSON stdout contract — same as Claude Code.

## Makefile integration

Delegation pattern: root `Makefile` has `harness-%: $(MAKE) -C apps/harness $*`. The app's own Makefile contains the bun-specific targets:

```make
# apps/harness/Makefile

install:
	bun install

dev:
	bun --watch run src/index.tsx

run:
	bun run src/index.tsx $(if $(PROMPT),--print "$(PROMPT)",)

build:
	bun build src/index.tsx --compile --outfile dist/harness

test:
	bun test

format:
	bunx biome format --write src

lint:
	bunx biome check src

clean:
	rm -rf dist node_modules
```

`make harness-install`, `make harness-dev`, `make harness-run PROMPT="..."`, etc., all work from the repo root via the delegation target.

Shared root `.env` is propagated — `ANTHROPIC_API_KEY` from the root `.env` is read by the harness just like `MONGO_*` is read by memory.

## Milestones — 7 chapters, ~2,000 cumulative lines

1. **Walking skeleton.** One file; no Ink; no tools. CLI takes a prompt, calls Claude with streaming, prints text. Defines `Message` / `ContentBlock` types used everywhere after. ~150 lines.
2. **Tool calling + registry.** Add `bash`, `read`, `write`, `edit`, `glob`, `grep`. Agent loop as async generator. Still CLI-only output. ~400 cumulative.
3. **Ink TUI.** Port the same loop into `<App/>`. Message list, input line, markdown, spinner. Payoff: the loop is unchanged; only the consumer differs. ~800 cumulative.
4. **Permissions + JSON sessions.** Permission prompts for destructive tools; JSONL transcript writer; `--resume` / `--continue`. Merged because each is under-taught alone. ~1,200 cumulative.
5. **MCP client + Twin memory integration.** Stdio MCP client, `.mcp.json`, adapter registers MCP tools. First end-to-end demo: the TS harness calls the existing Python FastMCP tools. ~1,500 cumulative.
6. **Sub-agents (`task` tool).** Recursive loop reuse with narrowed tools and fresh transcript. Inline streaming to parent UI. Depth limit, timeouts, structured return. Placed here because it stress-tests every subsystem already built. ~1,800 cumulative.
7. **Hooks + polish.** `PreToolUse` / `PostToolUse` / `UserPromptSubmit` / `Stop` hooks. Slash commands (`/help`, `/clear`, `/resume`). Error surfacing. README. ~2,000 cumulative.

**Skills deferred to an appendix.** The same "extensibility without changing the harness" lesson is delivered by hooks + MCP with less code. Skills can be added later as a loader + `SkillTool` without churning anything else.

## Critical reference files (from the Claude Code leak)

Keep these open while implementing. Paths under `/Users/pauliusztin/Documents/01-Projects/claude-code-leaks/claude-code-1/`:

- `src/query.ts` — distill the agent-loop state machine.
- `src/Tool.ts` — tool interface contract (minimum viable version).
- `src/tools/AgentTool/builtInAgents.ts` — sub-agent registry shape.
- `src/tools/AgentTool/runAgent.ts` — sub-agent lifecycle.
- `src/tools/AgentTool/AgentTool.tsx` — wiring sub-agent streaming into the UI.
- `src/services/mcp/client.ts` + `src/tools/MCPTool/MCPTool.ts` — MCP client + adapter.
- `src/history.ts` — JSONL transcript format and `--resume` semantics.
- `src/entrypoints/cli.tsx` — argv / setup reference for `apps/harness/src/index.tsx`.

In this repo (do NOT modify from the harness side):

- `apps/memory/scripts/serve_mcp.py` — MCP stdio entrypoint the harness spawns.
- `.mcp.json` — reference for the `mcpServers` config shape.

## Verification plan

Per milestone:

1. **Skeleton:** `make harness-install && PROMPT="hello" make harness-run` streams a Claude response.
2. **Tools:** `PROMPT="list files in apps/memory/src/twin" make harness-run` uses `glob` and returns a list.
3. **Ink TUI:** `make harness-dev` — interactive REPL; type a prompt, see message history.
4. **Permissions + sessions:** prompt for a destructive bash; permission dialog fires; `~/.harness/projects/*/*.jsonl` exists; `--resume` shows the session.
5. **MCP:** with `make local-start` running MongoDB, `make harness-dev` → ask a memory question → verify a Twin-memory MCP tool is called (prefixed `mcp__twin-memory__...`).
6. **Sub-agents:** parent is asked to "use an `explore` sub-agent to summarize the memory module" — verify nested UI block, fresh sub-agent transcript file, final summary stitched into parent.
7. **Hooks:** add a `PreToolUse` hook in `settings.json` that blocks `bash(rm:*)`; verify the tool call is denied.

Cross-cutting:

- `make harness-lint` and `make harness-format` pass.
- `make harness-test` passes unit tests for: loop state machine, tool registry, permission matcher, JSONL writer, sub-agent depth enforcement.
- `make harness-build` produces a single `apps/harness/dist/harness` executable.
