---
status: done
feature: tree-agent-harness
---

# [PR review rollup] tree-agent harness — one loop, four tools, MCP exposures, codemode + llm(), permissions, JSONL sessions

Tags: `rollup`, `pr-review`
Refs: PR #48 (branch: `feat/tree-agent-harness`, head 9524e49); tasks `tasks/done/202…213`, `tasks/done/215`; ADR-016; `docs/notes/tree-agent-harness-spec.md`

## Scope

PR Reviewer found **1 Blocker** and **16 Nits** in the diff (184 files, +30 368 / −5 559; every `apps/harness/src` file, every test file, the config/meta files and the docs were read).

The code side is clean: `make harness-lint-check` (one pre-existing Biome warning, Nit 1), `make harness-typecheck` and `make harness-tests` (46 files / 869 tests) all pass; `pnpm install --frozen-lockfile --offline` is up to date; no wasm, `dist/` or stray artefacts are committed; `agent-loop.ts` is 180 lines and imports only types; `src/tui/*` imports nothing from `provider/ loop/ tools/ mcp/ codemode/ compaction/`; the spec texts the code pins (§9 skills wrapper, §10.3 codemode preamble, §11.1 DangerCheck system prompt) are byte-equal; the README flag table matches the `cli.ts` parser (13/13) and the settings table matches `config/settings.ts` (every default); the QuickJS sandbox exposes only `console.*`, `setTimeout`, `clearTimeout`, `tools.*`, `searchTools`, `describeTool`, `llm` (the `__host` object is deleted after the prelude); the gate is fail-closed, the headless deny is uncatchable (the BLOCKED abort flips `#done` before the bridge rejection can reach the script), `.mcp.json` is never read, the project `.tree-agent/{mcp.json,skills,settings.json}` are trust-gated, no secret is logged and `.env.example` carries only the key name.

The one Blocker is documentation discipline and routes to **PA**, not SWE: the glossary gained ten tree-agent rows but none for the feature's own concept **Skill** (nor for **Trusted directory**). PA writes the entries (and may fold the `[PA]` Nits below into the same pass); the SWE fixes Nits at their discretion. Then the pipeline re-runs: Tester QA → PA acceptance → push → PR Reviewer re-review.

## Acceptance Criteria

- [x] Blocker 1 (`[PA]`): `docs/glossary.md` has a **Skill** row (the `SKILL.md` directory format, the discovery roots and their nearest-wins precedence, the prompt listing + `skills.listingBudgetRatio` budget, `disable-model-invocation`, the `/skill:<name> [args]` message shape with its `# <SKILL.md path>` header and `$ARGUMENTS`, `--no-skills`), citing the owning module (`skills/skills.ts`) and spec §9, in the table's existing Term / Definition / Notes style. Strongly recommended in the same pass (not gating): a **Trusted directory** row (`~/.tree-agent/trusted.json`, `--trust`, `TREE_AGENT_TRUST=1`, the TUI's one-time question, what loads only when trusted; `config/trust.ts`, spec §16), today only a parenthetical inside the **Context file** row.
- [x] Tester re-runs the harness cadence (`make harness-lint-fix && make harness-lint-check && make harness-typecheck && make harness-tests && make pre-commit`) and PASSES.
- [ ] PA re-runs acceptance review and ACCEPTS.
- [ ] PR Reviewer re-runs and reports `NO BLOCKERS`.

## Blockers (detail)

### 1. [PA] [Documentation discipline] — `docs/glossary.md` (rows 66–76) has no entry for **Skill** / **Trusted directory**
- **What's wrong:** the diff introduces Agent Skills as a first-class concept — `src/skills/skills.ts` (`Skill`, `discoverSkills`, `skillMessage`, `parseSkillCommand`), the `skills` prompt section (`prompt/system-prompt.ts:94-130`), the `/skill:<name>` command (`tui/commands.ts`, `headless/print.ts`), the `--no-skills` flag, the `skills.listingBudgetRatio` setting, task 207, spec §9 and a README section — plus user-facing strings (`unknown skill "x"; known: …`, `skills: over the listing budget`). `grep -ni skill docs/glossary.md` finds the word only inside three unrelated memory rows (lines 50, 51, 53); there is no defining row. The same holds for the trust model (`config/trust.ts`, `trusted.json`, `--trust`, `TREE_AGENT_TRUST=1`, the TUI trust prompt), which only appears as a parenthetical inside the **Context file** row.
- **Why it's a Blocker:** Dimension E's first trigger — a new domain noun in code identifiers, error messages and user-facing strings with no glossary entry. The cure is a PA-authored glossary entry, not an SWE patch.
- **Suggested fix:** PA adds the two rows described in the AC above. Fold in the `[PA]` Nits (3, 4, 5) in the same pass so the docs agree with the code once.
- **Regression test:** none (docs).

