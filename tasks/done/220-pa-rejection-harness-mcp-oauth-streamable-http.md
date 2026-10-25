---
id: 220-pa-rejection-harness-mcp-oauth-streamable-http
status: done
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

- [x] Issue 1: in the TUI (stdin and stderr TTYs, no token file, the hosted entry configured), a user sees
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
- [x] Issue 2: `docs/adrs/016_tree_agent_harness.md`, amendment "Decision models are real decision models":
      "destructive ones 0.95–0.99" reads "destructive ones 0.87–0.99" (the recorded jev-1.14 fixture scores
      0.87; spec §11.1 and the README already say 0.87–0.99).
- [x] Issue 3: ADR-016 amendment "MCP authorization …" and spec §1 "MCP client" row: "~300 lines, stdlib
      only: `crypto`, `http` for the loopback redirect, `fetch`" reads "~650 lines (`mcp/auth.ts` +
      `mcp/auth-store.ts`), no new dependency: `crypto`, `http`, `fetch` and the already-installed `zod`";
      the spec §1 row's "~300 lines" for the whole MCP client is corrected to the real figure or dropped.
- [x] Issue 4: `docs/glossary.md` **tree-agent** row Notes: "its tools register as
      `mcp__tree_memory_local__<tool>`" reads "their tools register as `mcp__tree_memory_local__<tool>` and
      `mcp__tree_memory__<tool>`".
- [x] Issue 5: the `"<server>" did not ask for a login` outcome of `--mcp-login` (exit 0; the server
      answered `initialize` without a 401) is listed in spec §10.2.1's CLI bullet and the README
      `--mcp-login` flag row.
