# The Why

**Tree: Your Rooted Personal Assistant.** Build a personal assistant rooted in a knowledge-graph memory, powered by ontologies, LLMs, and agents.

## Key Principles You Will Respect All Over Your Work

- Always prioritize removing instructions over adding more.

# The What 

The assistant is based on two apps:

- `apps/harness`: the user facing component, built as a TUI CLI app that implements a custom harness
- `apps/memory`: the context layer, served via an MCP server to the harness, that contains the ingestion and retrieval pipelines for the knowledge graph

## Harness Key Components

... TBD

## Memory Key Components

- **Data Pipeline:** ETL pipelines gathering data from multiple sourcing and normalizing everything into the `documents` collection. One ETL pipeline per source, such as Substack, Substack RSS feeds, HuggingFace Datasets, YouTube, Custom sites, etc.
- **Memory Pipeline:** Pipeline that maps `documents` to `knowledge graph objects` within the `knowledge_graph` collection by cleaning, chunking, graph extracting, normalizing and upserting nodes and edges directly.
- **The Unified Memory:** The agent's unified memory powered by MongoDB that leverages text, semantic and graph search. The data is stored in a single mutable `knowledge_graph` collection.
- **Agentic Tools:** Tools used to query or write to the unified memory. 
- **MCP Server:** The memory is served as an MCP server to the harness, allowing the harness to query and write to the memory.
- **Configuration:** The configuration is done in two ways. 
  1. We use the root `.env` file to inject environment variables, such as credentials or higher level configuration that is required to boot the harness and memory. You can check the `.env.example` to see the available environment variables. The env vars are loaded into the code at runtime via `apps/memory/src/tree/config/settings.py`.
  2. For all the memory application configuration we use YAML files configured in `apps/memory/src/tree/config`, which is loaded into the code at `apps/memory/src/tree/config/app_config.py`.
  3. **Escape hatch.** Operators may override any YAML key via `TREE_<SECTION>__<KEY>` env vars — for example `TREE_EXTRACTION__DEDUP__AUTO_MERGE_THRESHOLD=0.99`. The mechanism is `_apply_env_overrides` in `app_config.py`. This is for emergency one-shot ops use; new knobs should not be documented in `.env.example`.

### Memory App Specifics

- Memory-app scripts (entry points in `apps/memory/scripts/`) must call `init_logger()` from `tree.logging` at module level to configure logging.

## Project Structure

This is a monorepo. Each app lives under `apps/` and owns its own build files (`pyproject.toml`, `Makefile`, etc.). Only cross-app concerns live at the repo root.

```
project-root/
├── apps/
│   ├── memory/                          # Python app: ETL + knowledge graph + MCP server
│   │   ├── src/
│   │   │   └── tree/                    # Core Python module
│   │   │       ├── config/              # Configuration (settings.py, app_config.py, paths.py)
│   │   │       ├── entities/            # Shared data structures as ODMs (documents, knowledge_graph, ontology, users, ...)
│   │   │       ├── models/              # LLM/embedding model interfaces (Gemini, Voyage, Modal, SentenceTransformer)
│   │   │       ├── db.py                # Database connection helpers
│   │   │       ├── logging.py           # Logger configuration
│   │   │       ├── orchestrator.py      # Prefect deployment registration + serve
│   │   │       ├── sharding.py          # Shared sharding helpers
│   │   │       ├── data/                # Data ETLs — one subpackage/file per source
│   │   │       │   ├── core/            # Core ingestion business logic
│   │   │       │   ├── pipeline.py      # Data ETL orchestrator/worker flows
│   │   │       │   ├── huggingface/     # Per-source ETLs (arxiv, ...)
│   │   │       │   ├── substack/        # Substack article + RSS ETLs
│   │   │       │   ├── web/             # Web scrape/search/SERP/unlocker ETLs
│   │   │       │   └── youtube/         # YouTube video + RSS ETLs
│   │   │       ├── mcp/                 # FastMCP server, tools, ingest, dashboard, deep search
│   │   │       └── memory/              # Unified memory module
│   │   │           ├── types.py
│   │   │           ├── extraction/      # Chunking, graph extraction, dedup, judge, supersession
│   │   │           ├── indexing/        # Post-extraction indexing (reverse edges, embeddings, search indexes)
│   │   │           ├── query/           # Query interfaces (NL query, kg query, visualize)
│   │   │           ├── resolution/      # Entity resolution (exact, alias, fuzzy, semantic, composite)
│   │   │           ├── review/          # Human-in-the-loop review of pending writes
│   │   │           └── consolidation/   # Scheduled dream consolidation
│   │   ├── deploy/                      # Cloud deployment scripts (Modal, Prefect, MongoDB, FastMCP)
│   │   ├── configs/                     # App YAML configs (default.yaml)
│   │   ├── scripts/                     # Entrypoints (serve_mcp.py, run_*.py, ...)
│   │   ├── tests/                       # unit/ + integration/
│   │   ├── docker/Dockerfile            # Memory-app image (Prefect worker)
│   │   ├── pyproject.toml
│   │   ├── uv.lock
│   │   ├── .python-version
│   │   └── Makefile                     # Memory-app targets
│   └── harness/                         # TS/Bun coding-agent harness (TUI CLI) — see docs/harness-plan.md
│       ├── src/
│       │   ├── agent/                   # Agent loop + subagents
│       │   ├── tools/                   # Built-in tools (bash, edit, read, write, grep, glob, task, ...)
│       │   ├── mcp/                     # MCP client + adapter
│       │   ├── hooks/                   # Hook config + runner
│       │   ├── permissions/             # Permission policy
│       │   ├── session/                 # Session store, resume, paths
│       │   ├── ui/                      # Slash commands + UI
│       │   ├── client.ts                # Model client
│       │   └── messages.ts
│       ├── tests/                       # unit/ + integration/
│       ├── package.json
│       ├── bun.lock
│       └── Makefile                     # Harness-app targets
├── docker/                              # SHARED infra (MongoDB + mongot config files)
├── docs/                                # Architecture & design docs (incl. adrs/, harness-plan.md)
├── tracker/                             # Task/issue tracker (done/ holds completed entries)
├── .env / .env.example                  # Shared secrets (Mongo creds, API keys)
├── .mcp.json                            # MCP servers the agents/harness spawn
├── docker-compose.yml                   # Shared infra orchestration (+ docker-compose.ci.yml)
└── Makefile                             # Thin root: delegates to apps/*/Makefile; shared infra targets
```

