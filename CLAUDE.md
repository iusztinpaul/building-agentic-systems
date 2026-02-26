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
│   └── twin/               # Core Python module
│       ├── config/         # Configuration
│       ├── entities/       # Key data structures as ORMs
│       ├── db.py           # Database connection helpers
│       ├── orchestrator.py # Orchestrator integration
│       ├── data/           # Data ETLs
│       │   ├── core/       # Core module business logic
│       │   ├── types.py    # Types used across the data layer 
│       │   └── ...         # One .py file per ETL served via Prefect
│       └── memory/         # Unified memory module
│           ├── types.py    # Types used across the memory layer
│           ├── extraction/ # Chunking, graph extraction, embedding
│           ├── materialization/ # Query views built from memory logs
│           └── query/      # Query interfaces over unified memory
├── models/                # Model configuration and utilities
│   ├── config.py          # Global model configuration
│   ├── exceptions.py      # Model-related exception types
│   ├── get_model.py       # Model factory and loading
│   ├── base.py            # Base interfaces
│   ├── gemini.py          # Gemini models
│   └── fake_model.py      # Fake models for testing
├── configs/                # App YAML configs
├── scripts/                # Entrypoints
├── tests/                  # Tests
│   ├── unit/
│   └── integration/
└── .env.example            # All supported env vars
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

### Tests

- Structure the tests following a one-on-one relationship with the core python module.
- When writing tests respect:
    - **Naming**: Files must be `test_*.py`; functions must be `test_*`.
    - **Pattern**: Use AAA (Arrange, Act, Assert).
    - **Fixtures**: Use `conftest.py` for shared logic; avoid manual setup/teardown methods.
    - **Mocking**: Use `pytest-mock` (the `mocker` fixture) to isolate unit tests from your MongoDB.
    - **Parametrize**: Use `@pytest.mark.parametrize` to test multiple inputs (e.g., different sensor values) in a single function.

## Tech Stack

- **Data validation and structuring:** Pydantic
- **ODM:** Beanie + PyMongo Async driver
- **MCP Server Framework:** FastMCP
- **Testing:** Pytest
- **CLI:** Click
- **Logging:** Native Python logger (never prints!)

- **LLM API:** Gemini
- **Embedding Models API:** Voyage AI
- **Model Definition:** HuggingFace Transformers
- **Crawling and scraping:** Firecrawl

- **Unified memory and database:** MongoDB
- **Orchestrator and durable workflows:** Prefect (
- **Observability and evals:** Opik
- **Containerization:** Docker
- **CI/CD:** GitHub Actions

### Orchestrator and Durable Workflows

- Tool: Prefect
- Sitemap: https://docs.prefect.io/llms.txt
- You can access deployments via `uv run prefect deployment ...` CLI commands. For example to run a deployment served in @src/twin/orchestrator/py you can run `prefect deployment run [DEPLOYMENT_NAME]`

# The How 

We manage all the core commands through GNU Make as our command center. 

We use `uv` to manage our Python virtual environment, dependencies, and run the project. Also, we use `ruff` as our formatter and linter. 

## Developing New Features

When developing new features you always have to:
- Plan and ask for user validation
- Implement the feature
    - Update @.env.example + @src/twin/config/settings.py with any new required env vars
- Write unit and integration tests
- Run the steps from `Test` and fix any errors
- Scan for any potential bugs that weren't detected within the `Test` step and highlight them for validation
- Suggest any potential updates for the main `CLAUDE.md` file, local `CLAUDE.md` or `.claude/rules`  based on the latest changes from the feature and highlights done by the user

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

## Run

If not present directly in the @Makefile, run any custom Python file using: `uv run python ...`

Use `mongosh` to interact with MongoDB directly through the CLI.
