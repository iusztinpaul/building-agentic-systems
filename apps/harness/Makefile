# Env-target switch (mirrors root Makefile): .env.target == "prod" selects ../../.env.prod, otherwise ../../.env.
ENV_TARGET := $(strip $(shell cat ../../.env.target 2>/dev/null))
ifeq ($(ENV_TARGET),prod)
ENV_FILE := ../../.env.prod
else
ENV_FILE := ../../.env
endif

ifeq (,$(wildcard $(ENV_FILE)))
$(error Root $(notdir $(ENV_FILE)) file is missing. From the repo root run: "cp .env.example $(notdir $(ENV_FILE))" and fill in OPENROUTER_API_KEY)
endif

include $(ENV_FILE)
export

.PHONY: help install dev run typecheck tests test-watch format-fix format-check lint-check lint-fix build clean

help: # Display harness-app targets.
	@grep -E '^[a-zA-Z0-9 _-]+:.*#'  Makefile | sort | while read -r l; do printf "\033[1;32m%s\033[00m:%s\n" "$$(echo $$l | cut -f 1 -d':')" "$$(echo $$l | cut -f 2- -d'#')"; done

install: # Install deps from pnpm-lock.yaml. Needs Node >= 22 + `corepack enable pnpm` (no corepack? `npm i -g corepack`).
	pnpm install --frozen-lockfile

dev: # Watch mode: re-run src/cli.ts on save (Node's native type stripping, Node >= 22.18).
	node --watch src/cli.ts $(ARGS)

run: # Run src/cli.ts without a build. PROMPT="..." is the positional prompt; ARGS="..." for flags.
	node src/cli.ts $(ARGS) $(if $(PROMPT),"$$PROMPT",)

typecheck: # Type-check src/ and tests/ (tsc --noEmit).
	pnpm exec tsc --noEmit

tests: # Run the vitest suite once (live tests run only when OPENROUTER_API_KEY is set).
	pnpm exec vitest run

test-watch: # Watch tests — TDD loop.
	pnpm exec vitest

format-fix: # Auto-format sources via Biome.
	pnpm exec biome format --write src tests

format-check: # Check formatting without writing.
	pnpm exec biome format src tests

lint-fix: # Auto-fix lint + format + organize-imports via Biome.
	pnpm exec biome check --write src tests

lint-check: # Check lint + format + organize-imports via Biome (no writes).
	pnpm exec biome check src tests

build: # Compile src/ with tsc to dist/ (bin: dist/cli.js).
	rm -rf dist
	pnpm exec tsc -p tsconfig.build.json
	chmod +x dist/cli.js

clean: # Remove build artifacts and installed deps.
	rm -rf dist node_modules
