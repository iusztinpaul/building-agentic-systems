---
id: 183-min-text-score-gate
status: done
feature: horizon-mcp-fixes
---

# A provisional `query.min_text_score` bar on the `$text` leg, pinned by a live on-topic vs off-topic eval (droppable)

Tags: `retrieval`, `rag`, `config`, `evals`, `docs`
Depends on: —
Blocks: —
Implements: ADR-013 §7 (amends ADR-008 §3 "`$text` hits are never gated" and the **Retrieval outcome** row)

## Problem

For "MongoDB Atlas Vector Search scalar binary quantization int8" the fused top-5 of `search_memory`
included a particle-physics paper ("dark photon… vector-like fermions") and the RRF scores showed the
`1/61, 1/62, …` interleave: a lexical hit on "vector" ranked next to the real answers. The vector leg is
gated by `query.min_vector_score: 0.70`; the `$text` leg is never gated (ADR-008 §3: "a lexical match is a
match; `textScore` is not normalisable"). Code: `_text_search` / `_gate_vector_candidates`
(`apps/memory/src/tree/memory/rag/search.py`), `QueryConfig` (`config/app_config.py`), `default.yaml`.

## Scope

**Human decision (provisional — may be cut at plan review):** add a configurable minimum text-score bar
for the `$text` leg (`TREE_QUERY__MIN_TEXT_SCORE` overridable, documented in `default.yaml` with the same
evidence-comment style as `min_vector_score`), pinned by a small live eval recorded in the task Log. Kept
as the last task so it can be dropped.

1. **Config:** `QueryConfig.min_text_score: float = Field(<pinned>, ge=0.0)` — `0.0` disables the gate;
   `default.yaml` key + comment: what MongoDB `textScore` is (the sum of per-term weights — field weight ×
   term frequency normalised by field length — an UNNORMALISED, corpus-relative number, which is why the
   bar is provisional and evals-owned), the eval queries, their top scores, the chosen 0.5-step, and
   `TREE_QUERY__MIN_TEXT_SCORE=…`.
2. **Gate:** `_gate_text_candidates(candidates)` mirroring `_gate_vector_candidates`: runs only on a leg that
   ANSWERED with candidates (never on `None`, never on `[]`); ALWAYS logs `text leg: %d candidate(s), %d
   kept at min_text_score=%.2f (top=%.3f)` (the vector leg's rule — the line is what the eval and an
   operator read); FILTERS (`_search_score >= min_text_score`) only when the bar is `> 0.0`, so at `0.0`
   every candidate is kept and the line still reports `top`; a leg gated to nothing returns `[]` (mode
   stays `hybrid`). Called at the end of `_text_search`'s success path.
3. **Docs:** ADR-008 §3 Status-line note (ADR-013 "Amendments"); `docs/glossary.md` **Retrieval outcome**
   row (Part 2); `tree/memory/rag/types.py` docstrings at ~L179 and ~L209 ("the text leg ungated") and the
   `search_memory` docstring's "relevance bar" sentence; `apps/memory/README.md` `search_memory` row.
4. **Eval protocol (LOCAL env, the real local corpus, `make memory-search QUERY=…` with the gate at 0.0 and
   INFO logs on; record every number in the Log):**
   - on-topic ×3: "MongoDB Atlas Vector Search scalar binary quantization int8", "how does the coordinator
     shard documents", one more the operator knows is in memory;
   - off-topic ×3: "zxqv plorb wumbus", "recipe for sourdough bread", "dark photon vector-like fermions"
     (ONLY if that paper is NOT in memory — else pick another known-absent topic);
   - for each: the text leg's `top` and the scores of its candidates (from the gate's log line at 0.0 and a
     temporary debug print of the candidate scores — not committed);
   - pick the lowest 0.5-step that empties every off-topic text leg while keeping ≥ 1 text hit for every
     on-topic query; show the fused top-5 for the quantization query before/after (the physics paper must
     leave the top-5, or the Log says why it legitimately stays);
   - if NO step separates them, ship `min_text_score: 0.0` with the gate present and say so in the Log.
