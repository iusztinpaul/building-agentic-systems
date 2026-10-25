---
id: 211-harness-sandbox-llm-depth-budgets-rlm
status: done
feature: tree-agent-harness
---

# M6b — `llm()` inside the sandbox: depth 1 by default (plain completions, no tools), depth 2 behind `codemode.llm.depth` (a sub-loop with `direct` tools, no nested codemode), per-script budgets, and `llm_call` events in the TUI (the RLM demo)

Tags: `harness`, `codemode`, `rlm`, `enhancement`
Depends on: 210 (sandbox + bridge)
Blocks: —
Implements: ADR-016 §9 (`llm()` depth 1 default, depth 2 opt-in)

## Scope

1. **`src/codemode/llm.ts`** — spec §10.6: `llm(prompt, {schema?, model?, system?, maxTokens?, tools?})`
   returns a string, or the parsed object when `schema` (JSON Schema) is given; `model` defaults to
   `settings.codemode.llm.model ?? settings.decisionModel`; `system` default "You are a sub-task worker.
   Answer only what is asked."; `maxTokens` 2 048; throws on budget or parse errors (after `complete`'s one
   retry).
2. **Depth** — `ToolContext.depth`; depth 1 (default): `provider.completeText` / `complete<T>`, no tools, no
   sandbox; depth 2 (`codemode.llm.depth: 2`): `runLoop` with a fresh `Context`, the SAME hooks (the gate
   applies), the registry's `direct` ∪ activated tools minus `codemode`, `maxSubTurns` 8; `tools: true` is
   honoured only at depth 2; a call beyond the configured depth returns an error to the script. Depth N is
   not implemented (spec).
3. **Budgets** (`settings.codemode.llm`) — `maxCalls` 16 per script, `maxInputChars` 200 000 per call,
   `maxTotalTokens` 400 000 per script (sum of `usage`); the sandbox's `concurrency` (4) semaphore is shared
   between tool and `llm()` calls.
4. **Events + TUI** — every call emits `{type: "llm_call", depth, model, usage}`; the TUI nests `llm_call`
   rows under the active `codemode` card; the status bar's session cost includes `llm()` usage × pricing.
   The gate treats `llm()` as read-only (never prompts); calls are budgeted and logged.
5. The `codemode` provider description gains the `llm(prompt, {schema?, model?, system?})` sentence (spec §10.3).

**Out of scope (intentional):** depth N / recursive codemode (spec: explained in the chapter, not built);
a per-call cost cap in currency (tokens are the budget); caching `llm()` answers.

## Acceptance Criteria

- [x] Depth 1: `llm("x")` reaches `completeText` with no `tools` and the default system prompt; with
      `schema` it reaches `complete<T>` and returns the parsed object; a schema mismatch after the retry throws
      inside the script with the parse message; `tools: true` at depth 1 is ignored (no sub-loop).
- [x] Budgets: the 17th `llm()` in one script throws naming `maxCalls`; a 200 001-char prompt throws naming
      `maxInputChars`; scripted `usage` summing past 400 000 throws naming `maxTotalTokens`;
      `Promise.all(8 llm calls)` shows ≤ 4 in flight.
- [x] Depth 2 (`codemode.llm.depth: 2`): a sub-call can `read` a file it was handed (FakeProvider scripts a
      `read` tool call inside the sub-loop) and the sub-loop stops at `maxSubTurns`; the sub-loop's tools array
      has no `codemode`; a non-read-only tool inside the sub-loop goes through the gate; with depth 1
      configured, `llm("…", {tools: true})` answers from the prompt only and a call that would need depth 3
      returns an error to the script.
- [x] Every call emits `llm_call` with `depth`, `model`, `usage`; the session cost in the status bar includes
      them (unit: the cost reducer sums `llm_call` usage × pricing).
- [x] Main-context accounting: running the §10.6 worked example on FakeProvider (40 scripted hits, 40 map
      answers, 1 reduce answer) grows `ctx.messages` by exactly 2 (the assistant `codemode` call and its tool
      result), and no hit text appears anywhere in `ctx.messages`.
- [x] `llm()` never triggers a permission prompt in `ask` mode (FakePrompter records zero asks).
- [x] [HUMAN] (`make local-start`, live key) the RLM demo: `tree-agent --json "Using one codemode script:
      search my memory for 'agents' with limit 20, ask llm() for a one-line summary of each hit in parallel,
      then ask llm() with a JSON schema to merge them into at most 5 bullet points, and return only the
      bullets"` shows one `codemode` call, ≥ 2 `llm_call` events (depth 1), and a final answer of ≤ 5 bullets
      (headless half — SWE live run, see "Fold-ins before commit").
