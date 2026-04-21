# Harness tests

## Layout

```
tests/
  helpers/
    tmp-fs.ts              # mkdtemp + scoped cleanup
  unit/                    # fast, no external infra (no Gemini, no MongoDB, no MCP spawn)
    <area>/<file>.test.ts  # mirrors src/ structure
  integration/             # slower; require make local-start etc.
    *.test.ts
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

Prefer **dependency injection** over module mocks when the call site allows — the client/tools/loop all take their deps as arguments for exactly this reason.

## Unit vs integration

Unit tests must pass without any external setup (no `make local-start`, no `GOOGLE_API_KEY`, no `ripgrep`). If a function needs a real subprocess (e.g. `runHook` shells out to `bash`), use `/bin/echo`, `/bin/true`, or similar POSIX-guaranteed commands so tests stay portable.

Integration tests live under `tests/integration/` and run via `make harness-integration-tests`. They may spawn the CLI, connect to a running `tree-memory` MCP server, exercise the real Gemini API, etc. CI should default to unit tests only.

## Running

```bash
make harness-unit-tests          # fast — default during development
make harness-integration-tests   # slow, needs infra
make harness-test                # everything

bun test --watch tests/unit/                         # TDD loop
bun test tests/unit/permissions/policy.test.ts       # single file
bun test --test-name-pattern "matches prefix"        # filter by name
```
