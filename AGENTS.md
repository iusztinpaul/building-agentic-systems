# Tree — Your Rooted Personal Assistant

A personal assistant rooted in a knowledge-graph memory, powered by ontologies, LLMs, and agents. A Python (`apps/memory`) + TypeScript (`apps/harness`) monorepo.

# Key Principles You Will Respect All Over Your Work

- Always prioritize removing instructions over adding more.
- Whenever you add a new rule within the memory (such as AGENTS.md), resources or skills, support it with a clear, concise explanation, plus a set of good and bad examples. Good examples: "a 200-token chunk size", "sub-100ms latency". Bad examples: "a powerful architecture", "a robust pipeline".

# Key Components

Two apps:

- **`apps/harness`** — the user-facing component: a TUI CLI implementing a custom coding-agent harness (TypeScript/Bun). Internals are still being designed — see `docs/harness-plan.md`.
- **`apps/memory`** — the context layer (Python): ingestion + retrieval pipelines for the knowledge graph, served to the harness via an MCP server.

## Memory

- **Data Pipeline:** ETL pipelines gathering data from multiple sources and normalizing everything into the `documents` collection. One ETL pipeline per source, such as Substack, Substack RSS feeds, HuggingFace Datasets, YouTube, Custom sites, etc.
- **Memory Pipeline:** Maps `documents` to `knowledge graph objects` within the `knowledge_graph` collection by cleaning, chunking, graph extracting, normalizing and upserting nodes and edges directly.
- **The Unified Memory:** The agent's unified memory powered by MongoDB that leverages text, semantic and graph search. The data is stored in a single mutable `knowledge_graph` collection.
- **Agentic Tools:** Tools used to query or write to the unified memory.
- **MCP Server:** The memory is served as an MCP server to the harness, allowing the harness to query and write to the memory.
- **Configuration:** done in two layers, plus an escape hatch:
  1. The root `.env` file injects environment variables (credentials + higher-level config needed to boot the harness and memory; see `.env.example`). Loaded at runtime via `apps/memory/src/tree/config/settings.py`.
  2. All memory-app config lives in YAML under `apps/memory/src/tree/config`, loaded at `apps/memory/src/tree/config/app_config.py`.
  3. **Escape hatch.** Operators may override any YAML key via `TREE_<SECTION>__<KEY>` env vars — e.g. `TREE_EXTRACTION__DEDUP__AUTO_MERGE_THRESHOLD=0.99`. Mechanism: `_apply_env_overrides` in `app_config.py`. For emergency one-shot ops use; new knobs should not be documented in `.env.example`.

### Memory App Specifics

- Memory-app entry-point scripts (`apps/memory/scripts/`) + deploy scripts (`apps/memory/deploy/`):
  - Don't implement business logic in the scripts. Only load it from `apps/memory/src/tree/` + write the glue code to call it.
  - Must call `init_logger()` from `tree.logging` at module level to configure logging.

# Project Structure

Monorepo: each app lives under `apps/` and owns its build files (`pyproject.toml`/`package.json`, `Makefile`). Only cross-app concerns live at the repo root.

- **`apps/memory`** — Python: ETL + knowledge graph + MCP server. Core module `src/tree/` holds `config/`, `entities/` (shared ODMs), `models/` (LLM/embedding interfaces), `data/` (one ETL subpackage per source), `mcp/` (FastMCP server), and `memory/` (extraction, indexing, query, resolution, review, consolidation); plus `deploy/`, `configs/`, `scripts/`, `tests/`.
- **`apps/harness`** — TS/Bun coding-agent TUI (see `docs/harness-plan.md`). `src/`: `agent/`, `tools/`, `mcp/`, `hooks/`, `permissions/`, `session/`, `ui/`.
- **Repo root (cross-app only):** `docker/` (shared MongoDB + mongot infra), `docs/` (incl. `adrs/`), `tracker/`, `.env`/`.env.example`, `.mcp.json`, `docker-compose.yml`, and the thin delegating `Makefile`.

# Key Software Design Choices

