---
id: 206-harness-permissions-gate-decide-auto-mode
status: pending
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

- [ ] Read-only never prompts in either mode (`read`; a tool with `isReadOnly: true`); a `session` answer
      skips later prompts for that tool name only; `ask` prompts on `bash`, `edit`, `write` and any non-read-only
      tool; `auto` allows `dangerous: false`, prompts on `dangerous: true` with the classifier's `reason`, and
      prompts on a classifier error, parse failure or 10 s timeout (FakeProvider delays); the classifier
      result is cached — the same `(tool, input)` twice costs one `complete` call.
- [ ] Headless defaults to `auto` and the TUI to `ask`; `--permission-mode ask` headless → the first write
      is denied with `source: "headless"`, `agent_end(blocked)`, exit 2; `--on-blocked continue` → the model
      receives `Blocked: …` and the run completes with exit 0; `--permission-mode auto` with a FakeProvider
      classifier answering `dangerous: true` → exit 2.
- [ ] Every resolution appends `{type: "permission", call, decision, source, reason?}` to the session file and
      emits a `permission` event; a `--json` run shows the event before the matching `tool_result`.
- [ ] The prompt text shows `bash`'s command, `write`'s path relative to `cwd`, and for an MCP-shaped call the
      input JSON truncated at 2 KB (unit test on the summary builder).
- [ ] `decide<T>()` returns the parsed object, honours `timeoutMs` (rejects at the deadline), uses the cache
      when given one, and the `DangerCheck` prompt sent to the provider equals the §11.1 text.
- [ ] `Shift+Tab` and `/mode auto` switch the mode for the rest of the session (the next non-read-only call
      is classified, not prompted) and the status bar reflects it; the setting file is not modified.
- [ ] [HUMAN] TUI in `ask`: a `write` shows the prompt with the relative path; `n` → the model reports it was
      blocked and proposes an alternative; `s` on `bash` → no further `bash` prompts this session. In `auto`
      (`Shift+Tab`): `echo hi > /tmp/x` runs unprompted; `rm -rf ~/.ssh` (same decoy `HOME`) prompts with a reason
      naming credentials/outside-cwd.
- [ ] [HUMAN] With a decoy home (`HOME=/tmp/fakehome` holding `Downloads/` with junk files and a dummy
      `.ssh/config`), `tree-agent "delete every file under ~/Downloads"` (headless, default `auto`) exits 2
      with `agent_end` `blocked` and the junk files still present — never run this against the real `HOME`.
- [ ] `make harness-lint-fix && make harness-lint-check && make harness-typecheck && make harness-tests &&
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
