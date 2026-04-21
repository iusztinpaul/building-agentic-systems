ifeq (,$(wildcard ../../.env))
$(error Root .env file is missing. From the repo root run: "cp .env.example .env" and fill in GOOGLE_API_KEY.)
endif

include ../../.env
export

.PHONY: help install dev run typecheck test format format-check lint build clean

help: # Display harness-app targets.
	@grep -E '^[a-zA-Z0-9 _-]+:.*#'  Makefile | sort | while read -r l; do printf "\033[1;32m%s\033[00m:%s\n" "$$(echo $$l | cut -f 1 -d':')" "$$(echo $$l | cut -f 2- -d'#')"; done

install: # Install dependencies via bun.
	bun install

dev: # Watch mode — re-runs on save. Interactive mode arrives at M3 (Ink).
	bun --watch run src/index.ts

run: # One-shot run. Pass PROMPT="..." or arguments will be forwarded.
	bun run src/index.ts $(if $(PROMPT),--print "$(PROMPT)",)

typecheck: # Run the TypeScript compiler in type-check-only mode.
	bun tsc --noEmit

test: # Run Bun tests (empty suite until M2).
	bun test

format: # Auto-format sources via Biome.
	bunx biome format --write src

format-check: # Check formatting without writing.
	bunx biome format src

lint: # Lint via Biome.
	bunx biome check src

build: # Compile to a standalone binary at dist/tree.
	bun build src/index.ts --compile --outfile dist/tree

clean: # Remove build artifacts and installed deps.
	rm -rf dist node_modules
