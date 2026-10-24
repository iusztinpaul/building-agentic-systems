---
id: 210-harness-codemode-sandbox-bridge
status: pending
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

- [ ] A script calling two fake tools and logging twice returns exactly the two log lines + the JSON return
      value — the tool results themselves never appear in the tool message; `structured` is what the script
      receives (a fake tool with `structured: {n: 1}` → `(await tools.fake()).n === 1`).
- [ ] `tools.x()` goes through the gate: with a FakePrompter answering `deny`, the promise rejects with the
      reason and a `try/catch` in the script continues; with `ask`, the sandbox waits (the FakePrompter resolves
      after 100 ms) and the call then runs; in headless mode a `deny` ends the run with `agent_end(blocked)` and
      exit 2 even when the script wraps the call in `try/catch`; a read-only tool inside a script never prompts.
- [ ] `await Promise.all(8 calls)` on a fake tool that records overlap shows ≤ 4 in flight and all 8 complete;
      the 51st `tools.x()` in one script rejects naming `maxToolCalls`.
- [ ] `while (true) {}` with `timeoutMs` 200 → an `isError` result naming the timeout within 1 s and the
      runtime is disposed (no leak: a second call runs normally); an allocation loop hits `memoryMb` → an error
      result, never a process crash; `typeof fetch === "undefined"`, `typeof require === "undefined"`,
      `typeof llm === "undefined"` inside the sandbox (this task); `setTimeout` works, `setInterval` does not.
- [ ] A 100 KB output is middle-truncated to 20 000 bytes with the tmp path; the `codemode` tool is absent
      with zero codemode tools and present with one; inline declarations stop at the 3 000-token budget and
      `searchTools` finds a declaration past it; `describeTool` returns the JSON Schema of a codemode tool.
- [ ] With the committed `.tree-agent/mcp.json` the direct set is `read`, `bash`, `edit`, `write`,
      `mcp__tree_memory_local__search_memory`, `tool_search`, `codemode` (M5's "4 + codemode" count).
- [ ] [HUMAN] `tree-agent --json "Use codemode: read package.json under apps/harness and apps/memory/pyproject.toml
      and return both project names"` runs ONE `codemode` call whose result is the two names; the TUI shows the
      script collapsed under the card.
- [ ] [HUMAN] (`make local-start`, live key, `BRIGHTDATA_API_KEY` + `BRIGHTDATA_UNLOCKER_ZONE` in `.env` — else
      substitute `search_memory` with a limit of 3) `tree-agent --trust "Write a codemode script that calls
      scrape_web on https://example.com and returns the page title"` — `scrape_web` is codemode-only, the script
      reaches it, the answer is "Example Domain"; in the TUI `ask` mode the user is asked for `codemode` and
      then for the `scrape_web` call inside it.
- [ ] `make harness-lint-fix && make harness-lint-check && make harness-typecheck && make harness-tests &&
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
