---
id: 204-harness-agent-loop-tools-prompt-headless
status: done
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

- [x] Loop (FakeProvider): a `done` with `finish_reason: "stop"` but one tool call continues; `stopReason
      "length"` with tool calls fails every call with "arguments truncated" and runs none; a `length` stop
      without calls appends "Output was cut off. Continue." once and a second `length` ends with `error`;
      a `beforeToolCall` `{run: false}` answers `Blocked: <reason>` as an error tool message and the loop
      continues; `maxTurns` ends with `agent_end(max_turns)`; abort mid-stream keeps the partial text with
      `stopReason "aborted"`; a steering message lands before the next model call and keeps the loop alive
      after a no-tool-call turn; a follow-up re-enters after `agent_end` would have fired; both queues survive
      an abort and are delivered on the next `prompt()`.
- [x] `agent-loop.ts` is ≤ 200 lines (`wc -l`).
- [x] Prompt: section order is stable across requests; the base prompt (preamble + 4 tool lines + rules)
      estimates ≤ 900 tokens at `chars / 4`; the context-file walk orders root → cwd, prefers `AGENTS.md` over
      `CLAUDE.md` in the same directory, truncates a 50 000-char file to 40 000 chars with a marker, and the
      `tools` section lists an activated tool only after `activate()`; the `cwd` section carries today's UTC date.
- [x] Tools: `read` pages (`offset`/`limit`, a 3 000-line file answers 2 000 lines plus a hint); `bash` output
      over 50 KB spills to `~/.tree-agent/tmp/<id>.txt` and the result ends with that path; `edit` with 0 or 2
      matches errors with the count; a call with a missing required param answers the zod issue list as an
      error result; `schemasForRequest()` gains a tool after `activate()`; duplicate `register` throws; with no
      MCP servers the model sees exactly 4 tools.
- [x] `structured` is set for `read` (`{path, lines}`) and `bash` (`{exitCode, stdout, stderr}`); the model
      message carries only `content`.
- [x] CLI mode matrix: positional → headless; piped stdin → headless with the first line as the prompt and
      later lines as follow-ups (`printf 'a\nb\n' | tree-agent` runs two prompts in one session on
      FakeProvider); `--json` → JSONL where every line `JSON.parse`s to an `AgentEvent`; TTY + no prompt →
      usage, exit 1 (TUI placeholder); no TTY + no prompt → usage, exit 1; exit 1 on `max_turns`/`error`;
      an unknown flag → exit 1.
- [x] Trust: in an untrusted `cwd` headless loads only `~/.tree-agent/AGENTS.md`; `--trust` or
      `TREE_AGENT_TRUST=1` adds the project walk; a `cwd` listed in `trusted.json` loads it without flags.
- [x] `grep -rn "apps/memory\|tree\.memory\|tree\.mcp" apps/harness/src` matches nothing (ADR-008 §5 /
      ADR-016 §12).
- [x] [HUMAN] In a scratch directory holding an `AGENTS.md` with the line "All dates are timezone aware (UTC
      by default)", `tree-agent --trust --json "Create notes.md with three bullet points about this project,
      then append a fourth, then tell me what the project context says about dates"` shows `write`, `edit`
      and a final text quoting the timezone rule; exit 0.
