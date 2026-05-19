ifeq (,$(wildcard ../../.env))
$(error Root .env file is missing. From the repo root run: "cp .env.example .env" and fill in the missing values.)
endif

include ../../.env
export

export UV_PROJECT_ENVIRONMENT=.venv
export VIRTUAL_ENV=.venv
export PYTHONPATH = ./src/

# --- macOS short-TMPDIR shim (see CLAUDE.md "Configuration > macOS torch / TMPDIR") ---
#
# On macOS arm64 + Python 3.14, torch's bundled ``torch_shm_manager`` constructs
# a Unix-domain socket path of the form ``<TMPDIR>/torch_<pid>_<rand>/manager.sock``
# and stuffs it into a ``sockaddr_un`` whose ``sun_path`` field is only 104 bytes
# on Darwin (vs. 108 on Linux). If the inherited ``TMPDIR`` is long (e.g. the
# ``/var/folders/.../com.apple.shortcuts.mac-helper/`` path some agent shells
# inherit, ~81 chars), the full socket path overflows, ``__stack_chk_fail`` traps,
# and the helper aborts with SIGABRT — surfacing as
# ``RuntimeError: no response from torch_shm_manager``. See
# ``tracker/done/035-pin-torch-version-py314-arm64.md`` for the full diagnostic
# log. The canonical per-user short tmpdir on macOS is ``getconf
# DARWIN_USER_TEMP_DIR`` (~49 chars), which keeps the path safely under 104.
# No-op on Linux (``sun_path`` is 108 bytes there and ``/tmp`` is 5 chars).
UNAME_S := $(shell uname -s)
ifeq ($(UNAME_S),Darwin)
export TMPDIR := $(shell getconf DARWIN_USER_TEMP_DIR)
endif

.PHONY: tests

# --- Default Values ---

QA_FOLDERS := src/ tests/ scripts/ deploy/
DIR_PATH ?= inputs/tests/00_debug
HUMAN_FEEDBACK ?=
TRANSPORT_ARG := $(if $(TRANSPORT),--transport $(TRANSPORT),)

# --- Utilities ---

help: # Display memory-app targets.
	@grep -E '^[a-zA-Z0-9 _-]+:.*#'  Makefile | sort | while read -r l; do printf "\033[1;32m%s\033[00m:%s\n" "$$(echo $$l | cut -f 1 -d':')" "$$(echo $$l | cut -f 2- -d'#')"; done

print-tmpdir: # Print the effective TMPDIR for memory-app make targets. Diagnoses the macOS sockaddr_un sun_path overflow that crashes torch_shm_manager.
	@echo "TMPDIR=$$TMPDIR"
	@echo "TMPDIR length: $$(printf '%s' "$$TMPDIR" | wc -c | tr -d ' ') bytes"
	@echo "uname -s: $$(uname -s)"

smoke-torch-shared-memory: # Smoke-test that torch.tensor(0).share_memory_() runs cleanly under the make-provided TMPDIR. Catches the macOS sockaddr_un sun_path overflow.
	@uv run python -c "import os, torch; print('TMPDIR=' + os.environ.get('TMPDIR', '<unset>')); torch.tensor(0).share_memory_(); print('OK')"

build: # Build the project.
	uv sync

generate-secret-key: # Generate a random secret key (e.g. for MODAL_EMBEDDING_API_KEY).
	@uv run python -c "import secrets; print(secrets.token_urlsafe(32))"


# --- Tests & QA ---

tests: # Run all tests (unit + integration).
	uv run pytest

unit-tests: # Run unit tests only.
	uv run pytest tests/unit

integration-tests: # Fast inner-loop integration tests (excludes @pytest.mark.slow). Target wall time <2 min.
	uv run pytest tests/integration -m "not slow"

integration-tests-slow: # Run only the slow integration tests (vector-index convergence + full Prefect e2e).
	uv run pytest tests/integration -m "slow"

integration-tests-all: # Run the full integration suite (fast + slow, incl. mongot). Used by the Tester's final acceptance gate; requires the full local mongot stack from docker-compose.yml.
	uv run pytest tests/integration

integration-tests-ci: # Mirror the CI integration command locally: skip mongot-dependent tests. Useful for reproducing CI behavior. Runs sequentially because the shared-DB cleanup fixture makes parallel runs collide.
	uv run pytest tests/integration -m "not requires_mongot" --timeout=300

pre-commit: # Run pre-commit hooks.
	uv run pre-commit run --all-files

check-kgquery-discipline: # Enforce that raw KnowledgeGraphEntry.find(...) reads only live inside the allow-listed modules. Run by pre-commit; fails on a planted violation.
	@uv run python scripts/check_kgquery_discipline.py

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
#
# Every pipeline below requires a tenant USER_ID (a 24-char Mongo ObjectId).
# Pass it on the make command line, e.g.::
#
#     make run-data-pipeline USER_ID=507f1f77bcf86cd799439011
#
# Wired in #020; there is no silent fallback to a default user.

