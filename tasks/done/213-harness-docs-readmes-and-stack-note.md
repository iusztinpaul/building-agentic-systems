---
id: 213-harness-docs-readmes-and-stack-note
status: done
feature: tree-agent-harness
---

# Docs — rewrite the root README's harness lines and `apps/harness/README.md` for tree-agent, document the hosted-server recipe and the committed `.tree-agent/mcp.json`, and point `docs/notes/modern-typescript-stack.md` at ADR-016

Tags: `harness`, `docs`
Depends on: 212 (every feature shipped; the docs describe what exists)
Blocks: —
Implements: ADR-016 §1, §3, §11 (the user-facing record of the toolchain, the MCP source and the cadence)

## Scope

1. **Root `README.md`** — line 10 (harness = `tree-agent`, a light pi clone in TypeScript: one loop, four
   built-in tools, MCP with exposure levels, codemode + `llm()`, two permission modes, JSONL sessions —
   no Ink, no auto-spawn of `.mcp.json`); line 19 (Node ≥ 22 + pnpm); line 22 (`.mcp.json` is Claude Code's
   file; the harness reads `.tree-agent/mcp.json`); line 32 (prerequisite: Node ≥ 22 with corepack —
   `corepack enable pnpm` — replacing Bun); line 41 (drop the ripgrep/`grep` tool line); lines 57–58
   (install comment); lines 189–202 (TUI `make harness-dev` / `tree-agent`, headless
   `make harness-run PROMPT="…"`, `--json`, the `.tree-agent/mcp.json` sentence with the tool names
   `mcp__tree_memory_local__*`, the exposure split and why); the "App guides" harness line (modes, built-in
   tools, exposure, permissions, skills, codemode/`llm()`, sessions — no sub-agents, no shell hooks).
   Also add `OPENROUTER_API_KEY` next to `GOOGLE_API_KEY` in the Installation step.
2. **`apps/harness/README.md`** — the full guide: what it is and the spec/ADR links; install (`corepack
   enable pnpm`, `make harness-install`); the CLI table from spec §15 (modes, flags, exit codes); config dirs
   and `settings.json` keys (§16) with `TREE_AGENT_HOME`; trust; `.tree-agent/mcp.json` schema (§10.1), the
   committed example with the reasons for each glob, and the hosted `tree-memory` recipe: "add it to
   `~/.tree-agent/mcp.json` as `{url: "https://tree-memory.fastmcp.app/mcp", headers: {Authorization:
   "Bearer <token>"}}` once a token exists (Horizon OAuth; the harness has no OAuth flow)"; the note that the
   memory tools carry no `readOnlyHint` (so `search_memory` is classified/prompted); permissions (`ask`/`auto`,
   exit 2, `--on-blocked`); exposure and `tool_search`; codemode with the §10.6 worked example; compaction
   knobs; sessions path and `--resume`; skills; test layout (`tests/`, one vitest file per module, two live
   tests skipped without the key); the verification cadence line from AGENTS.md.
3. **`docs/notes/modern-typescript-stack.md`** — a two-line status note at the top: the harness runs on
   Node + pnpm + vitest per ADR-016 §1 (book fidelity to pi); the note stays as a general reference. No other edit.
