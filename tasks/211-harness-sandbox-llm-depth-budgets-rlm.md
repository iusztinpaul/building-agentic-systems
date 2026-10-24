---
id: 211-harness-sandbox-llm-depth-budgets-rlm
status: pending
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

- [ ] Depth 1: `llm("x")` reaches `completeText` with no `tools` and the default system prompt; with
      `schema` it reaches `complete<T>` and returns the parsed object; a schema mismatch after the retry throws
      inside the script with the parse message; `tools: true` at depth 1 is ignored (no sub-loop).
- [ ] Budgets: the 17th `llm()` in one script throws naming `maxCalls`; a 200 001-char prompt throws naming
      `maxInputChars`; scripted `usage` summing past 400 000 throws naming `maxTotalTokens`;
      `Promise.all(8 llm calls)` shows ≤ 4 in flight.
- [ ] Depth 2 (`codemode.llm.depth: 2`): a sub-call can `read` a file it was handed (FakeProvider scripts a
      `read` tool call inside the sub-loop) and the sub-loop stops at `maxSubTurns`; the sub-loop's tools array
      has no `codemode`; a non-read-only tool inside the sub-loop goes through the gate; with depth 1
      configured, `llm("…", {tools: true})` answers from the prompt only and a call that would need depth 3
      returns an error to the script.
- [ ] Every call emits `llm_call` with `depth`, `model`, `usage`; the session cost in the status bar includes
      them (unit: the cost reducer sums `llm_call` usage × pricing).
- [ ] Main-context accounting: running the §10.6 worked example on FakeProvider (40 scripted hits, 40 map
      answers, 1 reduce answer) grows `ctx.messages` by exactly 2 (the assistant `codemode` call and its tool
      result), and no hit text appears anywhere in `ctx.messages`.
- [ ] `llm()` never triggers a permission prompt in `ask` mode (FakePrompter records zero asks).
- [ ] [HUMAN] (`make local-start`, live key) the RLM demo: `tree-agent --json "Using one codemode script:
      search my memory for 'agents' with limit 20, ask llm() for a one-line summary of each hit in parallel,
      then ask llm() with a JSON schema to merge them into at most 5 bullet points, and return only the
      bullets"` shows one `codemode` call, ≥ 2 `llm_call` events (depth 1), and a final answer of ≤ 5 bullets;
      the TUI shows the `llm_call` rows nested under the card and the cost rising.
- [ ] `make harness-lint-fix && make harness-lint-check && make harness-typecheck && make harness-tests &&
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
