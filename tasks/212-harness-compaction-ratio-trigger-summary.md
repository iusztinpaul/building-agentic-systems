---
id: 212-harness-compaction-ratio-trigger-summary
status: pending
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

- [ ] With a fake `modelInfo` of `contextLength` 10 000 and a FakeProvider run that grows the context past
      8 000 estimated tokens, `prepareTurn` compacts before the next request: the request's messages are
      `[<summary> user message, …tail]`, the system prompt is unchanged, the activated tools are still sent.
- [ ] `--compact-at 0.5` compacts at 5 000; `0.2` and `0.99` are rejected at parse time; `/model` to a model
      with a smaller `contextLength` compacts on the next turn without new messages.
- [ ] The cut never splits an assistant `tool_calls` message from its `tool` results (a context whose
      20 000-token boundary falls inside a tool batch moves the cut to the next `user` message).
- [ ] A second compaction merges the previous summary (the summariser input contains the earlier summary and
      the result has one `<summary>` block); `/compact focus on the test failures` passes the instruction to
      the summariser.
- [ ] A `context_length_exceeded` error compacts and retries once; a second overflow ends with `agent_end(error)`;
      three consecutive summariser failures (FakeProvider rejects) emit one `error` event and stop further
      compaction attempts for the session.
- [ ] A session file with a `compaction` entry loads as `[summary][tail]` plus the activations recorded before
      it (the "session figure" test); the original messages stay on disk.
- [ ] `estimateTokens` applies the ratio from the last `usage.inputTokens` clamped to 0.5–2.
- [ ] [HUMAN] In the TUI, `/compact` mid-session shows the inline `compacted …` line, the gauge drops, and the
      next answer still knows the task's goal and file paths (ask it).
- [ ] `make harness-lint-fix && make harness-lint-check && make harness-typecheck && make harness-tests &&
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