## Nits (non-blocking; will be appended to PR description if pipeline advances)

### 1. [Clean code] — `apps/harness/tests/tui/app.test.ts:238`
- **Suggestion:** the one Biome warning (`lint/complexity/noCommaOperator`): `answers: [async () => (await held, summary)]` → `answers: [async () => { await held; return summary; }]`. Makes `make harness-lint-check` fully silent.

### 2. [Clean code] — `apps/harness/src/tui/tool-card.ts:25-30` duplicates `src/permissions/summary.ts:31-36` (`displayPath`)
- **Suggestion:** the six lines (relative-to-cwd, `"."` for cwd, as-given when outside) are copy-pasted because `summary.ts` pulls in `tools/truncate.ts`, which the TUI must not import. Move `displayPath` to a neutral leaf module (e.g. `src/paths.ts`, no imports) and use it from both.

### 3. [PA] [Documentation discipline] — `docs/glossary.md:69` (**Codemode script** row) says `llm()` "16 calls / 400 k tokens per script"
- **Suggestion:** the default became 64 in 9b94fc9 (`config/settings.ts:51`, README:179, spec §10.6 / §16). Update the row.

### 4. [PA] [Documentation discipline] — `docs/notes/tree-agent-harness-spec.md:385` describes the `.agents/skills` walk as "from `cwd` up to the git root" under "later wins"
- **Suggestion:** the code (`skills/skills.ts:149-156`, `gitRootToCwd`) and README:396 walk git root → cwd so the *nearest* directory wins; the spec's wording gives the opposite precedence. Reword the spec to "from the git root down to `cwd` (the nearest wins)".

### 5. [PA] [Documentation discipline] — `docs/notes/tree-agent-harness-spec.md:696-716` (§16 Settings) omits `mcp.initializeTimeoutMs`
- **Suggestion:** 879d648 added `mcp: { initializeTimeoutMs: 30_000 }` to `config/settings.ts:61-72` and the README table (line 183); the spec never mentions the knob (nor `codemode.memoryMb` / `codemode.outputMaxBytes`, which README:187 already admits). Add the three keys to the §16 listing.

### 6. [Standards] — PR #48 description is stale on three lines
- **Suggestion:** line 41 "856 tests" (now 869), line 54 "The default `codemode.llm.maxCalls` is 16 … 41 calls" (now 64, the example fits) and line 55 "The `/skill:` message has no skill-dir header" (fixed in 9b94fc9). Update the body before hand-off so the human merger does not chase closed follow-ups.

### 7. [Clean code] — `apps/harness/src/cli.ts:146-148` + `src/session.ts:187-188` load a resumed session twice; README:388 misattributes the effect
- **Suggestion:** `cli.ts` pre-loads to fail fast before the TUI starts, then `AgentSession.#restore` loads again, so every crash-cut warning prints twice for `--resume` as well as `--continue` (the README says only `--continue`, "once while picking the session" — `list()` does not parse files). Either pass the already-restored session into `create()`, or fix the README sentence to "a resumed session warns twice (the CLI pre-check and the session restore)".

### 8. [Simplicity] — `apps/harness/src/mcp/config.ts:117-157` (`describe` / `phrase` / `article`)
- **Suggestion:** `shrink:` 40 lines that rewrite zod issues into prose ("must be a string", "must be one of …", an article helper) for one config file, while `config/settings.ts:155-164` formats the same kind of issue in two lines (`path: message`). Use the settings formatter for `mcp.json` too; the union-branch pick (`url` → HTTP, `command` → stdio) is the only part worth keeping.

### 9. [Standards] — `apps/harness/src/tools/registry.ts:131-135` (`#callable`) lets a `codemode`-origin call run any non-hidden tool
- **Suggestion:** for `origin === "codemode"` it returns `true` for a `deferred` tool that is not activated, so such a call would also skip the gate (`session.ts:487-493` only gates tools in `scriptable()`). Unreachable today — the bridge only exposes `scriptable()` names and `host.call` is closed over — but it is the one place the exposure model is not enforced. Make it `tool.exposure === "codemode" || tool.exposure === "direct" || this.#activated.has(tool.name)`.

