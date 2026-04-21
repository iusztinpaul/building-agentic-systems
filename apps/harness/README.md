# Tree — Coding-Agent Harness

The harness half of **Tree: Your Rooted Personal Assistant**. Minimal TypeScript coding agent (`tree` CLI) that pairs with the `tree-memory` MCP server in `apps/memory/`.

This is a walking skeleton at Milestone 1: argv → Gemini streaming → stdout. No tools, no TUI, no MCP yet. See [`../../docs/harness-plan.md`](../../docs/harness-plan.md) for the full architecture and the 7-milestone roadmap; per-milestone task files live under [`../../docs/tasks/`](../../docs/tasks/).

## Quick start

```bash
# Prereqs: Bun installed (`brew install bun` or `curl -fsSL https://bun.sh/install | bash`).
# And GOOGLE_API_KEY set in the root .env (shared with the memory app).

make harness-install
PROMPT="what is 2+2?" make harness-run
```

Run targets from the repo root via the delegation Makefile: `make harness-install`, `make harness-dev`, `make harness-run`, `make harness-typecheck`, `make harness-lint`, `make harness-format`. Inside this directory, `make help` lists app-local targets.

## Stack

Bun + Biome + `tsc` per [`../../docs/modern-typescript-stack.md`](../../docs/modern-typescript-stack.md). Orchestrator LLM: Gemini via `@google/genai`, reusing the existing `GOOGLE_API_KEY`. The loop is provider-isolated in `src/client.ts` so swapping to Anthropic/OpenAI later is localized.

## Layout

```
apps/harness/
  src/
    index.ts       # CLI entry: argv parse, load env, stream to stdout
    client.ts      # Gemini SDK wrapper (generator interface)
    messages.ts    # Role / ContentBlock / Message type vocabulary (reused by every milestone)
  package.json     # "name": "tree"
  tsconfig.json    # strict TS, ESNext, Bundler resolution, bun-types
  biome.json       # 2-space indent, double quotes, recommended rules
  bunfig.toml      # minimal Bun config
  Makefile         # app-local targets
```