5. **Tests** (`tests/unit/memory/rag/test_search.py`): gate keeps/drops around the bar; `0.0` → nothing
   filtered, the log line still present with `top`; a text leg gated to nothing fuses as `hybrid` with
   vector-only hits; `None` leg untouched (`vector_only`, no line); config default and env override;
   `retrieve_parents` outcome `nothing_found` when both legs gate to nothing.

## Acceptance criteria

- [x] `query.min_text_score` exists in YAML, `QueryConfig` and the env override; `0.0` disables the gate.
- [x] `_text_search` candidates below the bar are dropped BEFORE fusion; the gate never runs on an
      unavailable leg; gated-to-nothing is `[]` (mode `hybrid`), not `None`.
- [x] Log line `text leg: N candidate(s), K kept at min_text_score=X (top=Y)` on every answered text leg
      with candidates (at `0.0`, `K == N`); none on an unavailable leg.
- [x] `default.yaml` comment carries the eval evidence in the `min_vector_score` style; ADR-008 §3 note,
      glossary row, `types.py` and tool docstrings no longer claim the text leg is never gated.
- [x] The Log records the six queries, their text-leg scores at 0.0, the chosen value and the before/after
      top-5 of the quantization query.
- [x] `make memory-format-check`, `make memory-lint-check`, `make pre-commit`, `make memory-tests` green.

## User Stories

### Story: The quantization question no longer surfaces the physics paper
1. The user asks "what did I read about scalar/binary quantization in Atlas Vector Search?"; the model calls `search_memory(query="MongoDB Atlas Vector Search scalar binary quantization int8")`.
2. The server logs `vector leg: 40 candidate(s), 9 kept at min_vector_score=0.70 (top=0.81)` and `text leg: 10 candidate(s), 4 kept at min_text_score=<X> (top=<Y>)`.
3. The top-5 parents are all MongoDB quantization passages; the dark-photon paper is absent.

### Story: Nonsense still answers nothing_found
1. `search_memory(query="zxqv plorb wumbus")`.
2. Both legs gate to nothing; the answer is `{"parents": [], "outcome": "nothing_found", "search_mode": "hybrid"}` and the skill says "Not in memory".

### Story: The operator turns the gate off for a day
1. `TREE_QUERY__MIN_TEXT_SCORE=0 make memory-search QUERY="binary quantization"`.
2. The log reads `text leg: 10 candidate(s), 10 kept at min_text_score=0.00 (top=2.431)`; every `$text` hit fuses as before.

### Story: The text leg is down
1. The text index is unavailable; `_text_search` returns `None`.
2. `search_mode` is `vector_only` and no text gate line is logged — degraded is not filtered.

---

Blocked by: (none)

## Log

### [PA] 2026-10-03 18:15 — Grooming

**Summary**
A provisional minimum `textScore` bar on the `$text` leg, mirroring the vector gate (before fusion, never on
a dead leg, `0.0` = off), with its value pinned by a six-query live eval recorded in this Log — kept last so
the plan can drop it.

**Key decisions**
- Same shape as `_gate_vector_candidates` (log line, `[]` not `None`) so an operator reads both gates alike.
- `0.0` disables: if the eval finds no separating step, the knob ships off and the task still documents why.
- The eval protocol names its queries and its stopping rule up front so the pin is reproducible (the
  `tasks/141` precedent).

**Dependencies**
- None.

**User stories**
- 4 stories: the quantization query, nonsense, the gate turned off, the text leg down.

**Open questions**
- Whether to keep this task at all (Feature Plan open question 3).

Ready for implementation.

### [SWE] 2026-10-03 21:10 — Implementation

