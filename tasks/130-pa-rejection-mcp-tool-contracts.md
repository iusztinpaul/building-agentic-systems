---
id: 130-pa-rejection-mcp-tool-contracts
status: pending
feature: mcp-tool-contracts
---

# [PA rejection] mcp-tool-contracts — Session-end hook README/wiring honesty, e2e-skill readiness line, graphrag stop rule

Tags: `rollup`, `pa-rejection`, `mcp`, `hooks`, `docs`, `skill`
Refs: `tasks/done/122-…129` (the feature's Tasks Plan), ADR-008, PR #43

## Scope

The feature PASSED automated QA on every task (122–129) and every tool contract holds on the
real server — verified by the PA over read-only stdio MCP sessions in BOTH modes (7 vs 14 tools,
`invalid_input` envelope on a blank query in both, `nothing_found` / `found` + `search_mode` in
rag, `duplicate: true` with no dispatch on an already-ingested URL). It failed the
user-perspective review on the **Session-end hook**'s operator-facing surface and on two
skill lines: three README/wiring claims are not true as written, the e2e skill points at a
log line that never appears on the local stack, and the tree-memory skill's stop rule has no
trigger in the shipped default mode. The SWE must fix every issue below in a single
coordinated pass, then hand back to the Tester (full pipeline re-runs from QA).

Nothing here changes an ADR-008 contract. ADR-008 §5's "opt-in cloud" is the only design
point at stake (issue 2), and the fix stays inside the hook.

## Acceptance Criteria

- [ ] Issue 1: `apps/memory/README.md` "Disable it" names `disableAllHooks` (in a settings file, or `claude --settings '{"disableAllHooks": true}'` for one run) and no longer claims `"timeout": 0` disables the hook — `grep -c '"timeout": 0' apps/memory/README.md` → 0, `grep -c disableAllHooks apps/memory/README.md` ≥ 1.
- [ ] Issue 2 (preferred): `tree.mcp.hooks.load_server_config` returns a remote (no `command`) entry with `auth == "oauth"` when the entry sets no `auth`, and leaves stdio entries and an explicit `auth` untouched — `tests/unit/mcp/test_hooks.py::TestLoadServerConfig::test_remote_entry_defaults_to_oauth`, `::test_explicit_auth_is_kept`. README's cloud paragraph documents the ONE-TIME login with the exact FastMCP command that populates FastMCP's own OAuth token storage (Claude Code's OAuth tokens are NOT shared with it) and says a session end with no cached token logs a skip after the 30 s call timeout.
- [ ] Issue 2 [HUMAN]: after the documented one-time login, the README smoke test with `tree-memory` as the argument logs `Ingested session … ` (or `duplicate=True` on rerun). If this cannot be verified in this pass, ship the fallback instead: delete the "Cloud server (opt-in)" paragraph, state in one sentence that the hook targets the local server only for now, and log the reason in this task — the README must not describe a path nobody has walked.
- [ ] Issue 3: the hook command carries no `--env-file` — `grep -c "env-file" .claude/settings.json` → 0; the README JSON snippet, the README smoke test and `.agents/skills/run-pipelines-e2e/SKILL.md`'s smoke test quote the SAME command as `.claude/settings.json` (`grep -n "hook_session_end.py" apps/memory/README.md .agents/skills/run-pipelines-e2e/SKILL.md .claude/settings.json` shows one command string); the smoke test with the new command logs a receipt (`duplicate=…`) — proving the spawned server still loads `.env` through its own `.mcp.json` args.
- [ ] Issue 3: `echo '{}' | uv --directory apps/memory run python scripts/hook_session_end.py tree-memory-local` exits 0 from the repo root — `tests/unit/scripts/test_hook_session_end.py::test_empty_stdin_exits_zero` still green, and the command run once by hand.
- [ ] Issue 4: `.agents/skills/run-pipelines-e2e/SKILL.md` no longer says the run log "ends in `ready (status=…)`"; it quotes the LOCAL line verbatim (`reports neither 'status' nor 'queryable' (local mongot); treating it as ready`), says it prints in the `make memory-serve-workflows` terminal (module logger — not in the streamed flow-run log until `tasks/132`), and names `ready (status=READY)` as the Atlas wording — `grep -n "treating it as ready" .agents/skills/run-pipelines-e2e/SKILL.md` matches.
- [ ] Issue 5: `.agents/skills/tree-memory/SKILL.md` Search loop rule 2 says what an empty answer means in `graphrag` (`[]` from `search_memory` / `query_memory`, `No results found.` from `deep_search_memory` — there is no `outcome`), and rule 4 says `search_mode` is rag-only; `wc -l` ≤ 153; every task-128 grep AC still holds (`grep -c "PROACTIVE\|background agent"` → 0, `grep -c '"error"'` → 0, tool table 13 rows, `disable-model-invocation: true`).
- [ ] `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green (env `local`).
- [ ] Tester re-runs full QA suite and PASSES.
- [ ] PA re-runs acceptance review on the feature and ACCEPTS.

## Issues (detail)

### 1. README "Disable it" names a mechanism that does not exist — `apps/memory/README.md:492-493`
- **What the user experiences (wrong):** a developer who wants one session NOT persisted sets `"timeout": 0` as told, and the hook still runs (or is killed mid-flight and prints an error). Claude Code's hooks reference (fetched 2026-09-12): *"There is no way to disable an individual hook while keeping it in the configuration"*; the only switch is `disableAllHooks`, and hooks MERGE across settings levels, so a `.claude/settings.local.json` cannot remove the project hook either.
- **What good UX implies (right):** the two honest options — `"disableAllHooks": true` in `~/.claude/settings.json` / `.claude/settings.local.json`, or `claude --settings '{"disableAllHooks": true}'` for one run — plus "edit `.claude/settings.json`" for a permanent removal.
- **Suggested fix:** rewrite the paragraph; drop the `timeout` parenthetical.

### 2. The documented cloud opt-in cannot authenticate — `apps/memory/README.md:486-490`, `apps/memory/src/tree/mcp/hooks.py::load_server_config`
- **What the user experiences (wrong):** the developer changes the argument to `tree-memory`, "completes the OAuth flow once" as told, and EVERY session end logs `ingest_conversation unreachable on 'tree-memory' … — session not persisted.` `load_server_config` passes the remote entry through unchanged, and fastmcp 3.2.0's `RemoteMCPServer.auth` defaults to `None` — the client sends no credentials, so the Horizon server answers 401. Nothing in this repo was ever tested against the cloud entry (129 Story 5 was not marked `[HUMAN]` — PA grooming miss); `fastmcp inspect` is not known to run OAuth, and Claude Code's own OAuth tokens live in Claude Code's store, not in FastMCP's client token storage.
- **What the spec implies (right):** ADR-008 §5 / glossary **Session-end hook**: "Cloud server (`tree-memory`, Horizon OAuth) is opt-in" — opt-in has to work when opted into, or say it does not yet.
- **Suggested fix:** in `load_server_config`, merge `"auth": "oauth"` into an entry without `command` when it sets no `auth` (FastMCP's OAuth client persists tokens per server URL, so a one-time login makes later hook runs non-interactive; an interactive login inside a hook just hits the 30 s call timeout and skips). README: the one-time login command (a FastMCP client with `auth="oauth"` against the URL — the SWE verifies the exact form), verified by a human once. If that cannot be done in this pass, ship the fallback in the AC and remove the paragraph.

### 3. The hook command exits non-zero on a checkout without `.env` — `.claude/settings.json`, `apps/memory/README.md:459,500`, `.agents/skills/run-pipelines-e2e/SKILL.md:64`
- **What the user experiences (wrong):** on a fresh clone (or any checkout where `.env` is absent), every session end prints `error: No environment file found at: …/.env` — `uv run --env-file` exits 2 before Python starts (verified: `uv run --env-file /nonexistent.env python -c …` → exit 2). Claude Code shows that stderr to the user on each SessionEnd. The README's headline "the hook always exits 0" is true of `tree.mcp.hooks.run` and false of the wired command.
- **What the spec implies (right):** ADR-008 §5 "always exits 0". Neither `scripts/hook_session_end.py` nor `tree.mcp.hooks` reads `.env` (`tree.logging` is stdlib-only); the spawned server loads `../../.env` through its own `.mcp.json` args.
- **Suggested fix:** drop `--env-file ../../.env` from the hook command everywhere it is quoted (settings.json, README ×2, e2e skill). Then a missing `.env` fails inside the spawned server → `unreachable … — session not persisted.` → exit 0, as designed.

### 4. e2e skill points at a log line that never appears locally — `.agents/skills/run-pipelines-e2e/SKILL.md:58`
- **What the user experiences (wrong):** an agent or developer following step 3 restores the index and greps the streamed run log for `ready (status=…)`; on the local stack the line is `Vector search index 'vector_index' reports neither 'status' nor 'queryable' (local mongot); treating it as ready`, and it is emitted by `tree.memory.rag.indexing`'s module logger, which Prefect does not forward (no `PREFECT_LOGGING_EXTRA_LOGGERS` anywhere; task 129's SWE verified the absence) — it prints only in the `make memory-serve-workflows` terminal.
- **What good UX implies (right):** name the line the operator will actually see, and where.
- **Suggested fix:** one sentence; `tasks/132` moves the line into the run log later and will edit this again.

### 5. tree-memory skill: the stop rule has no trigger in the default mode — `.agents/skills/tree-memory/SKILL.md:83,89,128`
- **What the user experiences (wrong):** the shipped default is `memory.mode: graphrag` (`configs/default.yaml:22`). There, `search_memory` answers `[]` for no matches (verified live: nonsense query → `list of 0`) and never emits `outcome` or `search_mode` (ADR-008 §3, by design). Rules 2 and 4 key on those two fields, so a model in graphrag reads `[]`, finds no rule for it, and can fall through to `search_web` — the exact behaviour rule 2 exists to stop.
- **What the spec implies (right):** the Search loop must be checkable in both modes without changing the graphrag contract.
- **Suggested fix:** one clause in rule 2 ("in `graphrag` there is no `outcome`: `[]` — or `deep_search_memory`'s `No results found.` — is `nothing_found` for this rule") and one in rule 4 (`search_mode` is rag-only; graphrag has no caveat to print). Removing beats adding — stay within 153 lines.

## User Stories

(Inherit from tasks 122–129 — no new stories. Re-verify each one passes after the fix; 129's
"Developer opts into the cloud server" is `[HUMAN]` from now on.)

---

Refs: `tasks/done/129-session-end-hook-mcp-client-and-e2e.md` (issues 1–4), `tasks/done/128-tree-memory-skill-search-loop.md` (issue 5)

## Log

### [PA] 2026-09-12 18:20 — Rollup filed from acceptance review

Evidence for every issue is in the PA acceptance-review entry on
`tasks/done/129-session-end-hook-mcp-client-and-e2e.md`. Follow-ups that are NOT part of this
rollup: `tasks/131` (client-side transcript cap) and `tasks/132` (module loggers into the
Prefect run log).