### Make command convention

- `make memory-<target>` — run `<target>` inside `apps/memory/` (e.g. `make memory-unit-tests`, `make memory-serve-mcp`).
- `make harness-<target>` — reserved for the future TS harness at `apps/harness/`.
- `make local-start` / `make local-stop` / `make local-restart` — shared Docker infra.
- `make tests` — aggregate: runs all apps' tests.
- `make pre-commit` — pre-commit across the repo.
- `make help` — list all root targets.

## Key Software Design Choices

- All the dates are timezone aware (UTC by default). We don't accept any naive datetime objects.
- Always add types to function or method parameters and return types. Even if they return `None`.
- Properties of pipelines:
  - Idempotency
  - Retries
  - Checkpointing

### Key TypeScript Design Choices

... TBD

### Key Python Design Choices

- We are using Python with async patterns.
- Loose clean architecture design decoupling infrastructure, serving, app and domain logic:
    - The `entities` folder defines shared ODM, enums or other data structures data are used all over the project. While we have local `types.py` files per app module to define data types that will be used only within that current module or layers upwards. 
    - Infrastructure exceptions we don't plan to change: MongoDB, Prefect, Opik. Thus, it doesn't make sense to make them modular. 
    - Flat structure and naming based on actionability rather than dogmatic clean architecture.

## Writing Tests

Always call the `/testing-python` skill from the Squid plugin when writing tests.

## Tech Stack

### Core
- **Data validation and structuring:** Pydantic
- **ODM:** Beanie + PyMongo Async driver
- **MCP Server Framework:** FastMCP
- **Testing:** Pytest
- **CLI:** Click
- **Logging:** Native Python logger (never prints!)
- **Embedding Models:** Sentence Transformers (local + open-source), Voyage AI (API + closed-source), Modal (remote + open-source)

