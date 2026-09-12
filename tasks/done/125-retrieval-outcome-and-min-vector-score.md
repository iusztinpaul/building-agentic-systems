---
id: 125-retrieval-outcome-and-min-vector-score
feature: mcp-tool-contracts
status: done
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

- `tree/config/app_config.py::QueryConfig`: `min_vector_score: float = Field(0.75, ge=0.0, le=1.0)`.
  `apps/memory/configs/default.yaml` `query:` block gains `min_vector_score: 0.75` with a comment:
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

- [x] `app_config.query.min_vector_score == 0.75` by default (0.65 failed the live pin — see Log); `TREE_QUERY__MIN_VECTOR_SCORE=0.9` overrides; `1.5` fails validation — `tests/unit/config/test_app_config.py::TestQueryConfig::test_min_vector_score_default_override_and_bounds`.
- [x] Vector rows with `_search_score` below the knob are absent from fusion; rows at exactly the knob are kept — `tests/unit/memory/rag/test_search.py::TestMinVectorScore::test_drops_vector_hits_below_threshold`, `::test_keeps_hit_at_threshold`.
- [x] Text hits are never gated (a `_search_score` of `0.1` on the text leg survives) — `::test_text_hits_are_not_gated`.
- [x] The INFO line reports candidates, kept count, knob and top score — `::test_logs_candidates_kept_and_top_score`.
- [x] `retrieve_parents` → `outcome == "nothing_found"` and `parents == []` when both legs return zero hits; `"found"` when any hit exists — `tests/unit/memory/rag/test_retrieval.py::TestOutcome::test_nothing_found_when_no_hits`, `::test_found_with_hits`.
- [x] `text_only` + zero text hits → `nothing_found` with `search_mode == "text_only"` (the mode is the caveat, the outcome is still honest) — `::test_degraded_and_empty_reports_both`.
- [x] Live e2e pin recorded in `## Log`: nonsense query `nothing_found` (top score noted), on-topic query `found` (top score noted); knob value final.
- [x] `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green.

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

### [SWE] 2026-09-12 15:55 — Implementation

**Files modified**
- `apps/memory/src/tree/config/app_config.py` — `QueryConfig.min_vector_score: float = Field(0.75, ge=0.0, le=1.0)` + docstring (why the bar is on `vectorSearchScore`, why the value is provisional).
- `apps/memory/configs/default.yaml` — `query.min_vector_score: 0.75` with the Atlas-normalisation / PROVISIONAL / override comment.
- `apps/memory/tests/unit/config/fixtures/frozen_config.yaml` — same key, so the frozen loader fixture stays a faithful copy.
- `apps/memory/src/tree/memory/rag/search.py` — `_gate_vector_candidates`: drops rows below the knob AFTER the #124 availability probe, logs the INFO line, and is called only from the non-empty branch of `_vector_search`.
- `apps/memory/src/tree/memory/rag/types.py` — `RetrievalOutcome = Literal["found", "nothing_found"]`; `RetrievalResult.outcome` (declared between `parents` and `search_mode`, so the JSON key order matches ADR-008).
- `apps/memory/src/tree/memory/rag/retrieval.py` — `outcome` computed on `result.hits` before grouping, carried by both return paths; the no-hits INFO line now names it.
- `apps/memory/src/tree/mcp/tools.py` — rag `search_memory` docstring: the answer shape now names BOTH `outcome` and `search_mode` (the #124 deferral). No other part of the file touched (#126 owns the error paths).
- `apps/memory/tests/unit/memory/conftest.py` — `FakeMemoryCollection` now emulates `$addFields: {_search_score: {$meta: …}}` (copy, not in-place) instead of silently dropping the stage; a row may declare its own score, otherwise `0.9`.
- `apps/memory/tests/unit/memory/rag/test_search.py` — `TestMinVectorScore` (6 tests).
- `apps/memory/tests/unit/memory/rag/test_retrieval.py` — `TestOutcome` (5 tests).
- `apps/memory/tests/unit/config/test_app_config.py` — `TestQueryConfig` (3 tests).
- `apps/memory/tests/unit/mcp/test_tools.py` — the rag no-hits test now asserts the three-key answer with `outcome: "nothing_found"`.

**Tests**
- Unit: 2980 passing, 0 failing (`make memory-tests`). Integration: N/A — the repo has no integration suite (AGENTS.md); e2e below instead.
- Red-checked, not assumed: with the gate call replaced by `return results`, 4 of the 6 `TestMinVectorScore` tests failed; with `outcome` hardcoded to `"found"`, 2 of the `TestOutcome` tests failed. Both reverted.

**Acceptance criteria**
- [x] Default / override / bounds — `tests/unit/config/test_app_config.py::TestQueryConfig::test_min_vector_score_default_override_and_bounds` (default now **0.75**, see the pin below).
- [x] Below-bar rows dropped, at-bar rows kept — `test_search.py::TestMinVectorScore::test_drops_vector_hits_below_threshold`, `::test_keeps_hit_at_threshold`.
- [x] Text hits ungated — `::test_text_hits_are_not_gated` (the row keeps its embedding, so BOTH legs return it: the vector copy is dropped at `_search_score=0.1`, the text copy survives — that is User Story 3, and it is the test that would catch a gate moved to post-fusion).
- [x] INFO line — `::test_logs_candidates_kept_and_top_score`; plus `::test_fully_gated_leg_stays_hybrid_and_never_probes` (a leg gated to nothing is `hybrid`, never `text_only`, and does not run the #124 index probe) and `::test_operator_override_raises_the_bar` (User Story 4).
- [x] `nothing_found` / `found` — `test_retrieval.py::TestOutcome::test_nothing_found_when_no_hits`, `::test_found_with_hits`; plus `::test_hits_whose_parents_are_missing_stay_found` (orphans are a data problem) and `::test_top_k_zero_keeps_the_found_default`.
- [x] Degraded + empty — `::test_degraded_and_empty_reports_both`.
- [x] Live pin — below.
- [x] QA loop green.

**Evidence — live acceptance (local stack, `rag` mode, user `paul@example.com`)**

Setup: restarted `make memory-serve-workflows` from this branch with `TREE_MEMORY__MODE=rag`; ingested
`sourdough-hydration.md` (329 words, deliberately off-corpus) → 1 `document` + 1 parent + 2 embedded
children; ran the indexing phase (`vector_index` was ABSENT beforehand — the local mongot had been
restarted — and the indexing run recreated it, 1024-d cosine with the five filter paths).

**The pin — ADR-008 §4's proposed 0.65 FAILED and the knob moved one 0.05 step up, to 0.75:**

| query | candidates | top= | kept @0.65 | kept @0.75 | outcome @0.75 |
|---|---|---|---|---|---|
| `zxqv plorb wumbus` (nonsense) | 40 | **0.728** | 40 | 0 | `nothing_found` |
| `what hydration percentage should a sourdough dough use?` (on-topic) | 40 | **0.882** | 40 | 2 | `found` |

At 0.65 the nonsense query kept every candidate, so `nothing_found` was unreachable — out-of-vocabulary
tokens still embed near the corpus centroid, and Atlas's `(1 + cos)/2` compresses everything into the
upper half of the range. 0.75 is the nearest step that clears 0.728 while leaving the on-topic leg alive
(`kept=2`, not just a text-leg `found`).

**Margins, for whoever tunes this next (Chapter 7):** the nonsense side has **0.022** of headroom
(0.728 → 0.75) and the on-topic side **0.132** (0.75 → 0.882). The risk is entirely on the nonsense
side: a corpus or an embedding-model change that lifts off-topic scores by 2 points makes
`nothing_found` unreachable again, and the symptom is a confident answer built from unrelated
passages. One pin on one corpus is what this number is — not a calibration.

```
$ TREE_MEMORY__MODE=rag make memory-query-graph QUERY="zxqv plorb wumbus"
vector leg: 40 candidate(s), 0 kept at min_vector_score=0.75 (top=0.728)
No child hits for query (outcome=nothing_found, search_mode=hybrid): zxqv plorb wumbus
No results.