**Files modified**
- `apps/memory/src/tree/memory/rag/search.py` — `_gate_text_candidates` (twin of `_gate_vector_candidates`); `_text_search` gates AFTER the `try` (a gate bug raises instead of reading as a dead leg), only when the leg answered with candidates; `[]` stays `[]`, `None` stays `None`. Vector-gate docstring no longer says `$text` is ungated.
- `apps/memory/src/tree/config/app_config.py` — `QueryConfig.min_text_score: float = Field(0.0, ge=0.0)` (no upper bound: `textScore` is unnormalised) + docstring.
- `apps/memory/src/tree/config/default.yaml` — `query.min_text_score: 0.0` with the eval-evidence comment (what `textScore` sums, the six+ queries and their tops, why no 0.5 step separates, how to re-pin, `TREE_QUERY__MIN_TEXT_SCORE=...`).
- `apps/memory/src/tree/memory/rag/types.py` — **Retrieval outcome** docstring + `RetrievalResult.outcome` description name both bars.
- `apps/memory/src/tree/mcp/tools.py` — `search_memory` docstring: "above either search leg's relevance bar".
- `apps/memory/README.md` — `query` knob list + `search_memory` row.
- `docs/glossary.md` — **Retrieval outcome** row (Part 2 wording, Definition + Notes).
- `docs/adrs/008_mcp_tool_contract.md` — Status line: the task-183 clause appended to the existing 176/177 clauses (Part 3 wording, merged, not duplicated).
- `apps/memory/tests/unit/memory/rag/test_search.py` — module-wide autouse `text_gate_off` (pins 0.0 so a future re-pin cannot gate other tests' text hits); `TestMinTextScore` (7 tests); `test_text_hits_are_not_gated` → `..._by_the_vector_bar`.
- `apps/memory/tests/unit/memory/rag/test_retrieval.py` — `test_nothing_found_when_both_legs_gate_to_nothing` (real `hybrid_search`, mode stays `hybrid`); docstrings.
- `apps/memory/tests/unit/config/test_app_config.py` + `fixtures/frozen_config.yaml` — default / absent-key / YAML key read (1.5) / env override wins / negative rejected; frozen key.

**Tests**
- Unit: 4892 passing, 0 failing (`make memory-tests`, env-status local).
- Mutation check: replacing the gate call with `return results` fails 5 of the new tests (keeps/drops, both log-line tests, gated-to-nothing hybrid, retrieve_parents nothing_found).
- Integration: N/A (no suite, per AGENTS.md).

**Acceptance criteria**
- [x] `query.min_text_score` in YAML / `QueryConfig` / env override; `0.0` disables — `test_app_config.py::TestQueryConfig::test_min_text_score_default_override_and_bounds`, `test_search.py::TestMinTextScore::test_zero_bar_keeps_everything_and_still_logs_top`
- [x] Dropped BEFORE fusion; never on an unavailable leg; gated-to-nothing is `[]` / `hybrid` — `TestMinTextScore::test_drops_text_hits_below_threshold`, `test_keeps_hit_at_threshold`, `test_text_leg_gated_to_nothing_stays_hybrid`, `test_unavailable_text_leg_is_not_gated`; `test_retrieval.py::TestOutcome::test_nothing_found_when_both_legs_gate_to_nothing`
- [x] Log line on every answered text leg with candidates (K == N at 0.0), none on unavailable / empty — `test_logs_candidates_kept_and_top_score`, `test_zero_bar_keeps_everything_and_still_logs_top`, `test_unavailable_text_leg_is_not_gated`, `test_empty_text_leg_logs_no_gate_line`
- [x] `default.yaml` evidence comment; ADR-008 Status note; glossary row; `types.py` + tool docstrings
- [x] Eval recorded below
- [x] format-check / lint-check / pre-commit / memory-tests green

**Eval (LOCAL only — env-status `local`, nothing ingested, nothing written)**

Corpus: user `paul.iusztin@example.com`, 14 documents / 372 child chunks — 10 decodingai.com AI-agent articles, 2 Claude Code sessions, 2 example.com pages. There is NO MongoDB page and NO coordinator/sharding page in the local memory, so the protocol's first two on-topic queries are OFF-topic here, and the on-topic set is drawn from the articles. The dark-photon paper is not in memory, so that query is a valid off-topic probe. Scores come from a scratchpad script (not committed) that calls `_text_search` at `limit=40` (= `search_memory`'s default `top_k=10` × 4) with the bar at 0.0 and prints every candidate.