### 10. [Standards] — `apps/harness/src/compaction/compaction.ts:108-129` (`cutPoint`) moves the cut *forward* to the next `user` message
- **Suggestion:** after walking back `keep` tokens, the cut advances to the nearest later `user` message, so the kept tail can be far smaller than `keepRecentTokens` (e.g. `[u1, 30 k of tool turns, u2, 2 k of turns]` keeps only the last 2 k). Walking *back* to the previous `user` boundary also never splits a tool batch and keeps ≥ `keep` tokens, matching the spec's "keeping ≈ 20 k tokens of tail". Judgment call; the current behaviour is tested and summarised context is not lost.

### 11. [Standards] — `apps/harness/src/mcp/transport-stdio.ts:105-109`, `:151-162`, `:65-83` — `close()` / `handleSignal()` await the child's `close` event unbounded
- **Suggestion:** the child is spawned without `detached: true`; a grandchild that inherited the pipes (`uv run` → `python`) survives the SIGKILL of `uv` and keeps stdout open, so `once(child, "close")` may never resolve and `main()`'s `finally` hangs. `tools/bash.ts:96-121` already solves this with an own process group and `process.kill(-pid)`; do the same here, or bound the await (`Promise.race` with the SIGKILL timer + a grace period).

### 12. [Clean code] — `export` on module-internal symbols across `apps/harness/src`
- **Suggestion:** about twenty constants / helpers are exported but consumed by no other module and no test, e.g. `session.ts RESULT_CAP_BYTES`, `tools/truncate.ts joinHeadTail`, `provider/openrouter.ts toWireMessages`, `OPENROUTER_BASE_URL`, `compaction/compaction.ts serializeConversation`, `SUMMARY_SYSTEM`, `SUMMARY_PROMPT`, `SUMMARY_TIMEOUT_MS`, `compaction/estimate.ts MIN_RATIO`, `MAX_RATIO`, `prompt/system-prompt.ts DESCRIPTION_CAP`, `permissions/summary.ts SUMMARY_CAP_BYTES`, `permissions/decision-model.ts REASON_MAX`, `INPUT_CAP_BYTES`, `mcp/register.ts MAX_TOOL_NAME`, `MAX_RESULT_BYTES`, `tools/read.ts MAX_READ_LINES`, `tools/bash.ts DEFAULT_TIMEOUT_S`, `MAX_TIMEOUT_S`, `tui/style.ts italic`, `underline`, `strike`, `session-store/jsonl.ts InMemorySessionStore`. Not dead code (each is used in its module), so drop the keyword where nothing imports it, or add `knip` to the harness lint so the list stays empty.

### 13. [Untested] — `apps/harness/tests/cli.test.ts:670` reads the repo's real `.agents/skills/tree-memory/SKILL.md`
- **Suggestion:** editing that skill's wording fails a harness unit test. Point the test at a fixture skill under `tests/fixtures/`, or loosen the assertion to the header line + `$ARGUMENTS` substitution.

### 14. [Standards] — `apps/harness/README.md:93` overclaims `--help`
- **Suggestion:** (PA-noted) the cell says `--help` wins "whatever else is on the line"; an unknown option still wins (`--help --bogus` → stderr, exit 1), as line 61 states. One-cell wording fix.

### 15. [Standards] — `tasks/done/206-harness-permissions-gate-decide-auto-mode.md:196`, `:243`
- **Suggestion:** (PA-noted) historical SWE log lines still describe the old "`/tmp/x` unprompted" expectation; the AC itself is correct. Add a one-line "superseded by 215 Issue 4" note or leave as history.

### 16. [Standards] — session JSONL persists raw tool output
- **Suggestion:** `bash` inherits `process.env` (by spec), so a model-run `env` or `printenv` lands `OPENROUTER_API_KEY` in the tool result, the session file under `~/.tree-agent/sessions/` and the provider request. Inherent to the design and the same as pi / Claude Code; one README sentence under Sessions ("session files contain tool output verbatim; treat them like shell history") would tell the user.

---

Refs: PR #48

## Log

### [PA] 2026-10-25 06:21 — Documentation discipline (Blocker 1 + Nits 3, 4, 5)

**Summary**
Authored the two missing glossary rows and aligned the spec with `config/settings.ts` and `skills/skills.ts`. Docs only — no harness source, tests or README touched (the SWE owns those in this pass). Not committed.

