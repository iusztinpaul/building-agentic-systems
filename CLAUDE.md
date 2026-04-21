# The Why

Build your digital twin through knowledge graphs, ontologies, memory, LLMs and agents.

# The What 

## Key Components

- **Data Pipeline:** ETL pipelines gathering data from multiple sourcing and normalizing everything into the `documents` collection. One ETL pipeline per source. Sources:
    - Substack RSS feeds (e.g., https://www.decodingai.com/feed)
    - Substack articles
    - YouTube RSS feeds
    - YouTube videos
    - Custom sites
    - Markdown files
    - HuggingFace Datasets (e.g., https://huggingface.co/datasets/arxiv-community/arxiv_dataset)
- **Memory Pipeline:** Pipeline that maps `documents` to `knowledge graph objects` within the `knowledge_graph` collection by cleaning, chunking, graph extracting, normalizing and upserting nodes and edges directly.
- **The Unified Memory:** The agent's unified memory powered by MongoDB that leverages text, semantic and graph search. The data is stored in a single mutable `knowledge_graph` collection with upsert semantics. Nodes use `_id = "type:name"` and edges use `_id = "source|type|target"` string identifiers.
- **Agentic Tools:** Tools used to query or write to the unified memory. 

## Project Structure

This is a monorepo. Each app lives under `apps/` and owns its own build files (`pyproject.toml`, `Makefile`, etc.). Only cross-app concerns live at the repo root.

```
project-root/
├── apps/
│   ├── memory/                          # Python app: ETL + knowledge graph + MCP server
│   │   ├── src/
│   │   │   └── twin/                    # Core Python module
│   │   │       ├── config/              # Configuration
│   │   │       ├── entities/            # Key data structures as ODMs
│   │   │       ├── db.py                # Database connection helpers
│   │   │       ├── orchestrator.py      # Orchestrator integration
│   │   │       ├── data/                # Data ETLs
│   │   │       │   ├── core/            # Core module business logic
│   │   │       │   ├── types.py         # Types used across the data layer
│   │   │       │   └── ...              # One .py file per ETL served via Prefect
│   │   │       └── memory/              # Unified memory module
│   │   │           ├── types.py
│   │   │           ├── extraction/      # Chunking, graph extraction, embedding
│   │   │           ├── indexing/        # Post-extraction indexing (reverse edges, embeddings, search indexes)
│   │   │           └── query/           # Query interfaces over unified memory
│   │   ├── deploy/                      # Cloud deployment scripts (Modal)
│   │   ├── configs/                     # App YAML configs (default.yaml)
│   │   ├── scripts/                     # Entrypoints (serve_mcp.py, run_*.py, ...)
│   │   ├── tests/                       # unit/ + integration/
│   │   ├── docker/Dockerfile            # Memory-app image (Prefect worker)
│   │   ├── pyproject.toml
│   │   ├── uv.lock
│   │   ├── .python-version
│   │   └── Makefile                     # Memory-app targets
│   └── harness/                         # Future TS/Ink/Bun coding-agent harness — see docs/harness-plan.md
├── docker/                              # SHARED infra (MongoDB + mongot config files)
├── docs/                                # Architecture & design docs (incl. harness-plan.md)
├── models/                              # Shared model interfaces (LLM/embedding abstractions)
├── .env / .env.example                  # Shared secrets (Mongo creds, API keys)
├── .mcp.json                            # MCP servers the agents/harness spawn
├── docker-compose.yml                   # Shared infra orchestration
└── Makefile                             # Thin root: delegates to apps/*/Makefile; shared infra targets
```

### Make command convention

- `make memory-<target>` — run `<target>` inside `apps/memory/` (e.g. `make memory-unit-tests`, `make memory-serve-mcp`).
- `make harness-<target>` — reserved for the future TS harness at `apps/harness/`.
- `make local-start` / `make local-stop` / `make local-restart` — shared Docker infra.
- `make tests` — aggregate: runs all apps' tests.
- `make pre-commit` — pre-commit across the repo.
- `make help` — list all root targets.

## Key Python Design Choices

- We are using Python with async patterns.
- Loose clean architecture design decoupling infrastructure, serving, app and domain logic:
    - The `entities` folder defines shared ODM, enums or other data structures data are used all over the project. While we have local `types.py` files per app module to define data types that will be used only within that current module or layers upwards. 
    - Infrastructure exceptions we don't plan to change: MongoDB, Prefect, Opik. Thus, it doesn't make sense to make them modular. 
    - Flat structure and naming based on actionability rather than dogmatic clean architecture.
- Properties of pipelines:
    - Idempotency
    - Retries
    - Checkpointing
- All the dates are timezone aware (UTC by default). We don't accept any naive datetime objects.
- Always add types to function or method parameters and return types. Even if they return `None`.

### Writing Scripts

- Memory-app scripts (entry points in `apps/memory/scripts/`) must call `init_logger()` from `twin.logging` at module level to configure logging.

### Writing Tests

- Structure the tests following a one-on-one relationship with the core python module.
- When writing tests respect:
    - **Naming**: Files must be `test_*.py`; functions must be `test_*`.
    - **Pattern**: Use AAA (Arrange, Act, Assert).
    - **Fixtures**: Use `conftest.py` for shared logic; avoid manual setup/teardown methods.
    - **Mocking**: Use `pytest-mock` (the `mocker` fixture) to isolate unit tests from your MongoDB.
    - **Parametrize**: Use `@pytest.mark.parametrize` to test multiple inputs (e.g., different sensor values) in a single function.
- Call the `testing-python` SKILL for step-by-step details
- Fix any `warnings`. Rerun the tests until we have 0 warnings.
- What to **AVOID**:
  - Writing unit tests for Prefect, Modal, Opik or other infra components. They represent our infrastructure layer, 
which is tested only via integration tests.

## Tech Stack

### Core
- **Data validation and structuring:** Pydantic
- **ODM:** Beanie + PyMongo Async driver
- **MCP Server Framework:** FastMCP
- **Testing:** Pytest
- **CLI:** Click
- **Logging:** Native Python logger (never prints!)

- **Embedding Model Definition:** Sentence Transformers

### Services
- **LLM API:** Gemini
- **Embedding Models API:** Voyage AI
- **Crawling and scraping:** Firecrawl

### Infrastructure
- **Unified memory and database:** MongoDB
- **Serving AI Models**: Modal
- **Observability and evals:** Opik
- **Containerization:** Docker
- **CI/CD:** GitHub Actions

### Orchestrator and Durable Workflows

- Tool: Prefect
- Sitemap: https://docs.prefect.io/llms.txt
- You can access deployments via `uv run prefect deployment ...` CLI commands. For example, to run a deployment served 
in @apps/memory/src/twin/orchestrator.py you can run `prefect deployment run [DEPLOYMENT_NAME]` (invoked from within `apps/memory/`).

### Access Documentation 

Use the `context7` MCP server to find out more about the tech stack usage and good practices.

# The How 

We manage all the core commands through GNU Make as our command center. File available at @Makefile. Run all the commands with `make ...`

We use `uv` to manage our Python project such as the virtual environment(s), dependencies, and overall package the project.

## Developing New Features and Bug Fixes Workflow

At the beginning of a conversation ALWAYS ask the user if they are developing a new feature/bug or continue working 
on an existing one. 

When developing new features follow this exact plan:
- Create a new branch that branches off from the current active branch. If the active branch is `main`, 
it branches off from `main`. If it's a feature branch `feat/...`, it branches off from that.
- Plan and ask for user validation
- Write unit and integration tests:
  - Use red/green TDD to first write unit and integration tests for the core functionality before implementing any feature.
  - Run `make memory-unit-tests` frequently during development (after each atomic change) to catch regressions early.
  - If working only a module, to speed things up, run the tests only from that module. For example, when changing module `twin.data.substack`, run the tests only related to the Substack data pipelines.
  - Run the actual code testing and debugging how the code works on dev machine.
  - In case of errors, write regression tests for the given errors, fix them, and repeat.
  - Only run `make memory-integration-tests` when the feature is considered done and ready for PR. Integration tests can take up to 15 minutes.
- Implement the feature. Special considerations to always look out for:
  - Add new dependencies to @apps/memory/pyproject.toml
  - Update @.env.example + @apps/memory/src/twin/config/settings.py with any new required env vars
  - After any atomic change, commit the changes to git using the `commit-commands` plugin. Then push them to git. Always check if the `pre-commit` passes.
- PR workflow:
  1. Use the `create-pr` skill to open/update the PR.
  2. Check if the CI/CD pipeline passed using the `gh` CLI to look at the GitHub Actions logs. If not, fix the errors and re-run the pipelines until they pass.
  3. Use the `code-review` plugin to review the code.
  4. Fix the code, based on the reviews and repeat step 2 in case the CI/CD pipeline fails.
  5. Repeat until the `code-review` and CI/CD passes.
  6. Use the `create-pr` skill to update the description,
  7. DON'T merge the PR. The user will.

## Step-by-Step Verification Steps

During development, run these steps after every atomic change or before commiting anything to git:

 1. Format and lint: `make memory-format-fix && make memory-lint-fix && make memory-format-check && make memory-lint-check`
 2. Pre-commit: `make pre-commit`
 3. Unit tests: `make memory-unit-tests`

When the feature is considered done and ready for PR, ALWAYS run:

 4. Integration tests: `make memory-integration-tests` (can take up to 15 minutes)
 5. Run and verify the code end-to-end. For example, when testing the memory run: `make memory-serve-workflows & `→ `make memory-run-memory-pipeline-extraction` → `make memory-run-memory-pipeline-indexing` → `make memory-query-graph QUERY="test query"` → verify results. Always adapt this e2e example based on the modifications you've made. If necessary you should run multiple tests covering all the modifications you've made in the feature PR you are working on.

## Build

```
make memory-build
```

## Running QA and Tests

We use `ruff` as our formatter and linter.

First always fix the formatting and linting errors with the fix commands:
```
make memory-format-fix
make memory-lint-fix
```

Then, check if there are any errors that couldn't be fixed automatically and fix them:
```
make memory-format-check
make memory-lint-check
make pre-commit
```

Run unit tests frequently during development:
```
make memory-unit-tests
```

Run integration tests only when the feature is done (can take up to 15 minutes):
```
make memory-integration-tests
```

Or run all tests together (aggregate across apps):
```
make tests
```

## Running Pipelines

To test a pipeline after making changes:

1. **Serve the workflows** in a background process to pick up the latest code:
```
make memory-serve-workflows &
```
If a serve process is already running, kill it first and re-serve to pick up the latest code changes.

2. **Run the pipeline** via the corresponding Make command (which streams logs to the terminal), such as:
```
make memory-run-all-data-pipelines
make memory-run-substack-rss-data-pipeline
make memory-run-substack-article-data-pipeline
make memory-run-arxiv-data-pipeline
make memory-run-memory-pipeline-extraction
make memory-run-memory-pipeline-indexing
```

The `make memory-serve-workflows` process must be running for pipeline triggers to be picked up, as it acts as the in-process Prefect worker. Without it, deployments are registered but no worker will execute them.

Always use these Make commands instead of `prefect deployment run` directly, as the scripts stream all logs (including errors) back to the current process so you can debug without checking the Prefect UI.

## Running Custom Commands for Project Level Dependencies

Use `uv` to run any custom command that is not present in the @Makefile or @apps/memory/Makefile, but uses Python or other dependency installed through uv, usually available in @apps/memory/pyproject.toml.

Run them from the repo root with `uv --directory apps/memory run ...`, or from `apps/memory/` with `uv run ...`. Examples:
- `uv --directory apps/memory run python ...`
- `uv --directory apps/memory run prefect ...`
- `uv --directory apps/memory run modal ...`

## Running Custom Commands for Accessing Infrastructure and External Services 

Always use the following CLIs installed directly on the system:

- MongoDB: `mongosh` CLI for CRUD operations and monitoring on the local MongoDB instance.
- GitHub: `gh` CLI to interact with the remote GitHub repository this project is attached to (e.g., accessing PRs, issues or GitHub Actions)
- Git: `git` CLI for generic Git operations.

## Self Improve

Run the `self-improve` SKILL to analyze corrections from the session and persist lessons learned to CLAUDE.md files or memory.
