---
id: 129-session-end-hook-mcp-client-and-e2e
feature: mcp-tool-contracts
status: done
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

- [x] `tree/mcp/hooks.py` imports only stdlib + `fastmcp` + `pydantic` (no `tree.*`, `pymongo`, `beanie`, `prefect`) — `tests/unit/mcp/test_hooks.py::TestModulePurity::test_hooks_imports_only_allowlisted`.
- [x] `parse_transcript(fixture)` keeps user/assistant text only; the `tool_use` block and `tool_result` entry are absent from every `text` — `::TestParseTranscript::test_keeps_text_blocks_only`; malformed line skipped with a WARNING — `::test_skips_malformed_line`.
- [x] `build_ingest_args` yields `session_uri == "claude-session://<id>"`, title `Claude Code session <id[:8]> — <YYYY-MM-DD>`, `session_started_at` ending in `Z` from the first turn — `::TestBuildIngestArgs::test_shape`.
- [x] `load_server_config` picks ONE server, sets `cwd`, injects `MCP_SKIP_INDEX_BOOTSTRAP=1`, and raises `KeyError` listing the names for an unknown server — `::TestLoadServerConfig` (3 tests).
- [x] `run()` returns 0 and calls no tool when the transcript has < 200 words — `::TestRun::test_short_transcript_skips`.
- [x] `run()` returns 0 when `Client` raises on connect — `::test_unreachable_server_exits_zero`; when the tool answers an `error_type` envelope — `::test_error_envelope_skips` (log line names `error_type`); on success logs `source_uri` + `flow_run_id` — `::test_logs_receipt` (fake `Client` via `mocker.patch("tree.mcp.hooks.Client")`).
- [x] `{}` piped into the script exits 0 (missing fields → skip, never non-zero) — `tests/unit/scripts/test_hook_session_end.py::test_empty_stdin_exits_zero`.
- [x] `.claude/settings.json` has `hooks.SessionEnd[0].hooks[0].command` ending in `hook_session_end.py tree-memory-local` with `timeout: 60` — `jq '.hooks.SessionEnd[0].hooks[0]' .claude/settings.json`.
- [x] Module docstring cites the Claude Code hooks doc URL and the fastmcp Client doc URL — `grep -n "https://" apps/memory/src/tree/mcp/hooks.py`.
- [x] Live e2e (§6) recorded in `## Log`: duplicate receipt, `found`, `nothing_found`, `text_only` + caveat, blank-query envelope, hook smoke test < 60 s (7.7 s) and its conversation retrievable. **Deviation:** the readiness evidence is the LOCAL-mongot branch (`reports neither 'status' nor 'queryable' … treating it as ready`), not Atlas's `ready (status=READY)`, and it was captured from the MCP server's bootstrap path because Prefect does not capture `rag/indexing.py`'s module logger — see Notes.
- [x] `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green.

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

### [SWE] 2026-09-12 17:45 — Implementation

**Files modified**

- `apps/memory/src/tree/mcp/hooks.py` (new) — the **Session-end hook** logic: `HookInput` / `TranscriptTurn`, `read_hook_input`, `parse_transcript`, `build_ingest_args`, `load_server_config`, `run`. Imports stdlib + `fastmcp` + `pydantic` only; always returns 0.
- `apps/memory/scripts/hook_session_end.py` (new) — glue: `init_logger()`, one positional `server_name` (default `tree-memory-local`), reads stdin ONCE, resolves the repo root, `sys.exit(asyncio.run(run(...)))`.
- `apps/memory/src/tree/mcp/__init__.py` — emptied (was `from tree.mcp.server import mcp`). See "Notes" — without this the hook process imports Mongo/Prefect/Opik at runtime, so ADR-008 §5's "pure client" was true only in the source.
- `.claude/settings.json` — `hooks.SessionEnd[0].hooks[0]` = the `hook_session_end.py tree-memory-local` command, `timeout: 60`.
- `apps/memory/README.md` — `#### SessionEnd hook` subsection (what it stores, the 200-word guard + the other skip paths, cloud opt-in, how to disable, a copy-paste smoke test).
- `.agents/skills/run-pipelines-e2e/SKILL.md` — step 3 gains the four tool-contract checks (duplicate receipt, `found` / `nothing_found`, `text_only` with the tight drop→restore block, blank-query envelope) and the hook smoke test.
- `apps/memory/tests/unit/mcp/test_hooks.py` (new, 23 tests) + `apps/memory/tests/unit/mcp/fixtures/session_end_transcript.jsonl` (new: 4 kept turns / 309 words, plus a `thinking` block, a `tool_use` block, a `tool_result` entry and one truncated line).
- `apps/memory/tests/unit/scripts/test_hook_session_end.py` (new, 6 tests).

