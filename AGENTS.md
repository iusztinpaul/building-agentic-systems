# Tree — Your Rooted Personal Assistant

A personal assistant rooted in a knowledge-graph memory, powered by ontologies, LLMs, and agents.

# Key Principles You Will Respect All Over Your Work

- Always prioritize removing instructions over adding more.
- Whenever you add a new rule within the memory (such as AGENTS.md), resources or skills, support it with a clear, concise explanation, plus a set of good and bad examples. Good examples: "a 200-token chunk size", "sub-100ms latency". Bad examples: "a powerful architecture", "a robust pipeline".

# Memory

- **Configuration:** done in two layers, plus an escape hatch:
  1. The root `.env` file injects environment variables (credentials + higher-level config needed to boot the harness and memory; see `.env.example`). Loaded at runtime via `apps/memory/src/tree/config/settings.py`.
  2. All memory-app config lives in YAML under `apps/memory/src/tree/config`, loaded at `apps/memory/src/tree/config/app_config.py`.
  3. **Escape hatch.** Operators may override any YAML key via `TREE_<SECTION>__<KEY>` env vars — e.g. `TREE_EXTRACTION__DEDUP__AUTO_MERGE_THRESHOLD=0.99`. Mechanism: `_apply_env_overrides` in `app_config.py`. For emergency one-shot ops use; new knobs should not be documented in `.env.example`.
- **Entry-point scripts** (`apps/memory/scripts/`, `apps/memory/deploy/`): no business logic — load it from `apps/memory/src/tree/` and write only the glue code to call it. Must call `init_logger()` from `tree.logging` at module level to configure logging.

# Key Software Design Choices

- All dates are timezone aware (UTC by default). We don't accept naive datetime objects.
- Always add types to function/method parameters and return types — even when they return `None`.
- Pipelines must be idempotent, retried, and checkpointed.
- Logging: the native Python logger — never `print`.
- Always Pydantic over dataclasses or typed dicts when defining data structures.
- Python with async patterns.
- Loose clean architecture decoupling infrastructure, serving, app and domain logic:
  - `entities/` defines shared ODMs, enums and other structures used across the project; per-module `types.py` files define types used only within that module or layers upwards.
  - Infrastructure we don't plan to change (MongoDB, Prefect, Opik) is imported directly — not made modular.
  - Flat structure and naming based on actionability rather than dogmatic clean architecture.

# Documentation

Before writing code against any dependency or external service, use the `tech-docs` skill (`.agents/skills/tech-docs/SKILL.md`): context7 first, then the `llms.txt` indexes it lists.

# Running Commands

We manage all core commands through GNU Make (see [`Makefile`](Makefile)); run everything with `make ...`. `uv` manages the `apps/memory` Python project (`uv run <command>`); `bun` manages the `apps/harness` TypeScript project (`bun run <command>`).

- `make help` — list all root targets.

## Environments

We have two environments: `local` (Docker-based, loads `.env`) and `production` (Cloud, loads `.env.prod`)

Run `make env-status` to see which environment is currently active. Switch between environments by running `make env-local` / `make env-prod`.

## Infrastructure & external-service CLIs

Use the CLIs installed directly on the system: `mongosh` (any MongoDB instance), `gh` (the remote GitHub repo — PRs, issues, Actions).

Run `uv`-managed CLIs from the repo root with `uv --directory apps/memory run ...` (or `uv run ...` from `apps/memory/`): `python ...`, `prefect ...` (deployments served in `apps/memory/src/tree/orchestrator.py` run with `prefect deployment run [DEPLOYMENT_NAME]`), `modal ...`, `opik ...`. Deps available in `apps/memory/pyproject.toml`.

# Testing & QA

Always call the `/squid-testing-python` skill from the Squid plugin when writing tests.

**Always run tests via the `make memory-*` targets, not a bare `uv run pytest`.** The Makefile does `include .env`/`export`, so credentials like `VOYAGE_API_KEY` are present; a bare `uv run pytest` does NOT load `.env`, so live-model tests fail with "Voyage API key is required" — which looks like real breakage but is just a missing-env artifact of the wrong invocation.

**Run tests only with the LOCAL env target (`make env-status` → local).** With `.env.target=prod` the suite hits the Atlas cluster, where index-creating tests fail with "The maximum number of FTS indexes has been reached for this instance size" (M0 cap) — and tests must not write to prod anyway. direnv exports the prod vars into every shell, so switch with `make env-local` rather than `--env-file` overrides, which do NOT win over already-exported vars.

## Verification cadence

After every commit to git:

1. Format and lint: `make memory-format-fix && make memory-lint-fix && make memory-format-check && make memory-lint-check`
2. Pre-commit: `make pre-commit`
3. Tests: `make memory-tests`

When a feature is done and ready for PR, ALWAYS run:

4. Run and verify the code end-to-end with the `run-pipelines-e2e` skill (`.agents/skills/run-pipelines-e2e/SKILL.md`), adapted to the changes you made.

NEITHER app has an integration test suite (deleted deliberately — too slow for feedback loops); each `make <app>-tests` target runs the unit suite, and e2e verification happens by running the real pipelines instead.

# Developing New Features & Bug Fixes

This project uses the **squid** agent-team plugin — follow its processes one-to-one. Direct chat for trivial edits; for one or a few groomed tasks use `/squid-implement-task`; to plan a whole feature use `/squid-plan` then `/squid-implement-night`. Bugs go through `/squid-triage-issue`, structural changes through `/squid-refactor`; `/squid-review` and `/squid-review-ci` run as standalone gates.
