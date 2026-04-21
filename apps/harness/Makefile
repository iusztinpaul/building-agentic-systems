ifeq (,$(wildcard ../../.env))
$(error Root .env file is missing. From the repo root run: "cp .env.example .env" and fill in GOOGLE_API_KEY.)
endif

include ../../.env
export

.PHONY: help install dev run typecheck test unit-tests integration-tests test-watch format format-check lint build clean

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

test: # Run all tests (unit + integration).
	bun test

unit-tests: # Run unit tests only — fast, no external infra.
	bun test tests/unit

integration-tests: # Run integration tests only (require make local-start, GOOGLE_API_KEY).
	bun test tests/integration

test-watch: # Watch unit tests — TDD loop.
	bun test --watch tests/unit

format: # Auto-format sources via Biome.
	bunx biome format --write src tests

format-check: # Check formatting without writing.
	bunx biome format src tests

lint: # Lint via Biome.
	bunx biome check src tests

build: # Compile to a standalone binary at dist/tree.
	bun build src/index.tsx --compile --outfile dist/tree

clean: # Remove build artifacts and installed deps.
	rm -rf dist node_modules