4. **Stale-mention sweep** — `grep -rn -i "bun\b\|ink\b\|sub-agent\|shell hook\|ripgrep" README.md
   apps/*/README.md docs/notes/*.md` and fix every hit that describes the harness (not Bun mentions in the
   stack note's own body).

**Out of scope (intentional):** AGENTS.md (done in 202); the ADR and glossary (grooming commit); tutorial
chapters; a CHANGELOG.

## Acceptance Criteria

- [x] Every listed root README line is rewritten; `grep -n -i "ink\|bun\|ripgrep\|sub-agent\|shell hook\|auto-spawn\|\.mcp\.json.*harness" README.md`
      returns only lines that describe Claude Code's `.mcp.json`, none that describe the harness.
- [x] `apps/harness/README.md` has sections for install, CLI, config, MCP (schema + committed example +
      hosted recipe), permissions, exposure, codemode/`llm()`, compaction, sessions, skills, tests, cadence;
      every flag in spec §15 appears in its table; every `settings.json` key in §16 appears.
- [x] `docs/notes/modern-typescript-stack.md` starts with the two-line pointer to ADR-016 and is otherwise
      byte-identical (`git diff --numstat` shows `2 0`).
- [x] All relative links in both READMEs resolve (a link check over `README.md` and `apps/harness/README.md`).
- [x] `make help` output is unchanged by this task.
- [ ] [HUMAN] Following only `README.md` + `apps/harness/README.md` on a machine without pnpm, the human
      reaches a working `tree-agent "say hi"` and a memory search through `tree-memory-local`.
- [x] `make harness-lint-fix && make harness-lint-check && make harness-typecheck && make harness-tests &&
      make pre-commit` green (pre-commit's prettier hook formats the touched markdown/JSON).

## User Stories

### Story: A reader installs from the README
1. They read Prerequisites (Node ≥ 22, corepack), run `corepack enable pnpm`, `cp .env.example .env`, add
   `OPENROUTER_API_KEY`, `make harness-install`.
2. `tree-agent "say hi"` answers; `make harness-run PROMPT="what is in my memory about agents?"` calls
   `mcp__tree_memory_local__search_memory` after `make local-start`.

### Story: A reader wants the hosted memory server
1. They find the "hosted `tree-memory`" subsection.
2. It tells them to add a `url` + `Authorization` header entry to `~/.tree-agent/mcp.json` and that no OAuth
   flow exists in the harness.

### Story: A reader wonders why not Bun
1. They open `docs/notes/modern-typescript-stack.md`.
2. The first two lines point to ADR-016 §1; the note's content is unchanged below.

### Story: A reviewer checks for stale text
1. They grep the READMEs for Ink, ripgrep, sub-agents, shell hooks, auto-spawn.
2. Nothing describes the old harness.

---

Blocked by: 212

## Log

### [PA] 2026-10-24 20:40 — Grooming

**Summary**
One documentation pass after the code: the root README's harness lines, the real `apps/harness/README.md`,
the hosted-server recipe that the repo deliberately does not commit, and a pointer from the Bun stack note to
the ADR that departs from it.

**Key decisions**
- Docs land last so they describe what shipped; AGENTS.md was the exception (202) because the cadence applies
  from the first harness commit.
- The stack note is pointed at, not rewritten — it remains a valid general reference.

**Dependencies**
- 212 — the last feature task.

**User stories**
- 4 stories covering: install from the README, the hosted recipe, the Bun question, a stale-text sweep.

Ready for implementation.

### [SWE] 2026-10-25 03:00 — Implementation

**Files modified**
- `README.md` — harness lines rewritten by content (line numbers had shifted by 0–1): What-Tree-is (tree-agent, light pi clone, four tools, exposure, codemode + `llm()`, `ask`/`auto`, JSONL, reaches memory via `.tree-agent/mcp.json`); "bundled harness" → "the harness in this repo"; repo layout (Node ≥ 22 + pnpm + vitest + Biome; `.mcp.json` = Claude Code's, never read by tree-agent; new `.tree-agent/mcp.json` line); prerequisites (Node ≥ 22.19 + corepack replaces Bun; Gemini scoped to the memory app; OpenRouter key added; ripgrep line dropped); install (`OPENROUTER_API_KEY` next to `GOOGLE_API_KEY`, corepack comment); step 7 (TUI `make harness-dev ARGS="--cwd ../.."`, headless `make harness-run … ARGS="--cwd ../.. --trust"`, `--json`, `mcp__tree_memory_local__*`, exposure split + why); App guides line.
- `apps/harness/README.md` — full guide, H2s: Install (corepack + fallback, repo-root pnpm trap, target table, cwd note, PATH symlink, ExperimentalWarning), CLI (verbatim usage text, no `--help`; modes; all 12 flags; exit codes 0/1/2/130/143; TUI keys + slash commands), Config (user/project paths, context files, env vars incl. `TREE_AGENT_LOG`/`TREE_AGENT_TRUST`, all settings keys + defaults, Trust), MCP (§10.1 schema, transports/timeouts/name sanitising, `/mcp`, the committed file + why each glob, readOnlyHint consequence, hosted `tree-memory` recipe, no OAuth), Permissions, Exposure (+ built-in tools table with bash bounds), Codemode and `llm()` (sandbox API, result shapes, `{result}` unwrap, limits, gate behaviour, depth/budgets, §10.6 example + the `maxCalls` override), Compaction, Sessions, Skills, Tests, Cadence (AGENTS.md harness lines verbatim).
- `docs/notes/modern-typescript-stack.md` — two-line blockquote pointer to ADR-016 §1 above the H1; nothing else (`git diff --numstat` → `2 0`, no mode change).
- `tasks/213-…md` — status in-progress, checkboxes, this log.

**Spec-vs-code differences found (code documented)**
- §16 omits `codemode.memoryMb` (256) and `codemode.outputMaxBytes` (20 000); `settings.ts` has both and every object is `.strict()`. Both are in the settings table, with a one-line note.
- Brief said the cwd slug maps `/`, `:` and `.` to `-`. `cwdSlug()` maps `/`, `\`, `:` only (dots survive); the `:`/`.` → `-` mapping is the FILE NAME timestamp (`2026-10-24T12-00-00-000Z_<uuid>.jsonl`). Documented both separately, with the `/a/b-c` vs `/a/b/c` collision example from 205's Tester.
- §10.6 example (`tools.mcp__memory__search({limit})`, `h.text`) adapted to the real tool and rag-mode shape: `tools.mcp__tree_memory_local__search_memory({query, top_k: 40})` → `{parents: [{content}]}` (graphrag differs, said so). The `maxCalls` ≥ 41 need is stated next to the listing with a `settings.json` override (`maxCalls: 48`).
- Brief said `dist/provider/fake.js` is excluded; it is not — `tsconfig.build.json` includes `src/**/*` with no exclude and `dist/provider/fake.js` exists. README says it is emitted and nothing at runtime imports it (grep: no `src/` importer).
- Spec §17 "two live tests": confirmed — `tests/provider/openrouter.test.ts` `describe.skipIf(!OPENROUTER_API_KEY)` holds 2 `it`s.
- `.env.example`'s `TREE_AGENT_LOG` (debug|info|warn|error|silent, default warn) is not in §16; documented in the env table.
- `make harness-run` / `harness-dev` run with cwd `apps/harness`, so the repo's `.tree-agent/mcp.json` only loads with `ARGS="--cwd ../.."` (+ `--trust` headless). The task's user story (`make harness-run PROMPT=…` alone) would get no memory tools; the READMEs print the working form.
- The brief says "pre-commit's prettier formats the markdown": the prettier hook is `types_or: [yaml, json5]`, so markdown is not touched by pre-commit.

**Tests**
- No code changes. `make harness-tests`: 46 files, 849 passed (live OpenRouter tests ran — key exported by the Makefile).

**Acceptance criteria**
- [x] Root README lines rewritten — AC grep `grep -n -i "ink\|bun\|ripgrep\|sub-agent\|shell hook\|auto-spawn\|\.mcp\.json.*harness" README.md` → no output (exit 1).
- [x] Harness README sections — 12 H2s (Install, CLI, Config, MCP, Permissions, Exposure, Codemode and `llm()`, Compaction, Sessions, Skills, Tests, Cadence); scripted check: all 12 §15 flags have a `| \`--flag` row, all 17 §16 keys + 2 code-only keys have a row.
- [x] Stack note — `git diff --numstat` → `2	0	docs/notes/modern-typescript-stack.md`.
- [x] Relative links — scratchpad `linkcheck.mjs` (files + GitHub-slug anchors, negative-tested on a bad anchor and a missing file): `24 relative links ok, 0 broken` over both READMEs.
- [x] `make help` — captured before/after, `cmp` identical (sha256 prefix e189ec1106b6d543).
- [ ] [HUMAN] fresh-machine walkthrough — see the BLOCKER note below.
- [x] Cadence green — lint-fix/lint-check (1 pre-existing warning, exit 0), typecheck, tests 849/849, pre-commit all Passed.

**Evidence**
```
$ TREE_AGENT_HOME=<scratch> make harness-run PROMPT="Reply with exactly: ok" ARGS="--no-mcp --no-skills"   → ok, exit 0
$ node apps/harness/src/cli.ts --help   → Unknown option '--help'. … + usage, exit 1
$ TREE_AGENT_HOME=<scratch> script … make harness-dev ARGS="--cwd ../.. --no-mcp"  → TUI renders, trust prompt
  "Trust this directory? y: … · n: …", status bar "anthropic/claude-sonnet-5.5 · ask · 671 / 1M (0 %) · $0.0000 · 0 MCP …"
$ TREE_AGENT_HOME=<scratch> make harness-run PROMPT="…memory about agents…" ARGS="--cwd ../.. --trust"   → exit 0, BUT
  [tree-agent] warn: mcp "tree-memory-local": initialize timed out after 10 s   (twice, two runs)
$ bare JSON-RPC initialize against the same uv command (python timer): 28.5 s cold, 11.0 s / 11.1 s warm
$ make help > before; …; make help > after; cmp → identical
$ make harness-build && (cd /tmp && TREE_AGENT_HOME=<scratch> node <repo>/apps/harness/dist/cli.js "Reply with exactly: ok" --no-mcp --no-skills)
  → ok, exit 0   (the documented PATH route: dist/ compiles and runs outside the repo with the key exported)
$ timestamped boot of the same uv command: 8.3 s first Python import warning (opik pydantic.v1), 17.4 s "Opik configured",
  17.6 s "MCP server ready" + initialize answered — index checks take ~0.2 s; the time is Python imports + Opik config
```

**Notes**
- BLOCKER for the [HUMAN] AC / the documented memory e2e: on this machine `tree-memory-local` answers `initialize` in ~11 s warm (28 s cold), (a timestamped run: 17.6 s, almost all Python imports + Opik configuration before the lifespan; index checks ~0.2 s), over the harness's fixed 10 s `INITIALIZE_TIMEOUT_MS` (`src/mcp/client.ts:19`, not a setting). The session then starts with 0 memory tools (the model fell back to `bash` + `mongosh` in `auto` mode on run 1). 209–211 passed, so the server's boot slowed since. Not fixed here (docs task; no harness/memory source edits). The README states the 10 s timeout and the warning's meaning. Options for PA: raise/make configurable the initialize timeout, or speed up the memory server's boot.
- Not edited (out of bounds): `apps/memory/README.md:436` ("the bundled harness") and `:445` ("Claude Code and the harness auto-spawn it" — stale: tree-agent never reads `.mcp.json`). PA follow-up for a memory-app docs task.
- 130/143 "MCP children are killed" is backed by `src/mcp/transport-stdio.ts:32-50` (prepended SIGINT/SIGTERM listeners + an `exit` hook SIGKILL every live child).
- Corepack: Node 25+ no longer bundles corepack, so both READMEs name `npm i -g corepack` as the first step there (this machine: Node v26.7.0, corepack at /usr/local/bin from a global npm install).
- Sweep: remaining `bun`/`ink` hits are the stack note's own body (excluded by scope) and "symlink" in the harness README; nothing describes the old harness.
- Not built, still true: `search_memory` prompted/classified until task 214 annotates the memory tools; the `/skill:` body has no skill-dir header (PA follow-up from 207) — both documented.

### [Tester] 2026-10-25 05:45 — QA

**Test summary**
- Cadence `make harness-lint-fix && make harness-lint-check && make harness-typecheck && make harness-tests && make pre-commit`: exit 0. Biome 1 pre-existing warning (0 new), tsc clean, 46 files / 855 tests passed (live OpenRouter tests ran), pre-commit prettier / ruff / biome Passed. `git diff` checksum identical before and after `lint-fix`.
- Warnings introduced: 0.

**E2E / adversarial pass**
- Happy path: `TREE_AGENT_HOME=<scratch> make harness-run PROMPT="Reply with exactly: ok" ARGS="--no-mcp --no-skills"` → `ok`, exit 0 (PASS). Documented stdin form: `printf 'Reply with exactly: one\nthen reply with exactly: two\n' | node apps/harness/src/cli.ts --no-mcp --no-skills` → `one`, `two` (PASS).
- Doc claim vs code, run for real: `--help` → `Unknown option '--help'` + usage, exit 1; `--json` alone, no TTY → usage, exit 1; `hi --resume nosuch` → `no session nosuch`, exit 1; `--compact-at 0.2` → `must be from 0.3 to 0.95`, exit 1; `--continue --resume a` → `not both`, exit 1; `--max-turns 0`, `--permission-mode foo`, `--on-blocked foo`, `--cwd /nonexistent`, missing key → one line, exit 1; `--model bogus/none` (real key) → `unknown model "bogus/none": not listed by GET /api/v1/models`, exit 1 (all PASS, all as the CLI table says).
- Link-checker attack: scratch tree under `.../scratchpad/qa-213/neg/` (copy of both READMEs, `docs` and `apps/memory` symlinked): `(#mcp)` → `(#nope)`, `README.md#install` → `#nope2`, `apps/memory/README.md` → `NOPE.md`. My independent Python checker flagged all 3 kinds, exit 1; the SWE's `linkcheck.mjs` flagged the same 6 broken links. The repo files were not touched (PASS).
- Live memory run from the repo root with the committed `.tree-agent/mcp.json` (also covers Part A): `make harness-run PROMPT="Call the search_memory tool once …" ARGS="--cwd ../.. --trust --json"` → `tool_call mcp__tree_memory_local__search_memory`, real result, answer `2`, no `timed out` line (PASS).

**Acceptance criteria**
- [x] PASS — Root README lines rewritten; AC grep `grep -n -i "ink\|bun\|ripgrep\|sub-agent\|shell hook\|auto-spawn\|\.mcp\.json.*harness" README.md` → no output, exit 1. The `.mcp.json` lines left (README.md:22 only) describe Claude Code's file. The task's sweep `grep -rn -i "bun\b\|ink\b\|sub-agent\|shell hook\|ripgrep" README.md apps/*/README.md docs/notes/*.md` hits are `link`/`symlink` substrings (README.md none; `apps/harness/README.md:48` "symlink"; `apps/memory/README.md:390-391` "link"; five `docs/notes/*` "link") plus the stack note body (excluded by scope). Nothing describes the old harness. Wider sweep for PA (out of scope here, `apps/memory/README.md`): `:436` "…Cursor, the bundled harness" and `:445` "The repo-root `.mcp.json` already wires this up — Claude Code and the harness auto-spawn it" — stale: tree-agent never reads `.mcp.json` and does not auto-spawn it.
- [x] PASS — `apps/harness/README.md` has the 12 H2s (Install, CLI, Config, MCP, Permissions, Exposure, Codemode and `llm()`, Compaction, Sessions, Skills, Tests, Cadence); scripted check: all 12 flags have a `| \`--flag` row, and all 20 `Settings` keys in `src/config/settings.ts` have a row (the SWE's "19" counts one fewer; the table is complete). Cross-check against the code, number by number: defaults model `anthropic/claude-sonnet-5.5`, decisionModel `anthropic/claude-haiku-5.5`, `permissions.mode ask`, `compaction.triggerRatio 0.8` (0.3–0.95), `keepRecentTokens 20 000` (capped at half the trigger: `compaction.ts:98`), codemode `3 000 / 60 000 / 256 / 50 / 20 000 / 4`, llm `depth 1|2, maxCalls 16, maxInputChars 200 000, maxTotalTokens 400 000, maxSubTurns 8`, `mcp.initializeTimeoutMs 30 000`, `contextFiles.maxChars 40 000` (3× total: `context-files.ts:35`), `skills.listingBudgetRatio 0.01` — all match. CLI: usage block is verbatim `cli.ts` `USAGE`; parser flags == table flags (no `--help`); defaults `--max-turns 50`, `--on-blocked stop`; exit codes 0/1/2 (`print.ts` `EXIT_CODES`, everything else 1) and 130/143 (`transport-stdio.ts:60-80`, `128 + signo` after killing children) match. Committed `.tree-agent/mcp.json` reproduced exactly (same command/args/env/`search_memory: direct`, `visualize_*: deferred`); hosted recipe URL `https://tree-memory.fastmcp.app/mcp` and `headers: {Authorization: "Bearer <token>"}` as the task states. Sessions: `--<slug>--/<timestamp>_<id>.jsonl`, slug maps `/ \ :` (`jsonl.ts:92-93`), timestamp `:`/`.` → `-` (`jsonl.ts:265`); a real run wrote `sessions/--Users-…-building-agentic-systems--/2026-10-25T03-15-37-734Z_<uuid>.jsonl`. Built-ins: `read → {path, lines}`, `bash → {exitCode, stdout, stderr}`, bash `timeout` 1–3600 default 120, 25 KB head/tail, 50 KB spill to `<tmp>/<uuid>.txt`, 100 MiB cap, read 2 000 lines / 50 KB — match `tools/*.ts`. Compaction (3 failures stop, `MAX_CONSECUTIVE_FAILURES`), classifier 10 s timeout (`decision-model.ts:15`), MCP 60 s `tools/call`, skill description cap 250, slash command list — match. Cadence paragraphs are verbatim substrings of `AGENTS.md`.
- [x] PASS — Stack note: `git diff --numstat -- docs/notes/modern-typescript-stack.md` → `2	0`; `git diff --summary` empty (no mode change); both added lines sit above `# The Modern TypeScript Stack…` (lines 1–2), the first names `ADR-016 §1` with a link to `../adrs/016_tree_agent_harness.md#decision` (ADR §Decision item 1 is the toolchain). No blank line between the blockquote and the H1; an ATX heading ends a blockquote, so it renders correctly.
- [x] PASS — Relative links: 24 ok / 0 broken over `README.md` + `apps/harness/README.md` with my independent checker (`scratchpad/qa-213/lc.py`; includes anchors such as `README.md#environment-loading-with-direnv`, `AGENTS.md#verification-cadence`, `docs/adrs/016…md#decision`) and the SWE's. 24 external URLs: all well-formed https; none fetched. The `docs/…` / `.tree-agent/…` strings my path scan flagged as "not found" are URL path fragments or `~/.tree-agent/…` user-level paths, not repo references. `docs/notes/tree-agent-harness-spec.md`, `docs/adrs/016_tree_agent_harness.md` exist.
- [x] PASS — `make help`: the SWE's before/after captures are `cmp`-identical; my run now is identical to them (sha1 `ddedc43e…` of the capture); neither `Makefile` nor `apps/harness/Makefile` is in `git status`.
- [ ] Awaiting human verification — [HUMAN] fresh-machine walkthrough (left unticked as instructed).
- [x] PASS — Cadence green (above). Install story as far as the machine allows: `make -n harness-install|dev|run|build|clean` resolve to the quoted commands; `.env.example:73` has `OPENROUTER_API_KEY`; `corepack enable pnpm` and the `npm i -g corepack` / `--install-directory ~/.local/bin` fallback are stated in the harness README, and `corepack enable pnpm` + the fallback link in the root README; `package.json` has `engines.node >=22.19`, `packageManager pnpm@10.34.8…`, `bin.tree-agent: dist/cli.js`. `make harness-build` not run (SWE did; `dist/` is gitignored).
- [x] PASS — Scope: `git status` shows no `.env*`, `AGENTS.md`, `apps/memory`, ADR, glossary change; the untracked spec doc is untouched. B changed no harness source. Source files moved under `apps/harness/src` are exactly A's four: `cli.ts`, `config/settings.ts`, `mcp/client.ts`, `mcp/jsonrpc.ts`.

**Other issues found (non-blocking, PASS with note)**
- `apps/harness/README.md` (TUI section, "`/model` and `/clear` are refused while a prompt runs") omits `/compact`, which `session.ts:421` also refuses mid-run (`#idle("compact")`; `tui/app.ts:184` says so). One-word fix.
- This task's SWE log is stale after Part A: the "BLOCKER" note (fixed 10 s `initialize`) and "849 tests" are superseded by the 208 fix (30 s setting, 855 tests). The README text itself is already correct (30 s, names the setting); suggest a one-line addendum.
- `docs/notes/modern-typescript-stack.md` still has no blank line after the pointer (a literal two-line add); fine.
- For the PA: `apps/memory/README.md:436,445` stale, see AC 1.

**VERDICT: PASS**

### [SWE] 2026-10-25 06:30 — Addendum
- The 10 s `initialize` "BLOCKER" in the SWE entry above is superseded by the 208 post-commit fix: `mcp.initializeTimeoutMs`, a setting with a 30 s default.
- Test count is now 856 (855 after the 208 fix, +1 for the `*Ms` 2^31 - 1 bound fold-in); the README also lists `/compact` among the commands refused mid-run.

### [PA] 2026-10-25 05:35 — Acceptance Review (feature `tree-agent-harness`, PR #48, head b31d8ca)

**VERDICT: REJECT**

Reviewed from the user's POV across tasks 202–213 (this entry sits on the feature's closing task). Evidence checked: `make harness-tests` 46 files / 856 passed incl. the two live OpenRouter tests; headless `--no-mcp --no-skills "Reply with exactly: ok"` → `ok`, exit 0; `--permission-mode ask` write → `permission` event (`deny`, `source: "headless"`) before the `tool_result`, `agent_end(blocked)`, exit 2, no file written; repo-root `--trust --json` memory search → `mcp__tree_memory_local__search_memory` (direct), classifier `allow`, answer citing the hits, exit 0; an `auto` write outside the cwd → classified dangerous → headless deny (confirms the 206 AC wording is wrong, not the gate); unknown `/skill:` → one line + exit 1; a bad `settings.json` → file + key path, exit 1; README relative links and anchors all resolve; ADR-016 and the glossary entries are in the diff.

Found 4 issues (the `/skill:` message has no skill-dir header and leaves `$ARGUMENTS` literal; the default `codemode.llm.maxCalls` 16 fails the feature's own §10.6 worked example; `--help` is an error; the 206 `[HUMAN]` `auto` AC contradicts the shipped classifier). Filed rollup task: `tasks/215-pa-rejection-tree-agent-harness.md`. The stale harness sentences in `apps/memory/README.md` are out of this feature's files → separate task `tasks/216-memory-readme-harness-mcp-wording.md`. Task 214 (memory-server `readOnlyHint`) scoping confirmed.
Pipeline re-runs from the inner loop with the rollup task; on green, re-run acceptance on the feature.
