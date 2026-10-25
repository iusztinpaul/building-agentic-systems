---
id: 220-pa-rejection-harness-mcp-oauth-streamable-http
status: pending
feature: tree-agent-harness
---

# [PA rejection] MCP authorization for streamable-HTTP servers (219) + decision-model docs (218)

Tags: `rollup`, `pa-rejection`, `harness`, `mcp`, `docs`
Refs: `tasks/done/219-harness-mcp-oauth-streamable-http.md` (lead), `tasks/done/218-harness-decision-models-openrouter-decisions-api.md` (one wording item)

## Scope

Tasks 218 and 219 PASSED automated QA (995/995, four live tests, the hosted login verified with the owner
through `--mcp-login`) but 219 fails the user-perspective acceptance review on its FIRST user story, "First
login from the TUI": the code starts the TUI before the MCP servers connect, so the `log in at <url>` line,
the 5-minute timeout warning and the skip warning are written to a stderr the TUI already owns. A TUI user
with the hosted entry and no token sees an empty screen for up to 5 minutes, cannot Ctrl+C out of it, and —
when the browser does not open (SSH, Linux without `xdg-open`, a sandbox) — never sees the URL. The README
and the spec describe the stderr line as if a TUI user gets it. The SWE recorded this as a finding and left
it; it is in scope (story 1 of 219), so it is fixed here. The remaining items are wording drift in the docs
the SWE and Tester already flagged.

The SWE fixes every issue below in a single coordinated pass, then hands back to the Tester (full pipeline
re-runs from QA). Settled decisions are NOT re-opened: hand-rolled OAuth, `--mcp-login` forcing a fresh
login, the 60 s `initializeTimeoutMs` default, the user-level hosted entry, `decisionModel` /
`smallModel` / `dangerThreshold` 0.3.

## Acceptance Criteria

- [ ] Issue 1: in the TUI (stdin and stderr TTYs, no token file, the hosted entry configured), a user sees
      the `mcp "<server>": log in at <url>` line ON THE TUI SCREEN while the login waits (not only on the raw
      stderr the TUI overdraws), can press Ctrl+C during that wait to leave with the terminal restored
      (exit 0 or 1, no hang), and sees the `login timed out after 5 min; run tree-agent --mcp-login <server>
      to log in` and `needs a login; …` lines on the screen too when they fire. A `tests/tui/app.test.ts`
      case (through `onStart`, as the trust prompt is tested) proves the URL line is rendered and Ctrl+C
      ends the wait. If the SWE instead chooses the documented-caveat route (Issue 1, option b), the AC is:
      the README hosted-server section, spec §10.2.1 (TTY rule) and the 219 story 1 say in one sentence
      that the TUI does not show the URL and that `tree-agent --mcp-login <server>` is the way to log in
      before the first TUI run; Ctrl+C during the TUI's startup wait still works (the trust prompt's
      pattern) — no silent 5-minute hang.
- [ ] Issue 2: `docs/adrs/016_tree_agent_harness.md`, amendment "Decision models are real decision models":
      "destructive ones 0.95–0.99" reads "destructive ones 0.87–0.99" (the recorded jev-1.14 fixture scores
      0.87; spec §11.1 and the README already say 0.87–0.99).
- [ ] Issue 3: ADR-016 amendment "MCP authorization …" and spec §1 "MCP client" row: "~300 lines, stdlib
      only: `crypto`, `http` for the loopback redirect, `fetch`" reads "~650 lines (`mcp/auth.ts` +
      `mcp/auth-store.ts`), no new dependency: `crypto`, `http`, `fetch` and the already-installed `zod`";
      the spec §1 row's "~300 lines" for the whole MCP client is corrected to the real figure or dropped.
- [ ] Issue 4: `docs/glossary.md` **tree-agent** row Notes: "its tools register as
      `mcp__tree_memory_local__<tool>`" reads "their tools register as `mcp__tree_memory_local__<tool>` and
      `mcp__tree_memory__<tool>`".
- [ ] Issue 5: the `"<server>" did not ask for a login` outcome of `--mcp-login` (exit 0; the server
      answered `initialize` without a 401) is listed in spec §10.2.1's CLI bullet and the README
      `--mcp-login` flag row.
- [ ] Tester re-runs the full QA suite and PASSES (`make harness-lint-fix && make harness-lint-check && make
      harness-typecheck && make harness-tests && make pre-commit`).
- [ ] PA re-runs the acceptance review on 219 (and the 218 wording item) and ACCEPTS.

## Issues (detail)