run-data-pipeline: # Trigger the unified data pipeline ETL via Prefect. Walks all entries in configs/default.yaml sources. Requires USER_ID=<oid>.
	@if [ -z "$(USER_ID)" ]; then echo 'USAGE: make run-data-pipeline USER_ID=<24-char Mongo ObjectId>'; exit 1; fi
	uv run python scripts/run_data_pipeline.py --user-id "$(USER_ID)"


# --- Memory Pipelines ---
# Pattern: run-<memory-pipeline-name>-memory-pipeline

run-memory-pipeline-extraction: # Trigger memory extraction pipeline via Prefect. Requires USER_ID=<oid>. Optionally pass DOC_IDS="id1,id2".
	@if [ -z "$(USER_ID)" ]; then echo 'USAGE: make run-memory-pipeline-extraction USER_ID=<24-char Mongo ObjectId>'; exit 1; fi
	uv run python scripts/run_memory_pipeline.py --user-id "$(USER_ID)" $(if $(DOC_IDS),--doc-ids "$(DOC_IDS)",)

run-memory-pipeline-indexing: # Trigger memory indexing pipeline via Prefect (reverse edges, embeddings, indexes). Requires USER_ID=<oid>.
	@if [ -z "$(USER_ID)" ]; then echo 'USAGE: make run-memory-pipeline-indexing USER_ID=<24-char Mongo ObjectId>'; exit 1; fi
	uv run python scripts/run_indexing_pipeline.py --user-id "$(USER_ID)"

# --- Migration (Phase 1 one-shot + Phase 2-5 reset-ontology) ---

migrate-multi-tenancy: # Multi-tenancy migration. Default = Phase-1 bootstrap. RESET_ONTOLOGY=1 = Phase 2-5 wipe-and-rebuild. Requires USER_IDENTIFIER=<email|sub>. Optional NAME="Display Name", DRY_RUN=1, NO_TRIGGER_PIPELINES=1, RESET_ONTOLOGY=1.
	@if [ -z "$(USER_IDENTIFIER)" ]; then echo 'USAGE: make migrate-multi-tenancy USER_IDENTIFIER=<email> [NAME="Display Name"] [DRY_RUN=1] [NO_TRIGGER_PIPELINES=1] [RESET_ONTOLOGY=1]'; exit 1; fi
	uv run python scripts/migrate_multi_tenancy.py --identifier "$(USER_IDENTIFIER)" \
		$(if $(NAME),--name "$(NAME)",) \
		$(if $(filter 1,$(DRY_RUN)),--dry-run,) \
		$(if $(filter 1,$(NO_TRIGGER_PIPELINES)),--no-trigger-pipelines,) \
		$(if $(filter 1,$(RESET_ONTOLOGY)),--reset-ontology,)

# --- Querying ---

query-graph: # Query and visualize the knowledge graph for one user. Requires USER_ID=<oid>. Pass QUERY="your query" for search, omit for full graph.
	@if [ -z "$(USER_ID)" ]; then echo 'USAGE: make query-graph USER_ID=<24-char Mongo ObjectId> [QUERY="..."]'; exit 1; fi
	uv run python scripts/query_graph.py --user-id "$(USER_ID)" $(if $(QUERY),--query "$(QUERY)",)

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

serve-mcp: # Serve the MCP server. Requires USER_ID=<oid> OR TREE_USER_IDENTIFIER env. Optional TRANSPORT=stdio|http|sse|streamable-http.
	@if [ -z "$(USER_ID)" ] && [ -z "$(TREE_USER_IDENTIFIER)" ]; then echo 'USAGE: make serve-mcp USER_ID=<oid>  (or set TREE_USER_IDENTIFIER=<handle>)'; exit 1; fi
	uv run python scripts/serve_mcp.py $(if $(USER_ID),--user-id "$(USER_ID)",) $(TRANSPORT_ARG)


# --- Smoke ---

smoke-resolution-dedup: # End-to-end resolution+dedup smoke. Pass STRATEGY=keep_primary|merge_properties|keep_aliases. Optional RESTART_INFRA=1 to run 'make local-restart' first.
	@if [ -z "$(STRATEGY)" ]; then echo 'USAGE: make smoke-resolution-dedup STRATEGY=keep_primary|merge_properties|keep_aliases [RESTART_INFRA=1]'; exit 1; fi
	TREE_EXTRACTION__DEDUP__MERGE_STRATEGY=$(STRATEGY) uv run python scripts/smoke_resolution_dedup.py run \
		--strategy $(STRATEGY) \
		$(if $(filter 1,$(RESTART_INFRA)),--restart-infra,)
