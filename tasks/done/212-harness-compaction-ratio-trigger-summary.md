---
id: 212-harness-compaction-ratio-trigger-summary
status: done
feature: tree-agent-harness
---

# M7 — compaction by summarisation: the `triggerRatio` × `contextLength` trigger in `prepareTurn`, the user-boundary cut, the merged structured summary on the decision model, `[system][summary][tail]`, `/compact`, `--compact-at`, overflow retry and the failure breaker

Tags: `harness`, `compaction`, `enhancement`
Depends on: 206 (`decide<T>()`), 205 (session store)
Blocks: 213
Implements: ADR-016 §7 (compaction), §6 (second consumer of `decide<T>()`)

## Scope

1. **`src/compaction/estimate.ts`** — spec §13: `estimateTokens(ctx) = chars / 4 × k`, `k = lastUsage.inputTokens
   / estimateAtThatTime` (default 1, clamped 0.5–2), updated after every turn.
2. **`src/compaction/compaction.ts`** — `maybeCompact` in `AgentSession.prepareTurn`: trigger when
   `estimateTokens(ctx) >= triggerRatio × modelInfo(currentModel).contextLength` (re-read after `/model`);
   cut point: walk back until the tail ≈ `keepRecentTokens` (20 000), then forward to the nearest `user`
   message so an assistant `tool_calls` message is never separated from its `tool` results; summary via
   `decide<T>()` on the decision model with the `Summary` schema (goal, constraints, progress, decisions,
   nextSteps, criticalContext — paths, names, errors verbatim) and pi's template; a previous summary is
   merged ("update the summary"); result `ctx.messages = [{role: "user", content: "<summary>…</summary>"},
   ...tail]`, system prompt rebuilt unchanged, activations kept; a `compaction` entry `{summary,
   firstKeptEntryId}` appended; a `compaction` event `{before, after}`.
3. **Knobs** — `settings.compaction.triggerRatio` (0.3–0.95, default 0.8) and `keepRecentTokens`;
   `--compact-at <ratio>`; `/compact [instructions]` runs `compact()` now with the extra guidance.
4. **Overflow** — a provider `error` with `code: "context_length_exceeded"` (203) or `finish_reason: "length"`
   on the first chunk → `compact()` then retry the request once; after `maxConsecutiveFailures` (3) failed
   summaries, stop compacting and emit an `error` event instead of looping.
5. **Store + TUI** — `load()` already reconstructs `[summary][tail]` (205); the status bar's gauge switches to
   `estimateTokens / contextLength (NN %)`; the transcript shows `compacted <before> → <after> tokens` inline.

**Out of scope (intentional):** a decision-model compaction trigger (ToC 5.5.2 — discussed, not built);
branch summaries; compacting `llm()` sub-contexts (they are per call).

## Acceptance Criteria

- [x] With a fake `modelInfo` of `contextLength` 10 000 and a FakeProvider run that grows the context past
      8 000 estimated tokens, `prepareTurn` compacts before the next request: the request's messages are
      `[<summary> user message, …tail]`, the system prompt is unchanged, the activated tools are still sent.
- [x] `--compact-at 0.5` compacts at 5 000; `0.2` and `0.99` are rejected at parse time; `/model` to a model
      with a smaller `contextLength` compacts on the next turn without new messages.
- [x] The cut never splits an assistant `tool_calls` message from its `tool` results (a context whose
      20 000-token boundary falls inside a tool batch moves the cut to the next `user` message).
- [x] A second compaction merges the previous summary (the summariser input contains the earlier summary and
      the result has one `<summary>` block); `/compact focus on the test failures` passes the instruction to
      the summariser.
- [x] A `context_length_exceeded` error compacts and retries once; a second overflow ends with `agent_end(error)`;
      three consecutive summariser failures (FakeProvider rejects) emit one `error` event and stop further
      compaction attempts for the session.
- [x] A session file with a `compaction` entry loads as `[summary][tail]` plus the activations recorded before
      it (the "session figure" test); the original messages stay on disk.
- [x] `estimateTokens` applies the ratio from the last `usage.inputTokens` clamped to 0.5–2.
- [ ] [HUMAN] In the TUI, `/compact` mid-session shows the inline `compacted …` line, the gauge drops, and the
      next answer still knows the task's goal and file paths (ask it).