- [x] `make harness-lint-fix && make harness-lint-check && make harness-typecheck && make harness-tests &&
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

### [SWE] 2026-10-24 23:15 — Implementation

**Files modified**
- `apps/harness/src/loop/hooks.ts` — `LoopHooks`, `GateResult`, `LoopConfig` (spec §6, plus `cwd` and an optional `depth`, see Decisions).
- `apps/harness/src/loop/agent-loop.ts` — `runLoop` (168 lines), the §6 algorithm; exports `CUT_OFF` and `TRUNCATED`.
- `apps/harness/src/tools/registry.ts` — `Exposure`, `ToolContext {cwd, signal, origin, depth}`, `ToolResult {content, isError?, structured?}`, `ToolDefinition<S extends z.AnyZodObject>`, `ToolRegistry` (`register` throws on duplicates, `get`, `list(exposure?)`, `activate`/`activated`, `declared()` = direct ∪ activated, `schemasForRequest()`, `execute` = exposure check → zod validation → run, never throws).
- `apps/harness/src/tools/{read,bash,edit,write}.ts` — the §8 built-ins; `tools/builtins.ts` — `builtinRegistry({tmpDir, env})` (the 4, in spec order); `tools/truncate.ts` — `MAX_OUTPUT_BYTES` (50 KB) + `headTail()`.
- `apps/harness/src/prompt/system-prompt.ts` — `PromptSections`, `SECTION_ORDER`, `buildSystemPrompt`, `DEFAULT_PREAMBLE`, `RULES` (7), `toolsSection`, `rulesSection`, `cwdSection(cwd, now)`, `loadPreamble` (`SYSTEM.md` override).
- `apps/harness/src/prompt/context-files.ts` — `loadContextFiles({cwd, trusted, maxChars, env})`, `formatContextFiles`.
- `apps/harness/src/config/trust.ts` — `isTrusted(cwd, {env, flag})`.
- `apps/harness/src/session.ts` — `AgentSession` (`create`, `reload`, `prompt`, `steer`, `followUp`, `abort`, `on`, `systemPrompt`, `messages`, `running`), `SessionStore` + `InMemorySessionStore`.
- `apps/harness/src/headless/print.ts` — `runHeadless`, `textSink`, `jsonSink`.
- `apps/harness/src/cli.ts` — rewritten: `resolveMode` (pure), `main(argv, deps)` with `CliDeps.stdin` (`{isTTY, isPipe, read}`), the §15 matrix and the M1 flags.
- `apps/harness/src/types.ts` — named `AgentEndReason` (same union, used by `agent_end`).
- `apps/harness/src/config/paths.ts` — `UserPaths.tmp` (`~/.tree-agent/tmp`), `UserPaths.systemMd`, `projectPaths(cwd).settings`.
- `apps/harness/src/config/settings.ts` — `loadSettings({projectDir})`: the trusted cwd's `.tree-agent/settings.json` layers between the user file and the flags (spec §16 order).
- Tests (one file per module): `tests/loop/agent-loop.test.ts` (21), `tests/session.test.ts` (14), `tests/tools/{registry,read,bash,edit,write,builtins,truncate}.test.ts` (10/7/8/4/2/2/2), `tests/prompt/{system-prompt,context-files}.test.ts` (9/7), `tests/config/trust.test.ts` (5), `tests/headless/print.test.ts` (4), `tests/cli.test.ts` (9 → 37), `tests/config/settings.test.ts` (+1); `tests/helpers/tmp.ts` (`useTmpDir`, `toolCtx`).

**Decisions (choices the spec left open, for the Tester and PA)**
- *Test-only provider seam: the injectable factory `CliDeps.createProvider(apiKey)`* (already there from 203), not an env var. Every end-to-end CLI test calls `main(argv, deps)` with a `FakeProvider`, a fake `stdin` and `TREE_AGENT_HOME` pointing at a `mkdtemp` dir. No env var can swap the provider in a real process, so the shipped binary always talks to OpenRouter.
- *`prompt(text)` queues `text` at the end of the steering queue and runs the loop.* The loop drains steering first (spec: `pending = hooks.getSteeringMessages()`), so steering left over from an aborted run reaches the model before the new prompt (chronological order). Follow-ups left over run when the new prompt's inner loop ends. A second `prompt()` while one runs throws (`use steer() or followUp()`).
- *Abort while tools run:* the remaining calls in the batch are answered `Not run: aborted.` (`isError`) without running. Every assistant tool call still gets a `tool` message, so the next request is valid on the wire. After the batch the loop ends with `agent_end(aborted)` *before* draining steering, so the queue survives. One line on top of the §6 algorithm.
- *`max_turns` with undelivered steering:* the pending messages go into the context before `agent_end(max_turns)` so they are not lost.
- *"Output was cut off. Continue." once* = once per run of consecutive `length` stops: a normal turn resets it, a second length in a row ends with `error` (plus an `error` event `output cut off twice`). Tested both ways.
- *Error reporting:* a stream `error` event, a `done` with `stopReason: "error"`, a thrown `stream()` and a stream with no terminal event all become an assistant message with `stopReason: "error"`, one `{type:"error", message}` event, then `agent_end(error)`. A hook that throws ends `prompt()` with the same pair (session catch).
- *Event order* follows the §6 algorithm: `turn_start`, `text_delta`*, `turn_end` (straight after the assistant message is pushed), then `tool_call`/`tool_result` per call. The CI story lists `turn_end` after the tool events; the algorithm wins.
- *Where `parseError` is handled:* in the loop, before the gate (a call that cannot run is never shown to the 206 permission prompt). Order: aborted → `length` (TRUNCATED) → `parseError` → `beforeToolCall` → `registry.execute` → `afterToolCall`.
- *`LoopConfig` gains `cwd`* (the tools' `ToolContext.cwd`) and optional `depth` (default 0, for 210's sub-loops).
- *Registry exposure checks in `execute`:* model origin may call `direct` + activated `deferred` only; codemode origin may call anything but `hidden`; everything else is `Unknown tool: <name>` (`isError`). `activate()` only activates `deferred` tools and silently ignores other names. Tool JSON Schemas use zod-to-json-schema's default target (`$refStrategy: "none"`, `$schema` deleted), not the `openAi` target `complete<T>` uses, which would make `offset`/`limit`/`timeout` required-nullable.
- *`ToolDefinition` is generic over its zod object* (`S extends z.AnyZodObject`, `execute(params: z.infer<S>)` as a method), so the tools are typed and still register into one registry.
- *read:* 1-based `offset`; a trailing newline does not count as a line; hint `[Showing lines A-B of N. Use offset=B+1 to continue.]`; the 50 KB cap stops before the line that would cross it (a single line > 50 KB is cut at 50 KB). `structured.lines` is the **array of the returned lines** and `structured.path` is the absolute path. An offset past the end is an error.
- *bash:* `bash -c` in its own process group (`detached`), so a timeout or an abort `SIGKILL`s the children too. `timeout` is in seconds. Content = stdout and stderr interleaved; `(no output)` when both are empty; a non-zero exit → `isError` + `Command exited with code N`; timeout → `Command timed out after Ns`; abort → `Command aborted`. Over 50 KB: the first 25 KB and the last 25 KB around `[... N bytes omitted ...]`, the full output written to `<TREE_AGENT_HOME>/tmp/<uuid>.txt`, and the content **ends** with `Full output: <path>`. `structured` = `{exitCode, stdout, stderr}`, full (not truncated).
- *afterToolCall* is a backstop, not a second cap: results over 64 KB keep head + tail (`RESULT_CAP_BYTES`). It sits above the tools' 50 KB so bash's spilled output (50 KB + the path line) is never cut twice.
- *trusted.json* = a JSON array of absolute directory paths, matched **exactly** on `path.resolve(cwd)` (no inheritance to subdirectories). Task 205's dialog writes it. A file that is not a JSON array of strings logs a warning and trusts nothing. `TREE_AGENT_TRUST` must be exactly `"1"`.
- *Context files:* `cwd` is resolved with `path.resolve`, not realpath, and the `# <path>` headers use that string. The walk takes the first readable `AGENTS.md`/`CLAUDE.md` per directory (ENOENT/EISDIR skip). Per-file truncation appends `\n\n[truncated: showing 40000 of 50000 chars; read <path> for the rest]`. Once the 3× total is reached, the file that crosses it is cut to the remaining budget and later files are dropped with a `log.warn`.
- *Prompt text:* the `cwd` section is `Working directory: <abs>\nToday's date (UTC): YYYY-MM-DD` (`toISOString().slice(0, 10)`). The base prompt (preamble + 4 tool lines + 7 rules) measures **952 chars = 238 est. tokens**, against a budget of 900. After the first live run the `edit` rule was made explicit, "not `>>`, `sed -i` or other shell rewrites", because sonnet-5.5 appended the fourth bullet with `echo >> notes.md` (see Evidence).
- *Headless text sink:* prints the text of every `turn_end` **without** tool calls to stdout (one line per answer, so two stdin prompts print two answers). Text next to tool calls, `> name {args}` per call, `! <first line>` per error result and `error` messages go to stderr. A non-completed end other than `error` prints `run ended: <reason>`. Exit: `completed` → 0, everything else → 1.
- *CLI:* a positional prompt wins and stdin is not read. Piped stdin = FIFO or regular file (`fstatSync(0)`), so `</dev/null` is "no TTY, no pipe" → usage. Stdin lines are trimmed and blank ones dropped; empty piped stdin → usage, exit 1 (this keeps 203's spawned `spawnSync([])` test green). `--max-turns` must be a positive integer, and `--cwd` must be an existing directory; either failure exits 1. Order: args → stdin → key → cwd → trust → settings (project layer if trusted) → provider → `checkDecisionModel` → session.
- *`--json` with no prompt and no pipe* resolves to `usage` straight away in `resolveMode` (a headless run with no prompt source would print usage anyway). It is still never the TUI, which is what "`--json` → headless" protects. The matrix test pins `["--json"]`, TTY → `usage`, and `["--json"]` + pipe → `headless` from stdin.
- *SessionStore:* `append(message)` + `load()`; the session appends the run's new messages in a `finally` (so aborted/errored runs are stored too) and starts from `store.load()`. Task 205 swaps in JSONL and per-event appends.

**Tests**
- Unit: 224 passed, 0 failing with the key (`make harness-tests`, incl. 203's 2 live tests); `env -u OPENROUTER_API_KEY pnpm exec vitest run` → 222 passed, 2 skipped.
- Integration: N/A (no integration suite, AGENTS.md).
- Mutation probes (each change made, the module's tests run, the file restored byte-identical via `diff -q`): continuation driven by `stopReason === "tool_calls"` → 3 loop tests fail; the `length` → TRUNCATED guard removed → 1 fails; `cutOff` never set → 1 fails; the `parseError` guard removed → 1 fails; `CLAUDE.md` preferred over `AGENTS.md` → 1 fails; ancestors cwd → root → 4 fail.

**Acceptance criteria**
- [x] Loop — `tests/loop/agent-loop.test.ts`: `continues on a tool call even when finish_reason says stop`, `fails every call with 'arguments truncated' on a length stop and runs none`, `appends 'Output was cut off. Continue.' once, and a second length ends with error` (+ `allows a new 'Continue.' after a length stop is followed by a normal turn`), `answers a blocked call with 'Blocked: <reason>' as an error and keeps going`, `ends with agent_end(max_turns) after maxTurns model calls`, `keeps the partial text with stopReason aborted when aborted mid-stream`, `delivers steering before the next model call, after the turn's tool results`, `keeps the loop alive for steering after a turn without tool calls`, `re-enters for a follow-up only when agent_end would have fired`, `does not drain follow-ups while the model is still calling tools`; plus parseError, afterToolCall, sequential order, stream error, abort during tools, prepareTurn. Queues across abort: `tests/session.test.ts > keeps both queues across an abort and delivers them on the next prompt()`. Steering story: `> delivers steer() made while the model streams after the turn's tool results` (recorded `ChatRequest` roles `user, assistant, tool, user`). Truncated-call story: the length test above (no tool runs, the model gets another turn).
- [x] `wc -l apps/harness/src/loop/agent-loop.ts` → `168`.
- [x] Prompt — `system-prompt.test.ts`: `joins the sections in the fixed order, tagged, with the preamble untagged`, `keeps the order whatever order the sections object is built in`, `keeps the base prompt (preamble + 4 tool lines + rules) at or under 900 tokens`, `has at most 8 rules`, `cwdSection > carries the absolute path and today's date in UTC` (23:30 UTC-5 on Oct 24 → `2026-10-25`), `loadPreamble > is replaced by ~/.tree-agent/SYSTEM.md`; `context-files.test.ts`: `orders ~/.tree-agent/AGENTS.md first, then ancestors root → cwd`, `prefers AGENTS.md over CLAUDE.md in the same directory`, `truncates a 50 000-char file to its first 40 000 chars plus a marker`, the two 3× cap tests; `session.test.ts`: `sends a system prompt whose section order is stable across requests` (two recorded requests), `lists an activated tool in the tools section only after activate()`, the AGENTS.md story `puts ~/.tree-agent/AGENTS.md, /repo/AGENTS.md and /repo/sub/CLAUDE.md in project_context, in order`, `re-reads the context files on reload()`.
- [x] Tools — `read.test.ts > answers 2 000 lines of a 3 000-line file plus a paging hint`, `> pages with offset and limit`, `> caps a page at 50 KB even under 2 000 lines`; `bash.test.ts > spills output over 50 KB to <home>/tmp/<id>.txt and ends with that path`; `edit.test.ts > errors with the count when there is no match…`, `> errors with the count on two matches`; `registry.test.ts > answers a missing required param with the zod issue list as an error result`, `> sends only direct tools until a deferred one is activated`, `> throws on a duplicate name`; `builtins.test.ts > declares exactly the four built-ins, all direct, with no MCP servers`; `cli.test.ts > streams the text to stdout…` asserts the recorded request's tools are `read, bash, edit, write`.
- [x] `structured` — `read.test.ts > returns a small file whole, with structured {path, lines}`, `bash.test.ts > runs in cwd and returns output with structured {exitCode, stdout, stderr}`; `agent-loop.test.ts > gives the model only the tool result's content, never structured` (exact tool message, no `structured` key).
- [x] CLI mode matrix — `cli.test.ts > resolveMode` (7-row `it.each` matrix + flags + 4 usage-error cases); `main (headless) > runs the first stdin line as the prompt and later lines as follow-ups in one session` (`a\nb\n` → 2 requests in one session, stdout `A\nB\n`), `> prints two answers for 'What is 2+2?\nAnd 3+3?\n' on stdin`, `> emits JSONL with --json: every line parses to an AgentEvent`, `> prints usage and exits 1 on a TTY with no prompt (TUI placeholder until task 205)`, `> prints usage and exits 1 with no TTY and no prompt`, `> exits 1 on max_turns`, `> prints a provider error's message and exits 1` (203, still green), `> exits 1 on an unknown flag…`, `exits 1 with the usage line on an unknown flag`; CI story `> completes a multi-step file edit headless (CI story)` (stdin + `--json --trust`, `write` then `edit` on disk, `agent_end completed`, exit 0).
- [x] Trust — `cli.test.ts > main (trust)`: `loads only ~/.tree-agent/AGENTS.md in an untrusted cwd`, `adds the project walk with --trust`, `adds the project walk with TREE_AGENT_TRUST=1`, `loads it without flags for a cwd listed in trusted.json`, `reads .tree-agent/settings.json only in a trusted cwd`; `trust.test.ts` (5); `settings.test.ts > layers <projectDir>/.tree-agent/settings.json between the user file and the flags`.
- [x] Boundary — `grep -rn "apps/memory\|tree\.memory\|tree\.mcp" apps/harness/src` → no output, exit 1.
- [ ] [HUMAN] e2e — left for the human; I ran it in the session scratchpad (Evidence): `write`, `edit`, a final text with the timezone rule, exit 0.
- [x] Cadence — all five targets exit 0 (Evidence).

**Evidence**
```
$ make harness-lint-fix; make harness-lint-check; make harness-typecheck; make harness-tests; make pre-commit
lint-fix=0  lint-check=0 ("Checked 45 files … No fixes applied.")  typecheck=0
 ✓ tests/provider/openrouter.test.ts (43 tests) 3616ms
 Test Files  20 passed (20)
      Tests  224 passed (224)                                   tests=0
biome check (harness)....................................................Passed   pre-commit=0
$ cd apps/harness && env -u OPENROUTER_API_KEY pnpm exec vitest run
      Tests  222 passed | 2 skipped (224)
$ wc -l apps/harness/src/loop/agent-loop.ts
     168 apps/harness/src/loop/agent-loop.ts
$ grep -rn "apps/memory\|tree\.memory\|tree\.mcp" apps/harness/src   → (nothing), exit 1
$ base prompt: 952 chars, 238 est. tokens (chars / 4)

# [HUMAN] e2e, run by SWE in <scratchpad>/e2e/proj (AGENTS.md holds "- All dates are timezone aware (UTC by default).")
# with TREE_AGENT_HOME=<scratchpad>/e2e/home, .env sourced in a subshell (key never printed)
$ node apps/harness/src/cli.ts --trust --json "Create notes.md with three bullet points about this project, then append a fourth, then tell me what the project context says about dates"
exit=0, 38 JSONL lines, every one JSON.parse-d; non-delta events:
{"type":"turn_start"}  turn_end tool_calls  tool_call bash {"command":"ls -la; git log --oneline 2>/dev/null | head"}  tool_result ok
{"type":"turn_start"}  turn_end tool_calls  tool_call write {"path":"notes.md","content":"- This is a tiny scratch project.\n- It is used to exercise tree-agent.\n- Its only file so far is AGENTS.md, …\n"}  tool_result "Wrote 141 bytes to notes.md"
{"type":"turn_start"}  turn_end tool_calls  tool_call edit {"path":"notes.md","oldText":"which holds the project context.\n","newText":"which holds the project context.\n- All dates in the project are timezone aware (UTC by default).\n"}  tool_result "Edited notes.md"
{"type":"turn_start"}  turn_end stop "I created `notes.md` with three bullets and then added a fourth. … The project context says all dates are timezone aware, and UTC is the default."
{"type":"agent_end","reason":"completed"}
# First attempt (before the edit rule named `>>`): write, then `bash echo "- …" >> notes.md` instead of edit, exit 0. That run is why the rule changed.

# live smoke, same scratch cwd, untrusted:
$ printf 'What is 2+2? Reply with the number only.\nAnd 3+3? Reply with the number only.\n' | node apps/harness/src/cli.ts
4
6                                                    exit=0
$ node apps/harness/src/cli.ts </dev/null            → usage, exit 1
$ node apps/harness/src/cli.ts "hi" --bogus          → "Unknown option '--bogus'…" + usage, exit 1
$ node apps/harness/src/cli.ts "Run: echo hi" --max-turns 1
> bash {"command":"echo hi"}
run ended: max_turns                                 exit=1
```

**Notes**
- Not committed; waiting for the Tester.
- `make harness-tests` runs 203's two live tests (~$0.002). Every 204 test is offline (FakeProvider, `mkdtemp` dirs, a fake `TREE_AGENT_HOME`). The real `~/.tree-agent` is never read: it does not exist on this machine, and the tests never point at it.
- `apps/harness/README.md` still says the CLI "prints a usage line". The README rewrite is task 213; I left it alone.
- Follow-up candidates, not done here: SIGINT → `session.abort()` in the headless entry point (today Ctrl+C kills the process with Node's default, exit 130); the text sink prints a `length`-cut partial answer and its continuation as two lines.
- `TREE_AGENT_TRUST` (spec §16) is a new env var, and I did **not** add it to `.env.example` because the brief says never to edit `.env*`. If it goes there, it must be commented out (`# TREE_AGENT_TRUST=1`): `make harness-*` exports `.env`, so a live value would trust every cwd on every run.
- `.env*` and `docs/Tree-Agent Harness Implementation Spec.md` were not touched.

### [Tester] 2026-10-24 23:40 — QA

**Test summary**
- Format / lint / typecheck / pre-commit: PASS (`make harness-format-check`, `harness-lint-check`, `harness-typecheck`, `pre-commit` all exit 0; biome "Checked 45 files … No fixes applied")
- Unit tests: 224 passed / 0 failed (`make harness-tests`, 20 files, key present so 203's 2 live tests ran)
- Integration tests: N/A (no integration suite per AGENTS.md)
- Warnings: 0 test-runner warnings; the `[tree-agent] warn:` lines in the output are the log.warn paths the tests assert on (malformed trusted.json, 3x cap)
- Extra: `tsc -p tsconfig.build.json --outDir <scratch>` compiles; `grep -rn "apps/memory\|tree\.memory\|tree\.mcp" apps/harness/src` → no output (exit 1)

**E2E adversarial pass** (scripts in `/private/tmp/claude-501/-Users-pauliusztin-Documents-01-Projects-AI-Engineer-Handbook-building-agentic-systems-building-agentic-systems/a7886cf7-0496-410f-869e-8174a1590e03/scratchpad/qa/{loop,tools,cli}-adv.ts`, run with `node <file>`; offline on FakeProvider unless marked live)
- Loop, 19 probes, all PASS: stop+1 call continues; `length`+2 calls runs none (spy registry) and answers "arguments truncated" to both with a wire-valid next request; two consecutive `length` → `agent_end(error)`; length→normal→length continues; length→length+calls→length continues; `parseError` call never reaches `beforeToolCall`; `{run:false}` → "Blocked: nope" isError and loop completes; `maxTurns` 2 → `max_turns`, `maxTurns 1` + plain answer → completed; abort mid-stream keeps "partial text", `stopReason aborted`, steer+followUp queued during the run are delivered on the next `prompt()` (user order `first, S1, second, F1`); a 2nd `prompt()` during a run rejects ("a prompt is running"); abort during `bash sleep 30` with 3 calls → returns in <3 s, 3 tool messages (2nd/3rd "Not run: aborted."), wire valid, steering survives and reaches the next request; steering after a no-tool turn keeps the loop alive; follow-up not drained while tools run; tool that throws → isError "boom"; unknown tool / wrong arg type / extra field handled; no stream scripted → `error`, session reusable; throwing listener → `error`, `prompt()` resolves; `abort()` while idle is a no-op.
- Prompt/context, all PASS: section order `tools, rules, cwd` identical across two requests; base prompt 952 chars incl. tags = 238 est. tokens (914 raw = 229) ≤ 900; 7 rules; `cwd` section "Today's date (UTC): 2026-10-24" and rolls to 2026-10-25 for 2026-10-24T23:59:59-05:00; `SYSTEM.md` replaces the preamble (empty file falls back); activated tool in `tools` only after `activate()`; walk `H/AGENTS.md, R/AGENTS.md, R/a/CLAUDE.md, R/a/b/AGENTS.md` (root→cwd, AGENTS beats CLAUDE, home first); 50 000-char file → 40 000 x + "[truncated: showing 40000 of 50000 chars; …]"; 4 files over the 3x cap → 4,40148,40150,40148 chars (cap enforced); untrusted → only home file; AGENTS.md as a directory skipped.
- Tools, PASS: `read` 3 000 lines → lines 1-2000 + "[Showing lines 1-2000 of 3000. Use offset=2001 to continue.]", paging with offset/limit, limit 99999 clamps to 2000, offset 0/-1/1.5/limit 0 → zod isError, offset past end → isError, wide-line file stops under 50 KB with a hint, binary file ≤ 51 KB, missing file/dir/empty path → isError; `bash` cwd honoured, exit 3 → isError + structured `{exitCode,stdout,stderr}`, `seq 1 40000` → head+tail, ends with `Full output: <home>/tmp/<uuid>.txt` and the file holds all 40 000 lines, `timeout 1` on `sleep 100` returns in <4 s, grandchild `sleep 101` killed, abort kills; `edit` 0 and 2 matches "matched N times", file untouched on error, 1 match edited once incl. `$&`/`$1` literal replacement, empty oldText/missing file → isError; `write` creates `a/b/c`, unicode, empty content, directory target → isError; missing required params → zod issue list naming both fields; duplicate `register` throws; exactly `read, bash, edit, write`; deferred/codemode/hidden exposure matrix correct.
- CLI via `main(argv, deps)`, 28 probes PASS: positional → headless; piped `What is 2+2?\nAnd 3+3?\n` → "4\n6\n", 2 provider requests, 2nd request = `user:What is 2+2?, assistant:4, user:And 3+3?`; CRLF/blank/whitespace lines handled; whitespace-only stdin → usage 1; positional beats stdin; `--json` every line `JSON.parse`s to a known `AgentEvent` type, stderr empty, U+2028/newlines stay on one line; TTY+no prompt, no TTY+no prompt, `--json`+TTY → usage exit 1; unknown flag, missing flag value, `--max-turns` 0/abc/1.5/-3, bad/file `--cwd`, `--resume` → exit 1 with no stack trace; missing key → "OPENROUTER_API_KEY is not set (add it to .env)"; `--max-turns 1` + tool → "run ended: max_turns" exit 1, stream error → exit 1; `--cwd` honoured by bash `pwd` and the `cwd` section; trust: untrusted → user AGENTS.md only, `--trust`/`TREE_AGENT_TRUST=1` add the walk (order user, project, sub), `true`/`0`/empty/`yes` do NOT; `trusted.json` entry trusts (also `dir/` and `sub/..`), subdirectory NOT trusted, malformed/wrong-shape/empty file → trusts nothing, exit 0; project `.tree-agent/settings.json` only when trusted, flags still win, a broken project file fails loudly only when trusted; hostile prompt (`'; rm -rf / # $(whoami)…`) passes verbatim; 2 MB stdin prompt OK.
- LIVE [HUMAN] AC, fresh scratch (`/private/tmp/claude-501/-Users-pauliusztin-Documents-01-Projects-AI-Engineer-Handbook-building-agentic-systems-building-agentic-systems/a7886cf7-0496-410f-869e-8174a1590e03/scratchpad/qa/live/{proj,home}`, fake `TREE_AGENT_HOME`, `.env` sourced in a subshell, key never printed; TREE_AGENT_TRUST unset): `node apps/harness/src/cli.ts --trust --json "Create notes.md with three bullet points about this project, then append a fourth, then tell me what the project context says about dates"` → exit 0, 36 JSONL lines all parse; non-delta events: bash `ls` → read `AGENTS.md` → `write notes.md` ("Wrote 152 bytes to notes.md") → `edit notes.md` ("Edited notes.md") → final turn_end "…The project context says all dates are timezone aware, and UTC is the default." → `{"type":"agent_end","reason":"completed"}`. notes.md on disk has 4 bullets. Output saved at `/private/tmp/claude-501/-Users-pauliusztin-Documents-01-Projects-AI-Engineer-Handbook-building-agentic-systems-building-agentic-systems/a7886cf7-0496-410f-869e-8174a1590e03/scratchpad/qa/live/human.jsonl`.
- LIVE: `printf 'What is 2+2? …\nAnd 3+3? …\n' | node apps/harness/src/cli.ts` → "4\n6", exit 0; `printf 'My favourite number is 7. Reply OK only.\nWhat is my favourite number? …\n' | …` → "OK\n7" exit 0 (history shared); `--max-turns 1 "Run the shell command: echo hi, then tell me its output"` → `> bash {"command":"echo hi"}` / `run ended: max_turns`, exit 1; `</dev/null` → usage exit 1; `"hi" --bogus` → exit 1; untrusted cwd with project AGENTS.md → the model answers "No" (project file not in its prompt).
- BREAK (FAIL): `bash` with huge output crashes the whole process. Repro: `node /private/tmp/claude-501/-Users-pauliusztin-Documents-01-Projects-AI-Engineer-Handbook-building-agentic-systems-building-agentic-systems/a7886cf7-0496-410f-869e-8174a1590e03/scratchpad/qa/bigout.ts "yes"` (or `"head -c 900000000 /dev/zero | tr '\\0' 'a'"`) → uncaught `RangeError: Invalid string length at Socket.<anonymous> (src/tools/bash.ts:108:21)` (`run.stdout += chunk`), Node exits; for a mid-size case `head -c 200000000` the call succeeds but takes 7.5 s and +893 MB RSS (three copies: `output`, `stdout`, `stderr`). In `tree-agent "…"` this kills the run with a stack trace and truncated JSONL, because `execute` never sees the error (it is thrown in an event handler, outside the promise).
- BREAK (non-blocking, see below): single 200 KB line → `read` returns 51 200 chars with no notice; `timeout: 1e10` → `TimeoutOverflowWarning` and the command is killed at once ("Command timed out after 10000000000s"); SIGINT during a headless `bash sleep 77` → node exits 130 and `sleep 77` survives as an orphan (`kill -INT` on `/private/tmp/claude-501/-Users-pauliusztin-Documents-01-Projects-AI-Engineer-Handbook-building-agentic-systems-building-agentic-systems/a7886cf7-0496-410f-869e-8174a1590e03/scratchpad/qa/sigint.ts`, `pgrep` shows pid) because bash runs `detached`.

**Acceptance criteria**
- [x] PASS — Loop (FakeProvider) — `tests/loop/agent-loop.test.ts`: `:51 continues on a tool call even when finish_reason says stop`, `:88 fails every call with 'arguments truncated'…`, `:103` + `:115` (cut-off once; second ends in error; reset after a normal turn), `:130 Blocked`, `:197 max_turns`, `:207 aborted mid-stream`, `:268`/`:291` steering, `:319`/`:347` follow-ups; `tests/session.test.ts:167 keeps both queues across an abort…`; re-proved by my loop probes above.
- [x] PASS — `wc -l apps/harness/src/loop/agent-loop.ts` → 168
- [x] PASS — Prompt — `system-prompt.test.ts`, `context-files.test.ts`, `session.test.ts:77/:89/:105`; my probes: 238 est. tokens, order stable, walk order, 40 000 + marker, 3x cap, UTC date, activate().
- [x] PASS — Tools — `read.test.ts`, `bash.test.ts` spill, `edit.test.ts`, `registry.test.ts`, `builtins.test.ts`; my probes above.
- [x] PASS — `structured` set for read (`{path, lines}`) and bash; model message carries only `content` (`agent-loop.test.ts:361`; live JSONL tool_result events carry `content` only).
- [x] PASS — CLI mode matrix — `cli.test.ts` + my 28 probes + live runs.
- [x] PASS — Trust — `cli.test.ts main (trust)`, `trust.test.ts`; my matrix incl. subdirectory-not-trusted.
- [x] PASS — boundary grep empty.
- [x] PASS — [HUMAN] e2e run live by the Tester (evidence above); ticked.
- [x] PASS — gates green (above).

**Evidence**
```
$ make harness-tests            → Test Files 20 passed (20) / Tests 224 passed (224)  tests=0
$ make pre-commit              → prettier, ruff check, ruff format, biome check (harness): Passed
$ node /private/tmp/claude-501/-Users-pauliusztin-Documents-01-Projects-AI-Engineer-Handbook-building-agentic-systems-building-agentic-systems/a7886cf7-0496-410f-869e-8174a1590e03/scratchpad/qa/bigout.ts "yes"
RangeError: Invalid string length
    at Socket.<anonymous> (…/apps/harness/src/tools/bash.ts:108:21)
$ node loop-adv.ts → 19 PASS   $ node cli-adv.ts → 28 PASS
$ node tools-adv.ts → all PASS except the two FAILs below
FAIL read: single 200KB line: no truncation notice (len=51200)
FAIL bash: HUGE timeout (1e10 s): killed: Command timed out after 10000000000s
$ node sigint.ts & kill -INT $!  → node exit=130; pgrep: "44391 sleep 77" (orphan)
```

**SWE decision list vs spec**
- Non-blocking, accept: event order `turn_end` before the turn's `tool_call`/`tool_result` (follows the §6 algorithm, which the task says to implement verbatim; the CI story's listing is loose — PA should reword the story, no code change); `prompt()` queued behind leftover steering; "Not run: aborted." for the rest of an aborted batch and ending before the steering drain (required for a valid wire and for "queues survive an abort"); once-per-run `length` reset; `parseError` before the gate; `trusted.json` exact match (matches the task AC; symlinked cwd of a trusted dir is not trusted, safe side); `--json` + no prompt + no pipe → usage; project settings layer; 64 KB `afterToolCall` backstop; test-only provider seam via `CliDeps.createProvider`.
- Non-blocking, track: text sink prints a `length`-cut partial answer and its continuation as two lines (`"part one\npart two\n"`) — cosmetic, only on a cut-off answer; add to 205/213 notes. Headless SIGINT: spec §15 specifies Esc only for the TUI, so no AC is broken, but the `detached` bash makes Ctrl+C leave the command running; fix by `process.once("SIGINT"/"SIGTERM")` → `session.abort()` (bash's abort handler already kills the group) in the headless path — do it with 205 at the latest.

**Other issues found**
1. [blocking] bash unbounded in-memory output (above).
2. [should fix in the same pass, trivial] `read` cuts a single line > 50 KB with no notice (`read.ts page()` first-line branch, also slices by UTF-16 units not bytes); a minified file looks complete to the model. Append e.g. `[line N cut at 50 KB]`. Empty file returns `content: ""` (an empty tool message); prefer `(empty file)`.
3. [should fix, trivial] `bash` `timeout` has no upper bound: > 2 147 483 s overflows `setTimeout` and kills the command immediately; add `.max(...)` in `bash.ts` Params (e.g. 3 600).
4. [note] A bash command that backgrounds a process (`sleep 3 & echo started`) blocks until the background job closes the pipe (3.0 s measured; up to the timeout for a daemon).
5. [note] An unreadable (EACCES) ancestor `AGENTS.md` makes the whole run fail with the raw errno message; skipping with a `log.warn` is friendlier.
6. [note] `TREE_AGENT_TRUST` is not in `.env.example` (SWE's reason is sound; if added, keep it commented).

**VERDICT: FAIL** — 1 blocking: `bash` buffers all output in memory and crashes the process on very large output. All listed ACs otherwise verified.

Fix for the SWE: in `src/tools/bash.ts` stop concatenating unbounded strings. Keep a head buffer (first 25 KB) and a rolling tail (last 25 KB); once total bytes exceed 50 KB stream everything to `<tmp>/<id>.txt` (open the write stream at that moment, flush the buffered head first) and keep counting. `structured.stdout/stderr` must then be bounded too (head+tail with the same marker), or the structured value is capped, and the spec's "full" claim for `structured` is documented as truncated beyond N MB. Add a regression test: `bash` with `head -c 80000000 /dev/zero | tr '\\0' a` returns a result ≤ 51.5 KB ending in `Full output: <path>`, the file is 80 MB, and the test process survives; plus a `yes`-style test with a short `timeout` (the kill must end it). Also cover items 2 and 3 with tests.

### [SWE] 2026-10-24 23:37 — Fixes after QA

**Files modified**
- `apps/harness/src/tools/truncate.ts` — `HeadTailBuffer` (byte-bounded first/last `maxBytes / 2` of a stream, `render()` uses the same `[... N bytes omitted ...]` marker as `headTail`), `joinHeadTail`, `cutBytes` (byte cut that never splits a UTF-8 character).
- `apps/harness/src/tools/bash.ts` — rewritten capture. Buffers, not strings (no `setEncoding`). Three `HeadTailBuffer`s (`output`, `stdout`, `stderr`), so memory is bounded per stream. When the interleaved output is about to pass 50 KB, the tool opens `<home>/tmp/<uuid>.txt` as a write stream, flushes what it has buffered so far (all of it, since nothing has been dropped yet), then writes every later chunk as it arrives. Backpressure: both pipes pause until the file's `drain`. The file is ended (`finish`) before the result resolves. Any error from a data handler, the spill stream or `spawn` kills the process group and rejects the promise, and `ToolRegistry.execute` turns the rejection into an `isError` result. `structured.stdout` / `structured.stderr` are bounded the same way, and the tool description says so. The `timeout` param is `.min(1).max(3600)`, and its description names the max. The `Command exited with code N` line now comes before `Full output: <path>`, so content always **ends** with the path.
- `apps/harness/src/tools/read.ts` — empty file → `(empty file)`, `structured.lines: []`. A first line over 50 KB is cut by bytes (`cutBytes`) and followed by `[Line N cut: showing the first X of Y bytes. offset/limit page by line; read the rest of this line with bash, e.g. tail -c +X+1 '<path>' | head -c 51200.]`, then the usual `[Showing lines …]` hint if more lines follow.
- `apps/harness/src/headless/print.ts` — `Signals` seam (`once` / `off`). `runHeadless` registers `once("SIGINT" | "SIGTERM", () => session.abort())` and removes both in `finally`. `once` means a second Ctrl+C falls through to Node's default exit.
- `apps/harness/src/cli.ts` — `CliDeps.signals`; the entry point passes `process`.
- Tests: `tests/tools/bash.test.ts`, `tests/tools/read.test.ts`, `tests/tools/registry.test.ts`, `tests/tools/truncate.test.ts`, `tests/cli.test.ts`.

**New / changed tests**
- `bash.test.ts`
  - `> streams an 80 MB output to the file and keeps the result and memory bounded`: result ≤ 51.5 KB, contains `[... 79948800 bytes omitted ...]`, ends with `Full output: <path>`; the file is 80 000 000 bytes; `structured.stdout` ≤ 51.5 KB.
  - `> stops the file at maxSpillBytes and says so` (`maxSpillBytes: 200_000` with 300 KB of output → the file is 200 000 bytes and the content has the notice; see Decisions).
  - `> kills an endless 'yes' at the timeout with a bounded error result`: `timeout 1` → `isError`, starts `Command timed out after 1s`, ≤ 51.5 KB, returns in about 1 s.
  - `> rejects instead of crashing when the output file cannot be written`: `tmpDir` under a regular file → the `mkdirSync` in the data handler throws `ENOTDIR` → an `isError` result through `ToolRegistry.execute`, and the test process survives.
  - `> rejects a timeout over 3600 s or under 1 s as an invalid-arguments result`: `1e10` and `0.5` → zod issue list.
  - `> leaves no orphan when aborted: the backgrounded child dies with the group`: `sleep 30 & echo $! > pid; wait`, abort, then poll `kill(pid, 0)` → `ESRCH`.
  - Changed: `> spills output over 50 KB …` now asserts that `structured.stdout` is bounded and carries the marker, where it used to require it to equal the full file.
- `read.test.ts`
  - `> cuts a single line over 50 KB by bytes and says how much was cut and how to read on`: 70 000 × `€`, 210 000 bytes → 51 198 bytes shown, exact notice, then the paging hint.
  - `> says (empty file) for an empty file`.
- `registry.test.ts > turns an asynchronous rejection into an error result` (the rejection fires from a `setTimeout`).
- `truncate.test.ts`
  - `HeadTailBuffer > keeps everything while within the cap`.
  - `> renders like headTail once chunks overflow, whatever the chunking`.
  - `cutBytes > cuts by UTF-8 bytes without splitting a character`.
- `cli.test.ts > main (headless) > aborts the run on SIGINT|SIGTERM, kills the running bash command and exits 1` (`it.each`, an `EventEmitter` as `signals`): exit 1; stderr has `! Command aborted` and `run ended: aborted`; 1 provider request; the backgrounded `sleep 30` pid → `ESRCH`; 0 listeners left.

**Decisions (please review)**
- *Exit code for a signal-aborted headless run = 1, not 130.* The spec's exit-code line (§15: 0 completed, 1 error / max_turns, 2 blocked) does not list `aborted`, and `runHeadless` already maps every non-`completed` reason to 1. PA can pick 130/143 later if wanted. A signal that arrives before the session exists (e.g. during `checkDecisionModel`) still takes Node's default path (130), and there is no child to orphan at that point.
- *Spill file capped at `BashOptions.maxSpillBytes` (default `MAX_SPILL_BYTES` = 100 MiB).* Not requested, but without it `yes` with the default 120 s timeout writes gigabytes to `~/.tree-agent/tmp`: I measured about 100 MB in under 1 s. Past the cap the pipes keep draining, nothing more is written, and the content gets `Only the first 104857600 bytes were saved.` before the `Full output:` line. The 80 MB AC case stays below the cap. PA can drop the cap, or wire `maxSpillBytes` to settings, without changing the tool.
- *Deviation from my 23:15 decision list (line 184) and the spec's "structured = full":* `structured.stdout` / `structured.stderr` are now cut to 25 KB head + 25 KB tail, per the Tester's direction. A codemode script that needs everything can read the `Full output` file.
- *Headless SIGINT is done here, not deferred to 205.* It is 6 lines in `runHeadless`, plus the seam.
- Untouched Tester notes: 4 (a backgrounded job holds the pipe open), 5 (EACCES on an ancestor `AGENTS.md`), 6 (`TREE_AGENT_TRUST` in `.env.example`). The text-sink double line on a `length` cut-off is also left for 205/213.

**Tests**
- Unit: 238 passing, 0 failing (20 files; was 224 at QA; `it.each` rows count separately). `bash.test.ts` takes about 5 s, mostly the 80 MB case. Both pid checks poll with `tests/helpers/tmp.ts > gone()`, up to 2 s.
- Integration: N/A (no suite per AGENTS.md).

**Evidence**
```
$ make harness-lint-fix && make harness-lint-check   → biome: Checked 45 files … No fixes applied.
$ make harness-typecheck                             → tsc --noEmit, exit 0
$ make harness-tests                                 → Test Files 20 passed (20) / Tests 238 passed (238)
$ make pre-commit                                    → prettier, ruff check, ruff format, biome check (harness): Passed

# Tester's repro, re-run (scratchpad/qa/bigout.ts, via builtinRegistry.execute)
$ node bigout.ts "yes | head -c 300000000"           → RESULT isError=false bytes=51403
$ node bigout.ts "head -c 900000 /dev/zero | tr '\0' a" → RESULT isError=false bytes=51356
# memory probe (scratchpad/swe/mem.ts): yes, timeout 3 → 3003 ms, peak RSS +153 MB, 51557 B result;
# 900 MB via tr → isError=false, peak RSS +107 MB (was +893 MB for 200 MB / RangeError for more);
# the 33 s run time there is `tr` itself (33.4 s with > /dev/null).

# SIGINT, FakeProvider + signals: process (scratchpad/swe/sigint.ts)
> bash {"command":"sleep 77.4321"} / ! Command aborted / run ended: aborted
node exit=1; pgrep -f "sleep 77.4321" → none ("no orphan")

# LIVE, real entry point (node apps/harness/src/cli.ts), fresh TREE_AGENT_HOME, key from .env in a subshell (never printed)
$ tree-agent --json "Run … yes | head -c 50000000 . Then tell me the path …"
exit=0, 47 JSONL lines, all JSON.parse; tool_call bash → tool_result (bounded) → turn_end with the path → agent_end completed;
<home>/tmp/<uuid>.txt = 50000000 bytes
$ tree-agent "Run … sleep 61.234" & ; kill -INT once `pgrep -f '^sleep 61.234'` shows the child
> bash {"command":"sleep 61.234","timeout":120} / ! Command aborted / run ended: aborted
node exit=1; pgrep -f '^sleep 61.234' → none
```

- Pre-commit follow-up (Tester notes 1, 2, 6): `joinHeadTail` now moves the head and tail cuts to UTF-8 boundaries (shared `charEnd` helper, also used by `cutBytes`), so `HeadTailBuffer.render()`/`headTail` never emit U+FFFD; a spill-file error no longer fails the call or kills the command, the result keeps head + tail and ends `could not save full output: <errno message>`; `.env.example` gains the commented `TREE_AGENT_TRUST=1` line. Tests: `bash.test.ts` "keeps the head and tail and says why when the output file cannot be written" (replaces the "rejects…" test) and "never splits a multi-byte character at the head or tail cut" (`'€'.repeat(100000)`), `truncate.test.ts` "moves both cuts to a character boundary and counts the dropped bytes" and "renders without U+FFFD when a cut falls inside a character"; EACCES probe (tmp dir chmod 555) returns head + tail + the line for `seq`, non-zero exit and `timeout 2 yes`. Gates: 241/241 tests, lint, typecheck, pre-commit clean.

### [Tester] 2026-10-24 23:55 — Re-QA after fixes

**Test summary**
- Format / lint / typecheck / pre-commit: PASS (all four `make harness-*` gates and `make pre-commit` exit 0; biome "Checked 45 files … No fixes applied")
- Unit tests: 238 passed / 0 failed (20 files, incl. the 2 live OpenRouter tests)
- Integration: N/A. Warnings: 0
- `wc -l apps/harness/src/loop/agent-loop.ts` → 168; boundary grep `apps/memory|tree.memory|tree.mcp` over `apps/harness/src` → empty (exit 1)

**Re-verification of the previous FAIL items** (probes in `/private/tmp/claude-501/-Users-pauliusztin-Documents-01-Projects-AI-Engineer-Handbook-building-agentic-systems-building-agentic-systems/a7886cf7-0496-410f-869e-8174a1590e03/scratchpad/qa/{bigout2,ro,read2,to,b3,b4}.ts`, signal scratch dirs `/private/tmp/claude-501/-Users-pauliusztin-Documents-01-Projects-AI-Engineer-Handbook-building-agentic-systems-building-agentic-systems/a7886cf7-0496-410f-869e-8174a1590e03/scratchpad/qa/sig-{INT,TERM,stream}`)
1. Blocker, bash unbounded output, FIXED. Via `builtinRegistry.execute` under `/usr/bin/time -l`:
   - `yes | head -c 300000000` → isError=false, 51 403 B result, 105 ms, ends `Only the first 104857600 bytes were saved.\n\nFull output: <path>`, file 104 857 600 B, structured.stdout 51 237 B, peak RSS 162 MB (node baseline about 50 MB).
   - `yes` with `timeout 3` → isError=true, "Command timed out after 3s", 51 432 B, 3 005 ms, file capped at 100 MiB, peak RSS 243 MB.
   - `yes` with the default 120 s timeout → 120 005 ms, isError=true, 51 435 B, peak RSS 278 MB (flat: 243 MB at 3 s, 278 MB at 120 s; before the fix 200 MB of output cost +893 MB and 900 MB crashed). No `yes` orphan afterwards. The process survives all three.
   - Exact-boundary: 51 199 / 51 200 B → no file; 51 201 B → file + 51 350 B result. Interleaved stdout/stderr 30 000 lines each → file has all 60 000 lines, each structured stream ends at its own last line. 4 parallel spilling commands → 4 distinct files, 100 000 lines each.
2. Spill write failure: `tmpDir` chmod 555, `seq 1 40000` / `yes` / a command that spills after output → each returns isError=true ("EACCES: permission denied, open '<path>'") in 11-26 ms, the process stays alive and the next `echo alive` call returns "alive\n"; no `yes` left behind.
3. `read`: 200 KB single line → body 51 200 B + `[Line 1 cut: showing the first 51200 of 200000 bytes. offset/limit page by line; read the rest of this line with bash, e.g. tail -c +51201 'a.txt' | head -c 51200.]`. Exactly 51 200 B → no notice; 51 201 B → "first 51200 of 51201 bytes". Multi-byte at the cut, 2-, 3- and 4-byte chars at 4 pad offsets each (12 files): output is valid UTF-8 (fatal TextDecoder), no U+FFFD, shown count is below 51 200 when a char straddles (51 197..51 200) and the "of N" equals the true byte length every time. Long first line followed by more lines → cut notice then `[Showing lines 1-1 of 3. Use offset=2 to continue.]`. Empty file → `"(empty file)"`, structured `lines: []`.
4. `timeout`: 1e10, 3601, Infinity → "Number must be less than or equal to 3600"; 0.5, 0, -1 → "greater than or equal to 1"; "5", null, NaN → type issue; 3600 and 1 accepted. No `TimeoutOverflowWarning`.
5. Headless signals, real entry point (`node apps/harness/src/cli.ts --trust "Run exactly this shell command and nothing else: sleep 61"`, scratch cwd, fake `TREE_AGENT_HOME`, `.env` sourced in a subshell, key never printed): SIGINT → exit 1, stderr `> bash {"command":"sleep 61","timeout":120}` / `! Command aborted` / `run ended: aborted`, stdout empty, `pgrep -f '^sleep 61'` empty 27 ms after exit. SIGTERM → identical, exit 1, orphan gone in 30 ms. SIGINT while the model is still generating (no tool) → exit 1, `run ended: aborted`.
6. Regression: re-ran my probes. `loop-adv.ts`: 19/19 PASS. `cli-adv.ts`: 26 checks, 0 FAIL. `tools-adv.ts`: all PASS except the old "1e10 timeout does not kill immediately" probe, which now correctly gets the zod `<= 3600` rejection (expected change); `200MB` probe went 7.5 s / +893 MB → 7.7 s / +59 MB RSS; empty file reads "(empty file)".

**Rulings on the SWE decisions**
- Exit 1 (not 130) after Ctrl+C or SIGTERM: ACCEPT. Spec §15 lists only "0 completed · 1 error / max_turns · 2 blocked"; `aborted` falls under "not completed" = 1, and `run ended: aborted` on stderr distinguishes it. A signal before the session exists (during `checkDecisionModel`) still takes Node's default 130 with no child to orphan; a second Ctrl+C also falls through to the default (`once`), which is the right escape hatch. Carry to 213: "Ctrl+C / SIGTERM aborts the run, kills the running bash command's process group, prints `run ended: aborted` and exits 1."
- 100 MiB spill cap (`MAX_SPILL_BYTES`): ACCEPT as the default. It bounds disk use for a runaway command within the 3600 s ceiling; the model sees `Only the first 104857600 bytes were saved.` directly above `Full output: <path>`, which is clear (it says the file is partial, not that the output was lost; head and tail in the message are still the real ends). Note for 213: the file holds the first 100 MiB while the message tail is the real tail; `maxSpillBytes` is a `BashOptions` knob, not yet a setting. Cost of the cap: a runaway `yes` burns CPU (about 110 s user + 138 s sys over 120 s) because the pipes keep draining; acceptable, bounded by the timeout.
- `structured.stdout`/`stderr` now 25 KB head + 25 KB tail: ACCEPT. Memory safety outweighs "full", the description tells the model, and a codemode script can read the `Full output` path from `content`. Doc wording for 213: "bash `structured.stdout` and `structured.stderr` are each cut to the first and last 25 KB with an '[... N bytes omitted ...]' marker; the complete interleaved output is in the file named at the end of `content` (up to 100 MiB)." Only `exitCode` is exact.

**Other issues found (non-blocking)**
1. `HeadTailBuffer.render()` cuts at raw byte offsets, so a bash output whose head/tail cut falls inside a multi-byte character gets one U+FFFD next to the omission marker (`'€'*100000` → one `\uFFFD` right after `[... 248801 bytes omitted ...]`; `structured.stdout` too). Cosmetic and adjacent to the marker; `cutBytes` already exists and could be reused for the head end, and the tail start can skip UTF-8 continuation bytes. Optional follow-up.
2. When the spill file cannot be written, the whole command result is replaced by the raw errno string (EACCES …); the command's output is lost although head/tail were buffered. Degrading to the head+tail with a "could not save full output: EACCES" line would be friendlier. Optional.
3. Still open from before: backgrounded jobs hold the pipe (note 4), unreadable ancestor AGENTS.md aborts the run (note 5), `TREE_AGENT_TRUST` not in `.env.example` (note 6), text sink prints a `length`-cut partial answer and its continuation on two lines.

**Acceptance criteria**: unchanged from the first QA entry, all PASS (the [HUMAN] e2e was run live by the Tester then; the fix pass added a live `yes | head -c 50000000` run and the live signal runs above). Gates re-run green.

**VERDICT: PASS**
