# The WHY

Build your digital twin through knowledge graphs, ontologies, memory, LLMs and agents.

# The What 

## Key Components

- **Data Pipeline:** ETL pipelines gathering data from multiple sourcing and normalizing everything into the `documents` collection.
- **Memory Pipeline:** Pipeline that maps `documents` to `knowledge graph objects` within the `knowledge_graph_log` collection by cleaning, chunking, graph extracting, normalizing and embedding the content of the chunks.
- **The Unified Memory:** The agent's unified memory powered by MongoDB that leverages text, semantic and graph search. The data is stored as immutable logs within the `knowledge_graph_log` collection, while building a query view for retrieval by aggregating logs with the same ID.
- **Agentic Tools:** Tools used to query or write to the unified memory. 

## Project Structure

```
project-root/
├── src/
│   └── twin/      # Core Python module
├── scripts/       # Entrypoints
└── tests/         # Tests
```

## Key Design Choices

- We are using Python with async patterns.
- Loose clean architecture design decoupling infrastructure, serving, app and domain logic. 
    - Infrastructure exceptions we don't plan to change: MongoDB, Prefect, Opik
- Structure the tests following a one-on-one relationship with the core python module.

## Tech Stack

- **Data validation and structuring:** Pydantic
- **ODM:** Beanie + PyMongo Async driver
- **MCP Server Framework:** FastMCP
- **Testing:** Pytest

- **LLM API:** Gemini
- **Embedding Models API:** Voyage AI
- **Model Definition:** HuggingFace Transformers

- **Unified memory and database:** MongoDB
- **Orchestrator and durable workflows:** Prefect
- **Observability and evals:** Opik
- **Containerization:** Docker
- **CI/CD:** GitHub Actions

# The How 

We manage all the core commands through GNU Make as our command center. 

We use `uv` to manage our Python virtual environment, dependencies, and run the project. Also, we use `ruff` as our formatter and linter. 

## Developing New Features

When developing new features you always have to:
- Plan and ask for user validation
- Implement the feature
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
