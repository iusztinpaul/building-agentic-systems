---
id: 202-harness-toolchain-node-pnpm-vitest-biome
status: pending
feature: tree-agent-harness
---

# Replace the Bun scaffold with the spec's toolchain — Node ≥ 22, ESM, `pnpm` (via corepack), `vitest`, Biome at 76/100 — and rename the package `tree-agent`, leaving every harness gate green on an empty-but-valid project

Tags: `harness`, `infra`, `refactor`
Depends on: None
Blocks: 203 (every later harness task builds on this project skeleton)
Implements: ADR-016 §1 (toolchain), §10 (Biome 76/100 split), §11 (harness verification cadence)

## Scope

No feature code. This task moves everything wired to Bun onto Node + pnpm and proves the gates
(`make harness-*`, `make pre-commit`, the CI harness job) are green before any `tree-agent` module exists.

1. **Package.** `apps/harness/package.json`: `name` `tree-agent`, `version` `0.1.0`, `private`, `type: module`,
   `bin: { "tree-agent": "dist/cli.js" }`, `engines.node ">=22"`, a `packageManager` pin for pnpm (corepack
   reads it — the local machine has corepack 0.25.2 and NO global pnpm; `corepack enable pnpm` is the documented
   install step). Dev deps: `typescript`, `vitest`, `@biomejs/biome` (current major), `@types/node`. Runtime
   deps: none yet (203 adds `zod` + `zod-to-json-schema`). Running `src/cli.ts` without a build (`dev`, `run`):
   the SWE picks native Node type-stripping (then tsconfig `erasableSyntaxOnly`) or one dev dep (`tsx`) —
   record the choice and why in the Log. Delete `bun.lock`, `bunfig.toml`, `@types/bun`, `src/index.ts`,
   `tests/unit/smoke.test.ts`; commit `pnpm-lock.yaml`.
2. **tsconfig.** `strict`, ESM (`module`/`moduleResolution` `NodeNext`), `target` ES2022, `types: ["node"]`
   (no `bun-types`, no `jsx`), `noUncheckedIndexedAccess`, `noImplicitOverride`, `verbatimModuleSyntax`,
   `isolatedModules`. Typecheck covers `src/` AND `tests/`; the build emits only `src/` to `dist/` (a separate
   build config is fine). Bias to plain `tsc` for the build — no bundler.
3. **Biome.** Migrate `biome.json` to the installed major's schema (the current `files.ignore` key must move to
   whatever that major uses). Formatter `lineWidth` 76 for `src/**`, 100 for `tests/**` via an override;
   keep double quotes, semicolons, trailing commas, organize-imports, `useImportType: error`,
   `noExplicitAny: warn`.
4. **Placeholder entry + test.** `src/cli.ts` (shebang `#!/usr/bin/env node`) prints a one-line usage to
   stderr and exits 1; `tests/cli.test.ts` is one vitest test that imports nothing from Bun. Both are replaced
   by 203/204.
5. **`apps/harness/Makefile`.** Same target names (help, install, dev, run, typecheck, tests, test-watch,
   format-fix, format-check, lint-fix, lint-check, build, clean) on pnpm/Node; keep the root-env `include` and
   the `ENV_TARGET` switch; the `$(error …)` text names `OPENROUTER_API_KEY` (not `GOOGLE_API_KEY`);
   `run` passes `PROMPT` as the positional prompt (`make harness-run PROMPT="say hi"`) and `ARGS` as flags;
   `build` emits `dist/cli.js`; `clean` removes `dist` and `node_modules`.
6. **CI** (`.github/workflows/ci.yml`, harness job only — the memory job is untouched): `actions/setup-node@v4`
   on Node 22 with pnpm caching plus the pnpm setup action (or corepack), `pnpm install --frozen-lockfile`,
   `pnpm exec biome check src tests`, `pnpm exec tsc --noEmit`, `pnpm exec vitest run`. Direct pnpm commands,
   not `make harness-*` (the harness Makefile `$(error)`s without a `.env` on the runner). The workflow `env:`
   block does NOT define `OPENROUTER_API_KEY` — live tests must stay skipped on CI.
7. **pre-commit.** Hook `biome-check-harness` entry becomes
   `bash -c 'cd apps/harness && pnpm exec biome check --write src tests'`; its comment says it needs Node ≥ 22
   with corepack/pnpm (not Bun).
8. **Repo files.** `AGENTS.md`: line 37 (`pnpm` manages the harness), line 55 (`squid-testing-python` is
   Python-only), one harness line in "Verification cadence" and the harness e2e clause in step 4 — the exact
   diff is in the Feature Plan (§4) and must be applied verbatim. `.gitignore`: drop `apps/harness/.bun/`.
   `.env.example`: add `OPENROUTER_API_KEY=your-openrouter-api-key` under `# LLM API` with a one-line comment
   ("the harness's only provider"). `apps/harness/README.md`: shrink the placeholder to what is true today
   (name, `corepack enable pnpm`, the three make commands) — the real README is task 213.

**Out of scope (intentional):** any `src/` module beyond the usage stub (203+); root `README.md` (213 — it
still says Bun/Ink until then, by the plan's choice); a bundler or a compiled single binary (`tsc` emit is
enough for a `bin`); a `pnpm-workspace.yaml` (single package); Turborepo.

## Acceptance Criteria

