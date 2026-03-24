ifeq (,$(wildcard .env))
$(error .env file is missing. Please create one based on .env.example. Run: "cp .env.example .env" and fill in the missing values.)
endif

include .env
export

export UV_PROJECT_ENVIRONMENT=.venv
export PYTHONPATH = ./src/

.PHONY: tests

# --- Default Values ---

QA_FOLDERS := src/ tests/ scripts/ deploy/
DIR_PATH ?= inputs/tests/00_debug
HUMAN_FEEDBACK ?=
TRANSPORT_ARG := $(if $(TRANSPORT),--transport $(TRANSPORT),)

# --- Utilities ---

help: # Display this help message with a list of available commands.
	@grep -E '^[a-zA-Z0-9 -]+:.*#'  Makefile | sort | while read -r l; do printf "\033[1;32m$$(echo $$l | cut -f 1 -d':')\033[00m:$$(echo $$l | cut -f 2- -d'#')\n"; done

build: # Build the project.
	uv sync

generate-secret-key: # Generate a random secret key (e.g. for MODAL_EMBEDDING_API_KEY).
	@uv run python -c "import secrets; print(secrets.token_urlsafe(32))"


# --- Tests & QA ---

tests: # Run tests.
	uv run pytest

pre-commit: # Run pre-commit hooks.
	uv run pre-commit run --all-files

format-fix: # Auto-format Python code using ruff formatter.
	uv run ruff format $(QA_FOLDERS)

lint-fix: # Auto-fix linting issues using ruff linter.
	uv run ruff check --fix $(QA_FOLDERS)

format-check: # Check code formatting without making changes using ruff formatter.
	uv run ruff format --check $(QA_FOLDERS) 

lint-check: # Check code for linting issues without fixing them using ruff linter.
	uv run ruff check $(QA_FOLDERS)


# --- Infrastructure ---

local-start: # Start local infrastructure (MongoDB + mongot) via Docker Compose.
	docker compose up -d

local-stop: # Stop local infrastructure.
	docker compose down

local-restart: # Restart local infrastructure.
	docker compose down && docker compose up -d

local-test: # Validate MongoDB setup (text, vector, graph search).
	uv run python scripts/test_mongodb_setup.py


# --- Deployment ---

deploy-embedding-model: # Deploy vLLM embedding server (voyage-4-nano) to Modal. Creates the API key secret if missing.
	@uv run modal secret list 2>/dev/null | grep -q "vllm-embedding-api" || \
		uv run modal secret create vllm-embedding-api-key MODAL_EMBEDDING_API_KEY="$(MODAL_EMBEDDING_API_KEY)"
	uv run modal deploy deploy/modal_vllm_embedding.py

deploy-embedding-model-test: # Test the deployed vLLM embedding server on Modal.
	uv run modal run deploy/modal_vllm_embedding.py

deploy-embedding-model-stop: # Stop the deployed vLLM embedding server on Modal.
	uv run modal app stop vllm-embedding-models


# --- Orchestration ---

serve-workflows: # Serve Prefect workflow deployments.
	uv run python -m src.twin.orchestrator


# --- Data Pipelines ---

run-data-pipeline: # Trigger Substack RSS ETL via Prefect. Reads feeds from configs/default.yaml.
	uv run python scripts/run_data_pipeline.py

run-arxiv-data-pipeline: # Trigger arxiv dataset ETL via Prefect. Reads max_samples from configs/default.yaml.
	uv run python scripts/run_arxiv_data_pipeline.py

run-data-substack-article-pipeline: # Trigger Substack article ETL via Prefect. Reads article URLs from configs/default.yaml.
	uv run python scripts/run_substack_article_data_pipeline.py


# --- Memory Pipelines ---

run-memory-pipeline-extraction: # Trigger memory extraction pipeline via Prefect. Optionally pass DOC_IDS="id1 id2".
	uv run python scripts/run_memory_pipeline.py $(DOC_IDS)

run-memory-pipeline-materialization: # Trigger memory materialization pipeline via Prefect.
	uv run python scripts/run_materialization_pipeline.py

# --- Querying ---

query-graph: # Query and visualize the knowledge graph. Pass QUERY="your query" for search, omit for full graph.
	uv run python scripts/query_graph.py $(if $(QUERY),--query "$(QUERY)",)