- [ ] [HUMAN] the same demo in the TUI shows the `llm_call` rows nested under the card and the cost rising.
- [x] `make harness-lint-fix && make harness-lint-check && make harness-typecheck && make harness-tests &&
      make pre-commit` green.

## User Stories

### Story: Map-reduce over memory without flooding the context
1. The user asks for every pricing decision in memory, summarised.
2. The model writes the §10.6 script: `search_memory` → `Promise.all(hits.map(h => llm(...)))` →
   `llm(merge, {schema})` → `return answer.facts`; the transcript shows one card with 41 nested `llm_call`
   rows; the main context grew by the script and the list only.

### Story: A sub-call needs to read a file (depth 2)
1. `settings.json` sets `codemode.llm.depth: 2`; the script calls `llm("Summarise /repo/CHANGELOG.md", {tools: true})`.
2. The sub-loop calls `read` on the file (read-only, so no prompt even in `ask` mode), answers;
   the main model receives only the summary.

### Story: A script over-spends
1. A script loops `llm()` over 100 slices.
2. The 17th call throws `llm budget exceeded: maxCalls=16`; the script's `catch` returns the partial result.

### Story: The default depth refuses tools
1. With depth 1, a script passes `{tools: true}`.
2. The call answers from its prompt; no sub-loop runs; nothing errors.

### Story: Cost stays visible
1. During the map-reduce the status bar's cost ticks up with each `llm_call`.
2. After the run, the cost equals main-turn usage + the sum of `llm_call` usage at the models' prices.

---

Blocked by: 210

## Log

### [PA] 2026-10-24 20:31 — Grooming

**Summary**
Make the sandbox recursive: `llm()` as a budgeted, depth-limited host function — plain completions by default,
a tool-capable sub-loop behind one setting — with every call visible as an `llm_call` event and counted in the
session cost; the chapter's RLM and agentic-RAG demo.

**Key decisions**
- Depth 1 default (the RLM paper's setting, linear cost, single gate); depth 2 reuses `runLoop` and the same
  hooks so no second permission path exists.
- One semaphore for tools and `llm()` — the spec's `concurrency` is one number.
- The context-accounting AC is the chapter's figure made into a test.

**Dependencies**
- 210 — the sandbox and bridge.

**User stories**
- 5 stories covering: map-reduce over memory, a depth-2 read, a budget overrun, depth-1 refusing tools, cost
  visibility.

Ready for implementation.

### [SWE] 2026-10-25 03:38 — Implementation

**Bindings (what later tasks build on)**
- `src/codemode/llm.ts` (new) — `createLlm(o) → AsyncHostFn`, one per script (the budgets count across its calls). Options are zod-checked (`.strict()`: `{temperature}` → `llm(): options: Unrecognized key(s)…`). Depth: a call's depth = `ctx.depth + 1`, or `+ 2` when `{tools: true}` is honoured (only with `codemode.llm.depth: 2`); `depth > codemode.llm.depth` → `Error("llm() at depth N is beyond codemode.llm.depth = D; nested llm() calls are not supported")`. Order of checks: options, depth, `maxCalls` (`++calls > max`, counted when the host fn starts, i.e. after it got a semaphore slot), `maxInputChars` (prompt + the script's own `system`; the default system is not counted), `maxTotalTokens` (`spent >= max` before the call: calls already in flight when the line is crossed still finish, every later one throws), schema+tools refused. Then `provider.modelInfo(model)` (unknown model → error to the script, nothing sent; the pricing goes to the event sink). Depth 1: `completeText({model, system, input, maxOutputTokens})` or `complete({…, schema: zodFromJsonSchema(schema), name: "llm", onUsage})`. Depth 2: `runLoop` with a fresh `Context {system, [user prompt]}`, a fresh `ToolRegistry` holding `registry.declared()` minus `codemode` (+ `activate(registry.activated())`), `maxTurns: maxSubTurns`, `signal: host.signal`, `depth: 2`, hooks = `{beforeToolCall: the codemode gate wrapped in host.hold(), emit: sums turn_end usage, keeps error/agent_end}` — ONLY the gate is shared: the session's `prepareTurn` would replace `session.#context` with the sub-context, its queues would drain into the sub-loop, and a forwarded `agent_end` would become the main run's reason. `max_turns` → `llm() sub-loop stopped at maxSubTurns=N without an answer`; any other non-completed end throws with the reason. Every call that reached the provider (success, parse failure, provider error) calls `onCall({type: "llm_call", depth, model, usage?}, pricing)` from a `finally`.
- `zodFromJsonSchema` (in llm.ts): type (or list), properties/required, items, enum, const, anyOf/oneOf, description; non-required properties `.nullish()` (strict structured outputs send them as required+nullable); unknown keywords pass through as `z.unknown()`. `complete<T>`'s `strictJsonSchema` re-derives the wire schema, so a script's schema without `additionalProperties` still goes out strict (pinned: `additionalProperties: false` on the wire).
- `StructuredRequest.onUsage?(usage)` (provider/types.ts) — called per attempt by `OpenRouterProvider.complete` (the retry is a second spend) and by `FakeProvider.complete` (estimate chars/4). Optional, so `decide<T>()` and the classifier are untouched. `FakeScript.texts` accepts `{text, usage}` to script usage.
- `bridge.ts` — `BridgeOptions.llm?: AsyncHostFn`; when set, prelude defines `globalThis.llm(prompt, opts)` over `host.llm` (a non-string prompt is a `TypeError` in the VM, nothing sent) and the fn joins `async` next to `call`, so the sandbox's ONE `#callHost` semaphore caps tools and llm() together (no new semaphore; `sandbox.ts` untouched).
- `tools/codemode.ts` — the §10.3 sentence verbatim after the `searchTools` line; `CodemodeLimits = Settings["codemode"]` (llm included); `CodemodeOptions` + `provider`, `decisionModel`, `onLlmCall?`.
- `session.ts` — passes `provider`, `decisionModel ?? DEFAULT_MODEL` (now exported from `decide.ts`) and `#llmCall(event, pricing)`: `cost += costOf(usage, pricing)`, `#persist()`, append `{type: "llm_call", depth, model, usage?}`, `#emit(event)`. llm() never reaches `#gate`.
- `session-store/jsonl.ts` — `EntryBody` `llm_call`; `restore()` pushes `{model: e.model, usage}` into `spend` (the existing per-model pricing in `#restore` then prices it), so the cost survives `--resume`.
- TUI — `ToolCard.addLlmCall(event)` rows `llm depth 1 · <model> · 1.2k in / 300 out` (or `no usage`), between the script and the result, collapsed by the same `#block` (first row + `(+N lines, Tab)`); `ToolCard.done`; `Transcript` keeps the last `codemode` card and routes `llm_call` to it while it has no result (the loop runs calls one at a time), dropping strays. `compact()` reused from `status-bar.ts`. Headless `--json` prints `llm_call` through the existing `jsonSink`; the text sink ignores it.
- Settings: the `codemode.llm` numbers are now `int().positive()` like the other codemode limits.
- `agent-loop.ts` untouched (180 lines, no `llm`).

