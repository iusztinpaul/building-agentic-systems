---
id: 210-harness-codemode-sandbox-bridge
status: done
feature: tree-agent-harness
---

# M6a — the QuickJS codemode sandbox, the `tools.*` / `searchTools` / `describeTool` bridge through the same permission gate, the `codemode` tool with budgeted inline declarations, and the sandbox limits

Tags: `harness`, `codemode`, `sandbox`, `enhancement`
Depends on: 209 (codemode-exposed tools, the BM25 index), 206 (the gate)
Blocks: 211
Implements: ADR-016 §9 (QuickJS sandbox; host promises + job pumping), §5 (one gate for model- and script-originated calls)

## Scope

1. **`src/tools/codemode.ts`** — spec §10.3: `codemode(script, description)`, `direct`, NOT read-only,
   registered only when ≥ 1 `codemode`-exposed tool exists; the provider description is the §10.3 text
   WITHOUT the `llm(...)` sentence (211 adds it) followed by inline declarations
   `name(params: <TS-ish type from schema>): <first description line>` for the `codemode` set in registry
   order until `codemode.inlineBudgetTokens` (3 000, `chars / 4`); the rest is discoverable via `searchTools`.
2. **`src/codemode/sandbox.ts`** — `quickjs-emscripten` 0.32.0 (verified to import and evaluate): one runtime
   + context per call, disposed after; `memoryMb` 256 (`setMemoryLimit`), `timeoutMs` 60 000 (interrupt
   handler on a deadline + a host-side timer that disposes the runtime); no `require`, `fetch`, `fs`, or
   timers other than `setTimeout`; `console.log` captured; the script runs as an async function body (ES2020,
   `return` allowed); output = captured logs + `JSON.stringify(returnValue)`, `outputMaxBytes` 20 000
   middle-truncated with the full text at `~/.tree-agent/tmp/`. Host functions are promise-based — the host
   returns a QuickJS promise and pumps `executePendingJobs()` whenever a host promise settles — so
   `await Promise.all([...])` fans out; a semaphore caps in-flight host calls at `codemode.concurrency` (4).
   (Asyncify suspends the whole VM and cannot fan out; do not use it for the bridge.)
3. **`src/codemode/bridge.ts`** — `tools.<name>(args)` for every `codemode` ∪ `direct` ∪ activated tool →
   a `ToolCall` with `origin: "codemode"` → the SAME `beforeToolCall` gate (206) → `registry.execute` →
   resolves with `structured ?? content`; `maxToolCalls` 50 per script; `deny` rejects the promise with the
   reason (the script can `catch`); `ask` pauses the sandbox on the host promise until the user answers;
   headless `deny` aborts the sandbox AND the run (`agent_end(blocked)`, exit 2) and cannot be caught.
   `searchTools(query)` = BM25 over the `codemode` set, activates nothing; `describeTool(name)` returns
   `{name, description, parameters}`.
4. **TUI** — the codemode tool card shows `description` and the collapsed script; the permission prompt (206)
   shows `description` + the first 2 KB of the script for `codemode` itself and the usual primary argument for
   each `tools.x()` call inside.
5. Settings: `codemode.timeoutMs`, `memoryMb`, `maxToolCalls`, `outputMaxBytes`, `concurrency`,
   `inlineBudgetTokens` (spec §10.4 / §16).

**Out of scope (intentional):** `llm()` (211 — not defined in the sandbox yet; a script calling it gets a
`ReferenceError` the test pins); `store()`/`load()` persistence, `codemode.mode: "only"` (pi features not in
the spec); nested `codemode` from a script (spec: never).

## Acceptance Criteria

- [x] A script calling two fake tools and logging twice returns exactly the two log lines + the JSON return
      value — the tool results themselves never appear in the tool message; `structured` is what the script
      receives (a fake tool with `structured: {n: 1}` → `(await tools.fake()).n === 1`).
- [x] `tools.x()` goes through the gate: with a FakePrompter answering `deny`, the promise rejects with the
      reason and a `try/catch` in the script continues; with `ask`, the sandbox waits (the FakePrompter resolves
      after 100 ms) and the call then runs; in headless mode a `deny` ends the run with `agent_end(blocked)` and
      exit 2 even when the script wraps the call in `try/catch`; a read-only tool inside a script never prompts.
- [x] `await Promise.all(8 calls)` on a fake tool that records overlap shows ≤ 4 in flight and all 8 complete;
      the 51st `tools.x()` in one script rejects naming `maxToolCalls`.
- [x] `while (true) {}` with `timeoutMs` 200 → an `isError` result naming the timeout within 1 s and the
      runtime is disposed (no leak: a second call runs normally); an allocation loop hits `memoryMb` → an error
      result, never a process crash; `typeof fetch === "undefined"`, `typeof require === "undefined"`,
      `typeof llm === "undefined"` inside the sandbox (this task); `setTimeout` works, `setInterval` does not.
- [x] A 100 KB output is middle-truncated to 20 000 bytes with the tmp path; the `codemode` tool is absent
      with zero codemode tools and present with one; inline declarations stop at the 3 000-token budget and
      `searchTools` finds a declaration past it; `describeTool` returns the JSON Schema of a codemode tool.
