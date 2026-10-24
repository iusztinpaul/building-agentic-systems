---
id: 204-harness-agent-loop-tools-prompt-headless
status: pending
feature: tree-agent-harness
---

# M1 — `runLoop` with the steering and follow-up queues, the four built-in tools and `ToolRegistry`, the sectioned system prompt with `AGENTS.md` injection, `AgentSession`, and the headless `print` / `--json` CLI

Tags: `harness`, `agent-loop`, `tools`, `enhancement`
Depends on: 203 (types, providers, settings)
Blocks: 205, 208 (trust gating), 209 (the registry)
Implements: ADR-016 §4 (exposure model — the registry side), §12 (MCP stays the only harness↔memory boundary: no memory-app import anywhere under `src/`)

## Scope

1. **Loop** — `src/loop/hooks.ts` (`LoopHooks`, spec §6) and `src/loop/agent-loop.ts` (`runLoop`, the §6
   algorithm verbatim: `maxTurns`, `prepareTurn`, steering drained after each turn's tool results, follow-ups
   drained only when the inner loop would end, `length` handling with the one-shot "Output was cut off.
   Continue.", `length` + tool calls → every call fails with "arguments truncated", sequential execution,
   blocked calls answered to the model, `signal.abort()` → `aborted` with partial text kept, continuation
   driven by `toolCalls.length` never `finish_reason`). The loop file stays ≤ 200 lines (spec §0).
2. **Tools** — `src/tools/registry.ts` (spec §8: `Exposure`, `ToolContext` with `origin` and a `depth`
   counter, `ToolResult` with `structured`, `ToolDefinition`, `ToolRegistry` — duplicate names throw,
   `list(exposure)`, `activate`/`activated`, `schemasForRequest()` = direct ∪ activated via zod → JSON Schema,
   `execute` validates first and answers zod issues as `{isError: true}`). `read.ts`, `bash.ts`, `edit.ts`,
   `write.ts` per the §8 table (read: 2 000 lines / 50 KB per call and a paging hint, `structured`
   `{path, lines}`; bash: `cwd`, inherited env, 120 s default timeout, > 50 KB → head + tail + full output at
   `~/.tree-agent/tmp/<id>.txt` with the path appended, `structured` `{exitCode, stdout, stderr}`; edit:
   exact unique match else an error with the count; write: creates parent dirs). Registry filtering for
   `deferred`/`codemode`/`hidden` works now so 209/210 only add tools. No `tool_search`, no `codemode` here.
