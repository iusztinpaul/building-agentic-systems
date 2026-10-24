---
id: 205-harness-jsonl-sessions-tui-resume
status: pending
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

- [ ] A new session's first line is `{type: "session", version: 1, cwd, model}` with `parentId: null`; every
      later line's `parentId` equals the previous line's `id`; every `timestamp` matches
      `^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$`; the file path matches
      `--<cwd-slug>--/<ISO-timestamp>_<id>.jsonl`.
- [ ] After an aborted turn the file already holds the partial assistant message (`stopReason: "aborted"`) —
      persistence is per finalized event, not per run.
- [ ] `load(id)` of a file with `session`, 6 `message`s, a `tool_activation` and a `model_change` rebuilds the
      messages in order, the activated set and the model; a crafted file whose last entry's `parentId` skips an
      entry loads only that entry's ancestry; `--continue` picks the newest file for the current `cwd` slug and
      not another cwd's; `--resume <unknown>` exits 1 with one line.
- [ ] `SessionStore.inMemory()` satisfies the same interface and is what the loop tests use.
- [ ] `/model <id>` appends `model_change` and the next request uses the new model; `/tools` lists exactly the
      direct ∪ activated names; `/reload` re-reads context files (a changed `AGENTS.md` appears in the next
      recorded system prompt); `/clear` starts a new session file; `/exit` exits 0.
- [ ] `grep -rln "provider/\|loop/\|tools/" apps/harness/src/tui` matches nothing (components depend on
      `types.ts` and `session.ts` only).
- [ ] [HUMAN] `tree-agent` in a trusted dir: text streams into the transcript; a `read` call renders a card
      with the relative path and a collapsed result that `Tab` expands; `Esc` during a long answer stops the
      stream, the partial text stays, and the status bar still shows the token gauge and cost; typing during a
      run and pressing `Enter` steers (the model acknowledges it on its next turn); `Alt+Enter` queues a
      follow-up that runs after the answer; `tree-agent --continue` reopens the same transcript.
- [ ] [HUMAN] In an untrusted directory the TUI asks for trust once; `n` → the prompt's `project_context`
      holds only the user-level file (verify with `/help`'s hint or a question about the project context).
- [ ] `make harness-lint-fix && make harness-lint-check && make harness-typecheck && make harness-tests &&
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
