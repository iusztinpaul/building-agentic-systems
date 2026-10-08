---
id: 192-text-leg-docs-and-ratio-repin
status: pending
feature: atlas-search-text-leg
---

# ADR-015's Status-line notes on ADR-008 / 012 / 013, the skill / README / runbook / docstring texts that still describe `$text` or `min_text_score`, and the live e2e that re-pins `query.text_min_match_ratio` (lowest 0.1 step separating four off-topic from six on-topic queries) with the evidence in `default.yaml`

Tags: `docs`, `adr`, `evals`, `config`, `e2e`
Depends on: 191
Blocks: —
Implements: ADR-015 §8 (the e2e re-pin) and its "Amendments to apply"

## Problem

After 191 the code has the ratio but the prose does not: ADR-008 §3, ADR-012 §1–§2 and ADR-013 §7 still
describe `$text`, `text_index` and `min_text_score` without a Status-line pointer to ADR-015; the
`tree-memory` skill's read chain 3 says "the text search hits ≥ `query.min_text_score`"; the runbook does
not say that a deploy must be followed by an indexing run before the text leg comes back. And `0.5` is a
guess — ADR-015 §8 pins it by the same on-topic vs off-topic protocol task 183 used, now with a rule that
CAN separate them.

## Scope

**Docs (no code besides the pinned value):**
1. **ADR Status-line notes** — exactly ADR-015's "Amendments to apply" list (ADR-008, ADR-012, ADR-013);
   body text untouched; no other ADR edited (ADR-006 line 183 and ADR-008 line 118 are diagram body
   text).
2. `.agents/skills/tree-memory/SKILL.md` read chain 3 (line 54): "the text search hits ≥
   `query.min_text_score`" → "the text search keeps only rows matching at least
   `ceil(query.text_min_match_ratio × M)` of the question's M content words (`0.5` → half of them)"; the
   rest of the sentence (never threshold the returned `score`) stays.
3. `apps/memory/README.md`: the `search_memory` row (line 464) and the `query` key line (76, if 191 left
   anything stale) describe the ratio; the Embedding-reset paragraph (292, "search runs on the text leg")
   still holds — leave it. `docs/notes/deployment-runbook.md` step 3's indexing paragraph (lines ~70–74)
   gains one sentence: "Re-run it after ANY deploy that changes a search-index definition (ADR-015 added
   `text_search_index`): until the run, `search_memory` answers `search_mode: vector_only`."
4. Docstrings / comments that still say `$text`, `textScore`, `text_index` or `min_text_score` in
   `apps/memory/src` (`rag/types.py`, `rag/search.py`, `entities/memory.py`, `mcp/tools.py` if any) —
   `grep -rn "min_text_score\|textScore\|text_index\b\|\$text" apps/memory/src apps/memory/README.md
   .agents docs/notes tutorials docs/glossary.md` is empty afterwards (`docs/adrs` and `tasks/done`
   excluded).