**Changes (file:line)**
- `docs/glossary.md:77` — NEW row **Skill**: `SKILL.md` directory format (`name` rule `^[a-z0-9]+(?:-[a-z0-9]+)*$` ≤ 64, `description`, `disable-model-invocation`); the four discovery roots in the code's order with later-root-wins (`~/.tree-agent/skills`, `~/.agents/skills`, then trusted-only `.agents/skills` from the git root DOWN to `cwd` — nearest wins — and `<cwd>/.tree-agent/skills`; realpath dedupe, symlinks, diagnostics never throw); the `<available_skills>` prompt listing (name + 250-char description + location) under the `skills.listingBudgetRatio` (0.01) × `contextLength` budget with the startup warning; `/skill:<name> [args]` message shape (`# <SKILL.md path>` header, `$ARGUMENTS` substitution or body + blank line + args, `unknown skill "x"; known: …`); `--no-skills`. Cites `skills/skills.ts`, spec §9, task 207. Order verified against `skills.ts:40-63, 148-156` and the 207 SWE log (`tasks/done/207-…md:128`).
- `docs/glossary.md:78` — NEW row **Trusted directory**: `~/.tree-agent/trusted.json` (JSON array, exact `resolve(cwd)` match — a trusted parent does not trust children), `--trust` / `TREE_AGENT_TRUST=1` (one run, nothing written), the TUI's one-time question → `trustDirectory()`, headless never prompts; what loads only when trusted (**Context file** walk, `.tree-agent/{settings.json,mcp.json,skills}`, `.agents/skills`); malformed-file behaviour. Cites `config/trust.ts`, spec §16. Facts from `trust.ts:1-6, 16-25, 29-40, 53-61`.
- `docs/glossary.md:76` — **Context file** row: the parenthetical `(~/.tree-agent/trusted.json, --trust, TREE_AGENT_TRUST=1)` replaced by a cross-reference to **Trusted directory**.
- `docs/glossary.md:69` — Nit 3: **Codemode script** row `llm()` "16 calls" → "64 calls" (`settings.ts:51` default 64). The row's other limits (60 s, 256 MB, 50 tool calls, concurrency 4, 400 k tokens) already matched `settings.ts:36-53`.
- `docs/notes/tree-agent-harness-spec.md:385` — Nit 4: the `.agents/skills` walk now reads "then — in a trusted `cwd` only — every `.agents/skills/*/` from the git root down to `cwd` (the nearest wins)" (was "from `cwd` up to the git root"). The trusted-only clause is a small addition beyond the nit, matching `skills.ts:47-53`. The byte-pinned `<available_skills>` wrapper block (lines 389–395) is unchanged — `git hash-object` of those lines is identical to `HEAD`.
- `docs/notes/tree-agent-harness-spec.md:707-708, 713` — Nit 5: §16 zod snippet gains `mcp: z.object({ initializeTimeoutMs: z.number().default(30_000) }).default({})` (`settings.ts:61-72`) and, as the nit also named them, `codemode.memoryMb` (256) and `codemode.outputMaxBytes` (20_000) (`settings.ts:42, 44`). The snippet now carries every key `config/settings.ts` declares.

**Not changed**
- Nits 1, 2, 6–16 are SWE / PR-description items, untouched here.
- `apps/harness/**`, `tasks/done/206-…`, `.env*`, the untracked `docs/Tree-Agent Harness Implementation Spec.md` — not opened.

Blocker 1 ticked. Next: Tester harness cadence → PA acceptance → PR Reviewer re-review.

### [SWE] 2026-10-25 06:29 — Implementation (code Nits 1, 2, 7, 9, 11, 12, 14, 15)

