---
id: 202-harness-toolchain-node-pnpm-vitest-biome
status: done
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

- [x] On a clean checkout with Node ≥ 22, corepack enabled and NO global `pnpm`, `make harness-install`
      succeeds from the committed `pnpm-lock.yaml`; `bun.lock`, `bunfig.toml`, `@types/bun`, `bun-types` and
      `jsx` are gone; `grep -ri "bun" apps/harness/package.json apps/harness/tsconfig.json
      apps/harness/Makefile .pre-commit-config.yaml .github/workflows/ci.yml .gitignore AGENTS.md` matches
      nothing.
- [x] `make harness-lint-check`, `make harness-format-check`, `make harness-typecheck`, `make harness-tests`
      (≥ 1 vitest test), `make harness-build` and `make pre-commit` are green; the root aggregates
      `make tests`, `make lint-check`, `make format-check`, `make typecheck` still delegate and pass.
- [x] Line width is enforced per tree, checked with a statement the formatter CAN rewrap (a call with several
      arguments, not a bare string literal): a 77-column one in `src/` fails `make harness-format-check` and is
      rewrapped by `make harness-format-fix`; the same statement in `tests/` passes; a 101-column one in
      `tests/` fails.
- [x] `make harness-build` writes `dist/cli.js` whose first line is `#!/usr/bin/env node`; `node dist/cli.js`
      prints usage and exits 1; `make harness-run` (no `PROMPT`) prints the same usage and exits 1 — without
      a build step (dev-time execution of `src/cli.ts` works).
- [x] `make harness-typecheck` also type-checks `tests/` (a deliberate type error in a test file fails it);
      `dist/` contains no `tests/` output.
- [x] `apps/harness/Makefile` keeps the `ENV_TARGET` switch (`echo prod > .env.target` makes it include
      `../../.env.prod`; `make env-local` restores `.env`) and its `$(error …)` text names `OPENROUTER_API_KEY`.
- [x] The CI harness job installs with `pnpm install --frozen-lockfile`, runs Biome, `tsc --noEmit` and
      `vitest run` through `pnpm exec`, and the `env:` block has no `OPENROUTER_API_KEY` (negative:
      `grep -n OPENROUTER .github/workflows/ci.yml` is empty); the memory job's lines are byte-identical.
- [x] With a harness `.ts` change staged, `make pre-commit` runs `biome-check-harness` through `pnpm exec`
      and passes; the entry no longer mentions `bunx`.
- [x] `AGENTS.md` carries exactly the diff in the Feature Plan §4 (`git diff AGENTS.md` shows changes at
      lines 37, 55 and the cadence block only); `.gitignore` no longer lists `apps/harness/.bun/`;
      `.env.example` has `OPENROUTER_API_KEY=your-openrouter-api-key`.