- [ ] On a clean checkout with Node ≥ 22, corepack enabled and NO global `pnpm`, `make harness-install`
      succeeds from the committed `pnpm-lock.yaml`; `bun.lock`, `bunfig.toml`, `@types/bun`, `bun-types` and
      `jsx` are gone; `grep -ri "bun" apps/harness/package.json apps/harness/tsconfig.json
      apps/harness/Makefile .pre-commit-config.yaml .github/workflows/ci.yml .gitignore AGENTS.md` matches
      nothing.
- [ ] `make harness-lint-check`, `make harness-format-check`, `make harness-typecheck`, `make harness-tests`
      (≥ 1 vitest test), `make harness-build` and `make pre-commit` are green; the root aggregates
      `make tests`, `make lint-check`, `make format-check`, `make typecheck` still delegate and pass.
- [ ] Line width is enforced per tree, checked with a statement the formatter CAN rewrap (a call with several
      arguments, not a bare string literal): a 77-column one in `src/` fails `make harness-format-check` and is
      rewrapped by `make harness-format-fix`; the same statement in `tests/` passes; a 101-column one in
      `tests/` fails.
- [ ] `make harness-build` writes `dist/cli.js` whose first line is `#!/usr/bin/env node`; `node dist/cli.js`
      prints usage and exits 1; `make harness-run` (no `PROMPT`) prints the same usage and exits 1 — without
      a build step (dev-time execution of `src/cli.ts` works).
- [ ] `make harness-typecheck` also type-checks `tests/` (a deliberate type error in a test file fails it);
      `dist/` contains no `tests/` output.
- [ ] `apps/harness/Makefile` keeps the `ENV_TARGET` switch (`echo prod > .env.target` makes it include
      `../../.env.prod`; `make env-local` restores `.env`) and its `$(error …)` text names `OPENROUTER_API_KEY`.
- [ ] The CI harness job installs with `pnpm install --frozen-lockfile`, runs Biome, `tsc --noEmit` and
      `vitest run` through `pnpm exec`, and the `env:` block has no `OPENROUTER_API_KEY` (negative:
      `grep -n OPENROUTER .github/workflows/ci.yml` is empty); the memory job's lines are byte-identical.
- [ ] With a harness `.ts` change staged, `make pre-commit` runs `biome-check-harness` through `pnpm exec`
      and passes; the entry no longer mentions `bunx`.
- [ ] `AGENTS.md` carries exactly the diff in the Feature Plan §4 (`git diff AGENTS.md` shows changes at
      lines 37, 55 and the cadence block only); `.gitignore` no longer lists `apps/harness/.bun/`;
      `.env.example` has `OPENROUTER_API_KEY=your-openrouter-api-key`.
- [ ] Every target works from a shell whose `PATH` has no `bun` binary.
- [ ] `make harness-lint-fix && make harness-lint-check && make harness-typecheck && make harness-tests &&
      make pre-commit` green.

## User Stories

### Story: A new contributor sets up the harness on a machine without pnpm
1. They clone the repo on Node 26, run `corepack enable pnpm`, then `make harness-install`.
2. corepack fetches the pnpm version pinned in `package.json`; the install finishes with no lockfile change
   (`git status` clean).
3. `make harness-tests` prints one passing vitest test; `make harness-typecheck` prints nothing and exits 0.

### Story: The SWE writes an over-long line in `src/`
1. They add a 77-column call `usage(process.stderr, "tree-agent", { exitCode: 1, showFlags: true });` to `src/cli.ts`.
2. `make harness-format-check` fails naming `src/cli.ts`; `make harness-format-fix` rewraps it; the check passes.
3. They paste the same line into `tests/cli.test.ts`; `make harness-format-check` passes (100-column tree).

### Story: CI runs the harness job on a pull request
1. The job checks out, sets up Node 22 + pnpm, runs `pnpm install --frozen-lockfile`.
2. `pnpm exec biome check src tests`, `pnpm exec tsc --noEmit`, `pnpm exec vitest run` pass; no step needs `.env`.
3. The memory job is unchanged and still green.

### Story: The orchestrator runs the harness cadence after a commit
1. They run `make harness-lint-fix && make harness-lint-check && make harness-typecheck && make harness-tests && make pre-commit`.
2. Every command exits 0; the `.env` include means the shell did not need to export anything.

### Story: An operator switches env targets
1. `make env-prod` → `make harness-help` includes `.env.prod` (the `$(error)` names `OPENROUTER_API_KEY` if that file is missing).
2. `make env-local` → `.env` is included again.

---

Blocked by: (none)

## Log

### [PA] 2026-10-24 19:40 — Grooming

**Summary**
Swap the harness scaffold from Bun to the spec's Node ≥ 22 / pnpm / vitest / Biome stack, rename the package
`tree-agent`, and move every repo hook (Makefile, CI, pre-commit, AGENTS.md, .gitignore, .env.example) with it,
proving the gates green on an empty project before any feature code lands.

**Key decisions**
- Toolchain fidelity to pi (the book's reference) outweighs the Bun-only convenience the repo note
  `docs/notes/modern-typescript-stack.md` argues for — recorded in ADR-016 §1.
- CI stays on direct `pnpm exec` commands (no `.env` on the runner) and gets NO `OPENROUTER_API_KEY`, so the
  live provider tests of 203 stay skipped there by design.
- Biome 76/100 is enforced by an override, not two configs; `tsc` emits `dist/cli.js` — no bundler.
- The root README keeps its Bun/Ink text until 213 (one docs pass at the end, by the plan).

**Dependencies**
- None.

**User stories**
- 5 stories covering: fresh install via corepack, the 76/100 width rule, the CI job, the cadence, env switching.

Ready for implementation.