- [x] `make harness-lint-fix && make harness-lint-check && make harness-typecheck && make harness-tests &&
      make pre-commit` green.

## User Stories

### Story: An autonomous run never overflows
1. A headless run reads many large files; the estimate crosses 80 % of the window between turns.
2. The next request carries a summary + the recent tail; `--json` shows `compaction` with `before > after`;
   the run finishes with exit 0.

### Story: The user compacts with guidance
1. `/compact keep the failing test names`.
2. The summary's `criticalContext` lists those names verbatim; the transcript shows the compaction line.

### Story: A smaller model after `/model`
1. The user switches from a 1 M-context model to a 200 k one.
2. On the next turn the trigger is re-evaluated against 200 k and compacts if needed.

### Story: Overflow despite the estimate
1. The provider answers 400 "context length exceeded".
2. The session compacts and retries once; the retry succeeds; nothing is lost on disk.

### Story: The summariser is broken
1. The decision model returns garbage three times in a row.
2. One error is shown; the session keeps running without further compaction attempts rather than looping.

---

Blocked by: 206

## Log

### [PA] 2026-10-24 20:36 — Grooming

**Summary**
Keep long sessions inside the window with one knob: at 80 % of the current model's context, cut at a user
boundary, summarise the head on the decision model (merging any earlier summary) and continue with
`[system][summary][tail]`, persisted as a `compaction` entry and reproducible from the session file.

**Key decisions**
- The ratio is the only trigger (user decision, spec §1); `/model` re-reads `contextLength` so a smaller model
  compacts next turn.
- The summariser is `decide<T>()`'s second consumer — no new primitive.
- A breaker after 3 failures beats an infinite compact loop; the error is visible.

**Dependencies**
- 206 — `decide<T>()`; 205 — the store.

**User stories**
- 5 stories covering: an autonomous run, guided `/compact`, a smaller model, an overflow retry, a broken
  summariser.

Ready for implementation.

### [SWE] 2026-10-25 02:20 — Implementation

**Bindings (how the spec landed)**
- `src/compaction/estimate.ts` — `TokenEstimator`: `chars/4 × k`, `k = usage.inputTokens / estimate of the context that was sent`, clamped 0.5–2, default 1, updated on every `turn_end` with usage. Chars = system + user text + assistant text + tool-call name/JSON args + tool name/content. Message sizes are memoised per message object (a `WeakMap`), so the status bar can ask on every `text_delta`.
- `src/compaction/compaction.ts` — `Summary` zod schema (goal, constraints, progress, decisions, nextSteps, criticalContext); pi's system prompt, summary prompt and "update the summary" prompt, mapped onto the schema fields; `cutPoint`, `keepTokens`, `renderSummary` (markdown, no tags — 205's convention; wrapped by the existing `summaryMessage()`), `serializeConversation`/`summaryInput` (`<conversation>`, `<previous-summary>`, `Additional focus: <instructions>`), the `Compactor` (estimator + trigger + breaker), and `retryOnOverflow`. The summariser is `decide<T>()` unchanged (`name: "compaction_summary"`, the session's `decisionModel`, 120 s timeout, the run's signal) — no forked path.
- Trigger in `AgentSession.prepareTurn`: system prompt rebuilt, then `estimateTokens(ctx) ≥ triggerRatio × modelInfo(current).contextLength` → compact. `#info` is re-read by `setModel`, so `/model` to a smaller window compacts on the next turn.
- Applying (session `#compact`): persist every message first, read the first kept message's entry id, `splice` `#context.messages` in place to `[<summary> user, …tail]` (the loop holds the same array — also during the overflow retry), append `compaction {summary, firstKeptEntryId}`, emit `compaction {before, after}`. Activations are untouched (registry state), the system prompt is rebuilt unchanged.
- Overflow retry without touching the loop: `runLoop` gets `retryOnOverflow(provider, recover)`; when the **first** stream event is an `error` with `code: "context_length_exceeded"` or a `done` with `stopReason: "length"` and no output, it compacts the request's context and re-sends once; a second overflow (or nothing compacted) is an `error` event → the loop's existing path → `agent_end(error)`. `agent-loop.ts` stays at 180 lines, unchanged.
- Breaker: failures 1–2 are `log.warn`; the 3rd consecutive failure emits ONE `error` event and turns automatic compaction (trigger + overflow) off for the session. An abort is not a failure; a success resets the count.
- 205 completion: `restore()` already rebuilt `[summary][tail]`, but `RestoredSession` dropped the entry ids, so a compaction after `--resume` could not name `firstKeptEntryId`. Added `RestoredSession.entryIds` (aligned with `messages`; the summary has none) and the session keeps `#entryIds` aligned with its context.
- Knobs: `--compact-at <ratio>` validated in `parseCli` (0.3–0.95, else the usage error, exit 1, no provider created) → `flags.compaction.triggerRatio` → `settings.compaction` → `SessionOptions.compaction`. `/compact [instructions]` → `AgentSession.compact()` (idle only; `running` is true and Esc aborts while it runs; the TUI shows a `compacting…` spinner).
- Front ends: status bar gauge is `status().tokens` = `estimateTokens` of system + messages (was the last `usage.inputTokens`); transcript line is now `compacted <before> → <after> tokens` (was `compacted: …`); headless text mode prints the same line on stderr; `--json` prints the `compaction` event as-is.

