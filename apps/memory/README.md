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
| **MCP tools** | 7 (`search_memory`, `ingest_*`, `search_web`, `scrape_web`, `visualize_memory_embeddings`) | those 7 + 7 graph tools (see [MCP server](#mcp-server)) |
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

- `models.llm` — provider + model (default: `gemini` / `gemini-3.1-flash-lite`).
- `models.resolution_embedding` — provider + model + dimensions for the **transient** resolution embedding (computed on the entity name during resolution's semantic stage, never persisted). Default: `voyage` / `voyage-4` / 1024.
- `models.search_embedding` — provider + model + dimensions for the **persisted** embedding used for dedup + search/query. Its `dimensions` is what the live mongot `vector_index` is asserted against at boot. Default: `voyage` / `voyage-4` / 1024.
- `memory` — `mode` (`rag` | `graphrag`), `chunking` (`strategy`, `parent.size/overlap`, `child.size/overlap`) and `clustering` (`umap`, `hdbscan`, `sampling`, `summaries`). `clustering` has no `enabled` key on purpose: the ON/OFF switch is the `run_clustering` flow parameter of `offline-pipeline` (default off), not YAML — an `enabled` key is a hard `ValidationError` at boot.
- `extraction` — `llm_concurrency`, `doc_concurrency`, `dedup_concurrency`, plus the `resolution` / `dedup` blocks.
- `query` — `top_k`, `max_hops`, `rrf_k` (reciprocal rank fusion), `embedding_batch_size`, `min_vector_score` (the bar the vector leg must clear before RRF fusion — Atlas-normalised cosine, default `0.70`, re-pinned live on voyage-4 in `tasks/141`; provisional per ADR-008 §4).
- `mcp` — `max_retries`, `max_results`.
- `modal` — the **Modal catalog** (`embedding_models` + `llm_models`) plus the App pins (`autoinference_utils_version`, `engines.vllm/sglang.version`) and the two waits, `warmup_deadline_s` (for a
  cold server) and `request_timeout_s` (for one answer). One entry per Hugging Face model that may be served on Modal: `repo_id`, `revision`, the model's own facts (`native_dimensions`, `matryoshka_dimensions`, `query_prompt`, `document_prompt` for embeddings; `n_gpus` plus the optional request knobs `max_tokens` and `chat_template_kwargs` for LLMs) and the App fields `gpu`, `cpu`, `memory_mb`, `max_model_len`, `extra_server_args`. There is NO `serving` and NO `base_model`: the **Serving path** is decided at deploy time by asking Modal, and the App fields are read only if it refuses (embeddings -> vLLM, LLMs -> SGLang). See ADR-009 §2/§3.

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
| `MODAL_PROXY_TOKEN_ID` | no | — | Modal Proxy token id (`wk-…`) — the only auth in front of a Modal-hosted embedding model |
| `MODAL_PROXY_TOKEN_SECRET` | no | — | Modal Proxy token secret (`ws-…`), sent joined as `Bearer <id>.<secret>` |
| `HF_TOKEN` | no | — | Hugging Face token — only to serve a private or gated repo on Modal |
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
| **memory** → `memory` rows | `run-memory-pipeline` (phases: `extraction` + `index`) | `run-memory-pipeline MODE=online DOC_IDS=…` or `SOURCE_URIS=…` |
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

Dispatches the `online-pipeline` flow with extraction OFF: ingests a single URL or local file in realtime into `documents` **only** — it does NOT extract or index. It prints the new document id; feed that to `make memory-run-memory-pipeline MODE=online DOC_IDS=<id>` (or `SOURCE_URIS=<uri>`, the `source_uri` an ingest receipt carries) to write it into the `memory` collection. `SOURCE` is auto-detected: an `http(s)` URL routes to the web/Substack/YouTube dispatcher; anything else is treated as a local file (`.txt` / `.md` / `.html`). Defaults to the current user; override with `USER_ID` / `USER_IDENTIFIER`. (The MCP `ingest_url` / `ingest_file` tools fire extraction automatically as a realtime convenience; this CLI keeps the two pipelines decoupled. Conversation ingestion is MCP-only.)

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
# Offline — ALL pending documents (batch fan-out; optional NUM_SHARDS=<n>),
# optionally narrowed with DOC_IDS="<id>[,<id2>]" or SOURCE_URIS="<uri>[,<uri2>]"
make memory-run-memory-pipeline
make memory-run-memory-pipeline DOC_IDS="507f1f77bcf86cd799439011,507f1f77bcf86cd799439012"

# Online — ONE document (e.g. the one just produced by run-data-pipeline MODE=online)
make memory-run-memory-pipeline MODE=online DOC_IDS="507f1f77bcf86cd799439011"

# Online — retry from an ingest receipt's source_uri (resolved to ids at flow
# entry; an unknown URI fails the run instead of extracting nothing). Mixable
# with DOC_IDS.
make memory-run-memory-pipeline MODE=online SOURCE_URIS="https://www.youtube.com/watch?v=abc"
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

#### Changing the embedding model

`memory` rows carry **no embedding-model stamp**, and the backfill only fills vectors that are
EMPTY — so switching `models.search_embedding` (or changing the **Embedding role** rule) leaves
every old vector in place, in a space the new ones don't share. Measured on live voyage-4, the same
text embedded role-less (the legacy way) scores cos=0.983 against its `query` embedding but only
0.774 against its `document` one: old vectors aren't merely older, they're incomparable, and hybrid
search quietly degrades instead of failing. The migration is an **Embedding reset** — empty the
vectors, then let the existing phases refill them (ADR-009 §7):

```bash
make memory-reset-embeddings                  # DRY RUN: counts, writes nothing, exits 1
make memory-reset-embeddings CONFIRM=yes      # empties them
make memory-run-indexing-pipeline             # re-embeds with the CURRENT model
make memory-run-clustering-pipeline           # only if you use the Embedding map
```

Three things to know:

- **It is per user.** The reset resolves ONE tenant (`USER_ID` / `USER_IDENTIFIER`, like every other
  command here); on a multi-user environment, loop over your users — there is no `--all-users`.
- **Between step 1 and step 2, search runs on the text leg.** The emptied rows carry no vector, so
  `$vectorSearch` returns nothing for them and retrieval reports `text_only` / fewer hits. Run the
  indexing pipeline right after, and re-run it if it fails: both the reset and the backfill are
  idempotent (a second reset matches 0 rows and writes nothing).
- **The map goes stale on purpose.** The reset clears the child chunks' `cluster_id` / `viz`, so
  `make memory-visualize-embeddings` warns "N of M chunks have no cluster assignment (or a stale
  one)" instead of silently drawing coordinates from the old space. `memory_clusters` rows are left
  alone — the next clustering run replaces them wholesale.

`CONFIRM=yes` is the only guard: the command is destructive on whichever environment is active
(`make env-status`), production included.

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
clusters. What READS the result is the [Embedding map](#embedding-map) (`make
memory-visualize-embeddings` and the `visualize_memory_embeddings` MCP tool) — surfaces draw the
stored coordinates, they never cluster. Two notes:

- **Cold start.** The first `import umap` on a machine compiles numba's kernels (~40 s; ~3 s
  afterwards from the on-disk cache), and Prefect Managed runs are fresh containers, so every
  cloud run pays it. Nothing else imports the stack — that is why the phase is a flag, not a
  YAML switch.
- **Small corpora.** Below `memory.clustering.hdbscan.min_cluster_size` (default 15) embedded
  children the run is skipped and nothing is written (the previous run stays readable); if every
  chunk comes back as noise the run IS written and warns. Both log the knob to turn,
  `TREE_MEMORY__CLUSTERING__HDBSCAN__MIN_CLUSTER_SIZE` — but set it where the FLOW runs, not where
  you type `make`: the command only dispatches and forwards no environment, so a variable in your
  shell is a silent no-op (the command warns when it sees one). Locally, `export
  TREE_MEMORY__CLUSTERING__HDBSCAN__MIN_CLUSTER_SIZE=5` in the shell that runs `make
  memory-serve-workflows` and restart it; on Prefect Managed put it in the deployment's
  environment. Then re-run `make memory-run-clustering-pipeline` as usual.

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

#### Embedding map

The 2-D picture of the [clustering run](#memory-clustering): one point per **child chunk** at its
stored `viz {x, y}`, coloured by cluster, with the LLM-written label and size per cluster in the
legend and the document title / heading path / snippet in the tooltip. Same renderer as the graph
(fixed coordinates, ForceAtlas2 skipped) — and identical in both memory modes, because the map has
no edges to miss.

```bash
make memory-visualize-embeddings                        # open the latest map
make memory-visualize-embeddings HULLS=true             # outline each cluster
make memory-visualize-embeddings OUTPUT=/tmp/map.html USER_IDENTIFIER=paul
```

It READS; it never clusters (ADR-007 Decision 8), so two outcomes are contracts rather than bugs:

- **No clustering run for this user** — it prints `No clustering run found for this user — run make
  memory-run-clustering-pipeline to build the embedding map.` and exits 1. No empty canvas.
- **A stale map** — chunks ingested since the last run have no coordinates, so the FIRST line of
  output is `N of M chunks have no cluster assignment (or a stale one) — run make
  memory-run-clustering-pipeline`; those points are left off the map and counted in the legend as
  "unclustered / stale (not shown)". Re-run the clustering phase to clear it.

The `visualize_memory_embeddings` MCP tool below answers with the same map, the same warning line
and the same message — one behaviour, two surfaces.

### MCP server

Expose the memory to any MCP-aware client (Claude Code, Claude Desktop, Cursor, the bundled harness):

```bash
make memory-serve-mcp                       # stdio transport (default)
make memory-serve-mcp TRANSPORT=streamable-http
```

The repo-root `.mcp.json` already wires this up — Claude Code and the harness auto-spawn it. No extra setup needed from those clients.

**Tools exposed.** The tool set IS the memory mode (`memory.mode`, ADR-006): the server reads it once at boot and registers only what that mode can honour. A graph tool called against a `rag` server returns the standard unknown-tool error — it was never registered.

*Both modes (7 tools):*

Every tool answers failures as data, never as an MCP protocol error: `{"error_type": …, "retryable": true|false, "message": …}` (ADR-008 §2). Retry the same call only when `retryable` is true; otherwise change the input or stop.

| Tool | Description |
|---|---|
| `search_memory` | Hybrid (vector + text) search. **Signature differs per mode** — see below. |
| `search_web` | On-demand web search via Bright Data SERP. **Does NOT touch memory by default.** Opt-in `ingest=true` fires the `ingest-web-url-batch-etl` deployment fire-and-forget — but that deployment is NOT registered (no free-tier slot is spare), so the ingest degrades to `{"triggered": false, "error": …}` while the search result itself still returns. |
| `scrape_web` | On-demand scrape of one or more URLs via Bright Data Web Unlocker. **Does NOT touch memory.** Returns markdown (or HTML) inline for exploration; pair with `search_web` to read SERP results, then call `ingest_url` on whichever URLs are worth keeping. Max 5 URLs per call. |
| `ingest_url` | Ingest a web page (Substack, arXiv, custom) through the data + memory pipelines. |
| `ingest_file` | Ingest a local file. |
| `ingest_conversation` | Ingest a chat transcript into memory. |
| `visualize_memory_embeddings` | Draws the [Embedding map](#embedding-map) of the latest clustering run: chunks as points coloured by cluster, `hulls=true` outlines them. READS only — with no run it answers with the `make memory-run-clustering-pipeline` message, and a stale map's answer starts with the warning line. In both modes: the map has no edges. |

*`graphrag` only (7 more, 14 total):*

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
| `rag` | `search_memory(query: str, top_k: int = 10)` | `RetrievalResult` JSON — `{"parents": [{parent_id, chunk_index, heading_path, content, score, document, matched_children}, …], "outcome": "found" \| "nothing_found", "search_mode": "hybrid" \| "text_only" \| "vector_only"}`, best match first. `top_k` IS the result cap; nothing above `min_vector_score` (or an empty memory) answers `"outcome": "nothing_found"` with `"parents": []`; a dead search leg shows as a non-`hybrid` `search_mode` (ADR-008 §3). |
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

#### SessionEnd hook

Claude Code sessions persist themselves through the same MCP surface — there is
no second path into memory (ADR-008 §5). The repo-root `.claude/settings.json`
wires `SessionEnd` to `scripts/hook_session_end.py`, which is glue around
`tree.mcp.hooks` (stdlib + `fastmcp` + `pydantic` only, AST-enforced — the hook
cannot drift into pipeline internals):

```json
"hooks": {
  "SessionEnd": [{"hooks": [{
    "type": "command",
    "command": "uv --directory apps/memory run python scripts/hook_session_end.py tree-memory-local",
    "timeout": 60
  }]}]
}
```

**What it stores.** The `user` / `assistant` turns of the transcript, text
blocks only — `thinking`, `tool_use` and `tool_result` blocks never reach
memory — as one `ingest_conversation` call with
`session_uri="claude-session://<session_id>"`,
`title="Claude Code session <id[:8]> — <YYYY-MM-DD>"` and `session_started_at`
from the first turn. `session_uri` is the natural key, and the first write
wins: ending the same session twice answers `duplicate: true` and re-ingests
nothing. The receipt is logged as one line (`source_uri`, `duplicate`,
`flow_run_id`, `status`); ingestion itself runs out-of-band, so the session is
searchable only after the `online-pipeline` run finishes.

**Guards — the hook always exits 0.** A session ending must never fail on
memory, so each of these is one log line and a skip: a transcript under **200
words** (`Transcript 30 words < 200 — skipped.` — a two-line session is noise),
a missing transcript, an unreachable server (Mongo down → the spawned server
dies in its lifespan), or a **Tool error envelope** in the answer (the log names
`error_type` and `retryable`). The spawned local server is started with
`MCP_SKIP_INDEX_BOOTSTRAP=1`, so it queries indexes instead of building them and
boots in seconds — `SessionEnd` hooks share a 1.5 s budget that the configured
`timeout: 60` raises to 60 s.

**Local server only, for now.** The hook targets `tree-memory-local`, and the
remote `tree-memory` entry is passed through as written. Pointing the hook at
`tree-memory` today therefore fails auth — the client sends no credentials, the
server answers 401, and the session end logs one skip; persistent OAuth token
storage is the prerequisite for the cloud route (follow-up), because `fastmcp`
3.2.0 keeps OAuth tokens in memory only, so an `auth: "oauth"` entry would
re-open a browser login at every session end instead. Nobody has walked the
cloud path end-to-end; treat `tree-memory` as unsupported until then.

**Disable it.** Set `"disableAllHooks": true` in `~/.claude/settings.json` or
`.claude/settings.local.json`; for one run, start Claude Code with
`claude --settings '{"disableAllHooks": true}'`. To remove it for good, drop the
`hooks` block from `.claude/settings.json`. There is no per-hook switch — hook
entries MERGE across settings levels, so a local settings file cannot unset the
project's hook ([hooks
reference](https://docs.claude.com/en/docs/claude-code/hooks)).

Smoke-test it without ending a session — the fixture transcript doubles as the
e2e payload:

```bash
echo '{"session_id":"smoke-1","transcript_path":"tests/unit/mcp/fixtures/session_end_transcript.jsonl","cwd":"'"$PWD"'","hook_event_name":"SessionEnd","reason":"other"}' \
  | uv --directory apps/memory run python scripts/hook_session_end.py tree-memory-local
```

## Serving models on Modal (optional)

Serve a model yourself, on your own GPU, instead of calling a hosted API. One
model = one entry in the **Modal catalog** (`modal.embedding_models` or
`modal.llm_models` in `configs/default.yaml`) + one command; the entry is the
single source of truth that both the deploy driver and the clients read, so a
model's app name, vector width and prompts cannot drift apart.

**1. The YAML names the model, never the path.** An entry is a Hugging Face
`repo_id`, its `revision`, and the facts about the model itself (for an
embedding model its `native_dimensions`, `matryoshka_dimensions` and the two
role prompts; for an LLM its `n_gpus` and its two optional request knobs) plus optional hardware defaults. There
is no `serving:` field: whether Modal can serve a model is something only Modal
knows.

`make memory-deploy-model MODEL=<repo_id>` asks it. The driver runs
`modal endpoint create` first and logs ONE line with the decision and its
reason:

```
Routing Qwen/Qwen3-Embedding-0.6B: Modal accepted it → Dedicated endpoint tree-qwen3-embedding-0-6b
Routing voyageai/voyage-4-nano: not in Modal's endpoint catalog, no catalog base → vLLM App
Routing LiquidAI/LFM2.5-350M: not in Modal's endpoint catalog, no catalog base → SGLang App
```

- **Accepted** → a **Dedicated endpoint**: Modal picks the recipe, GPU, engine
  and flags, and we write no serving code.
- **Refused** (`… is not available for dedicated Endpoints`) → the driver reads
  the model's fine-tune lineage from the Hub and, if an ancestor IS in the list
  Modal just printed, retries it as custom weights
  (`--custom-hf-repo`/`--custom-hf-revision`). Otherwise it deploys **our App
  for the model's kind**: embeddings go to `deploy/modal_vllm_embedding.py`,
  LLMs to `deploy/modal_sglang_llm.py` — Modal's own eject path, the `serve.py`
  its **Source view** generates, parameterised by the catalog entry, which
  crosses into the container as one JSON env var. Engine flags live in the
  entry's `extra_server_args`, never in the scripts. Modal's endpoint catalog
  held 44 models on 2026-09-20, two of them embedding models, so the Apps are
  the general path.

**1a. Serving your own LLM.** Any Hugging Face LLM Modal will not host goes to
the SGLang App — `make memory-deploy-model MODEL=LiquidAI/LFM2.5-350M` is the
whole command, and the entry is five YAML lines:

```yaml
llm_models:
  - repo_id: LiquidAI/LFM2.5-350M
    revision: <the commit sha you want served> # pinning one is what makes a re-deploy reproducible
    gpu: A10
    n_gpus: 1 # = SGLang's tensor parallelism, and the `:N` of the GPU string
    max_model_len: 32768 # the card's context is 128k; 32k keeps the KV cache small
    max_tokens: 4096 # the completion budget sent on every chat request
    chat_template_kwargs: { enable_thinking: false } # what the chat template is told, e.g. thinking off
```

`max_tokens` and `chat_template_kwargs` are **request** knobs, not server
flags: both clients and the chat smoke test send them from one helper, so they
behave identically on both Serving paths and changing either needs **no
redeploy** — write the line, re-run `-test`. Omit a knob and nothing is sent
for it (the smoke test then spends its own 256-token bound). `enable_thinking:
false` is how a THINKING model is told to skip reasoning: with it on,
`Qwen/Qwen3.5-0.8B` spent its whole budget reasoning and returned an empty
message. On a **Dedicated endpoint** that is not enough — with thinking off the
same model, and `google/gemma-3-1b-it` which has no thinking mode at all, both
ran the strict-JSON decoder into an unbounded filler run until
`finish_reason=length`, while plain JSON mode answered sanely (`tasks/141`
round 2). Strict-schema completions are proven on the **App** path
(`LiquidAI/LFM2.5-350M`), not on the endpoint one.

`deploy/modal_sglang_llm.py` follows the `serve.py` Modal generates for its own
LLM endpoints: the official `lmsysorg/sglang:<tag>` image (the engine is IN the
image — `modal.engines.sglang.version` is a DOCKER TAG, not a PyPI version),
`SGLangEndpoint(tp=<n_gpus>)`, and a warm-up that is a chat completion under a
strict JSON schema — **a server that cannot do constrained JSON never reports
healthy**, because constrained JSON is exactly what the memory asks of it. The
flags are the generic-safe subset (`--served-model-name`, `--revision`,
`--trust-remote-code`, `--mem-fraction-static 0.85`, `--context-length`);
anything model-specific — `--reasoning-parser`, `--tool-call-parser`, a lower
memory fraction — goes in that entry's `extra_server_args`, which also
overrides those two tuning defaults. Modal's own recipes add speculative
decoding with a per-model draft model plus mamba/multimodal flags; a script
that serves whatever the catalog names cannot assume any of it, so it does not.
- **Any other failure** (auth, quota, network, a text we do not know) → the
  deploy ABORTS with Modal's own message and exit code. An unknown failure is
  not evidence that a model is ineligible.

`SERVING=endpoint|app` is an escape hatch for ONE command — e.g. to pin the
vLLM version yourself on a model Modal would accept. It is never written down:
the clients resolve the same app name either way, and `-stop` tries both.

`HF_TOKEN` in `.env` is optional and only for private or **gated** repos: a
custom-weights endpoint create receives it as `--custom-hf-token` (logged as
`***`), an App as a Modal Secret built at deploy time — so a gated *base* model
is served with `SERVING=app`, not as an endpoint.

*Troubleshooting:* `401` / `403` / `GatedRepoError` from `huggingface.co` in
`modal app logs ep-<endpoint_name>` (e.g. `modal app logs ep-tree-voyage-4-nano`)
→ accept the model's licence on its Hub page, set `HF_TOKEN` in `.env`, deploy
again. The `HF_TOKEN` hint the driver prints appears only for those
gated-looking failures — never under a cold-start `503` or an architecture
mismatch, which no token fixes.

**1b. Nothing these targets touch is yours by accident.** Every name we create
on Modal starts with `tree-` (endpoint `tree-<model>`, app `ep-tree-<model>`),
so a Dedicated Endpoint you made by hand in the dashboard (`ep-<model>`) can
never be overwritten or stopped by these targets. On top of that:

- `deploy` looks before it writes: it refuses (exit 3) when the name is already
  live as something this command would not merely update — a Dedicated Endpoint
  before either route. A live App of yours is redeployed in place (stop it first
  if you want it re-routed). `FORCE=yes` overrides that refusal (it never
  overrides the `tree-` check); `-stop` only ever stops `tree-` names.
- `DRY_RUN=yes` prints the (redacted) `modal` command and exits 0 without
  starting `modal` or reading the Hub — the ONLY way to try these targets
  without deploying. Since Modal is the oracle, it also says which command each
  verdict would lead to:

  ```bash
  make memory-deploy-model MODEL=voyageai/voyage-4-nano DRY_RUN=yes
  ```

  A fake `modal` on `PATH` does NOT work: `make` and `uv run` put `.venv/bin`
  first, so the real CLI wins — which is how two accidental deploys once
  happened. The unit suite therefore closes both doors to Modal itself — the
  CLI one and the SDK one — with two autouse fixtures (see "Tests & QA").

**2. Mint a Proxy token** in Modal → Settings → Proxy Auth Tokens and put both
halves in `.env` as `MODAL_PROXY_TOKEN_ID` / `MODAL_PROXY_TOKEN_SECRET`. It is
the only auth in front of every model, on both paths: Modal's edge rejects
unauthenticated traffic *before* a GPU container wakes. (`HF_TOKEN` is separate
and optional — it downloads private or gated weights, it never authenticates a
request.)

**3. Deploy, smoke-test, stop** — always with `MODEL=<repo_id>`:

```bash
make memory-deploy-model MODEL=Qwen/Qwen3-Embedding-0.6B        # serve it
make memory-deploy-model-test MODEL=Qwen/Qwen3-Embedding-0.6B   # health, dims, ranking, 401-without-token
make memory-deploy-model-stop MODEL=Qwen/Qwen3-Embedding-0.6B   # stop paying for it, whichever way it was served
```

`-stop` needs no `SERVING=`: it stops the Dedicated Endpoint if the model is
one, and the App otherwise. `-test` covers both kinds — the entry's kind picks
the test, so `make memory-deploy-model-test MODEL=LiquidAI/LFM2.5-350M` asks the
LLM for one strict-JSON chat completion instead of embedding three texts:

```
health 200 after 96.0s
served model id: LiquidAI/LFM2.5-350M
chat completion: {"city": "Tokyo", "population": 13960000}
strict JSON schema honoured: city=Tokyo population=13960000
unauthenticated health -> 401
Smoke test passed
```

The smoke test polls `/health` until the container is up — a scaled-to-zero
Modal server answers HTTP 503 in about a second and boots *because* it is
polled — for up to `modal.warmup_deadline_s` (600 s; ~3x the slowest boot
measured). A first-ever deploy that also downloads tens of GB of weights can
outlast that: raise the budget for one command, without editing a file, with
`make memory-deploy-model-test MODEL=<repo_id> TREE_MODAL__WARMUP_DEADLINE_S=1200`.
A 401/403 (wrong **Proxy token**) or a 404 is never waited out — it fails after
one poll.

That budget waits for a COLD server; `modal.request_timeout_s` (300 s;
`TREE_MODAL__REQUEST_TIMEOUT_S`) bounds ONE answer from a living one — the
smoke test's completion and every call both Modal clients make, sent once, with
no SDK retries. A timeout is therefore never read as a cold start (a cold
server answers 503 in a second): it fails with the knob's name instead of
re-warming and waiting again.

A **Dedicated endpoint** is `provisioning` before it is `live`: `modal endpoint
create` returns in a few seconds and the endpoint starts serving minutes later
(measured 2026-09-21: 2m15s for `Qwen/Qwen3-Embedding-0.6B`, 9m25s for
`Qwen/Qwen3.5-0.8B`). `-test` waits that out by itself — one read-only `modal
endpoint list --json` on the same poll schedule, for up to 30 minutes
(`Provisioning: tree-qwen3-5-0-8b is not live yet — 120s/1800s` … `Live:
tree-qwen3-5-0-8b after 548s`) — and only then polls `/health`. An App has no
such row, so nothing is waited for there.

**4. Point the memory at it.** Set `models.search_embedding` (and, if you want,
`models.resolution_embedding`) in `configs/default.yaml` to
`{provider: modal, model: Qwen/Qwen3-Embedding-0.6B, dimensions: 1024}`. The
client asks the server for the model's native width, then truncates +
L2-renormalises client-side to `dimensions` — so a model whose native width is
wider (e.g. `voyageai/voyage-4-nano` at 2048) still fits the 1024-d vector
index, and a mismatch between the catalog and the server fails loudly instead of
writing a wrong-width vector.

For an **LLM** it is the same switch one block up: `models.llm: {provider: modal,
model: LiquidAI/LFM2.5-350M}` (any id from `modal.llm_models`) sends every
`generate_json` call to your own server, in JSON mode, instead of to Gemini —
which stays the default. For one run without editing the file:
`TREE_MODELS__LLM__PROVIDER=modal TREE_MODELS__LLM__MODEL=<repo_id>`. Whether a
small model is good enough for extraction is yours to judge; the client
guarantees the plumbing, not the quality.

A Dedicated endpoint's min/max containers are dashboard-only settings; to change
what was created, stop the endpoint first and deploy again.

## Testing

```bash
make memory-tests              # unit suite (needs the local MongoDB from make local-start)
```

Layout mirrors the source tree: `tests/unit/<area>/` — unit tests with mocks (`pytest-mock`). The mirroring is enforced for the memory layers by `tests/unit/memory/test_package_layout.py`, which also asserts that no module under `rag/` imports `graph/`. There is no integration suite (see `AGENTS.md`); e2e verification happens by running the real pipelines (see "Running pipelines").

*Safety:* the unit suite closes TWO doors to Modal, both as autouse fixtures in `tests/unit/conftest.py` — the CLI door (`TREE_MODAL_DRY_RUN=1`, so the deploy driver starts no `modal` process; a fake `modal` on `PATH` does not work, see above) and the SDK door (`modal.Server.from_name` and its neighbours raise `live Modal SDK lookup from a unit test`, so a test that flips a provider to `modal` without patching the factory fails instead of waking a GPU; request the `modal_sdk_allowed` fixture to opt one test out).

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
                        #   retrieval, kgquery, nl_query
      clustering/       # neutral: the Clustering run (core, summaries, store)
      visualize/        # neutral: the Graph renderer (graph.py) + the
                        #   Embedding map payload (embeddings.py)
    mcp/                # FastMCP server + tools; viz_app.py = the neutral
                        #   MCP App layer (ui:// + graphs:// + dual delivery)
    db.py               # Mongo + Beanie init
    orchestrator.py     # Prefect `serve(...)` registering deployments
  configs/default.yaml  # app tuning
  deploy/               # Modal App deploy scripts (vLLM embeddings, SGLang LLMs), Prefect, Atlas
  scripts/              # CLI entrypoints (serve_mcp, run_*, query_graph,
                        #   visualize_embeddings, signup, check_db)
  tests/unit
  docker/Dockerfile     # image used by the compose `prefect-worker`
  Makefile              # app-local targets (see make memory-help)
  pyproject.toml, uv.lock
```