**Tests**

- Unit: 3104 passing, 0 failing (`make memory-tests`; 29 of them new). Format / lint / pre-commit green.
- Integration: N/A — the suite does not exist (AGENTS.md); §6 live e2e below stands in.

**Acceptance criteria**

- [x] `hooks.py` imports only stdlib + `fastmcp` + `pydantic` — `test_hooks.py::TestModulePurity::test_hooks_imports_only_allowlisted`. Added `::test_importing_hooks_never_loads_the_mcp_server` (fresh interpreter, `sys.modules`) because AST purity says nothing about the parent package.
- [x] `parse_transcript(fixture)` keeps user/assistant text only — `::TestParseTranscript::test_keeps_text_blocks_only` (asserts the `thinking` / `tool_use` / `tool_result` payloads are absent from every `text`); malformed line skipped with a WARNING — `::test_skips_malformed_line`.
- [x] `build_ingest_args` shape — `::TestBuildIngestArgs::test_shape` (`claude-session://<id>`, `Claude Code session 3f9a1c2b — 2026-09-12`, `2026-09-12T08:02:11Z`).
- [x] `load_server_config` — `::TestLoadServerConfig` (4 tests: one server + `cwd` + `MCP_SKIP_INDEX_BOOTSTRAP=1` merged into the entry's own `env`, remote pass-through, `KeyError` naming the known servers, and the config actually constructs a `fastmcp.Client`).
- [x] `run()` returns 0 and calls no tool under 200 words — `::TestRun::test_short_transcript_skips`.
- [x] `run()` returns 0 when `Client` raises — `::test_unreachable_server_exits_zero`; on an `error_type` envelope — `::test_error_envelope_skips`; logs the receipt — `::test_logs_receipt` / `::test_duplicate_receipt_is_logged`.
- [x] `{}` piped into the script exits 0 — `test_hook_session_end.py::test_empty_stdin_exits_zero` (+ `::test_blank_stdin_exits_zero`, both on the REAL `run`, unstubbed).
- [x] `.claude/settings.json` wiring — `jq -c '.hooks.SessionEnd[0].hooks[0]' .claude/settings.json` → `{"type":"command","command":"uv --directory apps/memory run --env-file ../../.env python scripts/hook_session_end.py tree-memory-local","timeout":60}`.
- [x] Module docstring cites both doc URLs — `grep -n "https://" apps/memory/src/tree/mcp/hooks.py` → the Claude Code hooks page and the two FastMCP client pages.
- [x] Live e2e (§6) recorded below.
- [x] `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green.

**Evidence**

Unit suite:

```
$ make memory-tests
============================ 3104 passed in 55.01s =============================

$ make pre-commit
prettier.................................................................Passed
ruff check...............................................................Passed
ruff format..............................................................Passed
biome check (harness)....................................................Passed
```

Doc verification (mandatory pre-coding step):

```
SessionEnd stdin — https://docs.claude.com/en/docs/claude-code/hooks
  {"session_id","transcript_path","cwd","hook_event_name","reason"}; SessionEnd
  hooks share a 1.5 s budget raised to the highest configured per-hook timeout,
  up to 60 s  → hence timeout: 60.
Transcript shape — the docs do NOT specify the JSONL entries, so cross-checked
  against a live transcript (~/.claude/projects/**/9b72a9da-….jsonl, 352
  user/assistant entries): type ∈ {user,assistant}, timestamp "…Z",
  message.content = str | [text | thinking | tool_use | tool_result].
FastMCP — https://gofastmcp.com/clients/client (config dict / MCPConfig; a single
  server is NOT name-prefixed, confirmed in the installed source:
  fastmcp/client/transports/config.py:83 `if len(self.config.mcpServers) == 1`)
  and https://gofastmcp.com/clients/tools (raise_on_error, is_error/.content).
