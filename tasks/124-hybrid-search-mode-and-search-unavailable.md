---
id: 124-hybrid-search-mode-and-search-unavailable
feature: mcp-tool-contracts
status: pending
---

# Search mode on `hybrid_search` + `SearchUnavailableError` when both legs fail

Tags: `memory`, `rag`, `retrieval`
Depends on: None
Blocks: #125, #126, #128
Implements: ADR-008 — Decision 3 (retrieval contract: Search mode)

## Scope

`tree/memory/rag/search.py::_vector_search` / `_text_search` each swallow exceptions, WARNING-log
and return `[]`, so two dead legs look like "no matches". Make each leg report its own failure and
let fusion decide the mode.

- `tree/memory/rag/types.py`: `SearchMode = Literal["hybrid", "text_only", "vector_only"]`;
  `class HybridSearchResult(BaseModel)` with `hits: list[ScoredHit]` and `search_mode: SearchMode`
  (field descriptions: `text_only` = vector leg unavailable, `vector_only` = text leg unavailable).
  `RetrievalResult.search_mode: SearchMode = "hybrid"`.
- `tree/memory/rag/search.py`: `class SearchUnavailableError(RuntimeError)` (module-level, with a
  one-line docstring: both legs raised). The two legs return `list[dict] | None` — `None` means
  "the leg raised" (WARNING with `exc_info=True`, replacing today's bare message), `[]` means
  "ran, no hits". `hybrid_search` returns `HybridSearchResult`: both `None` → raise
  `SearchUnavailableError("vector and text search are both unavailable")`; vector `None` →
  `text_only`; text `None` → `vector_only`; else `hybrid`. RRF fusion unchanged.
- `tree/memory/rag/retrieval.py::retrieve_parents`: `result = await hybrid_search(...)`;
  `hits = result.hits`; every returned `RetrievalResult` carries `search_mode=result.search_mode`
  (the `top_k <= 0` early return keeps the default). `SearchUnavailableError` propagates (the MCP
  boundary catches it in #126).
- `tree/memory/graph/retrieval.py` (line ~176): read `.hits`, ignore the mode; `QueryResult` unchanged.
- `tree/cli.py` / `scripts/query_graph.py` rag printer: when `search_mode != "hybrid"`, print ONE
  first line `Search ran <mode> — the other leg was unavailable; results may miss matches.`

## Out of scope
- `outcome` / score gating (#125). Tool-boundary catch and envelope (#126). graphrag `QueryResult` fields.

## Acceptance Criteria

- [ ] Vector leg raising → `HybridSearchResult.search_mode == "text_only"` with the text hits — `tests/unit/memory/rag/test_search.py::TestSearchMode::test_vector_leg_failure_reports_text_only`.
- [ ] Text leg raising → `"vector_only"` — `::test_text_leg_failure_reports_vector_only`.
- [ ] Both legs raising → `SearchUnavailableError` — `::test_both_legs_failing_raises`.
- [ ] Both legs returning (even `[]`) → `"hybrid"`; a leg returning `[]` is NOT a failure — `::test_empty_leg_is_hybrid_not_degraded`.
- [ ] The leg WARNING carries the traceback (`caplog` record `exc_info` set) — `::test_leg_failure_logs_traceback`.
- [ ] `retrieve_parents` copies the mode onto `RetrievalResult` and defaults to `"hybrid"` — `tests/unit/memory/rag/test_retrieval.py::test_search_mode_copied_onto_result`, `::test_top_k_zero_keeps_default_mode`.
- [ ] `tree.memory.graph.retrieval` seeds from `.hits`; existing graph retrieval tests pass unchanged — `tests/unit/memory/graph/test_retrieval.py::test_reads_hits_from_hybrid_search_result`.
- [ ] `RetrievalResult.model_json_schema()` lists `search_mode` — `tests/unit/memory/rag/test_types.py::test_retrieval_result_has_search_mode`.
- [ ] `tests/unit/memory/test_package_layout.py` still green (no new imports across the rag/graph valve).
- [ ] `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green.

## User Stories

### Story: Vector index is mid-rebuild, the user searches anyway
1. `vector_index` was dropped for a dimension change; `$vectorSearch` raises.
2. `retrieve_parents(query="prefect retries")` returns the text-leg parents with `search_mode: "text_only"`.
3. The WARNING log carries the Mongo error traceback; nothing is silently `[]`.

### Story: Mongo is down
1. Both aggregations raise `ServerSelectionTimeoutError`.
2. `hybrid_search` raises `SearchUnavailableError`; `retrieve_parents` propagates it (no fake "0 results").

### Story: Operator queries from the CLI in rag mode during degradation
1. `TREE_MEMORY__MODE=rag make memory-query-graph QUERY="voyage rate limit"` while the vector index is missing.
2. First output line: `Search ran text_only — the other leg was unavailable; results may miss matches.`, then the ranked parents as before.

---

Blocked by: (none)

## Log

### [PA] 2026-09-12 10:10 — Grooming

**Summary**
Legs report their own failure; fusion names the mode; two dead legs raise instead of returning nothing.

**Key decisions**
- `None`-vs-`[]` return on the legs (not exceptions) keeps fusion a pure function and lets "ran, empty" stay `hybrid`.
- Exception lives in `search.py` (where it is raised); shapes in `types.py` (the rag output contract).

**Dependencies**
- None.

**User stories**
- 3 stories covering: degraded vector leg, both legs down, CLI caveat.

Ready for implementation.
