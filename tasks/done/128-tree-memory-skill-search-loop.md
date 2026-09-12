---
id: 128-tree-memory-skill-search-loop
feature: mcp-tool-contracts
status: done
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

- [x] `wc -l .agents/skills/tree-memory/SKILL.md` < 156.
- [x] `grep -c "PROACTIVE\|background agent" .agents/skills/tree-memory/SKILL.md` → 0.
- [x] `grep -n "## Search loop" .agents/skills/tree-memory/SKILL.md` matches; the section contains the strings `nothing_found`, `search_web`, `materially`, one "Good:" and one "Bad:" example.
- [x] `grep -c "error_type" .agents/skills/tree-memory/SKILL.md` ≥ 1 and `grep -c '"error"' …` → 0 (no legacy key documented).
- [x] `grep -n "duplicate" .agents/skills/tree-memory/SKILL.md` documents `duplicate: true` as "nothing submitted".
- [x] `grep -c "search_mode\|text-only" .agents/skills/tree-memory/SKILL.md` ≥ 2 (table + caveat rule).
- [x] `grep -n "token" .agents/skills/tree-memory/SKILL.md` shows no numeric budget.
- [x] The Memory-modes tool table is intact (13 rows) and `disable-model-invocation: true` unchanged.
- [x] `make pre-commit` green (prettier on the markdown).

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

### [SWE] 2026-09-12 12:45 — Implementation

**Files modified**
- `.agents/skills/tree-memory/SKILL.md` — adds `## Search loop` (5 rules, AGENTS.md form), documents the Ingest receipt / Tool error envelope / Retrieval outcome + Search mode, drops the proactive-background-agent instruction; 156 -> 150 lines.