**Files modified**
- `apps/harness/src/codemode/llm.ts` (new), `src/codemode/bridge.ts`, `src/tools/codemode.ts`, `src/session.ts`, `src/session-store/jsonl.ts`, `src/provider/types.ts`, `src/provider/openrouter.ts`, `src/provider/fake.ts`, `src/decision/decide.ts` (export `DEFAULT_MODEL`), `src/config/settings.ts`, `src/tui/tool-card.ts`, `src/tui/transcript.ts`.
- Tests: `tests/codemode/llm.test.ts` (new, 25), `tests/session.test.ts` (+6; setup takes `texts`/`models`; codemode LIMITS gain `llm`), `tests/tools/codemode.test.ts` (2 updated: the description now has the llm sentence; the 210 `typeof llm === "undefined"` / ReferenceError test became "llm() is defined (task 211); fetch and require still are not"), `tests/tui/tool-card.test.ts` (+1), `tests/tui/transcript.test.ts` (+1), `tests/session-store/jsonl.test.ts` (+1), `tests/headless/print.test.ts` (1 extended). `tests/codemode/sandbox.test.ts` unchanged: a bare `runSandbox` has no bridge, so `llm` is still undefined there, correctly.

**Tests**
- Unit: 793 passing, 0 failing, 44 files (was 759 / 43) — `make harness-tests`.
- Integration: N/A (the harness has none).
- Mutation checks (each restored): dropping the session's llm cost line fails the 2 cost tests; registering `codemode` in the sub-loop registry fails the depth-2 "no codemode" tests (tool-level and session-level); disabling the `maxTotalTokens` check fails its test.