**Files modified**
- `apps/harness/tests/tui/app.test.ts` — Nit 1: the comma operator is now a block body; Biome reports 0 warnings.
- `apps/harness/src/display-path.ts` (new, imports only `node:path`) — Nit 2: `displayPath`, used by `src/tui/tool-card.ts` and `src/permissions/summary.ts` (the copy and the `summary.ts` export are gone). `src/tui` still imports nothing from `tools/ permissions/ provider/ loop/ mcp/ codemode/ compaction/`.
- `apps/harness/tests/display-path.test.ts` (new) — the `displayPath` cases moved from `tests/permissions/summary.test.ts`, plus inside / outside cwd.
- `apps/harness/src/session.ts`, `src/cli.ts` — Nit 7: `SessionOptions.resume` is `string | RestoredSession`. The CLI's fail-fast check loads the session once and passes it to the session, so a crash-cut line warns once. `apps/harness/README.md` Sessions bullet now says one warning for both `--resume` and `--continue`.
- `apps/harness/src/tools/registry.ts` — Nit 9: `#callable` lets a `codemode`-origin call run only `codemode` ∪ `direct` ∪ activated, the same set as `scriptable()`. `tests/mcp/register.test.ts` called a deferred tool without activating it (it relied on the loophole), so it now calls `activate()` first.
- `apps/harness/src/mcp/transport-stdio.ts` — Nit 11: the child is spawned `detached: true`. `close()`, `handleSignal()` and the exit hook now signal the process group (`process.kill(-pid, …)`). Every wait is capped by `closedWithin()` (SIGTERM at 1 s, SIGKILL at 3 s, stop waiting 1 s later). `close()` is memoised, so a second call does not wait again. It returns early on the `close` event, not on the child's exit, so it still kills the group when the direct child is gone but a grandchild holds the pipes.
- `apps/harness/README.md:93` — Nit 14: the `--help` cell now names the exception (an unknown option still wins: its error and the usage on stderr, exit 1). The same correction went into the `parseCli` comment in `cli.ts`.
- `tasks/done/206-harness-permissions-gate-decide-auto-mode.md` — Nit 15: added one line `(superseded by 215 Issue 4: the AC now expects a prompt for writes outside cwd)` after line 196 and after the Tester's note 6. Nothing else changed.
- Nit 12: removed the `export` keyword from 23 symbols that no other module or test imports. Each is still used in its own module, so `tsc` and Biome stay clean. The symbols: `session.ts RESULT_CAP_BYTES`, `tools/truncate.ts joinHeadTail`, `provider/openrouter.ts toWireMessages`, `OPENROUTER_BASE_URL`, `compaction/compaction.ts serializeConversation`, `SUMMARY_SYSTEM`, `SUMMARY_PROMPT`, `SUMMARY_TIMEOUT_MS`, `compaction/estimate.ts MIN_RATIO`, `MAX_RATIO`, `prompt/system-prompt.ts DESCRIPTION_CAP`, `permissions/summary.ts SUMMARY_CAP_BYTES`, `permissions/decision-model.ts REASON_MAX`, `INPUT_CAP_BYTES`, `mcp/register.ts MAX_TOOL_NAME`, `MAX_RESULT_BYTES`, `tools/read.ts MAX_READ_LINES`, `tools/bash.ts DEFAULT_TIMEOUT_S`, `MAX_TIMEOUT_S`, `tui/style.ts italic`, `underline`, `strike`, `session-store/jsonl.ts InMemorySessionStore`. No `knip` added.

**Tests** (each was red before its fix)
- `tests/cli.test.ts` › `main (sessions)` › "a crash-cut session resumed via --continue warns exactly once and mends the file", and the same for "--resume <id>". Before the fix: 2 warnings, not 1. The tests also check that the next entries are written on a fresh line and that every line apart from the cut one parses.
- `tests/tools/registry.test.ts` › "lets a codemode script call only codemode ∪ direct ∪ activated tools". It replaces the old test that asserted the loophole. An unactivated deferred tool gives `Unknown tool: b`, and the call runs after `activate()`.
- `tests/mcp/transport-stdio.test.ts`:
  - "close() ends a grandchild that holds the pipes open and returns promptly": a `node -e` child spawns `sleep 30` with inherited stdio. `close()` returns in under 3 s (about 1.1 s), the grandchild is gone and `pgrep -g <pgid>` finds nothing. Before the fix: timed out at 5 s.
  - "alone, it also ends a pipe-holding grandchild before exiting": the same check through `handleSignal`, exit 130. Before the fix: timed out.
  - "close() stops waiting ~1 s after the SIGKILL when a pipe outlives the group": the grandchild calls setsid, so no signal reaches it. `close()` returns at 3.9–6 s; this tests the cap on the wait.
  - Cleanup: an `afterEach` SIGKILLs the recorded groups and grandchildren, so a failing run leaves nothing behind.
- Unit: 876 passing, 0 failing, 47 files (was 869 / 46).
- Integration: N/A (the harness has none).

**Evidence**
```
$ make harness-lint-fix && make harness-lint-check && make harness-typecheck && make harness-tests && make pre-commit   # exit 0
Checked 104 files in 52ms. No fixes applied.          (0 warnings)
pnpm exec tsc --noEmit                                (clean)
 Test Files  47 passed (47)
      Tests  876 passed (876)
   Duration  7.97s
biome check (harness)....................................................Passed
$ pgrep -fl "sleep 30|fake-mcp-server|vitest"          # nothing (exit 1)

# Smoke: the real `uv run` → python shape through StdioTransport
$ node <scratch>/uv-smoke.ts     # uv run --no-project python -c '…print frame; sleep 60'
group before: 2 procs
close() took 1011 ms; group after: []

$ node src/cli.ts --help --bogus   → stderr "Unknown option '--bogus'…", exit 1
$ node src/cli.ts --help --max-turns x → usage on stdout (help wins over a bad value)
```

**Acceptance criteria**: none ticked (the Tester ticks them).

