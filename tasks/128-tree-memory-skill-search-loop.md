---
id: 128-tree-memory-skill-search-loop
feature: mcp-tool-contracts
status: pending
---

# `tree-memory` skill: Search loop stop rule, receipt/error/outcome contracts, net line count down

Tags: `docs`, `skill`
Depends on: #122, #124, #125, #126
Blocks: —
Implements: ADR-008 — Decisions 1–3 as the model reads them

## Scope

`.agents/skills/tree-memory/SKILL.md` (156 lines today). Touch the skill ONCE for the whole
feature; server `instructions` in `mcp/server.py` stay untouched; `disable-model-invocation: true` stays.

**Add — `## Search loop` (after "Reading Strategy"), with the AGENTS.md rule form (rule + why +
one good and one bad example each):**
1. At most 3 `search_memory` calls per user question; every retry must change the query
   MATERIALLY — different entities or angle, not synonyms. Good: `"Prefect deployment slots"` →
   `"free-tier limit five deployments"`. Bad: `"Prefect deployment slots"` → `"Prefect deployment slot"`.
2. `outcome: "nothing_found"` → at most ONE materially different retry, then answer "Not in memory"
   naming what was searched. Never fall through to `search_web` unless the user asked for the web.
3. Stop early once the answer is covered — 3 is a budget, not a target.
4. `search_mode != "hybrid"` → prefix ONE caveat line (`Search ran text-only — vector search was
   unavailable; results may miss semantic matches`) and offer to retry later; then proceed.
5. No token-budget numbers anywhere (unenforceable).

**Update:**
- Reading table, `rag` "Returns": `{"parents": [...], "outcome": "found" | "nothing_found", "search_mode": "hybrid" | "text_only" | "vector_only"}`.
- Writing: all three ingest tools answer the receipt `{"source_uri", "duplicate", "document_id", "flow_run_id", "status"}` (+ `url` / `file_path`); `duplicate: true` means "already in memory, nothing submitted — tell the user, do not resubmit". "After ingestion": confirm later with `search_memory` for the title; a `nothing_found` right after the submit means "not written yet".
- Errors (one short subsection under Presenting): every tool may answer `{"error_type", "retryable", "message"}`; retry once only when `retryable` is true; when false, change the input or stop and relay `message`.
- Presenting: "If results are empty…" → "On `nothing_found`, say so and name what was searched (see Search loop)".

**Delete:** the `PROACTIVE USE … background agent` sentences from the `description` frontmatter and
the `ingest_conversation` bullet (the SessionEnd hook, #129, owns session persistence); the
`ingest_url` "Currently supports Substack articles" line (the URL router handles web + YouTube
too); duplicated sentences between Reading/Writing/Presenting ("no counts" appears three times —
keep one).

**Budget:** final `wc -l` strictly below 156 — removing beats adding.

## Out of scope
- `mcp/server.py` instructions. `run-pipelines-e2e` skill (touched in #129). `docs/` prose.

## Acceptance Criteria

- [ ] `wc -l .agents/skills/tree-memory/SKILL.md` < 156.
- [ ] `grep -c "PROACTIVE\|background agent" .agents/skills/tree-memory/SKILL.md` → 0.
- [ ] `grep -n "## Search loop" .agents/skills/tree-memory/SKILL.md` matches; the section contains the strings `nothing_found`, `search_web`, `materially`, one "Good:" and one "Bad:" example.
- [ ] `grep -c "error_type" .agents/skills/tree-memory/SKILL.md` ≥ 1 and `grep -c '"error"' …` → 0 (no legacy key documented).
- [ ] `grep -n "duplicate" .agents/skills/tree-memory/SKILL.md` documents `duplicate: true` as "nothing submitted".
- [ ] `grep -c "search_mode\|text-only" .agents/skills/tree-memory/SKILL.md` ≥ 2 (table + caveat rule).
- [ ] `grep -n "token" .agents/skills/tree-memory/SKILL.md` shows no numeric budget.
- [ ] The Memory-modes tool table is intact (13 rows) and `disable-model-invocation: true` unchanged.
- [ ] `make pre-commit` green (prettier on the markdown).

## User Stories

### Story: Assistant searches for something not in memory
1. User: "What did I decide about the sourdough starter?" Assistant calls `search_memory("sourdough starter decision")` → `nothing_found`.
2. Assistant retries once with a different angle (`"bread baking notes"`) → `nothing_found`.
3. Assistant answers: "Not in memory — I searched for the sourdough starter decision and bread baking notes." It does NOT call `search_web`.

### Story: Assistant answers from a degraded search
1. `search_memory` answers `search_mode: "text_only"` with two parents.
2. Assistant's reply starts with `Search ran text-only — vector search was unavailable; results may miss semantic matches`, then the passages, then offers to retry later.

### Story: Assistant hits a duplicate on ingest
1. User shares a URL already in memory; `ingest_url` answers `duplicate: true`.
2. Assistant says the page is already in memory (with its `source_uri`) and submits nothing else.

### Story: Assistant gets a retryable error
1. `ingest_url` answers `error_type: pipeline_unavailable, retryable: true`.
2. Assistant retries once; on a second failure it relays `message` and stops.

---

Blocked by: #122, #124, #125, #126

## Log

### [PA] 2026-09-12 10:30 — Grooming

**Summary**
The skill teaches the stop rule and the three new contracts, and gets shorter doing it.

**Key decisions**
- Rules follow AGENTS.md's form (rule + why + good/bad) so they are checkable, not vibes.
- Session persistence leaves the skill entirely — the hook owns it.

**Dependencies**
- #122, #124, #125, #126 — the contracts it documents must exist first.

**User stories**
- 4 stories covering: nothing found, degraded, duplicate, retryable error.

Ready for implementation.