| query | class (locally) | text candidates | text top | 2nd | vector top |
|---|---|---|---|---|---|
| "MongoDB Atlas Vector Search scalar binary quantization int8" | off-topic | 40 | 1.524 (session) | 1.512 | 0.656 |
| "how does the coordinator shard documents" | off-topic | 40 | 1.014 | 1.009 | — |
| "dark photon vector-like fermions" | off-topic | 40 | 1.472 | 1.267 | 0.587 |
| "recipe for sourdough bread" | off-topic | 1 | 0.504 | — | 0.631 |
| "zxqv plorb wumbus" | off-topic | 0 (no line) | — | — | 0.600 |
| "context engineering" | on-topic | 40 | 2.099 | 2.074 | 0.773 |
| "structured outputs pydantic" | on-topic | 40 | 2.975 | 2.948 | — |
| "ReAct agent tool calling" | on-topic | 40 | 3.736 | 3.596 | — |
| "pydantic" (one word) | on-topic | 19 | 1.072 | — | 0.821 |
| "ReAct" (one word) | on-topic | 40 | 1.062 | — | — |
| "Gemini" (one word) | on-topic | 39 | 1.028 | — | — |
| "context window" | on-topic | 40 | 2.053 | — | — |
| "tool calling" | on-topic | 40 | 2.184 | — | — |