- [x] Tester re-runs the full QA suite and PASSES (`make harness-lint-fix && make harness-lint-check && make
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

### [SWE] 2026-10-25 10:55 — Implementation

**Files modified**
- `apps/harness/src/tui/startup.ts` (new) — `StartupScreen`: the focused component shown while `open()` runs. It shows the lines startup prints (warnings in red, the login line plain) above a dim `starting… (Ctrl+C quits)`. Ctrl+C quits once and the line changes to `quitting…`. Esc is ignored, so a reflexive Esc does not end the program. The lines are sanitized.
- `apps/harness/src/tui/app.ts` — `runTui` mounts and focuses `StartupScreen` before `o.open()`, so it is focused for a trusted cwd as well. `open()` gets a third argument, `StartupUi { notify(line, error?): boolean; signal }`. `notify` draws the line on the startup screen, or as a transcript notice once `App` is up (that covers the mid-session login URL), and returns false once the TUI has stopped. Ctrl+C aborts `signal`, `runTui` waits for `open()` to settle (an error after the abort is swallowed) and returns 1. The startup lines stay on screen. A final `renderNow()` before `tui.stop()` keeps the last frame in the scrollback. The startup lines carry into the transcript as notices.
- `apps/harness/src/cli.ts` — the TUI branch builds the `LoginUi` from the screen: `notify` goes to the screen, or to stderr when the TUI is down; `signal` is the screen's Ctrl+C. `startMcp` gets a `screenLog` whose `warn` goes to the screen, or to `log.warn` when the TUI is down. That puts the `login timed out …`, `needs a login; …` and `login cancelled` lines on the screen. After `start()` resolves and the clients are pushed (so `main`'s `finally` closes them), an aborted signal throws, so no session is built. Headless and `--mcp-login` behave as before; `--mcp-login` still overrides `signal`.
- `apps/harness/tests/tui/startup.test.ts` (new, 5 tests) — wrapping and the starting line; Ctrl+C quits once and shows `quitting…`; warning red / login line plain; other keys (Esc included) ignored; escape sequences dropped.
- `apps/harness/tests/tui/app.test.ts` (+2) — through `onStart`: the login line from `open()` is on screen with `starting… (Ctrl+C quits)`, and Ctrl+C ends the wait (the signal aborts, `open` rejects): exit 1, terminal restored, `notify` returned true. A second case: a startup warning stays in the transcript in red, a mid-session `notify` shows as a (non-red) notice, and `notify` returns false after exit.
- `apps/harness/tests/cli.test.ts` (+3, "main (MCP login)", against `startFakeOAuth`) — TUI, not logged in: `log in at` on the terminal, stderr `""`, the browser is called once, the URL is on screen, the status bar shows ` · 1 MCP · `, exit 0. TUI Ctrl+C during the wait: exit 1, stderr `""`, terminal stopped, `login cancelled` and `quitting…` in the last frame, token file `tokens: null`. TUI with a redirected stderr: the `needs a login; run tree-agent --mcp-login tree-memory once, then re-run` line is on screen, stderr `""`, no browser.
- `docs/adrs/016_tree_agent_harness.md` — Issue 2: `0.95–0.99` → `0.87–0.99`. Issue 3: the amendment's parenthetical now reads "~650 lines (`mcp/auth.ts` + `mcp/auth-store.ts`), no new dependency: `crypto`, `http`, `fetch` and the already-installed `zod`". Original-Decision lines 54/116 (`~300 lines` for the base client) are untouched: they are not the amendment.
- `docs/notes/tree-agent-harness-spec.md` — §1 MCP-client row: "~2,000 lines across `mcp/`; … (task 219: ~650 lines, `mcp/auth.ts` + `mcp/auth-store.ts`, no new dependency)". The §10.2.1 intro adds `zod`. §10.2.1 "The login and its bound" adds the TUI screen, the `starting…` line and Ctrl+C → exit 1. The §10.2.1 CLI bullet adds the `did not ask for a login` outcome (Issue 5).
- `docs/glossary.md` — Issue 4: "their tools register as `mcp__tree_memory_local__<tool>` and `mcp__tree_memory__<tool>`".
- `apps/harness/README.md` — hosted-server paragraph: "printing the URL as well: on stderr, or in the TUI on its screen, where Ctrl+C quits the wait". The `--mcp-login` row adds the `did not ask for a login` outcome (Issue 5). The Ctrl+C key row and the exit-code-1 row add "Ctrl+C while the session opens: exit 1".

**Tests**
- Unit: 1005 passing, 0 failing (before: 995). `make harness-tests`: 50 files.
- Integration: N/A (the harness has no integration suite).
- `make harness-lint-fix && make harness-lint-check && make harness-typecheck && make harness-tests && make pre-commit`: all clean.

**Acceptance criteria**
- [x] Issue 1 (option a): the URL is on the TUI screen, Ctrl+C during the wait exits 1 with the terminal restored, and the timeout and skip lines are on screen. Verified by `tests/tui/app.test.ts` ("shows open()'s login line on screen while it waits; Ctrl+C ends the wait…", "keeps startup lines in the transcript…") and `tests/cli.test.ts` (the three `TUI…` cases in "main (MCP login)"). The timeout line goes through the same `screenLog.warn` path as the `needs a login` line; it has no CLI-level test because `LOGIN_TIMEOUT_MS` is a constant not exposed to `main`.
- [x] Issue 2: ADR line 223.
- [x] Issue 3: ADR amendment + spec §1 row.
- [x] Issue 4: glossary line 66.
- [x] Issue 5: README `--mcp-login` row + spec §10.2.1 CLI bullet.
- [ ] Tester QA, [ ] PA acceptance: pending.

**Evidence**
```
$ make harness-tests
 Test Files  50 passed (50)
      Tests  1005 passed (1005)
$ make pre-commit
prettier / ruff check / ruff format / biome check (harness) ... Passed
```
E2E: the real `src/cli.ts` in a pty (python `pty.fork`). Scratch `TREE_AGENT_HOME`/`HOME` under the session scratchpad, `--cwd <scratch>/work --trust`. The user-level `mcp.json` points `tree-memory` at `startFakeOAuth()`. `open` on `PATH` is stubbed: run 1 only logs the URL, run 3 `curl -L`s it to play the browser. Live OpenRouter key from `.env` (not printed).
```
run 1 (no token, browser never completes): saw 'log in at' after 1.2s; Ctrl+C → exit code 1 in 0.0s
  screen: " mcp "tree-memory": log in at" / " http://127.0.0.1:…/oauth2/authorize?response_type=code&…" /
          " quitting…" + " mcp "tree-memory": login cancelled"; token file tokens: None
run 2 (stderr redirected to a file): ' 0 MCP · ' status bar; screen shows
  'mcp "tree-memory": needs a login; run tree-agent --mcp-login tree-memory once, …'; stderr.log 0 bytes; exit 0
run 3 (curl completes the login): ' 1 MCP · ' status bar after 0.4s; token stored; Ctrl+C → exit 0
run 4 (token now stored): no 'log in at' on screen, browser not opened again (opened.log: 2 lines = runs 1 + 3)
```

**Notes**
- Still open (finding, not fixed): a mid-session login, after a failed refresh inside a tool call, now shows its URL as a transcript notice. Ctrl+C there still cannot cancel the wait: `McpClient.callTool` ignores the run's abort signal, so the run waits up to 5 min. This was already true before this change, and the AC covers startup only.
- Bounded wait: Ctrl+C cancels the login wait at once. If another server is still in `initialize`, startup still waits for that server's handshake to settle (at most `mcp.initializeTimeoutMs`, 60 s) before exiting. `McpClient.close()` does not abort an in-flight `#attempt`. The status reads `quitting…` meanwhile.
- The TTY rule is unchanged: TUI + redirected stderr skips the login with the on-screen `needs a login` line.
- The long authorize URL is hard-wrapped at the terminal width. Browsers' address bars strip the line breaks on paste. It is not an OSC 8 hyperlink.
- 219's story 1 text in `tasks/done/219-…` ("On startup stderr shows …") is not edited: it is a done task, and option (a) makes the README/spec true instead.
- `apps/harness/src/loop/agent-loop.ts` is unchanged (180 lines). `.tree-agent/mcp.json` is unchanged, `apps/memory` was not touched, and nothing is committed.

### [Tester] 2026-10-25 13:20 — QA

**Test summary**
- Suite first: `make harness-tests` -> Test Files 50 passed (50), Tests 1005 passed (1005), exit 0 (live tests ran). Re-run serially under load (8 busy loops): `cli.test.ts` 127 + `tui/app.test.ts` 21 + `tui/startup.test.ts` 5 = 153 passed.
- `make harness-lint-check` (biome, 116 files, no fixes), `make harness-typecheck` (tsc), `make pre-commit` (prettier, ruff, ruff format, biome): all PASS; `git status` unchanged by the hooks.
- Warnings: 0. Untouched: `.tree-agent/mcp.json` (md5 unchanged, not in `git status`), `apps/memory`, `.env*`, the untracked "Implementation Spec".

**E2E adversarial pass** (real `src/cli.ts` in a pty, rendered through `pyte`; scratch `TREE_AGENT_HOME`/`HOME`; stub `open` on PATH; `startFakeOAuth()` from `tests/helpers/oauth-http.ts`; `OPENROUTER_API_KEY` a dummy; drivers under `scratchpad/qa/` (`drive.py`, `scen.py`, `servers.ts`, `hl.py`))
- Happy path, login completes (stub browser `curl -L`s the URL at +2 s, 100 cols): URL line on screen under `starting… (Ctrl+C quits)`, then `1 MCP · 0 activated` status bar after 2.4 s with the `log in at` + URL lines carried into the transcript above it; token file written (0600), `starting…` gone, Ctrl+C when idle -> exit 0. PASS.
- Narrow width, 60 cols: URL hard-wrapped in 59-char lines (`mcp "tree-memory": log in at` on its own line, URL below); the lines joined without separators equal the URL the stub browser received, 350 chars == 350 chars (a copy line by line + joined reproduces it; a single-click selection does not, see note 3). PASS.
- Ctrl+C once during the wait: exit 1 at once (0.0 s), final frame `login cancelled` + `quitting…`, terminal restored (raw tail: `ESC[?25h`, `ESC[?2004l`, `ESC[<u`), token file `tokens: null` (client registration kept, as before), the pty cannot separate stderr from the screen, so "no stray stderr" rests on `cli.test.ts` "TUI: Ctrl+C while the login waits" asserting stderr `""` plus the redirected-stderr pty run (0 bytes). PASS.
- Ctrl+C three times 1 ms apart: exit 1, no crash (the process is gone after the first, later writes get EIO). PASS.
- Esc x2, "hello world", Enter, arrows, Backspace, Tab, Ctrl+D, a bracketed paste, `/exit\r`, emoji, Ctrl+L during the wait: screen unchanged (`starting…`), no echo, no exit, no crash; Ctrl+C afterwards -> exit 1. PASS.
- Warning-only, stdin TTY + stderr redirected to a file: `needs a login; run tree-agent --mcp-login tree-memory once, then re-run` on screen, `0 MCP`, `stderr.log` 0 bytes, no browser call, exit 0. PASS.
- Untrusted cwd (no `--trust`): trust prompt, `y` + Enter, then the startup screen with the URL (prompt gone), Ctrl+C -> exit 1. PASS.
- Hostile server text (an HTTP server answering 500 with `ESC[2J ESC[H ESC]0;title BEL ESC[31m NUL CR U+202E`): screen shows `mcp "evil": HTTP 500: boompwnedRED`; no `ESC[2J` in the raw stream. PASS with note 4.
- Malformed `mcp.json` in the TUI: exit 1, the error printed on the restored terminal. PASS with note 2.
- Two servers, the second never answers `initialize` (HTTP, `mcp.initializeTimeoutMs` 8 s): login completes via curl -> the session opens only after 8.3 s with `mcp "slow": initialize timed out after 8 s` as a transcript notice and `1 MCP`. PASS (spec behaviour).
- Two servers, Ctrl+C while the other is hung (timeout 20 s, Ctrl+C at +4.4 s): screen `quitting…` at once, exit 1 after 15.8 s (= the remaining handshake), the `slow ... timed out` line appears just before. A second Ctrl+C at +1 s changes nothing (the screen ignores keys once `quitting…`). Same with a stdio server that hangs (timeout 12 s, Ctrl+C at +2.8 s): exit 1 after 7.8 s, child gone afterwards (no orphan). Confirmed deferral, see the verdict.
- Login timeout (5 min): `LOGIN_TIMEOUT_MS` is a constant not reachable from the CLI, so not run end to end. Covered by `app.test.ts` ("keeps startup lines...": the `login timed out after 5 min` line is red on screen, then in the transcript) and by 219's `auth.test.ts` / `client.test.ts`; the CLI wiring (`screenLog.warn`) is the same one the verified `needs a login` and `login cancelled` lines use. SKIPPED, said so.
- Mid-session login (after a failed refresh inside a tool call): not run end to end (needs a model turn); `app.test.ts` proves the notice path (non-red notice, `notify` false after exit). Ctrl+C there does not cancel the wait: `McpClient.callTool` (`client.ts:162`) ignores any signal; `loginUi`'s `signal` is the startup one. Confirmed by reading.
- Resize during the wait: could not be verified (my `pyte` screen does not reflow; the artifacts are the driver's). The process survived SIGWINCH and exited 1 on Ctrl+C.
- Headless / `--mcp-login`, HEAD (`git archive` copy) vs the working tree, same env, outputs normalised (ports, state, challenge, client id, home): byte-identical stdout, stderr and exit code in 4 cases: (1) `--mcp-login tree-memory` piped stdin, browser played by curl: `logged in to ...` exit 0; (2) `--mcp-login` + SIGINT after 2 s: URL line, `login cancelled`, exit 1; (3) `--mcp-login nope`: `no MCP server "nope" in mcp.json; known: tree-memory`, exit 1; (4) headless `--json "say ok"` un-logged-in: the single `needs a login` warning on stderr, JSONL stdout, exit 1 (dummy key, same on both). PASS.
- Secret hygiene: in all pty transcripts (`raw.bin`), no `at-N-client`, `rt-N-client`, `code-N`, `code_verifier=` and neither stored token value (python scan of the two logged-in runs, 0 hits); token files 0600; every other file under the two logged-in scratch homes (no session JSONL: no prompt was sent; `mcp.json`, `settings.json`, `server-state.json`, `opened.log`) holds none of the 4 stored token values, `code_verifier=`, `access_token` or `refresh_token`. The URL on screen carries `state` and `code_challenge`, as the printed stderr URL always did. PASS.

**Acceptance criteria**
- [x] PASS — Issue 1: URL line on the TUI screen while the login waits (`wrap60`, `login`, `trust` runs; `app.test.ts` "shows open()'s login line on screen..."; `cli.test.ts` "TUI, not logged in: ..."), Ctrl+C ends the wait with exit 1 and a restored terminal (pty runs; `cli.test.ts` "TUI: Ctrl+C while the login waits ends it", `app.test.ts`), `needs a login` on the screen (pty `redirect` run; `cli.test.ts` "TUI with a redirected stderr"), the timeout line on the screen (`app.test.ts`, the CLI-level run skipped as stated).
- [x] PASS — Issue 2: ADR-016 line 223 reads "destructive ones 0.87–0.99" (diff read).
- [x] PASS — Issue 3: ADR-016 amendment reads "~650 lines (`mcp/auth.ts` + `mcp/auth-store.ts`), no new dependency: `crypto`, `http`, `fetch` and the already-installed `zod`" (wc: 543 + 116 = 659); spec §1 row "~2,000 lines across `mcp/`" (wc of `src/mcp/*.ts`: 2058) with the 219 figure; §10.2.1 intro adds `zod`.
- [x] PASS — Issue 4: glossary line 66 "their tools register as `mcp__tree_memory_local__<tool>` and `mcp__tree_memory__<tool>`".
- [x] PASS — Issue 5: `did not ask for a login` in README `--mcp-login` row and spec §10.2.1 CLI bullet (both exit 0).
- [x] PASS — Tester full-suite criterion (this entry).
- [ ] PA acceptance — pending, not mine.

**Judgement on the two logged gaps**
1. Ctrl+C cannot cancel a MID-session login wait: acceptable deferral. The AC and the verified user story are the STARTUP login; this path is rarer (a refresh token that fails inside a tool call), bounded at 5 min, pre-existing (`callTool` ignores abort), and the URL is now visible (the part of the issue that made it a dead end). File a follow-up task.
2. Ctrl+C at startup still waits for ANOTHER server's in-flight handshake (up to `mcp.initializeTimeoutMs`, 60 s default): acceptable for this rollup, not FAIL. The AC says "exit ... no hang"; the wait is bounded, shows `quitting...`, exits 1, leaves no child process, and replaces an unbounded silent swallow. But it is the realistic default (committed stdio `tree-memory-local` takes 11-28 s locally, plus the hosted entry), and a second Ctrl+C is ignored, so a follow-up is warranted. In practice the wait is short: with the committed stdio `tree-memory-local` (11-28 s handshake) plus the hosted entry, only a Ctrl+C within the first ~28 s waits at all. The lever is NOT `McpClient.close()`: `open()` awaits `startMcp` -> `connectMcpServers` -> `Promise.all(listTools())`, and the hung client's `listTools` keeps `Promise.all` pending, so `mcpClients.push` and `main`'s `finally` (the only `close()` caller) are unreachable until that handshake times out. The signal has to reach the handshake: a `signal` in `McpClientOptions`/`StartMcpOptions` that `#attempt` honours (abort the `initialize` request and close the transport), or `connectMcpServers` racing each handshake against it. Alternative: a second Ctrl+C forces the exit.

**Other issues found (none blocks the verdict)**
1. Spec §15's TUI paragraph and `startup.ts`'s header ("spec §15") do not mention the startup screen; the behaviour is in §10.2.1 and the README only.
2. When `open()` fails with a non-abort error (e.g. a malformed `mcp.json`), the dim `starting… (Ctrl+C quits)` line stays in the scrollback above the error message (the `finally` renders the last frame).
3. The authorize URL is hard-wrapped (no OSC 8 link), so a mouse selection across lines carries the line breaks and the leading space of each line; the SWE documented this. Copying with a terminal that unwraps by newline is fine, a plain paste into a browser needs the breaks removed.
4. `sanitize` strips escape sequences and control characters but lets U+202E (bidi override) through to the terminal (pre-existing `style.ts`).
5. Formatting: the ADR amendment and README hosted-server paragraph have one over-long line each after the rewording (prettier accepts it).

**VERDICT: PASS**


### [SWE] 2026-10-25 13:40 — Tester fold-ins

Folded in: (1) `McpClient` takes the startup `signal` (TUI Ctrl+C), so a pending handshake rejects at once (`connect cancelled`) and drops its child / HTTP session. pty run: hung HTTP server (timeout 20 s) exits 1 at 0.0 s after Ctrl+C. Deaf stdio server: 0.9 s (its SIGTERM), no child left. Tests: `client.test.ts` +5, `cli.test.ts` +1 plus a headless `signal: undefined` check, 1011 passing. (2) The startup screen is in spec §15 and the `startup.ts` header. (3) The ADR-016 amendment and the README hosted-server paragraph are re-wrapped. The mid-session-login Ctrl+C gap stays as logged.