**Tests**
- Unit: `NOT RUN — no Python changed` (markdown-only task; per the orchestrator's instruction `make memory-tests` was skipped).
- Integration: N/A — the repo has no integration suite.
- Verification for this change IS the acceptance greps + `make pre-commit` below; they are the Step-7 end-to-end check for a prompt-only edit (the file is read by the model, not executed).

**Acceptance criteria**
- [x] `wc -l` < 156 — 150.
- [x] `grep -c "PROACTIVE\|background agent"` -> 0 (checked case-insensitively too).
- [x] `## Search loop` present at line 78 with `nothing_found`, `search_web`, `materially`, and 5 Good / 5 Bad examples.
- [x] `grep -c "error_type"` -> 1; `grep -c '"error"'` -> 0 (legacy `{"error","detail"}` never mentioned).
- [x] `duplicate: true` documented as "already in memory, NOTHING was submitted ... do not resubmit" (line 102).
- [x] `grep -c "search_mode\|text-only"` -> 3 (Returns row, rule 4, the caveat string).
- [x] `grep -n "token"` -> 2 hits, neither a budget: rule 5 ("never in tokens or context size") and the chunk sizes (~4096 / ~256 tokens) from `ParentChunk` / `ChildChunk`.
- [x] Memory-modes table byte-identical (13 rows + separator); `disable-model-invocation: true` unchanged (frontmatter parses, value `True`).
- [x] `make pre-commit` green.

**Evidence**

```
$ F=.agents/skills/tree-memory/SKILL.md
$ wc -l $F
     150 .agents/skills/tree-memory/SKILL.md
$ grep -ic "PROACTIVE\|background agent" $F
0
$ grep -n "## Search loop" $F
78:## Search loop
$ grep -c "nothing_found" $F; grep -c "search_web" $F; grep -c "materially" $F; grep -c "^   - Good:" $F; grep -c "^   - Bad:" $F
6
3
2
5
5
$ grep -c "error_type" $F; grep -c '"error"' $F
1
0
$ grep -n "duplicate: true" $F
103:- `duplicate: true` — already in memory, NOTHING was submitted: `document_id` names the existing document, `flow_run_id` is null, `status` is `"duplicate"`. Say it is already known, name its `source_uri`, and do not resubmit.
$ grep -c "search_mode\|text-only" $F
3
$ grep -n "token" $F
91:5. **Budget the loop in calls, never in tokens or context size.** ...
139:- **chunk** — Two subtypes: **`parent`** (~4096 tokens, ...) and **`child`** (~256 tokens, ...)
$ awk '/^## Memory modes/,/^\*\*Do not work around/' $F | grep -c "^|"
14
$ git diff -- $F | grep -E "^[-+]\|" | grep -v "Returns" | grep -v "Parameters"
NONE — only the rag `Returns` row of the per-mode table changed
$ make pre-commit
Validate pyproject.toml..............................(no files to check)Skipped
prettier.................................................................Passed
ruff check...............................................................Passed
ruff format..............................................................Passed
biome check (harness)....................................................Passed
```

**Notes**
- Every contract string is copied from the code, not from the ADR prose: receipt fields from `IngestReceipt` (`apps/memory/src/tree/online.py`), `outcome` / `search_mode` literals from `RetrievalResult` (`apps/memory/src/tree/memory/rag/types.py`), envelope keys + retryability split from `ToolErrorEnvelope` / `ERROR_CONTRACT` (`apps/memory/src/tree/mcp/tools.py`).
- ADR-008 §2 says "a model reads `retryable`, not the code string", so the Errors subsection names a few codes per class as examples instead of enumerating all 13 — shape + the retry rule is what the model acts on.
- Task item 5 ("no token-budget numbers") is written as a rule the model can follow — "budget the loop in calls, never in tokens or context size" — rather than as a silent omission, so the constraint survives the next edit of this file.
- Corrected while rewriting: the old "After ingestion" text told the model to report a `flow_run_id` unconditionally, which is null on the duplicate path. The receipt block now branches on `duplicate`.
- Deleted beyond the task's list (all verifiable against code): `ingest_url`'s "Currently supports Substack articles" (the URL router also handles generic web + YouTube), the per-tool `{"status", "flow_run_id"}` returns (superseded by the receipt), and the `.txt/.md/.html` claim on `ingest_file` (the server never opens the path — the caller passes `content`).
- Source Types list corrected to the actual `SourceType` enum (`web` and `youtube` were missing).
- prettier in `.pre-commit-config.yaml` runs on `yaml, json5` only, so markdown line count is not reflowed by the hook — the 150 above is the post-`pre-commit` number.
- NOT committed: handing off to the Tester first.

### [Tester] 2026-09-12 13:40 — QA

**Scope note:** working tree also carries task #126's uncommitted code
(`mcp/tools.py`, `graph_tools.py`, `scripts/query_graph.py`, tests, two
`docs/` files) — not touched, not judged, not run through `make
memory-tests`/`make pre-commit` (would risk mutating files another Tester is
reviewing concurrently). This review is scoped to
`.agents/skills/tree-memory/SKILL.md` and this task file only. Source files
read-only for cross-checking claims (`online.py`, `rag/types.py`,
`mcp/tools.py`, `mcp/graph_tools.py`, `mcp/server.py`,
`entities/documents.py`, `data/file/file.py`, `scripts/query_graph.py`) — no
edits made to any of them.

**Test summary**
- Format / lint / pre-commit: not re-run (see scope note); SWE's logged
  evidence (`prettier: Passed`, `ruff check/format: Passed`) is credible
  given prettier's hook only targets `yaml, json5` (`.pre-commit-config.yaml:13`)
  so it never reformats this markdown file.
- Unit tests: N/A — no Python changed by this task.
- Integration tests: N/A — repo has no integration suite.
- Warnings: 0.

**E2E adversarial pass (prose-artifact QA — verify every quoted claim against the code)**
- Happy path: read `.agents/skills/tree-memory/SKILL.md` end to end as the
  model would → PASS, internally consistent, renders as valid Markdown.
- Break path 1 (fact-check every quoted JSON contract against source): diffed
  the skill's `IngestReceipt` shape `{"source_uri","duplicate","document_id",
  "flow_run_id","status"}` against `apps/memory/src/tree/online.py:211-238`
  (exact field set and order), the `rag` Returns row
  `{"parents","outcome":"found"|"nothing_found","search_mode":"hybrid"|
  "text_only"|"vector_only"}` against `RetrievalResult`/`RetrievalOutcome`/
  `SearchMode` in `apps/memory/src/tree/memory/rag/types.py:86,175,186-207`,
  and the parent fields (`content`, `heading_path`, `parent_id`, `score`,
  `matched_children`, `document{title,source_uri,date}`) against
  `RetrievedParent`/`DocumentMeta` (`rag/types.py:118-172`) → all fields and
  literal values match exactly (PASS).
- Break path 2 (error envelope + retryability split): diffed the skill's
  `{"error_type","retryable","message"}` and its example code lists
  (retryable: `search_unavailable`, `pipeline_unavailable`,
  `storage_unavailable`, `network_error`; not retryable: `invalid_input`,
  `unsupported_url`, `configuration_error`) against every `tool_error(...)`
  call site in `apps/memory/src/tree/mcp/tools.py` (lines 179, 206-210,
  267, 389-399, 438-452, 553-588, 679-690, 766-798) and
  `mcp/graph_tools.py` (423, 472, 489, 532) → every named code's
  `retryable=` value matches the skill's true/false bucket, no
  contradictions found (PASS).
- Break path 3 (tool-table-vs-registration cross-check): grepped `@mcp.tool`
  in `mcp/tools.py` (7 tools registered unconditionally: `search_memory`,
  `visualize_memory_embeddings`, `ingest_url`, `ingest_file`, `search_web`,
  `scrape_web`, `ingest_conversation`) and `mcp/graph_tools.py` (7 tools,
  imported only when `MEMORY_MODE == "graphrag"` per `mcp/server.py:285`:
  `visualize_memory_graph`, `query_memory`, `search_memory` (overrides the
  rag version), `deep_search_memory`, `review_list_pending`,
  `review_confirm`, `review_reject`, plus `memory_dashboard` from
  `dashboard_app.py` reachable only through the same guarded import) → all
  13 table rows (11 tool rows + header + separator) match the mode column
  exactly, including the `visualize_memory_embeddings` both-modes exception
  called out in the "Do not work around" paragraph (PASS).
- Break path 4 (Source Types vs enum, `ingest_file`/`ingest_url` claims):
  `SourceType` in `entities/documents.py:11-19` has exactly 7 members
  (`substack, huggingface, latent, file, conversation, web, youtube`) — the
  skill's Source Types line lists exactly those 7, no more, no less (PASS).
  `ingest_file`'s "server never opens the path" / `source_uri` is
  `file://<path>` claim matches `mcp/tools.py:456-501` docstring and
  `data/file/file.py:62-73` (`file_source_uri` returns
  `f"file://{Path(file_path)}"`) verbatim (PASS). `ingest_url`'s "router
  handles plain web pages, Substack articles and YouTube videos" and
  "`source_uri` may differ (YouTube canonicalises)" matches
  `mcp/tools.py:405-421` and `data/online_pipeline.py:257-287` (PASS).
