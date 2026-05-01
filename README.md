# Tree: Your Rooted Personal Assistant

Build a personal assistant rooted in a knowledge-graph memory, powered by ontologies, LLMs, and agents.

## What Tree is

Tree has two halves, wired together by [MCP](https://modelcontextprotocol.io/):

- **Memory** (`apps/memory/`) — a Python app that ingests documents from multiple sources (Substack, arXiv, files, conversations), extracts a knowledge graph with an LLM, indexes it for hybrid text + vector + graph search on MongoDB, and exposes the whole thing over a FastMCP server.
- **Harness** (`apps/harness/`) — a minimal TypeScript coding-agent (`tree` CLI, Bun + Ink) that spawns the memory's MCP server automatically and lets you query, explore, and write to the graph in natural language.

The goal: a personal assistant whose memory is a graph you own, queryable by any MCP-aware client (the bundled harness, Claude Code, Claude Desktop, Cursor, …).

## Repo layout

This is a monorepo. Each app owns its own build files; cross-app concerns stay at the root.

- `apps/memory/` — Python app (uv + Prefect + FastMCP). See [`apps/memory/README.md`](apps/memory/README.md).
- `apps/harness/` — TypeScript agent (Bun + Ink). See [`apps/harness/README.md`](apps/harness/README.md).
- `docker/` — shared infra (MongoDB replica-set config, `mongot` search config).
- `docker-compose.yml` — spins up MongoDB + mongot + Prefect server + a Prefect worker.
- `.mcp.json` — declares the `tree-memory` MCP server so the harness (and any other MCP client) can spawn it.
- `.env` — shared secrets and infra settings (see `.env.example`).
- `Makefile` — thin root that delegates per-app (`make memory-<t>` / `make harness-<t>`) and owns infra + aggregate targets.

## Prerequisites

**Required**

- [Python 3.14+](https://www.python.org/)
- [uv](https://docs.astral.sh/uv/) — Python package manager
- [Bun](https://bun.sh/) ≥ 1.1 — runtime for the harness (`brew install bun` or `curl -fsSL https://bun.sh/install | bash`)
- [Docker](https://www.docker.com/) and Docker Compose
- [GNU Make](https://www.gnu.org/software/make/)
- [mongosh](https://www.mongodb.com/docs/mongodb-shell/install/)
- A [Google AI API key](https://aistudio.google.com/apikey) (Gemini, used by both apps)

**Optional**

- [MongoDB Compass](https://www.mongodb.com/products/tools/compass) — GUI for inspecting collections.
- [`ripgrep`](https://github.com/BurntSushi/ripgrep) — enables the harness's `grep` tool.
- [Voyage AI](https://www.voyageai.com/) key — only if you want Voyage embeddings.
- [Modal](https://modal.com/) account + API key — only if you want to swap the local embedding model for a Modal-hosted vLLM server.

## Installation

```bash
# Clone
git clone <repo-url> && cd building-agentic-systems

# Shared .env — fill in GOOGLE_API_KEY at minimum
cp .env.example .env

# Memory app (Python)
make memory-build

# Harness (TypeScript)
make harness-install
```

## End-to-end quick start

Run everything from the repo root.

**1. Start shared infra.** MongoDB (replica set), mongot (Atlas Search locally), Prefect server + worker.

```bash
make local-start
```

**2. Validate the stack.** Confirms text, vector, and graph search all round-trip.

```bash
make memory-local-test
```

**3. Ingest → extract → index → query.**

```bash
make memory-run-data-pipeline             # walks sources.sources in configs/default.yaml (Substack RSS + articles + arXiv + web)
make memory-run-memory-pipeline-extraction # LLM → nodes + edges → knowledge_graph collection
make memory-run-memory-pipeline-indexing   # reverse edges, embeddings, search indexes
make memory-query-graph QUERY="AI agents"  # renders interactive HTML of the result
```

The Dockerized `prefect-worker` serves all deployments in-container, so these `make` triggers work without any extra setup. If you're iterating on pipeline code and want live reloads, run `make memory-serve-workflows` in a separate terminal instead — but don't do both (duplicate workers). See [`apps/memory/README.md`](apps/memory/README.md#serving-workflows) for details.

**4. Drive memory with the agent.**

```bash
# Interactive Ink REPL
make harness-dev

# One-shot
PROMPT="what do I have on AI agents in memory?" make harness-run
```

The harness reads `.mcp.json` at the repo root and auto-spawns the `tree-memory` MCP server, so its seven memory tools (`mcp__tree-memory__query_memory`, `search_memory`, `deep_search_memory`, `search_web`, `ingest_url`, `ingest_file`, `ingest_conversation`) are available from the first prompt.

**On-demand web search via `search_web`** — search the live web (Google / Bing / Yandex via Bright Data's SERP API) without polluting memory. By default `search_web` returns SERP results only; pass `ingest=True` (or `INGEST=true` on the CLI) to opt into batching the URLs through the same `ingest-web-url-batch-etl` flow that backs `ingest_url`. Requires `BRIGHTDATA_API_KEY` and `BRIGHTDATA_SERP_ZONE` in `.env`.

```bash
# Search-only — no side effects on memory.
make memory-search-web QUERY="anthropic claude api" NUM_RESULTS=5

# Search + opt-in fire-and-forget ingest of the top 1 result.
make memory-serve-workflows &   # workflows must be served for the ingest path
make memory-search-web QUERY="anthropic claude api" NUM_RESULTS=5 INGEST=true INGEST_TOP_K=1
```

## App guides

- **Memory app** → [`apps/memory/README.md`](apps/memory/README.md). Configuration (`configs/default.yaml`), every Prefect deployment, the full MCP tool catalogue, Modal embedding deployment, test layout.
- **Harness app** → [`apps/harness/README.md`](apps/harness/README.md). Modes (CLI vs Ink), native tools, permissions, sub-agents, shell hooks, JSONL sessions.

## Monitoring

**Prefect dashboard** — track pipeline runs, inspect task states, trigger deployments:

```
http://127.0.0.1:4200/dashboard
```

**MongoDB Compass** — inspect the `documents` and `knowledge_graph` collections:

```
mongodb://tree:tree@localhost:27017/?directConnection=true&authSource=admin
```

## QA and tests

Aggregate targets at the root run across both apps:

```bash
make format-check    # ruff (memory) + biome (harness)
make lint-check      # ruff (memory) + biome (harness)
make typecheck       # TypeScript (harness only — memory is dynamically typed)
make pre-commit      # repo-wide pre-commit
make unit-tests      # memory + harness unit suites
make integration-tests  # memory + harness integration suites (up to 15 min for memory)
make tests           # unit + integration across all apps
```

Per-app variants are available under `make memory-*` and `make harness-*` (e.g. `make memory-unit-tests`). Run `make help` to list all root targets.

## CI

GitHub Actions runs two parallel jobs on push / PR to `main`:

- **memory** — uv sync, ruff format/lint check, Docker infra up, pytest (unit + integration).
- **harness** — bun install, biome check, TypeScript typecheck, bun test.
