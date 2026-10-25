---
id: 206-harness-permissions-gate-decide-auto-mode
status: done
feature: tree-agent-harness
---

# M3 — the permission gate (`ask` | `auto`, read-only exemption), the TUI prompter and headless fail-stop with exit 2, `permission` audit entries, the `decide<T>()` primitive and the `auto`-mode danger classifier

Tags: `harness`, `permissions`, `decision-model`, `enhancement`
Depends on: 205 (TUI prompter component, JSONL audit entry)
Blocks: 210 (the gate the bridge reuses), 212 (`decide<T>()`)
Implements: ADR-016 §5 (two permission modes, read-only exemption, headless `auto` + exit 2), §6 (`decide<T>()`)

## Scope

1. **`src/permissions/gate.ts`** — spec §11 `resolve()` with the first-match order: `isReadOnly` → allow
   (`source: "readOnly"`); `sessionAllow` → allow; `ask` → prompt; `auto` → classifier (`dangerous: false` →
   allow, `true` or failure → prompt with the reason); prompt: `once` → allow, `session` → allow + add,
   `deny` → deny; no prompter → deny with `source: "headless"`. `PermissionPrompter` shows the primary
   argument (`bash.command`; `read/edit/write.path` relative to `cwd`; `codemode` description + first 2 KB
   of script; MCP input JSON ≤ 2 KB) and the classifier reason. Every resolution → a `permission` JSONL
   entry and a `permission` `AgentEvent`. `AgentSession.beforeToolCall` now calls `resolve`; a deny answers
   `Blocked: <reason>` to the model.
2. **Headless policy** — a `headless` deny ends the run with `agent_end(blocked)` and exit code 2 unless
   `--on-blocked continue`; headless defaults to `auto`, the TUI to `settings.permissions.mode` (`ask`);
   `--permission-mode ask|auto` overrides both.
3. **TUI** — `src/tui/permission-prompt.ts` (keys `y` once, `s` session, `n` deny), `Shift+Tab` toggles
   `ask` ↔ `auto`, `/mode ask|auto`, status bar shows the live mode.
4. **`src/decision/decide.ts`** — spec §12 `decide<T>(provider, {name, system, input, schema, model?,
   timeoutMs?, cache?})` = `provider.complete` on `settings.decisionModel` with a timeout, one parse retry
   (inside `complete`), optional cache. Knows nothing about its consumers.
5. **`src/permissions/decision-model.ts`** — spec §11.1 `DangerCheck` schema and system prompt verbatim; input
   = tool name, primary argument + full input (≤ 2 KB), `cwd`, the last 3 user messages; 10 s timeout;
   fail-closed (error/parse/timeout → treated as dangerous); per-session cache on `hash(tool, input)`; it
   never auto-denies.
6. Nothing persists across sessions beyond `permissions.mode` in `settings.json` (no allow lists).

**Out of scope (intentional):** permission rule files, a bypass mode, `settings.local.json` (spec non-goals);
the codemode bridge's use of the same gate (210 — the gate is written so a call with `origin: "codemode"`
needs no special case).

## Acceptance Criteria

- [x] Read-only never prompts in either mode (`read`; a tool with `isReadOnly: true`); a `session` answer
      skips later prompts for that tool name only; `ask` prompts on `bash`, `edit`, `write` and any non-read-only
      tool; `auto` allows `dangerous: false`, prompts on `dangerous: true` with the classifier's `reason`, and
      prompts on a classifier error, parse failure or 10 s timeout (FakeProvider delays); the classifier
      result is cached — the same `(tool, input)` twice costs one `complete` call.
- [x] Headless defaults to `auto` and the TUI to `ask`; `--permission-mode ask` headless → the first write
      is denied with `source: "headless"`, `agent_end(blocked)`, exit 2; `--on-blocked continue` → the model
      receives `Blocked: …` and the run completes with exit 0; `--permission-mode auto` with a FakeProvider
      classifier answering `dangerous: true` → exit 2.
- [x] Every resolution appends `{type: "permission", call, decision, source, reason?}` to the session file and
      emits a `permission` event; a `--json` run shows the event before the matching `tool_result`.
- [x] The prompt text shows `bash`'s command, `write`'s path relative to `cwd`, and for an MCP-shaped call the
      input JSON truncated at 2 KB (unit test on the summary builder).
- [x] `decide<T>()` returns the parsed object, honours `timeoutMs` (rejects at the deadline), uses the cache
      when given one, and the `DangerCheck` prompt sent to the provider equals the §11.1 text.
- [x] `Shift+Tab` and `/mode auto` switch the mode for the rest of the session (the next non-read-only call
      is classified, not prompted) and the status bar reflects it; the setting file is not modified.
- [ ] [HUMAN] TUI in `ask`: a `write` shows the prompt with the relative path; `n` → the model reports it was
      blocked and proposes an alternative; `s` on `bash` → no further `bash` prompts this session. In `auto`
      (`Shift+Tab`): `echo hi > note.txt` (inside the cwd) runs unprompted; `echo hi > /tmp/x` prompts with a
      reason naming a write outside the cwd (answer `n`); `rm -rf ~/.ssh` (same decoy `HOME`) prompts with a
      reason naming credentials/outside-cwd.
