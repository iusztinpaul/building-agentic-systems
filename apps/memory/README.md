# Twin Memory

The memory half of the monorepo: ETL data pipelines, a knowledge-graph memory backed by MongoDB, and a FastMCP server that exposes memory tools to agents.

- Architecture, usage, and contribution instructions live in the repo-root [`README.md`](../../README.md) and [`CLAUDE.md`](../../CLAUDE.md).
- Harness (the agentic client that calls this memory) is planned separately — see [`docs/harness-plan.md`](../../docs/harness-plan.md).

Run targets from the repo root via the delegation Makefile, e.g. `make memory-build`, `make memory-tests`, `make memory-serve-mcp`. Inside this directory, `make help` lists the app-local targets.