### Services
- **Frontier Model API:** Gemini
- **Embedding Models API:** Voyage AI
  - [Documentation Text Embeddings](https://docs.voyageai.com/docs/embeddings)
  - [API Reference Text Embeddings](https://docs.voyageai.com/reference/embeddings-api)
  - [Documentation Multimodal Embeddings](https://docs.voyageai.com/docs/multimodal-embeddings)
  - [API Reference Multimodal Embeddings](https://docs.voyageai.com/reference/multimodal-embeddings-api)
- **Searching, crawling, scraping:** Bright Data

### Infrastructure
- **Unified memory and database:** MongoDB
- **Serving AI Models & Remote Sandboxing**: Modal
- **Observability and evals:** Opik
- **Containerization:** Docker
- **CI/CD:** GitHub Actions
- **Pipeline Orchestrator and Agentic Runtime Durable Workflows:** Prefect
  - Sitemap: https://docs.prefect.io/llms.txt
  - You can access deployments via `uv run prefect deployment ...` CLI commands. For example, to run a deployment served 
in @apps/memory/src/tree/orchestrator.py you can run `prefect deployment run [DEPLOYMENT_NAME]` (invoked from within `apps/memory/`).

### Access Dynamic Documentation 

Use the `context7` MCP server to find out more about the tech stack usage and good practices.

# The How 

We manage all the core commands through GNU Make as our command center. File available at @Makefile. Run all the commands with `make ...`

We use `uv` to manage our `apps/memory` Python project such as the virtual environment(s), dependencies, and overall package the project. Run `uv run <command>` to execute commands within the Python virtual environment.

We use `bun` to manage our `apps/harness` TypeScript project such as the virtual environment(s), dependencies, and overall package the project. Run `bun run <command>` to execute commands within the TypeScript virtual environment.

## Populating the Users Collection

By default, you will use the "Paul Iusztin" user when testing.

## Build

```
make memory-build
```

## Developing New Features and Bug Fixes Workflow

Use Squid's plugin `/night` and `/day` skills to do any changes to the codebase. Follow one-on-one the processes defined there.

## Testing & QA

**Always run tests via the `make memory-*` targets, not a bare `uv run pytest`.** The Makefile does `include .env`/`export`, so credentials like `VOYAGE_API_KEY` are present; a bare `uv run pytest` does NOT load `.env`, so live-model tests fail with "Voyage API key is required" — which looks like real breakage but is just a missing-env artifact of the wrong invocation.

For the `apps/memory` Python project, we use `ruff` as our formatter and linter.

### Step-by-Step Verification Steps

During development, run these steps after every commit to git:

 1. Format and lint: `make memory-format-fix && make memory-lint-fix && make memory-format-check && make memory-lint-check`
 2. Pre-commit: `make pre-commit`
 3. Unit tests: `make memory-unit-tests`
 4. Exceptions case by-case:
  - Run only the fast integration tail (skips `@pytest.mark.slow` tests) when doing changes to the infra layer: `make memory-integration-tests`
  - Run only the slow integration tail when iterating on a vector-index or full-Prefect-e2e change: `make memory-integration-tests-slow`

When the feature is considered done and ready for PR, ALWAYS run:

 5. Full integration tests: `make memory-integration-tests-all` (~5 min; includes `@pytest.mark.slow`). CI runs this same target.
 6. Run and verify the code end-to-end. For example, when testing the memory run: `make memory-serve-workflows & `→ `make memory-run-data-pipeline` → `make memory-run-memory-pipeline-extraction` → `make memory-run-memory-pipeline-indexing` → `make memory-query-graph QUERY="test query"` → verify results. Always adapt this e2e example based on the modifications you've made. If necessary you should run multiple tests covering all the modifications you've made in the feature PR you are working on.

### Other Useful Commands

Mirror the CI integration command locally (skip mongot-dependent tests; runs sequentially because the shared-DB cleanup fixture makes parallel `-n auto` workers collide):
```
make memory-integration-tests-ci
```

Or run all tests together (aggregate across apps):
```
make tests
```

### Test-marker hierarchy

Two **orthogonal** pytest markers gate test selection:

- `@pytest.mark.slow` — tests that take >3s or require vector-index convergence / full Prefect e2e. Excluded from the fast inner loop (`make memory-integration-tests`); included in `make memory-integration-tests-all` and the local CI mirror.
- `@pytest.mark.requires_mongot` — tests that need a working Atlas Search / mongot service (live `$vectorSearch`, `create_search_index`, or the `_skip_without_mongot` fixture). **Excluded from CI** because mongot's Search Index Management gRPC channel is unreliable on GitHub runners (CI run 25989844295: 16 connectivity errors + 7 five-minute hangs). Included in every local target, where the full `docker-compose.yml` stack brings mongot up. A test can be `slow` without needing mongot, or vice versa.

## Running Pipelines

To test a pipeline after making changes:

1. **Serve the workflows** in a background process to pick up the latest code:
```
make memory-serve-workflows &
```
If a serve process is already running, kill it first and re-serve to pick up the latest code changes.

2. **Run the pipeline** via the corresponding Make command (which streams logs to the terminal), such as:
```
make memory-run-data-pipeline
make memory-run-memory-pipeline-extraction
make memory-run-memory-pipeline-indexing
```

The `make memory-serve-workflows` process must be running for pipeline triggers to be picked up, as it acts as the in-process Prefect worker. Without it, deployments are registered but no worker will execute them.

Always use these Make commands instead of `prefect deployment run` directly, as the scripts stream all logs (including errors) back to the current process so you can debug without checking the Prefect UI.

## Running Custom Commands for Accessing Infrastructure and External Services 

Always use the following CLIs installed directly on the system:

- MongoDB: `mongosh` CLI for accessing any MongoDB instance.
- GitHub: `gh` CLI to interact with the remote GitHub repository this project is attached to (e.g., accessing PRs, issues or GitHub Actions)
- Git: `git` CLI for any Git operations.

## Running Custom Commands for Project Level Dependencies

Use `uv` to run any CLI that installed via the `uv` virutal environment, rather than as a binary on the host. Available in @apps/memory/pyproject.toml.

Run them from the repo root with `uv --directory apps/memory run ...`, or from `apps/memory/` with `uv run ...`.

- Python: `uv --directory apps/memory run python ...` to run any Python script or module
- Prefect: `uv --directory apps/memory run prefect ...` to run any Prefect CLI command
- Modal: `uv --directory apps/memory run modal ...` to run any Modal CLI command
- Opik: `uv --directory apps/memory run opik ...` to run any Opik CLI command
