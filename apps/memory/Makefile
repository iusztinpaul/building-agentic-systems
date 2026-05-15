ifeq (,$(wildcard ../../.env))
$(error Root .env file is missing. From the repo root run: "cp .env.example .env" and fill in the missing values.)
endif

include ../../.env
export

export UV_PROJECT_ENVIRONMENT=.venv
export VIRTUAL_ENV=.venv
export PYTHONPATH = ./src/

.PHONY: tests

# --- Default Values ---

QA_FOLDERS := src/ tests/ scripts/ deploy/
DIR_PATH ?= inputs/tests/00_debug
HUMAN_FEEDBACK ?=
TRANSPORT_ARG := $(if $(TRANSPORT),--transport $(TRANSPORT),)

# --- Utilities ---

help: # Display memory-app targets.
	@grep -E '^[a-zA-Z0-9 _-]+:.*#'  Makefile | sort | while read -r l; do printf "\033[1;32m%s\033[00m:%s\n" "$$(echo $$l | cut -f 1 -d':')" "$$(echo $$l | cut -f 2- -d'#')"; done

build: # Build the project.
	uv sync

generate-secret-key: # Generate a random secret key (e.g. for MODAL_EMBEDDING_API_KEY).
	@uv run python -c "import secrets; print(secrets.token_urlsafe(32))"


# --- Tests & QA ---

tests: # Run all tests (unit + integration).
	uv run pytest

unit-tests: # Run unit tests only.
	uv run pytest tests/unit

integration-tests: # Run integration tests only (can take up to 15 minutes).
	uv run pytest tests/integration

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
	uv run python -m tree.orchestrator


# --- Data Pipelines ---

run-data-pipeline: # Trigger the unified data pipeline ETL via Prefect. Walks all entries in configs/default.yaml sources.
	uv run python scripts/run_data_pipeline.py


# --- Memory Pipelines ---
# Pattern: run-<memory-pipeline-name>-memory-pipeline

run-memory-pipeline-extraction: # Trigger memory extraction pipeline via Prefect. Optionally pass DOC_IDS="id1 id2".
	uv run python scripts/run_memory_pipeline.py $(DOC_IDS)

run-memory-pipeline-indexing: # Trigger memory indexing pipeline via Prefect (reverse edges, embeddings, indexes).
	uv run python scripts/run_indexing_pipeline.py

# --- Querying ---

query-graph: # Query and visualize the knowledge graph. Pass QUERY="your query" for search, omit for full graph.
	uv run python scripts/query_graph.py $(if $(QUERY),--query "$(QUERY)",)

search-web: # On-demand web search via Bright Data SERP. Pass QUERY="your query" [ENGINE=google] [NUM_RESULTS=10] [COUNTRY=us] [LANGUAGE=en] [INGEST=true] [INGEST_TOP_K=N] [INGEST_URLS="a,b,c"].
	@if [ -z "$(QUERY)" ]; then echo 'USAGE: make search-web QUERY="your query"'; exit 1; fi
	uv run python scripts/search_web.py --query "$(QUERY)" \
		$(if $(ENGINE),--engine $(ENGINE),) \
		$(if $(NUM_RESULTS),--num-results $(NUM_RESULTS),) \
		$(if $(COUNTRY),--country $(COUNTRY),) \
		$(if $(LANGUAGE),--language $(LANGUAGE),) \
		$(if $(filter true,$(INGEST)),--ingest,) \
		$(if $(INGEST_TOP_K),--ingest-top-k $(INGEST_TOP_K),) \
		$(if $(INGEST_URLS),--ingest-urls "$(INGEST_URLS)",)

scrape-web: # On-demand scrape of one or more URLs via Bright Data Web Unlocker (no ingestion). Pass URLS="a,b,c" [DATA_FORMAT=markdown|html] [MAX_CHARS=30000] [TIMEOUT=60].
	@if [ -z "$(URLS)" ]; then echo 'USAGE: make scrape-web URLS="https://a,https://b"'; exit 1; fi
	uv run python scripts/scrape_web.py --urls "$(URLS)" \
		$(if $(DATA_FORMAT),--data-format $(DATA_FORMAT),) \
		$(if $(MAX_CHARS),--max-chars $(MAX_CHARS),) \
		$(if $(TIMEOUT),--timeout $(TIMEOUT),)

serve-mcp: # Serve the MCP server for knowledge graph queries.
	uv run python scripts/serve_mcp.py $(TRANSPORT_ARG)


# --- Smoke ---

smoke-resolution-dedup: # End-to-end resolution+dedup smoke. Pass STRATEGY=keep_primary|merge_properties|keep_aliases. Optional RESTART_INFRA=1 to run 'make local-restart' first.
	@if [ -z "$(STRATEGY)" ]; then echo 'USAGE: make smoke-resolution-dedup STRATEGY=keep_primary|merge_properties|keep_aliases [RESTART_INFRA=1]'; exit 1; fi
	TREE_EXTRACTION__DEDUP__MERGE_STRATEGY=$(STRATEGY) uv run python scripts/smoke_resolution_dedup.py run \
		--strategy $(STRATEGY) \
		$(if $(filter 1,$(RESTART_INFRA)),--restart-infra,)
