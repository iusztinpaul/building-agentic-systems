# Tree Memory

The memory half of **Tree: Your Rooted Personal Assistant**. A Python app that ingests documents from multiple sources, turns them into memory (vanilla RAG rows, plus an LLM-extracted knowledge graph in `graphrag` mode), indexes it for hybrid search on MongoDB, and exposes the result over a [FastMCP](https://gofastmcp.com/) server.

For the wider system (harness, end-to-end flow, shared infra) see the repo-root [`README.md`](../../README.md). For the harness that drives this memory, see [`../harness/README.md`](../harness/README.md).

## What this app contains

- **Data pipelines** (`src/tree/data/`) — one Prefect flow per source. Normalizes everything into the `documents` collection.
- **Memory pipeline** (`src/tree/memory/pipeline.py`) — the three Prefect flows (extraction worker, coordinator fan-out, indexing). ONE flow body: the `rag/` stages always, the `graph/` stages behind `if mode == "graphrag"`.
  - `src/tree/memory/rag/` — clean → chunk (parent/child) → embed children → load rows, plus hybrid search, parent-document retrieval and the index builder. The complete Chapter-4 system; it never imports `graph/`.
  - `src/tree/memory/graph/` — what Chapter 8 adds: structural edges, LLM entity extraction, resolution, dedup, review, dream consolidation, graph expansion, NL query and the HTML graph renderer.
- **MCP + query CLI** (`src/tree/mcp/`, `scripts/query_graph.py`) — a FastMCP server exposing the memory tools to any MCP client, and a CLI that prints retrieved parents (`rag`) or renders an interactive HTML graph (`graphrag`).

Nodes use `_id = "{user_id}:type:name"`; edges use `_id = "source|type|target"` (both endpoints already carry the user prefix). Everything is upserted into a single mutable `memory` collection.

## Memory modes

ONE switch — `memory.mode` in [`configs/default.yaml`](configs/default.yaml), default `graphrag`, overridable per process with `TREE_MEMORY__MODE=rag|graphrag` — decides how much of the memory half runs (ADR-006). It is read ONCE at flow entry, at MCP-server boot and at CLI start; never per request.

| | `rag` | `graphrag` |
|---|---|---|
| **Pipeline stages** | `clean_text` → `split_document` → `embed_children` → `load_rag_rows` | the same four, then structural edges → LLM extract → validate → resolve + embed entities → `apply_writes` |
| **Writes to `memory`** | node rows only: one `document` row, its `chunk`/`parent` rows and its embedded `chunk`/`child` rows | the same rows **plus** `part_of` / `next` / `mentions` / `referenced` edges and entity nodes |
| **Embedded** | child chunks | child chunks + entity nodes |
| **Retrieval** | `retrieve_parents` — hybrid search (vector + text, RRF) over children, grouped by `parent_id`, returning whole parents with their document metadata | the same parent resolution, then `expand_graph` from the parent ids ∪ entity seeds |
| **MCP tools** | 6 (`search_memory`, `ingest_*`, `search_web`, `scrape_web`) | those 6 + 7 graph tools (see [MCP server](#mcp-server)) |
| **`make memory-query-graph`** | prints the retrieved parents as text | writes + opens `.tree/graphs/<slug>-<stamp>.html` |

Both modes write the SAME `memory` collection with the same row shapes (`parent_id` and `chunk_index` are present in both), so a chunk row is byte-identical across modes. There is **no migration**: switching modes means dropping the collection and re-ingesting from scratch.

```bash
make memory-check-db                      # confirm which Mongo you are pointed at
mongosh "mongodb://$MONGO_INITDB_ROOT_USERNAME:$MONGO_INITDB_ROOT_PASSWORD@localhost:$MONGO_PORT/?directConnection=true" \
  --quiet --eval 'db.getSiblingDB("tree").memory.drop()'
TREE_MEMORY__MODE=rag make memory-run-pipeline MODE=online SOURCE="https://…"
```

## Setup

All `make memory-*` targets are invoked from the repo root.

```bash
make memory-build    # runs uv sync into apps/memory/.venv
```

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
- `memory` — `mode` (`rag` | `graphrag`), `chunking` (`strategy`, `parent.size/overlap`, `child.size/overlap`) and `clustering` (`umap`, `hdbscan`, `sampling`, `summaries`). `clustering` has no `enabled` key on purpose: the ON/OFF switch is the `run_clustering` flow parameter of `offline-pipeline` (default off), not YAML — an `enabled` key is a hard `ValidationError` at boot.
- `extraction` — `llm_concurrency`, `doc_concurrency`, `dedup_concurrency`, plus the `resolution` / `dedup` blocks.
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

The deployments registered by `src/tree/orchestrator.py` (the always-on core 5, exactly filling the Prefect free-tier cap; the data worker dispatches each platform to one unified pipeline in-process — those are NOT separate deployments):

- `data-etl-worker` (ingests ONE data shard — a platform bucket or a HuggingFace
  offset window — #072)
- `memory-extract-etl-worker` (runs the ONE memory-pipeline body — the rag stages
  always, the graph stages only in `graphrag` — over one shard of pending
  documents — #067)
- `online-pipeline`, `offline-pipeline` (the two end-to-end flows in `tree/online.py` /
  `tree/offline.py`; `offline-pipeline` also carries the [nightly cron](#offline--selecting-sources))
- `dream-consolidation-all-users` (the nightly incremental dedup sweep across every active
  user, on its own cron — `dream.cron`, `0 4 * * *` UTC)

The two **Coordinators** (`data_etl_coordinator`, `memory_extract_etl_coordinator`) and the
indexing step (`memory_indexing` — embeddings backfill, search indexes) are still FLOWS, but
they are no longer Deployments: each runs as an **inline subflow**. The Coordinators run inside an
`offline-pipeline` run, which holds the single admission slot while they fan out their Workers;
`memory_indexing` runs as that same run's third **Offline phase** — once per target user, after
every user's extraction (ADR-007) — or inline inside `online-pipeline` for a single document.
`memory_clustering` is the fourth phase, OFF by default. Every manual single-step run is the same
`offline-pipeline` deployment with the other phases off (`make memory-run-data-pipeline` /
`make memory-run-memory-pipeline` / `make memory-run-indexing-pipeline` /
`make memory-run-clustering-pipeline`); no script runs a flow in the operator's own process.

Both end-to-end pipelines carry BOTH identity tags, so they belong to BOTH the `data` and `memory`
deployment groups: a group-scoped teardown (`make memory-deploy-prefect-setup-down GROUPS=data`)
deletes them too, and the next `...-up` restores them.

That is 5 of the 5 deployments the Prefect free tier allows, with **no slot spare**: a sixth
deployment must either displace one of these or be marked `optional=True` and registered only
when `prefect.deploy_optional: true` (a paid plan or a self-hosted server) — see
[Configuration](#configuration).

### Pipelines at a glance

Two stages — **data** (sources → `documents`) then **memory** (documents → rows of the `memory` collection, then indexing) — each runnable **offline** (config-driven batch) or **online** (one source on demand). Offline, each row below is ONE `offline-pipeline` run with a different set of **Offline phase**s on (`run_data` / `run_extraction` / `run_indexing` / `run_clustering`):

| stage | offline | online |
|---|---|---|
| **data** → `documents` | `run-data-pipeline` (phase: `data`) | `run-data-pipeline MODE=online SOURCE=…` |
| **memory** → `memory` rows | `run-memory-pipeline` (phases: `extraction` + `index`) | `run-memory-pipeline MODE=online DOC_IDS=…` |
| **index** (shared, standalone) | `run-indexing-pipeline` (phase: `index`) | `run-indexing-pipeline` |
| **clustering** → `memory_clusters` | `run-clustering-pipeline` (phase: `clustering`) | — (maintenance phase; no online form) |

**Run it all in one shot** — `run-pipeline` dispatches ONE end-to-end flow run (`offline-pipeline` / `online-pipeline`, the glue flows in `tree/offline.py` / `tree/online.py`) and blocks until it finishes; offline that is all three phases in a row, so memory is queryable when it returns:

```bash
# Offline: every configured source -> documents -> memory collection (+ index)
make memory-run-pipeline                                                  # default sources (backfill + listen)
make memory-run-pipeline USER_IDENTIFIER=paul SOURCE_FILE="sources/listen.yaml"   # chosen file, another user

# Online: one source -> document -> memory collection (+ index), end to end
make memory-run-pipeline MODE=online SOURCE="https://www.decodingai.com/p/agentic-harness-engineering"
make memory-run-pipeline MODE=online SOURCE="/path/to/notes.md" TITLE="My notes"
```

`run-pipeline MODE=online` ingests the source and runs extraction inline in the SAME flow run, then indexes that document inline (a duplicate source skips extraction). The sections below break each step out for running them individually.

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

Dispatches ONE `offline-pipeline` run with `run_extraction=False, run_indexing=False` (data phase only). Inside it, the data **Coordinator** runs as an inline subflow: it resolves its source set, groups it by platform, and dispatches one `data-etl-worker` per non-HuggingFace platform (`substack` / `youtube` / `custom`) plus `num_workers` HuggingFace offset-window workers (each worker dispatches its shard's entries to the right sub-flow — Substack RSS / article batches, YouTube RSS / video batches, HuggingFace arXiv, web URLs). No extraction, no indexing — those phases are off. Fan-out is per-source — platform bucketing is automatic and the HuggingFace fan-out width is that source's `num_workers` in `sources/backfill.yaml`, not a global flag.

Source selection is freely combinable (ADR-003):

- **Neither flag** → the default set: `sources/backfill.yaml` + `sources/listen.yaml`.
- **`SOURCE_FILE="..."`** (space-separated, repeatable) → load the named source file(s).
- **`URI="..."`** (space-separated, repeatable) → ad-hoc URLs; suffix a token `=TYPE` to force a type (e.g. `…/feed=substack_rss`), otherwise the type is inferred. `huggingface_dataset` is rejected here — define HF datasets in a source file instead.
- Files and URIs combine: the resolved set is the loaded files followed by the built URLs.

The **nightly cron** (`0 3 * * *` UTC) fires the `offline-pipeline` deployment with `source_files=["sources/listen.yaml"]` and no `user_id` — so it ingests the polled listen feeds AND writes them to the `memory` collection AND indexes, fanned out across **all active users** (nightly documents no longer sit `PENDING` waiting for a manual extraction run). The cadence is the filename: there is no per-source flag.

#### Online — one source on demand

```bash
make memory-run-data-pipeline MODE=online SOURCE="https://www.decodingai.com/p/agentic-harness-engineering"
make memory-run-data-pipeline MODE=online SOURCE="/path/to/notes.md" TITLE="My notes"
```

Dispatches the `online-pipeline` flow with extraction OFF: ingests a single URL or local file in realtime into `documents` **only** — it does NOT extract or index. It prints the new document id; feed that to `make memory-run-memory-pipeline MODE=online DOC_IDS=<id>` to write it into the `memory` collection. `SOURCE` is auto-detected: an `http(s)` URL routes to the web/Substack/YouTube dispatcher; anything else is treated as a local file (`.txt` / `.md` / `.html`). Defaults to the current user; override with `USER_ID` / `USER_IDENTIFIER`. (The MCP `ingest_url` / `ingest_file` tools fire extraction automatically as a realtime convenience; this CLI keeps the two pipelines decoupled. Conversation ingestion is MCP-only.)

### Memory pipeline

Turn `documents` into rows of the `memory` collection. The worker flow
(`memory-extract-etl-worker`, `src/tree/memory/pipeline.py`) runs ONE body whose stages depend
on [`memory.mode`](#memory-modes):

1. `clean-and-chunk` — `clean_text` then two-level splitting into parent + child chunks.
2. `embed-children` — the **contextual header** text (`title` + heading path + content) of every child.
3. `load-rag-rows` — one unordered `bulk_write` of the `document` / parent / child rows.
4. *(graphrag only)* structural edges → `llm-extract-entities` over parent chunks → `validate-raws`
   → first-person + supersession → `resolve-entities` → `embed-entities` → `dedupe-entities` →
   `apply-writes`.

Both run modes below are dispatched as ONE `offline-pipeline` run with `run_data=False` (the two
memory phases only); inside it the extraction **Coordinator** runs as an inline subflow that shards
the pending documents across `memory-extract-etl-worker` runs, and the indexing phase then runs
once for the user as a sibling subflow:

```bash
# Offline — ALL pending documents (batch fan-out; optional NUM_SHARDS=<n>)
make memory-run-memory-pipeline
make memory-run-memory-pipeline DOC_IDS="507f1f77bcf86cd799439011,507f1f77bcf86cd799439012"

# Online — ONE document (e.g. the one just produced by run-data-pipeline MODE=online)
make memory-run-memory-pipeline MODE=online DOC_IDS="507f1f77bcf86cd799439011"
```

### Memory indexing

The indexing **Offline phase** (`memory-indexing-etl`) — works in both memory modes.
`embed-kg-nodes` backfills the vectors that are missing on the rows that are supposed to carry one
(child chunks always; entity nodes in `graphrag`), and `ensure-kg-indexes` asserts the text index
and the vector index (filter paths `user_id`, `kind`, `type`, `subtype`, `merged_into`) on
`memory`. Running it alone dispatches ONE `offline-pipeline` run with `run_data=False,
run_extraction=False` — so it needs served workflows, like every other pipeline command:

```bash
make memory-run-indexing-pipeline
```

### Memory clustering

The clustering **Offline phase** (`memory-clustering-etl`) — the data behind the **Embedding
map**. Works in both memory modes and writes no graph rows and no edges (ADR-007). Four tasks:
`load-child-embeddings` (every embedded **child chunk** of the user, in `_id` order) →
`reduce-and-cluster` (UMAP to 5-d, `sklearn` HDBSCAN there, then a SEPARATE 2-D UMAP fit on the raw
embeddings for display) → `summarise-cluster` (one Gemini call per cluster over ≤ 20 sampled
members: a ≤ 6-word label, a ≤ 100-word summary, 3–5 keywords) → `write-clustering-run`.

Where it lands: one row per cluster in the **`memory_clusters`** collection (`label`, `summary`,
`keywords`, `size`, `sample_chunk_ids`, `centroid`, `run_id`), plus `cluster_id` and
`viz {x, y, run_id}` on every child chunk. Noise gets `cluster_id: -1` and coordinates but no
row. A run REPLACES the previous one wholesale — there is no run history — so chunks ingested
after the last run show up as "unclustered / stale" on the map until you re-run the phase.

```bash
make memory-run-clustering-pipeline
```

It is OFF in every other entry point, the nightly cron included: this is the only command that
clusters. Two notes:

- **Cold start.** The first `import umap` on a machine compiles numba's kernels (~40 s; ~3 s
  afterwards from the on-disk cache), and Prefect Managed runs are fresh containers, so every
  cloud run pays it. Nothing else imports the stack — that is why the phase is a flag, not a
  YAML switch.
- **Small corpora.** Below `memory.clustering.hdbscan.min_cluster_size` (default 15) embedded
  children the run is skipped and nothing is written (the previous run stays readable); if every
  chunk comes back as noise the run IS written and warns. Both log the knob to turn:
  `TREE_MEMORY__CLUSTERING__HDBSCAN__MIN_CLUSTER_SIZE=5 make memory-run-clustering-pipeline`.

### Query CLI

The output follows [`memory.mode`](#memory-modes): in `graphrag` it renders an interactive HTML
graph under `.tree/graphs/` and opens it; in `rag` there is no graph to draw, so it prints the
retrieved parents (score, document title, heading path, a 300-char excerpt and the matched-children
count) as text — and a full-graph run with no `QUERY` exits 1 with an explanatory message.

```bash
# graphrag: visualize the entire graph
make memory-query-graph

# Query a specific topic — HTML graph in graphrag, retrieved parents as text in rag
make memory-query-graph QUERY="Paul Iusztin"
TREE_MEMORY__MODE=rag make memory-query-graph QUERY="Paul Iusztin"
```

### MCP server

Expose the memory to any MCP-aware client (Claude Code, Claude Desktop, Cursor, the bundled harness):

```bash
make memory-serve-mcp                       # stdio transport (default)
make memory-serve-mcp TRANSPORT=streamable-http
```

The repo-root `.mcp.json` already wires this up — Claude Code and the harness auto-spawn it. No extra setup needed from those clients.

**Tools exposed.** The tool set IS the memory mode (`memory.mode`, ADR-006): the server reads it once at boot and registers only what that mode can honour. A graph tool called against a `rag` server returns the standard unknown-tool error — it was never registered.

*Both modes (6 tools):*

| Tool | Description |
|---|---|
| `search_memory` | Hybrid (vector + text) search. **Signature differs per mode** — see below. |
| `search_web` | On-demand web search via Bright Data SERP. **Does NOT touch memory by default.** Opt-in `ingest=true` fires the `ingest-web-url-batch-etl` deployment fire-and-forget — but that deployment is NOT registered (no free-tier slot is spare), so the ingest degrades to `{"triggered": false, "error": …}` while the search result itself still returns. |
| `scrape_web` | On-demand scrape of one or more URLs via Bright Data Web Unlocker. **Does NOT touch memory.** Returns markdown (or HTML) inline for exploration; pair with `search_web` to read SERP results, then call `ingest_url` on whichever URLs are worth keeping. Max 5 URLs per call. |
| `ingest_url` | Ingest a web page (Substack, arXiv, custom) through the data + memory pipelines. |
| `ingest_file` | Ingest a local file. |
| `ingest_conversation` | Ingest a chat transcript into memory. |

*`graphrag` only (7 more, 13 total):*

| Tool | Description |
|---|---|
| `query_memory` | Translates natural language to MongoDB aggregation pipelines via LLM. Best for structured questions, counts, filters. |
| `deep_search_memory` | Broader exploration — persists results to disk for follow-up. |
| `visualize_memory_graph` | Renders the graph (whole graph or one query) as an interactive HTML view. |
| `memory_dashboard` | Graph-statistics dashboard (node/edge counts by type). |
| `review_list_pending` / `review_confirm` / `review_reject` | Human review of the flagged `same_as` duplicate queue. |

*The two `search_memory` signatures* — one name, one registered function per mode, no parameter the mode cannot honour:

| Mode | Signature | Returns |
|---|---|---|
| `rag` | `search_memory(query: str, top_k: int = 10)` | `RetrievalResult` JSON — `{"parents": [{parent_id, chunk_index, heading_path, content, score, document, matched_children}, …]}`, best match first. `top_k` IS the result cap; empty memory answers `{"parents": []}`. |
| `graphrag` | `search_memory(query: str, top_k: int = 10, max_hops: int = 1, max_results: int = 10, visualize: bool = False)` | Serialized nodes + edges after graph expansion, plus the interactive graph when `visualize=true`. |

In `graphrag`, `query_memory` and `search_memory` accept a `visualize` flag that renders an interactive HTML graph.

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
make generate-secret-key                  # generate MODAL_EMBEDDING_API_KEY, put it in .env
make memory-deploy-embedding-model        # deploys to Modal (creates the vllm-embedding-api-key secret)
make memory-deploy-embedding-model-test   # smoke-test the deployment
make memory-deploy-embedding-model-stop   # tear it down
```

Then flip `models.search_embedding` (and, if desired, `models.resolution_embedding`) in `configs/default.yaml` to the Modal provider.

## Testing

```bash
make memory-tests              # unit suite (needs the local MongoDB from make local-start)
```

Layout mirrors the source tree: `tests/unit/<area>/` — unit tests with mocks (`pytest-mock`). The mirroring is enforced for the memory layers by `tests/unit/memory/test_package_layout.py`, which also asserts that no module under `rag/` imports `graph/`. There is no integration suite (see `AGENTS.md`); e2e verification happens by running the real pipelines (see "Running pipelines").

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
      pipeline.py       # the 3 Prefect flows (worker, coordinator, indexing)
      embedding_text.py # shared batching + entity node text
      types.py          # transit types shared by both layers
      rag/              # Chapter 4: cleaning, chunking, embedding, load,
                        #   search, retrieval, indexing — never imports graph/
      graph/            # Chapter 8: extraction, add_entity, dedup, validation,
                        #   judge, first_person_resolver, preference_supersession,
                        #   sharding, resolution/, review/, consolidation/,
                        #   retrieval, kgquery, nl_query, visualize
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