**Files modified**
- `apps/harness/src/compaction/estimate.ts` — new: the estimator.
- `apps/harness/src/compaction/compaction.ts` — new: schema, prompts, cut, summariser, `Compactor`, `retryOnOverflow`.
- `apps/harness/src/session.ts` — trigger in `prepareTurn`, `compact()`, `#compact`/`#recover`, `#entryIds`, estimator observation, gauge from the estimate; `lastInputTokens` removed.
- `apps/harness/src/session-store/jsonl.ts` — `RestoredSession.entryIds`.
- `apps/harness/src/cli.ts` — `--compact-at`, usage line, `compaction` passed to the session.
- `apps/harness/src/tui/commands.ts` — `/compact [instructions]` (and the stale "joins later" comment).
- `apps/harness/src/tui/app.ts` — `#busy()` spinner helper, used by prompts and by `/compact` when idle (mid-run `/compact` is refused at once and the run keeps its spinner).
- `apps/harness/src/tui/status-bar.ts`, `src/tui/transcript.ts`, `src/headless/print.ts` — gauge comment/type, inline line, stderr line.
- Tests: `tests/compaction/estimate.test.ts` (7), `tests/compaction/compaction.test.ts` (20), `tests/session.test.ts` (+10 in `AgentSession compaction`, 2 gauge tests rewritten), `tests/cli.test.ts` (+2), `tests/tui/commands.test.ts` (+2), `tests/headless/print.test.ts` (+1), `tests/tui/app.test.ts` (+1: `/compact during a run is refused with one line and the run keeps its spinner` — fails without the idle guard), `tests/tui/app.test.ts` / `status-bar.test.ts` / `transcript.test.ts` (gauge/line expectations updated).

**Tests**
- Unit: 846 passing, 0 failing, 46 files (`make harness-tests`; was 803 / 44).
- Integration: N/A (the harness has no integration suite).

**Acceptance criteria**
- [x] 10 000 window, trigger at 8 000: `[<summary>, …tail]`, same system prompt, activated tool still sent — `tests/session.test.ts::AgentSession compaction > compacts in prepareTurn past 80 % of a 10 000 window…`
- [x] `--compact-at 0.5` at 5 000; 0.2 / 0.99 rejected at parse time; `/model` to a smaller window compacts next turn — `…> triggerRatio 0.5 (--compact-at 0.5) compacts at 5 000 where 0.8 does not`, `tests/cli.test.ts::parses --compact-at and rejects…`, `::--compact-at 0.5 compacts a 10 000 window at 5 000; --json prints the compaction event`, `…> re-evaluates the trigger after /model…`
- [x] The cut never splits a tool batch (a 20 000 boundary inside a batch moves to the next `user`) — `tests/compaction/compaction.test.ts::cutPoint > moves a boundary that falls inside a tool batch forward to the next user message` (+3 boundary cases)
- [x] Second compaction merges the previous summary into one `<summary>`; `/compact focus on the test failures` reaches the summariser — `…> merges the previous summary on a second compaction…`, `…> /compact <instructions> runs now…`, `tests/tui/commands.test.ts::/compact <instructions> passes them to the summariser…`
- [x] Overflow → compact + one retry; second overflow → `agent_end(error)`; 3 summariser failures → one `error`, no more attempts — `…> context_length_exceeded: compacts and retries the request once; nothing is lost on disk`, `…> a second overflow after the retry ends with agent_end(error)`, `…> three failed summaries: one error event, no more attempts, the run goes on`, `compaction.test.ts::Compactor` / `::retryOnOverflow`
- [x] Session figure: JSONL `compaction` entry loads as `[summary][tail]` + activations; originals on disk; a resumed session compacts again from the right entry — `…> the session figure…`
- [x] `estimateTokens` ratio from the last `usage.inputTokens`, clamped 0.5–2 — `tests/compaction/estimate.test.ts`
- [ ] [HUMAN] TUI `/compact` mid-session: inline line, gauge drops, the next answer still knows the goal and paths — left for the human.
- [x] Cadence green (below).

