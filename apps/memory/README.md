# Tree Memory

The memory half of **Tree: Your Rooted Personal Assistant**. A Python app that ingests documents from multiple sources, extracts a knowledge graph with an LLM, indexes it for hybrid search on MongoDB, and exposes the result over a [FastMCP](https://gofastmcp.com/) server.

For the wider system (harness, end-to-end flow, shared infra) see the repo-root [`README.md`](../../README.md). For the harness that drives this memory, see [`../harness/README.md`](../harness/README.md).

## What this app contains

- **Data pipelines** (`src/tree/data/`) — one Prefect flow per source. Normalizes everything into the `documents` collection.
- **Memory pipelines** (`src/tree/memory/`) — `extraction/` chunks + LLM-extracts nodes and edges; `indexing/` builds reverse edges, embeds nodes, ensures text/vector/search indexes.
- **Query + MCP** (`src/tree/memory/query/`, `src/tree/mcp/`) — a CLI that renders interactive HTML graphs, and a FastMCP server exposing the memory tools to any MCP client.

Nodes use `_id = "type:name"`; edges use `_id = "source|type|target"`. Everything is upserted into a single mutable `knowledge_graph` collection.

## Prerequisites

- Python 3.14+
- [uv](https://docs.astral.sh/uv/)
- Docker + Docker Compose (shared infra lives at the repo root)
- GNU Make, [mongosh](https://www.mongodb.com/docs/mongodb-shell/install/)
- A [Google AI API key](https://aistudio.google.com/apikey) in the repo-root `.env`

Optional: [MongoDB Compass](https://www.mongodb.com/products/tools/compass), the [Modal](https://modal.com/) CLI (only if you deploy the vLLM embedding server).

Secrets live in the repo-root `.env` (see [`../../.env.example`](../../.env.example)) — app-level tuning lives in [`configs/default.yaml`](configs/default.yaml).

## Setup

All `make memory-*` targets are invoked from the repo root.

```bash
make memory-build    # runs uv sync into apps/memory/.venv
```

The virtual environment lives at `apps/memory/.venv`. `uv` manages dependencies via `pyproject.toml`.

## Configuration

Three sources of configuration, split by concern:

| Where | What | Override |
|---|---|---|
| [`configs/default.yaml`](configs/default.yaml) | Static memory config: model names, chunking, query tuning, dream/concurrency/prefect/MCP defaults | Set `APP_CONFIG_PATH=<path>` to point at a different YAML |
| Repo-root [`sources/`](../../sources) | Data-ingestion sources (operator data), split by cadence: `backfill.yaml` (one-shot) + `listen.yaml` (polled RSS) | Edit the files; select per-run with `--source-file` / `--uri` |
| Repo-root `.env` | Secrets + infra (Mongo, Prefect, LLM/embedding keys) | Edit the file |

### Source files (`sources/`)

Data-ingestion sources are operator **data**, kept out of `default.yaml` and committed under the repo-root [`sources/`](../../sources) directory, split by **cadence** (ADR-003):

- [`sources/backfill.yaml`](../../sources/backfill.yaml) — one-shot ingests (`substack_article`, `huggingface_dataset`, `youtube_video`, plain `web`); sources that do not gain new items after first ingest.
- [`sources/listen.yaml`](../../sources/listen.yaml) — repeatedly-polled feeds (`substack_rss`, `youtube_rss`). The nightly cron loads **only** this file, across all active users — the filename _is_ the schedule selector, so there is no per-source `scheduled` flag.

Each file is a flat top-level YAML list of entries; an entry is a dict with a `uri` and an optional `type` (one of `substack_rss`, `substack_article`, `youtube_rss`, `youtube_video`, `huggingface_dataset`, `web`). Untyped entries have `type` inferred from the URL shape (YouTube watch/feed URLs → `youtube_video` / `youtube_rss`; substack subdomain or a configured Substack custom domain → `substack_article`; otherwise → `web`, ingested via Bright Data Web Unlocker). For `huggingface_dataset` entries the `uri` is the HF dataset id (e.g. `librarian-bots/arxiv-metadata-snapshot`); the dispatcher routes by dataset id to a registered ETL in `tree.data.offline_pipeline._HUGGINGFACE_DATASET_HANDLERS`, and unknown ids raise. These entries also accept `max_samples`, `fetch_content`, `batch_size`, `num_workers`, and `concurrency` for tuning the dataset ingestor. A run selects sources with `--source-file` / `--uri` (see [Data pipelines](#data-pipelines)); `huggingface_dataset` can only be defined in a file, never via `--uri`.

### `default.yaml` sections

- `models.llm` — provider + model (default: `gemini` / `gemini-2.5-flash-lite`).
- `models.resolution_embedding` — provider + model + dimensions for the **transient** resolution embedding (computed on the entity name during resolution's semantic stage, never persisted). Default: `voyage` / `voyage-multimodal-3` / 1024.
- `models.search_embedding` — provider + model + dimensions for the **persisted** embedding used for dedup + search/query. Its `dimensions` is what the live mongot `vector_index` is asserted against at boot. Default: `voyage` / `voyage-multimodal-3` / 1024.
- `extraction` — `chunk_size`, `chunk_overlap`, `llm_concurrency`, `similarity_threshold`.
- `query` — `top_k`, `max_hops`, `rrf_k` (reciprocal rank fusion), `embedding_batch_size`.
- `mcp` — `max_retries`, `max_results`.

### Environment variables

| Variable | Required | Default | Purpose |
|---|---|---|---|
| `MONGO_HOST` | yes | `localhost` | MongoDB host |
| `MONGO_PORT` | yes | `27017` | MongoDB port |
| `MONGO_INITDB_ROOT_USERNAME` | yes | `tree` | Mongo root user |
| `MONGO_INITDB_ROOT_PASSWORD` | yes | `tree` | Mongo root password |
| `MONGO_INITDB_DATABASE` | yes | `tree` | Default database |
| `MONGOT_PORT` | yes | `27028` | Mongot (search) port |
| `PREFECT_PORT` | yes | `4200` | Prefect server port |
| `PREFECT_API_URL` | yes | `http://127.0.0.1:4200/api` | Prefect API URL |
| `GOOGLE_API_KEY` | **yes** | — | Gemini (LLM extraction + NL query) |
| `VOYAGE_API_KEY` | no | — | Voyage AI embeddings (alternative embedder) |
| `MODAL_EMBEDDING_API_KEY` | no | — | Auth for the Modal-hosted vLLM embedding server |
| `BRIGHTDATA_API_KEY` | no | — | Bright Data API key (Web Unlocker fallback + SERP API) |
| `BRIGHTDATA_UNLOCKER_ZONE` | no | — | Bright Data Web Unlocker zone (used by the web fallback ingest pipeline) |
| `BRIGHTDATA_SERP_ZONE` | no | — | Bright Data SERP zone (used by `search_web`) |
| `APP_CONFIG_PATH` | no | `apps/memory/configs/default.yaml` | Override the YAML config path |

## Running the components

### Infrastructure

Start MongoDB (replica set), mongot, Prefect server, and the Prefect worker from the repo root:

```bash
make local-start
make local-stop
make local-restart
```

Check the configured MongoDB target is reachable (pings, lists collection counts):

```bash
make memory-check-db
```

### Serving workflows

The Dockerized `prefect-worker` container (started by `make local-start`) runs `python -m tree.orchestrator`, which serves all deployments. If you're iterating on pipeline code and want live reloads without rebuilding the container, run the orchestrator locally instead:

```bash
make memory-serve-workflows
```

**Pick one — don't run both.** Running both serves duplicate workers that race for the same deployments.

The deployments registered by `src/tree/orchestrator.py` (the always-on core 5; the data worker dispatches each platform to one unified pipeline in-process — those are NOT separate deployments):

- `data-etl-coordinator`, `data-etl-worker` (data ingestion is split into an
  operator-facing coordinator that groups the configured `sources:` list by platform
  and windows HuggingFace, and a worker that ingests one shard — #072)
- `memory-extract-etl-coordinator`, `memory-extract-etl-worker` (memory extraction
  is split into a coordinator that shards pending docs + indexes once and a worker
  that runs the six-task extraction body — #067), `memory-indexing-etl`

Plus the optional `dream-consolidation-all-users` (nightly cron) when `prefect.deploy_optional: true` — see [Configuration](#configuration).

### Pipelines at a glance

Two stages — **data** (sources → `documents`) then **memory** (extraction → knowledge graph, then indexing) — each runnable **offline** (config-driven batch) or **online** (one source on demand):

| stage | offline | online |
|---|---|---|
| **data** → `documents` | `run-data-pipeline` | `run-data-pipeline MODE=online SOURCE=…` |
| **memory** → graph (+ trailing index) | `run-memory-pipeline` | `run-memory-pipeline MODE=online DOC_IDS=…` |
| **index** (shared, standalone) | `run-indexing-pipeline` | `run-indexing-pipeline` |

**Run it all in one shot** — `run-pipeline` dispatches ONE end-to-end flow run (`offline-pipeline` / `online-pipeline`, the glue flows in `tree/offline.py` / `tree/online.py`) and blocks until it finishes; extraction fires the trailing index, so the graph is queryable when it returns:

```bash
# Offline: every configured source -> documents -> graph (+ index)
make memory-run-pipeline                                                  # default sources (backfill + listen)
make memory-run-pipeline USER_IDENTIFIER=paul SOURCE_FILE="sources/listen.yaml"   # chosen file, another user

# Online: one source -> document -> graph (+ index), end to end
make memory-run-pipeline MODE=online SOURCE="https://www.decodingai.com/p/agentic-harness-engineering"
make memory-run-pipeline MODE=online SOURCE="/path/to/notes.md" TITLE="My notes"
```

`run-pipeline MODE=online` ingests the source and runs extraction inline in the SAME flow run, then submits the trailing indexing run (a duplicate source skips extraction). The sections below break each step out for running them individually.

### Data pipelines

The data pipeline produces `documents` **only** — it does NOT extract or index (that's the [memory pipeline](#memory-extraction), a separate step). It runs **offline** (source-file / URI-driven, fanned out over Prefect workers) and **online** (realtime, one source at a time). Each target streams logs from the local `make memory-serve-workflows` (or the Dockerized worker) back to the terminal.

#### Offline — selecting sources

```bash
make memory-run-data-pipeline                                       # default set (backfill + listen), current user
make memory-run-data-pipeline USER_IDENTIFIER=paul                  # default set, another user
make memory-run-data-pipeline SOURCE_FILE="sources/listen.yaml"     # only the listen feeds
make memory-run-data-pipeline URI="https://blog.com/feed=substack_rss https://news.site/post"  # ad-hoc URLs
make memory-run-data-pipeline SOURCE_FILE="sources/backfill.yaml" URI="https://news.site/post" # combine both
```

Triggers `data-etl-coordinator`: resolves its source set, groups it by platform, and dispatches one `data-etl-worker` per non-HuggingFace platform (`substack` / `youtube` / `custom`) plus `num_workers` HuggingFace offset-window workers (each worker dispatches its shard's entries to the right sub-flow — Substack RSS / article batches, YouTube RSS / video batches, HuggingFace arXiv, web URLs). No trailing index. Fan-out is per-source — platform bucketing is automatic and the HuggingFace fan-out width is that source's `num_workers` in `sources/backfill.yaml`, not a global flag.

Source selection is freely combinable (ADR-003):

- **Neither flag** → the default set: `sources/backfill.yaml` + `sources/listen.yaml`.
- **`SOURCE_FILE="..."`** (space-separated, repeatable) → load the named source file(s).
- **`URI="..."`** (space-separated, repeatable) → ad-hoc URLs; suffix a token `=TYPE` to force a type (e.g. `…/feed=substack_rss`), otherwise the type is inferred. `huggingface_dataset` is rejected here — define HF datasets in a source file instead.
- Files and URIs combine: the resolved set is the loaded files followed by the built URLs.

The **nightly cron** (`0 3 * * *` UTC) runs the same coordinator with `source_files=["sources/listen.yaml"]` and no `user_id` — ingesting the polled listen feeds fanned out across **all active users**. The cadence is the filename: there is no per-source flag.

#### Online — one source on demand

```bash
make memory-run-data-pipeline MODE=online SOURCE="https://www.decodingai.com/p/agentic-harness-engineering"
make memory-run-data-pipeline MODE=online SOURCE="/path/to/notes.md" TITLE="My notes"
```

Dispatches the `online-pipeline` flow with extraction OFF: ingests a single URL or local file in realtime into `documents` **only** — it does NOT extract or index. It prints the new document id; feed that to `make memory-run-memory-pipeline MODE=online DOC_IDS=<id>` to build the graph. `SOURCE` is auto-detected: an `http(s)` URL routes to the web/Substack/YouTube dispatcher; anything else is treated as a local file (`.txt` / `.md` / `.html`). Defaults to the current user; override with `USER_ID` / `USER_IDENTIFIER`. (The MCP `ingest_url` / `ingest_file` tools fire extraction automatically as a realtime convenience; this CLI keeps the two pipelines decoupled. Conversation ingestion is MCP-only.)

### Memory extraction

Extract knowledge-graph nodes + edges from `documents` into `knowledge_graph`. Two modes, both via `memory-extract-etl-coordinator` (which also fires one trailing index run):

```bash
# Offline — ALL pending documents (batch fan-out; optional NUM_SHARDS=<n>)
make memory-run-memory-pipeline
make memory-run-memory-pipeline DOC_IDS="507f1f77bcf86cd799439011,507f1f77bcf86cd799439012"

# Online — ONE document (e.g. the one just produced by run-data-pipeline MODE=online)
make memory-run-memory-pipeline MODE=online DOC_IDS="507f1f77bcf86cd799439011"
```

### Memory indexing

The single indexing step — works after either extraction mode. Builds reverse edges for bidirectional traversal, computes node embeddings, and ensures text / vector / Atlas-search indexes on `knowledge_graph`:

```bash
make memory-run-indexing-pipeline
```

### Query CLI

```bash
# Visualize the entire graph
make memory-query-graph

# Query a specific topic — renders an interactive HTML graph and opens it
make memory-query-graph QUERY="Paul Iusztin"
```

### MCP server

Expose the knowledge graph to any MCP-aware client (Claude Code, Claude Desktop, Cursor, the bundled harness):

```bash
make memory-serve-mcp                       # stdio transport (default)
make memory-serve-mcp TRANSPORT=streamable-http
```

The repo-root `.mcp.json` already wires this up — Claude Code and the harness auto-spawn it. No extra setup needed from those clients.

**Tools exposed:**

| Tool | Description |
|---|---|
| `query_memory` | Translates natural language to MongoDB aggregation pipelines via LLM. Best for structured questions, counts, filters. |
| `search_memory` | Semantic + text search with graph expansion. Best for open-ended queries. |
| `deep_search_memory` | Broader exploration — persists results to disk for follow-up. |
| `search_web` | On-demand web search via Bright Data SERP. **Does NOT touch memory by default.** Opt-in `ingest=true` fires the `ingest-web-url-batch-etl` deployment fire-and-forget. |
| `scrape_web` | On-demand scrape of one or more URLs via Bright Data Web Unlocker. **Does NOT touch memory.** Returns markdown (or HTML) inline for exploration; pair with `search_web` to read SERP results, then call `ingest_url` on whichever URLs are worth keeping. Max 5 URLs per call. |
| `ingest_url` | Ingest a web page (Substack, arXiv, custom) through the data + memory pipelines. |
| `ingest_file` | Ingest a local file. |
| `ingest_conversation` | Ingest a chat transcript into memory. |

`query_memory` and `search_memory` accept a `visualize` flag that renders an interactive HTML graph.

#### `search_web` example

```bash
# Pure search — NO writes to MongoDB.
make memory-search-web QUERY="MongoDB Atlas vector search" NUM_RESULTS=5

# Optional opt-in ingest of the top K results. Requires the Prefect workflow
# server to be up (Dockerized worker via `make local-start`, or
# `make memory-serve-workflows` if you're iterating).
make memory-search-web QUERY="MongoDB Atlas vector search" NUM_RESULTS=5 INGEST=true INGEST_TOP_K=2
```

Equivalent MCP-tool invocation (JSON `arguments` an MCP client would send):

```json
{
  "name": "search_web",
  "arguments": {
    "query": "MongoDB Atlas vector search",
    "engine": "google",
    "num_results": 5,
    "ingest": false
  }
}
```

Required env vars: `BRIGHTDATA_API_KEY` + `BRIGHTDATA_SERP_ZONE`.

#### `scrape_web` example

```bash
# Scrape a couple of URLs; markdown returned inline. NO writes to MongoDB.
make memory-scrape-web URLS="https://example.com,https://www.iana.org/help/example-domains"

# Bound the per-URL payload (default 30000 chars). Set MAX_CHARS=0 to disable.
make memory-scrape-web URLS="https://en.wikipedia.org/wiki/Knowledge_graph" MAX_CHARS=2000

# Raw HTML instead of markdown.
make memory-scrape-web URLS="https://example.com" DATA_FORMAT=html
```

Equivalent MCP-tool invocation:

```json
{
  "name": "scrape_web",
  "arguments": {
    "urls": ["https://example.com", "https://www.iana.org/help/example-domains"],
    "data_format": "markdown",
    "max_chars": 30000
  }
}
```

Each result includes `url`, `success`, `content`, `length`, `truncated`, plus
`error` / `error_type` (`invalid_input` / `configuration_error` /
`fetch_failed` / `http_error` / `network_error`) on failure. One bad URL
does not kill the batch; order of results matches input order.

Required env vars: `BRIGHTDATA_API_KEY` + `BRIGHTDATA_UNLOCKER_ZONE` (the
*Unlocker* zone, distinct from the SERP zone used by `search_web`).

## Modal embedding deployment (optional)

The default embedding model is local sentence-transformers (`all-MiniLM-L6-v2`). For heavier workloads, swap in a Modal-hosted vLLM server running `voyageai/voyage-4-nano` on an A10G.

```bash
make memory-generate-secret-key           # generate MODAL_EMBEDDING_API_KEY, put it in .env
make memory-deploy-embedding-model        # deploys to Modal (creates the vllm-embedding-api-key secret)
make memory-deploy-embedding-model-test   # smoke-test the deployment
make memory-deploy-embedding-model-stop   # tear it down
```

Then flip `models.search_embedding` (and, if desired, `models.resolution_embedding`) in `configs/default.yaml` to the Modal provider.

## Testing

```bash
make memory-tests              # unit suite (needs the local MongoDB from make local-start)
```

Layout mirrors the source tree: `tests/unit/<area>/` — unit tests with mocks (`pytest-mock`). There is no integration suite (deleted deliberately — too slow for feedback loops); e2e verification happens by running the real pipelines (see "Running pipelines").

Auto-format + lint before committing:

```bash
make memory-format-fix && make memory-lint-fix
make memory-format-check && make memory-lint-check
make pre-commit
```

## Layout

```
apps/memory/
  src/tree/
    config/             # Pydantic settings + YAML loader
    entities/           # Beanie ODMs shared across the app
    data/               # one module per ingestion source
      core/             # base flow, URL dispatch, ingest framework
      substack/         # substack.py + batch/single-article pipelines
      huggingface/      # arxiv_dataset_pipeline.py
      conversation.py   # conversation ingestion
      file.py           # local file ingestion
      pipeline.py       # data_pipeline (dispatcher over sources.sources)
    memory/
      extraction/       # chunk + LLM extract → nodes + edges
      indexing/         # reverse edges, embeddings, indexes
      query/            # hybrid search + NL query + HTML visualize
    mcp/                # FastMCP server + tools
    db.py               # Mongo + Beanie init
    orchestrator.py     # Prefect `serve(...)` registering deployments
  configs/default.yaml  # app tuning
  deploy/               # Modal deployments (vLLM embedding)
  scripts/              # CLI entrypoints (serve_mcp, run_*, query_graph, signup, check_db)
  tests/unit
  docker/Dockerfile     # image used by the compose `prefect-worker`
  Makefile              # app-local targets (see make memory-help)
  pyproject.toml, uv.lock
```

## Monitoring

**Prefect dashboard** — `http://127.0.0.1:4200/dashboard`. Or open directly:

```bash
uv run prefect dashboard open
```

**MongoDB** — connect Compass (or `mongosh`) to:

```
mongodb://tree:tree@localhost:27017/?directConnection=true&authSource=admin
```

Collections to inspect: `documents` (raw ingest) and `knowledge_graph` (nodes + edges).