Stopping rule: the lowest 0.5 step that empties every off-topic text leg is **2.0** (the local quantization query peaks at 1.524). With only the three multi-word on-topic queries it would "separate" by 0.099 ("context engineering" 2.099). But a one-word on-topic query tops out at ~1.0–1.1, so 2.0 — and even 1.5 — empties the text leg of every one-word on-topic query, and two-word on-topic queries ("context window" 2.053) sit 0.05 above the bar. `textScore` is a sum over matched query terms, so the score mostly measures how many query words matched, not relevance: an 8-word off-topic query that hits "vector", "search" and "binary" outscores a one-word exact on-topic hit. **No 0.5 step separates on-topic from off-topic → shipped `min_text_score: 0.0` (gate present, off), per open question 3.** (For "pydantic" the vector leg still keeps 16 hits at 2.0, so the fused answer survives — but the protocol's criterion is "≥1 text hit", and the queries the text leg exists for — rare identifiers the vector leg misses — are exactly the short ones.)

Fused top-5 of the quantization query (`retrieve_parents(top_k=5)`), local:
- at 0.0 (shipped): `found` — Claude Code session 1cd439a0 [0.0164], Stop Converting Documents to Text [0.0161], How Does Memory for AI Agents Work? [0.0156], Tool Calling From Scratch to Production [0.0154], Claude Code session f5a51576 [0.0147] — all off-topic, all lexical (`vector leg: 20 candidate(s), 0 kept ... (top=0.656)`).
- at 1.5: `found` — the two top sessions/articles only (`text leg: 20 candidate(s), 2 kept at min_text_score=1.50 (top=1.524)`).
- at 2.0: `nothing_found`, `search_mode=hybrid` (`text leg: 20 candidate(s), 0 kept at min_text_score=2.00 (top=1.524)`).

The dark-photon paper is not in the LOCAL memory, so the production collision cannot be reproduced here; with the gate shipped at 0.0 it LEGITIMATELY stays in production's fused top-5 for that query until the bar is re-pinned (no value was found that removes it without also deleting one-word on-topic text hits). Same for "dark photon vector-like fermions" locally: 0.0 → `found` (5 unrelated parents), 1.5 → `nothing_found`.

**Evidence (end-to-end, `make memory-search`, local)**
```
$ make memory-tests
======================= 4892 passed in 64.50s (0:01:04) ========================

$ TREE_QUERY__MIN_TEXT_SCORE=0 make memory-search QUERY="binary quantization" TOP_K=5
vector leg: 20 candidate(s), 0 kept at min_vector_score=0.70 (top=0.622)
text leg: 9 candidate(s), 9 kept at min_text_score=0.00 (top=0.508)
Parent-document retrieval: 9 child hit(s) -> 4 parent(s), returning 4

$ TREE_QUERY__MIN_TEXT_SCORE=2 make memory-search QUERY="binary quantization" TOP_K=5
vector leg: 20 candidate(s), 0 kept at min_vector_score=0.70 (top=0.622)
text leg: 9 candidate(s), 0 kept at min_text_score=2.00 (top=0.508)
No child hits for query (outcome=nothing_found, search_mode=hybrid): binary quantization
No results.

$ make memory-search QUERY="zxqv plorb wumbus" TOP_K=5
vector leg: 20 candidate(s), 0 kept at min_vector_score=0.70 (top=0.600)
No child hits for query (outcome=nothing_found, search_mode=hybrid): zxqv plorb wumbus

$ make memory-search QUERY="recipe for sourdough bread" TOP_K=5
text leg: 1 candidate(s), 1 kept at min_text_score=0.00 (top=0.504)
[0.016] Context Engineering: 2025’s #1 Skill in AI
```

**Notes**
- Shipping 0.0 keeps today's behaviour: any off-topic query sharing one indexed word with the memory still answers `found` (see "recipe for sourdough bread" above). That is the known cost; the gate + its log line exist so an operator can measure and re-pin.
- "zxqv plorb wumbus" proves nothing about the bar (its text leg is empty at any value); it is listed, not counted.
- Upgrade path (out of scope, for the PA): a per-query-normalised text score (e.g. `textScore` / number of query terms, or a min fraction of query terms matched) would compare across query lengths; the raw-`textScore` bar cannot.
- NOT RUN: the production / Atlas reproduction of the dark-photon case — orchestrator ruling: local only. No local documents were ingested (the structural finding — score scales with matched-term count — does not depend on corpus content), so there was nothing to delete.
- Scratchpad eval script: `.../scratchpad/text_gate_eval.py` (not committed).

### [Tester] 2026-10-03 22:10 — QA

**Test summary**
- Format / lint / pre-commit: PASS (`make memory-format-check` 333 files formatted, `make memory-lint-check` clean, `make pre-commit` all hooks passed)
- Unit tests: 4892 passed / 0 failed (`make memory-tests`, env-status `local`)
- Integration tests: N/A (no suite, per AGENTS.md)
- Warnings: 0 new (only the pre-existing Python 3.14 / opik pydantic-v1 import notice outside pytest)

**E2E adversarial pass** (LOCAL env only; nothing ingested or written; processes: none left running; HEAD worktree removed)
- Reproduce SWE eval independently (scratchpad `text_gate_eval.py`, same corpus): "recipe for sourdough bread" top 0.504 (1 cand), "pydantic" top 1.072 (19 cand), "context engineering" top 2.099, quantization query top 1.524 — all match the SWE table. At 1.5 and 2.0: sourdough -> `nothing_found`; "pydantic" text leg 0 kept at both (outcome still `found` via the vector leg, 16 kept); "context engineering" 18 kept at 1.5 / 4 kept at 2.0; quantization 2 kept at 1.5, 0 kept at 2.0 -> `nothing_found`/`hybrid`. (PASS — numbers and the "no 0.5 step separates" conclusion reproduce.)
- Bar 0.0 is byte-for-byte the old behaviour: ran `retrieve_parents(top_k=10)` for 11 queries (sourdough, pydantic, context engineering, quantization, nonsense, ReAct agent tool calling, "how does memory work", `"exact phrase" -agents`, Gemini, empty, whitespace) against a `git worktree` of HEAD (verified `tree.__file__` pointed at the HEAD tree via PYTHONPATH) and against the working tree; `cmp old.json new.json` -> IDENTICAL (1,101,517 bytes each, full `model_dump` incl. scores). (PASS)
- Dead text leg, bar 0.0 and 2.0 (proxy collection raising on the `$text` aggregate): `search_mode=vector_only`, outcome `found`, 4 parents both times, NO `text leg:` log line, WARNING "Text search leg unavailable" only. (PASS)
- Dead vector leg, bar 0.0 / 2.0: `search_mode=text_only`; `text leg: 20 candidate(s), 20 kept ... (top=2.099)` -> 5 parents at 0.0; `... 4 kept at min_text_score=2.00` -> 1 parent at 2.0 (gate runs on the surviving leg, filters correctly). (PASS)
- Invalid config: `TREE_QUERY__MIN_TEXT_SCORE` = `-1` -> pydantic `greater_than_equal` ValidationError at boot; `abc` and `""` -> `float_parsing` error; `nan` -> rejected (not >= 0); `0` -> 0.0; `2.5`, `1e3` accepted. Clean one-line pydantic error, no crash mid-query. `inf` is accepted (would gate every text hit) — see notes. Negative YAML covered by the SWE's unit test. (PASS)
- Logging: gate line is `logger.info`, same level as the vector gate's line, one line per answered text leg with candidates, none for `None`/`[]` legs (verified live above and by unit tests). Not noisy beyond the existing vector line. (PASS)
- graphrag path: only shared surface is `hybrid_search` (graph/retrieval.py:311, `node_filter={}`); at 0.0 it is the identical code path (covered by the byte-identical comparison of the shared function); graphrag reads `.hits` only and `QueryResult` is unchanged. (PASS, see note 2)
- Mutation re-check not repeated; read the 7 `TestMinTextScore` tests: inclusive bar, drop, log text, 0.0, gated-to-nothing hybrid, unavailable leg (no line), empty leg (no line) — meaningful.

**Acceptance criteria**
- [x] PASS — `query.min_text_score` in YAML, `QueryConfig` (`app_config.py:345`, `Field(0.0, ge=0.0)`), env override; 0.0 disables — live env override test above; `test_app_config.py` default/override/bounds; byte-identical run at 0.0
- [x] PASS — dropped BEFORE fusion; never on unavailable leg; gated-to-nothing is `[]`/`hybrid` — live dead-leg runs; `TestMinTextScore::test_text_leg_gated_to_nothing_stays_hybrid`, `test_unavailable_text_leg_is_not_gated`; live quantization at 2.0 -> `nothing_found`, `search_mode=hybrid`
- [x] PASS — log line on every answered text leg with candidates (K==N at 0.0), none otherwise — live: `text leg: 19 candidate(s), 19 kept at min_text_score=0.00 (top=1.072)`; no line for the dead leg or the zero-candidate nonsense query
- [x] PASS — `default.yaml` evidence comment (:118-136), ADR-008 Status clause, glossary row, `types.py:179,210`, `tools.py` docstring, README — all in `git diff --stat`; no remaining "never gated"/"ungated" claims (checked in diff)
- [x] PASS — Log records the queries, scores, chosen 0.0 (accepted per orchestrator ruling / open question 3), before/after top-5. Honest deviations stated by the SWE (no MongoDB/coordinator page locally, so those two on-topic queries are off-topic here; substituted on-topic from the corpus) — reproduced independently
- [x] PASS — format-check / lint-check / pre-commit / memory-tests green

**Other issues found (non-blocking)**
1. `TREE_QUERY__MIN_TEXT_SCORE=inf` is accepted and silently gates every text hit (all queries lose the lexical leg). `ge=0.0` has no upper bound by design; a `allow_inf_nan=False` on the Field would reject it. Optional.
2. The gate is applied inside `_text_search`, so a non-zero bar also filters the graphrag seed search (`node_filter={}`, entity/chunk rows), not only rag. Docs/README describe it under rag `search_memory`; harmless at the shipped 0.0, but worth a sentence when someone re-pins above 0.
3. Candidate counts differ by caller: `retrieve_parents` logs 20 text candidates (its limit) while the SWE's eval script used limit 40; `top` is identical either way (reproduced), only N differs. The user story's "10 candidate(s)" is illustrative.
4. Pre-existing (identical at HEAD, not this task): `retrieve_parents` with an empty-string query raises `ExtractionError` from Voyage 400; the MCP/CLI layers should reject it earlier.
5. `tools.py` docstring edit left one line longer than the surrounding wrap (lint passes).

**VERDICT: PASS**

### [SWE] 2026-10-03 22:30 — Pre-commit follow-ups (coordinator)

**Files modified**
- `apps/memory/src/tree/config/app_config.py` — `min_text_score` gains `allow_inf_nan=False` (with no upper bound, `inf` would pass `ge=0` and gate every text hit away).
- `apps/memory/tests/unit/config/test_app_config.py` — `test_min_text_score_rejects_inf_and_nan[inf|nan]`.
- `apps/memory/src/tree/config/default.yaml` — evidence comment adds the upgrade path (raw `textScore` sums matched query words → a per-query normalised score, e.g. `textScore` / query word count) and that a bar > 0 also filters graphrag's seed text search.
- `apps/memory/src/tree/memory/rag/search.py` — `_gate_text_candidates` docstring: the gate lives in the shared `_text_search`, so a bar > 0 filters graphrag seeds too.

**Tests**
- Unit: 4894 passing, 0 failing (`make memory-tests`, env-status local); format-check / lint-check / pre-commit clean.
