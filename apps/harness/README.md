# tree-agent — Coding-Agent Harness

The agent half of **Tree: Your Rooted Personal Assistant**, pairing with the `tree-memory` MCP server in `apps/memory/`.

`tree-agent` is being rewritten as a light pi clone on Node ≥ 22, TypeScript, `pnpm`, `vitest` and Biome. Today it is a project skeleton: `src/cli.ts` prints a usage line and exits 1.

```bash
corepack enable pnpm   # once; if `corepack --version` fails, first run `npm i -g corepack`
make harness-install
make harness-run
make harness-tests
```
