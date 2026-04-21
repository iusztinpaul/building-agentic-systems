# Milestone 6 — Sub-agents (`task` tool)

## Goal

`task` tool = a recursive call into the same `loop()` with narrowed context. Three registered sub-agent types, depth cap 2, 5-min wall-clock, 30-call cap, shared MCP handles, fresh JSONL transcript per sub-agent.

## Depends on

M2 (loop), M4 (session store), M5 (shared MCP handles).

## Files

New:
- `src/tools/task.ts` — the `task` tool. Input schema `{ subagent_type, description, prompt }`; output `SubagentResult`. ~80 lines.
- `src/agent/subagents.ts` — registry of agent types (`general`/`explore`/`plan`), narrowed tool-set per type, depth/limit enforcement. ~100 lines.
- `src/ui/AgentProgress.tsx` — indented collapsible block showing live sub-agent events. ~60 lines.

Modified:
- `src/agent/loop.ts` — accept `depth` on `ToolContext`; pass through to `task` tool.
- `src/session/paths.ts` — sub-agent transcripts nested at `<parent-session>/<subagent-id>.jsonl`.
- `src/app.tsx` — `Map<subagentId, SubagentState>` + `<AgentProgress/>` rendering.

## New dependencies

None.

## LOC budget

+~300 (cumulative ~1800).

## Verification

1. Parent prompted: "use an `explore` sub-agent to summarize `apps/memory/src/tree/memory/`".
2. Nested `<AgentProgress/>` block renders in the UI.
3. Fresh JSONL at `~/.tree/projects/<cwd-hash>/<parent-session>/<subagent-id>.jsonl`.
4. Final `summary` stitches back into parent as a `tool_result`.
5. An `explore` sub-agent asked to spawn another sub-agent returns "depth exceeded".
6. A sub-agent that hits 30 tool calls aborts cleanly.

## Out of scope

- Per-call tool overrides — explicitly deferred to keep API teachable.
- Parallel sub-agents — sequential only in v1.