**Not changed (judgement calls, as filed)**: Nits 8 (`mcp/config.ts` prose formatter), 10 (`cutPoint` direction), 13 (cli test reads the real `tree-memory` SKILL.md), 16 (README sentence on raw tool output in sessions). Nits 3, 4, 5 and Blocker 1 belong to PA. Nit 6 (PR description) is handled at push time.

**Notes**
- Behaviour change from Nit 11: with `detached: true`, an MCP child no longer receives the terminal's SIGINT or SIGHUP through process-group membership. The harness's `handleSignal` and the headless abort deliver SIGINT/SIGTERM explicitly; that path is already designed and tested. If the terminal closes (SIGHUP) the harness dies without an `exit` event, and the children see stdin EOF; a well-behaved MCP stdio server (FastMCP) exits on that. Adding SIGHUP to `SIGNALS` is a possible follow-up, not done here.
- Test count 869 → 876 (+7): cli +2 (`it.each`), `display-path.test.ts` +2 net (3 new, 1 moved out of `summary.test.ts`), transport-stdio +3; the registry test was replaced 1-for-1.
- Post-Tester fold-in (Tester "Other issues" 1): after `close()` gives up on a pipe held by an orphan that left the group, `transport-stdio.ts` now closes both readline interfaces and destroys the child's stdout/stderr, so the harness's event loop drains instead of lingering until the orphan exits; the child's `"close"` (and `onClose`) then fires, which `client.ts` ignores for a peer it already dropped.
- The existing setsid-orphan test now also asserts `onClose` fires and the `PipeWrap` count in `process.getActiveResourcesInfo()` returns to its pre-start value (red before the fix: "timed out waiting"); cadence green, still 876 tests, `pgrep -lf 'sleep 30'` empty after vitest exits.

### [Tester] 2026-10-25 06:55 — QA

**Test summary**
- Cadence `make harness-lint-fix && make harness-lint-check && make harness-typecheck && make harness-tests && make pre-commit`: exit 0. Biome "Checked 104 files … No fixes applied" (0 warnings, 0 errors), `tsc --noEmit` clean, prettier / ruff / biome pre-commit hooks Passed.
- Unit tests: 876 passed / 0 failed (47 files, ~8 s); vitest exits by itself, no leftover process.
- Integration tests: N/A (none by design).
- `tests/session.test.ts` x10: 81/81 each time (real-timer 206 test stable).
- `pnpm install --frozen-lockfile --offline`: "Already up to date". `agent-loop.ts` 180 lines, byte-identical to HEAD. `make harness-build` clean, `node dist/cli.js --help` exit 0, `--help --bogus` exit 1 (stderr), `dist/` git-ignored.
- git status: 32 entries as before; no `.env*`, `apps/memory` or `docs/Tree-Agent Harness Implementation Spec.md` change; uncommitted PA ACCEPT + PR-Reviewer entries in `tasks/done/215-…md` intact (+39/-1).

