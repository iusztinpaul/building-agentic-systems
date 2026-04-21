ifeq (,$(wildcard .env))
$(error .env file is missing. Please create one based on .env.example. Run: "cp .env.example .env" and fill in the missing values.)
endif

include .env
export

.PHONY: help tests pre-commit local-start local-stop local-restart

# --- Utilities ---

help: # Display this help message with a list of available commands.
	@grep -E '^[a-zA-Z0-9 _%-]+:.*#'  Makefile | sort | while read -r l; do printf "\033[1;32m%s\033[00m:%s\n" "$$(echo $$l | cut -f 1 -d':')" "$$(echo $$l | cut -f 2- -d'#')"; done

# --- Delegation to per-app Makefiles ---

memory-%: # Run <target> inside apps/memory. Example: make memory-tests, make memory-serve-mcp.
	$(MAKE) -C apps/memory $*

harness-%: # Run <target> inside apps/harness. (Harness is a planned TS app; see docs/harness-plan.md.)
	$(MAKE) -C apps/harness $*

# --- Shared infrastructure (MongoDB + mongot) ---

local-start: # Start shared infra (MongoDB + mongot) via Docker Compose.
	docker compose up -d

local-stop: # Stop shared infra.
	docker compose down

local-restart: # Restart shared infra.
	docker compose down && docker compose up -d

# --- Convenience aggregates ---

tests: # Run tests across all apps.
	$(MAKE) memory-tests

pre-commit: # Run pre-commit hooks across the repo.
	uv run --project apps/memory pre-commit run --all-files