**Evidence**
```
$ make harness-lint-fix && make harness-lint-check && make harness-typecheck && make harness-tests && make pre-commit
pnpm exec biome check src tests
Checked 102 files in 53ms. No fixes applied.
pnpm exec tsc --noEmit
 Test Files  46 passed (46)
      Tests  846 passed (846)
biome check (harness)....................................................Passed   (prettier, ruff: Passed)

# live, fake TREE_AGENT_HOME in the scratchpad, --no-mcp --no-skills
$ make -C apps/harness run PROMPT="Reply with exactly: hi" ARGS="--json --compact-at 0.5 --cwd <scratch>"
{"type":"turn_start"} … {"type":"turn_end","message":{"content":"hi",…,"usage":{"inputTokens":1544,"outputTokens":4}}}
{"type":"agent_end","reason":"completed"}                      # exit 0
$ make -C apps/harness run PROMPT="hi" ARGS="--compact-at 0.2 …"
--compact-at must be from 0.3 to 0.95: 0.2                      # + usage, exit 1

# live summariser probe (scratch script: Compactor.compact on a 5-message context, keepRecentTokens 50,
# instructions "keep the failing test names") on anthropic/claude-haiku-5.5 via OpenRouter:
cut 4; schema-valid Summary; "## Critical context\n- Failing test: tests/parser.test.ts > rejects empty input\n
- Error: TypeError: cannot read 'length' of undefined at tokenize (src/parser.ts:42)\n- Test command: pnpm vitest run …"
```