**Acceptance criteria**
- [x] Depth 1 — `tests/codemode/llm.test.ts` "reaches completeText with no tools, the default system, 2 048 tokens and the decision model" (no stream request = no sub-loop), "takes model, system and maxTokens from the call, and codemode.llm.model over decisionModel", "with a schema reaches complete<T> and returns the parsed object", "a schema mismatch after complete's one retry throws inside the script with the parse message" (real `OpenRouterProvider` over `fakeFetch`: 2 POSTs, script catches `llm: facts: Expected array, received string`, both attempts' usage in the event), "ignores {tools: true} at depth 1".
- [x] Budgets — "the 17th call in one script throws naming maxCalls; the catch keeps the partial result" (`n: 16`, `llm budget exceeded: maxCalls=16 per script (codemode.llm.maxCalls)`), "a 200 001-char prompt throws naming maxInputChars and sends nothing", "scripted usage summing past 400 000 tokens throws naming maxTotalTokens" (2 × 210 000 → 3rd throws `used 420000`), "Promise.all over 8 llm() calls keeps at most 4 in flight" (peak exactly 4, all 8 answered), "tools.x() and llm() share the one concurrency semaphore" (4 tools + 4 llm interleaved → combined peak 4), "the budget is per script".
- [x] Depth 2 — "a {tools: true} call runs a sub-loop that can read the file it was handed; codemode is not in its tools" (sub tools exactly `read, bash, edit, write`; the 2nd request carries the file text; the gate saw `read`; one `llm_call` depth 2 with the summed usage), "a non-read-only tool inside the sub-loop goes through the gate, and a deny is answered to the sub-model", "the sub-loop stops at maxSubTurns and the call throws naming it", "a call that would need depth 3 returns an error to the script" (codemode at `ctx.depth 1` + `{tools: true}`), plus depth-1 config at `ctx.depth 1` and schema+tools; session level: `tests/session.test.ts` "depth 2: the sub-loop's read never prompts, its write goes through the same gate in ask mode" (asks `["codemode", "write"]`, read `source: readOnly`, `out.md` written, main context `user, assistant, tool, assistant`, ONE `agent_end`); "headless depth 2: a dangerous call inside the sub-loop ends the run blocked; the script's catch never runs" (210's uncatchable headless deny holds through the sub-loop: one `agent_end(blocked)`, no `caught`, 2 provider streams).
- [x] `llm_call` + cost — "every call reports depth, model and usage, with the model's pricing", "an unknown model fails before anything is sent and reports no call", "a provider error still reports the call, without usage"; `session.test.ts` "the session cost sums the main turns and every llm_call's usage × its model's pricing, and survives a resume" (distinct prices for main and decision model; 41 `llm_call` spend entries in the store; resumed cost equal), "the cost rises with each llm_call as it arrives, before the codemode result"; `jsonl.test.ts` "restores llm_call usage as spend…"; TUI "nests one row per llm() call…", "nests llm_call rows under the running codemode card only"; headless jsonSink prints `llm_call`.
- [x] Main-context accounting — `session.test.ts` "the §10.6 worked example grows the context by exactly 2 and no hit text enters it": the §10.6 script verbatim over a fake `search_memory` returning 40 hits, 40 scripted map answers, 1 schema answer; messages grow by 2 across the codemode call, roles `user, assistant, tool, assistant`, the tool result is `["Pro is $5","Team is $20"]`, no `HIT-` and no map answer anywhere in `ctx.messages`; 40 `completeText` (all carrying hit text) + 1 `complete`; 41 `llm_call` at depth 1.
- [x] Zero prompts — "ask mode: llm() never prompts; the only question is the codemode call itself": prompter asks `["codemode"]` (the codemode call is not read-only, 210), permission events only `codemode` (user) and `search_memory` (readOnly) — none for 41 llm() calls; tool-level "never goes through the permission gate" (`gated` empty).
- [ ] [HUMAN] RLM demo — run once live, NOT ticked (see Evidence): 3 codemode calls, not 1.
- [x] Cadence green.

**Evidence**
```
$ make harness-lint-fix && make harness-lint-check && make harness-typecheck && make harness-tests && make pre-commit
Checked 98 files in 58ms. No fixes applied.
pnpm exec tsc --noEmit
 Test Files  44 passed (44)
      Tests  793 passed (793)
biome check (harness)....................................................Passed   (prettier, ruff check, ruff format Passed)

# [HUMAN] RLM demo, once, live: make env-status → local; tree-mongodb/mongot up. Repo root, scratch TREE_AGENT_HOME
# whose settings.json sets codemode.llm.maxCalls = 32 (see Notes), the AC's prompt verbatim:
$ TREE_AGENT_HOME=<scratch>/home211 make -C apps/harness run ARGS='--cwd ../.. --trust --json' PROMPT="Using one codemode script: search my memory for 'agents' with limit 20, …"   → exit 0
event counts: tool_call 3 (all codemode), llm_call 2, permission 6 (codemode + search_memory, classifier, x3), agent_end completed
codemode #1: search_memory({query:'agents', top_k:20}) → expected `res.parents`; got `{result: "<json string>"}` → returned {"bullets":[]}
codemode #2: probe of the result shape → {"type":"object","keys":["result"],…}
codemode #3: unwraps res.result → Promise.all(parents.map(p => llm(`Summarize … one line`))) → llm(merge, {schema: {bullets: string[], maxItems 5}})
  {"type":"llm_call","depth":1,"model":"anthropic/claude-haiku-5.5","usage":{"inputTokens":1286,"outputTokens":366}}
  {"type":"llm_call","depth":1,"model":"anthropic/claude-haiku-5.5","usage":{"inputTokens":321,"outputTokens":76}}
  tool_result: an array of 3 strings (isError false)
turn_end: 3 bullets + "the search for 'agents' returned weak matches" (memory content not copied here)
```
- What passed: the live schema path (strict `json_schema` from the script's JSON Schema, `maxItems` included) returned parsed JSON; `llm_call` events at depth 1 with usage; ≥ 2 `llm_call`; final answer 3 bullets (≤ 5); the hits never entered the main context (only the 3 bullets did).
- What did not: 3 codemode calls instead of 1. The cause is not llm(): `search_memory`'s `structuredContent` is FastMCP's `{result: "<json string>"}` wrapper (210's Notes saw the same with `scrape_web`), so the first script found no `parents`, the second probed the shape. Memory also returned only 1 parent for 'agents' (1 map call + 1 merge), so the 20-way fan-out was not exercised live. The TUI half (rows nested under the card, cost rising) was not run.

**Notes**
- Spec tension for the PA: §10.6's example (`limit: 40` → 41 calls) and this task's [HUMAN] demo (`limit 20` → 21 calls) both go past the spec default `maxCalls` 16, which the Budgets AC pins (17th throws). With defaults, a 20-hit `Promise.all` loses the whole map at the 17th call. I kept 16 (the AC) and raised it to 41 in the worked-example test and to 32 in the live run's scratch settings; the PA should pick: raise the default (e.g. 64), or change the demo/example to ≤ 15 slices, or document the override in the chapter.
- Follow-up candidate (memory app or `mcp/register.ts`, not this task): unwrap FastMCP's `{result: "<json string>"}` `structuredContent` (or teach the codemode prompt line about it); it cost two extra codemode calls in the live demo, as in 210's.
- Deliberate choices: `{tools: true, schema}` at depth 2 is refused with a hint (no story needs it; the script can chain two calls); the depth-2 sub-loop has no `afterToolCall` (built-ins cap their own output at 50 KB, MCP at 20 KB; the session's 64 KB backstop is private and the sub-context dies with the call); `tool_search` stays in the sub-loop's tools (spec: direct minus codemode) but would activate in the main registry; llm() time counts against `codemode.timeoutMs` (only gate prompts inside a sub-loop hold the deadline) — 40 calls at concurrency 4 is 10 rounds; a slow model can hit 60 s.
- A provider call still in flight when the script is torn down (timeout, abort, headless block) rejects, and its `finally` still reports an `llm_call` (no usage, cost 0): it can land in the JSONL/`--json` after the codemode result. Intent, not a leak; the TUI drops it (the card is done).
- `Transcript.load()` (resume) does not rebuild `llm_call` rows under old codemode cards; the JSONL has them, the AC does not ask for it.
- The cost reducer is the session's `#llmCall` (same `costOf` as turns) plus `restore()`'s spend entries; `status-bar.ts` only formats `status().cost`.


### [Tester] 2026-10-25 03:55 — QA

**Test summary**
- Format / lint / typecheck / pre-commit: PASS (`make harness-lint-fix && harness-lint-check && harness-typecheck && harness-tests && make pre-commit`, exit 0, run twice: before and after my probes)
- Unit tests: 793 passed / 0 failed (44 files); integration: N/A (none by design)
- Warnings: 0 vitest warnings (the `[tree-agent] warn:` lines are the corrupt-store / trust-file tests' intended logs, pre-existing)

**E2E adversarial pass** (throwaway tests `qa211*.test.ts` over the real `registry.execute`/`AgentSession`, deleted afterwards; plus one live headless run)
- Happy path (live, fake `TREE_AGENT_HOME`, defaults): the AC's prompt via `make -C apps/harness run ARGS='--cwd ../.. --trust --json'` → exit 0, 3 `codemode` calls, 2 `llm_call` depth 1 (haiku-5.5, 1697/87 and 354/99 tokens), 3 bullets, `agent_end completed`. Same shape as the SWE's recording.
- Depth-1 routing: `llm("x")` → `completeText` request `{model, system: default, input, maxOutputTokens: 2048}`, no `tools` key (`"tools" in req === false`); system/maxTokens override reach `complete<T>` too; `tools:true` at depth 1 → plain completion (PASS). Prompt `undefined`/42/`{}`/`null`/`['x']`/no-arg → `TypeError: llm(prompt): prompt must be a string`, nothing sent (PASS). `llm("")` → empty string forwarded as-is (documented, no error). Options `null` OK; `"str"`, `[]`, `maxTokens` 0/-1/1.5/"5", `model:""`, `schema:'x'|[]`, `tools:'yes'`, `system:5` → named zod error to the script (PASS). Schema mismatch: exactly 2 POSTs, `llm: facts: Expected array, received string` (PASS).
- Schema converter: nested object / array of objects / enum / nullable / anyOf / integer-vs-number → wire schema strict (`additionalProperties:false`, all properties required, optional → `anyOf [T, null]`/`[T,"null"]`), answers parse (PASS). Malformed: `"banana"`, `[]`, `5` → error to the script, nothing sent (PASS). `{type:"banana"}`, `{}`, `$ref` degrade to `z.unknown()` (wire `OpenAiAnyType`, answer accepted; live strict acceptance of that not checked). 3 000-deep schema → `the sandbox crashed: Maximum call stack size exceeded`, isError, process fine (PASS).
- Budgets: 17th call names `maxCalls` (16 succeed); calls that fail provider-side, parse-fail or hit an unknown model COUNT toward `maxCalls`; option/prompt validation errors and depth errors do NOT (documented). 200 000-char prompt passes, 200 001 throws naming `maxInputChars`; system + prompt are counted together (100 000 + 100 001 throws; 100 000 + 100 000 passes), default system not counted; length is UTF-16 code units (😀×100 001 = 200 002 throws). Retry usage counts: two failing schema calls at 150 k/attempt → 3rd and 4th throw `maxTotalTokens ... used 600000`, 4 POSTs, per-call event usage 300 k each. `allSettled` over 20 → 16 fulfilled, 4 rejected naming `maxCalls`. 8 concurrent × 100 k tokens → 7 fulfilled, 8th rejected (in-flight calls finish: overshoot ≤ (concurrency-1) × per-call usage, as documented). Peak in flight 4 for 8 llm and for 4 tools + 4 llm (PASS).
- Depth 2: `maxSubTurns` cap → the script gets a THROWN error `llm() sub-loop stopped at maxSubTurns=3 without an answer`, not partial text; sub-loop tools = `read,bash,edit,write` (no `codemode`); with a deferred MCP tool `tool_search` IS in the sub-loop tools (SWE choice, see Other issues). Gate: read `readOnly` no prompt, write asked (`["codemode","write"]`). Steer queued during a depth-2 read is NOT in the 3 sub-loop requests and IS in the 4th (main loop) request; one `agent_end(completed)`. Abort at the sub-loop's gate → `aborted`, a scripted follow-up `write` never ran (`after.md` absent); abort with the sub-loop provider hanging → `aborted` within 2 ms. Depth-3 / `tools+schema` guards → error to script (SWE tests, re-read).
- Events/cost: order in `--json`/events = `tool_call:codemode, permission:codemode, llm_call×N, tool_result`; the cost reducer test (two models) and the resume equality are in the SWE's tests (re-run green); my teardown probe: cost before = after resume (0.0012).
- No prompts: 41 `llm()` in ask mode → prompter asked `["codemode"]` only; auto mode: classifier (`danger_check`) called once for `codemode`, never for `llm()` (structured requests `["danger_check","llm"]`).
- Main-context accounting (own sentinels `SENT-i-ZQX` in 40 hits, `ANS-i-QQQ` in 40 map answers): roles `user, assistant, tool, assistant`; neither sentinel appears in `session.messages` nor in `store.load()`.
- Sandbox interplay: script deadline 300 ms with `llm()` hung → `the script timed out after 300 ms (codemode.timeoutMs)`, provider's AbortSignal `aborted === true`, one `llm_call` without usage emitted immediately (before `tool_result`); user abort the same. Rejection caught in the script → script continues.
- Mutations (restored, `cmp` identical, sha 6bcbbfe…): (a) register `codemode` in the sub-loop tools → 2 tests fail (llm.test.ts depth-2 read test, session.test.ts depth-2 test); (b) `maxTotalTokens` check disabled → 1 test fails ("scripted usage summing past 400 000").
- Bad settings: `{"codemode":{"llm":{"maxCalls":0,"depth":3}}}` → `invalid settings` listing `codemode.llm.depth` and `maxCalls`, no crash.

**Acceptance criteria**
- [x] PASS — Depth 1: `tests/codemode/llm.test.ts` "reaches completeText with no tools…", "with a schema reaches complete<T>…", "a schema mismatch after complete's one retry…", "ignores {tools: true} at depth 1"; re-verified by my probes above.
- [x] PASS — Budgets: llm.test.ts "the 17th call…", "a 200 001-char prompt…", "scripted usage summing past 400 000…", "Promise.all over 8 llm() calls keeps at most 4 in flight"; mutation (b) kills the tokens test.
- [x] PASS — Depth 2: llm.test.ts depth-2 describe (read/sub-loop tools, gate, maxSubTurns, depth 3, depth 1 config) + session.test.ts "depth 2: the sub-loop's read never prompts…" and "headless depth 2…"; mutation (a) kills the no-`codemode` tests.
- [x] PASS — `llm_call` + cost: llm.test.ts "every call reports depth, model and usage…", session.test.ts "the session cost sums…and survives a resume", "the cost rises with each llm_call…"; TUI `status` refreshes on every event (`app.ts:120-122`).
- [x] PASS — Main-context accounting: session.test.ts "the §10.6 worked example grows the context by exactly 2…" + my sentinel probe.
- [x] PASS — Zero prompts: session.test.ts "ask mode: llm() never prompts…" + my 41-call probe.
- [ ] AWAITING HUMAN — RLM demo (not ticked). Met: ≥ 2 `llm_call` depth 1 (2 in the SWE's recording and in my re-run), final answer 3 bullets (≤ 5), hits not in the main context, `llm_call` reaches `--json`. NOT met: "shows one `codemode` call" — 3 in both runs (first script reads `res.parents` but search_memory's `structuredContent` is `{result: "<json string>"}`; second probes; third unwraps). Not run: TUI rows/cost rising on a live terminal. Also only 1 hit for 'agents' live, so the 20-way fan-out was not exercised live.
- [x] PASS — Cadence green; `agent-loop.ts` 180 lines, no `llm`/`codemode`/`mcp`; TUI boundary grep empty; `decide.ts` is NOT byte-unchanged vs HEAD: the diff is one keyword (`const` -> `export const DEFAULT_MODEL`, needed by `session.ts`, SWE-declared; harmless); `sandbox.ts`/`agent-loop.ts` untouched; the codemode description byte-equals the spec §10.3 text (checked against the extracted spec lines); `pnpm install --frozen-lockfile` up to date; `git status`: no `.env*`, `apps/memory`, spec doc untouched (still untracked).

**Other issues found** (none blocks; orchestrator decides)
1. Schema constraints are silently dropped. `zodFromJsonSchema` ignores `maxItems`/`minItems`/`minimum`/`pattern`/…; the wire schema for `{bullets: {type:"array", maxItems: 2}}` has no `maxItems` and an answer of 5 items is accepted. The demo's "at most 5 bullets" therefore rests on the prompt only, and the SWE's note that `maxItems` was "included" in the live schema is inaccurate. Same family: `additionalProperties: {type:"string"}` and a `"__proto__"` property are dropped (answer `{}`); `enum` of objects can never match (`z.literal` identity). Suggest `z.array().min()/max()` for min/maxItems (or reject unsupported keywords by name).
2. A call torn down while the provider is hung can emit `llm_call` AFTER `agent_end` (abort case: events end `…, tool_result, agent_end, llm_call:d2`; timeout case it lands before `tool_result`). SWE-documented; the TUI drops it, but consumers that treat `agent_end` as last (headless `--json`, JSONL tail) see it.
3. `tool_search` in the depth-2 sub-loop is useless and has a side effect: it activates the tool in the MAIN registry (the main loop's next request lists it) while the sub-loop's own registry copy never gets it (2nd sub request tools unchanged). Suggest excluding `tool_search` from the sub-loop.
4. Open PA decisions confirmed: with the default `maxCalls: 16` the spec's own worked example (limit 40 → 41 calls) rejects at the 17th, so `Promise.all` over the hits fails whole; and the `{result: "<json string>"}` wrapper costs two extra codemode calls (reproduced live).
5. Transcript attribution with two `codemode` calls in one turn is fine: the loop runs calls sequentially and emits each `tool_call` right before it executes (`agent-loop.ts:56,123`).
6. `maxTokens` has no upper clamp (a script can request 1e9 output tokens of a single call; only `maxTotalTokens` after the fact limits it). Nit.

**VERDICT: PASS** (all non-[HUMAN] ACs verified; the [HUMAN] demo AC is left unticked with the unmet "one codemode call" clause recorded above)

### [SWE] 2026-10-25 04:01 — Fold-ins before commit

**Files modified**
- `apps/harness/src/codemode/llm.ts` — `zodFromJsonSchema` forwards `minItems`/`maxItems`, `minLength`/`maxLength`/`pattern`, `minimum`/`maximum`/`exclusiveMinimum`/`exclusiveMaximum`/`multipleOf`; any other keyword (e.g. `uniqueItems`, `format`, `allOf`, `additionalProperties` other than `false`, a `"__proto__"` property, an `enum`/`const` of objects, an unknown `type`, a typeless constraint) throws `TypeError("llm(): schema<.path>: …")` naming it. `{}` and `$ref` stay any value (documented; refs are not resolved). Annotations (`description`, `title`, `default`, `examples`, `$comment`, `$schema`, `$id`, `$defs`) are allowed. The schema is converted right after the options parse, so a rejected schema sends nothing, counts no call, emits no `llm_call`. The depth-2 sub-loop drops `tool_search` as well as `codemode`. `maxTokens` is clamped to `modelInfo().maxOutputTokens` (OpenRouter's `max_completion_tokens`, falling back to the context length when the catalogue has none — so the clamp is always a bound).
- `apps/harness/src/mcp/register.ts` — `toToolResult`: `structuredContent` exactly `{result: <string that parses as JSON>}` → `structured` is the parsed value (`content` unchanged); `{result: "plain text"}`, `{result, other}` and non-string `result` stay as is.
- `apps/harness/src/session.ts` — `#ended` set by `#emit` on `agent_end`, reset at `prompt()` start; `#llmCall` still costs and saves a late call (the `--resume` cost stays true; the JSONL has no `agent_end` entry) but no longer emits it once `agent_end` is out.
- `apps/harness/tests/fixtures/fake-mcp-server.mjs` — `echo` knob `{structured: <value>}` answers it as `structuredContent` (a knob, not a new tool: `client.test.ts` pins the listed tools to `["echo", "read_note"]`).
- Tests: `tests/codemode/llm.test.ts` "clamps maxTokens to the model's output cap", "forwards array, string and number constraints", "rejects a keyword it cannot represent by name; {} and $ref are any value", "llm() schema constraints on the wire › maxItems reaches the provider and a 3-item answer fails the parse" (real `OpenRouterProvider` over `fakeFetch`: wire `properties.bullets` carries `maxItems: 2`; 2 POSTs; `llm: bullets: Array must contain at most 2…`), "› a 2-item answer parses", "› an unsupported keyword is an error to the script: nothing sent, no call counted"; `tests/mcp/register.test.ts` "unwraps FastMCP's {result: <json string>}…", "a script gets the parsed JSON of a {result: <json string>} wrapper…" (stdio fixture); `tests/session.test.ts` "depth 2: the sub-loop has no tool_search, so it never activates a deferred tool in the main registry", "an llm() call settling after an abort reports nothing after agent_end, but is still saved" (provider rejects 20 ms after the abort; red before the fix: the stream ended `agent_end, llm_call`).

**Tests**
- Unit: 803 passing, 0 failing, 44 files (was 793) — `make harness-tests`. Mutations (restored): emitting regardless of `#ended` fails the late-`llm_call` test; dropping the unwrap fails both register tests; before the `tool_search` exclusion the depth-2 test failed.
- Integration: N/A.

**Evidence**
```
$ make harness-lint-fix && make harness-lint-check && make harness-typecheck && make harness-tests && make pre-commit
Checked 98 files in 46ms. No fixes applied.
pnpm exec tsc --noEmit
 Test Files  44 passed (44)
      Tests  803 passed (803)
biome check (harness)....................................................Passed   (prettier, ruff check, ruff format Passed)

# [HUMAN] RLM demo, headless, once, live: make env-status → local; tree-mongodb/mongot up. Repo root, fresh scratch
# TREE_AGENT_HOME with NO settings.json (defaults: maxCalls 16), the AC's prompt verbatim:
$ TREE_AGENT_HOME=<scratch>/home211b make -C apps/harness run ARGS='--cwd ../.. --trust --json' PROMPT="Using one codemode script: …"   → exit 0
event counts: tool_call 1 (codemode), permission 2 (codemode + search_memory, classifier), llm_call 2, tool_result 1, agent_end completed (last event)
codemode #1: search_memory({query:'agents', top_k:20}) → res.parents read directly (no probe) → Promise.all(parents.map(p => llm(one line)))
  → llm(merge, {schema: {bullets: {type:'array', items:{type:'string'}, maxItems: 5}}}) → return bullets
  {"type":"llm_call","depth":1,"model":"anthropic/claude-haiku-5.5","usage":{"inputTokens":1280,"outputTokens":90}}
  {"type":"llm_call","depth":1,"model":"anthropic/claude-haiku-5.5","usage":{"inputTokens":362,"outputTokens":100}}
  tool_result: an array of 2 strings (isError false)
turn_end: 2 bullets + a note that they are not about agents (memory content not copied here)
```
- Headless half met: ONE `codemode` call (was 3: the `{result}` unwrap removed the probe/unwrap round-trips), 2 `llm_call` at depth 1, 2 bullets (≤ 5), `agent_end` last. The live provider accepted `maxItems: 5` in the strict schema (no 400). Memory again returned 1 parent for 'agents', so the 20-way fan-out (and the default `maxCalls` 16 tension) was not exercised live. TUI half left for the human.
- `maxCalls` default unchanged (PA decision pending).