- All dates are timezone aware (UTC by default). We don't accept naive datetime objects.
- Always add types to function/method parameters and return types — even when they return `None`.
- Pipelines must be idempotent, retried, and checkpointed.

## Python

- Always Pydantic over dataclasses or typed dicts when defining data structures.
- Python with async patterns.
- Loose clean architecture decoupling infrastructure, serving, app and domain logic:
  - `entities/` defines shared ODMs, enums and other structures used across the project; per-module `types.py` files define types used only within that module or layers upwards.
  - Infrastructure we don't plan to change (MongoDB, Prefect, Opik) is imported directly — not made modular.
  - Flat structure and naming based on actionability rather than dogmatic clean architecture.

# Tech Stack

## Core

- **Data validation and structuring:** Pydantic
- **ODM:** Beanie + PyMongo Async driver
- **MCP Server Framework:** FastMCP
- **Testing:** Pytest
- **CLI:** Click
- **Logging:** Native Python logger (never prints!)
- **Embedding Models:** Sentence Transformers (local + open-source), Voyage AI (API + closed-source), Modal (remote + open-source)

## Services

- **Frontier Model API:** Gemini
- **Embedding Models API:** Voyage AI — [text embeddings docs](https://docs.voyageai.com/docs/embeddings) · [text API](https://docs.voyageai.com/reference/embeddings-api) · [multimodal docs](https://docs.voyageai.com/docs/multimodal-embeddings) · [multimodal API](https://docs.voyageai.com/reference/multimodal-embeddings-api)
- **Searching, crawling, scraping:** Bright Data

## Infrastructure

- **Unified memory and database:** MongoDB
- **Serving AI Models & Remote Sandboxing:** Modal
- **Observability and evals:** Opik
- **Containerization:** Docker
- **CI/CD:** GitHub Actions
- **Pipeline orchestrator + agentic durable workflows:** Prefect

## Access Documentation

Use the `context7` MCP server (when connected) to look up authoritative usage for any tech-stack item or external service above; fall back to web search otherwise.

**Reference docs (`llms.txt` — fetch on demand).** Each link below is an *index* of doc pages. Fetch the index first, then fetch only the specific page(s) you need. Do **not** pull whole `llms-full.txt` files into context unless a task truly requires the full reference, as it's large and consume tons of tokens.

- **Gemini:** https://ai.google.dev/gemini-api/docs/llms.txt — scoped API reference index also at https://ai.google.dev/api/llms.txt (no Python-only variant; append .md.txt to any docs page (e.g. …/docs/libraries.md.txt) for a scoped, plain-markdown version.)
- **MongoDB:** https://www.mongodb.com/llms.txt
- **MongoDB Voyage AI:** https://docs.voyageai.com/llms.txt
- **Modal:** https://modal.com/llms.txt — full reference at https://modal.com/llms-full.txt
- **Opik:** https://www.comet.com/docs/opik/llms.txt — also append /llms.txt to any section URL for a scoped index.
- **Prefect:** https://docs.prefect.io/llms.txt — full reference at https://docs.prefect.io/llms-full.txt
- **FastMCP:** https://gofastmcp.com/llms.txt — full reference at https://gofastmcp.com/llms-full.txt
- **Bright Data:** https://docs.brightdata.com/llms.txt — full reference at https://docs.brightdata.com/llms-full.txt

# Running Commands

We manage all core commands through GNU Make (see [`Makefile`](Makefile)); run everything with `make ...`. `uv` manages the `apps/memory` Python project (`uv run <command>`); `bun` manages the `apps/harness` TypeScript project (`bun run <command>`).

- `make memory-<target>` — run `<target>` inside `apps/memory/` (e.g. `make memory-unit-tests`, `make memory-serve-mcp`).
- `make harness-<target>` — reserved for the future TS harness at `apps/harness/`.
- `make local-start` / `make local-stop` / `make local-restart` — shared Docker infra.
- `make tests` — aggregate: runs all apps' tests.
- `make pre-commit` — pre-commit across the repo.
- `make memory-build` — build the memory app.
- `make help` — list all root targets.

## Environments

We have two environments: `local` (Docker-based, loads `.env`) and `production` (Cloud, loads `.env.production`)

Run `make env-status` to see which environment is currently active. Switch between environments by running `make env-local` / `make env-production`.

## Infrastructure & external-service CLIs

Use the CLIs installed directly on the system: `mongosh` (any MongoDB instance), `gh` (the remote GitHub repo — PRs, issues, Actions), `git` (Git operations).

Run `uv`-managed CLIs from the repo root with `uv --directory apps/memory run ...` (or `uv run ...` from `apps/memory/`): `python ...`, `prefect ...`, `modal ...`, `opik ...`. Deps available in `apps/memory/pyproject.toml`.

Trigger Prefect deployments via `uv run prefect deployment ...` from `apps/memory/` — e.g. a deployment served in `apps/memory/src/tree/orchestrator.py` runs with `prefect deployment run [DEPLOYMENT_NAME]`.

# Testing & QA

Always call the `/testing-python` skill from the Squid plugin when writing tests.

**Always run tests via the `make memory-*` targets, not a bare `uv run pytest`.** The Makefile does `include .env`/`export`, so credentials like `VOYAGE_API_KEY` are present; a bare `uv run pytest` does NOT load `.env`, so live-model tests fail with "Voyage API key is required" — which looks like real breakage but is just a missing-env artifact of the wrong invocation.

**Run tests only with the LOCAL env target (`make env-status` → local).** With `.env.target=prod` the suite hits the Atlas cluster, where index-creating tests fail with "The maximum number of FTS indexes has been reached for this instance size" (M0 cap) — and tests must not write to prod anyway. direnv exports the prod vars into every shell, so switch with `make env-local` (and back with `make env-prod`) rather than `--env-file` overrides, which do NOT win over already-exported vars.

For `apps/memory` we use `ruff` as formatter and linter.

## Verification cadence

After every commit to git:

1. Format and lint: `make memory-format-fix && make memory-lint-fix && make memory-format-check && make memory-lint-check`
2. Pre-commit: `make pre-commit`
3. Unit tests: `make memory-unit-tests`
4. Case by case:
   - Fast integration tail (skips `@pytest.mark.slow`) when touching the infra layer: `make memory-integration-tests`
   - Slow integration tail when iterating on a vector-index or full-Prefect-e2e change: `make memory-integration-tests-slow`

When a feature is done and ready for PR, ALWAYS run:

5. Full integration tests: `make memory-integration-tests-all` (~5 min; includes `@pytest.mark.slow`). CI runs this same target.
6. Run and verify the code end-to-end (see "Running pipelines & E2E"), adapted to the changes you made.

Mirror the CI integration command locally (skips mongot-dependent tests; runs sequentially because the shared-DB cleanup fixture makes parallel `-n auto` workers collide): `make memory-integration-tests-ci`. Run all apps' tests together: `make tests`.

## Test markers

Test selection is gated by two orthogonal markers — `slow` and `requires_mongot`. Authoritative definitions live in `apps/memory/pyproject.toml` (`[tool.pytest.ini_options] markers`); which target includes/excludes each is in the Makefile target comments.

## Running pipelines & E2E

By default, use the "Paul Iusztin" user when testing.

1. **Serve the workflows** in the background to pick up the latest code: `make memory-serve-workflows &`. This process is the in-process Prefect worker — without it, deployments register but nothing executes. If a serve process is already running, kill it first and re-serve.
2. **Run a pipeline** via its Make command (which streams logs to the terminal — use these instead of `prefect deployment run` directly so errors surface here): `make memory-run-data-pipeline` → `make memory-run-memory-pipeline-extraction` → `make memory-run-memory-pipeline-indexing` → `make memory-query-graph QUERY="test query"` → verify results.

# Developing New Features & Bug Fixes

This project uses the **squid** agent-team plugin — follow its processes one-to-one. Direct chat for trivial edits; for one or a few groomed tasks use `/implement-task`; to plan a whole feature use `/plan` then `/implement-night`. Bugs go through `/triage-issue`, structural changes through `/refactor`; `/review` and `/review-ci` run as standalone gates.