- [x] With the committed `.tree-agent/mcp.json` the direct set is `read`, `bash`, `edit`, `write`,
      `mcp__tree_memory_local__search_memory`, `tool_search`, `codemode` (M5's "4 + codemode" count).
- [ ] [HUMAN] `tree-agent --json "Use codemode: read package.json under apps/harness and apps/memory/pyproject.toml
      and return both project names"` runs ONE `codemode` call whose result is the two names; the TUI shows the
      script collapsed under the card.
- [ ] [HUMAN] (`make local-start`, live key, `BRIGHTDATA_API_KEY` + `BRIGHTDATA_UNLOCKER_ZONE` in `.env` — else
      substitute `search_memory` with a limit of 3) `tree-agent --trust "Write a codemode script that calls
      scrape_web on https://example.com and returns the page title"` — `scrape_web` is codemode-only, the script
      reaches it, the answer is "Example Domain"; in the TUI `ask` mode the user is asked for `codemode` and
      then for the `scrape_web` call inside it.
- [x] `make harness-lint-fix && make harness-lint-check && make harness-typecheck && make harness-tests &&
      make pre-commit` green.

## User Stories

### Story: The model batches many small calls into one script
1. The user asks for the titles of the five most recent memory documents.
2. The model writes one script: `search_memory`, then a loop over hits; only the final list reaches the
   transcript; one tool card, one permission prompt for `codemode`.

### Story: A script hits a denied tool and recovers
1. Inside a script `tools.bash({command: "rm -rf build"})` is denied by the user.
2. The promise rejects with "denied by user"; the script's `catch` logs it and returns the partial result;
   the model explains what was skipped.

### Story: Headless, a script tries something dangerous
1. In CI (`auto`), a script calls `tools.write` on `~/.ssh/config`; the classifier flags it; no prompter.
2. The sandbox is aborted, the run ends `blocked`, exit 2; the JSONL has the `permission` entry.

### Story: A runaway script
1. The model writes an infinite loop by mistake.
2. After `timeoutMs` the tool result says the script timed out; the next turn works normally.

### Story: Discovering a tool past the inline budget
1. The server has 60 codemode tools; only the first 40 fit the declaration budget.
2. The script calls `searchTools("embeddings map")`, finds `visualize_memory_embeddings`, calls it.

---

Blocked by: 209

## Log

### [PA] 2026-10-24 20:26 — Grooming

**Summary**
A per-call QuickJS sandbox with a hand-written bridge: every `tools.x()` is a real `ToolCall` through the
one permission gate, host calls are promise-based so scripts fan out under a concurrency cap, and the limits
(time, memory, call count, output size) are enforced by the host.

**Key decisions**
- Promise + `executePendingJobs` pumping over Asyncify: Asyncify suspends the whole VM, which would serialise
  `Promise.all` and break the spec's fan-out; the API choice is left to the SWE, the observable concurrency
  is pinned by a test.
- `llm()` is deliberately undefined here so 211's tests can prove its arrival; the `codemode` description
  gains the `llm` sentence in 211.
- The headless deny is uncatchable by design (spec §10.5): CI never degrades silently.

**Dependencies**
- 209 — codemode-exposed tools and the BM25 index; 206 — the gate.

**User stories**
- 5 stories covering: batching, a denied call inside a script, headless abort, a runaway script, discovery
  past the budget.

Ready for implementation.

### [SWE] 2026-10-25 04:05 — Implementation

**Bindings (what 211 builds on)**
- `src/codemode/sandbox.ts` — `runSandbox({script, prelude?, async?, sync?, signal?, timeoutMs, memoryMb, concurrency}) → {logs, value? (JSON), error?, aborted?}`. One wasm module + runtime + context per call (`newQuickJSWASMModule(newVariant(RELEASE_SYNC, {wasmMemory}))`, sync variant, no Asyncify). Host functions take/return strings; an async one returns a `context.newPromise()` handle, starts after the VM yields, runs under a `concurrency` semaphore, and every settle pumps `runtime.executePendingJobs()` then checks `getPromiseState` of the script promise. `HostCall = {signal, hold()}`: `signal` aborts when the script ends; `hold()` stops the deadline until released. Script = `(async () => {<script>})().then(JSON.stringify)`; `console.log/info/warn/error` (a JS prelude formats, one string crosses); `setTimeout`/`clearTimeout` native; `__host` deleted after the preludes.
- `src/codemode/bridge.ts` — `createBridge({registry, gate, ctx, callId, maxToolCalls, exclude}) → {prelude, async: {call}, sync: {searchTools, describeTool}}`. `tools` = `registry.scriptable()` (codemode ∪ direct ∪ activated, registration order) minus `codemode`; each call → `{id: "<callId>.<n>", name, arguments, origin: "codemode"}` → `gate` (with `hold()`) → `registry.execute(…, {origin: "codemode", signal: host.signal})` → `structured ?? content`; deny → `Error("Blocked: <reason>")`; `isError` result → `Error(content)`; 51st → `tool call limit reached: 50 per script (codemode.maxToolCalls)`. `searchTools(query, limit = 5)` = `rankTools` (the 209 BM25) over `list("codemode")`, activates nothing; `describeTool(name)` = `{name, description, parameters: parametersOf(tool)}`.
- `src/tools/codemode.ts` — `CODEMODE`, `CODEMODE_PREAMBLE` (spec §10.3 minus the `llm` sentence — 211 inserts it there), `codemodeTool`, `registerCodemode` (only with ≥ 1 codemode tool), `declarations()` (budget `inlineBudgetTokens × 4` chars, then `(N more; find them with searchTools(query))`), `tsType()`. `description` is a getter, so it follows the registry.
- `ToolCall.origin?: "codemode"`; `ToolRegistry.scriptable()`; `parametersOf(tool)` exported (schemasForRequest uses it).
- Session: `SessionOptions.codemode?: CodemodeLimits & {tmpDir}` → `AgentSession.create` registers codemode with `gate = session.#gate`. `#gate` looks up `scriptable()` for `origin: "codemode"`, `declared()` otherwise; nothing else in the gate changed. The headless deny path is the existing one: `#gate` aborts the run with `BLOCKED`, the sandbox's abort listener tears the VM down before the rejection is delivered (so no `catch` runs), the tool returns `isError` "the script was stopped: a tool call was blocked", the loop ends `blocked`, the CLI exits 2. `agent-loop.ts` untouched (180 lines).
- Prompt FIFO: `PromptQueue` in `permissions/gate.ts`, `GateDeps.prompts?`; the session passes one. Only the prompt step is queued (classifier calls overlap); a call that waited re-checks `sessionAllow` (a `session` answer covers the queue) and `signal.throwIfAborted()` (no dialog for a stopped run).
- Settings: `codemode.memoryMb` (256) and `codemode.outputMaxBytes` (20 000) added; the codemode numbers are now `int().positive()` (`inlineBudgetTokens` `nonnegative`) — `concurrency: 0` would hang.
- TUI: the codemode card head is `description`, the script sits under it collapsed (first line + `(+N lines, Tab)`), expanded in full with the result; the permission prompt title for a script's call reads `Allow write (in a codemode script)?`. The `codemode` summary (description + first 2 KB) is 206's, unchanged.
- `quickjs-emscripten` pinned `0.32.0` (exact) in `dependencies`; `pnpm install --frozen-lockfile` → "Already up to date". Imports under Node type-stripping (`node src/cli.ts`) and from `dist/` after `make build`; no tsconfig.build or Biome ignore needed (the wasm stays in `node_modules`).

**Sources**
- context7 is not connected in this session; per the `tech-docs` fallback I used the installed package: `node_modules/quickjs-emscripten/README.md` (§Runtime: `setMemoryLimit`, `setMaxStackSize`, `setInterruptHandler`; §Promises: `newPromise`, `executePendingJobs` after every settle, `getPromiseState`, `resolvePromise` deadlocks; §Asyncify; §Variants: `newQuickJSWASMModule` vs `newQuickJSAsyncWASMModule`, `RELEASE_SYNC`, `newVariant`/`wasmMemory`; §Memory Management / Scope / `consume`), `quickjs-emscripten-core/dist/index.d.ts` (`QuickJSDeferredPromise`, `newPromise` overloads, `executePendingJobs` result, `hasPendingJob`, `getWasmMemory`, the `setInterval`/`dup()` note), and the compiled core chunk for two ownership facts the docs leave out: a handle returned from a `newFunction` callback is dup'd into the VM and disposed by the library, and `deferred.resolve(h)` does not take ownership of `h`.

**Probes that shaped the design (scratch scripts outside the tree)**
- `setMemoryLimit` under-counts realloc-heavy patterns in this build: `while (true) a.push(new Array(10000).fill(1))` with a 16 MB limit grew the wasm heap to 2 147 MB (RSS 2.2 GB) before OOM. A per-call `WebAssembly.Memory({initial: 256 pages, maximum: 256 pages + memoryMb})` is the hard cap: same loop → clean `out of memory` at 66 MB, the context still evaluates, dispose succeeds. Single big allocations (`"x".repeat(100 MB)`, `new ArrayBuffer(200 MB)`) were already caught by `setMemoryLimit`.
- No stack limit by default: `function f(){return f()+1}` overflowed the host stack (process crash when uncaught). `setMaxStackSize(256 KB)` → `InternalError: stack overflow`; deep `JSON.parse("[".repeat(200000)…)` still overflows the host stack as a catchable `RangeError` that leaves the module unusable; 128 KB corrupted the runtime (`dispose` aborted on `gc_obj_list`). Hence a fresh module per call (~2 ms measured) and every VM entry + dispose in try/catch: a crash is an error result and the module is dropped.
- The interrupt handler fired 9 times in 1.6 s of native-heavy work, so the host-side timer (re-armed after each hold) is what bounds an idle or native-bound script; the handler bounds JS loops (`while (true)` stops at 200–205 ms).

**Files modified**
- `apps/harness/package.json`, `apps/harness/pnpm-lock.yaml` — `quickjs-emscripten@0.32.0`.
- `apps/harness/src/codemode/sandbox.ts` (new), `src/codemode/bridge.ts` (new), `src/tools/codemode.ts` (new) — above.
- `apps/harness/src/types.ts` — `ToolCall.origin?`.
- `apps/harness/src/tools/registry.ts` — `scriptable()`, exported `parametersOf()`.
- `apps/harness/src/permissions/gate.ts` — `PromptQueue`, `GateDeps.prompts?`.
- `apps/harness/src/session.ts` — `codemode` option, registration with its own gate, origin-aware lookup, the queue.
- `apps/harness/src/cli.ts` — passes `{...settings.codemode, tmpDir}`.
- `apps/harness/src/config/settings.ts` — `memoryMb`, `outputMaxBytes`, int/positive.
- `apps/harness/src/tui/tool-card.ts`, `src/tui/permission-prompt.ts` — card + title.
- Tests: `tests/codemode/sandbox.test.ts` (new, 21), `tests/tools/codemode.test.ts` (new, 23), additions in `tests/session.test.ts` (+10), `tests/cli.test.ts` (+1, 1 updated: the default-exposure test now expects `codemode`), `tests/permissions/gate.test.ts` (+2), `tests/tui/tool-card.test.ts` (+1), `tests/tui/permission-prompt.test.ts` (+1), `tests/config/settings.test.ts` (+1, defaults updated).

**Tests**
- Unit: 755 passing, 0 failing, 43 files (was 696 / 41) — `make harness-tests`; vitest exits cleanly (a test pins no `Timeout` left after a timed-out script with two pending `setTimeout`s).
- Integration: N/A (the harness has no integration suite).
- Mutation checks: removing the sandbox's abort listener fails 3 tests (incl. the headless try/catch one); removing the session's `PromptQueue` fails 2.

**Acceptance criteria**
- [x] Two fake tools + two logs → exactly the logs + JSON, results stay in the script; `structured {n: 1}` → `.n === 1` — `tests/tools/codemode.test.ts` "returns exactly the logs and the JSON return value…", "a tool without structured gives the script its content".
- [x] Gate — deny rejects with the reason and `catch` goes on: `tests/session.test.ts` "ask: prompts for codemode, then for a write inside it…" (+ `codemode.test.ts` "a deny rejects…"); ask waits (FakePrompter answers after 100 ms, `timeoutMs` 50 to prove the hold) then runs: "ask: the script waits on the prompt…"; headless deny ends `agent_end(blocked)` despite `try/catch` (no `caught`, the write after it never runs, JSONL `permission` entry with `origin: "codemode"`, `source: "headless"`): "headless: a dangerous call inside a script ends the run blocked…" and exit 2 via the real CLI: `tests/cli.test.ts` "headless: a dangerous tools.x() inside a codemode script exits 2 even when the script catches"; read-only never prompts: "a read-only tool inside a script never prompts". Prompt FIFO: "parallel calls get one dialog at a time…" (peak 1, order a, b, c), "a `session` answer covers the parallel calls queued behind it", `gate.test.ts` "shows one dialog at a time… while classifier calls overlap" (classifier peak 3), "a queued prompt of an aborted run is never shown".
- [x] 8 parallel → peak exactly 4, all 8 complete — `codemode.test.ts` "8 parallel calls…", `sandbox.test.ts` "fans Promise.all out…"; 51st rejects naming `maxToolCalls` — "the 51st tools.x()…".
- [x] `while (true) {}` with `timeoutMs` 200 → isError naming the timeout in < 1 s, `QuickJSRuntime.prototype.dispose` called and `alive === false`, next call runs — `sandbox.test.ts` "stops while (true) {} at timeoutMs within 1 s…", `codemode.test.ts` "a timeout is an isError result…"; also the idle-await host timer, try/catch cannot swallow it, the timed-out script's tool signal is aborted. Memory loop (`memoryMb` 16) → `InternalError: out of memory (codemode.memoryMb = 16)`, next run works — "an allocation loop hits memoryMb…", "a memory blow-up is an error result…"; deep recursion / deep JSON don't kill the worker. `typeof fetch/require/llm/setInterval/process === "undefined"`, `setTimeout` works, `setInterval(...)` → `ReferenceError`, `await llm("hi")` → `ReferenceError: 'llm' is not defined` (pinned for 211) — "exposes no fetch…", "calling setInterval is a ReferenceError", "llm() is not defined yet (task 211)…".
- [x] 100 KB → 10 000 B head + 10 000 B tail around `[... 80008 bytes omitted ...]` + `Full output: <tmpDir>/<uuid>.txt` holding all 100 008 B — "middle-truncates a 100 KB output…"; absent with zero codemode tools, present with one — "registers nothing with zero codemode tools…"; declarations stop under 12 000 chars with 60 tools and `searchTools("embeddings map")` returns the 60th first — "inline declarations stop at the 3 000-token budget…"; `describeTool` JSON Schema (MCP passthrough and zod-derived) — "describeTool returns name, description and the JSON Schema…", "describeTool turns a built-in's zod parameters…".
- [x] Committed `.tree-agent/mcp.json` → exactly `read, bash, edit, write, mcp__tree_memory_local__search_memory, tool_search, codemode` — `tests/session.test.ts` "with the committed .tree-agent/mcp.json the direct set is exactly…" (reads the committed file's `toolExposure`).
- [ ] [HUMAN] AC 1 — not ticked; live run below: ONE `codemode` call, two `read`s inside (`source: readOnly`), final answer `tree-agent` / `tree-memory`. But the script returned the raw `{path, lines}` and the model pulled the names out (the AC wants the *result* to be the names), and the "script collapsed under the card" half is TUI-only. Human to judge.
- [ ] [HUMAN] AC 2 — headless half run from a scratch cwd (user-level `mcp.json` pointing at `tree-memory-local`; `BRIGHTDATA_*` present in `.env`, checked by count only): `scrape_web` is codemode-only (not in the tools array), the script reached it, final answer "Example Domain" (model took 2 codemode calls: one to inspect the result shape, one returning `"Example Domain"`). The TUI `ask` double prompt (codemode, then `scrape_web`) needs a human.
- [x] Cadence green.

**Evidence**
```
$ make harness-lint-fix && make harness-lint-check && make harness-typecheck && make harness-tests && make pre-commit
Checked 96 files in 60ms. No fixes applied.
Checked 96 files in 39ms. No fixes applied.
pnpm exec tsc --noEmit
 Test Files  43 passed (43)
      Tests  755 passed (755)
biome check (harness)....................................................Passed   (prettier, ruff check, ruff format Passed)

# HUMAN AC 1, from the repo root: TREE_AGENT_HOME=<scratch>/home1 make -C apps/harness run ARGS='--cwd ../.. --trust --json' PROMPT='Use codemode: read package.json under apps/harness and apps/memory/pyproject.toml and return both project names'   → exit 0, 1 tool_call
{"type":"tool_call","call":{"id":"toolu_01HYnW…","name":"codemode","arguments":{"script":"const a = await tools.read({path: \"apps/harness/package.json\"});\nconst b = await tools.read({path: \"apps/memory/pyproject.toml\"});\nreturn {a: …, b: …};","description":"Read harness package.json and memory pyproject.toml"}}}
{"type":"permission","call":{"id":"codemode-cc5f6208.1","name":"read","arguments":{"path":"apps/harness/package.json"},"origin":"codemode"},"decision":"allow","source":"readOnly"}
{"type":"permission","call":{"id":"codemode-cc5f6208.2","name":"read","arguments":{"path":"apps/memory/pyproject.toml"},"origin":"codemode"},"decision":"allow","source":"readOnly"}
{"type":"turn_end","message":{"role":"assistant","content":"- `apps/harness/package.json`: `tree-agent`\n- `apps/memory/pyproject.toml`: `tree-memory`",…}}
{"type":"agent_end","reason":"completed"}

# HUMAN AC 2 (headless half): TREE_AGENT_HOME=<scratch>/home2 make -C apps/harness run ARGS="--cwd <scratch>/scratch2 --trust --json" PROMPT='Write a codemode script that calls scrape_web on https://example.com and returns the page title'   → exit 0
{"type":"permission","call":{"id":"codemode-6bda634e.1","name":"mcp__tree_memory_local__scrape_web","arguments":{"urls":["https://example.com"],"data_format":"html"},"origin":"codemode"},"decision":"allow","source":"classifier",…}
{"type":"tool_result","callId":"toolu_011spw…","content":"\"Example Domain\"","isError":false}
{"type":"agent_end","reason":"completed"}
```

**Notes**
- Deliberate choices (intent, not slop): `console.info/warn/error` alias `log` (spec lists `log` only; models write `console.error` in catches); `clearTimeout` exists (ignores bad ids, like Node); the codemode `promptSnippet` says every tool above is `tools.<name>` inside a script — the first live run spent a probe call finding that out, the second run took one call; the provider description itself is the spec text verbatim. The semaphore wraps gate + execute, so classifier calls are bounded by `concurrency` too. The deadline is held during the whole gate call (prompt or classifier) — a 60 s timeout would otherwise kill a script while the user reads its dialogs — but one synchronous VM slice can never outlast `timeoutMs` even while held (a busy loop would block the event loop the prompt needs; pinned by "still stops a busy loop while another call holds the deadline"). An error thrown in a `setTimeout` callback is logged as `Uncaught …` and the script goes on. Captured console output is capped at 10 MB (`LOGS_CUT` line); the shown output is then cut to `outputMaxBytes` with the full text in tmp. A script stuck on a promise nothing can settle ends at once ("the script awaits a promise that can never settle") instead of waiting for `timeoutMs`.
- Known limitation (follow-up candidate, not done): the gate only sees the run's signal, so prompts already queued for a script that then timed out are still shown, one after another, once the user answers the current one. Needs a per-call signal through `beforeToolCall`.
- Observation for the memory app (not this task): `scrape_web`'s `structuredContent` arrives as `{result: "<json string>"}` (FastMCP wrapping), so scripts must `JSON.parse(res.result)`; the live model did.
- `session.ts` grows to 541 lines (+15); `sandbox.ts` is 488 lines with the comments the book listing needs.

### [Tester] 2026-10-25 03:20 — QA

**Test summary**
- Format / lint / pre-commit / typecheck: PASS (`make harness-lint-fix && make harness-lint-check && make harness-typecheck && make harness-tests && make pre-commit`, run twice, exit 0)
- Unit tests: 755 passed / 0 failed (43 files); integration: N/A (none in the harness)
- Warnings: 0 vitest warnings, no open-handle message, vitest exits cleanly, no leftover processes

**E2E adversarial pass** (scratch scripts and a throw-away `tests/qa210.test.ts`, both removed; scratch under `.../scratchpad/qa-210/`)
- Happy path (live, `TREE_AGENT_HOME` scratch, repo cwd, `--trust --json`): codemode script with two `read`s runs in 1 call, both reads `source: readOnly`; sandbox listing live: `Object.keys(tools)` = read, bash, edit, write, 5 codemode-only `mcp__tree_memory_local__*`, `tool_search`; `typeof process/fetch/llm/require` all `undefined`; `describeTool(search_memory).parameters.required` = `["query"]` (PASS)
- Break 1 sandbox escapes (45 probes, `escapes.ts`): `process`, `fetch`, `require`, `module/exports/Buffer`, `import('node:fs')` ("could not load module"), `new Function("return this")().process`, `this.constructor.constructor("return process")()`, `eval("process")`, `console.log.constructor(...)`, `setTimeout.constructor(...)`, `llm`, `setInterval`, `queueMicrotask`, `Atomics`, `WebAssembly`, `TextDecoder/Encoder`, `structuredClone/URL/performance`, `__host/host` all undefined or throw "'process' is not defined". `SharedArrayBuffer` is defined (in-VM only, harmless); `Date.now()` works; hidden tool, `tools["__proto__"]`, `tools.codemode`, `describeTool("codemode"|"hid"|"constructor")` unreachable ("not a function" / "unknown tool"); `Object.prototype.polluted = 1` in the script leaves the host `({}).polluted === undefined`; `tools.x.call({evil:1})` just runs `x`; `tools` is frozen; non-object args rejected (PASS)
- Break 2 timeouts (`timeouts.ts`): `while(true){}` 200 ms -> isError "timed out after 200 ms" in 208 ms, next call 3 ms; `await new Promise(()=>{})` -> fails in 3 ms "can never settle"; `setTimeout(r,500)` with `timeoutMs` 2000 -> "woke" in 506 ms (heuristic does not misfire); host tool 300 ms vs `timeoutMs` 100 -> timeout in 111 ms, late tool does not run on (signal aborted); self-rescheduling `setTimeout` loop -> ends at 311 ms; sync throw / reject / `throw "str"` / `throw {}` -> isError; `undefined`/function/symbol -> "(no output)"; cyclic -> `TypeError: circular reference`; BigInt -> `Do not know how to serialize a BigInt`; NaN -> `null`; 1 001 timers -> clear error; syntax error -> `SyntaxError: ...`; 50 MB string -> truncated 20 KB with tmp path (PASS)
- Break 3 memory/stack (`mem.ts`): alloc loop at `memoryMb` 8/64/256 -> `InternalError: out of memory (codemode.memoryMb = N)` in 18/73/184 ms, next call "alive" each time; `ArrayBuffer(500MB)`, object churn, string doubling -> error result; `function f(){f()}` -> "stack overflow"; recursion in a `.then` callback -> "stack overflow"; deep JSON -> "the sandbox crashed: Maximum call stack size exceeded", next call fine; 10 000 and 12 000 `console.log` of 1 KB (>10 MB) -> ok, 20 KB output, 76-85 ms; regex catastrophic backtracking with `timeoutMs` 500 -> timeout at 505 ms; busy loop while a tool is pending -> timeout at 305 ms (PASS, with the notes below)
- Break 4 concurrency + gate (session level): `Promise.all(8)` peak 4 (`concurrency` 4), 2 (2), 1 (1), result `[1..8]`; 8 parallel writes in `ask` -> peak 1 dialog, order `codemode, f0..f7`, 3rd denied -> `["ok","ok","Blocked: denied by the user","ok",...]`, f2.md absent, other 7 written; `session` answer on first -> asked `["codemode","write"]`; two `codemode` calls in one turn -> peak 1 dialog; `auto` classifier overlap peak 4 (<= `concurrency`), all 8 written; 51st call rejects with `tool call limit reached: 50 per script (codemode.maxToolCalls)`, 50 before succeed; denied/failed and invalid-argument calls DO count towards `maxToolCalls` (counted before validation); `tools.nope()` is `not a function` (PASS)
- Break 5 headless deny uncatchable (`auto`, classifier `dangerous`, `prompter: null`): `try/catch`, `Promise.allSettled`, `.catch(()=>{})`, deny on the 2nd of two parallel calls, a deny caught inside a `setTimeout` callback, `try { } finally { write after.md }`, `Promise.race([... .catch(), timer])` -> all `agent_end(blocked)`, tool message `Error: the script was stopped: a tool call was blocked`, none of `caught/swallowed/settled/TIMER-CAUGHT/FINALLY-RAN` appear, `after.md` never written; `ask` headless -> `codemode` itself denied (`source: headless`, run blocked) before any script runs; two parallel codemode calls with a deny in the 2nd -> blocked (PASS)
- Break 6 abort: `session.abort()` mid-`setTimeout` -> `aborted`, tool message "the run was aborted", no unhandled rejection, `late.md` absent; mid host call -> `late2.md` absent; abort while a write prompt is open and then answered -> `late3.md` absent; live `kill -SIGINT` of a headless CLI 1 s after the `codemode` permission event with a 90 s script -> `tool_result` "the script was stopped: the run was aborted", `agent_end(aborted)`, exit 1 (existing aborted convention), process gone ~1 s later, no leftover process (PASS)
- Break 7 output/declarations: 100 KB -> 20 154 B shown (10 000 head + marker + 10 000 tail + `Full output:` path), file holds 100 002 B; 30 000 x "日" and 15 000 x "é" cut with no U+FFFD; 80 codemode tools -> 61 declared (11 997 chars of declarations, `(19 more; find them with searchTools(query))`), `searchTools('tool number 79 words')` finds #79; `inlineBudgetTokens: 0` -> only the "N more" line; `describeTool` returns the server `inputSchema` verbatim for an MCP tool and `{"type":"object","properties":{},"additionalProperties":true}` for a zod passthrough built-in; provider description = spec §10.3 lines minus the `llm` line + `Available tools:` (checked line by line against `docs/notes/tree-agent-harness-spec.md`) (PASS)
- Break 8 direct set: the committed `.tree-agent/mcp.json` `toolExposure` (`search_memory: direct`, `visualize_*: deferred`) -> `session.test.ts` "with the committed .tree-agent/mcp.json the direct set is exactly..." passes; live server confirms `search_memory` direct, `visualize_*` absent from `tools` (PASS)
- Break 9 dependency hygiene: `"quickjs-emscripten": "0.32.0"` exact; `pnpm install --frozen-lockfile` "Already up to date"; `make -C apps/harness build` OK, `dist/codemode/sandbox.js` runs a script; `node dist/cli.js --help` prints the usage after "Unknown option '--help'" (the CLI has no `--help` flag; `cli.ts` change is one line, so pre-existing); no `.wasm` under `src/` or `tests/`; `dist/` git-ignored
- Break 10 mutations: (a) removed `signal?.addEventListener("abort", ...)` in `sandbox.ts` -> 4 tests fail (`sandbox.test.ts` "an abort tears the script down before a catch can run", `codemode.test.ts` "an aborted run stops the script and says so", `session.test.ts` headless try/catch, `cli.test.ts` exits 2 even when the script catches); (b) removed `prompts: this.#prompts` in `session.ts` -> 2 tests fail ("parallel calls get one dialog at a time...", "a `session` answer covers..."); both restored, `shasum -c` OK
- Break 11 regression: `agent-loop.ts` 180 lines, no `codemode`/`mcp` reference; TUI boundary grep (`grep -rln "provider/\|loop/\|tools/\|mcp/\|codemode/" apps/harness/src/tui`) empty (`tool-card.ts` and `permission-prompt.ts` import only `pi-tui`, `../types.ts` and `./style.ts`); `git status`: no `.env*`, no `apps/memory`, spec doc untouched (still untracked, not mine); no leftover processes

**Acceptance criteria**
- [x] PASS — two fake tools + two logs -> exactly logs + JSON, `structured` is what the script receives — `tests/tools/codemode.test.ts:112` and `:132`; my `misc.ts`: structured BigInt/cyclic reject catchably
- [x] PASS — gate (deny rejects + catch continues, `ask` waits and then runs, headless deny blocked + uncatchable, read-only never prompts) — `tests/session.test.ts` "ask: prompts for codemode, then for a write inside it...", "ask: the script waits on the prompt...", "headless: a dangerous call inside a script ends the run blocked, even inside try/catch", "a read-only tool inside a script never prompts"; `tests/cli.test.ts` exit 2; plus my 7 headless variants above and mutation (a)
- [x] PASS — 8 parallel peak <= 4 and all complete; 51st names `maxToolCalls` — `codemode.test.ts:207`, `:227`; my session-level peaks 4/2/1
- [x] PASS — `while(true){}` 200 ms < 1 s + disposed + 2nd call; alloc loop -> error; `typeof fetch/require/llm` undefined; `setTimeout` works, `setInterval` does not — `sandbox.test.ts:73`, `:113`, `:44`, `:54`; reproduced in `timeouts.ts`/`mem.ts`/`escapes.ts`
- [x] PASS — 100 KB middle-truncated with tmp path; `codemode` absent with 0 / present with 1; declarations stop at the budget and `searchTools` finds one past it; `describeTool` JSON Schema — `codemode.test.ts:81`, `:313`, `:367`, `:388`, `:410`; reproduced in `out.ts`
- [x] PASS — committed `.tree-agent/mcp.json` direct set — `session.test.ts` (reads the committed file); live listing consistent
- [ ] [HUMAN] AC 1 — NOT ticked (see assessment below)
- [ ] [HUMAN] AC 2 — NOT ticked (headless half not re-run: needs Bright Data; per the brief)
- [x] PASS — cadence green (twice)

**[HUMAN] AC 1 assessment**: not satisfied as run. My own live run (same prompt, repo cwd, `--trust --json`) also made ONE `codemode` call with two `read`s (`source: readOnly`), but the script's result was `{"harness":null,"memory":null,"rawA":"{\"path\":...,\"lines\":[...`: the model regex-parsed `JSON.stringify(read result)` as if it were file text, got nulls, and the final answer (`tree-agent` / `tree-memory`) was then extracted by the model from the raw output. The SWE's run returned raw contents and the model extracted the names. Two runs, two different ways of not meeting "result is the two names". Root cause is prompt quality, not the AC wording and not a code defect: `read`'s `structured` is `{path, lines: string[]}`, but nothing the model sees says so (the codemode declarations list only the `codemode`-exposed tools, not `read`/`bash`/`edit`/`write`, and the §10.3 text only says "returns its structured result"). Recommendation: keep the AC; add a one-clause hint to `codemode`'s `promptSnippet` (free text, not the verbatim §10.3 description), e.g. "built-in results: `read` -> `{path, lines}`, `bash` -> ...", or have 211/212 declare direct tools' result shapes; then re-run the human check. Do not relax the AC.

**Other issues found (none blocks PASS)**
1. stderr noise on large values (reproduce: `runSandbox` / `codemode` script `return 'x'.repeat(5000000)`, or `const s = await tools.big(); return s.length` with a 5 MB tool result, or an async function awaiting itself 20 000 times): QuickJS prints `Aborted(Assertion failed: list_empty(&rt->gc_obj_list), at: ../../vendor/quickjs/quickjs.c,2036,JS_FreeRuntime)` to stderr during dispose (emscripten's own `printErr`). The result is correct, the throw is caught by `#dispose`'s try/catch (only `log.debug`), 30 repeated runs show no RSS growth (122 -> 140 MB), the next call works. In the TUI that stderr line would land in the rendered screen; a scraped page over a few MB hits it. Suggest passing `printErr` (no-op or `log.debug`) in `newVariant`, or capping what enters the VM. Follow-up task, not a blocker.
2. A QuickJS out-of-memory thrown inside a long `.then` chain (`let p=Promise.resolve(0); for(let i=0;i<1e6;i++) p=p.then(v=>v+1)` at `memoryMb` 64) surfaces as `Error: null` (`#describe` of a `null` dump) instead of naming `memoryMb`; at 1024 MB the same script succeeds. Cosmetic.
3. A script error is printed `Error: Error: boom` (the tool's `Error:` prefix + `Name: message`), cosmetic.
4. Known limitation as stated by the SWE (prompts queued for a timed-out script are still shown) could not be reproduced with a short timeout because the deadline is held while prompts are open (a 100 ms `timeoutMs` script with three 80 ms prompts finished normally); it needs a script that times out while it is NOT in a prompt but has later calls queued. Left as documented.
5. Tool rejecting with a non-Error (`throw "str"`) reaches the script as an empty message (`registry.execute` does `(err as Error).message`), pre-existing in `registry.ts`, only surfaced here.
6. `tsType` renders an array of objects as `object[]` and anything past two levels as `object` (`$ref`/`allOf` -> `unknown`); acceptable for a hint, `describeTool` carries the full schema.
7. Headless `--permission-mode ask` denies `codemode` itself (it is not read-only) before any script runs; consistent with 206, worth one line in the book.

**VERDICT: PASS**

### [SWE] 2026-10-25 03:21 — Fold-ins before commit

**Files modified**
- `apps/harness/src/tools/codemode.ts` — the `codemode` `promptSnippet` (free text; `CODEMODE_PREAMBLE`, the §10.3 description, is unchanged) now ends "…in it too, where read returns {path, lines: string[]}, bash {exitCode, stdout, stderr} (a non-zero exit rejects), edit and write a status string" (shapes from `read.ts`/`bash.ts`; `edit`/`write` have no `structured`, so the script gets their `content`). A script error that already names itself (`/^\w*Error: /`) is no longer prefixed: `Error: boom`, `TypeError: bad`, `InternalError: out of memory (…)`; bare messages (`throw "boom"`, the timeout) keep `Error: `.
- `apps/harness/src/codemode/sandbox.ts` — the module's `printErr` goes to `log.debug("codemode sandbox: …")` via `newVariant(…, {emscriptenModule: {printErr}})` (emscripten's default is `console.error`), so the `Aborted(Assertion failed: list_empty(&rt->gc_obj_list)…)` line no longer reaches the TUI. QuickJS throws `null` when it runs out of memory with no room left for the error object (a 1e6-step `.then` chain); `#fail` maps a `null` error to `InternalError: out of memory` + the `memoryMb` hint only when the wasm heap is within 1 MB of its cap (probe: 79.94/80 MB for the chain, 16/80 MB for a plain `throw null`), so `throw null` still reads `null`. The rejection comes through the main promise, not a host-call settle.
- `apps/harness/tests/codemode/sandbox.test.ts` — "an out-of-memory in a long .then chain names memoryMb; a `throw null` stays null", "a 3 MB returned string comes back whole; the module's stderr goes to log.debug" (asserts a `codemode sandbox: Aborted(` debug line and no `console.error`; each assertion was checked red with `printErr` removed).
- `apps/harness/tests/tools/codemode.test.ts` — "a script error is rendered once: `Error: boom`, a bare throw gets the prefix"; "its prompt line names the built-ins' result shapes, and scripts get exactly those" (real `builtinRegistry`: `read` keys `[path, lines]`, `bash` keys `[exitCode, stderr, stdout]`, `write`/`edit` strings, `exit 3` rejects); the `llm` and OOM expectations lost the double prefix.

**Tests**
- Unit: 759 passing, 0 failing, 43 files (was 755) — `make harness-tests`; vitest exits cleanly, no `Aborted(` anywhere in the run's output.
- Cadence: `make harness-lint-fix && make harness-lint-check && make harness-typecheck && make harness-tests && make pre-commit` green.

**[HUMAN] AC 1 re-run (once, live, repo root, `--trust --json`, scratch `TREE_AGENT_HOME`, read-only)**
```
$ TREE_AGENT_HOME=<scratch>/home-ac1 make -C apps/harness run ARGS='--cwd ../.. --trust --json' PROMPT='Use codemode: read package.json under apps/harness and apps/memory/pyproject.toml and return both project names'   → exit 0
tool_call codemode: const a = await tools.read({path: "apps/harness/package.json"}); const b = await tools.read({path: "apps/memory/pyproject.toml"});
  const pkgText = a.lines.join("\n"); … harness = JSON.parse(pkgText).name …; const line = b.lines.find(l => /^\s*name\s*=/.test(l)); return {harness, memory: line};
permission read (origin codemode) allow readOnly   ×2
tool_result: {"harness":"tree-agent","memory":"name = \"tree-memory\""}   isError false
turn_end: "- `apps/harness/package.json`: `tree-agent`\n- `apps/memory/pyproject.toml`: `tree-memory`"
agent_end completed
```
- ONE `codemode` call, and the script now uses `a.lines` / `b.lines` correctly (the shape hint worked: no probe call, no parsing of `JSON.stringify(result)`). The result carries both names, but only the harness one is bare: the memory value is the matched TOML line `name = "tree-memory"`, not the name alone. I left the AC unticked: the headless half is a near-pass for the human to judge, and the TUI "script collapsed under the card" half was not run.

**Notes**
- The null-to-OOM mapping is a heuristic on the wasm heap; a null OOM raised by `setMemoryLimit` (heap not at the cap) would still read `null`. I did not observe one.
