# Env-target switch (mirrors root Makefile): .env.target == "prod" selects ../../.env.prod, otherwise ../../.env.
ENV_TARGET := $(strip $(shell cat ../../.env.target 2>/dev/null))
ifeq ($(ENV_TARGET),prod)
ENV_FILE := ../../.env.prod
else
ENV_FILE := ../../.env
endif

ifeq (,$(wildcard $(ENV_FILE)))
$(error Root $(notdir $(ENV_FILE)) file is missing. From the repo root run: "cp .env.example $(notdir $(ENV_FILE))" and fill in GOOGLE_API_KEY.)
endif

include $(ENV_FILE)
export

.PHONY: help install dev run typecheck tests test-watch format-fix format-check lint-check lint-fix build clean

help: # Display harness-app targets.
	@grep -E '^[a-zA-Z0-9 _-]+:.*#'  Makefile | sort | while read -r l; do printf "\033[1;32m%s\033[00m:%s\n" "$$(echo $$l | cut -f 1 -d':')" "$$(echo $$l | cut -f 2- -d'#')"; done

install: # Install dependencies via bun.
	bun install

dev: # Watch mode — re-runs on save. Interactive Ink mode (requires a TTY).
	bun --watch run src/index.tsx $(ARGS)

run: # Run tree. Pass PROMPT="..." for one-shot; ARGS="..." for flags like --resume, --continue.
	bun run src/index.tsx $(ARGS) $(if $(PROMPT),--print "$(PROMPT)",)

typecheck: # Run the TypeScript compiler in type-check-only mode.
	bun tsc --noEmit

tests: # Run the test suite (unit only — fast, no external infra; integration tests were removed deliberately: too slow for feedback loops).
	bun test

test-watch: # Watch tests — TDD loop.
	bun test --watch tests/unit

format-fix: # Auto-format sources via Biome.
	bunx biome format --write src tests

format-check: # Check formatting without writing.
	bunx biome format src tests

lint-fix: # Auto-fix lint + format + organize-imports via Biome.
	bunx biome check --write src tests

lint-check: # Check lint + format + organize-imports via Biome (no writes).
	bunx biome check src tests

build: # Compile to a standalone binary at dist/tree.
	bun build src/index.tsx --compile --outfile dist/tree

clean: # Remove build artifacts and installed deps.
	rm -rf dist node_modules
