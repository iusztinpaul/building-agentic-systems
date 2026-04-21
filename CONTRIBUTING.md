# Contributing

## Repo layout

This is a monorepo. Each app is self-contained; only cross-app concerns live at the repo root.

```
building-agentic-systems/
├── apps/
│   ├── memory/          # Python app: ETL + knowledge-graph memory + FastMCP server
│   └── harness/         # Planned TypeScript/Ink/Bun coding-agent harness (see docs/harness-plan.md)
├── docker/              # Shared infra config (MongoDB + mongot)
├── docs/                # Architecture & design docs
├── docker-compose.yml   # Shared infra orchestration
├── .mcp.json            # MCP servers the agents/harness spawn
├── .env / .env.example  # Shared secrets across apps
└── Makefile             # Thin root: delegates to apps/*/Makefile; shared infra targets
```

See [`README.md`](README.md) for prerequisites and installation.

## Development workflow

1. Branch off `main`: `git checkout -b feat/my-feature` (or `fix/...`, `refactor/...`).
2. Work inside the app that owns the change — memory code goes in `apps/memory/src/`, harness code in `apps/harness/src/` (once the harness lands).
3. Run targets via the root Makefile:
   - `make memory-<target>` delegates to `apps/memory/Makefile` (e.g., `make memory-unit-tests`, `make memory-serve-mcp`).
   - `make harness-<target>` is reserved for the future harness app.
   - `make local-start` / `make local-stop` — shared MongoDB + mongot infra.
   - `make tests` — aggregate across all apps.
   - `make pre-commit` — lint / format across the repo.
   - `make help` — list top-level targets.
4. Before committing: `make memory-format-fix && make memory-lint-fix && make pre-commit`.
5. Open a PR against `main`. CI runs format, lint, and tests (`.github/workflows/ci.yml`).

The full feature-development workflow, test conventions, and per-app design rules live in [`CLAUDE.md`](CLAUDE.md).

## One-time local setup

After cloning, tell git to ignore the monorepo-restructure commit in `blame` output so line authorship reflects the original authors:

```bash
git config blame.ignoreRevsFile .git-blame-ignore-revs
```

## Where to edit what

| Change | Location |
|---|---|
| Memory-app business logic | `apps/memory/src/tree/` |
| Memory ETL / pipeline entry scripts | `apps/memory/scripts/` |
| Memory tests | `apps/memory/tests/{unit,integration}/` |
| App-level YAML config | `apps/memory/configs/default.yaml` |
| Python deps | `apps/memory/pyproject.toml` |
| Memory Dockerfile | `apps/memory/docker/Dockerfile` |
| Harness (TS) code | `apps/harness/src/` *(planned — see [`docs/harness-plan.md`](docs/harness-plan.md))* |
| Shared env vars | `.env` / `.env.example` |
| MCP server registration | `.mcp.json` |
| Shared MongoDB/mongot config | `docker/` |
| Shared infra orchestration | `docker-compose.yml`, `docker-compose.ci.yml` |
| Root-level delegating Makefile | `Makefile` |
| CI workflow | `.github/workflows/ci.yml` |

## Editor settings

`.editorconfig` defines indent / line-ending / trailing-whitespace rules per file type. VS Code, JetBrains IDEs, and Zed read it natively; Vim / Emacs need a plugin. No manual setup beyond opening the repo.