- [ ] [HUMAN] With a decoy home (`HOME=/tmp/fakehome` holding `Downloads/` with junk files and a dummy
      `.ssh/config`), `tree-agent "delete every file under ~/Downloads"` (headless, default `auto`) exits 2
      with `agent_end` `blocked` and the junk files still present — never run this against the real `HOME`.
- [x] `make harness-lint-fix && make harness-lint-check && make harness-typecheck && make harness-tests &&
      make pre-commit` green.

## User Stories

### Story: A user in `ask` mode approves one command for the session
1. The agent proposes `bash: pnpm test`; the prompt shows the command; the user presses `s`.
2. The test runs; later `bash` calls run without prompting; `read` calls never prompted at all.

### Story: `auto` mode flags a dangerous edit
1. In `auto`, the agent calls `edit` on `~/.zshrc`.
2. The classifier answers `dangerous: true, reason: "edits a shell rc file"`; the prompt appears with that
   reason; the user presses `n`; the model is told the call was blocked.

### Story: CI is stopped by a dangerous call
1. A pipeline runs `tree-agent "clean up the build and push"`; the model tries `git push --force`.
2. The classifier flags it; there is no prompter; the run ends `blocked`, exit 2; the JSONL has the
   `permission` entry with `source: "headless"`.

### Story: CI chooses to continue instead
1. The same run with `--on-blocked continue`.
2. The model gets `Blocked: dangerous: force-push …`, picks a different route, exit 0.

### Story: The classifier is down
1. OpenRouter times out on the classifier call.
2. The call is treated as dangerous: the TUI prompts; headless exits 2 — never a silent allow.

---

Blocked by: 205

## Log

### [PA] 2026-10-24 20:04 — Grooming

**Summary**
Two permission modes behind one gate with a read-only exemption, a TUI prompter and a headless fail-stop
(exit 2), audit entries, and the single `decide<T>()` primitive that backs the `auto` classifier.

**Key decisions**
- Fail-closed everywhere the classifier cannot answer; the human is the only one who denies in the TUI.
- `decide<T>()` is consumer-agnostic by test: its prompt/schema come from the caller, so 212's summariser
  reuses it unchanged.
- The gate takes `origin: "codemode"` calls with no special case so 210 adds no second gate.

**Dependencies**
- 205 — the TUI (prompter component) and the JSONL store (audit entry).

**User stories**
- 5 stories covering: session approval, a flagged rc-file edit, headless fail-stop, `--on-blocked continue`,
  a classifier outage.

Ready for implementation.

### [SWE] 2026-10-24 21:57 — Implementation

**Files added**
- `apps/harness/src/decision/decide.ts` — `decide<T>(provider, {name, system, input, schema, model?, timeoutMs?, cache?, cacheKey?})`. One `provider.complete` (the provider does the one parse retry), a `setTimeout` deadline that aborts the request's signal and rejects with `DecisionTimeout` ("<name>: no answer within N ms"), and a cache that only stores answers that parsed. `model` defaults to `Settings.parse({}).decisionModel`. `cacheKey` (default: `input`) was added so a caller can cache on less than its full prompt.
- `apps/harness/src/permissions/decision-model.ts` — the §11.1 `DangerCheck` schema plus `.describe("One sentence, at most 200 characters.")` on `reason`. The `DANGER_CHECK_SYSTEM` prompt is verbatim. The input is `Tool`, `Primary argument`, `Input (JSON)` ≤ 2 KB, `cwd`, and the last 3 user messages (each ≤ 2 KB), and it ends with "Answer dangerous, risk and reason (one sentence, at most 200 characters)." The schema sent is `z.preprocess(clipReason, DangerCheck)`: the wire JSON schema keeps `maxLength: 200`, and an over-long reason is cut to 199 chars + `…` instead of failing to parse. Timeout is 10 s. The cache key is `sha256(tool name + JSON(arguments))`.
- `apps/harness/src/permissions/gate.ts` — `resolve(req, {mode, prompter, classifier, sessionAllow})`, first match wins, exactly as spec §11. Fail-closed lives here: a classifier that throws (error, parse, timeout) becomes `{dangerous: true, reason: "the safety classifier failed (<msg>)"}`.
- `apps/harness/src/permissions/summary.ts` — `primaryArgument(call, cwd)`: the bash command, cut at 2 KB with the marker (corrected 2026-10-25; it said "the whole bash command"); the read/edit/write `path` relative to cwd; codemode description plus the first 2 KB of the script; anything else (MCP) as input JSON. Every cut is at 2 KB (UTF-8 safe, via `cutBytes`) and gets the ` … (cut at 2 KB)` marker. The classifier input uses the same builder.
- `apps/harness/src/tui/permission-prompt.ts` — `PermissionPrompt`: `Allow <tool>?`, the summary (first 12 lines + "… (+N lines)", sanitized), `classifier: <reason>` in yellow, then `y once · s this session · n deny`. Esc and Ctrl+C deny. `tuiPrompter(tui)` mounts it, takes the focus and gives it back afterwards.
- Tests: `tests/decision/decide.test.ts`, `tests/permissions/{gate,decision-model,summary}.test.ts`, `tests/tui/permission-prompt.test.ts`.

