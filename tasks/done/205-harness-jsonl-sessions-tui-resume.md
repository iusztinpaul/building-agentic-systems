---
id: 205-harness-jsonl-sessions-tui-resume
status: done
feature: tree-agent-harness
---

# M2 — append-only JSONL session store in pi's envelope (written linearly), the pi-tui TUI (transcript, tool cards, status bar, input, slash commands), and `--resume` / `--continue`

Tags: `harness`, `tui`, `sessions`, `enhancement`
Depends on: 204 (`AgentSession`, events, CLI)
Blocks: 206, 207, 212
Implements: ADR-016 §8 (sessions), §9 (TUI invariant: nothing is TUI-only)

## Scope

1. **`src/session-store/jsonl.ts`** — spec §14: `~/.tree-agent/sessions/--<cwd-slug>--/<ISO-timestamp>_<id>.jsonl`;
   `Envelope {id, parentId, timestamp}` with `timestamp` ISO 8601 UTC (`…Z`); entries `session` (first line,
   `version: 1`, `cwd`, `model`), `message`, `compaction`, `permission`, `tool_activation`, `model_change`;
   `appendFileSync` per finalized event; `parentId` = the previous entry's `id`, always; `load(id)` rebuilds
   `Context` (messages after the last `compaction`, prefixed by its summary) and the activated-tool set — the
   loader understands every entry type now even though `permission` (206), `tool_activation` (209) and
   `compaction` (212) are first written later; `list(cwd)`; `SessionStore.inMemory()` for tests; a file whose
   `parentId`s branch is read along the last entry's ancestry only.
2. **`AgentSession` persistence** — every finalized `Message` and `/model` (`model_change`) is appended;
   `--resume <id>` / `--continue` (latest for this `cwd`) work for the TUI and headless.
3. **`src/tui/`** (`@earendil-works/pi-tui` 1.4.0, verified to install) — `app.ts`, `transcript.ts`
   (streamed assistant text with code blocks and bullets, user messages, inline events), `tool-card.ts`
   (name + primary argument, collapsed result, `Tab` expands; the `llm_call` nesting is 211),
   `status-bar.ts` (model · mode — shows the configured `permissions.mode`, wired in 206 · `tokens /
   contextLength (NN %)` from the last `usage.inputTokens` until 212 swaps in the estimator · session cost
   from usage × `pricing` · MCP server count (0 until 209) · activated-tool count), `input.ts` (multiline
   with `Shift+Enter`, history; `Enter` during a run → `steer`, `Alt+Enter` → `followUp`, idle → `prompt`;
   `Esc` aborts). `permission-prompt.ts` is created in 206. Slash commands in THIS task: `/help`, `/model <id>`,
   `/tools` (direct ∪ activated), `/reload` (context files), `/clear`, `/exit`. Added later: `/mode` (206),
   `/skill:` (207), `/mcp` (209), `/compact` (212). Components subscribe to `AgentEvent`s and call the
   session's public methods only — no provider, loop or tool imports under `src/tui/`.
4. **Trust dialog** — on an untrusted `cwd` the TUI asks once ("Trust this directory? y/n"); `y` writes
   `trusted.json`; `n` runs with user-level config only.
