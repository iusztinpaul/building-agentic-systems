---
id: 129-session-end-hook-mcp-client-and-e2e
feature: mcp-tool-contracts
status: pending
---

# SessionEnd hook that calls `ingest_conversation` over MCP (pure client) + feature live e2e

Tags: `mcp`, `hooks`, `scripts`, `e2e`
Depends on: #122, #126
Blocks: —
Implements: ADR-008 — Decision 5 (MCP is the only harness↔memory boundary)

## Scope

Claude Code's `Stop` fires per assistant turn (rejected — with first-write-wins on `session_uri`
only the first turn would persist); `SessionEnd` fires once. The hook is a pure MCP client: it
imports no `tree.data` / pipeline code and reaches memory only through `ingest_conversation`.

**Before coding (mandatory):** (a) verify the SessionEnd stdin JSON fields (`session_id`,
`transcript_path`, `cwd`, `hook_event_name`, `reason`) and the transcript JSONL entry shape
(`type: "user" | "assistant"`, `message.content` as a string or a list of `text` / `tool_use` /
`tool_result` blocks, `timestamp`) against the official Claude Code docs via the `claude-code-guide`
agent; (b) `tech-docs` skill (context7 → fastmcp) for `Client` config-dict usage and `call_tool`
(`raise_on_error=False`, `CallToolResult.is_error` / `.content`). Record both doc URLs in the module docstring.

**1. Logic — `apps/memory/src/tree/mcp/hooks.py`.** Imports: stdlib + `fastmcp` + `pydantic` ONLY
(pydantic per CLAUDE.md; it ships with fastmcp). Pydantic models `HookInput` (`session_id`,
`transcript_path`, `cwd`), `TranscriptTurn` (`role`, `text`, `timestamp: datetime` tz-aware) and
functions:
- `read_hook_input(stream: TextIO) -> HookInput`.
- `parse_transcript(path: Path) -> list[TranscriptTurn]` — keep `user` / `assistant` entries; from
  list content keep `type == "text"` blocks only (tool calls/results dropped); skip malformed lines
  with a WARNING; timestamps parsed tz-aware (naive → assume UTC, log once).
- `build_ingest_args(turns, session_id) -> dict[str, Any]` — `conversation_text` as
  `User: …\n\nAssistant: …` blocks; `title = f"Claude Code session {session_id[:8]} — {first.date()}"`;
  `session_uri = f"claude-session://{session_id}"`; `session_started_at` = first turn, UTC ISO with `Z`.
- `load_server_config(mcp_json: Path, server_name: str, *, cwd: Path) -> dict[str, Any]` — returns
  `{"mcpServers": {server_name: entry}}` for ONE server; unknown name → `KeyError` with the names
  present. For stdio entries set `cwd` (the `.mcp.json` args are repo-root relative) and merge
  `env["MCP_SKIP_INDEX_BOOTSTRAP"] = "1"` so the spawned local server never touches indexes.
  Remote entries pass through (the cloud `tree-memory` needs Horizon OAuth — opt-in, documented).
- `async def run(stdin: TextIO, mcp_json: Path, server_name: str, *, min_words: int = 200) -> int`
  — ALWAYS returns 0. Guards, each a single INFO/WARNING line then return: transcript missing or
  < `min_words` words → skip; `fastmcp.Client` connect/tool call raising → skip; tool answer
  parsed as JSON containing `error_type` → log `error_type` + `message`, skip. Success: log the
  receipt (`source_uri`, `duplicate`, `flow_run_id`).

**2. Glue — `apps/memory/scripts/hook_session_end.py`:** `init_logger()` at module level; one
positional arg `server_name` (default `tree-memory-local`); resolves the repo root from
`HookInput.cwd` (fallback: two parents up from `apps/memory`); `sys.exit(asyncio.run(run(...)))`.
No logic.

**3. Wiring — repo-root `.claude/settings.json`:**
`"hooks": {"SessionEnd": [{"hooks": [{"type": "command", "command": "uv --directory apps/memory run --env-file ../../.env python scripts/hook_session_end.py tree-memory-local", "timeout": 60}]}]}`.

**4. Tests** — `tests/unit/mcp/test_hooks.py` with a fixture
`tests/unit/mcp/fixtures/session_end_transcript.jsonl` (user text, assistant text + a `tool_use`
block + a `tool_result` entry, ≥ 200 words). `TestModulePurity::test_hooks_imports_only_allowlisted`
mirrors `test_cleaning.py::TestModulePurity` (AST walk; allow-list = stdlib modules + `fastmcp` +
`pydantic`; explicitly asserts no `tree.`, `pymongo`, `beanie`, `prefect`, `motor`).
`tests/unit/scripts/test_hook_session_end.py` mirrors the other script tests (importable, calls `run`).

**5. Docs** — `apps/memory/README.md`: "SessionEnd hook" subsection (what it stores, the 200-word
guard, how to point it at the cloud server, how to disable). `.agents/skills/run-pipelines-e2e/SKILL.md`
step 3 gains: the hook smoke test command, the `nothing_found` / `text_only` checks, the duplicate receipt.