5. The PR description carries the runbook step verbatim ("run `make memory-run-indexing-pipeline`
   against prod right after merging; the text leg is `vector_only` until then").

**E2E and re-pin (run-pipelines-e2e skill, LOCAL env, the real local corpus; every number in the Log):**
6. `make env-status` → local; `make memory-serve-workflows &` from this checkout; `make
   memory-run-indexing-pipeline`; verify with `mongosh`: `$listSearchIndexes` = `vector_index` +
   `text_search_index`, `getIndexes()` without `text_index`.
7. N depends only on K, and K takes at most M distinct values per query — so do NOT run 10 ratios × 10
   queries (≈ 100 Voyage embeds under `voyage_rpm: 3`). Per query: compute M and the K each 0.1 step
   yields, run ONE `make memory-search QUERY="…"` per DISTINCT K (`TREE_QUERY__TEXT_MIN_MATCH_RATIO=r`
   with any r producing that K; one-word queries = one run; the 8-term query = eight), and read the
   `text leg: N candidate(s), M query term(s), min_should_match=K` line (INFO logs on) — or produce the
   N-per-K column with the exact `$search` pipeline in `mongosh` (no embedding) and confirm only the
   chosen step with `make memory-search`. Queries — off-topic ×4: "dark photon vector-like fermions"
   (task 183 measured a local text-leg top of 1.472 for it, so that paper IS most likely in the local
   corpus — expect to substitute another known-absent topic and name it), "recipe for sourdough bread",
   "zxqv plorb wumbus", "MongoDB Atlas Vector Search scalar binary quantization int8"; on-topic ×6:
   "context engineering", "structured outputs pydantic", "ReAct agent tool calling", "pydantic", "ReAct",
   "Gemini". Record a table `query → M, (K → N) per 0.1 step` (≈ 30 runs). One-word queries are K = 1 at
   every r — expected; they verify the leg still finds single terms.
8. Pick the LOWEST r at which every off-topic N == 0 and every on-topic N ≥ 1. Set it in `default.yaml`,
   `frozen_config.yaml` and `QueryConfig`'s default; rewrite the `default.yaml` comment in the
   `min_vector_score` evidence style (date, corpus size, the table's decisive rows, the chosen step, the
   `TREE_QUERY__…` override, "re-pin with the same protocol"). Show the fused top-5 for the quantization
   query before / after the pin: the physics paper must be absent, or the Log says why it legitimately
   stays (e.g. the vector leg keeps it). If NO step separates them (an off-topic query shares ≥ K terms
   at every r while an on-topic one loses its only hit), ship `0.5` unchanged, write the table and the
   conflict in the Log and the comment, and report it — do not force a value.
9. MCP smoke per the skill: serve `make memory-serve-mcp TRANSPORT=streamable-http`, call
   `search_memory("context engineering")` through the header-carrying `fastmcp.Client` snippet →
   `search_mode: hybrid`; then `drop_search_index("text_search_index")` by hand → the same call answers
   `vector_only` with the server WARN; re-run the indexing pipeline → `hybrid` again (this is the prod
   rollout window, rehearsed locally).

## Acceptance criteria

- [ ] ADR-008, ADR-012 and ADR-013 Status lines carry ADR-015's notes verbatim; `git diff docs/adrs`
      touches only those three Status lines.
- [ ] The grep in Scope 4 is empty; the skill's read chain 3 and the runbook sentence read as specified.
- [ ] The Log holds the `query × r → N` table for all 10 queries, M and K per query, the chosen step (or
      the "no step separates" finding), and the quantization top-5 before / after.
- [ ] `default.yaml`, `frozen_config.yaml` and `QueryConfig` agree on the pinned value;
      `test_app_config.py`'s default assertion matches it; the `default.yaml` comment carries the
      evidence in the `min_vector_score` style.
- [ ] The MCP smoke shows `hybrid` → `vector_only` (index dropped by hand, WARN naming it) → `hybrid`
      (after re-indexing), recorded in the Log.
- [ ] The PR description names the post-merge prod step; `make memory-format-check`, `make
      memory-lint-check`, `make pre-commit`, `make memory-tests` green (env-status local).
- [ ] [HUMAN] Post-merge: run `make memory-run-indexing-pipeline` against prod (`make env-prod`), confirm
      in Atlas → Search & Vector Search that exactly `vector_index` and `text_search_index` exist (2 of
      the M0's 3), and that a `tree-memory` `search_memory` call from Claude Code answers
      `search_mode: hybrid`; evidence in the Log.

## User Stories

### Story: The model reads the skill and does not threshold the score
1. The model reads read chain 3: relevance is gated server-side — the vector bar and the K-of-M word rule
   — so it keeps `top_k=5` and never filters on the returned `score`.

### Story: An operator deploys and the text leg is briefly missing
1. Following the runbook, the operator merges, sees `search_mode: vector_only` in the first Horizon
   answer, runs `make memory-run-indexing-pipeline` against prod as the runbook sentence says, and the
   next answer is `hybrid`.

### Story: A reviewer traces the text leg's design
1. From ADR-008 §3's Status note they land on ADR-015 §2–§4; from ADR-012's note they learn
   `text_index` left the Beanie set and `ensure_indexes` owns both mongot indexes; from ADR-013 §7's
   note that `min_text_score` became `text_min_match_ratio`.

### Story: The evals chapter re-pins the ratio next quarter
1. They open `default.yaml`, read the comment's table excerpt and the protocol, re-run the 10 queries
   with `TREE_QUERY__TEXT_MIN_MATCH_RATIO=r make memory-search …`, and move the value with the same
   evidence shape.

### Story: The Tester reproduces the e2e
1. Following the Log's commands verbatim on the local corpus, the Tester gets the same N for the
   decisive rows (within corpus drift they note), and the same `hybrid → vector_only → hybrid` sequence
   on the MCP smoke.

---

Blocked by: 191

## Log

### [PA] 2026-10-08 19:30 — Grooming

**Summary**
Make every operator- and model-facing text describe the K-of-M text leg, apply ADR-015's Status-line
notes, and pin `query.text_min_match_ratio` with the live 10-query protocol — or report that no step
separates the sets.

**Key decisions**
- The re-pin is this task, not 191's: the value is evidence-owned (ADR-008 §4's rule for `min_vector_score`),
  and 191 must stay shippable with the provisional 0.5 even if the eval is inconclusive.
- "No separating step" is a valid outcome that ships 0.5 and reports — the human re-decides, the task
  does not force a number.
- The post-merge prod indexing run is `[HUMAN]` by necessity (Horizon deploys from `main`); the local MCP
  smoke rehearses the exact window.

**Dependencies**
- 191 — the ratio, the log line, the index drop and the `vector_only` behaviour this task documents and
  measures.

**User stories**
- 5 stories: the skill reader, the deploying operator, the reviewer's trail, the evals re-pin, the
  Tester's reproduction.

Ready for implementation.