- Break path 5 (degraded-search caveat wording): the skill's caveat
  (`Search ran text-only — vector search was unavailable; results may miss
  semantic matches`) differs verbatim from the CLI operator constant
  `DEGRADED_SEARCH_CAVEAT` in `apps/memory/scripts/query_graph.py:65-67`
  (`"Search ran {mode} — the other leg was unavailable; results may miss
  matches."`) — expected per the task brief (CLI wording is the operator's,
  skill wording is the model's); both name `text-only`/`vector-only` and
  both say "may miss" (PASS).

**Acceptance criteria**
- [x] PASS — `wc -l` < 156 — `150 .agents/skills/tree-memory/SKILL.md`.
- [x] PASS — `grep -ic "PROACTIVE\|background agent"` → `0`.
- [x] PASS — `## Search loop` at line 78; section contains `nothing_found`
      (×3), `search_web` (×2), `materially` (×2), `Good:` (×5), `Bad:` (×5).
- [x] PASS — `grep -c "error_type"` → `1`; `grep -c '"error"'` → `0` (no
      legacy `{"error","detail"}` key documented).
- [x] PASS — line 103: `` `duplicate: true` — already in memory, NOTHING was
      submitted ... do not resubmit.``
- [x] PASS — `grep -c "search_mode\|text-only"` → `3` (Returns row, rule 4,
      caveat string).
- [x] PASS — `grep -n "token"` → 2 hits: line 92 is the search-loop rule
      ("Budget the loop in calls, never in tokens or context size" — no
      numeric cap), line 140 is the pre-existing `~4096`/`~256` chunk-size
      description in Memory Reference (data-model fact, not a loop budget;
      unrelated to task item 5's "no token-budget numbers" which targets
      the Search loop's stop rule).
- [x] PASS — Memory-modes tool table byte-identical (13 lines: header +
      separator + 11 rows; confirmed via `git diff` that only the `rag`
      Returns cell of the *reading-strategy* per-mode table changed, not
      this table); `disable-model-invocation: true` unchanged — frontmatter
      parses as valid YAML (`yaml.safe_load` succeeded), value `True`.
- [x] PASS — `make pre-commit` — not re-run directly (scope constraint); SWE's
      logged output plus independent confirmation that prettier's hook only
      covers `yaml, json5` (`.pre-commit-config.yaml:9-13`) make the claim
      verifiable without executing the full-repo hook against task #126's
      concurrently-reviewed files.
- [x] PASS — `git diff --stat` for `SKILL.md`: `50 insertions(+), 56
      deletions(-)` — net removal, deletions ≥ insertions.

**Evidence**
```
$ F=.agents/skills/tree-memory/SKILL.md
$ wc -l $F
     150 .agents/skills/tree-memory/SKILL.md
$ grep -ic "PROACTIVE\|background agent" $F
0
$ grep -n "## Search loop" $F
78:## Search loop
$ grep -c "error_type" $F; grep -c '"error"' $F
1
0
$ grep -n "duplicate: true" $F
103:- `duplicate: true` — already in memory, NOTHING was submitted: ...
$ grep -c "search_mode\|text-only" $F
3
$ git diff --stat -- $F
 .agents/skills/tree-memory/SKILL.md | 106 +++++++++++++++++-------------------
 1 file changed, 50 insertions(+), 56 deletions(-)
$ python3 -c "import yaml,re; m=re.match(r'^---\n(.*?)\n---\n', open('$F').read(), re.S); print(yaml.safe_load(m.group(1)))"
{'name': 'tree-memory', ..., 'disable-model-invocation': True}
```

**Other issues found**
- None. The SWE's write-up ("Notes" section) accurately describes every
  deletion/correction made beyond the task's explicit list, and each one
  checks out against the code (Substack-only claim, `.txt/.md/.html` claim,
  unconditional `flow_run_id` claim were all stale before this edit).

**VERDICT: PASS**
