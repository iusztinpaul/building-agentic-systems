---
id: 131-session-end-hook-transcript-cap
status: pending
feature: mcp-tool-contracts-followups
---

# Session-end hook: cap the transcript client-side so long sessions are persisted, not skipped

Tags: `mcp`, `hooks`
Depends on: None (builds on `tasks/done/129`)
Blocks: —
Implements: ADR-008 — Decision 5 (the hook stays a pure client; the cap is enforced on its side of the boundary)

## Scope

`ingest_conversation` rejects a payload over `tree.online.MAX_SOURCE_PAYLOAD_BYTES` (400 000 B,
the flow-run parameter cap) with `invalid_input`; the **Session-end hook** has no cap of its
own, so a long session — the one most worth remembering — ends in
`ingest_conversation answered error_type=invalid_input … — session not persisted.` (task 129's
Tester reproduced it with a 5 MB transcript). Trim on the client so the call always fits.

- `apps/memory/src/tree/mcp/hooks.py`: `_MAX_CONVERSATION_BYTES = 380_000` — a module constant
  (the hook may NOT import `tree.online`; purity is AST-enforced) that leaves ~20 KB for the
  other arguments and the JSON envelope. A comment names the server constant it mirrors.
- `build_ingest_args(turns, session_id)`: if the joined `conversation_text` exceeds the cap in
  UTF-8 bytes, drop the OLDEST whole turns until it fits — the end of a session holds its
  conclusions — and prepend one line `[Session-end hook: N earlier turn(s) dropped to fit the
  ingest cap]` so the stored text says it is partial. If the LAST turn alone exceeds the cap,
  keep its tail (cut the head) and say so in the same line. Log ONE WARNING with the dropped
  count and byte sizes. `title`, `session_uri` and `session_started_at` are unchanged
  (`session_started_at` still comes from the FIRST turn of the ORIGINAL transcript).
- The word-count guard (`min_words`) still runs on the full transcript, before trimming.
- `apps/memory/README.md` "SessionEnd hook" → "What it stores": one sentence on the cap.

## Out of scope
- Summarising instead of truncating. Splitting one session into several documents.
- Changing the server cap.

## Acceptance Criteria

- [ ] `_MAX_CONVERSATION_BYTES < tree.online.MAX_SOURCE_PAYLOAD_BYTES` and within 25 KB of it — `tests/unit/mcp/test_hooks.py::TestTranscriptCap::test_cap_mirrors_the_server_constant` (the TEST may import `tree.online`; the module may not — `TestModulePurity` stays green).
- [ ] A transcript whose text is 3× the cap → `conversation_text` ≤ cap bytes, the LAST turn is present verbatim, the first line is the `[Session-end hook: N earlier turn(s) dropped …]` notice, and ONE WARNING names `N` — `::test_drops_oldest_turns_until_it_fits`.
- [ ] A single final turn of 2× the cap → its tail is kept, the notice says the turn was cut — `::test_oversized_last_turn_keeps_its_tail`.
- [ ] A transcript under the cap is byte-identical to today's output (no notice, no WARNING) — `::test_small_transcript_is_untouched`.
- [ ] `session_started_at` comes from the first ORIGINAL turn even when that turn was dropped — `::test_session_started_at_survives_trimming`.
- [ ] Live: the 129 Tester's synthetic 12 000-turn transcript piped through the wired command logs `Ingested session … duplicate=False flow_run_id=…` (no `invalid_input`) — recorded in `## Log`.
- [ ] The README `SessionEnd hook` smoke-test blurb and the e2e skill's Session-end hook step each say, in ONE sentence, that the fixture's line 4 is deliberately malformed and prints one `Skipping malformed transcript line 4` WARNING before the receipt — `grep -c "malformed transcript line 4" apps/memory/README.md .agents/skills/run-pipelines-e2e/SKILL.md` → 1 each. (PA round-2 acceptance review, from the Tester's note on `tasks/done/130`.)
- [ ] `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green.

## User Stories

### Story: Developer ends a four-hour session
1. The transcript's user/assistant text is 1.2 MB.
2. Hook log: `Transcript 1200000 B over the 380000 B cap — dropped 212 earlier turn(s), kept 41`, then `Ingested session claude-session://… duplicate=False flow_run_id=…`; exit 0.
3. `search_memory` later finds a parent from the LAST hour of the session; its document's text starts with the `[Session-end hook: 212 earlier turn(s) dropped …]` line.

### Story: Developer ends a normal session
1. The transcript is 40 KB. Nothing is dropped, no notice line, no WARNING — identical to today.

---

Blocked by: (none)

## Log

### [PA] 2026-09-12 18:20 — Grooming

**Summary**
The hook trims the oldest turns to the server's payload cap so the longest sessions persist instead of skipping.

**Key decisions**
- Drop oldest whole turns, keep the end: a session's conclusions are at the end.
- Constant duplicated (purity) and pinned to the server constant by a test, not by an import.
- A visible notice line in the stored text over a silent cut — memory must not read partial as complete.

**Dependencies**
- None (task 129 is done).

**User stories**
- 2 stories covering: oversized session, normal session.

Ready for implementation.

### [PA] 2026-09-12 19:05 — Grooming addendum

Added one doc AC from the round-2 acceptance review of PR #43: the documented smoke test prints an
unexplained `Skipping malformed transcript line 4` WARNING (the fixture's line 4 is malformed on
purpose, to exercise the guard). This task already rewrites the same README paragraph for the cap
notice, so the sentence lands here rather than in a task of its own.

