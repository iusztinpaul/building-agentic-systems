---
id: 125-retrieval-outcome-and-min-vector-score
feature: mcp-tool-contracts
status: pending
---

# Retrieval outcome (`found` / `nothing_found`) + `query.min_vector_score` gate on the vector leg

Tags: `memory`, `rag`, `config`
Depends on: #124
Blocks: #126, #128
Implements: ADR-008 — Decision 3 (retrieval contract: Retrieval outcome) and Decision 4 (`min_vector_score` provisional)

## Scope

RRF fused scores are rank-based and only comparable within one query, so "nothing relevant" cannot
be read off `ScoredHit.score`. Gate the VECTOR leg on `vectorSearchScore` before fusion; keep every
`$text` hit (a lexical match is a match; `textScore` is not normalisable).

- `tree/config/app_config.py::QueryConfig`: `min_vector_score: float = Field(0.65, ge=0.0, le=1.0)`.
  `apps/memory/configs/default.yaml` `query:` block gains `min_vector_score: 0.65` with a comment:
  Atlas normalises cosine to `(1 + cos) / 2`; PROVISIONAL — pinned by the e2e below, owned by
  Chapter 7's evals; override `TREE_QUERY__MIN_VECTOR_SCORE=…`. Same comment, shorter, in code.
- `tree/memory/rag/search.py::_vector_search`: after the aggregate, drop rows with
  `_search_score < app_config.query.min_vector_score`; log INFO
  `vector leg: %d candidate(s), %d kept at min_vector_score=%.2f (top=%.3f)` (the e2e reads the
  top score from this line). A leg that raised still returns `None` (#124) — the gate never runs on
  an unavailable leg, so `text_only` stays "degraded", never "filtered".
- `tree/memory/rag/types.py`: `RetrievalOutcome = Literal["found", "nothing_found"]`;
  `RetrievalResult.outcome: RetrievalOutcome = "found"`.
- `tree/memory/rag/retrieval.py::retrieve_parents`: `outcome = "nothing_found"` iff
  `result.hits` is empty after gating (computed on hits, before grouping — orphans dropped by
  `group_children_by_parent` keep `found`, with a WARNING as today). The answer then is
  `RetrievalResult(parents=[], outcome="nothing_found", search_mode=…)`. The `top_k <= 0` early
  return keeps both defaults (no search ran).
- rag `search_memory` docstring (`mcp/tools.py`): the answer is
  `{"parents": [...], "outcome": "found" | "nothing_found", "search_mode": …}`; an empty memory or
  an off-topic query answers `nothing_found`.

**Live acceptance (this task, before hand-off):** run the `run-pipelines-e2e` skill in `rag`
mode on the local stack: ingest one document, index, then `search_memory` with a nonsense query
(`"zxqv plorb wumbus"`) → `outcome: "nothing_found"`, and a query about the ingested text →
`"found"`. Record BOTH observed `top=` scores from the INFO line in `## Log`. If either check
fails at 0.65, move the default to the nearest 0.05 step that passes both and log the change.

## Out of scope
- Gating the text leg. Gating graphrag seeds (`node_filter={}` callers pass through the same
  vector-leg gate — acceptable and noted, but `QueryResult` reports no outcome).
- Tuning beyond the two-query pin — Chapter 7 evals.

## Acceptance Criteria

- [ ] `app_config.query.min_vector_score == 0.65` by default; `TREE_QUERY__MIN_VECTOR_SCORE=0.9` overrides; `1.5` fails validation — `tests/unit/config/test_app_config.py::TestQueryConfig::test_min_vector_score_default_override_and_bounds`.
- [ ] Vector rows with `_search_score` below the knob are absent from fusion; rows at exactly the knob are kept — `tests/unit/memory/rag/test_search.py::TestMinVectorScore::test_drops_vector_hits_below_threshold`, `::test_keeps_hit_at_threshold`.
- [ ] Text hits are never gated (a `_search_score` of `0.1` on the text leg survives) — `::test_text_hits_are_not_gated`.
- [ ] The INFO line reports candidates, kept count, knob and top score — `::test_logs_candidates_kept_and_top_score`.
- [ ] `retrieve_parents` → `outcome == "nothing_found"` and `parents == []` when both legs return zero hits; `"found"` when any hit exists — `tests/unit/memory/rag/test_retrieval.py::TestOutcome::test_nothing_found_when_no_hits`, `::test_found_with_hits`.
- [ ] `text_only` + zero text hits → `nothing_found` with `search_mode == "text_only"` (the mode is the caveat, the outcome is still honest) — `::test_degraded_and_empty_reports_both`.
- [ ] Live e2e pin recorded in `## Log`: nonsense query `nothing_found` (top score noted), on-topic query `found` (top score noted); knob value final.
- [ ] `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green.

## User Stories

### Story: User asks about something never stored
1. Memory holds three Substack posts about Prefect. Assistant calls `search_memory(query="best sourdough hydration")`.
2. Answer: `{"parents": [], "outcome": "nothing_found", "search_mode": "hybrid"}`.
3. The assistant says "Not in memory — I searched for sourdough hydration" instead of listing unrelated Prefect passages.

### Story: User asks an on-topic question
1. `search_memory(query="how does the coordinator shard documents?")`.
2. Answer has `outcome: "found"` and ≥ 1 parent whose `matched_children` quote the sharding passage.

### Story: A rare term only matches lexically
1. The query is an exact identifier (`"memory-extract-etl-worker"`) whose embedding similarity is weak (< 0.65) but the `$text` leg matches.
2. Vector candidates are dropped; the text hit survives; `outcome: "found"`.

### Story: Operator raises the bar for a noisy corpus
1. `TREE_QUERY__MIN_VECTOR_SCORE=0.75` exported in the MCP-serving shell.
2. The INFO line reads `… kept at min_vector_score=0.75 …`; borderline results vanish.

---

Blocked by: #124

## Log

### [PA] 2026-09-12 10:15 — Grooming

**Summary**
An honest empty answer: vector hits are gated on the only comparable score, text hits are kept, and the result says `nothing_found` when nothing survived.

**Key decisions**
- Bar on `vectorSearchScore`, never on RRF (ADR-008 §3); text leg ungated.
- 0.65 is provisional and pinned by two live queries recorded in this task's log; evals (Chapter 7) own it afterwards.
- Outcome computed on hits before grouping — orphans are a data problem, not "nothing found".

**Dependencies**
- #124 — `HybridSearchResult` / `search_mode` (the gate must not fire on an unavailable leg).

**User stories**
- 4 stories covering: off-topic, on-topic, lexical-only, operator override.

Ready for implementation.
