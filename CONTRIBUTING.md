# Contributing

## Repo layout

This is a monorepo — see [`README.md`](README.md#repo-layout) for the layout, prerequisites, and installation.

## Development workflow

1. Branch off `main`: `git checkout -b feat/my-feature` (or `fix/...`, `refactor/...`).
2. Work inside the app that owns the change — memory code goes in `apps/memory/src/`, harness code in `apps/harness/src/`.
3. Run targets via the root Makefile — `make help` lists them (see [`README.md`](README.md#qa-and-tests)).
4. Before committing: `make memory-format-fix && make memory-lint-fix && make pre-commit`.
5. Open a PR against `main`. CI runs format, lint, and tests (`.github/workflows/ci.yml`).

The full feature-development workflow, test conventions, and per-app design rules live in [`CLAUDE.md`](CLAUDE.md).

## One-time local setup

After cloning, tell git to ignore the monorepo-restructure commit in `blame` output so line authorship reflects the original authors:

```bash
git config blame.ignoreRevsFile .git-blame-ignore-revs
```

## Editor settings

`.editorconfig` defines indent / line-ending / trailing-whitespace rules per file type. VS Code, JetBrains IDEs, and Zed read it natively; Vim / Emacs need a plugin. No manual setup beyond opening the repo.