**E2E adversarial pass** (fake `TREE_AGENT_HOME` under the scratchpad `qa-217/`; real `tree-memory-local` over `uv run`, infra local)
- Docs 1 (glossary shape): rows 77 (**Skill**) and 78 (**Trusted directory**) are single lines with 3 cells (`awk -F'|'`); row 76 cross-refs; PASS. No other row says `llm()` 16 (`grep -n '\b16\b'` hits only unrelated rows 29, 78).
- Docs 2 (Skill claims vs code): discovery order `~/.tree-agent/skills` → `~/.agents/skills` → trusted `.agents/skills` git-root→cwd → `<cwd>/.tree-agent/skills`, later wins, realpath dedupe, diagnostics not throws, name regex/64 cap, 250-char description cap, `skills.listingBudgetRatio` 0.01, `$ARGUMENTS` / blank-line+args shape, case-insensitive `/skill:`, `unknown skill "x"; known: …` all match `skills/skills.ts`, `session.ts:245-290`, `prompt/system-prompt.ts:99`. `--no-skills` → `skills: !values["no-skills"]` → `skills: null` → no discovery, no section, `/skill:` answers "skills are disabled (--no-skills)" (`cli.ts:263,306,207`, `session.ts:89,261`). PASS.
- Docs 3 (Trusted directory claims vs code): exact `resolve(cwd)` equality, `--trust` / `TREE_AGENT_TRUST=1` = one run and nothing written (`askTrust: !trusted`), TUI-only `trustDirectory`, headless `open(false, null)` never prompts, non-array `trusted.json` warns+trusts nothing on read / throws on write, loads-only-when-trusted list (context-files, project settings, mcp, skills) — all match `config/trust.ts`, `cli.ts:143-156,225`, `prompt/context-files.ts`. PASS.
- Docs 4 (spec): `git show HEAD:…spec.md | sed -n 389,395p | shasum` = working tree `4e3501fe…` (byte-pinned `<available_skills>` block untouched); §9 prose and §16 diff reviewed; §16 snippet lists every key of `config/settings.ts` (model, decisionModel, permissions.mode, compaction x2, codemode x9 incl. memoryMb/outputMaxBytes/llm x6, mcp.initializeTimeoutMs, contextFiles.maxChars, skills.listingBudgetRatio), defaults equal, no extra key. §9 wrapper / §10.3 preamble / §11.1 DangerCheck pinned-text tests pass in the 876. PASS.
- Nit 7 `--continue` with a crash-cut file (4 full lines + truncated 5th, no newline): `node apps/harness/src/cli.ts --no-mcp --continue "Reply with the single word ok."` → exit 0, stdout `ok`, `grep -c 'warn:'` stderr = 1; file then 7 lines, truncated line on its own line, all others parse. PASS.
- Nit 7 `--resume <id>` on a fresh copy: exit 0, `ok`, warn lines = 1. PASS. Unknown id: exit 1, `no session nope`. PASS.
- Nit 7 compaction regression (hand-built session: user/assistant old turn, tail turn, `compaction` entry with `firstKeptEntryId`): `--resume` → model sees `PINEAPPLE-77` (summary) and `TAILWORD-42` (tail), not `OLDWORD`; 0 warnings. PASS (activation restore: `session.test.ts:2134` green, `#restore` unchanged for `restored.activated`).
- Nit 7 `--continue` with no prior session: exit 0, starts a new session (answers normally). This matches README:100 ("none: a new session") and `cli.test.ts:949`, and HEAD behaviour; the "exit 1" expectation in the brief is not the specified behaviour. PASS (note).
- Nit 9 via the real `codemode` tool (scratch test, removed): script `typeof` of `call`, `host`, `__host`, `tool`, `tools.codemode`, `tools.hid` all `undefined`; `Object.keys(tools)` = `dir,deff,cm` after activation; inactive deferred: `tools.deff` is not a function (TypeError, gate never invoked — `gated` lists only `dir,deff` after activation); after `registry.activate(["deff"])` the call runs and is gated. Registry-level `Unknown tool: b` / `Unknown tool: d` for a codemode-origin call is asserted by `registry.test.ts:156`. PASS (note: from a script the inactive-deferred failure is `TypeError: not a function`, `Unknown tool` is the defence-in-depth layer below).
- Nit 11 fixtures (StdioTransport driven from a scratch script; `pgrep`/`ps` after): grandchild in the group holding the pipes, child exits on EOF → `close()` 1006 ms, no `sleep` left; child ignores EOF + grandchild → 1008 ms, none left; EOF-ignoring + SIGTERM-ignoring child → 3006 ms, none left; second `close()` 0 ms. PASS.
- Nit 11 grandchild that `setsid`s out (detached): `close()` 4002-4003 ms (cap = 3 s SIGKILL + 1 s), orphan `sleep` survives reparented to PID 1 (documented limit). NOTE: the harness process itself stays alive until that orphan dies, because the child's stdout/stderr readline handles stay referenced after the give-up (`close()` returned, event loop did not drain; observed: the driver only exited when `sleep 48` ended). Only reachable by a server that leaves its own group.
- Nit 11 real `uv run … serve_mcp.py` through the transport: `pgrep -f serve_mcp` = 2 procs before; `close()` 1010 ms (2 s after start) and 931 ms (35 s after start, fully booted); 0 procs after. PASS.
- Nit 11 headless run, real server, model running `bash sleep 300`: single SIGINT → exit 1 (abort), 0 `serve_mcp`, 0 `sleep 300`, 1 s; double SIGINT (0.3 s apart) → exit 130, none left; SIGTERM → exit 1, none left; SIGINT / SIGTERM at t=2 s (during MCP `initialize`) → exit 130 / 143 in <1 s, none left, warn `server exited (code 143)`. PASS.
- Nit 11 SIGHUP (not handled): harness dies exit 129; `serve_mcp` gone 1.34 s later (stdin EOF); NOTE: the `bash` tool's `sleep 300` (own process group; `tools/bash.ts` only changed by Nit 12, so pre-existing) survives the SIGHUP orphaned — unrelated to MCP, same follow-up as the SWE's SIGHUP note.
- Nit 11 SIGKILL of the harness: real server exits within 4 s (EOF); fixture child that ignores EOF stays orphaned (PPID 1) — the known limit. PASS (documented).
- Nit 11 forced test failure: scratch copy of `transport-stdio.test.ts` with `expect(1).toBe(2)` injected into the three grandchild tests → 3 failed as injected, `pgrep -fl "sleep 30|fake-mcp|child_process"` empty afterwards; scratch file deleted. PASS.
- Nit 12: `make harness-typecheck` and `make harness-build` clean; none of the 23 symbols is still `export`ed or named in an import (`grep` of `^export (const|function|class) <sym>` and `import … <sym>` over `src` and `tests` empty). PASS.
- Nit 2: `grep -rln "provider/\|loop/\|tools/\|mcp/\|codemode/\|compaction/\|permissions/" apps/harness/src/tui` empty (exit 1); `display-path.ts` imports only `node:path`; the only `displayPath` users are `tui/tool-card.ts` and `permissions/summary.ts` via `../display-path.ts`; no second definition. PASS.
- Mutation 1 (revert `#callable` to HEAD: `hidden → false; origin==="codemode" → true`): `tests/tools/registry.test.ts › lets a codemode script call only codemode ∪ direct ∪ activated tools` FAILS (1 failed / 40 passed). Restored; `shasum` identical to the pre-mutation file. KILLED.
- Mutation 2 (`process.kill(-child.pid, …)` → `process.kill(child.pid, …)` in `signalGroup`): `close() ends a grandchild that holds the pipes open …` and `handleSignal › alone, it also ends a pipe-holding grandchild …` FAIL (2 failed / 13 passed). Restored; `shasum` identical. KILLED.

