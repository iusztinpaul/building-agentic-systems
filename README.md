# Building Agentic Systems

Build your digital twin through knowledge graphs, ontologies, memory, LLMs and agents.

## Prerequisites

- [Python 3.14+](https://www.python.org/)
- [uv](https://docs.astral.sh/uv/) (Python package manager)
- [Docker](https://www.docker.com/) and Docker Compose
- [GNU Make](https://www.gnu.org/software/make/)
- A [Google AI API key](https://aistudio.google.com/apikey) (for Gemini LLM extraction)

## Installation

```bash
# Clone the repository
git clone <repo-url> && cd building-agentic-systems

# Install dependencies
make build

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
make local-test
```

### App configuration

App-level tuning (model names, chunk sizes, concurrency) lives in `configs/default.yaml`. Infrastructure secrets (API keys, DB credentials) stay in `.env`.

To override the config path, set `APP_CONFIG_PATH` in your `.env`.

## Running

### Quick start (Docker only)

For just running the pipelines without a local dev setup, everything is containerized. Docker Compose starts MongoDB, Prefect server, and a Prefect worker that serves all workflow deployments:

```bash
make local-start
```

The worker container (`twin-prefect-worker`) automatically registers and serves all deployments. You can trigger runs from the Prefect dashboard or the CLI scripts below.

### Development mode

For development, run the infrastructure via Docker but serve workflows locally so you can iterate without rebuilding the container:

```bash
# Start infra only
make local-start

# Serve workflow deployments locally
make serve-workflows
```

### Step 1: Data pipelines

Ingest documents from Substack RSS feeds into the `documents` collection:

```bash
# Single feed
make run-etl-substack FEED_URL=https://www.decodingai.com/feed

# Multiple feeds (via script)
uv run python scripts/run_data_pipeline.py https://www.decodingai.com/feed https://other.substack.com/feed
```

### Step 2: Memory extraction

Extract knowledge graph entities (nodes + edges) from documents into the `knowledge_graph_log` collection:

```bash
# Process all unprocessed documents
uv run python scripts/run_memory_pipeline.py

# Process specific documents by ID
uv run python scripts/run_memory_pipeline.py 507f1f77bcf86cd799439011
```

### Step 3: Materialization

Rebuild the materialized `knowledge_graph` collection from logs, compute embeddings, create reverse edges for bidirectional traversal, and ensure search indexes:

```bash
uv run python scripts/run_materialization_pipeline.py
```

### Step 4: Query and visualize

Query the knowledge graph and generate an interactive HTML visualization:

```bash
# Visualize the full graph
uv run python scripts/query_graph.py

# Query a specific topic
uv run python scripts/query_graph.py -q "Paul Iusztin"

# Customize search parameters
uv run python scripts/query_graph.py -q "MLOps" --top-k 5 --max-hops 2 -o result.html
```

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

Download [MongoDB Compass](https://www.mongodb.com/products/tools/compass) and connect to your local MongoDB to inspect collections (`documents`, `knowledge_graph_log`, `knowledge_graph`):

```
mongodb://twin:twin@localhost:27017/?directConnection=true&authSource=admin
```

## Tests

```bash
make format-fix    # Auto-format
make lint-fix      # Auto-fix lint issues
make format-check  # Check formatting
make lint-check    # Check linting
make pre-commit    # Run pre-commit hooks
make tests         # Run test suite
```
