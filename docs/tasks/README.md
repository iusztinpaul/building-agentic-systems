# Harness Milestones — Task Breakdown

One file per upcoming milestone of the Tree coding-agent harness (`apps/harness/`). Meant to be pickable-up cold: goal, dependencies, files, LOC budget, verification.

For architecture, prompt/tool design, and the overall ~2,000-line target, see [`../harness-plan.md`](../harness-plan.md). For stack rationale (Bun + Biome + tsc), see [`../modern-typescript-stack.md`](../modern-typescript-stack.md).

Milestone 1 (walking skeleton) is implemented directly in the scaffold — no task file for it. Each milestone below builds on the previous and is expected to land as one PR.

- [milestone-02-tools-and-registry.md](milestone-02-tools-and-registry.md) — 7 native tools + registry + agent loop as async generator.
- [milestone-03-ink-tui.md](milestone-03-ink-tui.md) — port the loop into `<App/>`.
- [milestone-04-permissions-and-sessions.md](milestone-04-permissions-and-sessions.md) — permission prompts + JSONL transcripts + `--resume` / `--continue`.
- [milestone-05-mcp-client.md](milestone-05-mcp-client.md) — stdio MCP client reading root `.mcp.json`; first `mcp__tree-memory__*` tool call.
- [milestone-06-subagents.md](milestone-06-subagents.md) — `task` tool = recursive loop call with narrowed context.
- [milestone-07-hooks-and-polish.md](milestone-07-hooks-and-polish.md) — shell-exec hooks + slash commands + README polish.

## Orchestrator LLM

M1 ships with **Gemini** (`@google/genai`) reusing the existing `GOOGLE_API_KEY` from the shared root `.env` — keeps the book-companion demo on a single API key. The agent loop (`src/agent/loop.ts` at M2) and `src/client.ts` isolate the provider so a later swap is localized.