**Acceptance criteria**
- [x] PASS — Blocker 1: **Skill** row (`docs/glossary.md:77`: SKILL.md format, discovery roots + nearest-wins, `<available_skills>` listing and `skills.listingBudgetRatio`, `disable-model-invocation`, `/skill:<name> [args]` with `# <path>` header and `$ARGUMENTS`, `--no-skills`; cites `skills/skills.ts`, spec §9) and **Trusted directory** row (`:78`; `trusted.json`, `--trust`, `TREE_AGENT_TRUST=1`, TUI question, what loads only when trusted; `config/trust.ts`, spec §16) — every claim traced to code above.
- [x] PASS — Tester cadence green (ticked by Tester).
- [ ] PA acceptance and PR Reviewer re-review — not Tester boxes, left unticked.

**Evidence**
```
$ make harness-lint-fix && make harness-lint-check && make harness-typecheck && make harness-tests && make pre-commit   # exit 0
Checked 104 files in 54ms. No fixes applied.
 Test Files  47 passed (47)
      Tests  876 passed (876)
$ ./sig.sh int1 INT   -> exit=1 serve_mcp left: 0 sleep300 left: 0
$ ./sig.sh int2 INT,INT -> exit=130 none left;  TERM -> exit=1 none left;  HUP -> exit=129, serve_mcp gone in 1.34 s, sleep 300 (bash tool) orphaned
$ node real.ts 35000 -> serve_mcp before: 2, close ms 931, after: 0
$ mutation 1: Tests 1 failed | 40 passed (registry);  mutation 2: Tests 2 failed | 13 passed (transport-stdio)
```

**Other issues found** (non-blocking, none is an acceptance failure)
- `src/mcp/transport-stdio.ts` give-up path: after `close()` stops waiting on a pipe held by a process that left the group, the child's stdout/stderr streams are still referenced, so `main()` returns but the Node process lingers until the orphan exits. Cheap fix: `child.stdout?.destroy(); child.stderr?.destroy()` after the cap. Only reachable by a server that `setsid`s a pipe-holder.
- SIGHUP is unhandled (SWE already noted): MCP child exits on EOF in ~1.3 s, but the `bash` tool's process group is orphaned on SIGHUP (pre-existing, independent of Nit 11). Follow-up task candidate: add SIGHUP to the signal hook and kill the bash group.
- Inactive deferred tool from a codemode script surfaces as `TypeError: not a function`, not `Unknown tool` (the registry message is only reachable by a direct `execute`); fine but the model sees a terse error.

- Nit 7 follow-ups: `--resume <id>` re-run 3 more times from fresh crash-cut copies: exit 0, 1 warning each (the one earlier exit 1 was my zsh word-splitting of `$args`, not the harness). Mended file reloaded with `--continue`: exit 0, 1 warning (the cut line is still in the file), file grew 7 → 9 lines. Nit 14: README:93 names the unknown-option exception. Nit 15: `tasks/done/206-…md` gained exactly two "(superseded by 215 Issue 4 …)" lines. Recommendation: route the two-line `child.stdout?.destroy(); child.stderr?.destroy()` fix (lingering process after the give-up, above) to the SWE in this uncommitted pass; it does not block the 217 ACs.

**VERDICT: PASS**
