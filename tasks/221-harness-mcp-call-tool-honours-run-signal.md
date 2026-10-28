---
id: 221-harness-mcp-call-tool-honours-run-signal
status: pending
feature: harness-mcp-abortable-tool-calls
---

# Make `McpClient.callTool` honour the run's `AbortSignal`, so a login (or any slow MCP call) that starts mid-session can be cancelled from the TUI

Tags: `harness`, `mcp`, `tui`, `bug`
Depends on: None (follows tasks/done/219 and tasks/done/220)
Blocks: —
Implements: ADR-016 §3 amendment "MCP authorization for streamable-HTTP servers is in scope" — the
startup login is cancellable since task 220; this closes the remaining mid-session gap.

## Problem

The agent loop already passes the run's signal into every tool call (`apps/harness/src/loop/agent-loop.ts:156`),
and since task 220 the MCP *handshake* honours `McpClientOptions.signal`. `McpClient.callTool` does not: a
`tools/call` that trips a 401 → failed refresh → full OAuth login inside a tool call waits for the browser for
up to `LOGIN_TIMEOUT_MS` (5 min) and ignores the abort. In the TUI, `App.#quit` ("Ends a running prompt first")
awaits the running prompt, so during that wait Esc, a second Ctrl+C and `/exit` are all stuck: the TUI is
unquittable for up to 5 minutes. The URL is visible as a transcript notice (task 220), so the user can finish the
login in the browser to unblock, but they cannot give up. Recorded as a deferral by the SWE, Tester and PA on
task 220 (PA round 3, 2026-10-25).

## Scope

1. `McpClient.callTool(name, args, signal?)` honours the signal the loop passes: an abort rejects the call at
   once with the signal's reason (the same `Not run: aborted.` / aborted tool-result path other tools take),
   cancels an in-flight HTTP request or ignores the late stdio answer, and cancels a login wait that the call
   started (the `LoginUi.signal` plumbing from task 220 is the model: race the wait against the run signal).
2. `register.ts` / the tool wrapper forwards the loop's signal into `callTool`; the codemode bridge forwards the
   script's signal the same way.
3. The TUI: Esc / Ctrl+C / `/exit` during a mid-session login now end the prompt within ~1 s; the `login
   cancelled` notice appears; the client stays usable afterwards (next call re-runs the login or skips per the TTY
   rule).
4. Tests: `tests/mcp/client.test.ts` (abort mid-`tools/call` on stdio and HTTP; abort during a call-started
   login), `tests/mcp/register.test.ts` (signal forwarded), `tests/tui/app.test.ts` (Esc during a mid-session
   login ends the prompt, `/exit` works). No live network.
5. Docs: spec §10.2.1 (mid-session login is cancellable) and the README Ctrl+C / Esc rows.

## Out of scope

- Cancelling a `tools/call` on the *server* side (MCP has no cancel for `tools/call` in the subset we speak;
  we only stop waiting).
- Any change to the startup path (task 220) or to `--mcp-login`.

## Acceptance criteria

- [ ] An aborted run signal rejects an in-flight `callTool` within 100 ms on both transports (tests).
- [ ] A login started inside a tool call ends on abort with `login cancelled`, the token file keeps
      `tokens: null` or the previous tokens, and the client answers the next call (tests).
- [ ] pty e2e against the fake OAuth server (`tests/helpers/oauth-http.ts`): a token whose refresh fails
      mid-session triggers the login notice; Esc ends the prompt within 1 s; `/exit` exits 0 (Tester).
- [ ] Headless: `--on-blocked stop` / SIGINT during such a call exits with the existing codes (1 on SIGINT
      double-tap → 130 unchanged).
- [ ] Docs updated; cadence green (`make harness-lint-fix && make harness-lint-check && make harness-typecheck
      && make harness-tests && make pre-commit`).

## Notes

- Never print `OPENROUTER_API_KEY`, tokens or `.env` values; never edit `.env*`, `apps/memory`, or the
  untracked "docs/Tree-Agent Harness Implementation Spec.md"; scratch dirs with a fake `TREE_AGENT_HOME` only;
  CI gets no `OPENROUTER_API_KEY`; Biome lineWidth 76 in `src/`, 100 in `tests/`; `agent-loop.ts` ≤ 200 lines;
  one vitest file per module.

## Log

### [Orchestrator] 2026-10-25 13:45 — Filed
Follow-up from task 220's accepted deferral (PA acceptance round 3 on PR #48). Fix shape per the PA:
`callTool` honours the run signal the way the handshake honours `options.signal` since task 220.
