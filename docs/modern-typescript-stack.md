# The Modern TypeScript Stack: Bun + Biome + tsc

A decision-oriented reference for picking TypeScript tooling on small to medium projects. Captures the trade-offs behind choosing **Bun**, **Biome**, and (sometimes) **Turborepo** over the legacy Node + npm + ESLint + Prettier + Jest + webpack sprawl.

---

## Table of Contents

1. [TL;DR](#1-tldr)
2. [Bun](#2-bun)
3. [Biome](#3-biome)
4. [Turborepo](#4-turborepo)
5. [Bun vs uv — the Python Parallel](#5-bun-vs-uv--the-python-parallel)
6. [The Minimal Stack for Small to Medium Projects](#6-the-minimal-stack-for-small-to-medium-projects)
7. [Do I Still Need Node Installed?](#7-do-i-still-need-node-installed)
8. [When to Grow the Stack](#8-when-to-grow-the-stack)

---

## 1. TL;DR

For a new small-to-medium TypeScript project in 2026, three tools cover the core dev loop:

```bash
bun      # runtime, package manager, bundler, test runner, TS execution
biome    # linter + formatter
tsc      # type checker (via `bun tsc --noEmit`)
```

Two binaries, one config file each, no Node installation needed. This replaces the `node + npm + tsc + jest + esbuild + eslint + prettier` setup that was standard 2-3 years ago.

Skip Turborepo unless you have ~5+ packages in a monorepo or genuinely slow builds. Skip framework-specific bundlers unless you're using those frameworks.

Real-world evidence: both the Claude Code harness and the opencode harness are built on this stack. Claude Code uses Bun + Biome. opencode uses Bun + Turborepo across ~20 packages.

---

## 2. Bun

**What it is:** an all-in-one JavaScript/TypeScript runtime and toolchain, written in Zig. Uses JavaScriptCore (Safari's engine) instead of V8. First stable release in 2023.

**What it replaces — all at once:**

| Legacy tool | Bun equivalent |
|---|---|
| `node` (runtime) | `bun run` |
| `npm` / `yarn` / `pnpm` | `bun install` |
| `tsc` (TS execution) | native — just `bun src/index.ts` |
| `jest` / `vitest` | `bun test` |
| `webpack` / `esbuild` | `bun build` |
| `ts-node` / `tsx` | not needed |

**Why projects like Claude Code and opencode adopted it:**

- **Native TypeScript execution** — no build step during development. `bun run src/index.ts` just works.
- **Fast installs** — ~10-25× faster than npm. Matters for CLI tools users install fresh.
- **Fast startup** — 2-4× faster than Node. Matters for CLIs invoked per command.
- **Built-in APIs** — `Bun.file()`, `Bun.serve()`, `Bun.$` (shell), SQLite, WebSocket. Fewer dependencies.
- **Node.js compatibility** — most npm packages work unchanged; uses the same registry.

**Trade-offs:**

- Younger ecosystem — occasional compatibility gaps with Node-specific packages, especially native modules.
- Production maturity improving quickly but Node is still the safer default for large-scale servers.
- Some native modules (`node-gyp` territory) can be rough. opencode ships a `fix-node-pty` postinstall script specifically for this.

---

## 3. Biome

**What it is:** an all-in-one linter and formatter for JS/TS, written in Rust. The successor to the Rome project. Config in `biome.json`.

**What it replaces:**

| Legacy tool | Biome equivalent |
|---|---|
| ESLint | `biome lint` |
| Prettier | `biome format` |

Two tools → one binary → one config. Typically 10-100× faster than ESLint + Prettier.

**What Biome does NOT do — the gap:**

Biome does not do **type checking**. It understands syntax and style, not types across files. You still need `tsc --noEmit` (or `bun tsc --noEmit`) for that. This is the single most common mistake when adopting Biome: assuming it replaces the TypeScript compiler. It does not.

**When to keep ESLint/Prettier instead:** if you rely on ESLint plugins that have no Biome equivalent (some React/framework-specific rules lag behind). Check the Biome rules page before committing.

---

## 4. Turborepo

**What it is:** a build system for JS/TS monorepos, written in Rust (by Vercel). Coordinates tasks (`build`, `test`, `typecheck`) across many packages with smart caching and parallel execution. Config in `turbo.json`.

**When it earns its keep:**

- Monorepo with **5+ packages** that depend on each other
- Tasks slow enough that caching saves real time (builds over ~10s)
- CI runs that benefit from remote caching across teammates/branches
- Team large enough that "don't rebuild packages I didn't touch" matters

**When to skip it:**

- Single-package repo — no task graph to orchestrate
- 2-3 packages with fast builds — plain scripts are fine
- You can't articulate a specific task that's slow today

**Lighter alternatives** if you outgrow plain scripts but aren't at Turborepo scale:

- Bun workspaces + `bun run --filter` (built-in, zero config)
- npm/pnpm workspaces with simple scripts

**Rule of thumb:** Turborepo is additive, not invasive. You can always adopt it later. Premature adoption costs mental overhead and a dependency for no real gain.

Real-world data points: opencode (20+ packages, desktop + web + CLI + SDK) → Turborepo clearly pays off. Claude Code (single-package) → no Turborepo needed.

---

## 5. Bun vs uv — the Python Parallel

People coming from Python often ask if Bun is "the Node equivalent of uv." **Spiritually yes, mechanically no** — the scopes differ significantly.

**What they share:**

- Both written in systems languages (uv in Rust, Bun in Zig) for speed
- Both are 10-100× faster than what they replace
- Both drop-in compatible with existing ecosystems (PyPI / npm)
- Both becoming the new default for greenfield projects

**Where they differ — the key table:**

| | **uv** | **Bun** |
|---|---|---|
| Package manager | ✅ replaces pip/poetry/pipenv | ✅ replaces npm/yarn/pnpm |
| Virtual env / version manager | ✅ replaces venv/pyenv | ❌ N/A (JS has no venv concept) |
| Runtime | ❌ still uses CPython | ✅ replaces Node.js itself |
| Bundler | ❌ N/A | ✅ replaces webpack/esbuild |
| Test runner | ❌ still uses pytest | ✅ replaces Jest/Vitest |
| Transpiler | ❌ N/A | ✅ executes TS directly |

**Mental model:**

- **uv** = `pip` + `poetry` + `venv` + `pyenv` in one fast binary. It manages *dependencies and environments* but hands off to CPython to run your code.
- **Bun** = `npm` + `node` + `tsc` + `jest` + `esbuild` in one fast binary. It *is the runtime* itself.

**Practical consequence:** uv doesn't change how your Python code runs. Bun *does* change how your code runs (different engine, different APIs), which is why Bun has compatibility caveats that uv doesn't. A true Python parallel to Bun would be "uv + a new Rust-based Python interpreter + pytest + a bundler" — no such thing has critical mass.

---

## 6. The Minimal Stack for Small to Medium Projects

```
bun      → run, install, test, bundle, TS execution, workspaces
biome    → lint + format
tsc      → typecheck (invoked as `bun tsc --noEmit`)
```

**Coverage breakdown:**

| Concern | Tool | Notes |
|---|---|---|
| Run TS files | Bun | `bun src/index.ts` — no compile step |
| Install deps | Bun | `bun install`, uses `bun.lock` |
| Run tests | Bun | `bun test`, Jest-compatible API |
| Bundle for prod | Bun | `bun build --target=bun` or `--target=browser` |
| Lint | Biome | `biome lint` |
| Format | Biome | `biome format --write` |
| **Type check** | **tsc** | **Biome cannot typecheck — this is the gap** |
| Workspaces (monorepo) | Bun | Built-in, good for 2-5 packages |

**What you still pull in as libraries (not tooling):**

- TypeScript itself — Bun executes `.ts`, but `tsc` is the authoritative type checker.
- A framework if building something specific: Hono / Elysia (servers), React / Vue / Svelte (UI), Next.js / Astro (full-stack), Electron / Tauri (desktop).
- Validation: Zod or Valibot for runtime schemas.
- Git hooks: husky or lefthook, if you want pre-commit automation.

---

## 7. Do I Still Need Node Installed?

**Short answer: no.** Bun ships its own JavaScript engine and is fully standalone:

```bash
curl -fsSL https://bun.sh/install | bash
# or: brew install bun
```

That's it. `bun run`, `bun install`, `bun test` all work without Node anywhere on the system.

**Caveats where Node might sneak back in:**

1. **Native modules using `node-gyp`.** Some npm packages compile C++ bindings against Node's headers at install time. Bun handles most now, but edge cases exist. opencode's `fix-node-pty` postinstall script is exactly this — patching `node-pty` to work under Bun.
2. **Tools that shell out to `node` explicitly.** Rare, but some older CLIs hardcode `#!/usr/bin/env node` or spawn `node` subprocesses. Bun provides a `node` symlink in some install paths, but not always.
3. **Deployment targets that only run Node.** AWS Lambda's default runtime, some serverless providers. Bun now has first-class support on Vercel, Cloudflare, Railway, Fly, and runs fine in Docker (`oven/bun` image, ~80MB self-contained).
4. **Team compatibility.** If teammates use Node, your `package.json` scripts should work under both. They usually do — but `Bun.*` APIs only run under Bun.

**Greenfield project, you control the whole stack:** Bun alone is sufficient on dev machines and in production containers.

**Migrating an existing Node project:** keep Node installed during transition as a hedge, but it's not a runtime requirement of Bun itself.

---

## 8. When to Grow the Stack

The Bun + Biome + tsc core covers most projects for a long time. Signs you need more:

| Signal | Add |
|---|---|
| Monorepo past ~5-10 packages with slow builds | Turborepo (or Nx) |
| Need Vite's HMR story for a specific framework | Vite alongside Bun |
| Building a Next.js / Astro / SvelteKit app | Framework's own toolchain (already integrated with Bun) |
| Deploying to Cloudflare Workers / edge-only | Wrangler / framework adapter |
| Serious type checking in CI across many packages | Turborepo's `typecheck` task with caching |
| Need runtime schema validation | Zod or Valibot |
| Pre-commit automation | husky or lefthook |

**Anti-pattern:** adopting Turborepo or adding tooling "because big projects use it." Big projects have problems small projects don't. Match tooling to pain, not aspiration.