3. **Prompt** — `src/prompt/system-prompt.ts` (§7: fixed section order `preamble, tools, rules,
   project_context, skills, mcp_servers, cwd`; `<name>…</name>` tags, preamble untagged; `~/.tree-agent/SYSTEM.md`
   overrides the preamble; `tools` lists direct ∪ activated as `- name: promptSnippet`; `rules` ≤ 8 bullets;
   `cwd` = absolute path + today's date, ISO `YYYY-MM-DD` in UTC). `src/prompt/context-files.ts`:
   `~/.tree-agent/AGENTS.md`, then every ancestor of `cwd` root → cwd, first match per directory among
   `AGENTS.md`, `CLAUDE.md`; each injected as `# <absolute path>\n<content>`; per-file cap
   `contextFiles.maxChars` (40 000, truncated copy), total cap 3×; re-read at session start and on `/reload`.
4. **`src/session.ts`** — `AgentSession` (`prompt`, `steer`, `followUp`, `abort`, `on(event)`), owns both
   queues, builds hooks: `prepareTurn` rebuilds the system prompt (212 adds compaction here),
   `beforeToolCall` answers `{run: true}` for every call until 206 replaces it with the gate (say so in a
   comment), `afterToolCall` is the truncation point. A `SessionStore` interface with an in-memory
   implementation (205 adds JSONL).
5. **Headless + CLI** — `src/headless/print.ts`: the text sink prints the final assistant text to stdout
   (tool activity to stderr), the `--json` sink prints one `AgentEvent` per line. `src/cli.ts` becomes the
   §15 matrix: a positional prompt, piped stdin or `--json` → headless; a TTY with no prompt → TUI (until
   205: print usage, exit 1); no TTY and no prompt → usage, exit 1. Flags landing now: `--model`,
   `--max-turns` (50), `--cwd`, `--trust`, `--json`; the remaining §15 flags are added by their tasks, and an
   unknown flag is an error (exit 1). Extra stdin lines after the first are follow-ups. Exit codes 0 and 1
   (`max_turns`, `error`); 2 arrives with 206. Mode resolution is a pure function
   (`resolveMode(argv, isTTY, stdinIsPipe)`) tested directly; end-to-end CLI tests run offline through a
   test-only provider seam (SWE's choice — an env var or an injectable factory — recorded in the Log).
6. **Trust** (§16) — `~/.tree-agent/trusted.json`; project-level files (the `cwd`-ancestor `AGENTS.md` walk,
   `.tree-agent/settings.json`, later `.tree-agent/mcp.json` and skills) load only when `cwd` is trusted or
   `--trust` / `TREE_AGENT_TRUST=1`; `~/.tree-agent/AGENTS.md` and user settings always load. The TUI
   confirmation is 205; headless never prompts.

**Out of scope (intentional):** the permission gate — until 206 every tool call runs, so the [HUMAN] e2e below
runs in a scratch directory; JSONL persistence, TUI (205); `tool_search`, `codemode` (209, 210); compaction
(212); skills (207); parallel tool execution (spec non-goal).

## Acceptance Criteria

- [ ] Loop (FakeProvider): a `done` with `finish_reason: "stop"` but one tool call continues; `stopReason
      "length"` with tool calls fails every call with "arguments truncated" and runs none; a `length` stop
      without calls appends "Output was cut off. Continue." once and a second `length` ends with `error`;
      a `beforeToolCall` `{run: false}` answers `Blocked: <reason>` as an error tool message and the loop
      continues; `maxTurns` ends with `agent_end(max_turns)`; abort mid-stream keeps the partial text with
      `stopReason "aborted"`; a steering message lands before the next model call and keeps the loop alive
      after a no-tool-call turn; a follow-up re-enters after `agent_end` would have fired; both queues survive
      an abort and are delivered on the next `prompt()`.
- [ ] `agent-loop.ts` is ≤ 200 lines (`wc -l`).
- [ ] Prompt: section order is stable across requests; the base prompt (preamble + 4 tool lines + rules)
      estimates ≤ 900 tokens at `chars / 4`; the context-file walk orders root → cwd, prefers `AGENTS.md` over
      `CLAUDE.md` in the same directory, truncates a 50 000-char file to 40 000 chars with a marker, and the
      `tools` section lists an activated tool only after `activate()`; the `cwd` section carries today's UTC date.
- [ ] Tools: `read` pages (`offset`/`limit`, a 3 000-line file answers 2 000 lines plus a hint); `bash` output
      over 50 KB spills to `~/.tree-agent/tmp/<id>.txt` and the result ends with that path; `edit` with 0 or 2
      matches errors with the count; a call with a missing required param answers the zod issue list as an
      error result; `schemasForRequest()` gains a tool after `activate()`; duplicate `register` throws; with no
      MCP servers the model sees exactly 4 tools.
- [ ] `structured` is set for `read` (`{path, lines}`) and `bash` (`{exitCode, stdout, stderr}`); the model
      message carries only `content`.
- [ ] CLI mode matrix: positional → headless; piped stdin → headless with the first line as the prompt and
      later lines as follow-ups (`printf 'a\nb\n' | tree-agent` runs two prompts in one session on
      FakeProvider); `--json` → JSONL where every line `JSON.parse`s to an `AgentEvent`; TTY + no prompt →
      usage, exit 1 (TUI placeholder); no TTY + no prompt → usage, exit 1; exit 1 on `max_turns`/`error`;
      an unknown flag → exit 1.
- [ ] Trust: in an untrusted `cwd` headless loads only `~/.tree-agent/AGENTS.md`; `--trust` or
      `TREE_AGENT_TRUST=1` adds the project walk; a `cwd` listed in `trusted.json` loads it without flags.
- [ ] `grep -rn "apps/memory\|tree\.memory\|tree\.mcp" apps/harness/src` matches nothing (ADR-008 §5 /
      ADR-016 §12).
- [ ] [HUMAN] In a scratch directory holding an `AGENTS.md` with the line "All dates are timezone aware (UTC
      by default)", `tree-agent --trust --json "Create notes.md with three bullet points about this project,
      then append a fourth, then tell me what the project context says about dates"` shows `write`, `edit`
      and a final text quoting the timezone rule; exit 0.
- [ ] `make harness-lint-fix && make harness-lint-check && make harness-typecheck && make harness-tests &&
      make pre-commit` green.

## User Stories

### Story: A CI script runs a multi-step file edit headless
1. `cat task.md | tree-agent --json --trust` where `task.md` asks for a new file and one edit to it.
2. stdout shows `turn_start`, `text_delta`s, a `tool_call` for `write`, its `tool_result`, a `tool_call` for
   `edit`, `turn_end`, `agent_end` with `reason: "completed"`; exit 0.

### Story: The user steers mid-run
1. Through `AgentSession`, a prompt is running; `steer("use pnpm, not npm")` is called while the model streams.
2. The loop finishes the current turn's tool results, pushes the steering text as a user message, and the next
   request to the provider contains it — the recorded `ChatRequest` proves the order.

### Story: The model's tool call arrives truncated
1. The provider ends a turn with `finish_reason: "length"` and one partial tool call.
2. No tool runs; the tool message says "arguments truncated"; the model gets another turn.

### Story: `AGENTS.md` reaches the prompt
1. `~/.tree-agent/AGENTS.md`, `/repo/AGENTS.md` and `/repo/sub/CLAUDE.md` exist; `cwd` is `/repo/sub`, trusted.
2. The recorded system prompt's `project_context` section lists them in that order, each under `# <abs path>`.

### Story: Two prompts from stdin
1. `printf 'What is 2+2?\nAnd 3+3?\n' | tree-agent`.
2. The second line is delivered as a follow-up after the first completes; two assistant answers are printed.

---

Blocked by: 203

## Log

### [PA] 2026-10-24 19:52 — Grooming

**Summary**
The agent core: the ~200-line loop with both queues, the four built-in tools behind a registry that already
understands exposure, the sectioned system prompt with AGENTS.md injection and trust, and the headless CLI that
drives it all — the largest task of the plan, grouped by spec row.

**Key decisions**
- The registry ships exposure filtering now so 209/210 only register tools; `tool_search` and `codemode`
  themselves wait for the tools that justify them (spec §8: registered only when ≥ 1 deferred/codemode tool).
- `beforeToolCall` is a documented pass-through until 206 — the milestone order is the spec's; the e2e runs in
  a scratch dir for that reason.
- Trust is wired headless-first (flag/env/`trusted.json`); the TUI dialog belongs to 205.
- Unknown flags are errors so each later task adds its flags deliberately.

**Dependencies**
- 203 — `types.ts`, `Provider`/`FakeProvider`, `Settings`.

**User stories**
- 5 stories covering: a headless multi-step edit, steering order, truncated arguments, the AGENTS.md walk,
  stdin follow-ups.

Ready for implementation.