- [x] Every target works from a shell whose `PATH` has no `bun` binary.
- [x] `make harness-lint-fix && make harness-lint-check && make harness-typecheck && make harness-tests &&
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

### [SWE] 2026-10-24 22:20 — Implementation

**Files modified**
- `apps/harness/package.json` — `tree-agent` 0.1.0, private, ESM, `bin: dist/cli.js`, `engines.node >=22`, `packageManager: pnpm@10.34.8+sha256…` (written by `corepack use`), dev deps `typescript ^5.9.3`, `vitest ^3.2.7`, `@biomejs/biome ^2.6.3`, `@types/node ^22.20.5`; `pnpm.onlyBuiltDependencies: ["esbuild"]` (silences pnpm 10's "Ignored build scripts: esbuild" without a `pnpm-workspace.yaml`). No runtime deps.
- `apps/harness/pnpm-lock.yaml` — new, pnpm 10 lockfile v9.
- `apps/harness/tsconfig.json` — strict, ES2022, `NodeNext`/`NodeNext`, `types: ["node"]`, `noUncheckedIndexedAccess`, `noImplicitOverride`, `verbatimModuleSyntax`, `isolatedModules`, `erasableSyntaxOnly`, `rewriteRelativeImportExtensions`, `noEmit`; includes `src/**` + `tests/**`.
- `apps/harness/tsconfig.build.json` — new; extends the above, `include: ["src/**/*"]`, `rootDir: src`, `outDir: dist`, emit on.
- `apps/harness/biome.json` — migrated with `biome migrate --write` (1.9.4 → 2.6.3: `files.ignore` → `files.includes: ["**", "!**/node_modules", "!**/dist"]`, `organizeImports` → `assist.actions.source.organizeImports: "on"`, `recommended: true` → `preset: "recommended"`), re-indented to 2 spaces; base `lineWidth: 76` + `overrides[{ includes: ["tests/**"], formatter.lineWidth: 100 }]`. Quotes/semicolons/trailing commas/`useImportType: error`/`noExplicitAny: warn` kept.
- `apps/harness/src/cli.ts` — new usage stub (`#!/usr/bin/env node`, one usage line to stderr, `process.exitCode = 1`).
- `apps/harness/tests/cli.test.ts` — the one vitest test: spawns `node src/cli.ts` (no build), asserts exit 1, empty stdout, and a stderr line starting with `usage: tree-agent `. Imports only `vitest`, `node:child_process`, `node:url`.
- `apps/harness/Makefile` — same 13 targets on pnpm/Node; `ENV_TARGET` switch and root-env `include` kept; `$(error …)` names `OPENROUTER_API_KEY`; `install` = `pnpm install --frozen-lockfile` (its help text carries the corepack note); `run` = `node src/cli.ts $(ARGS) "$(PROMPT)"`; `build` = `rm -rf dist && tsc -p tsconfig.build.json && chmod +x dist/cli.js`.
- Deleted: `apps/harness/bun.lock`, `bunfig.toml`, `src/index.ts`, `tests/unit/smoke.test.ts` (and the Bun-installed `node_modules`, re-created by pnpm).
- `.github/workflows/ci.yml` — harness job only: `pnpm/action-setup@v4` (`package_json_file: apps/harness/package.json`, since the repo root has no `packageManager`) → `actions/setup-node@v4` (Node 22, `cache: pnpm`, `cache-dependency-path: apps/harness/pnpm-lock.yaml`) → `pnpm install --frozen-lockfile` → `pnpm exec biome check src tests` / `pnpm exec tsc --noEmit` / `pnpm exec vitest run`.
- `.pre-commit-config.yaml` — `biome-check-harness` entry → `pnpm exec biome check --write src tests`, comment says Node ≥ 22 + corepack/pnpm. ALSO (not in scope, needed): the prettier hook gets `exclude: ^apps/harness/pnpm-lock\.yaml$` — without it `make pre-commit` rewrote the staged lockfile (1234 changed lines, quote style) and failed; pnpm owns that file's format and would undo prettier on every `pnpm add`.
- `AGENTS.md` — the Feature Plan §4 diff, applied verbatim (hunks at 37, 55, the cadence block and step 4 only).
- `.gitignore` — dropped `apps/harness/.bun/`. `.env.example` — `OPENROUTER_API_KEY=your-openrouter-api-key` under `# LLM API` with "the harness's (tree-agent) only provider" comment.
- `apps/harness/README.md` — shrunk to name, `corepack enable pnpm` (+ `npm i -g corepack` fallback), the three make commands.

