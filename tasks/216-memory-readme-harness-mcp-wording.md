---
status: pending
feature: memory-readme-harness-wording
---

# Fix the two stale harness sentences in `apps/memory/README.md` (`MCP server` section): the harness is `tree-agent`, it reads `.tree-agent/mcp.json`, never `.mcp.json`

Tags: `docs`, `memory`
Depends on: None
Blocks: —
Implements: ADR-016 §3 (`.tree-agent/mcp.json` is the harness's ONLY MCP source; `.mcp.json` is Claude Code's file) — docs alignment, no code.

## Scope

Two sentences in `apps/memory/README.md` predate the `tree-agent` harness (feature `tree-agent-harness`,
ADR-016) and now mislead a reader who comes from the root README:

- line ~436: "Expose the memory to any MCP-aware client (Claude Code, Claude Desktop, Cursor, the bundled
  harness):" — the harness has a name, **tree-agent**.
- line ~445: "The repo-root `.mcp.json` already wires this up — Claude Code and the harness auto-spawn it.
  No extra setup needed from those clients." — `tree-agent` never reads `.mcp.json` (ADR-016 §3; root
  README line 22); it spawns the server from the repo-root `.tree-agent/mcp.json` when the session cwd is
  the trusted repo root.

Edit only those two sentences (plus any immediately adjacent clause that repeats the claim). Nothing else
in `apps/memory` changes; no code, no tests. Use the glossary's **tree-agent** term verbatim.

Replacement text:

- 436 → "Expose the memory to any MCP-aware client (Claude Code, Claude Desktop, Cursor, and this repo's
  harness, `tree-agent`):"
- 445 → "Two client configs point at it: the repo-root `.mcp.json` for Claude Code (which spawns the server
  itself), and `.tree-agent/mcp.json` for `tree-agent`, which spawns it when the session cwd is the trusted
  repo root (see [`apps/harness/README.md#mcp`](../harness/README.md#mcp)). No extra setup from either
  client."

## Acceptance Criteria

- [ ] `grep -n "bundled harness\|the harness auto-spawn" apps/memory/README.md` matches nothing.
- [ ] `grep -n "tree-agent" apps/memory/README.md` matches the two edited sentences (and the existing line 5 pointer); the relative link `../harness/README.md#mcp` resolves to an existing heading.
- [ ] `git diff --stat apps/memory/README.md` is the only change; `make pre-commit` (prettier on markdown) is green.

## User Stories

### Story: Reader follows the memory README to connect an agent
1. User reads `apps/memory/README.md` → "MCP server".
2. The text names `tree-agent` and tells them which file each client reads (`.mcp.json` → Claude Code, `.tree-agent/mcp.json` → `tree-agent`).
3. User runs `make harness-run PROMPT="…" ARGS="--cwd ../.. --trust"` from the root README's step 7 and the memory tools appear — the two READMEs agree.

---

Blocked by: (none)

## Log

### [PA] 2026-10-25 05:35 — Grooming

**Summary**
Docs-only: two sentences in the memory README still describe the pre-`tree-agent` harness and claim the
harness reads `.mcp.json`. Filed as a separate task because feature `tree-agent-harness` deliberately
touches nothing under `apps/memory` (found during that feature's acceptance review, 213's Tester note).

**Key decisions**
- Separate task, not part of rollup 215: different app, different owner of the file.
- No ADR/glossary change: ADR-016 §3 already records the decision this text catches up with.

**Dependencies**
- None.

**User stories**
- 1 story: a reader reconciling the two READMEs.

Ready for implementation.
