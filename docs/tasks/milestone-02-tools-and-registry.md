# Milestone 2 — Tool Calling + Registry

## Goal

Add seven native tools (`bash`, `read`, `write`, `edit`, `glob`, `grep`, `todo`) behind a registry, and refactor the agent loop into an async generator. Still CLI-only output — Ink comes at M3.

## Depends on

M1 scaffold (`package.json`, `tsconfig.json`, `biome.json`, `Makefile`, `src/messages.ts`, `src/client.ts`).

## Files

New:
- `src/tools/types.ts` — `Tool<Input, Output>`, `ToolContext`, `ToolResult` types. ~30 lines.
- `src/tools/registry.ts` — `Map<string, Tool>`; exports both the neutral `FunctionDeclaration[]` and a converter to Gemini's `Tool` shape. ~50 lines.
- `src/tools/bash.ts` — shell exec via `Bun.$`, timeout + captured stdout/stderr. Marked destructive. ~40 lines.
- `src/tools/read.ts` — file reader with optional offset/limit. Read-only. ~25 lines.
- `src/tools/write.ts` — overwrite file; destructive. ~20 lines.
- `src/tools/edit.ts` — literal string replace; destructive. ~35 lines.
- `src/tools/glob.ts` — fast glob via `Bun.Glob`. Read-only. ~20 lines.
- `src/tools/grep.ts` — pattern search via `ripgrep` (fallback to native). Read-only. ~30 lines.
- `src/tools/todo.ts` — in-memory todo list (planning primitive). ~30 lines.
- `src/agent/loop.ts` — async generator yielding `assistant_text | tool_use | tool_result | done | error`. ~150 lines, distilled from Claude Code's `src/query.ts` (read-only reference at `/Users/pauliusztin/Documents/01-Projects/claude-code-leaks/claude-code-1/src/query.ts`).

Modified:
- `src/client.ts` — extend the `streamText` wrapper to pass `tools` (Gemini `FunctionDeclaration[]`) and emit `tool_use` events when the model returns a `functionCall` part.
- `src/index.ts` — switch from direct `streamText` call to consuming `loop()`.

## New dependencies

- `zod` — tool input schemas; also used to generate JSON Schema for Gemini `FunctionDeclaration.parameters`.

## LOC budget

+~250 (cumulative ~400).

## Verification

1. `make harness-typecheck && make harness-lint && make harness-format-check` — all pass.
2. `PROMPT="list files in apps/memory/src/tree" make harness-run` — invokes `glob`, returns a list.
3. `PROMPT="read apps/memory/pyproject.toml and tell me the package name" make harness-run` — invokes `read`, answer mentions `tree-memory`.
4. Destructive tools log intent (no permission dialog yet — that's M4).
5. Loop terminates when the model emits a response with no `functionCall` part.

## Out of scope

- Permission prompts — M4.
- Tool output rendering in Ink — M3.
- MCP-backed tools — M5.
