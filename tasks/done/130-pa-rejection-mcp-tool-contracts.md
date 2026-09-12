---
id: 130-pa-rejection-mcp-tool-contracts
status: done
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

- [x] Issue 1: `apps/memory/README.md` "Disable it" names `disableAllHooks` (in a settings file, or `claude --settings '{"disableAllHooks": true}'` for one run) and no longer claims `"timeout": 0` disables the hook — `grep -c '"timeout": 0' apps/memory/README.md` → 0, `grep -c disableAllHooks apps/memory/README.md` ≥ 1.
- [ ] ~~Issue 2 (preferred): `tree.mcp.hooks.load_server_config` returns a remote (no `command`) entry with `auth == "oauth"` when the entry sets no `auth`, and leaves stdio entries and an explicit `auth` untouched — `tests/unit/mcp/test_hooks.py::TestLoadServerConfig::test_remote_entry_defaults_to_oauth`, `::test_explicit_auth_is_kept`. README's cloud paragraph documents the ONE-TIME login with the exact FastMCP command that populates FastMCP's own OAuth token storage (Claude Code's OAuth tokens are NOT shared with it) and says a session end with no cached token logs a skip after the 30 s call timeout.~~ **SUPERSEDED by the Issue 2 fallback** (coordinator decision, 2026-09-12): fastmcp 3.2.0 keeps OAuth tokens in a `MemoryStore` (`fastmcp/client/auth/oauth.py:275`; https://gofastmcp.com/clients/auth/oauth#token-storage), so `auth: "oauth"` would open a browser at EVERY session end instead of the one 401 → skip line — remote entries pass through unchanged and the README ships the fallback wording.
- [ ] Issue 2 [HUMAN]: after the documented one-time login, the README smoke test with `tree-memory` as the argument logs `Ingested session … ` (or `duplicate=True` on rerun). If this cannot be verified in this pass, ship the fallback instead: delete the "Cloud server (opt-in)" paragraph, state in one sentence that the hook targets the local server only for now, and log the reason in this task — the README must not describe a path nobody has walked. — **still unchecked**, same reason: with tokens in fastmcp's `MemoryStore` there is no login to walk, so the cloud smoke test cannot be verified by anyone yet.
- [x] Issue 3: the hook command carries no `--env-file` — `grep -c "env-file" .claude/settings.json` → 0; the README JSON snippet, the README smoke test and `.agents/skills/run-pipelines-e2e/SKILL.md`'s smoke test quote the SAME command as `.claude/settings.json` (`grep -n "hook_session_end.py" apps/memory/README.md .agents/skills/run-pipelines-e2e/SKILL.md .claude/settings.json` shows one command string); the smoke test with the new command logs a receipt (`duplicate=…`) — proving the spawned server still loads `.env` through its own `.mcp.json` args.
- [x] Issue 3: `echo '{}' | uv --directory apps/memory run python scripts/hook_session_end.py tree-memory-local` exits 0 from the repo root — `tests/unit/scripts/test_hook_session_end.py::test_empty_stdin_exits_zero` still green, and the command run once by hand.
- [x] Issue 4: `.agents/skills/run-pipelines-e2e/SKILL.md` no longer says the run log "ends in `ready (status=…)`"; it quotes the LOCAL line verbatim (`reports neither 'status' nor 'queryable' (local mongot); treating it as ready`), says it prints in the `make memory-serve-workflows` terminal (module logger — not in the streamed flow-run log until `tasks/132`), and names `ready (status=READY)` as the Atlas wording — `grep -n "treating it as ready" .agents/skills/run-pipelines-e2e/SKILL.md` matches.
- [x] Issue 5: `.agents/skills/tree-memory/SKILL.md` Search loop rule 2 says what an empty answer means in `graphrag` (`[]` from `search_memory` / `query_memory`, `No results found.` from `deep_search_memory` — there is no `outcome`), and rule 4 says `search_mode` is rag-only; `wc -l` ≤ 153; every task-128 grep AC still holds (`grep -c "PROACTIVE\|background agent"` → 0, `grep -c '"error"'` → 0, tool table 13 rows, `disable-model-invocation: true`).
- [x] `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green (env `local`).
- [x] Tester re-runs full QA suite and PASSES.
- [x] PA re-runs acceptance review on the feature and ACCEPTS.

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

### [SWE] 2026-09-12 18:40 — Implementation

**Files modified**
- `apps/memory/src/tree/mcp/hooks.py` — `load_server_config` merges `auth: "oauth"` into a remote entry that sets no `auth`; docstring records the fastmcp 3.2.0 token-storage caveat.
- `apps/memory/tests/unit/mcp/test_hooks.py` — `test_remote_entry_defaults_to_oauth` (replaces `test_remote_entry_passes_through_untouched`), `test_explicit_auth_is_kept`, `test_stdio_entry_gets_no_auth`.
- `apps/memory/tests/unit/scripts/test_hook_session_end.py` — `test_runs_with_no_env_file_variables` (subprocess, env = PATH + HOME only) and `test_wired_hook_command_carries_no_env_file` (pins the `.claude/settings.json` command string).
- `.claude/settings.json` — dropped `--env-file ../../.env` from the SessionEnd command.
- `apps/memory/README.md` — "Disable it" rewritten around `disableAllHooks`; "Cloud server (opt-in)" replaced by the honest "Local server only, for now"; both hook commands lost `--env-file`.
- `.agents/skills/run-pipelines-e2e/SKILL.md` — readiness wording (local mongot line + where it prints + the Atlas wording); smoke command lost `--env-file`.
- `.agents/skills/tree-memory/SKILL.md` — one clause in Search-loop rule 2 (graphrag has no `outcome`: `[]` / `No results found.` IS `nothing_found`) and one in rule 4 (`search_mode` is rag-only). Still 150 lines (clauses appended in place, no new lines).

**Tests**
- Unit: 3108 passing, 0 failing (`make memory-tests`, env `local`) — suite 3104 → 3108. Four tests added, one renamed: `test_remote_entry_defaults_to_oauth` (renamed from `test_remote_entry_passes_through_untouched`, whose exact-equality assertion the `auth` merge invalidated), `test_explicit_auth_is_kept`, `test_stdio_entry_gets_no_auth`, `test_runs_with_no_env_file_variables`, `test_wired_hook_command_carries_no_env_file`.
- Integration: N/A — the repo has no integration suite by design; e2e is the live hook run below.

**Acceptance criteria**
- [x] Issue 1 — `grep -c '"timeout": 0' apps/memory/README.md` → 0, `grep -c disableAllHooks` → 2. Mechanism taken from the primary doc, not guessed: https://docs.claude.com/en/docs/claude-code/hooks — *"There is no way to disable an individual hook while keeping it in the configuration"*, `disableAllHooks` in a settings file, `--settings '{"disableAllHooks": true}'` for one run, and hook entries MERGE across settings levels.
- [ ] Issue 2 (preferred) — **PARTIAL, deliberately left unchecked.** The code half is done and green (`::test_remote_entry_defaults_to_oauth`, `::test_explicit_auth_is_kept`). The README half ("document the ONE-TIME login … that populates FastMCP's own OAuth token storage") is **impossible as written on the pinned version**: fastmcp 3.2.0 defaults OAuth token storage to `MemoryStore` — `.venv/…/fastmcp/client/auth/oauth.py:275` `token_storage = self._token_storage or MemoryStore()`, with a warning "tokens will be lost when the client restarts"; the docs say the same (https://gofastmcp.com/clients/auth/oauth#token-storage), and the CLI passes the bare string too (`fastmcp/cli/client.py:247`), so `fastmcp list --auth oauth` caches nothing a later process can read. There is therefore no one-time login to document. Shipped the AC's FALLBACK README wording instead.
- [ ] Issue 2 [HUMAN] — **pending, fallback shipped.** README now says the hook targets the local server today, that the remote entry is sent with FastMCP OAuth, and that with no cached token a session end starts a fresh browser login and skips at the 30 s call timeout — unverified until a human walks it. Consequence the PA should weigh: with in-memory tokens, pointing the hook at `tree-memory` now opens a browser at EVERY session end (before this change it got a clean 401 → skip). A follow-up task for persistent token storage (`py-key-value-aio[disk]` + encryption) is the real fix; I did not add the dependency or an env knob — that is an architectural fork, not a rollup fix.
- [x] Issue 3 (command) — `grep -c "env-file" .claude/settings.json` → 0; one distinct command string across the three files: `grep -h "hook_session_end.py" apps/memory/README.md .agents/skills/run-pipelines-e2e/SKILL.md .claude/settings.json | grep -o 'uv --directory[^"]*' | sort -u` → `uv --directory apps/memory run python scripts/hook_session_end.py tree-memory-local`. `.mcp.json` KEEPS its own `--env-file ../../.env` — that is how the spawned server still gets `.env`, proven by the live receipt below.
- [x] Issue 3 (exit 0) — `echo '{}' | uv --directory apps/memory run python scripts/hook_session_end.py tree-memory-local` from the repo root → exit 0, `SessionEnd input has no session_id / transcript_path — skipped.`; `::test_empty_stdin_exits_zero` green; new `::test_runs_with_no_env_file_variables` runs the script with an environment carrying nothing from `.env` and asserts exit 0 (grep-verified first: no `os.environ` / `getenv` / `dotenv` in `tree/logging.py`, `scripts/hook_session_end.py`, `tree/mcp/hooks.py`).
- [x] Issue 4 — `grep -n "treating it as ready" .agents/skills/run-pipelines-e2e/SKILL.md` matches; the local line is quoted byte-for-byte from `tree/memory/rag/indexing.py:566-567` and `ready (status=%s)` (line 573) is named as the Atlas wording.
- [x] Issue 5 — rule 2 and rule 4 extended in place, with both literals read off the code, not off the AC text: graphrag `search_memory` / `query_memory` return `_serialize(results)` = `json_util.dumps([], indent=2)` → `[]` (`tree/mcp/graph_tools.py:74-78, 246`), and `deep_search_memory` returns the bare string `No results found.` (`tree/mcp/graph_tools.py:367`, trailing period included). `wc -l` → 150 (≤ 153); task-128 greps still hold: `PROACTIVE\|background agent` → 0, `'"error"'` → 0, `disable-model-invocation: true` → 1, tool table untouched.
- [x] `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green (env `local`).

**Evidence**

```
$ make memory-tests
======================= 3108 passed in 66.91s (0:01:06) ========================

$ make pre-commit
prettier / ruff check / ruff format / biome check (harness) .............. Passed

# Live check — the wired command EXACTLY as it reads in .claude/settings.json, fresh session_id
$ echo '{"session_id":"rollup130-smoke-1","transcript_path":"tests/unit/mcp/fixtures/session_end_transcript.jsonl","cwd":"'"$PWD"'",...}' \
    | uv --directory apps/memory run python scripts/hook_session_end.py tree-memory-local
MCP_SKIP_INDEX_BOOTSTRAP set — skipping ensure_indexes + vector-index assertion (fast serverless boot).
Ingested session claude-session://rollup130-smoke-1 duplicate=False flow_run_id=a88e4bff-1db9-4d03-b151-313221c7e78c status=scheduled
EXIT=0
# rerun:
Ingested session claude-session://rollup130-smoke-1 duplicate=True flow_run_id=None status=duplicate
EXIT=0
```

**Notes**
- `docs/adrs/008_mcp_tool_contract.md` §5 and `docs/glossary.md`'s Session-end hook row both still read the cloud server as a working opt-in, which the fastmcp 3.2.0 token-storage finding contradicts. I am read-only on both — PA's call.
- `docs/glossary.md`'s Session-end hook row still reads "Cloud server (`tree-memory`, Horizon OAuth) is opt-in", which the fastmcp 3.2.0 finding contradicts. I am read-only there — PA's call.
- NOT RUN: the cloud (`tree-memory`) path. No credentials, and no persistable login exists on the pinned version (see Issue 2 above).

### [SWE] 2026-09-12 19:05 — Fixes (coordinator: revert the `auth: "oauth"` default)

**What changed**
- `apps/memory/src/tree/mcp/hooks.py` — reverted the `auth: "oauth"` merge; remote entries pass through UNCHANGED again. The docstring now records WHY the obvious-looking fix is the wrong one: with fastmcp 3.2.0's `MemoryStore` default, `auth: "oauth"` trades one silent 401 → skip for a browser popup plus a 30 s timeout at every session end.
- `apps/memory/tests/unit/mcp/test_hooks.py` — restored `TestLoadServerConfig::test_remote_entry_passes_through_untouched` (with the reason in-comment); dropped `test_remote_entry_defaults_to_oauth` and `test_explicit_auth_is_kept`; dropped `test_stdio_entry_gets_no_auth` too — with nothing injecting `auth` anywhere it asserted a vacuous truth, and `test_picks_one_server_and_pins_cwd_and_bootstrap_flag` already pins the stdio entry.
- `apps/memory/README.md` — "Local server only, for now" kept, plus the required sentence: pointing the hook at `tree-memory` today fails auth (401) and is skipped, and persistent OAuth token storage is the prerequisite (follow-up).
- Task file — the Issue 2 "preferred" AC is struck through as SUPERSEDED by the fallback, with the one-line reason (fastmcp `MemoryStore`, https://gofastmcp.com/clients/auth/oauth#token-storage); the Issue 2 `[HUMAN]` AC stays unchecked with the same reason.
- Untouched, as instructed: `docs/adrs/`, `docs/glossary.md`. Issues 1, 3, 4 and 5 are unchanged from the entry above.

**Tests**
- Unit: 3105 passing, 0 failing (`make memory-tests`, env `local`) — 3104 before this rollup, +1 net: `test_runs_with_no_env_file_variables` and `test_wired_hook_command_carries_no_env_file` added, three `auth` tests removed, one restored.
- `make memory-format-check && make memory-lint-check && make pre-commit` green.

**Notes**
- The live hook check from the entry above still stands: it exercised `tree-memory-local`, which this revert does not touch. Re-ran `make memory-tests` and the QA loop after the revert; the hook code path for the local server is byte-identical to the verified run.
- Follow-up worth filing: persistent, encrypted OAuth token storage for the hook's FastMCP client (`py-key-value-aio[disk]` + `FernetEncryptionWrapper`), which is what would make ADR-008 §5's cloud opt-in real. Not done here — new dependency + token-at-rest policy is an architectural decision, not a rollup fix.
- Flake seen ONCE, not reproduced: the first `make memory-tests` after the revert exited non-zero, output truncated by my `tail`. Four subsequent full runs are green (3106 passed) and the two new tests take 0.55–0.58 s each (nowhere near their 60 s subprocess timeout), so it is not the new code — flagging it rather than hiding it, in case the Tester sees it again.

### [Tester] 2026-09-12 19:40 — QA

**Test summary**
- Format / lint / pre-commit: PASS (`make memory-format-check`, `make memory-lint-check`, `make pre-commit` all green)
- Unit tests: `make memory-tests` run TWICE, env `local` — 3106 passed / 0 failed both times (46.08s, 47.71s). No flake reproduced. `tests/unit/scripts/test_hook_session_end.py` isolated run: 8 passed in 1.44s; `test_runs_with_no_env_file_variables` 0.58s, well inside its 60s subprocess timeout.
- Integration tests: N/A by design (no integration suite; e2e is the live hook runs below).
- Warnings: 0 test-collection warnings; the `pydantic.v1` UserWarning from `opik`'s own import is pre-existing/unrelated.
- `code-review` plugin: enabled in `.claude/settings.json` (`code-review@claude-plugins-official`), but the Tester subagent has no tool surface to invoke a Claude Code slash command — not skipped by choice, just not reachable from here. Substituted a full manual read of every diff hunk (see below); no nested `claude -p` was shelled out.

**E2E adversarial pass**
- Happy path: wired command verbatim (`uv --directory apps/memory run python scripts/hook_session_end.py tree-memory-local`) via `env -i PATH="$PATH" HOME="$HOME"`, fixture transcript, fresh `session_id` → `Ingested session claude-session://tester130-adv-… duplicate=False flow_run_id=… status=scheduled`, exit 0 (PASS). Proves `.env` is not needed by the hook process itself.
- Break path 1 (state edge — flakiness/repeat invocation): ran `make memory-tests` twice back-to-back plus one isolated run of the two new subprocess tests → 3106/3106/8 passed every time, no flake (PASS).
- Break path 2 (failure mode — server can't boot, missing config): scratch `.mcp.json` (absolute `--directory`, `--env-file` and `env` block both deleted) run under `env -i PATH HOME`. Spawned server raised `RuntimeError: MCP server boot: neither --user-id nor TREE_USER_IDENTIFIER is set...`, and the hook logged exactly one WARNING — `ingest_conversation unreachable on 'tree-memory-local' (McpError: Connection closed) — session not persisted.` — exit 0 (PASS). (First attempt used a relative `--directory` and hit a path-resolution error instead of a config error; corrected to an absolute `--directory` so the failure actually isolates the missing-`.env` case the AC is about.)
- Break path 3 (hostile/negative — cloud path, no credentials): wired command with `tree-memory` as the argument (real `.mcp.json`, no scratch) → `ingest_conversation unreachable on 'tree-memory' (HTTPStatusError: Client error '401 Unauthorized' ...) — session not persisted.`, exit 0, total runtime ~1.2s (no browser popup, no 30s timeout) (PASS). This is the exact behaviour the README/glossary/ADR-008 §5 fallback wording now claims — previously unverified by the SWE ("NOT RUN: the cloud path"); now walked and confirmed true.
- Break path 4 (pass-through contract): `load_server_config(mcp_json, "tree-memory", cwd=...)` on the real repo-root `.mcp.json` returns the entry byte-identical to the raw JSON — no `auth`, no `cwd`, no `env` merged (PASS).

**Acceptance criteria**
- [x] PASS — Issue 1 (disableAllHooks, no `"timeout": 0` claim) — `grep -c '"timeout": 0' apps/memory/README.md` → 0; `grep -c disableAllHooks apps/memory/README.md` → 2; wording matches the cited hooks reference (merge-across-levels caveat present).
- [ ] SUPERSEDED (left unchecked, as instructed) — Issue 2 (preferred, `auth: "oauth"` default) — struck through in the task file with the coordinator's fastmcp `MemoryStore` reasoning; code reverted, `test_remote_entry_passes_through_untouched` restored and green.
- [ ] Awaiting human verification (left unchecked, as instructed) — Issue 2 `[HUMAN]` — fallback wording shipped instead; live-verified today that the fallback claim itself (401 → one skip → exit 0, no browser) is TRUE (see break path 3 above), so the shipped README/glossary/ADR text is honest, even though the original `[HUMAN]` AC (post-login smoke test) still has no login path to walk.
- [x] PASS — Issue 3 (no `--env-file` in the wired command) — `grep -c "env-file" .claude/settings.json` → 0; `grep -h "hook_session_end.py" apps/memory/README.md .agents/skills/run-pipelines-e2e/SKILL.md .claude/settings.json | grep -o 'uv --directory[^"'"'"']*' | sort -u` → exactly one string, `uv --directory apps/memory run python scripts/hook_session_end.py tree-memory-local`; live smoke test logs a receipt (`duplicate=False` then `duplicate=True` on rerun, verified separately in the SWE's evidence and re-confirmed live above).
- [x] PASS — Issue 3 (exits 0 on empty stdin) — `echo '{}' | uv --directory apps/memory run python scripts/hook_session_end.py tree-memory-local` → `SessionEnd input has no session_id / transcript_path — skipped.`, exit 0; `test_empty_stdin_exits_zero` green.
- [x] PASS — Issue 4 (e2e skill readiness line) — `grep -n "treating it as ready" .agents/skills/run-pipelines-e2e/SKILL.md` matches; local line quoted byte-for-byte against `apps/memory/src/tree/memory/rag/indexing.py:566-567`; `ready (status=%s)` (line 573) named as the Atlas wording; "prints in the `make memory-serve-workflows` terminal" claim confirmed — `grep -rn PREFECT_LOGGING_EXTRA_LOGGERS apps/memory/` → no matches (module logger not forwarded).
- [x] PASS — Issue 5 (tree-memory skill graphrag stop-rule clauses) — `wc -l .agents/skills/tree-memory/SKILL.md` → 150 (≤153); rule 2/4 literals verified against `apps/memory/src/tree/mcp/graph_tools.py`: `_serialize([])` = `json_util.dumps([], indent=2)` = `"[]"` (line 78), `deep_search_memory` returns the literal `"No results found."` (line 367); task-128 greps still hold — `PROACTIVE\|background agent` → 0, `'"error"'` → 0, `disable-model-invocation: true` → 1, tool table 13 rows (header + 12 data rows) unchanged.
- [x] PASS — `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green (env `local`, confirmed `make env-status` → local) — see Test summary above.
- [x] PASS — docs/adrs/008_mcp_tool_contract.md §5 and docs/glossary.md "Session-end hook" row agree with the README/code: both now say the cloud entry is unsupported until persistent OAuth token storage exists, 401 → skip. No unrelated content changed in either file (diff limited to the one sentence/clause each).
- [x] PASS — Tester re-runs full QA suite and PASSES (this entry).
- [ ] Not this agent's call — PA re-runs acceptance review on the feature and ACCEPTS.

**Evidence**
```
$ make memory-tests   # run 1
============================ 3106 passed in 46.08s =============================
$ make memory-tests   # run 2
============================ 3106 passed in 47.71s =============================

$ jq '.hooks.SessionEnd[0].hooks[0]' .claude/settings.json
{
  "type": "command",
  "command": "uv --directory apps/memory run python scripts/hook_session_end.py tree-memory-local",
  "timeout": 60
}
$ grep -c '"timeout": 0' apps/memory/README.md   # 0
$ uv --directory apps/memory run python -c "... load_server_config(mcp_json, 'tree-memory', cwd=...) ..."
MATCH: byte-identical, no auth/cwd/env added

# Cloud-path live check (tree-memory argument, real .mcp.json, no credentials)
$ echo '{...fixture...}' | uv --directory apps/memory run python scripts/hook_session_end.py tree-memory
ingest_conversation unreachable on 'tree-memory' (HTTPStatusError: Client error '401 Unauthorized' for url 'https://tree-memory.fastmcp.app/mcp' ...) — session not persisted.
EXIT=0   # ~1.2s total, no browser, no 30s hang

# Broken local server (no --env-file, absolute --directory, env -i PATH HOME)
$ echo '{...fixture...}' | env -i PATH="$PATH" HOME="$HOME" uv --directory apps/memory run python scripts/hook_session_end.py tree-memory-local
RuntimeError: MCP server boot: neither --user-id nor TREE_USER_IDENTIFIER is set...
ingest_conversation unreachable on 'tree-memory-local' (McpError: Connection closed) — session not persisted.
EXIT=0
```

**Other issues found**
- Not a FAIL, worth a follow-up: the fixture transcript (`tests/unit/mcp/fixtures/session_end_transcript.jsonl`) has a deliberately malformed line 4, so EVERY smoke test run by README:508 / the e2e skill / this QA pass prints an unexplained `Skipping malformed transcript line 4: Unterminated string starting at: line 1 column 43 (char 42)` before the receipt line. Neither doc mentions it, so a developer following the smoke test verbatim sees an unexplained WARNING and may reasonably wonder if something is broken. One sentence in the smoke-test blurb ("line 4 is a deliberately malformed fixture line — you'll see one `Skipping malformed transcript line 4` WARNING before the receipt") would close this. Filed as a note, not a blocker — behavior is correct, only the doc is silent.
- The one prior-session flake noted by the SWE (a single non-zero `make memory-tests` run right after the revert, output truncated) did not reproduce across my two full runs plus the SWE's four; treating it as noise per the SWE's own assessment.

**VERDICT: PASS**

### [PA] 2026-09-12 19:05 — Acceptance Review, round 2 (feature mcp-tool-contracts, PR #43, HEAD f9962c8)

**VERDICT: ACCEPT**

Re-verified every issue against the pushed tree (grep ACs re-run, docs re-read, wired command run
live from the repo root: `echo '{}' | …` → `skipped.` exit 0; a rerun of the already-ingested
`rollup130-smoke-1` → `duplicate=True … status=duplicate` exit 0 in 5.5 s):

- Issue 1 — fixed. README "Disable it" names `disableAllHooks` (settings file or `--settings` for
  one run), the merge-across-levels caveat, and "drop the `hooks` block" for permanent removal.
  `"timeout": 0` gone.
- Issue 2 — fallback accepted; the revert of `auth: "oauth"` was the right call. With fastmcp
  3.2.0's `MemoryStore` the "preferred" default would trade one silent 401 → skip for a browser
  popup plus a 30 s stall at EVERY session end — strictly worse for the user. What ships is honest:
  README "Local server only, for now" says exactly what happens when `tree-memory` is used (401 →
  one skip), names persistent token storage as the prerequisite, and says nobody has walked the
  cloud path; the Tester walked the 401 → skip claim live (~1.2 s, exit 0). ADR-008 §5 and the
  glossary "Session-end hook" row say the same thing (diff limited to that one clause each — an
  in-place correction of this feature's own unmerged ADR, not an edit of a shipped decision).
  The `[HUMAN]` cloud-login AC stays unchecked and deferred to the owner post-merge.
- Issue 3 — fixed. One command string across `.claude/settings.json`, README ×2 and the e2e skill
  (`uv --directory apps/memory run python scripts/hook_session_end.py tree-memory-local`), no
  `--env-file`; `.mcp.json` keeps its own `--env-file` so the spawned server still loads `.env`
  (proven by the live receipt). `test_wired_hook_command_carries_no_env_file` pins it.
- Issue 4 — fixed. e2e skill quotes the local mongot line verbatim, says it prints in the
  `make memory-serve-workflows` terminal (not the streamed run log, until `tasks/132`), and names
  `ready (status=READY)` as the Atlas wording.
- Issue 5 — fixed. Rule 2 maps graphrag's `[]` / `No results found.` onto `nothing_found`; rule 4
  says `search_mode` is rag-only. 150 lines; every task-128 grep still holds.

Spot-check of previously-PASS surfaces: hook JSON snippet, guards paragraph, smoke test and
`load_server_config` docstring all consistent with the wired behaviour; `.mcp.json` untouched.

Tester's non-blocking note (fixture line 4 prints one `Skipping malformed transcript line 4`
WARNING on every documented smoke test, undocumented) — confirmed live; not REJECT-worthy (the
line names its own cause and the receipt follows). Folded as one doc AC into
`tasks/131-session-end-hook-transcript-cap.md`, which already rewrites that README paragraph.

Hand off to the PR Reviewer.