5. **CLI** — `tree-agent` on a TTY opens the TUI (replacing 204's placeholder); `--resume`, `--continue`.

**Out of scope (intentional):** `/tree`, forks, branch summaries (spec non-goals); themes; the permission
prompt's behaviour (206); `llm_call` rows (211); the estimator-based token gauge (212).

## Acceptance Criteria

- [x] A new session's first line is `{type: "session", version: 1, cwd, model}` with `parentId: null`; every
      later line's `parentId` equals the previous line's `id`; every `timestamp` matches
      `^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$`; the file path matches
      `--<cwd-slug>--/<ISO-timestamp>_<id>.jsonl`.
- [x] After an aborted turn the file already holds the partial assistant message (`stopReason: "aborted"`) —
      persistence is per finalized event, not per run.
- [x] `load(id)` of a file with `session`, 6 `message`s, a `tool_activation` and a `model_change` rebuilds the
      messages in order, the activated set and the model; a crafted file whose last entry's `parentId` skips an
      entry loads only that entry's ancestry; `--continue` picks the newest file for the current `cwd` slug and
      not another cwd's; `--resume <unknown>` exits 1 with one line.
- [x] `SessionStore.inMemory()` satisfies the same interface and is what the loop tests use.
- [x] `/model <id>` appends `model_change` and the next request uses the new model; `/tools` lists exactly the
      direct ∪ activated names; `/reload` re-reads context files (a changed `AGENTS.md` appears in the next
      recorded system prompt); `/clear` starts a new session file; `/exit` exits 0.
- [x] `grep -rln "provider/\|loop/\|tools/" apps/harness/src/tui` matches nothing (components depend on
      `types.ts` and `session.ts` only).
- [x] [HUMAN] `tree-agent` in a trusted dir: text streams into the transcript; a `read` call renders a card
      with the relative path and a collapsed result that `Tab` expands; `Esc` during a long answer stops the
      stream, the partial text stays, and the status bar still shows the token gauge and cost; typing during a
      run and pressing `Enter` steers (the model acknowledges it on its next turn); `Alt+Enter` queues a
      follow-up that runs after the answer; `tree-agent --continue` reopens the same transcript.
- [x] [HUMAN] In an untrusted directory the TUI asks for trust once; `n` → the prompt's `project_context`
      holds only the user-level file (verify with `/help`'s hint or a question about the project context).
- [x] `make harness-lint-fix && make harness-lint-check && make harness-typecheck && make harness-tests &&
      make pre-commit` green.

## User Stories

### Story: A user resumes yesterday's session
1. They run `tree-agent --continue` in the same directory.
2. The transcript shows yesterday's messages; the status bar shows the model recorded by the last
   `model_change`; the next prompt appends to the same file with a `parentId` chain unbroken.

### Story: A user aborts a runaway answer
1. During a long stream they press `Esc`.
2. The stream stops within a second, the partial text stays in the transcript, the JSONL file ends with that
   assistant message marked `aborted`; the input is live again.

### Story: A user steers and queues
1. The agent is editing files; the user types "skip the tests for now" and presses `Enter` (steer), then
   "when done, summarise the diff" and `Alt+Enter` (follow-up).
2. The steer is in the next model request; the follow-up runs after the agent would otherwise have ended.

### Story: A user switches the model mid-session
1. `/model anthropic/claude-haiku-5.5`.
2. The status bar updates; `model_change` is on disk; the next request carries the new id.

### Story: A developer inspects a session file
1. `cat ~/.tree-agent/sessions/--repo--/2026-10-24T…_abc.jsonl | jq .type` prints `session`, then `message`
   lines — one JSON object per line, readable without the app.

---

Blocked by: 204

## Log

### [PA] 2026-10-24 19:58 — Grooming

**Summary**
Persist every finalized event in pi's JSONL envelope (linear `parentId`), put a pi-tui front end over
`AgentSession`'s events, and make sessions resumable from both entry points.

**Key decisions**
- The loader understands all six entry types from day one so 206/209/212 only add writers.
- Slash commands are distributed: this task ships the ones its modules can honour; `/mode`, `/skill:`,
  `/mcp`, `/compact` arrive with their features.
- The token gauge reads the last `usage.inputTokens` until the estimator exists (212) — a true number, not a
  placeholder.
- `src/tui/` may import only `types.ts` and `session.ts`, enforced by a grep AC (spec §3 invariant).

**Dependencies**
- 204 — `AgentSession`, events, the CLI matrix.

**User stories**
- 5 stories covering: resume, abort, steer + follow-up, model switch, a readable session file.

Ready for implementation.

### [SWE] 2026-10-25 00:20 — Implementation

**Files modified**
- `apps/harness/package.json`, `pnpm-lock.yaml` — `@earendil-works/pi-tui` `1.4.0` (exact pin, `pnpm add` inside `apps/harness`). No other dependency.
- `apps/harness/src/session-store/jsonl.ts` — new. `Envelope`, `EntryBody` (all six types), `Entry`, `RestoredSession {id, cwd, model, messages, activated, spend}`, the `SessionStore` interface (`create(cwd, model)`, `append(id, body)`, `load(id)`, `list(cwd)`), and a value of the same name: `SessionStore.inMemory(now?)` / `SessionStore.jsonl(dir, now?)`. Also `JsonlSessionStore`, `InMemorySessionStore`, `SessionNotFound`, `cwdSlug`, `ancestry`, `restore`, `summaryMessage`.
- `apps/harness/src/session.ts` — the in-memory store moved out. New options `store` (default `SessionStore.inMemory()`), `resume`, `permissionMode`. Per-event persistence. New `id`, `model`, `trusted`, `status(): SessionStatus`, `toolNames()`, `setModel(id)`, `clear()`.
- `apps/harness/src/tools/registry.ts` — `deactivateAll()`, which `/clear` calls.
- `apps/harness/src/config/trust.ts` — `trustDirectory(cwd, env)` (the dialog's `y`).
- `apps/harness/src/tui/` — new: `app.ts` (`runTui`), `transcript.ts`, `tool-card.ts`, `status-bar.ts`, `input.ts`, `commands.ts` (the six slash commands), `trust-prompt.ts`, `style.ts` (raw SGR + the Editor/Markdown themes).
- `apps/harness/src/cli.ts` — `--resume <id>`, `--continue`. `CliDeps.terminal`. A TTY with no prompt opens the TUI (this replaces 204's usage placeholder). One shared `open(trust)` serves both modes.
- Tests (one file per module): new `tests/session-store/jsonl.test.ts` (13), `tests/tui/{app,transcript,tool-card,status-bar,input,commands,trust-prompt}.test.ts` (11/9/7/11/17/9/9) and `tests/helpers/terminal.ts` (`FakeTerminal`, `settle`). Extended: `tests/session.test.ts` (14 → 27), `tests/cli.test.ts` (39 → 52, the TUI-placeholder test replaced), `tests/config/trust.test.ts` (5 → 9), `tests/tools/registry.test.ts` (11 → 12).

**Decisions (choices the spec left open, for the Tester and PA)**
- *File and directory names follow pi* (checked in `@earendil-works/pi-coding-agent@1.4.0`'s `dist/core/session-manager.js`, unpacked in the scratchpad):
  - The directory is `--${cwd.replace(/^[/\\]/,"").replace(/[/\\:]/g,"-")}--`, e.g. `/Users/me/repo` → `--Users-me-repo--`.
  - The file is `<toISOString() with ":" and "." → "-">_<id>.jsonl`, e.g. `2026-10-24T21-14-20-049Z_b69f9740-….jsonl`, so the name is also valid on Windows. The entries' `timestamp` field keeps the real ISO form (`2026-10-24T21:14:20.049Z`, which matches the AC regex).
  - The session id is `randomUUID()` (pi uses uuidv7, which Node does not ship). Entry ids are 8 hex chars (`randomUUID().slice(0, 8)`, as in pi), unique within their session.
- *The file is created lazily*, when the first entry is written (pi does the same). An opened-and-closed TUI leaves nothing on disk, so it never becomes the session that `--continue` picks. `session.id` stays `undefined` until then.
- *Per-event persistence happens in `AgentSession.#emit`*, before any listener runs. `prepareTurn` keeps the context the loop pushes to. The loop always pushes a message before the event that reports it (user/steer/follow-up/"Continue." → `turn_start`, assistant → `turn_end`, tool → `tool_result`, max-turns leftovers and hook throws → `agent_end`), so every message is on disk before the TUI or the headless sink sees its event. The loop is unchanged (168 lines). `finally` still flushes as a backstop.
- *`--continue` = the newest by mtime, ties broken by the timestamp in the file name* (pi sorts by mtime). `list(cwd)` reads only that cwd's slug directory. `--continue` with no session for the cwd starts a new one, as pi does.
- *`--resume <id>` takes the exact id* (the part of the file name after `_`), searched in every slug directory. It is checked before the TUI starts, so an unknown id prints the one line `no session <id>` and exits 1, with no TUI output. `--resume` together with `--continue`, or `--resume ""`, is a usage error.
- *Model on resume:* the recorded model wins (the header, then the last `model_change`). An explicit `--model` that differs is applied through `setModel`, which appends a `model_change`.
- *`setModel(id)`* validates the id through `provider.modelInfo` (an unknown id throws; nothing is written), refreshes `contextLength` and pricing, and appends `model_change`. It is refused while a prompt runs, and so is `clear()`. `/model` with no argument shows the current model.
- *`AgentSession.create` now calls `modelInfo(model)`* for the status bar's context length and pricing, so an unknown main model fails at start, like `checkDecisionModel` does. Test impact: 203's recorded-stream CLI test listed only the haiku model in its fake `/models` body. I added the sonnet entry, which the real catalog has.
- *Cost* = Σ `inputTokens × pricing.prompt + outputTokens × pricing.completion`, with the pricing of the model that ran each turn. It is restored on resume from `spend` (each assistant usage paired with the model in force at that point in the chain). A model with no pricing counts 0 and logs a warning. `cachedTokens` is not priced, because `ModelInfo.pricing` has no cache rate. The gauge is the last `usage.inputTokens` against the current model's `contextLength`.
- *Compaction convention for 212:* the `compaction` entry's `summary` is the rendered markdown without tags. The loader turns it into `summaryMessage(summary)` = `{role:"user", content:"<summary>\n…\n</summary>"}`, followed by the message entries from `firstKeptEntryId` on (in chain order, so the kept tail sits before the compaction line in the file), then the messages after it. If the id is not found, only the summary is kept. Activations survive compaction. `permission` entries are skipped on load (audit only). An unknown entry type, or a line that does not parse, is skipped with a `log.warn`.
- *A branched file* is read along the last line's ancestry (`ancestry()` stops on a cycle). Appends after a `load` chain on from the last line.
- *Not written yet:* `permission`, `tool_activation` and `compaction` entries (206/209/212). The `/clear` path and the resume path already handle activations.
- *Keys:*
  - `Alt+Enter` is intercepted before pi-tui's Editor (`editor.js` treats `\x1b\r` as a newline). It then queues a follow-up, or prompts when idle.
  - `Enter` prompts when idle and steers during a run. `Shift+Enter` / `Ctrl+J` insert a newline (Editor default).
  - `Esc` aborts the run, unless the autocomplete menu is open.
  - `Tab` on an empty editor expands or collapses all tool results (one toggle for every card; new cards follow it). With text in the editor, `Tab` is the Editor's completion.
  - `Ctrl+C` (raw mode, no SIGINT) aborts the run if one is going, otherwise exits 0. So two presses during a run abort and then quit.
  - `/exit` aborts a running prompt, waits for its partial answer to be saved, then exits 0.
- *Slash commands* are `^/[a-z][a-z:-]*` plus optional arguments, so `/usr/bin/env …` is still a prompt. An unknown `/name` gets an error line. Commands also work during a run, except `/model` and `/clear`, which answer with the "while a prompt is running" error.
- *Trust dialog* (`trust-prompt.ts`, a file the task did not list): it runs inside the TUI before the session opens, because settings and context files depend on the answer. `y` writes via `trustDirectory`, which refuses to overwrite a `trusted.json` that is not a JSON array of paths, so a broken hand edit is never lost. `n`, `Esc` or `Ctrl+C` give a user-level-only run. It is asked on every launch until the answer is `y`; nothing records a `n`.
- *Status bar:* `model · mode · 12.3k / 1M (1 %) · $0.0421 · 0 MCP · 2 activated`. `mode` is `settings.permissions.mode` in the TUI and `auto` headless (display only until 206). MCP is `0` until 209.
- *Rendering:*
  - Assistant text uses pi-tui's `Markdown` (bullets, dimmed fences, indented code), re-parsed per delta. That is O(n²) over one answer; fine for answers of a few KB, cache per block if long answers lag.
  - Model text, user text, notices, the tool-card header and tool output are all untrusted. Each goes through `style.ts > sanitize` (pi-tui's `stripTerminalSequences`, then every control character except `\n` and `\t`), so none of it can clear the screen or move the cursor. Card bodies also expand tabs.
  - Renderer: `TuiMainScreen`, so the terminal keeps its scrollback.
- *Seams:* `CliDeps.terminal` (a `FakeTerminal` in tests) and `TuiOptions.onStart(tui)` (tests read the screen through `tui.render(width)`). The headless and TUI paths share one `open(trustNow)`.
- *Headless now writes session files too.* That is the spec's "`--resume` … for headless": every `tree-agent "<prompt>"` leaves `~/.tree-agent/sessions/--<slug>--/…jsonl`.

**Tests**
- Unit: 358 passed, 0 failed (`make harness-tests`, 28 files, including 203's 2 live tests). On Node 22, as CI runs it: `env -u OPENROUTER_API_KEY npx -y node@22 node_modules/vitest/vitest.mjs run` (v22.23.4) → 355 passed, 2 skipped (the live tests), 0 failed. Every 205 test is offline (`FakeProvider`, a race-free held provider in `app.test.ts`, `mkdtemp` homes).
- Integration: N/A (no suite, AGENTS.md).
- Mutation probes (`scratchpad/probe.py`: apply one edit, run the module's tests, restore; `md5` before and after identical). Each probe made tests fail:

| Probe | Failed |
|---|---|
| flush only at the end of a run | 2 (mid-tool-call disk check, aborted-on-disk-at-`turn_end`) |
| `restore` walks every entry instead of the ancestry | 1 |
| in-memory `list` ignores the cwd | 1 |
| `Alt+Enter` left to the Editor | 2 |
| `model_change` ignored on load | 3 |
| `setModel` does not append | 3 |

**Acceptance criteria**
- [x] First line, `parentId` chain, timestamp regex, path. The file name uses pi's stamp (`:` and `.` → `-`), see Decisions; check with `ls ~/.tree-agent/sessions/--<slug>--/ | grep -E '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}-[0-9]{2}-[0-9]{2}-[0-9]{3}Z_[0-9a-f-]{36}\.jsonl$'`. Verified by `jsonl.test.ts`: `writes the session line first, chains every parentId…` (also asserts the path `^/--work-repo--/<stamp>_<id>.jsonl$`), `stamps entries with the injected clock in UTC` (23:30 −05:00 → `2026-10-25T04:30:00.000Z`), and `session.test.ts > writes nothing until the first message, then a session line with cwd and model`.
- [x] Aborted turn on disk. Verified by `session.test.ts`: `has the partial assistant message (aborted) on disk when turn_end reaches listeners`, `has the user and assistant messages on disk while the tool call still runs`; and `app.test.ts > aborts on Esc: the partial text stays…` (store holds `{content:"ans", stopReason:"aborted"}`).
- [x] `load(id)`, branch, `--continue`, `--resume <unknown>`:
  - `jsonl.test.ts`: `rebuilds 6 messages in order, the activated set and the last model_change`, `loads only the last entry's ancestry when a parentId skips an entry`, `rebuilds [summary][tail] from the last compaction…`.
  - `cli.test.ts > main (sessions)`: `--continue picks the newest session of this cwd, not another cwd's`, and `--resume <unknown> exits 1 with one line (headless | the TUI)` (stderr exactly `no session nope\n`, provider never created, terminal never started).
- [x] `SessionStore.inMemory()`. Verified by `jsonl.test.ts > SessionStore.inMemory` (2 tests). It is the default store of every `AgentSession` the session, loop-through-session and TUI tests build (`session.test.ts`, `app.test.ts`).
- [x] Slash commands (`tests/tui/commands.test.ts`):
  - `/model <id> appends model_change and the next request uses the new model`
  - `/tools lists exactly the direct and the activated tools` (4, then 5 after `activate`)
  - `/reload re-reads AGENTS.md: the change is in the next recorded system prompt`
  - `/clear starts a new session file` (2 files, fresh context)
  - `/exit asks the TUI to exit`
  - `app.test.ts > exits 0 on /exit and restores the terminal`
  - `cli.test.ts > main (TUI) > opens the TUI on a TTY with no prompt and exits 0 on /exit`
- [x] Boundary grep: `grep -rln "provider/\|loop/\|tools/" apps/harness/src/tui` → no output, exit 1. Pinned by `app.test.ts > the TUI's imports never reach the provider, the loop or the tools` (asserts exit status 1 and empty stdout). `src/tui` imports only `../types.ts`, `../session.ts` and its own files (plus pi-tui and `node:path`).
- [ ] [HUMAN] Interactive TUI story. Left for the human. Covered offline: `app.test.ts` (streaming, steer on Enter and follow-up on Alt+Enter in the recorded requests, Esc abort, Tab expand, resumed transcript) and `transcript.test.ts`/`tool-card.test.ts` (read card with the relative path, collapsed `(+N lines, Tab)`). I also drove it live on a PTY; see Evidence.
- [ ] [HUMAN] Untrusted dir, `n` → user-level only. Covered offline (`cli.test.ts > main (TUI)`: `y` writes `trusted.json` and loads the project `AGENTS.md`; `n` writes nothing and the system prompt has no project file; a listed cwd is not asked). Live PTY: `n`, then `/help` shows `untrusted directory: user-level config only` and `context files: none`.
- [x] Cadence green (Evidence).

**Evidence**
```
$ make harness-lint-fix        → biome: Checked 63 files … No fixes applied.        exit 0
$ make harness-lint-check      → Checked 63 files … No fixes applied.               exit 0
$ make harness-typecheck       → tsc --noEmit                                       exit 0
$ make harness-tests           → Test Files 28 passed (28) / Tests 357 passed (357) exit 0
$ make pre-commit              → prettier, ruff check, ruff format, biome check (harness): Passed
$ grep -rln "provider/\|loop/\|tools/" apps/harness/src/tui ; echo $?   → (nothing) 1
$ make harness-build && node apps/harness/dist/cli.js --resume zz "hi"  → "no session zz", exit 1 (compiled bin resolves pi-tui)

# PTY smoke (scratchpad/tui-smoke/bin/pty_drive.py: python pty.fork → `node apps/harness/src/cli.ts`,
# cwd scratchpad/tui-smoke/proj (AGENTS.md "- dates are UTC"), TREE_AGENT_HOME=scratchpad/tui-smoke/home,
# .env sourced in a subshell, key never printed)
run 1 (untrusted): "Trust this directory?" → n → status bar → /help → shows "session: (not saved yet) ·
      untrusted directory: user-level config only", "context files: none" → /exit → exit status 0 after 1.5 s;
      home stays empty (no trusted.json, no session file)
run 2 (live): y → trusted.json = [".../tui-smoke/proj"]; prompt "Use the read tool on AGENTS.md, then tell me …
      dates" → card "✓ read AGENTS.md" / "# smoke project (+1 lines, Tab)" → answer "AGENTS.md says that dates
      are UTC." streamed; Tab → card expands ("- dates are UTC"); /tools → "tools (4): read, bash, edit,
      write"; "Count from 1 to 300 …" → Esc at "12" → "(aborted)", status bar still "1.8k / 1M (0 %) ·
      $0.0077"; /exit → exit 0. File: session, user, assistant(tool_calls read), tool, assistant(stop),
      user, assistant(aborted, "1\n2\n…132")
run 3/4 (--continue): "resumed session <id>" + yesterday's transcript; /model anthropic/claude-haiku-5.5 →
      "model: anthropic/claude-haiku-5.5", status bar shows the haiku id and the restored cost; "What is 4242
      times 2? Digits only." → "8484"; Ctrl+C (idle) → exit status 0. Still one file, chain unbroken:
      session,user,assistant,tool,assistant,user,assistant,model_change,user,assistant,…
headless: `node cli.ts --continue "What number did you compute last? Digits only."` → "8484", exit 0
      (same file, 14 lines, every parentId = previous id, every timestamp matches the AC regex);
      `node cli.ts --resume nope "hi"` → "no session nope", exit 1;
      `script -q /dev/null node cli.ts --resume nope` (TTY) → "no session nope", exit 1, no TUI drawn;
      `--resume a --continue` → "use --resume <id> or --continue, not both" + usage, exit 1
```

**Notes**
- `make pre-commit` runs `--all-files` over *tracked* files, so it skipped the new untracked `src/session-store/`, `src/tui/`, `tests/tui/`, `tests/session-store/` and `tests/helpers/terminal.ts`. `make harness-lint-check` (Biome over `src tests`) covers them, and prettier does not format `.ts`.
- pi-tui's native clipboard helper loads lazily (`getNativePlatformHelper()` inside a `try`, linux/darwin/win32 prebuilds shipped), so importing it on the CI runner is safe. It does raise the *runtime* Node floor to pi-tui's `engines: >=22.19.0`, while `package.json` still says `>=22`. 202 kept `>=22` because `dist/cli.js` was plain JS, and that no longer holds. I left `engines` alone; flag for PA/213.
- Test polls wait up to 2 s (100 × 20 ms, like `tmp.ts > gone()`). The CLI TUI tests wait for the status bar (or the trust question) on the terminal before typing.
- Not committed; waiting for the Tester. `.env*` and `docs/Tree-Agent Harness Implementation Spec.md` were not touched. No new env vars.
- Known limits, left as follow-ups and not built:
  - Keys typed between the trust answer and the session opening are dropped. This includes `Ctrl+C` while `GET /models` is slow. The window is the settings load plus `checkDecisionModel`: 0.4 s on the live runs.
  - A resumed session whose recorded model has left OpenRouter fails at `create` (`unknown model`), even with `--model`, because the restored model is looked up first.
  - `--resume` takes no id prefix.
  - The status bar truncates at 80 columns with the long sonnet id (`… 0 ac...`).
- `apps/harness/README.md` still describes the 204 CLI. The docs pass is task 213. Carry these to it:
  - `--resume`/`--continue`
  - where session files live
  - the TUI keys above
  - headless runs leave session files
- The 204 note about the text sink printing a `length`-cut answer on two lines is untouched.

### [Tester] 2026-10-25 00:35 — QA

**Test summary**
- Format / lint / typecheck / pre-commit: PASS (`make harness-format-check`, `harness-lint-check` 63 files, `harness-typecheck`, `make pre-commit`: all exit 0)
- Unit tests: 358 passed / 0 failed with the key (`make harness-tests`, 28 files)
- Without key: `env -u OPENROUTER_API_KEY pnpm exec vitest run` → 356 passed / 2 skipped / 0 failed
- Node 22: `env -u OPENROUTER_API_KEY npx -y node@22 node_modules/vitest/vitest.mjs run` (v22.23.4) → 356 passed / 2 skipped / 0 failed. `import("@earendil-works/pi-tui")` also loads on Node 22.12.0.
- Warnings: 0 (only the expected `[tree-agent] warn` lines from the corrupt-line probes)

**E2E adversarial pass** (store scripts and PTY driven through tmux, scratch project + fake `TREE_AGENT_HOME`, key sourced in a subshell; scripts in the session scratchpad `qa/`)
- Happy path (live, trusted dir): `y` at the trust prompt → `trusted.json` written; "read notes.txt, 3rd line" → card `✓ read notes.txt` / `line one (+4 lines, Tab)`, streamed answer, status bar `anthropic/claude-sonnet-5.5 · ask · 1.8k / 1M (0 %) · $0.0079 · 0 MCP · 0 activated`; `Tab` → card shows the 3 lines + the read tool's footer; `Tab` again → collapsed. PASS
- Esc: "Count 1 to 400" streamed to 347, `Escape` → `(aborted)` within the next 1.2 s sample; status bar unchanged (gauge + cost kept); the session file, read while the TUI was still open, ended with the assistant message `stopReason: aborted`, 1279 chars, timestamp 10 ms after the Esc. PASS
- Steer / follow-up: "run `sleep 8` in bash, then `echo done`, reply FINISHED"; at +3 s typed a steer + `Enter`, then a follow-up + `Alt+Enter` (tmux `M-Enter`). Transcript: `(steer)` and `(follow-up)` tags; answer `FINISHED PINEAPPLE` (steer acknowledged), then `FOLLOWED` (follow-up ran after). File chain: user, assistant(tool_calls), tool, user(steer), assistant(tool_calls), tool, assistant, user(follow-up), assistant; 0 broken `parentId` links over 23 lines. PASS
- `--continue` / `--resume <id>`: `--continue` in the same cwd, with a NEWER session of another cwd present → opened this cwd's newest, `resumed session be609ee1…`, transcript replayed, status bar showed the recorded model. `--resume 55686bfa…` replayed the older session including cards, restored cost `$0.0456`, a new question appended to the same file, chain unbroken. PASS
- Untrusted dir: prompt shown once; `n` → `trusted.json`/session untouched, `/help` → `untrusted directory: user-level config only`, only `homeu/AGENTS.md` under context files; asked the model for both codenames → "only one codename, OTTER-7" (project's ZEBRA-42 absent). Next launch in a trusted dir does not ask again. PASS
- Slash commands (live): `/model bogus/nope` → `unknown model "bogus/nope": not listed by GET /api/v1/models`, model kept, nothing written. PASS. `/frobnicate x` → `unknown command /frobnicate (see /help)`. PASS. `/tools` → `tools (4): read, bash, edit, write`. `/reload` after editing `AGENTS.md` ZEBRA-42 → GIRAFFE-99: `reloaded 1 context file(s)`, the next answer was `GIRAFFE-99`. `/model anthropic/claude-haiku-5.5` → status bar switched, `model_change` line on disk. `/clear` → `new session`, no new file until the next message (1 file, then 2), cost reset to $0.0000. `/exit` and idle `Ctrl+C` → `EXIT=0`. PASS
- Ctrl+C mid-tool: `sleep 20` running, one `Ctrl+C` → tool card `✗ bash sleep 20 / Command aborted`, TUI stays; second `Ctrl+C` → `EXIT=0`; no orphan `sleep`. PASS
- Hostile output: model ran `printf "BEFORE\033[2J\033[HAFTER\007END"` → card shows `BEFOREAFTEREND`, screen not cleared. Model-text sanitising: `tests/tui/transcript.test.ts:128-133` (`\x1b[2J`, `\x07`, `\x1b]0;` absent from the render). PASS
- Envelope: store script → first line `{"id":..,"parentId":null,"timestamp":"2026-10-24T21:24:55.203Z","type":"session","version":1,"cwd":"/work/repo","model":"m/a"}`; each `parentId` = previous `id`; all timestamps match the AC regex; real file `2026-10-24T21-26-11-273Z_55686bfa-661b-4239-84b2-55f834e70fbb.jsonl` under `--<cwd-slug>--/`. PASS
- Crafted `load`: file with `session`, 6 messages, `tool_activation`, `model_change` → msgs `1..6` in order, `activated:["mcp__x__q"]`, `model:"b/m"`. Compaction file → `["<summary>\nSUMMARY\n</summary>","keep1","keep2","new1"]`. Branch file (`3`'s parent skips `2`) → `["k1","k2"]`. PASS
- Bad files: empty file / first line not `session` / garbage → `Error: session <id> has no session entry` (loud, one line); `--resume nope`, `--resume ../../x` → `no session …`, exit 1, headless and on a TTY (tmux) with no TUI drawn; `--continue` with no session in the cwd → new session, exit 0; `--continue` whose newest file is an empty file → `session empty has no session entry` (loud, but `--continue` stays stuck until the file is removed; note only). PASS
- **State edge: crash mid-append (truncated last line, no trailing newline), then resume and keep working: FAIL.** Details below.
- Slug collision `/a/b-c` vs `/a/b/c`: both → `--a-b-c--`; `list("/a/b/c")` returns `/a/b-c`'s session, so `--continue` in one would resume the other project's session. Same as pi; the AC says "cwd slug", so PASS WITH NOTE.

**Acceptance criteria**
- [x] PASS — first line / parentId chain / timestamp regex / path. `tests/session-store/jsonl.test.ts:59` (`writes the session line first, chains every parentId…`), `:93`; live file listing above. File-name deviation (pi's `-` for `:` and `.`) ACCEPTED: the AC's own regex is on the entries' `timestamp` (kept ISO), the name is still `<ISO-ish timestamp>_<id>.jsonl`, lexicographically sortable (list() relies on it), valid on Windows (`:` is not), and identical to the pi files the spec clones. Ask PA to word spec §14 / the AC as "ISO timestamp with `:`/`.` → `-`".
- [x] PASS — aborted partial on disk. `tests/session.test.ts:345`; live: file read during the open TUI ended with the `aborted` message.
- [x] PASS — `load`, branch, `--continue` (newest of this cwd, not another's), `--resume <unknown>` exit 1 one line: `jsonl.test.ts:105,125,141`; `cli.test.ts` (`--continue picks the newest…`, `--resume <unknown>…`); live runs above.
- [x] PASS — `SessionStore.inMemory()`: `jsonl.test.ts:224,240`; default store in `session.ts:94` (`SessionStore.inMemory(o.now)`), used by `session.test.ts`/`app.test.ts`.
- [x] PASS — slash commands: `tests/tui/commands.test.ts:60,94,107,123,137`, `app.test.ts:122`, live runs above.
- [x] PASS — boundary: `grep -rln "provider/\|loop/\|tools/" apps/harness/src/tui` → empty, exit 1; imports are `../session.ts`, `../types.ts`, own files, pi-tui, `node:path`; pinned by `tests/tui/app.test.ts:317`.
- [x] PASS — [HUMAN] trusted-dir interactive story: I drove every clause (streaming, `read` card with relative path + `Tab`, `Esc` keeps partial + gauge/cost, `Enter` steer acknowledged, `Alt+Enter` follow-up ran after, `--continue` reopened the transcript).
- [x] PASS — [HUMAN] untrusted dir: asked once, `n` → user-level context only (model could not see the project codename).
- [x] PASS — cadence green.

**Evidence of the FAIL (crash recovery loses data)**
```
$ node --experimental-strip-types qa/store.mts     # file: session + message, then '{"type":"message","id":"deadbeef","parentId":"x","timesta' with NO "\n"
load truncated => 1                              # warn: …:3: not a session entry; skipped   (recovers)
s2.append(id2, user "after-crash")               # appendFileSync(`${JSON}\n`)
file tail:  {"type":"message","id":"deadbeef","parentId":"x","timesta{"id":"074f61f1","parentId":"0ddaca3d",…"after-crash"…}
reload after append post-crash => ["one"]        # "after-crash" silently gone

# through the CLI (headless, copy of a real session):
$ printf '{"id":"zz","parentId":"x","timestamp":"2026-10-2' >> <session>.jsonl
$ tree-agent --continue "Reply with only: AFTER-CRASH"   → "AFTER-CRASH", exit 0
  file: line 3 UNPARSEABLE (cut line + the new user message glued on); line 4 = the assistant answer
$ tree-agent --continue "What was the first thing I asked…"
  → "I can't see any earlier conversation … The session may have restarted after a crash and lost its history."
```
Cause: `JsonlSessionStore.append` (`src/session-store/jsonl.ts`, `appendFileSync(s.file, `${JSON.stringify(e)}\n`)`) glues the new entry onto an unterminated last line; the user message becomes unparseable, the next assistant entry's `parentId` points at the lost entry, and `ancestry()` then stops there, so the next resume drops the whole earlier history, not just one message. The loader's comment promises "a line cut by a crash is skipped … rather than failing" but the recovery path destroys the session on the very next turn.

**Verdict: FAIL — 1 must-fix**
- Fix: in `src/session-store/jsonl.ts`, when `load()`/`#reopen()` reads a file whose last byte is not `"\n"`, remember it (e.g. `needsNewline` in the `Open` record) and have the next `append` write `"\n"` first ((do NOT write at load time: `cli.ts` calls `store.load()` as a read-only pre-check)). Regression test in `tests/session-store/jsonl.test.ts`: craft a file with a truncated tail and no newline, `load`, `append` a message, `load` again → the appended message is present and the messages from before the cut still precede it. Optional hardening: `restore()` should treat an ancestry that does not reach the `session` entry as a warning, not a silent history drop.

**Other issues found / rulings on the known limits**
- `engines` `>=22` vs pi-tui `>=22.19.0`: CI (`.github/workflows/ci.yml:158-161`, `node-version: 22`) resolves to the latest 22.x (22.23.4 today, ≥ 22.19), so CI is safe. Importing pi-tui also works on 22.12. Not a blocker; bump `engines` to `>=22.19` when touching `package.json` (one line, also for 213).
- Keys dropped for ~0.4 s after the trust answer: acceptable for this task (follow-up).
- Resumed session whose model left OpenRouter fails to open even with `--model`: acceptable for this task, but file a follow-up; it is the only way a stale session becomes unrecoverable without editing the file.
- Exact-id `--resume` only: acceptable (AC says `<id>`). 
- Status bar clipped at 80 columns with the long sonnet id: acceptable, cosmetic (clipped to `0 ac...` at 80 cols in the SWE's run2.log; full line at 110 cols in my runs).
- Slug collision (`/a/b-c` vs `/a/b/c`) and two processes on one session (branches, the second writer's messages are dropped by the ancestry rule): same as pi; follow-up, e.g. filter `list()` by the header's `cwd`.
- `--continue` stuck on a zero-byte / headerless newest file until it is removed: note only (the append of the header makes a zero-byte file unlikely).
- Collapsed read card says `(+4 lines, Tab)` for a 4-line file whose first line is shown (counts the tool's footer lines); cosmetic.
- `make pre-commit` skips untracked files, but `make harness-lint-check` (Biome over `src tests`) covers them: confirmed green.
- `--resume`/`--continue` parse the file twice (the `cli.ts` pre-check `store.load(resume)`, then `AgentSession.#restore`), so the corrupt-line warning prints twice; pass the loaded result through or drop one parse (note, not a blocker).
- Live 23-line session file re-checked: all 23 timestamps match the AC regex, line 0 is `session` v1 with `parentId: null`.
- Ticking: I ticked the two `[HUMAN]` ACs after driving them myself on the orchestrator's instruction; no human reviewed them.

### [SWE] 2026-10-25 00:34 — Fixes

**Files modified**
- `apps/harness/src/session-store/jsonl.ts` — `load()` reads the file once and records `needsNewline` (file non-empty and its last byte is not `\n`) on the open-session record; the next `append` writes `\n` first, then clears the flag. `create` sets it `false`; `#reopen` inherits it through `load()`. Nothing is written at load time (the `cli.ts` pre-check stays read-only). `restore()` logs one `log.warn` when the last entry's ancestry does not reach the `session` entry.
- `apps/harness/tests/session-store/jsonl.test.ts` — 4 new tests (below).
- `apps/harness/package.json` — `engines.node` `>=22` → `>=22.19` (pi-tui's floor; CI's `node-version: 22` resolves to 22.23.x).

**Tests**
- Unit: 362 passed, 0 failed (`make harness-tests`, 28 files, live tests included).
- New, in `tests/session-store/jsonl.test.ts`:
  - `JsonlSessionStore > ends a crash-cut last line before the next append, so the appended entry survives` — unterminated truncated tail, fresh store `load` + `append`, reload: `["one", "two", "after the crash"]`, and the appended entry's `parentId` equals the last intact entry's id. Red without the fix (reload lost `"after the crash"`), green with it.
  - `JsonlSessionStore > does not write to a crash-cut file on load` — file bytes unchanged after `load`.
  - `JsonlSessionStore > warns once when the last entry's ancestry does not reach the session entry` — broken `parentId` file; one warning, only the orphan's chain restored.
  - `JsonlSessionStore > does not warn when the ancestry reaches the session entry`.

**Evidence**
```
$ make harness-lint-fix && make harness-lint-check && make harness-typecheck && make harness-tests && make pre-commit
 Test Files  28 passed (28)
      Tests  362 passed (362)
biome check (harness)....................................................Passed
$ node --experimental-strip-types .../scratchpad/qa/store.mts   # the Tester's repro
load truncated => 1
file tail:
{"type":"message","id":"deadbeef","parentId":"x","timesta
{"id":"1043dc49","parentId":"83a304e7",...,"content":"after-crash"}}
reload after append post-crash => ["one","after-crash"]
```

**Notes**
- The cut line stays in the file on its own line, so every later load still warns `not a session entry; skipped` for it. That is deliberate: no rewrite of history.
- Supersedes the earlier "I left `engines` alone" note: it is now `>=22.19`.

### [Tester] 2026-10-25 01:05 — Re-QA (crash-recovery fix)

**Test summary**
- `make harness-lint-check`, `harness-format-check`, `harness-typecheck`, `pre-commit`: exit 0 each.
- `make harness-tests` (with the key): 28 files, 362 passed / 0 failed.
- `grep -rln "provider/\|loop/\|tools/" apps/harness/src/tui` → empty (exit 1).
- `engines.node` = `>=22.19`; `pnpm install --frozen-lockfile` → "Lockfile is up to date"; `pnpm-lock.yaml` diff stat identical before/after (36 insertions, the pi-tui entry).

**E2E adversarial pass** (scripts: scratchpad `qa/store.mts`, `qa/variants.mts`; CLI via `qa/run.sh`)
- Repro `store.mts`: after-crash append → reload = `["one","after-crash"]` (was `["one"]`); cut line sits on its own line, entry on the next. PASS
- CLI: copy of a real session + `printf '{"id":"zz",...' >>` (no `\n`) → `--continue "Reply with only: AFTER-CRASH"` → `AFTER-CRASH`, exit 0; then `--continue "...first thing I asked..."` → `The very first thing you asked was "Say HELLO2."`. File: lines 1-3 intact, 4 = cut line (alone), 5-8 chain `5ff3867e → a1f6f46f → a694b4a5 → e4130431 → cebabeb1`, no glued line. PASS
- Bare `load()` x2 on a cut file: sha256 and size unchanged. PASS
- Multi-byte cut (file cut 1 byte into `日`): load → 2 messages, append `after-mb` → `[one,two,after-mb]`, no `\n\n`. PASS
- Trailing `\r` only after the last full line: append works, `[one,two,after-cr]`. PASS
- File ending `\n\n`: append works, no extra newline added (the pre-existing blank line is the only one). PASS
- Two appends after a cut: `[one,two,a1,a2]`, no `\n\n`, file ends `\n`. PASS
- `#reopen` (append with no prior `load`) after a cut, then a second store instance appending twice: `[one,two,r1,r2,r3]`, no `\n\n`. PASS
- Last line complete JSON but missing its `\n` (hand edit): `\n` added, `[one,two,g1]`. PASS
- `load`, `append`, `load`, `append` on one instance: flag cleared after the first append, `[one,two,h1,h2]`, no blank lines. PASS
- Mutation 1 (`const lead = ""`, i.e. `\n`-first write off): `make harness-tests` → 1 failed / 361 passed, the failing test is `ends a crash-cut last line before the next append, so the appended entry survives`. Restored; sha1 `6f70885b…` identical to before.
- Mutation 2 (mend written inside `load()`, and the ancestry warning disabled): `does not write to a crash-cut file on load` and `warns once when the last entry's ancestry does not reach the session entry` fail (2 failed / 360 passed). Restored, sha1 identical. The fourth test (`does not warn when the ancestry reaches...`) guards against a spurious warning.

**Acceptance criteria**
- [x] PASS — previously failed crash-recovery must-fix: verified above (store repro, CLI two-turn, 6 break variants, 4 tests present, 3 of them red under mutation as expected).
- [x] PASS — all earlier criteria unchanged: full suite 362 green, gates green, boundary grep empty.

**Other issues found (notes, not blockers)**
- The corrupt-line warning still prints twice on `--continue`/`--resume` (cli.ts pre-check load + session restore), as noted in my first entry; the cut line also warns on every later load by design (no history rewrite).
- Only `append` mends the file; a session that is loaded and never appended to leaves the cut tail, which is the intended read-only behaviour.

**VERDICT: PASS** (task 205 ready to commit)