**Notes**
- Cut fallback (implementation detail, the invariant is the spec's): forward to the nearest `user` message; with none ahead (one prompt and many tool turns, the autonomous-run case) forward to the nearest `assistant` message (a turn start, so its `tool` results follow it); with only `tool` results after the walk point, back to the assistant that owns them. The tail never starts with a `tool` message.
- `keepTokens = min(keepRecentTokens, ⌊triggerRatio × contextLength / 2⌋)`: with the 20 000 default and a 10 000 window the tail is 4 000, so a small window still frees room instead of re-triggering every turn. That cap is what makes the 10 000-window AC work with default settings.
- Summariser input: each tool result enters as head + tail of ≤ 2 000 bytes (`TOOL_RESULT_BYTES`, pi's serializer does the same); messages are serialised pi-style (`[User]:`, `[Assistant]:`, `[Assistant tool calls]:`, `[Tool result <name>]:`).
- `maxSummaryTokens` (spec §13) is not sent: `decide()` has no `maxOutputTokens` option and ADR-016 §6 says reuse it as-is; the schema bounds the answer. The summariser's usage is not added to the session cost — `decide()` does not forward `onUsage` (the auto classifier's cost isn't counted either). Follow-up candidate if cost accuracy matters.
- `/compact` after the breaker tripped still runs (the user asked) and a success re-arms automatic compaction.
- Known ceiling: when the kept tail alone is above the trigger (one huge tool result), the trigger fires again next turn; and with a tiny head the summary can be larger than what it replaced (`after > before`). Both are bounded to one summary per turn; not guarded further.
- Public type change: `SessionStatus.tokens` went from `number | undefined` to `number` — always (the estimate incl. the system prompt), so the existing gauge tests in `session.test.ts` and `tui/app.test.ts` were rewritten against the estimate rather than `usage.inputTokens`.
- NOT RUN: a live compaction trigger through the CLI (no tiny-context model on OpenRouter; a 0.3 × 1 M trigger would cost ~300 k tokens). The in-process `main()` test with FakeProvider covers the CLI path; the live probe above covers the summariser on the real decision model.
- Behaviour change at session level: a stream whose first event is a `done` with `stopReason: "length"` and no output (no text, no tool calls) is now read as a context overflow (spec §5, "finish_reason: length on the first chunk") → compact + one retry, instead of the loop's `Continue.` path. A `length` stop after any output still takes the loop's path unchanged.
- Mutation checks: seeding `#entryIds` with `[]` on resume fails the session-figure test; dropping the TUI idle guard fails the new app test.

### [Tester] 2026-10-25 04:40 — QA

**Test summary**
- Format / lint / pre-commit: PASS (`make harness-lint-check`, `harness-typecheck`, `make pre-commit`; biome "Checked 102 files, No fixes applied")
- Unit tests: 846 passed / 0 failed (46 files) on the final run. Integration: N/A (harness has none).
- Warnings: 0 vitest warnings (the `[tree-agent] warn:` lines are the tests' expected `log.warn` output).
- Flake (pre-existing, not 212): `tests/session.test.ts > AgentSession with codemode > an llm() call settling after an abort…` failed 1/15 isolated runs on the working tree and 1/40 on a pristine `git archive HEAD` copy (timing test: 10 ms abort / 20 ms late settle / 60 ms wait). Passed on the next full run.

**E2E adversarial pass** (a throw-away `tests/qa212.test.ts`, 28 tests, run against the real `AgentSession` + `FakeProvider` + `SessionStore.jsonl`; deleted afterwards; plus real CLI runs)
- Happy path: `TREE_AGENT_HOME=<scratch> make -C apps/harness run PROMPT="Reply with exactly: hi" ARGS="--json --no-mcp --no-skills --compact-at 0.5 --cwd <scratch>"` → `text_delta "hi"`, `turn_end usage.inputTokens 1551`, `agent_end completed` (PASS). Live trigger NOT attempted (300 k tokens).
- Trigger: `due()` at exactly 8 000 (10 000 window, ratio 0.8) true, 7 999 false, 7 999 + 1 char (8 000 ceil) true (PASS). k: usage 2× → ratio 2; 0 / NaN / −5 → unchanged; 10× and Infinity → 2; 1 token → 0.5; 1.5× → 1.5; empty sent context → no divide by zero (PASS). Session: first turn reports `inputTokens` 10 000 → k=2 → compacts where the same script without usage (≈6 000) does not (PASS). `/model` to a 200 k model → no compaction; to a 5 000 model → exactly 1 compaction on the next prompt, `status().contextLength` 5 000 (PASS).
- Cut point: 3 000 random contexts (`seed`-fixed, 1–12 turns, 0–3 tool calls, random keep) → `cut` in range, `messages[cut]` never `tool`, no kept `tool` result without its owning assistant in the tail (PASS). Batch at the very end with no user → assistant fallback, intact (PASS). One prompt + 9 write turns in a 10 000 window → compacts; no request starts `[summary, tool…]` (PASS). Tool result with no owner (`[tool, tool, user, assistant]`, `[tool, tool]`) → no crash, `cutPoint` 0 (PASS). Steer + follow-up queued while the summariser runs inside `prepareTurn` → both reach the next request (PASS).
- "Tail alone over trigger" quantified: one 9 000-token result in a 10 000 window then 5 small turns → 1 summariser call over 7 requests, event `before 9292 → after 9329` (summary larger than the head, as documented), then settles. 12 000-token result across 3 prompts → 2 summariser calls, final estimate 316 tokens. No per-turn loop (PASS with note).
- Summary: planted `PATH=/srv/app/src/deep/file.ts` at the head and `TypeError: boom at tokenize (src/parser.ts:42)` at the tail of a 10 KB tool result → both present in the summariser INPUT; tool part = 2 048 bytes (2 000 content + the 48-byte `[... N bytes omitted ...]` marker) (PASS). Schema-failing answer (`{goal: 5}`) twice → messages unchanged, no summary message, no `compaction` JSONL entry, no error event yet (PASS). `/compact "   "` → no `Additional focus`; second `/compact focus on the test failures` → input has `<previous-summary>`, "Update the existing structured summary", the focus line; context holds exactly one `<summary>` (`match(/<summary>/g)` length 1) (PASS).
- Overflow: first-event `length` with no output → compact + retry → completed, 3 requests (PASS). `length` after "part" text → not overflow: no summariser call, next request ends with `Output was cut off. Continue.` (PASS). Error after a `text_delta` (mid-stream) → `agent_end(error)`, no compaction, no crash (PASS). Overflow with a failing summariser → `agent_end(error)`, exactly 1 summariser call (PASS). Overflow with nothing to compact → error, 0 summariser calls (PASS). Overflow after the breaker tripped → `agent_end(error)`, 3 summariser calls total, no loop (PASS).
- Breaker: fail, fail, ok, fail, fail → no `error` event until my script ran out of answers and the third consecutive failure tripped it (correct behaviour; my script ran out) (PASS). 3 failures → ONE `error`, no 4th call; `/compact` then calls the summariser (4th call) and succeeds; the next growth compacts automatically again (re-armed) (PASS). Four consecutive Esc-aborted `/compact` calls → each rejects, `running` false afterwards, messages byte-identical, no `compaction` entry, no `error` event, and a 5th `/compact` succeeds (aborts do not count) (PASS).
- Store: lines present when the `compaction` event fires are a byte-prefix of the final file (append-only); resume → 2 more compactions → file prefix of the pre-resume bytes unchanged, 3 compaction entries each with a `firstKeptEntryId` that is a real `message` entry id, `load()` equals the live session messages with exactly one summary (PASS). Appended a `compaction` with `firstKeptEntryId: "nope"` → loads as `[summary BAD]` only (latest wins; see note 3); appended a crash-cut half line → `warn … not a session entry; skipped`, still loads (PASS).
- CLI: `--compact-at` `0.2 0.99 abc 1 "" " " 0 NaN Infinity 0.29999 0.950001 0x0.5` → `--compact-at must be from 0.3 to 0.95: <v>` + usage, exit 1 (checked 0.2/0.99/abc/1: exit 1, no provider call); `-0.5` → parseArgs "ambiguous" usage error; no value → "argument missing"; `0.3`, `0.95`, `5e-1`, `" 0.5 "` accepted (reach the provider) (PASS).
- Mutations: (1) `let cut = boundary("user")` → `let cut = -1` → 8 tests fail (cutPoint ×4, Compactor summarise, runCommand `/compact` nothing-to-do, 2 session compaction tests). (2) delete the success `this.#failures = 0` → `Compactor > a success in between resets the count` fails (+ the known flake). Both restored; `shasum` identical to the saved original (`64a210fd…`).

**Acceptance criteria**
- [x] PASS — 10 000-window trigger, `[summary, …tail]`, same system prompt, activated tool sent — `tests/session.test.ts` "compacts in prepareTurn past 80 % of a 10 000 window…" (green) + my exact-8 000 / k-update / `/model` checks above
- [x] PASS — `--compact-at 0.5` at 5 000; 0.2 / 0.99 rejected; `/model` smaller compacts next turn — "triggerRatio 0.5 …", `tests/cli.test.ts` compact-at tests, "re-evaluates the trigger after /model…" + CLI matrix above
- [x] PASS — cut never splits a tool batch — `tests/compaction/compaction.test.ts > cutPoint > moves a boundary that falls inside a tool batch forward…` + 3 000-case random check; mutation 1 kills it
- [x] PASS — second compaction merges previous summary (one `<summary>`), `/compact focus…` reaches the summariser — "merges the previous summary on a second compaction…", "/compact <instructions> runs now…", my grep-count check
- [x] PASS — overflow compacts + retries once; second overflow `agent_end(error)`; 3 failures → one `error`, no more attempts — "context_length_exceeded: compacts and retries…", "a second overflow after the retry…", "three failed summaries…", plus my overflow/breaker paths
- [x] PASS — session file with `compaction` loads as `[summary][tail]` + activations; originals stay on disk — "the session figure…" and my prefix-bytes / resume check
- [x] PASS — `estimateTokens` ratio from last `usage.inputTokens`, clamped 0.5–2 — `tests/compaction/estimate.test.ts` (7) + my clamp matrix
- [ ] [HUMAN] — TUI `/compact` mid-session (left unticked, awaiting human verification)
- [x] PASS — cadence green: lint-check, typecheck, 846/846 tests, pre-commit

**Regression checks**
- `agent-loop.ts` 180 lines; `git diff HEAD -- src/loop src/decision` empty (loop.ts and `decide.ts` unchanged).
- TUI boundary: `grep -rln "provider/\|loop/\|tools/\|mcp/\|codemode/\|compaction/" apps/harness/src/tui` → empty (exit 1). `pnpm install --frozen-lockfile` → "Already up to date".
- `git status` identical to the start of QA: no `.env*`, no `apps/memory`, spec doc still untracked and untouched; `tests/session-store/jsonl.test.ts` green after the `entryIds` change.
- Transcript line `compacted 900 → 100 tokens` (`tests/tui/transcript.test.ts:139`); gauge `160k / 200k (80 %)` (`tests/tui/status-bar.test.ts:29`); headless text writes `compacted <b> → <a> tokens` on stderr; `--json` prints the raw `compaction` event (`tests/headless/print.test.ts`, `tests/cli.test.ts`).

**Other issues found (non-blocking, orchestrator decides)**
1. Text typed in the TUI while `/compact` runs is routed to `steer()` (`running` is true), shown in the transcript as a steer, but nothing delivers it: after `compact()` resolves there is no run, so it is only sent with the NEXT `prompt()`. Repro: `const p = session.compact(); session.steer("x"); await p;` → 0 new provider requests until the next `prompt()`, which then contains "x". Not lost, but the user sees a steer that gets no reply. Possible fix: in `app.ts` queue it as a prompt after `#busy` finishes, or refuse input while compacting.
2. `contextLength` 0/unknown (`fakeModel({contextLength: 0})`) → `due()` is always true: 4 summariser calls over 5 requests. Guard `contextLength > 0` in `Compactor.due` (OpenRouter's `context_length` is typed `number`, so unlikely in practice).
3. `restore()` with an unknown/missing `firstKeptEntryId` (also what `#compact` writes as `""` if `#entryIds[cut]` were ever undefined) silently drops the whole tail: `kept = []`, no `log.warn` (`session-store/jsonl.ts:133-134`, 205 code). The brief expected a warning; consider a `log.warn` there.
4. A `length` stop with empty content on the first event is now an overflow (compact + retry once, else error), so a reasoning model that spends the whole output budget on hidden reasoning costs a summariser call instead of the old "Continue." (documented behaviour change).
5. The estimator counts UTF-16 code units / 4: CJK/emoji-heavy contexts are under-estimated, bounded by the k ≤ 2 clamp; the overflow retry is the safety net.
6. Pre-existing flake above (session cost/abort timing test), ~2–3 % per run, reproduces at HEAD.

**VERDICT: PASS**

### [SWE] 2026-10-25 04:41 — Fold-ins before commit

**Files modified**
- `apps/harness/src/compaction/compaction.ts` — `Compactor.due` returns false when `contextLength` is not > 0 and `log.warn`s once per session (Tester note 2).
- `apps/harness/src/session-store/jsonl.ts` — `restore()` warns `session <id>: compaction <entry> keeps from unknown entry <firstKeptEntryId>; its tail is dropped`; behaviour unchanged (note 3).
- `apps/harness/src/tui/app.ts` — while a TUI `/compact` runs, typed text (Enter or Alt+Enter, not a `/command`) is refused with the error notice `compacting… send it when done (Esc aborts)` instead of becoming a steer that no run answers (note 1).
- `apps/harness/tests/session.test.ts` — the 211 flake "an llm() call settling after an abort…" is now deterministic: the provider signals when the `llm()` request is in flight, the test aborts then, and the late settle is a deferred the test resolves after `prompt()` returns `aborted`. No wall-clock timers. It also asserts `llm_call` is not saved before the settle, so the late path is really exercised (note 6).

**Tests**
- New: `tests/compaction/compaction.test.ts > Compactor > is never due on an unknown contextLength, and warns once`; `tests/session-store/jsonl.test.ts > JsonlSessionStore > warns, naming the id, when a compaction keeps from an unknown entry, and drops its tail`; `tests/tui/app.test.ts > runTui > refuses text typed during /compact with one line instead of steering` (red with the guard disabled: timed out waiting for the notice).
- Flake: `tests/session.test.ts` run 30 times in a row with the Makefile's `.env` exported: 30/30 passed.
- `make harness-lint-fix && make harness-lint-check && make harness-typecheck && make harness-tests && make pre-commit`: all green, 46 files, 849/849 tests.

**Notes**
- Not changed (adjacent, possible follow-up): `compact()` itself with `contextLength` 0 (a manual `/compact` or the overflow `#recover`) gets `keepTokens` 0, so it keeps only the last turn. Only the automatic trigger is guarded.
- The [HUMAN] TUI `/compact` criterion is still open.