**Files changed**
- `src/types.ts` — `PermissionPrompter` added (so `tui/` needs nothing from `permissions/`). The `permission` AgentEvent gets an optional `reason?` (the spec's §4 event lacks it; the JSONL entry has it).
- `src/loop/agent-loop.ts` (175 lines, ≤ 200) — `export const BLOCKED`. After a batch, an abort whose `signal.reason === BLOCKED` ends the run `agent_end(blocked)`; any other abort still ends `aborted`. This is the only loop change. The gate itself goes through `beforeToolCall`.
- `src/session.ts` — new options `prompter` (default none = headless), `onBlocked` (`stop` default | `continue`) and `decisionModel`. `permissionMode` is now mutable through `setPermissionMode()`/`permissionMode`, works mid-run and never touches settings. `beforeToolCall` → `#gate`. An unknown tool name is not gated (the registry answers `Unknown tool`). Otherwise `resolve` runs, the `permission` entry is appended to the store, then the `permission` event is emitted, both before the tool runs. A headless deny with `onBlocked: stop` calls `run.abort(BLOCKED)`, so later calls in the same batch are answered `Not run: aborted.` and never run. `clear()` resets session approvals and cached verdicts.
- `src/headless/print.ts` — exit codes `completed → 0`, `blocked → 2`, else 1.
- `src/cli.ts` — `--permission-mode ask|auto` and `--on-blocked stop|continue`, both validated (bad value → message + usage, exit 1) and listed in `USAGE`. Mode = flag ?? (TUI ? `settings.permissions.mode` : `auto`). `open(trust, prompter)`: the TUI passes `tuiPrompter(tui)`, headless passes `null`.
- `src/tui/{app,input,commands,transcript}.ts` — Shift+Tab (`\x1b[Z`, caught before the Editor) toggles the mode and adds a `permission mode: X` notice. `/mode ask|auto` was added (no argument shows the mode; any other value is an error line), and `/help` lists Shift+Tab. Transcript permission notices now append `— <reason>` and skip `readOnly` allows (one line after every `read` was noise, and nothing was decided).
- `src/provider/fake.ts` — a `complete()` answer can now be a function `(req, signal) => unknown | Promise` (used for delays, never-settling answers and signal checks).
- Existing tests: session/app fixtures pass an approving prompter, and headless cli fixtures script classifier `answers`. The `--json` sequence now includes `permission`, and the persistence peek test now expects the `permission` entry before `tool`.

**Tests**
- Unit: 445 passed, 0 failed, 33 files (`make harness-tests`, live OpenRouter tests included). The suite had 362 before.
- Integration: N/A (none in this app).
- Mutation probes (each file restored afterwards): classifier failure → `dangerous: false` breaks 2 tests; `run.abort(BLOCKED)` removed breaks 4; the `sessionAllow` check disabled breaks 4; `cacheKey` removed (cache on the full input) breaks 1.

**Acceptance criteria**
- [x] Read-only / session / ask / auto / fail-closed / cache — `gate.test.ts` (read-only in both modes incl. an `isReadOnly` MCP tool; ask on bash/edit/write/MCP; `session` per tool name; auto allow / prompt-with-reason / headless `dangerous:`; error and timeout fail-closed), `decision-model.test.ts > gives up after 10 s` (fake timers, FakeProvider never answers), `> classifies the same (tool, input) once per cache, whatever the conversation`.
- [x] Headless defaults — `cli.test.ts > main (permissions)`: auto by default (classifier called on haiku even with `settings.permissions.mode: ask`); `--permission-mode ask` → deny `headless`, `run ended: blocked`, exit 2, JSONL entry, no classifier call; `--on-blocked continue` → `Blocked: …` to the model, exit 0; auto + `dangerous: true` → exit 2; classifier down → exit 2. TUI default `ask` and settings/flag precedence → `main (TUI permissions)`.
- [x] Audit — `session.test.ts > AgentSession permissions > records every resolution…` (disk order `permission, tool` per call, `reason` only when present); `cli.test.ts > --json shows each permission event before the matching tool_result`.
- [x] Summary — `summary.test.ts` (bash command, relative path, outside path kept, codemode description + 2 KB, MCP JSON cut at exactly 2048 bytes + marker, UTF-8 boundary).
- [x] `decide<T>()` — `decide.test.ts` (parsed answer, default model, rejects at 1000 ms and not at 999 ms, aborts the request signal, no timer left behind, cache by input / `cacheKey`, failures not cached); §11.1 prompt equality → `decision-model.test.ts > sends the §11.1 system prompt verbatim` (the expected text is parsed out of `docs/notes/tree-agent-harness-spec.md` itself).
- [x] Shift+Tab and `/mode auto` — `cli.test.ts > main (TUI permissions) > %s switches to auto…` (×2: status bar `· auto ·`, the next write classified and not prompted, `settings.json` byte-identical), `app.test.ts > toggles ask/auto on Shift+Tab`, `commands.test.ts > /mode …`, `session.test.ts > switches the mode for the rest of the session`.
- [ ] [HUMAN] TUI in ask/auto walkthrough. Automated stand-ins: `app.test.ts > s on bash approves bash for the session` and `> shows the classifier's reason in the prompt in auto, and n tells the model it was blocked`, plus the cli TUI test for `n` on a relative-path write.
- [ ] [HUMAN] Decoy-home `~/Downloads`: NOT RUN. The scratch cwd path contains the real user name (`-Users-pauliusztin-…`), so a model could build a real absolute `~/Downloads` path; if the classifier then judged the delete as "what the user asked", real files could go. Leave this to a human with a cwd that does not reveal the real home.
- [x] Cadence green (below).

**Evidence**
```
$ make harness-lint-fix && make harness-lint-check && make harness-typecheck && make harness-tests && make pre-commit
Checked 73 files … No fixes applied.        (lint-check)
pnpm exec tsc --noEmit                       (clean)
 Test Files  33 passed (33)
      Tests  445 passed (445)
biome check (harness)....Passed  (prettier/ruff Passed)

# live, scratch cwd, fake HOME + TREE_AGENT_HOME (…/scratchpad/e2e)
$ node src/cli.ts --cwd work1 --permission-mode ask "Create a file named a.txt containing hello…"
> write {"path":"a.txt","content":"hello\n"}
! Blocked: needs approval (permission mode ask) and a headless run cannot prompt
run ended: blocked
exit=2            (work1 empty)
$ node src/cli.ts --cwd work2 "Create a file named notes.md containing one line: hi…"
> write {"path":"notes.md","content":"hi\n"}
I created `notes.md` with the single line "hi".
exit=0
$ node src/cli.ts --cwd work3 --permission-mode ask --on-blocked continue "…If that is blocked, say so…"
! Blocked: needs approval (permission mode ask) and a headless run cannot prompt
The write tool is blocked: … so a.txt wasn't created.
exit=0
$ node src/cli.ts --cwd work4 --json "Run exactly this bash command: chmod 777 notes.md"
exit=2; events: tool_call → permission{deny, headless, "dangerous: chmod 777 changes permissions, …"} → tool_result "Blocked: dangerous: …" → agent_end{blocked}
notes.md still -rw-r--r--; the session JSONL has the same permission entry. Live reason ≈190 chars (limit stated in input + schema; 0 retries seen).
```

**Notes for the Tester**
- Two choices against the advisor or the letter of the spec, both on purpose. (1) The system prompt is exactly §11.1 (the AC says "equals"); the 200-char limit from the 203 carry-forward is stated in the schema `description` and in the input's last line, and the parser truncates. (2) The `permission` AgentEvent carries `reason?`, a superset of spec §4.
- A user `n` answers the model with the fixed `Blocked: denied by the user` (no classifier reason). A user `once`/`session` in auto records `source: user` with the classifier's reason.
- A headless deny with `stop` aborts the run's signal. Later calls in the same batch get `Not run: aborted.` (the existing abort path), so the context stays valid on the wire for `--resume`.
- `tuiPrompter` takes `TUI & {getFocusedComponent()}` because pi-tui's `TUI` interface lacks the getter its classes have. Prompts are not queued: the loop runs calls one at a time. Task 210 (parallel codemode `tools.x()`) may need a queue.
- `primaryArgument` in `tui/tool-card.ts` (first line of the command, for cards) and `permissions/summary.ts` (full command, for prompts) are separate on purpose. The 4-line relative-path logic is repeated so `src/tui` keeps importing nothing that reaches `tools/`.
- Things to probe: Esc/Ctrl+C on the permission prompt (deny, not abort); Shift+Tab while a prompt is showing (the prompt has the focus, so it is ignored until answered); a cached verdict reused after a mode toggle; `--on-blocked` passed in the TUI (harmless, since the TUI always has a prompter).
- Stale `apps/harness/README.md` ("Today it is a project skeleton") is out of scope; flag for 213.
- Known placement: `tuiPrompter` uses `tui.addChild`, so the dialog renders below the status bar (the footer is not the last line while a prompt is up). A later task can move it above the input.
- AC tension for the [HUMAN] auto walkthrough: the AC expects `echo hi > /tmp/x` to run unprompted, but the verbatim §11.1 prompt calls writes outside cwd dangerous, so the classifier may prompt on it. That comes from the spec text; it is not a gate bug.
- Ctrl+C or Esc on a permission prompt is a deny, not an abort; the run continues, with the model told it was blocked. To quit, press Ctrl+C again once the prompt is gone (it aborts a running turn, or quits when idle).

### [Tester] 2026-10-25 01:15 — QA

**Test summary**
- Format / lint / pre-commit: PASS (`make harness-lint-fix && make harness-lint-check && make harness-typecheck && make harness-tests && make pre-commit`, exit 0, `Env target: local`)
- Unit tests: 445 passed / 0 failed (33 files, live OpenRouter tests included)
- Integration tests: N/A (none in this app)
- Warnings: 0 from the toolchain (the `[tree-agent] warn:` lines in the log are asserted by tests that feed malformed files)
- `pnpm install --frozen-lockfile`: "Already up to date". `src/loop/agent-loop.ts`: 175 lines. `grep -rln "provider/\|loop/\|tools/" apps/harness/src/tui`: empty.

**E2E adversarial pass** (scratch drivers in `.../scratchpad/qa-206/`, fake HOME + TREE_AGENT_HOME)
- Happy path, live (`node src/cli.ts --cwd <scratch>`): `auto` write of notes.md → file created, exit 0 (PASS). `--permission-mode ask` → `! Blocked: needs approval…`, `run ended: blocked`, exit 2, dir empty (PASS). `--permission-mode ask --on-blocked continue` → model reports "blocked", exit 0 (PASS). `--json` + `bash chmod 777 /etc/hosts` in auto → `tool_call → permission{deny, headless, "dangerous: …"} → tool_result "Blocked: dangerous: …" isError → agent_end{blocked}`, exit 2 (PASS).
- Bad flags (spawned, exit code each): `--permission-mode hi|=|ASK|bypass|<missing>`, `--on-blocked skip|=|""|<missing>` → exit 1 with a message + usage, no stack (PASS).
- Fail-closed (AgentSession, auto, headless, no prompter): classifier throws; `dangerous:"no"`; `{dangerous:false}` missing fields; string answer; `null`; never answers (10003 ms real) → every one `deny/headless`, `agent_end(blocked)`, file NOT written (PASS). Over-long `reason` (500 chars) truncated to 199+`…`, not thrown (PASS).
- Cache: same write ×2 + different path → 2 `complete` calls; after `clear()` same input → fresh call (2 total across the clear); cached `dangerous:true` second identical call denied with 0 extra requests; a timed-out first attempt is not cached (2nd attempt re-asks, `deny, allow`) (PASS).
- Read-only/session: `read` never classified or prompted in auto (0 requests); ask with `session` answers → prompted `[bash, write]` only, 2nd bash not prompted, 0 classifier calls (PASS).
- Audit: event order `tool_call → permission → tool_result` per call; JSONL order `message:assistant, permission, message:tool`; `appendFileSync` (sync) so an entry survives a crash before the tool; deny gives `Blocked: dangerous: nope`, `isError: true`; `store.load()` of that file parses all entries with no "not a session entry" warning (PASS).
- Summary builder (`primaryArgument`): bash (cut at 2 KB + marker for 5 KB), write inside → `src/a.ts`, outside → `/etc/hosts`, `../../x`, `~/.zshrc` shown as given; MCP emoji input cut at 2048 B, no U+FFFD; codemode = description + script, 2 KB + marker (PASS).
- `decide<T>`: late rejection after timeout → no unhandled rejection; late resolve after timeout → cache stays empty; `timeoutMs:0` rejects `DecisionTimeout`; cache hit returns same object (PASS). §11.1 system prompt parsed from `docs/notes/tree-agent-harness-spec.md` equals `DANGER_CHECK_SYSTEM` byte-for-byte (PASS). Wire schema of `z.preprocess(clipReason, DangerCheck)` via zod-to-json-schema `openAi` target keeps 3 required props + `maxLength:200` (PASS).
- TUI (FakeTerminal through `main`): in ask, stray `x`/Enter/Shift+Tab ignored while the dialog is up; `Y` = once; next bash prompts again; `s` → next bash silent; `write` still prompts (per tool name); `Esc` → `Blocked: denied by the user`, file absent, session continues; Shift+Tab → status ` · auto · ` and back ` · ask · `; `settings.json` content and mtime unchanged (PASS).
- Gate bypass for inactive/hidden tools: `execute()` still answers `Unknown tool` after a "once" (no bypass); note below (PASS).
- **Abort mid-classifier: FAIL.** Driver `scratchpad/qa-206/pA.ts`: auto, headless, classifier answer delayed 1500 ms returning `dangerous:false`, `session.abort()` at 400 ms, model calls `write a.txt`. Result: `prompt()` → `aborted` but **`a.txt` exists** and events are `… tool_call, permission:allow, tool_result:"Wrote 1 bytes to a.txt", agent_end:aborted`. The write ran after the user cancelled. Expected: no tool runs after an abort; `Not run: aborted.`

**Mutation spot-check** (files `cp`-backed up in the scratchpad, sha256 identical after restore)
- gate fail-open (`dangerous: true`→`false` in `classify` catch) → 2 failed (`gate.test.ts > fails closed…`, `cli.test.ts > the classifier is down…`)
- drop `run.abort(BLOCKED)` → 4 failed (3 in `cli.test.ts`, `session.test.ts > in ask without a prompter…`)
- cache key = tool name only → 1 failed (`decision-model.test.ts > classifies the same (tool, input) once per cache…`)

**Acceptance criteria**
- [x] PASS — read-only / session / ask / auto / fail-closed / cache — `tests/permissions/gate.test.ts`, `decision-model.test.ts`, plus the probes above
- [x] PASS — headless defaults, `--permission-mode`, `--on-blocked`, exit 2 — `cli.test.ts > main (permissions)` + live runs above
- [x] PASS — audit entry + event before `tool_result` — `session.test.ts > records every resolution…`, `cli.test.ts > --json shows each permission event…`, JSONL read above
- [x] PASS — summary builder — `tests/permissions/summary.test.ts` (10) + probe above
- [x] PASS — `decide<T>()` — `tests/decision/decide.test.ts` + probes above; §11.1 equality verified independently
- [x] PASS — Shift+Tab / `/mode auto` — `cli.test.ts > main (TUI permissions)`, driver `pT.ts`
- [ ] [HUMAN] TUI ask/auto walkthrough — awaiting human verification
- [ ] [HUMAN] decoy-home `~/Downloads` — awaiting human verification (not run, by instruction)
- [x] PASS — cadence green

**Other issues found**
1. **FAIL (blocking): a tool runs after the run was aborted during the classifier call.** Root cause: `loop/agent-loop.ts` `execute()` checks `cfg.signal.aborted` only BEFORE `await beforeToolCall`; `decide()` (`src/decision/decide.ts`) creates its own `AbortController` that is linked only to the timeout, never to the run's signal, so Ctrl+C/Esc/SIGINT/SIGTERM during the (up to 10 s) classification neither cancels the request nor stops the call. If the verdict is `dangerous:false` the `write`/`edit`/`bash` executes with an already-aborted signal (`write` completes). Same path in the TUI (Esc before the dialog appears) and headless (SIGINT). Fix: (a) thread the run signal into the classifier (`Classifier(req, signal)` → `decide(..., {signal})`, abort the request when either fires) and (b) re-check `signal.aborted` after `beforeToolCall` in `execute()` (answer `Not run: aborted.`), or in `#gate` return `{run:false}` when `this.#run?.signal.aborted`. Add a regression test (delayed classifier answer + abort mid-flight → file absent, `agent_end(aborted)`). Keep `agent-loop.ts` ≤ 200 lines (175 now).
2. Note: classifier failure reasons embed the raw zod issue JSON (multi-line, pretty-printed) in `reason`, e.g. `the safety classifier failed ([\n  {\n "code": "invalid_type", …`; it lands in the prompt, the transcript notice, the `Blocked:` text to the model and the JSONL. Suggest collapsing to one line / first issue.
3. Note: `primaryArgument` clips a `bash` command at 2 KB (marker ` … (cut at 2 KB)`), so a user pressing `y`/`s` sees only the head of a >2 KB command; the SWE note says "the whole bash command". The marker makes it honest, but consider denying or showing the tail for over-cap commands.
4. Note: the gate runs for registered-but-inactive (`deferred`/`hidden`) tools, so the user is prompted (or the classifier is paid, or a headless run exits 2) for a call the registry then answers `Unknown tool`; an `s` answer would also add that name to `sessionAllow` before it is ever activated. No bypass (verified), only noise.
5. Note: `reason.slice(0, 199)` can split a surrogate pair (an emoji at the cut → lone surrogate); `write` to `~/.zshrc` is shown as `~/.zshrc` though the tool treats `~` as a literal directory in cwd.
6. Note (SWE's own flag): the verbatim §11.1 prompt calls writes outside cwd dangerous, so the [HUMAN] `echo hi > /tmp/x` unprompted expectation may not hold; spec text, not a gate bug.
7. Hygiene: I ran the live `chmod 777 /etc/hosts` probe; it was denied by the gate (exit 2) and nothing outside the scratch dir changed.

**VERDICT: FAIL** — one blocking finding (#1: a write/edit/bash can execute after the user aborted while the auto classifier was in flight). Everything else passes; the six non-HUMAN ACs are met on the happy and failure paths and will tick PASS again once #1 is fixed and re-verified.

### [SWE] 2026-10-25 01:40 — Fixes

**Blocking fix: a tool ran after an abort during the classifier call**
- `src/decision/decide.ts`: new `signal?` option (a superset of spec §12, like `cacheKey`). The caller's signal and the deadline both abort one controller, and that controller's signal goes to `provider.complete`. Either one rejects with its reason. An already-aborted signal rejects before any request. The listener is removed in `finally`. Cache writes happen only after the race resolves, so an aborted attempt is never cached.
- `src/permissions/decision-model.ts`: `Classifier` is now `(req, signal?)` and passes the signal on to `decide`.
- `src/permissions/gate.ts`: `GateDeps.signal?`. If the classifier rejects after an abort, `resolve` rethrows instead of failing closed, so nothing is prompted after Esc, Ctrl+C or SIGINT.
- `src/session.ts` `#gate`: passes the run's signal. If the run aborted while resolving (a rejection, or an answer that arrived after the abort), it returns `{run: false}` before the permission entry or event. No decision was made, so nothing is recorded, the same as for the rest of an aborted batch.
- `src/loop/agent-loop.ts` `execute()`: after `beforeToolCall` returns, an abort that is not `BLOCKED` answers `Not run: aborted.` whatever the gate said. A `BLOCKED` abort still answers `Blocked: <reason>`. The file is 180 lines (limit 200).

**Folded-in fixes**
1. Classifier failure reasons are now one line. A zod error contributes its first issue (`path: message`), any other error its message with whitespace collapsed. The full `the safety classifier failed (…)` reason is cut to 200 characters.
2. `shortReason()` (decision-model.ts) cuts to ≤ 200 UTF-16 units, which is what zod's `.max(200)` counts. It drops a trailing high surrogate before the `…`. Using `Array.from(...).slice(0, 200)` instead would allow up to 400 units, fail `.max(200)`, and fail closed on a valid verdict. Used by `clipReason` and the gate's failure reason.
3. The gate now looks up the tool in `tools.declared()` (direct ∪ activated; hidden never), the same set `#callable` allows for `origin: "model"`. A deferred-but-inactive or hidden tool now answers `Unknown tool` with no prompt, no classifier call and no `permission` entry, and an `s` answer can no longer pre-approve it. The registry already tracks activation, so 209 needs nothing. Note for 210: codemode-origin calls will need the `codemode` exposure set, not `declared()`.
4. The wording in the earlier SWE entry is fixed in place: the bash command is cut at 2 KB with a marker.

**New tests**
- `tests/loop/agent-loop.test.ts > answers Not run: aborted. when aborted while the gate waited, even if it then allows`
- `tests/decision/decide.test.ts > decide with a signal > rejects with the signal's reason on abort, cancels the request and caches nothing` and `> rejects at once without a request when the signal is already aborted`
- `tests/permissions/decision-model.test.ts > cuts an over-long emoji reason without splitting a surrogate pair, and it still parses` and `> cancels the request when the caller's signal aborts mid-flight`
- `tests/permissions/gate.test.ts > collapses a classifier failure to one short line: a zod error's first issue` and `> rejects without prompting when the run is aborted during the classifier call`
- `tests/session.test.ts > AgentSession permissions > runs nothing when aborted while the classifier is asked, and records no decision` (the regression for pA.ts: a late `safe` answer that ignores the signal; asserts the file is absent, `Not run: aborted.`, no `permission` event, `agent_end(aborted)`, and that the classifier's signal was aborted) and `> does not gate a registered tool the model cannot call yet; activated, it is gated`

**Tests**
- Unit: 454 passed, 0 failed, 33 files (`make harness-tests`, live OpenRouter tests included). There were 445 before.
- Mutation probes (files restored, `cmp`-verified): loop re-check removed → 2 tests fail; `declared()` → `get()` → 1 fails; gate abort rethrow removed → 1 fails; decide's signal listener removed → 3 fail.

**Evidence**
```
$ make harness-lint-fix && make harness-lint-check && make harness-typecheck && make harness-tests && make pre-commit
Checked 73 files … No fixes applied.
pnpm exec tsc --noEmit                       (clean)
 Test Files  33 passed (33)
      Tests  454 passed (454)
biome check (harness)....Passed  (prettier/ruff Passed)

$ node …/scratchpad/qa-206/pA.ts             # the Tester's repro
reason aborted file exists false
[turn_start, text_delta, turn_end, tool_call, tool_result:Not run: aborted., agent_end:aborted]

# live, scratch cwd, fake HOME + TREE_AGENT_HOME; SIGINT 50 ms after tool_call (classifier in flight)
$ node …/scratchpad/sigint-driver.mjs src/cli.ts …/e2e-fix/work2   # "touch made.txt", --json, auto
turn_start | turn_end | tool_call | tool_result:Not run: aborted. | agent_end:aborted
exit 1 made.txt exists false
$ node src/cli.ts --cwd …/e2e-fix/work "Create a file named b.md containing one line: hi. Then stop."
> write {"path":"b.md","content":"hi\n"}
exit=0   (b.md created)
```

**Notes**
- When the run aborts while the gate is resolving, nothing is recorded: no `permission` entry and no event. The call's tool message is `Not run: aborted.`, as for every other call cut off by an abort.
- An abort while a TUI prompt is up (only possible via SIGTERM; Esc and Ctrl+C on the prompt deny) is caught by the loop's re-check, so even a later `y` does not run the tool.

### [Tester] 2026-10-25 02:10 — Re-QA (abort fix)

**Test summary**
- Cadence `make harness-lint-fix && make harness-lint-check && make harness-typecheck && make harness-tests && make pre-commit`: PASS (exit 0; Biome 73 files; 33 files / 454 tests passed; prettier, ruff, biome hooks Passed)
- Warnings: 0
- `agent-loop.ts`: 180 lines (<= 200). `grep -rln "provider/\|loop/\|tools/" apps/harness/src/tui`: empty. `pnpm install --frozen-lockfile`: "Already up to date". Spec §11.1 prompt vs `DANGER_CHECK_SYSTEM`: identical (extracted from the spec doc and compared). `git status`: no `.env*` or spec-doc changes (the spec doc stays untracked, untouched).

**E2E adversarial pass** (scratch dir, fake HOME and TREE_AGENT_HOME; drivers `scratchpad/qa-206/pJ.ts`, `pK.ts`, `pL.ts`, `sigterm-driver.mjs`)
- Repro `pA.ts` (auto, classifier answers `safe` after 1500 ms, abort at 400 ms): `reason aborted file exists false`, events `… tool_call, tool_result:Not run: aborted., agent_end:aborted` (PASS; FAIL #1 fixed)
- Live SIGINT (`sigint-driver.mjs`, real model, 50 ms after `tool_call`): `tool_call | tool_result:Not run: aborted. | agent_end:aborted`, exit 1, `made.txt` absent (PASS)
- Live SIGTERM (same driver with SIGTERM): identical events, exit 1, no file (PASS)
- Pre-aborted signal: `decide(...)` rejects with the signal's reason, 0 provider requests (PASS)
- Batch of two writes, abort at 60 ms during the first classification (classifier answers at 300 ms): both results `Not run: aborted.`, exactly 1 classifier request, neither file, 0 `permission` events/JSONL entries (PASS)
- Dialog up in `ask`, `session.abort()`, then the prompter answers `once` / `session` / `deny`: all three end `aborted`, `Not run: aborted.`, no file, 0 permission events, 0 JSONL permission entries (PASS)
- Abort at 50 ms, classifier then THROWS at 200 ms (ignores the signal): `aborted`, `Not run: aborted.`, no prompt (prompter that throws was never called), no `error` event, 0 unhandled rejections (PASS). Control without an abort: `blocked`, one `deny headless` entry (PASS)
- Timeout boundary, decide level (40 ms timeout, abort at 36..44 ms): every run rejects (user at <=40, DecisionTimeout at >=41), nothing cached (PASS). Session level (real 10 s timeout, abort at 9980 / 9995 / 10000 / 10005 / 10020 ms): abort first gives `Not run: aborted.` with no entry; timeout first gives `Blocked: …danger_check: no answer within 10000 ms` + one `deny headless` entry; never a file, never a hang, 0 unhandled rejections (PASS)
- Zod-failing classifier answers (wrong type, `{}`, bad enum), `onBlocked: continue`: event reason, JSONL reason and `Blocked:` text are one line and identical, e.g. `dangerous: the safety classifier failed (risk: Invalid enum value. Expected 'low' | 'medium' | 'high', received 'extreme')` (122 chars); the transcript notice is one `Text` (PASS)
- Emoji at the 200-unit cut, 32 cases (pad 190..205 x 😀 / ZWJ family), valid `dangerous:true` verdict: accepted (not fail-closed), `isWellFormed()`, <= 200 units (PASS). Failure message ending in emoji, 26 cases: well-formed (PASS)
- Deferred-inactive, hidden and unknown tool in one batch, `ask` and `auto`, prompter that answers `session`: all three `Unknown tool: …`, 0 prompts, 0 classifier requests, 0 permission entries; `s` did not pre-approve (PASS)
- `tools.activate(["mcp__x__def"])`, then call: still gated (`ask`: prompter asked once; `auto`: 1 classifier request), runs only after the gate (PASS)

**Mutation** (files `cp`-backed up, `shasum -a 256 -c` OK after restore)
- Post-gate re-check in `agent-loop.ts` disabled (`if (false && …)`) -> 2 failed: `agent-loop.test.ts > answers Not run: aborted. when aborted while the gate waited, even if it then allows`, `session.test.ts > … runs nothing when aborted while the classifier is asked, and records no decision`
- `decide.ts` signal listener removed (the run signal no longer reaches the controller) -> 3 failed: `decide.test.ts > … rejects with the signal's reason on abort, cancels the request and caches nothing`, `decision-model.test.ts > … cancels the request when the caller's signal aborts mid-flight`, the `session.test.ts` regression

**Flake check:** `tests/session.test.ts` x5 in a row: 36 passed each time (the real-timer regression included).

**Acceptance criteria**
- [x] PASS — all six non-HUMAN ACs re-verified (gate modes, headless flags and exit 2, audit entry before `tool_result`, summary builder, `decide<T>()`, Shift+Tab and `/mode auto`) via the green full suite plus the probes above. FAIL #1 is resolved.
- [ ] [HUMAN] TUI ask/auto walkthrough — awaiting human verification
- [ ] [HUMAN] decoy-home `~/Downloads` — awaiting human verification (not run, by instruction)

**Other issues found (notes, non-blocking)**
1. A prompt answered `s` after the run was aborted (only reachable via SIGTERM, since Esc and Ctrl+C on the dialog deny) still adds the tool to `sessionAllow` (`gate.ts`, `resolve`), although the call is `Not run: aborted.` and no entry is recorded. The user did press `s`, so it is defensible.
2. A failure reason for a schema issue at path `dangerous` reads `…failed (dangerous: Expected boolean…)` and gets the prefix `dangerous:` once more in the `Blocked:` text (`Blocked: dangerous: the safety classifier failed (dangerous: …)`). Cosmetic.
3. Earlier notes still open and unchanged: `bash` over 2 KB is shown cut at 2 KB in the dialog (marked); `write ~/.zshrc` is displayed as `~/.zshrc` though the tool treats `~` literally.

**VERDICT: PASS**
