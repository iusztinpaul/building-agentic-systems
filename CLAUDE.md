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
- **Memory Pipeline:** Pipeline that maps `documents` to `knowledge graph objects` within the `knowledge_graph_log` collection by cleaning, chunking, graph extracting, normalizing and embedding the content of the chunks.
- **The Unified Memory:** The agent's unified memory powered by MongoDB that leverages text, semantic and graph search. The data is stored as immutable logs within the `knowledge_graph_log` collection, while building a query view for retrieval by aggregating logs with the same ID.
- **Agentic Tools:** Tools used to query or write to the unified memory. 

## Project Structure

```
project-root/
├── src/
│   └── twin/                        # Core Python module
│       ├── config/                  # Configuration
│       ├── entities/                # Key data structures as ORMs
│       ├── db.py                    # Database connection helpers
│       ├── orchestrator.py          # Orchestrator integration
│       ├── data/                    # Data ETLs
│       │   ├── core/                # Core module business logic
│       │   ├── types.py             # Types used across the data layer
│       │   └── ...                  # One .py file per ETL served via Prefect
│       └── memory/                  # Unified memory module
│           ├── types.py             # Types used across the memory layer
│           ├── extraction/          # Chunking, graph extraction, embedding
│           ├── materialization/     # Query views built from memory logs
│           └── query/               # Query interfaces over unified memory
├── models/                          # Model configuration and utilities
│   ├── base.py                      # Base interfaces (BaseLLM, BaseEmbeddingModel)
│   ├── exceptions.py                # Model-related exception types
│   ├── get_model.py                 # Model factory and loading
│   ├── gemini.py                    # Gemini LLM and embedding models
│   ├── modal_embedding.py           # Modal provider: dynamic URL resolution + health check
│   ├── voyage_multimodal_embedding.py # Voyage AI multimodal embeddings API
│   ├── sentence_transformer.py      # In-process sentence-transformers embedding
│   └── fake_model.py                # Fake/mock models for testing
├── deploy/                          # Cloud deployment scripts
│   └── modal_vllm_embedding.py      # Modal vLLM embedding server (voyage-4-nano)
├── configs/                         # App YAML configs
├── scripts/                         # Entrypoints
├── tests/                           # Tests
│   ├── unit/
│   └── integration/
└── .env.example                     # All supported env vars
```

## Key Design Choices

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

## Writing Python Code

- Always add types to function or method parameters and return types. Even if they return `None`.

### Tests

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
in @src/twin/orchestrator/py you can run `prefect deployment run [DEPLOYMENT_NAME]`

### Access Documentation 

Use the `context7` MCP server to find out more about the tech stack usage and good practices.

# The How 

We manage all the core commands through GNU Make as our command center. 

We use `uv` to manage our Python virtual environment, dependencies, and run the project. Also, we use `ruff` as our formatter and linter. 

## Developing New Features and Bug Fixes Workflow

At the beginning of a conversation ALWAYS ask the user if they are developing a new feature/bug or continue working 
on an existing one. 

When developing new features follow this exact plan:
- Create a new branch that branches off from the current active branch. If the active branch is `main`, 
it branches off from `main`. If it's a feature branch `feat/...`, it branches off from that.
- Plan and ask for user validation
- Implement the feature. Special considerations to always look out for:
  - Add new dependencies to @pyproject.toml
  - Update @.env.example + @src/twin/config/settings.py with any new required env vars
  - After any atomic change, commit and push the changes to git using the `commit-commands` plugin
- Write unit and integration tests:
  - Write unit and integrations tests for the core functionality.
  - Run the tests. In case of errors fix the code until all the tests successfully run.
  - Run the actual code testing and debugging how the code works on dev machine.
  - In case of errors, write regression tests for the given errors, fix them, and repeat.
- Update memory: 
  - If the user corrected you in any way, suggest any potential updates for the main `CLAUDE.md` file, local `CLAUDE.md` or `.claude/rules`
- PR workflow:
  - Use the `create-pr` skill to open/update the PR
  - Use the `code-review` plugin to review and optimize the code.
  - Check if the CI/CD pipeline passed using the `gh` CLI to look at the GitHub Actions logs. If not, fix the errors and re-run the pipelines until they pass.
  - After fixing the PR, use the `create-pr` skill to update the description
  - Repeat until the `code-review` and CI/CD passes.
  - DON'T merge the PR. The user will.

## Working with Git

- Always use `git commit -m <message>` to commit changes, where the messages follow the [Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/) format.


## Build

```
make build
```

## Test

First always fix the formatting and linting errors with the fix commands:
```
make format-fix
make lint-fix
```

Then, check if there are any errors that couldn't be fixed automatically and fix them:
```
make format-check
make lint-check
make pre-commit
```

Ultimately, run the tests and fix the core module for any potential issues:
```
make tests
```

## Running Pipelines

To test a pipeline after making changes:

1. **Serve the workflows** in a background process to pick up the latest code:
```
make serve-workflows &
```
If a serve process is already running, kill it first and re-serve to pick up the latest code changes.

2. **Run the pipeline** via the corresponding Make command (which streams logs to the terminal), such as:
```
make run-data-pipeline
make run-memory-pipeline-extraction
make run-memory-pipeline-materialization
```

The `make serve-workflows` process must be running for pipeline triggers to be picked up, as it acts as the in-process Prefect worker. Without it, deployments are registered but no worker will execute them.

Always use these Make commands instead of `prefect deployment run` directly, as the scripts stream all logs (including errors) back to the current process so you can debug without checking the Prefect UI.

## Running Custom Commands for Project Level Dependencies

Use `uv` to run any custom command that is not present in the @Makefile, but uses Python or other dependency installed through uv, usually available in @pyproject.toml.

Run them by prefixing the command with `uv run ...`, such as:
- `uv run python ...` 
- `uv run prefect ...`
- `uv run modal ...`

## Running Custom Commands for Accessing Infrastructure and External Services 

Always use the following CLIs installed directly on the system:

- MongoDB: `mongosh` CLI for CRUD operations and monitoring on the local MongoDB instance.
- GitHub: `gh` CLI to interact with the remote GitHub repository this project is attached to (e.g., accessing PRs, issues or GitHub Actions)
- Git: `git` CLI for generic Git operations.