**Decisions recorded (the task asked for these)**
- *Dev-time TS execution: native Node type stripping, no `tsx`.* pi itself runs its TS sources with plain `node` (`node --import ./src/…ts`, `tsc -p tsconfig.build.json` for the build — checked in `@earendil-works/pi-coding-agent`'s published scripts), so this matches ADR-016 §1's fidelity rule, and it is zero deps. Consequences for 203–213: (1) relative imports use `.ts` specifiers (`import { x } from "./foo.ts"`) — Node's stripper does not map `.js` → `.ts`; `rewriteRelativeImportExtensions` makes `tsc` emit them as `.js` (probed: a `./usage.ts` import built to `./usage.js` in `dist/` and ran), no `allowImportingTsExtensions` needed; (2) `erasableSyntaxOnly` forbids `enum`, `namespace`, constructor parameter properties; (3) dev-time `make harness-run`/`dev` and the vitest test need Node ≥ 22.18 (stripping is unflagged from 22.18; 22.6–22.17 need `--experimental-strip-types`). `engines` stays `>=22` as the task specifies because the shipped `dist/cli.js` is plain JS; CI's `node-version: 22` resolves to the latest 22.x. If a < 22.18 floor ever matters, `tsx` is the one-dep swap.
- *pnpm pin: `pnpm@10.34.8` (latest 10.x), not 11/12.* With the machine's corepack 0.25.2, `corepack pnpm --version` under `packageManager: pnpm@12.15.0` fails with `Cannot find module …/corepack/v1/pnpm/12.15.0/bin/pnpm.cjs`, and `pnpm@11.0.0` fails with `ERR_VM_DYNAMIC_IMPORT_CALLBACK_MISSING`; `pnpm@10.34.0` works. Bumping to 11+ needs a newer corepack (npm has 0.36.0) on every contributor machine.
- *Versions:* `typescript ^5.8` → 5.9.3 and `vitest ^3.2` → 3.2.7, as directed; npm also lists TS 7.0.2, vitest 4.1.12 / 5.0.5 (vitest 5 needs Node ^22.12; pi uses vitest 4.1.11). Vitest 3.2 supports Node ≥ 22.0, matching `engines`.

**Tests**
- Unit: harness 1 passing, 0 failing (`make harness-tests`); memory 5218 passed via root `make tests`.
- Integration: N/A — no integration suite (AGENTS.md).

**Acceptance criteria**
- [x] Clean-checkout install — `rsync` of `apps/harness` minus `node_modules`/`dist` into the scratchpad + empty `.env`: `make install` → `Done in 513ms using pnpm v10.34.8`, `cmp` lockfile → unchanged; tests/typecheck/build green there. Bun artefacts gone. The AC's literal `grep -ri "bun" …` matches `ubuntu-latest` (twice) in `ci.yml` — both jobs' `runs-on`, which must stay byte-identical; the word-level grep `grep -riE "bun([^t]|$)|bunx|bun-types|@types/bun" <same files> apps/harness/biome.json apps/harness/README.md` → no match (exit 1); no `bun`/`jsx` in `pnpm-lock.yaml`.
- [x] `make harness-lint-check`, `-format-check`, `-typecheck`, `-tests`, `-build`, `make pre-commit` exit 0; root `make tests` (memory 5218 passed + harness 1 passed), `make lint-check`, `make format-check`, `make typecheck` exit 0 and show the `make -C apps/harness …` delegation.
- [x] Width probe (call expression `usage(process.stderr, "tree-agent", { flags: "aaa…" }, 1);` padded to an exact width, appended then restored): src 76 → format-check 0; src 77 → 2, `make harness-format-fix` rewraps to one argument per line, re-check 0; tests 77 → 0; tests 100 → 0; tests 101 → 2. (The story's sample line is 70 columns, not 77, so I padded.)
- [x] `make harness-build` → `dist/cli.js` only, first line `#!/usr/bin/env node`, mode `-rwxr-xr-x`; `node dist/cli.js` → usage, exit 1; `make harness-run` → `node src/cli.ts` prints the same usage, Node exits 1 (`make[1]: *** [run] Error 1`; outer `make` returns 2 as it always does for a failed recipe). `make harness-run PROMPT="say hi" ARGS="--json"` → `node src/cli.ts --json "say hi"`.
- [x] A `const bad: number = "not a number";` appended to `tests/cli.test.ts` → `tests/cli.test.ts(14,7): error TS2322`, typecheck fails; restored → 0. `dist/` has no `tests/` output.
- [x] `make env-prod` → `make -C apps/harness -pn help | grep ^ENV_FILE` = `../../.env.prod`; `make env-local` → `../../.env`. `$(error)` checked in a scratch copy of the Makefile with no env files: `Root .env.prod file is missing. … fill in OPENROUTER_API_KEY.` (and the `.env` variant).
- [x] CI: `grep -n OPENROUTER .github/workflows/ci.yml` → empty (exit 1); `md5` of the memory-job block and of the header/`env:` block identical to `HEAD`.
- [x] With `apps/harness` staged, `make pre-commit` → `biome check (harness)…Passed` via `pnpm exec`; no `bunx` in the entry. (Index reset afterwards; nothing committed.)
- [x] `git diff AGENTS.md` shows only the four §4 hunks; `.gitignore` drops `.bun/`; `.env.example` has the key.
- [x] With `PATH=<scratch dir with only node+git>:~/.local/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin` (`command -v bun` → exit 1): clean, install, lint-fix, lint-check, format-fix, format-check, typecheck, tests, build, help → exit 0; run → usage + recipe exit 1. `dev` checked separately: `node --watch src/cli.ts` prints usage then `Failed running 'src/cli.ts'. Waiting for file changes…` (expected for an exit-1 stub). `test-watch` not run (interactive).
- [x] `make harness-lint-fix && make harness-lint-check && make harness-typecheck && make harness-tests && make pre-commit` → exit 0.

**Evidence**
```
$ make harness-tests
 ✓ tests/cli.test.ts (1 test) 62ms
 Test Files  1 passed (1)
      Tests  1 passed (1)
$ make tests
======================= 5218 passed in 60.44s (0:01:00) ========================
 Test Files  1 passed (1)
      Tests  1 passed (1)
make tests exit 0
$ make pre-commit
prettier.................................................................Passed
ruff check...............................................................Passed
ruff format..............................................................Passed
biome check (harness)....................................................Passed
$ make harness-build && head -1 apps/harness/dist/cli.js && node apps/harness/dist/cli.js; echo $?
#!/usr/bin/env node
usage: tree-agent [flags] "<prompt>"
1
```

**Notes**
- Local setup step I had to take: `corepack enable pnpm` fails writing to `/usr/local/bin` (permissions), so I ran `corepack enable --install-directory ~/.local/bin pnpm` (`~/.local/bin` is on PATH). The README/Makefile text only says `corepack enable pnpm`; 213 may want this fallback in the real README.
- NOT RUN — the CI job itself (runs on push). Two de-risk probes instead: (1) `pnpm/action-setup@v4`'s `src/install-pnpm/run.ts` (read via `gh api`) passes the raw `packageManager` string to `pnpm install <spec> --no-lockfile`; replaying that with `npx pnpm@9 install "pnpm@10.34.8+sha256.5be9…" --no-lockfile` in the scratchpad installed pnpm 10.34.8, so the hash suffix is accepted. (2) Node 22 as CI will run it: `npx -y node@22` (v22.23.4) `src/cli.ts` → usage, exit 1, no ExperimentalWarning; `npx -y node@22 node_modules/vitest/vitest.mjs run` → 1 passed.
- Test assertion loosened from `/^usage: tree-agent /` to `/^usage: tree-agent /m`: on Node 22.18–22.x lines that still print the type-stripping `ExperimentalWarning` to stderr before the script's output, the column-0 anchor would fail; `dist/cli.js` (plain JS) never prints it. Cadence re-run after the change: green.
- `make harness-install` uses `--frozen-lockfile`, so it never rewrites the lockfile; adding a dep is `pnpm add` inside `apps/harness` (203).
- The untracked `docs/Tree-Agent Harness Implementation Spec.md` was not touched.
- Post-QA fixes (Tester's non-blocking notes): `run` now passes the prompt via env (`"$$PROMPT"`) so quotes/backticks are not shell-evaluated (verified: backtick `touch` in PROMPT created no file, usage printed, recipe exit 1); `$(error)` text no longer ends in a doubled period. Re-ran `make harness-lint-check && make harness-tests && make pre-commit` → green.

### [Tester] 2026-10-24 22:30 — QA

**Test summary**
- Format / lint / pre-commit: PASS (`make harness-lint-check`, `harness-format-check`, `harness-typecheck`, `harness-build`, `make pre-commit` all exit 0; root `make lint-check`, `format-check`, `typecheck` exit 0 and show `make -C apps/harness …` delegation)
- Unit tests: harness 1 passed / 0 failed; memory 5218 passed / 0 failed (root `make tests`, exit 0)
- Integration tests: N/A (no integration suite per AGENTS.md)
- Warnings: 0

**E2E adversarial pass**
- Happy path: `make harness-build; head -1 apps/harness/dist/cli.js; node apps/harness/dist/cli.js; echo $?` → `#!/usr/bin/env node`, `usage: tree-agent [flags] "<prompt>"`, exit 1 (PASS)
- Break 1 (fresh install, scratch rsync copy w/o node_modules/dist, empty `.env`): `make install` → `Done in 416ms using pnpm v10.34.8`, `cmp pnpm-lock.yaml` vs original → unchanged (PASS). Lockfile drift negative: editing `vitest` range in the scratch package.json → `pnpm install --frozen-lockfile` exit 1 ("dependencies are mismatched") (PASS, CI would fail loudly).
- Break 2 (line width, statement `usage(process.stderr, "tree-agent", { flags: "aaa…" }, 1);` padded to exact width, files restored byte-identical after, `cmp` verified): src 76 → `make format-check` 0; src 77 → 2 (output names `src/cli.ts`), `make format-fix` rewraps one-arg-per-line, recheck 0; tests 77 → 0; tests 100 → 0; tests 101 → 2 (PASS)
- Break 3 (type error): appended `const bad: number = "not a number";` to tests/cli.test.ts → `make harness-typecheck` exit 2, `tests/cli.test.ts(14,7): error TS2322`; restored → 0 (PASS)
- Break 4 (no bun on PATH): scratch dir with only `node`+`git` symlinks + `~/.local/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin`; `command -v bun` → exit 1 (note `/opt/homebrew/bin` holds both node and bun here, so it was excluded). In a fresh scratch copy: install, lint-fix, lint-check, format-fix, format-check, typecheck, tests, build, help, clean all exit 0; run → usage, recipe exit 1 (PASS)
- Break 5 (env-target, scratch copy of Makefile, human's .env* untouched): no `.env` → `Root .env file is missing. … fill in OPENROUTER_API_KEY.`; `.env.target=prod` + no `.env.prod` → same text with `.env.prod`; with `.env.prod` present `make -pn help | grep ^ENV_FILE` → `../../.env.prod`; real repo (target local) → `../../.env` (PASS)
- Break 6 (shell metacharacters in `PROMPT`): `make harness-run PROMPT='say "hi" `echo BT`'` → recipe expands to `node src/cli.ts  "say "hi"  `echo BT`"`: embedded `"` end the quoting and backticks are executed by the shell (see Other issues; non-blocking because the stub ignores its args and no AC covers it). `PROMPT="what's up"` is passed intact. (PASS with note)

**Acceptance criteria**
- [x] PASS — Install / Bun artefacts gone. Evidence: fresh-copy `make install` green and lockfile byte-identical; `git status` shows `bun.lock`, `bunfig.toml`, `src/index.ts`, `tests/unit/smoke.test.ts` deleted; `grep -nE '"jsx|bun-types|@types/bun|bun' apps/harness/pnpm-lock.yaml` → no match; tsconfig has no `jsx`/`bun-types`. 
      RULING on the literal `grep -ri "bun"`: it matches only `.github/workflows/ci.yml:40` and `:145` (`runs-on: ubuntu-latest`), both substring hits on "ubuntu" in the memory/harness job runners (the memory job line must stay byte-identical). AC judged by intent (no Bun toolchain reference). Check run: `grep -rni "bun" apps/harness/package.json apps/harness/tsconfig.json apps/harness/tsconfig.build.json apps/harness/Makefile apps/harness/biome.json apps/harness/README.md apps/harness/src apps/harness/tests .pre-commit-config.yaml .github/workflows/ci.yml .gitignore AGENTS.md | grep -vi ubuntu` → empty (exit 1). Intent met.
- [x] PASS — All gates + root aggregates green (see Test summary; `make tests` = 5218 memory + 1 harness).
- [x] PASS — Per-tree width enforced (Break 2).
- [x] PASS — Build and run. `make harness-build` → `dist/cli.js` only (`find apps/harness/dist -type f`), first line `#!/usr/bin/env node`, mode `-rwxr-xr-x`; `node dist/cli.js` → usage, exit 1; `node src/cli.ts` and `pnpm exec node src/cli.ts` (no build) → same usage, exit 1; `make harness-run` → same usage.
      RULING on exit code: the recipe's own exit is 1 (Node exit 1 shown as `make[1]: *** [run] Error 1`); GNU make always reports 2 for the outer `make`/`make -C` when any recipe fails (`make exit=2` observed). The AC's intent — the run prints the usage and the command signals failure with the CLI's exit 1 — is met; the wording "exits 1" applies to the CLI process, not to `make`. No change required.
- [x] PASS — Typecheck covers tests (Break 3); `find apps/harness/dist -type f` → only `dist/cli.js`.
- [x] PASS — ENV_TARGET switch and `$(error)` naming `OPENROUTER_API_KEY` (Break 5; Makefile:10).
- [x] PASS — CI harness job: `.github/workflows/ci.yml` lines 143-end; steps `pnpm/action-setup@v4`, `actions/setup-node@v4` (node 22, cache pnpm), `pnpm install --frozen-lockfile`, `pnpm exec biome check src tests`, `pnpm exec tsc --noEmit`, `pnpm exec vitest run`. `git diff HEAD -U0 .github/workflows/ci.yml` hunks all start at line ≥150 (harness job begins at 143) so the memory job is untouched; `grep -n OPENROUTER .github/workflows/ci.yml` → empty (exit 1). I ran the four CI commands verbatim in apps/harness: install ("Lockfile is up to date"), biome, tsc, vitest → all exit 0. The CI job itself was not run (needs a push).
- [x] PASS — pre-commit: `make pre-commit` (`pre-commit run --all-files`) → `biome check (harness)....Passed`; `.pre-commit-config.yaml` entry is `bash -c 'cd apps/harness && pnpm exec biome check --write src tests'`; no `bunx`. Working tree unchanged afterwards. Extra: prettier `exclude: ^apps/harness/pnpm-lock\.yaml$` is justified (lockfile is pnpm-owned, otherwise prettier rewrites it) and scoped to that file only.
- [x] PASS — `git diff -U0 AGENTS.md` hunks at lines 37, 55, 68-69 (cadence block, +2 lines), 71/73 (step 4) = the four expected hunks; `.gitignore` diff removes only `apps/harness/.bun/`; `.env.example:73: OPENROUTER_API_KEY=your-openrouter-api-key`.
- [x] PASS — No-bun PATH (Break 4).
- [x] PASS — `make harness-lint-fix && make harness-lint-check && make harness-typecheck && make harness-tests && make pre-commit` → all exit 0 (run individually above; lint-fix also exit 0 in the fresh copy).

**Evidence**
```
$ make harness-tests
 ✓ tests/cli.test.ts (1 test) 61ms
 Test Files  1 passed (1)   Tests  1 passed (1)
$ make tests   -> 5218 passed in 59.14s ; harness 1 passed ; exit 0
$ make pre-commit -> prettier/ruff check/ruff format/biome check (harness) Passed
$ node apps/harness/dist/cli.js; echo $?  -> usage: tree-agent [flags] "<prompt>" / 1
```

**Other issues found (non-blocking)**
- `apps/harness/Makefile:34` (`run`): `$(if $(PROMPT),"$(PROMPT)",)` splices the prompt into the shell command, so a prompt containing `"` is mangled and backticks/`$(...)` are executed by the shell. Suggested one-line fix (verified in a scratch Makefile; works because the Makefile `export`s everything): `node src/cli.ts $(ARGS) $(if $(PROMPT),"$$PROMPT",)`. Worth doing in 204 when the prompt starts to matter, or now for free.
- README/Makefile only say `corepack enable pnpm`; on this machine it needed `--install-directory ~/.local/bin` (already logged by SWE for 213).
- `OPENROUTER_API_KEY..` shows a double period in the `$(error)` text (trailing period of the sentence plus the quoted one); cosmetic.
- Repo-root `pnpm -v` (outside `apps/harness`) fails here because of a stale corepack cache for pnpm 12.15.0 from a different package.json/global config; inside `apps/harness` it resolves 10.34.8 correctly. Environment artefact, not a repo defect.

**VERDICT: PASS**
