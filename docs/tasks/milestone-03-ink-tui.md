# Milestone 3 — Ink TUI

## Goal

Port the same `loop()` from M2 into an Ink `<App/>` component. Message list, input line, markdown rendering, spinner. The loop is unchanged; only the consumer differs — that's the teaching point.

## Depends on

M2 (`src/agent/loop.ts`, tool registry).

## Files

New:
- `src/app.tsx` — top-level Ink component. Manages message state, streams loop events. ~80 lines.
- `src/ui/Message.tsx` — user/assistant message block with role-coloured label. ~40 lines.
- `src/ui/ToolCall.tsx` — collapsible tool-invocation block with input/result. ~50 lines.
- `src/ui/Input.tsx` — prompt line with `ink-text-input`; parses slash commands (stubbed until M7). ~40 lines.
- `src/ui/Spinner.tsx` — status spinner during streaming. ~20 lines.
- `src/ui/Markdown.tsx` — tiny markdown → Ink `<Text>` renderer. ~60 lines.

Modified:
- `src/index.ts` — route to `<App/>` when no `--print`; fallback to CLI streaming for `--print`.

## New dependencies

- `ink`, `ink-text-input`, `react`, `@types/react`.

## LOC budget

+~400 (cumulative ~800).

## Verification

1. `make harness-typecheck && make harness-lint` — all pass.
2. `make harness-dev` — interactive REPL. Type a prompt; see streamed response.
3. Tool calls render as `<ToolCall/>` blocks, collapsible.
4. `PROMPT="hi" make harness-run` (CLI mode) still works — loop is consumed both ways.

## Out of scope

- Slash commands — M7.
- Permission dialog inline in message stream — M4.
- Sub-agent nested blocks — M6.