$ TREE_MEMORY__MODE=rag make memory-query-graph QUERY="what hydration percentage should a sourdough dough use?"
vector leg: 40 candidate(s), 2 kept at min_vector_score=0.75 (top=0.882)
[0.033] Sourdough hydration, measured — Sourdough hydration, measured
...

# MCP surface (TRANSPORT=http, FASTMCP_PORT=8099, MCP_SKIP_INDEX_BOOTSTRAP=true)
$ uv run fastmcp call http://127.0.0.1:8099/mcp --auth none search_memory query="zxqv plorb wumbus" top_k=3
{ "parents": [], "outcome": "nothing_found", "search_mode": "hybrid" }
   server log: vector leg: 12 candidate(s), 0 kept at min_vector_score=0.75 (top=0.728)

$ uv run fastmcp call … search_memory query="what hydration percentage should a sourdough dough use?" top_k=2
outcome: found | search_mode: hybrid | parents[0]: "Sourdough hydration, measured" (2 matched children)
   server log: vector leg: 8 candidate(s), 2 kept at min_vector_score=0.75 (top=0.882)
```

The CLI prints no `outcome` (it prints `No results.`); that surface is nobody's task in this feature, so
the outcome evidence comes from the MCP answer, which IS the ADR-008 contract.

**Notes**
- **PA follow-up — two docs hardcode the old value and I am read-only on both:** `docs/adrs/008_mcp_tool_contract.md` §4 ("`min_vector_score = 0.65` is provisional") and `docs/glossary.md` line 35, row **Retrieval outcome** (twice: "default `0.65`" and "`0.65` is PROVISIONAL"). The live pin this task owns moved it to 0.75; both need the same edit. Grepped `docs/ tasks/ .agents/ .claude/` for `0.65` — those are the ONLY two hits outside this task file (`tasks/128` does not quote the number), so no further doc drift to chase.
- User Story 4's example value (`TREE_QUERY__MIN_VECTOR_SCORE=0.75`) went stale when the pin moved: 0.75 is now the shipped default, so `test_operator_override_raises_the_bar` uses 0.85 to keep "override" meaning override. The story's intent (an override changes the INFO line and borderline results vanish) is what the test asserts.
- The e2e document's `source_uri` is `file:///private/tmp/claude-501/…/scratchpad/e2e/sourdough-hydration.md` — a SESSION-TEMP path. Its rows live in Mongo, so both queries reproduce as recorded, but a re-ingest of that URI will not (the file is gone with the session).
- `node_filter={}` (graphrag seeds) passes through the same vector gate — entity rows embed short names and can score low. Accepted and named in this task's "Out of scope"; flagging it out loud so it reads as intent, not oversight. The MAGNITUDE grew with the pin: at the shipped 0.75 more seeds drop than ADR-008 §4's 0.65 assumed, and the whole noise band measured here (`[0.65, 0.728]`) now falls on the dropped side. `QueryResult` still reports no outcome (Chapter 8).
- `FakeMemoryCollection` change is deliberate scope: without the `$addFields` emulation every gate test would have passed against rows that carry no `_search_score` — i.e. for the wrong reason. Production reads `doc["_search_score"]` with no default: the field is added three lines above in the same pipeline, and a fail-open default would let a future pipeline edit disable the gate silently.
- Infra left as found: docker `tree-prefect-worker` was stopped for the run (it executes the MAIN checkout baked into its image and grabbed the first dispatch, failing on an invalid Gemini key in `graphrag` mode) and has been **restarted**; the MCP server was stopped; `make memory-serve-workflows` is left RUNNING (it was running when this task started), now from this branch in `rag` mode. The `memory` collection was NOT dropped (task #127 reads it live); the e2e document is additive (4 rows).

### [Tester] 2026-09-12 16:40 — QA

**Test summary**
- Format / lint / pre-commit: PASS (`make memory-format-check`, `make memory-lint-check`, `make pre-commit` all green)
- Unit tests: 2980 passed / 0 failed (`make memory-tests`, env target `local`)
- Integration tests: N/A — no integration suite in this repo (AGENTS.md); e2e run instead (below)
- Warnings: pytest summary line reads `2980 passed` with no warnings collected by pytest itself. One `UserWarning` ("Core Pydantic V1 functionality isn't compatible with Python 3.14 or greater") prints at interpreter start from `site-packages/opik/rest_api/core/pydantic_utilities.py:13` — reproduced identically on an unrelated `make memory-query-graph` run with no test collection involved, so it is import-time noise from the `opik` dependency on Python 3.14, not project code and not introduced by this diff.
- code-review plugin: enabled in `.claude/settings.json` but exposed only as a slash command (`/code-review`) with no corresponding tool in this agent's toolset — not invocable here. Substituted a full manual read of every changed file (`git diff` on all 6 source files + 7 test/fixture files) in its place.

**E2E adversarial pass**
- Happy path 1 (nonsense query, local `rag` stack): `TREE_MEMORY__MODE=rag make memory-query-graph QUERY="zxqv plorb wumbus"` → `vector leg: 40 candidate(s), 0 kept at min_vector_score=0.75 (top=0.728)` / `No child hits for query (outcome=nothing_found, search_mode=hybrid)` / `No results.` — exact match to the SWE's recorded pin (PASS)
- Happy path 2 (on-topic query): `TREE_MEMORY__MODE=rag make memory-query-graph QUERY="what hydration percentage should a sourdough dough use?"` → `vector leg: 40 candidate(s), 2 kept at min_vector_score=0.75 (top=0.882)`, top hit `[0.033] Sourdough hydration, measured` — exact match to the SWE's recorded pin (PASS)
- Operator override, live stack: `TREE_MEMORY__MODE=rag TREE_QUERY__MIN_VECTOR_SCORE=0.9 make memory-query-graph QUERY="what hydration percentage should a sourdough dough use?"` → `vector leg: 40 candidate(s), 0 kept at min_vector_score=0.90 (top=0.882)` — top score unchanged, kept count and knob both reflect the override (PASS)
- Break path 1 (state edge — vector leg gated to zero on a queryable index, text leg has a hit): ad-hoc script against `hybrid_search` with a `FakeMemoryCollection` holding one embedded row scored 0.30 (vector-only, content that does not match the query) and one non-embedded row whose content matches the query text → `HITS: ['c1'], MODE: hybrid`; fed the equivalent `HybridSearchResult(hits=[hit], search_mode="hybrid")` through `retrieve_parents` with a fake parent/document row → `outcome: found, search_mode: hybrid, n_parents: 1`. Gating never flips the mode. (PASS)
- Break path 2 (failure mode — vector leg unavailable): ad-hoc script wrapping `FakeMemoryCollection.aggregate` to raise `RuntimeError` only for the `$vectorSearch` pipeline (text pipeline untouched) → `HITS: ['c1'], MODE: text_only`, captured log confirms `"vector leg:" in log == False` (only `"Vector search leg unavailable; the query runs text-only"` logged) — the gate genuinely never runs on a `None` leg. (PASS)
- Break path 3 (boundary — exact-threshold score at the real shipped default): ad-hoc script with two vector-only rows at `_search_score=0.75` and `0.7499` against the actual `app_config.query.min_vector_score` (no patching) → `HITS: ['at']` only, `below` dropped. Matches AC "rows at exactly the knob are kept." (PASS)
- Break path 4 (hostile-adjacent / fusion edge — text-leg row with a very low `_search_score`): ad-hoc script, one row present in both legs with `_search_score=0.1`, query text matches its content exactly → `HITS: ['c0'], MODE: hybrid` — the vector copy at 0.1 is gated, the text copy survives untouched. (PASS)
- `top_k <= 0`: `tests/unit/memory/rag/test_retrieval.py::TestOutcome::test_top_k_zero_keeps_the_found_default` asserts `hybrid_search` is never called and `outcome == "found"`; independently confirmed by reading `retrieval.py:92-97` — the early return constructs a bare `RetrievalResult()`, whose Pydantic defaults are `outcome="found"`, `search_mode="hybrid"`. (PASS)
- Config fail-loud, non-numeric override: `TREE_QUERY__MIN_VECTOR_SCORE=abc` + `load_app_config()` → `pydantic_core.ValidationError: query.min_vector_score / Input should be a valid number, unable to parse string as a number`, raised at module import (`app_config = load_app_config()` at `app_config.py:725`). (PASS)
- Config fail-loud, out-of-bounds override: `TREE_QUERY__MIN_VECTOR_SCORE=1.5` → `ValidationError: query.min_vector_score / Input should be less than or equal to 1`. (PASS)
- Additional check (advisor-directed): the other `hybrid_search` caller, graphrag seeds (`node_filter={}`), was not exercised in the original pass. `TREE_MEMORY__MODE=graphrag make memory-query-graph QUERY="what hydration percentage should a sourdough dough use?"` → `vector leg: 10 candidate(s), 2 kept at min_vector_score=0.75 (top=0.882)`, `Graph expansion: 6 seed(s) → 89 nodes, 85 edges`. Non-zero kept on an on-topic query, so per the task's explicit "Out of scope" (graphrag seed loss accepted) this is a note, not a blocker — recorded below.

**Acceptance criteria** (all independently re-verified with fresh commands/scripts, not inherited from the SWE's pre-checked boxes)
- [x] PASS — `app_config.query.min_vector_score == 0.75` by default; `TREE_QUERY__MIN_VECTOR_SCORE=0.9` overrides; `1.5` fails validation — `tests/unit/config/test_app_config.py::TestQueryConfig::test_min_vector_score_default_override_and_bounds` passes in `make memory-tests`; independently reran the override live against the local stack (0.9 → 0 kept, top unchanged) and the bounds failure at the CLI (`ValidationError`, both `1.5` and `abc`); default 0.75 also confirmed in `apps/memory/configs/default.yaml:122`, `apps/memory/src/tree/config/app_config.py:307`, and `apps/memory/tests/unit/config/fixtures/frozen_config.yaml` (`test_min_vector_score_loaded_from_frozen_config`).
- [x] PASS — vector rows below the knob absent from fusion, at-knob rows kept — `test_search.py::TestMinVectorScore::test_drops_vector_hits_below_threshold`, `::test_keeps_hit_at_threshold` pass; independently reproduced the inclusive boundary at the real 0.75 default (0.75 kept, 0.7499 dropped) with an unpatched `app_config`.
- [x] PASS — text hits never gated (0.1 score survives) — `::test_text_hits_are_not_gated` passes; independently reproduced with a fresh script (`HITS: ['c0'], MODE: hybrid`).
- [x] PASS — INFO line reports candidates/kept/knob/top — `::test_logs_candidates_kept_and_top_score` passes; independently observed identical format live: `vector leg: 40 candidate(s), 0 kept at min_vector_score=0.75 (top=0.728)`.
- [x] PASS — `retrieve_parents` → `nothing_found`/`parents=[]` on zero hits, `found` on any hit — `test_retrieval.py::TestOutcome::test_nothing_found_when_no_hits`, `::test_found_with_hits` pass; independently reproduced `found` at the `retrieve_parents` layer for a gated-to-zero-vector/text-survives scenario (break path 1 above).
- [x] PASS — `text_only` + zero text hits → `nothing_found` with `search_mode="text_only"` — `::test_degraded_and_empty_reports_both` passes.
- [x] PASS — Live e2e pin recorded in `## Log`: nonsense `top=0.728` → `nothing_found`, on-topic `top=0.882` → `found`, knob final at 0.75 — independently reproduced both live queries verbatim on the running local stack (see Happy paths above); the 4 e2e rows (`sourdough-hydration.md`) confirmed present via `mongosh` (`db.memory.countDocuments({"properties.source_uri": /sourdough-hydration/})` → `4`).
- [x] PASS — `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green — all four reran clean in this QA pass.

**Evidence**
```
$ make memory-tests
...
============================ 2980 passed in 49.52s =============================

$ TREE_MEMORY__MODE=rag make memory-query-graph QUERY="zxqv plorb wumbus"
vector leg: 40 candidate(s), 0 kept at min_vector_score=0.75 (top=0.728)
No child hits for query (outcome=nothing_found, search_mode=hybrid): zxqv plorb wumbus
No results.

$ TREE_MEMORY__MODE=rag make memory-query-graph QUERY="what hydration percentage should a sourdough dough use?"
vector leg: 40 candidate(s), 2 kept at min_vector_score=0.75 (top=0.882)
[0.033] Sourdough hydration, measured — Sourdough hydration, measured
...

$ TREE_MEMORY__MODE=rag TREE_QUERY__MIN_VECTOR_SCORE=0.9 make memory-query-graph QUERY="what hydration percentage should a sourdough dough use?"
vector leg: 40 candidate(s), 0 kept at min_vector_score=0.90 (top=0.882)

$ TREE_QUERY__MIN_VECTOR_SCORE=abc uv --directory apps/memory run python -c "from tree.config.app_config import load_app_config"
pydantic_core._pydantic_core.ValidationError: 1 validation error for AppConfig
query.min_vector_score
  Input should be a valid number, unable to parse string as a number [type=float_parsing, input_value='abc', input_type=str]

$ TREE_QUERY__MIN_VECTOR_SCORE=1.5 uv --directory apps/memory run python -c "from tree.config.app_config import load_app_config"
pydantic_core._pydantic_core.ValidationError: 1 validation error for AppConfig
query.min_vector_score
  Input should be less than or equal to 1 [type=less_than_equal, input_value=1.5, input_type=float]
```

**Other issues found**
- **Doc drift (not an AC of this task, PA follow-up)**: `docs/adrs/008_mcp_tool_contract.md:66` and `docs/glossary.md:35` still say `min_vector_score = 0.65`; grep of `docs/ tasks/ .agents/ .claude/` confirms these are the only two hits outside this task file, matching the SWE's note. The SWE is read-only on both files by design (docs discipline lives elsewhere) — flagging so PR Reviewer/PA picks it up rather than it going unmentioned.
- **Task-file internal inconsistency (cosmetic)**: the Scope section (lines 20-21) still quotes `min_vector_score: 0.65` as the value to ship, while the Acceptance Criteria (line 53) and the Log correctly reflect the pinned 0.75. The task's own "Live acceptance" clause (line 44) explicitly authorizes moving the default if 0.65 fails the pin, and the Log documents the move — so this is a stale prose artifact, not a behavioral gap. Worth a one-line edit to Scope for whoever reads this task next.
- **Observability gap (not a defect)**: when the ANN stage returns zero raw candidates on a queryable index, `_gate_vector_candidates` never runs, so there is no `vector leg: 0 candidate(s)...` INFO line at all for that path (the function guards `max()` over an empty list, which would raise). An operator debugging a totally-empty vector leg sees no vector-leg line to reason from. Not in scope for this task's AC, flagging as a possible follow-up for whoever owns observability.
- **graphrag seed impact confirmed non-zero but real**: live graphrag run kept 2/10 vector candidates on an on-topic query (same 0.75 bar, `node_filter={}`). Consistent with the SWE's Log note that the pin move increases seed loss for graphrag; explicitly out of scope for #125 per the task's own "Out of scope" section, but recording the live number here since #128 will need it.
- Infra verified left as found: `docker ps` shows the same 6 containers (`tree-mongodb`, `tree-mongot`, `tree-prefect-server`, `tree-prefect-worker`, `kitaru-local-server-1`, `kitaru-local-db-1`) all `Up`; `tree.orchestrator` process for `make memory-serve-workflows` still running from this branch in `rag` mode (unchanged PID 49009); `memory` collection has exactly 6337 total rows / 4 `sourdough-hydration` rows (matches the SWE's additive e2e document, nothing added or removed by this QA pass — all commands run were read-only queries).

**VERDICT: PASS**
