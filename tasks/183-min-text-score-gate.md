---
id: 183-min-text-score-gate
status: pending
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

- [ ] `query.min_text_score` exists in YAML, `QueryConfig` and the env override; `0.0` disables the gate.
- [ ] `_text_search` candidates below the bar are dropped BEFORE fusion; the gate never runs on an
      unavailable leg; gated-to-nothing is `[]` (mode `hybrid`), not `None`.
- [ ] Log line `text leg: N candidate(s), K kept at min_text_score=X (top=Y)` on every answered text leg
      with candidates (at `0.0`, `K == N`); none on an unavailable leg.
- [ ] `default.yaml` comment carries the eval evidence in the `min_vector_score` style; ADR-008 §3 note,
      glossary row, `types.py` and tool docstrings no longer claim the text leg is never gated.
- [ ] The Log records the six queries, their text-leg scores at 0.0, the chosen value and the before/after
      top-5 of the quantization query.
- [ ] `make memory-format-check`, `make memory-lint-check`, `make pre-commit`, `make memory-tests` green.

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
