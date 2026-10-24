---
id: 213-harness-docs-readmes-and-stack-note
status: pending
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

- [ ] Every listed root README line is rewritten; `grep -n -i "ink\|bun\|ripgrep\|sub-agent\|shell hook\|auto-spawn\|\.mcp\.json.*harness" README.md`
      returns only lines that describe Claude Code's `.mcp.json`, none that describe the harness.
- [ ] `apps/harness/README.md` has sections for install, CLI, config, MCP (schema + committed example +
      hosted recipe), permissions, exposure, codemode/`llm()`, compaction, sessions, skills, tests, cadence;
      every flag in spec §15 appears in its table; every `settings.json` key in §16 appears.
- [ ] `docs/notes/modern-typescript-stack.md` starts with the two-line pointer to ADR-016 and is otherwise
      byte-identical (`git diff --numstat` shows `2 0`).
- [ ] All relative links in both READMEs resolve (a link check over `README.md` and `apps/harness/README.md`).
- [ ] `make help` output is unchanged by this task.
- [ ] [HUMAN] Following only `README.md` + `apps/harness/README.md` on a machine without pnpm, the human
      reaches a working `tree-agent "say hi"` and a memory search through `tree-memory-local`.
- [ ] `make harness-lint-fix && make harness-lint-check && make harness-typecheck && make harness-tests &&
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
