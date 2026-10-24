# Tree — Coding-Agent Harness

The agent half of **Tree: Your Rooted Personal Assistant**, pairing with the `tree-memory` MCP server in `apps/memory/`.

The harness is being rewritten from scratch. Only the scaffolding is kept: the Bun + TypeScript project (`package.json`, `tsconfig.json`, `biome.json`), the per-app `Makefile` behind the root `make harness-*` targets, a stub entry at `src/index.ts`, and a smoke test under `tests/unit/` so the CI and pre-commit wiring stay green.

```bash
make harness-install
make harness-run
make harness-tests
```