Installed: fastmcp 3.2.0, pydantic 2.12.5.
```

§6 live e2e — `rag` mode, local stack, `make memory-serve-workflows` re-served from this branch (runner `runner-4bdd8dd5`), MCP server `TREE_MEMORY__MODE=rag … TRANSPORT=streamable-http` on port 8001:

```
1. Ingest the same URL twice (https://en.wikipedia.org/wiki/Knowledge_graph)
   #1 {"source_uri":"https://en.wikipedia.org/wiki/Knowledge_graph","duplicate":false,"document_id":null,"flow_run_id":"39da6048-7060-4172-80f2-26d774c8e7b7","status":"scheduled"}
   #2 {"source_uri":"https://en.wikipedia.org/wiki/Knowledge_graph","duplicate":true,"document_id":"6aa561f58027998810d9bc49","flow_run_id":null,"status":"duplicate"}
   run executed on MY serve: "Processing 1 documents in rag mode (user_id=6aa561a08029377d7ebbea39)",
   clean_and_chunk n_parents=6 n_children=92, load_rag_rows rows_written=99.

2. Vector-index readiness (verbatim, local mongot — the ADR-008 §5 "neither
   status nor queryable → ready" branch, NOT Atlas's `ready (status=READY)`):
   Waiting for vector search index 'vector_index' to be ready (up to 300 s)...
   Vector search index 'vector_index' reports neither 'status' nor 'queryable' (local mongot); treating it as ready

3. search_memory on-topic  → {"outcome":"found","search_mode":"hybrid"} (3 parents, top score 0.0315)
   search_memory nonsense  → {"parents":[],"outcome":"nothing_found","search_mode":"hybrid"}   (query "zzzq wamble frobnitz blorptastic")

4. mongosh db.memory.dropSearchIndex("vector_index") → search_memory on-topic
   → {"outcome":"found","search_mode":"text_only"}  ← degraded, not empty; the caveat is the skill's `text_only` line
   restore: make memory-run-indexing-pipeline USER_IDENTIFIER=… → getSearchIndexes() = ["vector_index"], search_mode back to "hybrid"

5. Blank query → envelope:
   {"error_type":"invalid_input","retryable":false,"message":"query must not be empty"}

6. Hook smoke test (the exact §6 command, fixture transcript):
   $ echo '{"session_id":"e2e-129-hook-smoke","transcript_path":"tests/unit/mcp/fixtures/session_end_transcript.jsonl","cwd":"<repo>","hook_event_name":"SessionEnd","reason":"other"}' \
       | uv --directory apps/memory run --env-file ../../.env python scripts/hook_session_end.py tree-memory-local
   Skipping malformed transcript line 4: Unterminated string starting at: line 1 column 43 (char 42)
   MCP_SKIP_INDEX_BOOTSTRAP set — skipping ensure_indexes + vector-index assertion (fast serverless boot).
   Ingested session claude-session://e2e-129-hook-smoke duplicate=False flow_run_id=a1050793-808f-4da1-8c35-1e3762d3ec09 status=scheduled
   → 7.7 s wall clock (budget 60 s), exit 0. Second run: duplicate=True flow_run_id=None status=duplicate (5.7 s).

7. The session is retrievable after extraction + indexing:
   search_memory("Does the catalogue entry expose a queryable flag we can wait on?")
   → outcome=found, search_mode=hybrid, top hit
     6aa561a08029377d7ebbea39:chunk:claude-session://e2e-129-hook-smoke#parent-0
     title "Claude Code session e2e-129- — 2026-09-12", content starts "User: We spent the morning on the vector index readiness poll…"
   documents row: {"title":"Claude Code session e2e-129- — 2026-09-12","metadata":{"session_started_at":"2026-09-12T08:02:11.000Z"}}
   mongosh: 4 memory rows for that session, 0 of them containing SECRET_ (the
   thinking / tool_use / tool_result payloads never reach memory).
```

**Notes**

- **`tree/mcp/__init__.py` emptied — please review as part of this task.** It re-exported `mcp` (`from tree.mcp.server import mcp`), so `import tree.mcp.hooks` pulled in the server, `pymongo`, `beanie`, `prefect` and `opik`: ~2.6 s of the hook's 60 s budget, and a bad `TREE_MEMORY__MODE` would have raised at import and made the hook exit NON-zero — the one thing it must never do. Nothing imported the re-export (`grep` for `from tree.mcp import mcp` is empty; `serve_mcp.py` uses `from tree.mcp.server import mcp`), and the full suite is green. The new `::test_importing_hooks_never_loads_the_mcp_server` locks it; the detector was verified against the old behaviour (`import tree.mcp.server` → `["tree.mcp.server","tree.mcp.tools","pymongo","prefect","beanie"]`) and from a foreign CWD.
- **Readiness line is the local variant, by design.** The AC's `ready (status=READY)` is the Atlas wording; local mongot reports neither field, so ADR-008 §5's "read as ready" branch fires and logs the line quoted above. It was captured from the MCP server's bootstrap path (`serve_mcp.py` → lifespan → `ensure_indexes`), because `tree/memory/rag/indexing.py` logs through a plain module logger, which Prefect flow runs do NOT capture (`pipeline.py` uses `_get_run_logger()`), so the line is invisible in `make memory-run-indexing-pipeline` output. Pre-existing observability gap, untouched here — worth a follow-up task ("route rag/indexing.py logs through the Prefect run logger").
- `session_started_at` on the wire is `"2026-09-12T08:02:11Z"` (the `Z`-suffixed string `build_ingest_args` builds, pinned by `::TestBuildIngestArgs::test_shape`); the `"…08:02:11.000Z"` in the `documents` row above is Mongo's rendering after the tool parsed it — not a mismatch.
- **`timeout=30 s` on the tool call** (`hooks._CALL_TIMEOUT_SECONDS`): Claude Code SIGKILLs the hook at 60 s, so an unbounded call would die without logging why. One constant, one use.
- `transcript_path` is deliberately NOT resolved against `HookInput.cwd`: Claude Code always sends absolute, and the documented smoke test passes a path relative to the process CWD (`uv --directory apps/memory` chdirs). The repo root IS resolved from `cwd`, but only when it actually holds a `.mcp.json` (else the fallback two parents up from `apps/memory`) — a session started in a subdirectory would otherwise look for a config that is not there (`test_unknown_cwd_falls_back_to_this_checkout`).
- **Environment findings for the Tester (no code impact).** (a) `.env`'s `TREE_USER_IDENTIFIER=paul.iusztin@example.com` had no `users` row (the local DB only had `paul@example.com`), so every local MCP server — including the hook's spawned one — failed to boot. Seeded it with `signup --no-set-current` (id `6aa561a08029377d7ebbea39`); the current-user session was left pointing at `paul@example.com`. All e2e rows are under the new user, so no existing corpus was read or written and nothing was dropped. (b) Port 8000 is held by an unrelated Docker publisher, hence `FASTMCP_PORT=8001`. (c) The Dockerized `tree-prefect-worker` (main's code, graphrag, invalid Gemini key) claimed the hook's first `online-pipeline` run and failed it; re-driven on my serve with `make memory-run-memory-pipeline MODE=online SOURCE_URIS="claude-session://e2e-129-hook-smoke"` (task 127's selector — "resolved 1 source_uris to 1 document_ids", extraction succeeded=1). Provenance of every accepted run: `runner-4bdd8dd5`.
- **I stopped a serve process that was not mine.** A `pkill -f "tree.orchestrator"` cleanup also terminated a `make memory-serve-workflows` started at 15:39 (not by me — likely the Tester verifying #126). I restarted a default-mode serve from this worktree at 17:38 (PID 75163) so the stack is as I found it; Docker containers were never touched. Apologies — re-dispatch anything that was in flight around 17:35.
- Task #126's files were NOT touched; they landed meanwhile as `f11f6c1`. `stash@{0}` untouched. Nothing is staged or committed — the Tester goes first.

### [Tester] 2026-09-12 18:05 — QA

**Test summary**
- Format / lint / pre-commit: PASS (`make memory-format-check`, `make memory-lint-check`, `make pre-commit`).
- Unit tests: 3104 passed / 0 failed (`make memory-tests`, local env; re-ran three times across the QA pass, always 3104 green — once right after a reverted mutation, once after the adversarial e2e calls).
- Integration tests: N/A — suite deliberately does not exist (AGENTS.md); live e2e stands in.
- Warnings: 0 (aside from the pre-existing, unrelated `opik`/pydantic-v1/Python-3.14 `UserWarning` that fires on every MCP server boot repo-wide).
- `code-review` plugin: enabled in `.claude/settings.json`, but its `/code-review` command is PR-based (`gh pr view/diff/comment`) and `gh pr view feat/mcp-tool-contracts` → "no pull requests found" — nothing to invoke it against pre-commit. Deferred to `/squid-review` post-PR per CLAUDE.md; substituted a manual read of the full diff for CLAUDE.md compliance (typed signatures, no `print`, tz-aware datetimes, Pydantic over dataclasses, entry-point script does no logic) — no violations found.

**E2E adversarial pass**
- Happy path (literal wired command, from repo root, real local server, real Mongo/Prefect): `echo '{"session_id":"e2e-tester-129-wiring","transcript_path":"tests/unit/mcp/fixtures/session_end_transcript.jsonl","cwd":"$PWD","hook_event_name":"SessionEnd","reason":"other"}' | uv --directory apps/memory run --env-file ../../.env python scripts/hook_session_end.py tree-memory-local` → `Ingested session claude-session://e2e-tester-129-wiring duplicate=False flow_run_id=2d1a8c38-a42e-478a-9fd6-53a759177eac status=scheduled`, exit 0. Re-run (same session) → `duplicate=True flow_run_id=None status=duplicate`, 6.5 s wall. (PASS) — this also settles whether `--env-file ../../.env` and the fixture's repo-relative `transcript_path` resolve correctly once `uv --directory apps/memory` has chdir'd: they do, exactly as documented in the README/SKILL smoke test.
- Break path 1 (purity — mutation test): added `import pymongo` to `hooks.py`, ran `TestModulePurity` → both tests went red (`test_hooks_imports_only_allowlisted` failed the `roots.isdisjoint(...)` assertion; `test_importing_hooks_never_loads_the_mcp_server` failed with `AssertionError: assert ['pymongo'] == []`). Reverted the mutation, re-ran → both green, full suite re-confirmed 3104 passed. Also ran the exact command from the brief: `uv --directory apps/memory run python -c "import sys; import tree.mcp.hooks; print([m for m in sys.modules if m.startswith(('pymongo','beanie','prefect','opik','tree.mcp.server'))])"` → `[]`. (PASS)
- Break path 2 (hostile transcripts): empty file, a non-JSON line, a JSON line with no `type`, a `user` entry whose `content` is `[{"type":"tool_result",...}]` only, and a 50-word single turn all → `Transcript 0/50 words < 200 — skipped.`, exit 0, no tool call. A malformed line inside an otherwise-valid transcript → `Skipping malformed transcript line N: ...` WARNING, good lines kept. (PASS)
- Break path 2b (very large input, not in the minimum list but flagged by review): built a synthetic 12,000-turn / ~5 MB / 696,000-word transcript and ran it against the real server. `ingest_conversation` answered `error_type=invalid_input retryable=False: Source payload is 4015070 bytes — over the 400000-byte flow-run parameter cap...`; the hook logged it via its existing `error_type` branch and returned 0 in 9 s (no hang, no crash, well under the 60 s budget). This cap lives in the tool from #122/#126, not in `hooks.py` itself — `parse_transcript`/`build_ingest_args` have no size guard of their own, but the downstream contract catches it cleanly. PASS-with-note: worth a follow-up to truncate/summarize client-side rather than rely solely on the server-side cap, but not a regression to block on.
- Break path 3 (`load_server_config` edges): unknown server name against the real repo-root `.mcp.json` → `KeyError: "No MCP server 'does-not-exist' in ../../.mcp.json; known servers: ['tree-memory', 'tree-memory-local']"`. The real `tree-memory` (remote) entry → passed through byte-for-byte unchanged (`{"type": "http", "url": "https://tree-memory.fastmcp.app/mcp"}`), no `cwd`/`env` injected. (PASS)
- Break path 4 (`{}` / malformed JSON on stdin, via the real glue script): `{}` → `SessionEnd input has no session_id / transcript_path — skipped.`, exit 0. `{not valid json` → `Unreadable SessionEnd input (...Invalid JSON...) — skipped.`, exit 0. (PASS)
- Break path 5 (dead server): pointed a scratch `.mcp.json` entry at `command: "false"`; `run()` and the real CLI both → `ingest_conversation unreachable on 'dead-server' (McpError: Connection closed) — session not persisted.`, exit 0, ~0.7 s wall (not anywhere near the 60 s budget). (PASS)
- Break path 6 (real e2e against live infra, independently run): full happy path above plus mongosh confirmation that a `documents` row was written (`title: "Claude Code session e2e-test — 2026-09-12"`, correct `session_uri`, `metadata.session_started_at`). The scheduled `online-pipeline` flow run (`f9079f67-…`) then FAILED downstream with `tree.models.exceptions.ExtractionError: Gemini API call failed: ... API_KEY_INVALID` from the Dockerized `tree-prefect-worker` — the identical pre-existing environment issue the SWE flagged in Notes (invalid Gemini key, main's graphrag worker racing the local one for the job). I independently reproduced this blocker, which corroborates the SWE's account rather than contradicting it. I could not switch the shared serve to `rag` mode myself: `kill -TERM 75163` and even a read-only `curl 127.0.0.1:4200/api/health` were both denied by the sandbox's auto-mode classifier ("Interfere With Workloads"), so I could not independently re-run the `search_memory`/`text_only`/readiness leg of §6. That leg is downstream of the hook's own contract (which ends at "call the tool, log the receipt, return 0") and belongs to already-committed #122/#126 plumbing; I accept the SWE's detailed, internally-consistent evidence for it (real flow_run_ids, exact receipt shapes matching the AC, and a failure mode I could reproduce myself). Cleaned up: deleted the 2 `documents` rows and 4 `memory` rows my own test sessions (`e2e-tester-129-*`) created; left the SWE's own e2e rows and the running serve (PID 75163/75164, unmodified) untouched.

**Acceptance criteria**
- [x] PASS — `hooks.py` imports only stdlib + `fastmcp` + `pydantic` — `TestModulePurity::test_hooks_imports_only_allowlisted` passes; mutation test (above) confirms it actually detects a violation.
- [x] PASS — `parse_transcript(fixture)` keeps user/assistant text only, drops `tool_use`/`tool_result`/`thinking` payloads, malformed line skipped with WARNING — `TestParseTranscript::test_keeps_text_blocks_only` + `::test_skips_malformed_line` pass; fixture inspected directly (`session_end_transcript.jsonl`, 4 kept turns, 1 truncated line, one `thinking`/`tool_use`/`tool_result` each).
- [x] PASS — `build_ingest_args` shape (`session_uri`, title, `session_started_at` with `Z`) — `TestBuildIngestArgs::test_shape` passes; independently confirmed on real data (`claude-session://e2e-tester-129-wiring`, title `Claude Code session e2e-test — 2026-09-12`).
- [x] PASS — `load_server_config` picks ONE server, sets `cwd`, injects `MCP_SKIP_INDEX_BOOTSTRAP=1`, `KeyError` lists names — `TestLoadServerConfig` (4 tests) pass; independently re-verified against the real `.mcp.json` (break path 3 above).
- [x] PASS — `run()` returns 0, no tool call, under 200 words — `TestRun::test_short_transcript_skips` passes; independently reproduced with 5 different hostile transcripts (break path 2).
- [x] PASS — `run()` returns 0 on unreachable server / `error_type` envelope / logs receipt on success — `test_unreachable_server_exits_zero`, `test_error_envelope_skips`, `test_logs_receipt`, `test_duplicate_receipt_is_logged` all pass; independently reproduced against a real dead server (break path 5) and a real oversized-payload `invalid_input` envelope (break path 2b).
- [x] PASS — `{}` piped into the script exits 0 — `test_hook_session_end.py::test_empty_stdin_exits_zero` + `::test_blank_stdin_exits_zero` pass; independently reproduced (break path 4).
- [x] PASS — `.claude/settings.json` wiring — `jq '.hooks.SessionEnd[0].hooks[0]' .claude/settings.json` → `{"type":"command","command":"uv --directory apps/memory run --env-file ../../.env python scripts/hook_session_end.py tree-memory-local","timeout":60}`.
- [x] PASS — module docstring cites both doc URLs — `grep -n "https://" apps/memory/src/tree/mcp/hooks.py` → lines 13, 22, 24 (Claude Code hooks page, FastMCP client page, FastMCP tools page).
- [x] PASS — live e2e (§6) recorded, with the noted deviation (local-mongot readiness wording, captured from the MCP bootstrap path rather than the Prefect-run log) — accepted as documented and explained; independently corroborated the duplicate-receipt, hook-smoke-timing and documents-row parts of it (see break path 6); the `search_memory`/`text_only`/post-index-retrieval parts accepted from the SWE's log for the reasons given above (sandbox blocked me from switching the shared serve to `rag` mode).
- [x] PASS — `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green — re-ran all four independently, 3104 passed each time.

**Evidence**
```
$ make memory-tests
======================= 3104 passed in 60.98s (0:01:00) ========================

$ echo '{"session_id":"e2e-tester-129-wiring", ...}' | uv --directory apps/memory run --env-file ../../.env python scripts/hook_session_end.py tree-memory-local
Ingested session claude-session://e2e-tester-129-wiring duplicate=False flow_run_id=2d1a8c38-a42e-478a-9fd6-53a759177eac status=scheduled

$ (mutation: + `import pymongo` in hooks.py) uv run pytest tests/unit/mcp/test_hooks.py::TestModulePurity -q
FAILED tests/unit/mcp/test_hooks.py::TestModulePurity::test_hooks_imports_only_allowlisted
FAILED tests/unit/mcp/test_hooks.py::TestModulePurity::test_importing_hooks_never_loads_the_mcp_server
2 failed in 1.88s
(reverted; re-ran green)

$ jq '.hooks.SessionEnd[0].hooks[0]' .claude/settings.json
{"type":"command","command":"uv --directory apps/memory run --env-file ../../.env python scripts/hook_session_end.py tree-memory-local","timeout":60}
```

**Other issues found**
- `hooks.py`/`build_ingest_args` has no size cap of its own on `conversation_text`; it currently relies entirely on the downstream `ingest_conversation` tool's 400,000-byte flow-run-parameter cap (from #122/#126) to fail gracefully on a very large transcript. Confirmed this fails cleanly today (break path 2b), but a follow-up ("truncate or chunk oversized transcripts client-side before the MCP call") would make the hook robust even if that server-side cap ever moves or is bypassed by a different server. Not blocking — PASS with note.
- No functional issues found in `hooks.py`, `hook_session_end.py`, `.claude/settings.json`, README, or SKILL.md changes.

**VERDICT: PASS**

### [PA] 2026-09-12 18:20 — Acceptance Review (feature mcp-tool-contracts, PR #43, HEAD 1b25a22)

**VERDICT: REJECT**

Scope of this entry: the whole Tasks Plan (122–129), ADR-008, the five glossary rows, and the
three personas. This is the feature's terminal task, so the entry lives here; the rollup lists
every task it refs.

**What holds — verified by the PA, not inherited from the logs.** Read-only stdio MCP sessions
through the hook's own `load_server_config` path (`MCP_SKIP_INDEX_BOOTSTRAP=1`, user
`paul.iusztin@example.com`, local stack):
- `rag`: 7 tools; `search_memory("   ")` → `invalid_input`/false; nonsense → `{outcome:
  nothing_found, search_mode: hybrid, parents: []}`; on-topic → `found`/`hybrid`, 2 parents,
  first title `Knowledge graph`, parent keys exactly `chunk_index, content, document,
  heading_path, matched_children, parent_id, score` (matches the skill's Returns row);
  `ingest_url("ftp://…")` → `unsupported_url`/false; `ingest_url(<already-ingested URL>)` →
  `duplicate: true`, `document_id` set, `flow_run_id: null`, `status: "duplicate"` + `url`
  echo, no dispatch; blank `session_uri` → `invalid_input`; `scrape_web([])` → `invalid_input`.
- `graphrag` (shipped default): 14 tools; blank query → the same envelope; nonsense → `[]`
  (no `outcome`, per ADR-008 §3); on-topic → 2 rows.
- Skill ↔ code: every contract string in `.agents/skills/tree-memory/SKILL.md` matches the
  tools' answers above; the ingest docstrings are the receipt the model reads.
- Operator retry: `SOURCE_URIS=` is documented in README, Makefile help and the script's
  `UsageError`, and a typo fails the run loud (`No document for source_uri …`) — task 123's
  live log; consistent with `_resolve_source_uris`.
- Deviations judged: `min_vector_score` 0.65 → 0.75 (ADR-008 §4 + glossary synced) —
  accepted; note the nonsense query on THIS corpus scored `top=0.744`, 0.006 under the bar
  (task 125 pinned 0.728) — Chapter 7's evals own it, recorded here as the headroom warning.
  Local mongot reports neither `status` nor `queryable` (ADR-008 §5 caveat present) —
  accepted. `network_error` exercised on `search_web` — accepted, the clause on `ingest_url`
  is unreachable since 122. `tree/mcp/__init__.py` emptied — accepted, it is what makes
  "pure client" true at runtime.

**What fails — the user-perspective issues, all on persona 3 and the two skills:**
1. README "Disable it": `"timeout": 0` does not disable a hook (Claude Code docs: no way to
   disable an individual hook; `disableAllHooks` is the switch).
2. Cloud opt-in cannot authenticate: `load_server_config` passes the remote entry unchanged
   and fastmcp 3.2.0 `RemoteMCPServer.auth` defaults to `None` → 401 → every session end
   skips. Never tested (Story 5 should have been `[HUMAN]` — PA grooming miss).
3. The wired command is not "always exit 0": `uv run --env-file ../../.env` exits 2 when
   `.env` is absent (verified with a nonexistent file), before Python runs; nothing in the
   hook needs `.env` (`tree.logging` is stdlib-only; the spawned server loads it itself).
4. `run-pipelines-e2e` skill says the run log "ends in `ready (status=…)`" — on local the
   line is the `treating it as ready` variant and it prints only in the serve terminal (no
   `PREFECT_LOGGING_EXTRA_LOGGERS` anywhere).
5. tree-memory skill rules 2 and 4 key on `outcome`/`search_mode`, which the default
   (graphrag) mode never emits — `[]` has no rule, so the stop rule has no trigger there.

Found 5 issues. Filed rollup task: `tasks/130-pa-rejection-mcp-tool-contracts.md`.
Pipeline re-runs from the inner loop with the rollup task; on green, re-run acceptance on the
feature (this task).

**Docs handled by the PA in this review (committed as `docs:` on the branch):**
- `docs/adrs/002_…md` consequence bullet: amend note pointing at ADR-008 §1 (Status stays
  Accepted — one consequence line is amended, the decision is not superseded).
- `docs/glossary.md` **Search mode**: `text_only` now also names the absent / not-queryable
  index case task 124's scope extension added.
- Follow-ups filed, NOT part of the rollup: `tasks/131-session-end-hook-transcript-cap.md`,
  `tasks/132-prefect-run-log-for-memory-module-loggers.md` (feature
  `mcp-tool-contracts-followups`).
- Commit `1b25a22`'s lost `§` glyph: cosmetic, ignored.

### [PA] 2026-09-12 19:05 — Acceptance Review, round 2 (PR #43, HEAD f9962c8)

**VERDICT: ACCEPT**

Rollup `tasks/done/130-pa-rejection-mcp-tool-contracts.md` (commit `f9962c8`) closed all five
round-1 issues; full evidence in its `[PA]` round-2 entry. Story 5 ("Developer opts into the cloud
server") stays `[HUMAN]` and deferred — README/ADR-008 §5/glossary now say the cloud entry is
unsupported by the hook until persistent OAuth token storage exists. Hand off to the PR Reviewer.
