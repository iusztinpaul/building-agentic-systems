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
- [mongosh](https://www.mongodb.com/docs/mongodb-shell/install/) — shell for interacting with MongoDB (`brew install mongosh`)
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

## Infrastructure

### Environment loading with direnv

The shared `.env` is the single source of truth for secrets and infra settings. The Makefiles load it on their own, but agent CLIs (Claude Code, the harness) and bare commands (`uv run pytest`) only see it if the shell exports it. We use [direnv](https://direnv.net/) for that: it auto-exports `.env` into any shell that enters the repo (and unloads it on exit), so every tool launched from here — including MCP servers spawned by agents — inherits the full environment.

One-time setup:

```bash
brew install direnv
echo 'eval "$(direnv hook zsh)"' >> ~/.zshrc   # adjust for your shell
cp .envrc.example .envrc && direnv allow
```

`.envrc` is gitignored; re-run `direnv allow` whenever it changes.

### MongoDB Atlas (remote) via the MongoDB MCP server

The MongoDB MCP server manages the remote Atlas environment (clusters, DB users, access lists) through the Atlas Admin API. It authenticates with an [Atlas service account](https://www.mongodb.com/docs/mcp-server/prerequisites/): in the Atlas UI, select your organization → **Identity & Access** → **Applications** → **Add new** → **Service Account** (grant Project Owner on the target project), copy the Client ID/Secret, and add your IP to the service account's **API Access List**.

Put the credentials in `.env` as `MDB_MCP_API_CLIENT_ID` / `MDB_MCP_API_CLIENT_SECRET` (see `.env.example`); direnv exposes them to the MCP server. Launch your MCP client from a terminal inside the repo so it inherits them.

### Managing the Atlas cluster as code (IaC)

The same `MDB_MCP_API_CLIENT_ID` / `MDB_MCP_API_CLIENT_SECRET` credentials drive an Infrastructure-as-Code CLI (`apps/memory/deploy/atlas_cluster.py`) that creates, updates, inspects, and tears down the Atlas cluster through code — no console clicking. The desired state defaults to the standard setup (an `M0` free-tier replica set on GCP `WESTERN_EUROPE` in project `Tree`); override per-command with `ATLAS_ARGS`.

```bash
make memory-atlas-up        # create cluster + seed DB user + IP access list, wait until IDLE (idempotent)
make memory-atlas-status    # print cluster state + connection string
make memory-atlas-update    # PATCH the cluster to match the spec, e.g. a tier change
make memory-atlas-down      # delete the cluster (pass ATLAS_ARGS=--yes to skip the prompt)

# Manage a different cluster / shape by passing flags through ATLAS_ARGS:
make memory-atlas-up ATLAS_ARGS="--project Tree --cluster tree-staging --tier M10 --provider AWS --region US_EAST_1"
```

The seed DB user reuses `MONGO_INITDB_ROOT_USERNAME` / `MONGO_INITDB_ROOT_PASSWORD`; `atlas-up` also adds any CIDRs in the optional `ATLAS_ACCESS_CIDRS` env var (comma-separated) to the project IP access list. The service account needs Project Owner (or Cluster Manager + Database Access Admin + Network Access Manager) on the target project.

This script is the **reproducible, CI-friendly** path (no extra binary, reuses the service-account creds). For **ad-hoc, interactive** work against the cluster, the official [MongoDB Atlas CLI](https://www.mongodb.com/docs/atlas/cli/current/) (`brew install mongodb-atlas-cli`, then `atlas auth login`) is handier — e.g. `atlas clusters list`, `atlas clusters describe tree`, `atlas clusters create … --file cluster.json`, `atlas dbusers list`. Note it authenticates separately (interactive login or an API-key profile, not the service-account `MDB_MCP_*` pair), so prefer the `make memory-atlas-*` targets for anything scripted or run in CI.

> **A note on scope.** This script is a deliberately simple, dependency-free take on IaC — enough to manage one cluster reproducibly from code. For true scale (many clusters/projects/environments, drift detection, state management, plan/apply review, multi-resource dependency graphs) you'd reach for a real IaC tool like [Terraform](https://registry.terraform.io/providers/mongodb/mongodbatlas/latest/docs) (the `mongodbatlas` provider) or [Pulumi](https://www.pulumi.com/registry/packages/mongodbatlas/) instead.

### Continuous deployment of Prefect deployments

`.github/workflows/cd.yml` keeps the Prefect Cloud deployments in sync with `main`: on every push, **after CI passes**, it runs `make memory-deploy-prefect` (which calls `deploy/prefect_pipelines.py`) to register/update the deployment definitions on Prefect Cloud — without serving. The long-running worker runs separately (`make memory-serve-workflows` on an always-on host, or the `prefect-worker` container); CD only syncs the definitions.

One-time setup — add the Prefect Cloud credentials as GitHub repository secrets so the workflow can reach your workspace:

```bash
gh secret set PREFECT_API_URL --body "https://api.prefect.cloud/api/accounts/<account-id>/workspaces/<workspace-id>"
gh secret set PREFECT_API_KEY --body "pnu_xxxxxxxxxxxxxxxx"
```

After that, every merge to `main` that passes CI re-applies the deployments automatically. To apply them manually (e.g. from your machine, with `.env.prod` selected via `make env-prod`):

```bash
make memory-deploy-prefect
```

## End-to-end quick start

Run everything from the repo root.

**1. Select the local env target.** Pipelines and tests run against whatever `.env.target` points at; for local dev that must be local infra, not Atlas.

```bash
make env-status      # show the active target; switch with `make env-local`
```

**2. Start shared infra.** MongoDB (replica set), mongot (Atlas Search locally), Prefect server + worker.

```bash
make local-start
```

**3. Check connectivity.** Confirms the configured MongoDB target is reachable and lists collection counts.

```bash
make memory-check-db
```

**4. Create your user.** Every pipeline runs under a `user_id`, and a fresh database has none. `signup` is idempotent and pins the new user as the **current user** — the singleton session pointer (`whoami`) that every pipeline and the harness default to, so you don't pass an id on each command.

```bash
make memory-signup USER_IDENTIFIER=paul NAME="Paul Iusztin"
make memory-whoami            # prints the current user (id, identifier, name)
```

Already signed up (or juggling several users)? Skip `signup` and just repoint the current-user pointer at an existing user — by handle or id:

```bash
make memory-set-current-user USER_IDENTIFIER=paul    # or: USER_ID=<oid>
```

**5. Ingest → extract → index → query.** These run as the current user by default; override any one with `USER_ID=<oid>` or `USER_IDENTIFIER=<handle>`. The data pipeline fills `documents`; the memory pipeline turns those into the knowledge graph.

```bash
make memory-run-data-pipeline              # walks sources.sources in configs/default.yaml (Substack RSS + articles + arXiv + web) → documents
make memory-run-memory-pipeline-extraction # documents → LLM → nodes + edges → knowledge_graph collection
make memory-run-memory-pipeline-indexing   # reverse edges, embeddings, search indexes
make memory-query-graph QUERY="AI agents"  # renders interactive HTML of the result

make memory-run-data-pipeline USER_IDENTIFIER=another@example.com  # one-off run as a different user
```

`run-memory-pipeline-extraction` accepts an optional `NUM_SHARDS=<n>` to fan out across more parallel workers (default 1); `run-data-pipeline` has no such flag — its parallelism is declared per-source (platform bucketing + the HuggingFace source's `num_workers` in `default.yaml`). The Dockerized `prefect-worker` serves all deployments in-container, so these `make` triggers work without any extra setup. If you're iterating on pipeline code and want live reloads, run `make memory-serve-workflows` in a separate terminal instead — but don't do both (duplicate workers). See [`apps/memory/README.md`](apps/memory/README.md#serving-workflows) for details.

**6. Drive memory with the agent.**

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
