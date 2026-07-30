# Harness tests

## Layout

```
tests/
  helpers/
    tmp-fs.ts              # mkdtemp + scoped cleanup
  unit/                    # fast, no external infra (no Gemini, no MongoDB, no MCP spawn)
    <area>/<file>.test.ts  # mirrors src/ structure
```

## Conventions

- **Files are `<name>.test.ts`** — `bun test` picks up `*.test.ts` / `*.spec.ts` automatically.
- **Mirror the src/ tree** under `tests/unit/`. When `src/foo/bar.ts` grows tests, they live at `tests/unit/foo/bar.test.ts`.
- **Arrange/Act/Assert** — same shape the memory app uses. No ceremony.
- **Parametrize via `test.each([...])("%s", (...) => {...})`** — the closest bun-test equivalent of `@pytest.mark.parametrize`.
- **No `conftest.py`.** Shared setup lives in `tests/helpers/*.ts` and is imported explicitly. Global preloads go in `bunfig.toml` `[test] preload` — reserved for heavier fixtures when the set grows.
- **Shared cleanup** uses `afterEach` with the helpers in `tmp-fs.ts`. No manual try/finally per test.

## Mocking

From `bun:test`:

```typescript
import { mock, spyOn } from "bun:test";

// Module-level stub:
mock.module("../../../src/session/resume", () => ({
  listSessions: () => [],
  findMostRecent: () => null,
  loadSession: () => ({ messages: [], sessionId: null }),
}));

// Call spy:
const spy = spyOn(console, "error").mockImplementation(() => {});
```

Two fakes under `tests/helpers/`:

- **`fake-mcp.ts`** — `makeFakeClient` / `makeFakeMcpServer`. Scripted `listTools` pages, `callTool` responses, capture of every call. Used by MCP unit tests; DI seam on `connectMcpServer(name, spawn, deps?)` wires in the fake `Client` without touching the SDK's module state.
- **`fake-gemini.ts`** — replaces the `@google/genai` `GoogleGenAI` client for loop + sub-agent tests.

Prefer dependency injection (`SlashActions.listSessions`, `McpConnectDeps`) over `mock.module`. Bun's module mocks are global and leak across test files — we hit it once already with slash + session-resume tests and had to refactor.

## Scope

Tests must pass without any external setup (no `make local-start`, no `GOOGLE_API_KEY`, no `ripgrep`). If a function needs a real subprocess (e.g. `runHook` shells out to `bash`), use `/bin/echo`, `/bin/true`, or similar POSIX-guaranteed commands so tests stay portable.

There is no integration suite (deleted deliberately — too slow for feedback loops); e2e verification happens by running the real harness.

## Running

```bash
make harness-tests               # everything — fast, no external infra

bun test --watch tests/unit/                         # TDD loop
bun test tests/unit/permissions/policy.test.ts       # single file
bun test --test-name-pattern "matches prefix"        # filter by name
```
