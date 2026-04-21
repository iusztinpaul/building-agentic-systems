# Building Agentic Systems — Tree

**Tree: Your Rooted Personal Assistant.** Build a personal assistant rooted in a knowledge-graph memory, powered by ontologies, LLMs, and agents.

## Repo layout

This is a monorepo. Each app owns its own build files:

- `apps/memory/` — Python app: ETL pipelines, knowledge-graph memory (MongoDB), and the FastMCP server. See [`apps/memory/README.md`](apps/memory/README.md).
- `apps/harness/` — planned TypeScript/Ink/Bun coding-agent harness. Design parked in [`docs/harness-plan.md`](docs/harness-plan.md).

Root-level files handle cross-app concerns: shared `.env`, MongoDB infra under `docker/`, `docker-compose.yml`, a thin root `Makefile` that delegates to each app (`make memory-<target>`), and the `.mcp.json` that defines which MCP servers agents spawn.

## Prerequisites

- [Python 3.14+](https://www.python.org/)
- [uv](https://docs.astral.sh/uv/) (Python package manager)
- [Docker](https://www.docker.com/) and Docker Compose
- [GNU Make](https://www.gnu.org/software/make/)

- [mongosh](https://www.mongodb.com/docs/mongodb-shell/install/)
- [MongoDB Compass](https://www.mongodb.com/products/tools/compass)

- A [Google AI API key](https://aistudio.google.com/apikey) (for Gemini LLM extraction)

## Installation

```bash
# Clone the repository
git clone <repo-url> && cd building-agentic-systems

# Install dependencies for the memory app
make memory-build

# Create your environment file
cp .env.example .env
# Edit .env and set your GOOGLE_API_KEY
```

## Setup

### Start infrastructure

This spins up MongoDB (with replica set + mongot for vector search) and Prefect server:

```bash
make local-start
```

Validate that MongoDB text, vector, and graph search all work:

```bash
make memory-local-test
```

### App configuration

App-level tuning (model names, chunk sizes, concurrency) lives in `apps/memory/configs/default.yaml`. Infrastructure secrets (API keys, DB credentials) stay in the shared root `.env`.

To override the config path, set `APP_CONFIG_PATH` in your `.env`.

## Running

### Quick start (Docker only)

For just running the pipelines without a local dev setup, everything is containerized. Docker Compose starts MongoDB, Prefect server, and a Prefect worker that serves all workflow deployments:

```bash
make local-start
```

The worker container (`tree-prefect-worker`) automatically registers and serves all deployments. You can trigger runs from the Prefect dashboard or the CLI scripts below.

### Development mode

For development, run the infrastructure via Docker but serve workflows locally so you can iterate without rebuilding the container:

```bash
# Start infra only
make local-start

# Serve workflow deployments locally
make memory-serve-workflows
```

### Step 1: Data pipelines

Ingest documents from multiple sources into the `documents` collection. Sources are configured in `apps/memory/configs/default.yaml`:

```bash
# Run all data pipelines (Substack RSS, articles, arxiv)
make memory-run-all-data-pipelines

# Or run individual pipelines
make memory-run-substack-rss-data-pipeline
make memory-run-substack-article-data-pipeline
make memory-run-arxiv-data-pipeline
```

### Step 2: Memory extraction

Extract knowledge graph entities (nodes + edges) from documents and upsert them directly into the `knowledge_graph` collection:

```bash
# Process all unprocessed documents
make memory-run-memory-pipeline-extraction

# Process specific documents by ID
make memory-run-memory-pipeline-extraction DOC_IDS="507f1f77bcf86cd799439011"
```

### Step 3: Indexing

Create reverse edges for bidirectional traversal, compute embeddings, and ensure search indexes on the `knowledge_graph` collection:

```bash
make memory-run-memory-pipeline-indexing
```

### Step 4: Query and visualize

Query the knowledge graph and generate an interactive HTML visualization:

```bash
# Visualize the full graph
make memory-query-graph

# Query a specific topic
make memory-query-graph QUERY="Paul Iusztin"
```

### Step 5: MCP server

Expose the knowledge graph as an [MCP](https://modelcontextprotocol.io/) server so LLM clients (Claude Code, Claude Desktop, Cursor, etc.) can query Tree's memory with natural language.

**Tools provided:**

| Tool | Description |
|------|-------------|
| `query_memory` | Translates natural language to MongoDB aggregation pipelines via LLM. Best for structured questions, counts, and filters. |
| `search_memory` | Semantic + text search with graph expansion. Best for open-ended or exploratory queries. |

Both tools accept a `visualize` flag that renders an interactive HTML graph and opens it in the browser.

**Run standalone:**

```bash
make memory-serve-mcp
```

**Use with Claude Code:**

The `.mcp.json` at the project root auto-configures the server. Claude Code picks it up automatically — no extra setup needed.

## Monitoring

### Prefect dashboard

Track pipeline runs, inspect task states, and trigger deployments from the UI:

```
http://127.0.0.1:4200/dashboard
```

Or open it directly:

```bash
uv run prefect dashboard open
```

### MongoDB Compass

Download [MongoDB Compass](https://www.mongodb.com/products/tools/compass) and connect to your local MongoDB to inspect collections (`documents`, `knowledge_graph`):

```
mongodb://tree:tree@localhost:27017/?directConnection=true&authSource=admin
```

## Tests

```bash
make memory-format-fix    # Auto-format
make memory-lint-fix      # Auto-fix lint issues
make memory-format-check  # Check formatting
make memory-lint-check    # Check linting
make pre-commit           # Run pre-commit hooks
make tests                # Run all test suites (aggregates across apps)
```