**6. Feature live e2e (run-pipelines-e2e skill, `rag` mode, local stack) — record in `## Log`:**
ingest one URL twice (second receipt `duplicate: true`); index (readiness log shows
`ready (status=READY)`); `search_memory` on-topic → `found`, nonsense → `nothing_found`;
`mongosh` `db.memory.dropSearchIndex("vector_index")` → `search_memory` → `search_mode: "text_only"`
with the skill caveat; `make memory-run-indexing-pipeline` restores it; blank query → envelope;
hook smoke test: `echo '{"session_id":"e2e-…","transcript_path":"tests/unit/mcp/fixtures/session_end_transcript.jsonl","cwd":"<repo>","hook_event_name":"SessionEnd","reason":"other"}' | uv --directory apps/memory run --env-file ../../.env python scripts/hook_session_end.py tree-memory-local`
completes < 60 s, logs the receipt, and `search_memory("<a phrase from the fixture>")` finds it after indexing.

## Out of scope
- Upsert on `session_uri` (first write wins). A `Stop`-event variant. Wiring the cloud server by default.
- Any `tree.*` import in the hook — by construction.

## Acceptance Criteria

- [ ] `tree/mcp/hooks.py` imports only stdlib + `fastmcp` + `pydantic` (no `tree.*`, `pymongo`, `beanie`, `prefect`) — `tests/unit/mcp/test_hooks.py::TestModulePurity::test_hooks_imports_only_allowlisted`.
- [ ] `parse_transcript(fixture)` keeps user/assistant text only; the `tool_use` block and `tool_result` entry are absent from every `text` — `::TestParseTranscript::test_keeps_text_blocks_only`; malformed line skipped with a WARNING — `::test_skips_malformed_line`.
- [ ] `build_ingest_args` yields `session_uri == "claude-session://<id>"`, title `Claude Code session <id[:8]> — <YYYY-MM-DD>`, `session_started_at` ending in `Z` from the first turn — `::TestBuildIngestArgs::test_shape`.
- [ ] `load_server_config` picks ONE server, sets `cwd`, injects `MCP_SKIP_INDEX_BOOTSTRAP=1`, and raises `KeyError` listing the names for an unknown server — `::TestLoadServerConfig` (3 tests).
- [ ] `run()` returns 0 and calls no tool when the transcript has < 200 words — `::TestRun::test_short_transcript_skips`.
- [ ] `run()` returns 0 when `Client` raises on connect — `::test_unreachable_server_exits_zero`; when the tool answers an `error_type` envelope — `::test_error_envelope_skips` (log line names `error_type`); on success logs `source_uri` + `flow_run_id` — `::test_logs_receipt` (fake `Client` via `mocker.patch("tree.mcp.hooks.Client")`).
- [ ] `{}` piped into the script exits 0 (missing fields → skip, never non-zero) — `tests/unit/scripts/test_hook_session_end.py::test_empty_stdin_exits_zero`.
- [ ] `.claude/settings.json` has `hooks.SessionEnd[0].hooks[0].command` ending in `hook_session_end.py tree-memory-local` with `timeout: 60` — `jq '.hooks.SessionEnd[0].hooks[0]' .claude/settings.json`.
- [ ] Module docstring cites the Claude Code hooks doc URL and the fastmcp Client doc URL — `grep -n "https://" apps/memory/src/tree/mcp/hooks.py`.
- [ ] Live e2e (§6) recorded in `## Log`: duplicate receipt, `ready (status=READY)`, `found`, `nothing_found`, `text_only` + caveat, blank-query envelope, hook smoke test < 60 s and its conversation retrievable.
- [ ] `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green.

## User Stories

### Story: Developer ends a long debugging session
1. Developer types `/exit` after a 40-minute session (transcript ≫ 200 words).
2. The hook reads `session_id` + `transcript_path` from stdin, keeps the user/assistant text, calls `ingest_conversation(conversation_text=…, title="Claude Code session 3f9a1c2b — 2026-09-12", session_uri="claude-session://3f9a1c2b-…", session_started_at="2026-09-12T08:02:11Z")` via the `tree-memory-local` server.
3. Hook log: `Ingested session claude-session://3f9a1c2b-… flow_run_id=…`; exit 0 within 60 s.
4. Next session, `search_memory("that flaky index test")` returns a parent from that conversation.

### Story: Developer ends a two-line session
1. Session transcript is 30 words. Hook logs `transcript 30 words < 200 — skipped`, exit 0; nothing is called.

### Story: Local Mongo is stopped
1. The spawned server fails its lifespan. Hook logs a WARNING with the exception class and exits 0; Claude Code exits normally.

### Story: Same session ends twice (resume then exit again)
1. Second SessionEnd for the same `session_id`: the tool answers `duplicate: true` (first write wins); the hook logs it and exits 0.

### Story: Developer opts into the cloud server
1. Developer changes the hook arg to `tree-memory` and completes Horizon OAuth once as documented in the README.
2. The hook uses the remote entry unchanged; the receipt is logged the same way.

---

Blocked by: #122, #126

## Log

### [PA] 2026-09-12 10:35 — Grooming

**Summary**
Session persistence moves out of the skill into a SessionEnd hook that is nothing but an MCP client; the task closes with the feature's live e2e.

**Key decisions**
- `SessionEnd` over `Stop` (first-write-wins would keep only the first turn).
- Allow-list = stdlib + fastmcp + pydantic, AST-enforced; the rule that matters is "no `tree.*`, no DB/pipeline libraries".
- Hook injects `MCP_SKIP_INDEX_BOOTSTRAP=1` into the spawned server's env itself (hook entries have no env field).
- Always exit 0 — a memory hiccup must never block leaving a session.

**Dependencies**
- #122 — the receipt the hook logs. #126 — the `error_type` envelope the hook checks.

**User stories**
- 5 stories covering: long session, short session, server down, repeated end, cloud opt-in.

Ready for implementation.
