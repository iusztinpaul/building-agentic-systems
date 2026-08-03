ENV_TARGET := $(strip $(shell cat .env.target 2>/dev/null))
ifeq ($(ENV_TARGET),prod)
ENV_FILE := .env.prod
# Prod talks to Prefect Cloud — no `local`-profile services (prefect-server).
COMPOSE_PROFILES :=
else
ENV_FILE := .env
# Local brings up the full stack including the in-compose prefect-server.
COMPOSE_PROFILES := local
endif
export COMPOSE_PROFILES

ifeq (,$(wildcard $(ENV_FILE)))
$(error $(ENV_FILE) file is missing. Please create one based on .env.example. Run: "cp .env.example $(ENV_FILE)" and fill in the missing values.)
endif

include $(ENV_FILE)
export

.PHONY: help env-prod env-local env-status env-reload-infra generate-secret-key tests format-check lint-check typecheck pre-commit local-start local-stop local-restart

# --- Utilities ---

help: # Display this help message with a list of available commands.
	@grep -E '^[a-zA-Z0-9 _%-]+:.*#'  Makefile | sort | while read -r l; do printf "\033[1;32m%s\033[00m:%s\n" "$$(echo $$l | cut -f 1 -d':')" "$$(echo $$l | cut -f 2- -d'#')"; done

env-local: # Switch make + direnv + running infra back to local (.env) by removing .env.target.
	@rm -f .env.target
	@echo local > .env.target
	@echo "Env target: local (.env)"

env-prod: # Switch make + direnv + running infra to prod (.env.prod) via .env.target.
	@rm -f .env.target
	@echo prod > .env.target
	@echo "Env target: prod (.env.prod)"

env-status: # Show which env target is active.
	@echo "Env target: $(if $(filter prod,$(ENV_TARGET)),prod (.env.prod),local (.env))"

generate-secret-key: # Generate a random URL-safe secret (e.g. MODAL_EMBEDDING_API_KEY).
	@python3 -c "import secrets; print(secrets.token_urlsafe(32))"

generate-password: # Generate a random URL-safe secret (e.g. MONGO_INITDB_ROOT_PASSWORD).
	@python3 -c "import secrets; print(secrets.token_urlsafe(16))"

# --- Delegation to per-app Makefiles ---

memory-%: # Run <target> inside apps/memory. Example: make memory-tests, make memory-serve-mcp.
	$(MAKE) -C apps/memory $*

harness-%: # Run <target> inside apps/harness. Example: make harness-tests, make harness-run PROMPT="hi".
	$(MAKE) -C apps/harness $*

# --- Shared infrastructure (MongoDB + mongot) ---

local-start: # Start shared infra (MongoDB + mongot) via Docker Compose.
	docker compose --env-file $(ENV_FILE) up -d

local-stop: # Stop shared infra.
	docker compose --env-file $(ENV_FILE) down

local-restart: # Restart shared infra.
	docker compose --env-file $(ENV_FILE) down && docker compose --env-file $(ENV_FILE) up -d

# --- Convenience aggregates ---

tests: # Run all tests across all apps (unit only — integration suites were removed deliberately: too slow for feedback loops).
	$(MAKE) memory-tests
	$(MAKE) harness-tests

format-check: # Run formatter checks across all apps.
	$(MAKE) memory-format-check
	$(MAKE) harness-format-check

lint-check: # Run linter checks across all apps.
	$(MAKE) memory-lint-check
	$(MAKE) harness-lint-check

typecheck: # Run static type-checks across all apps (harness/TS only — memory is dynamically typed).
	$(MAKE) harness-typecheck

pre-commit: # Run pre-commit hooks across the repo (covers memory + harness via local hooks).
	uv run --project apps/memory pre-commit run --all-files