### 1. The TUI's first login is invisible and un-interruptible — `apps/harness/src/tui/app.ts:39`, `apps/harness/src/cli.ts` `loginUi()` / `interactive`
- **What the user experiences (wrong):** `runTui` calls `tui.start()` (pi-tui takes the screen, raw mode)
  and only then `o.open()`, which runs `startMcp` with `login: loginUi(deps, true)`. `loginUi.notify` writes
  to `deps.stderr`; the timeout and skip warnings go through `log.warn` → stderr. There is no "connecting"
  state and no log sink on the screen (`grep` of `src/tui/*.ts`). So after the trust prompt the user sees an
  empty TUI for up to `LOGIN_TIMEOUT_MS` (5 min). pi-tui hands Ctrl+C to the focused component
  (`pi-tui/dist/tui.js:773–781`: no focused component → the input is dropped) and nothing is focused
  between the trust prompt and `App` — for a trusted cwd (`askTrust: false`) nothing is ever focused before
  `App` — and `App`'s `interrupt` handler exists only after `open()` resolves, so Ctrl+C is swallowed for the
  whole wait. On macOS the
  browser opens blindly and the common case "works"; over SSH, on Linux without `xdg-open`, or when `open`
  fails, the user has no URL, no way out, and after 5 minutes an overdrawn warning and a session missing the
  server with no on-screen reason. The same applies to a mid-session login after a failed refresh (spec
  §10.2.1: "the next call runs the login").
- **What the spec / good UX implies (right):** 219 story 1: "On startup stderr shows `mcp "tree-memory":
  log in at …` and the browser opens; the user logs into Horizon; … the TUI opens". README "The hosted
  `tree-memory` server": "opens your browser (printing the URL on stderr as well)". A TUI user must see the
  URL where they are looking and must be able to leave.
- **Suggested fix (SWE decides):** (a) preferred, least code: a TUI-mode `LoginUi` whose `notify` renders
  the line on the screen — e.g. `runTui` passes a notice sink into `open()` (the trust prompt already shows
  how to render before `App` exists), `cli.ts` builds `loginUi` from it for the TUI branch, and a focused
  component during `open()` turns Ctrl+C into the `#closing` abort (the `McpClient.close()` path already
  cancels a login wait). Route the startup `log.warn` lines through the same sink while the TUI is up, or at
  least the two login ones. (b) acceptable minimum: keep the behaviour and make the docs true of it — one
  sentence in the README hosted-server paragraph, spec §10.2.1 and 219 story 1: "in the TUI the URL is not
  shown; run `tree-agent --mcp-login <server>` once before the first TUI run" — plus the Ctrl+C escape,
  which is required either way (a 5-minute un-interruptible blank screen is not acceptable in any variant).

### 2. ADR-016 (218 amendment) states a range the evidence contradicts — `docs/adrs/016_tree_agent_harness.md`, "Decision models are real decision models" paragraph
- **Wrong:** "live safe calls score 0.01–0.02, destructive ones 0.95–0.99 on both tested models".
- **Right:** the recorded `tests/fixtures/openrouter/decisions-rm-rf.json` (jev-1.14) scores 0.87; the
  SWE's and the Tester's logs both flag it; spec §11.1 and README Permissions step 4 say 0.87–0.99.
- **Fix:** "destructive ones 0.87–0.99". The PA-authored "verbatim" text is mine to correct; an amendment
  is the one ADR section that may be edited for accuracy in the same feature.

### 3. ADR-016 (219 amendment) and spec §1 understate the client — `docs/adrs/016_tree_agent_harness.md` last paragraph; `docs/notes/tree-agent-harness-spec.md` §1 "MCP client" row
- **Wrong:** "by hand in `mcp/auth.ts` (~300 lines, stdlib only: `crypto`, `http` for the loopback
  redirect, `fetch`)". `auth.ts` is 543 lines, `auth-store.ts` 116, and both import `zod`. The SWE noted
  the line count and did not edit the wording. The spec §1 row still says the whole MCP client is "~300
  lines".
- **Right:** the decision (hand-rolled, no NEW dependency) stands; the numbers and "stdlib only" must be
  true, since the ADR is what a reader trusts.
- **Fix:** the wording of AC Issue 3.

### 4. Glossary `tree-agent` row half-updated — `docs/glossary.md` line 66
- **Wrong:** the Notes now name the hosted `tree-memory` after an **MCP login** but still end "its tools
  register as `mcp__tree_memory_local__<tool>`".
- **Fix:** name both prefixes (AC Issue 4).

### 5. An undocumented `--mcp-login` outcome — `apps/harness/src/cli.ts` `mcpAuthCommand` (the `did not ask for a login` branch)
- **Wrong:** a user running `--mcp-login` against a server that never answers 401 gets `"<server>" did not
  ask for a login` (exit 0); the README flag row lists every other outcome and the spec §10.2.1 CLI bullet
  too, but not this one.
- **Fix:** one clause in each (AC Issue 5).

## User Stories

(Inherit from 219 — no new stories. Story 1 "First login from the TUI" is the one that must pass after the
fix; re-verify the other six. 218's seven stories are unaffected by the wording fix.)

---

Refs: `tasks/done/219-harness-mcp-oauth-streamable-http.md`, `tasks/done/218-harness-decision-models-openrouter-decisions-api.md`

## Log
