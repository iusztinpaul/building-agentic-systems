---
id: 195-comma-grouped-numbers-in-token-pattern
status: pending
feature: atlas-search-text-leg
---

# Keep comma-grouped numbers (`1,000`) whole in `TOKEN_PATTERN`, the way the **Text search index**'s standard tokenizer does, and list the residual tokenizer mismatches (apostrophes, colons) in the constant's docstring

Tags: `retrieval`, `bug`, `tests`
Depends on: None (194 is done; this extends its `TOKEN_PATTERN`)
Blocks: —
Implements: ADR-015 §3 (query-side tokenizer; amended by task 194's Status-line note — this task adds one
more clause to the same note, PA-applied at task start)

## Problem

Task 194 closed the decimal class of phantom terms (`4.1` → `4`, `1`), but the index's standard (UAX#29)
tokenizer also keeps a comma BETWEEN DIGITS inside one token (`1,000`, `1,000.50`), and `TOKEN_PATTERN`
(`\w+(?:\.\w+)*`) still splits it into `1` and `000`. `1` is a real index token in 95 of the corpus's
rows ("step 1", "1."), so the phantom term matches rows that never mention the number. Measured live by
the Tester on 194 and reproduced by the PA at acceptance (local corpus, 14 documents / 372 child chunks,
ratio `0.5`):

```
$ make memory-search QUERY="sourdough 1,000 tokens" TOP_K=5
vector leg: 20 candidate(s), 0 kept at min_vector_score=0.70 (top=0.637)
text leg: 9 candidate(s), 4 query term(s), min_should_match=2
→ outcome=found: "Stop Converting Documents to Text", "How Does Memory for AI Agents Work?",
  "Context Engineering: 2025's #1 Skill in AI", "The AI Agents Roadmap", a Claude Code session
```

A baking question answered with five AI-article passages, on `tokens` + the phantom `1`. The leak is
pre-existing (identical under the old `\w+`), narrow (it needs a SHORT query carrying a grouped number:
`"gpt pricing 1,000 tokens"` is M = 5, K = 3 → 0), and disclosed in PR #47's "Known follow-ups" — which
is why it did not block 194's acceptance. It is the same mechanism as 194's Blocker 1, so it gets the
same one-line fix plus the docstring that tells the next reader what is still NOT modelled.

## Scope

1. **`TOKEN_PATTERN`** (`apps/memory/src/tree/memory/rag/search.py`): a comma between two digits stays
   inside the token; a comma anywhere else still splits. The Tester's candidate,
   `\w+(?:\.\w+|(?<=\d),(?=\d)\w+)*`, is one option — the SWE picks the pattern, subject to the pins
   below. Every existing `TestQueryTerms` pin stays unchanged (hyphen split, trailing dot dropped,
   `e.g` / `0.70` whole, stop words removed, 64-term cap).
2. **Docstring** of `TOKEN_PATTERN` (and the fake's class docstring in
   `apps/memory/tests/unit/memory/conftest.py`): the Good / Bad examples gain `1,000`; the "NOT
   modelled" sentence names ALL residual UAX#29 mismatches, not only colon-joined identifiers:
   apostrophe words (`o'reilly` → `reilly`; `don't` → `don`, `t` — under-recall only, and the
   `STOP_WORDS` list relies on that split, so it stays), colon-joined identifiers (`genai:gemini`), and
   digit-dot-letter runs (`v1.a`). One sentence each, with why it is left alone.
3. **The fake** keeps using the imported constant (one shared pattern, 194's contract) — no second
   regex anywhere.
4. **Live confirmation** through the user's path at the pinned ratio (one Voyage embed each, ≥ 21 s
   apart): `make memory-search QUERY="sourdough 1,000 tokens" TOP_K=5` → `3 query term(s),
   min_should_match=2` and `0 candidate(s)` (or the Log names each remaining candidate and shows it
   contains BOTH `sourdough` and `tokens` — a real co-occurrence, not a phantom); `"gpt-4.1 pricing"`,
   `"Gemini"` and `"ReAct agent tool calling"` log the same lines as 194's acceptance entry (0 / 3 / K2;
   20 / 1 / K1 at `TOP_K=5`; 20 / 4 / K2).

**Out of scope (intentional):** apostrophes (changing the split alters which contraction halves hit
`STOP_WORDS`; the measured effect is under-recall, never a leak); colon-joined identifiers (no measured
query); any change to `STOP_WORDS`, the K formula, the ratio pin or the vector leg. Task 193's re-pin
protocol is unchanged — if 193 runs first, the SWE re-runs only the `"sourdough 1,000 tokens"` row.

## Acceptance Criteria

- [ ] `query_terms("sourdough 1,000 tokens") == ["sourdough", "1,000", "tokens"]` and
      `query_terms("gpt pricing per 1,000 tokens")` contains `"1,000"` and not `"000"`.
- [ ] `query_terms("1,000.50 total") == ["1,000.50", "total"]`; `query_terms("foo,bar") == ["foo", "bar"]`
      (a comma between letters still splits — `a,b` would read `["b"]` only because `a` is a stop word);
      `query_terms("foo,1 bar") == ["foo", "1", "bar"]`; `query_terms("1, 2") == ["1", "2"]` (a comma
      followed by a space still splits).
- [ ] Every pre-existing `TestQueryTerms` param passes unchanged (`git diff` touches no existing
      `pytest.param` line in that class).
- [ ] `TestMinimumMatchRatio` gains a behavioural pin: a text-only row with content such as `"step 1 of
      the tokens guide"` is ABSENT from the hits for `"sourdough 1,000 tokens"` at ratio `0.5` (M = 3,
      K = 2), and a row containing `"sourdough 1,000 tokens"` is present; log line `text leg: 1
      candidate(s), 3 query term(s), min_should_match=2`.
- [ ] `TOKEN_PATTERN`'s docstring and the fake's class docstring name apostrophe words, colon-joined
      identifiers and digit-dot-letter runs as the residual mismatches, each with its one-line why;
      `grep -n "1,000" apps/memory/src/tree/memory/rag/search.py` hits the docstring.
- [ ] ADR-015 carries one more clause on its task-194 Status-line note ("…a comma between digits stays
      inside the token too, `1,000`, per task 195") — PA-applied at task start; `git diff --numstat
      docs/adrs` is `1 1` on 015 only; the glossary **Minimum match ratio** row's pattern phrase names
      `1,000` alongside `4.1` (PA-applied; the SWE does not edit either).
- [ ] Live, local, at the pin: `make memory-search QUERY="sourdough 1,000 tokens" TOP_K=5` logs `3 query
      term(s), min_should_match=2` with `0 candidate(s)` (or the Log justifies each candidate as a real
      `sourdough` + `tokens` co-occurrence); the three control queries of Scope 4 are unchanged.
- [ ] `make memory-format-check`, `make memory-lint-check`, `make pre-commit`, `make memory-tests` green
      (env-status local).

## User Stories

### Story: A user asks about a number-bearing topic the memory does not hold
1. The user asks "sourdough 1,000 tokens"; the skill calls `search_memory(query=…, top_k=5)`.
2. The server logs `text leg: 0 candidate(s), 3 query term(s), min_should_match=2`; the vector leg keeps
   nothing (top ≈ 0.64).
3. `search_memory` answers `outcome: nothing_found`; the skill says "Not in memory" instead of reading
   five passages about document conversion and agent memory.

### Story: A user asks an on-topic pricing question with a grouped number
1. The user asks "gpt pricing per 1,000 tokens" (M = 5 — `per` is NOT in `STOP_WORDS`: `gpt`, `pricing`,
   `per`, `1,000`, `tokens`; K = 3 at `0.5`).
2. A row that says "priced per 1,000 tokens" matches on `pricing` (stemmed) + `per` + `1,000` + `tokens`
   — the grouped number is now a term that can hit, instead of two phantoms that never could (under the
   current pattern M = 6, K = 3, and the same row reaches K only through `pricing` + `per` + `tokens`).
3. The fused top-5 leads with the pricing passage; `outcome: found`.

### Story: A reviewer checks what the query tokenizer still gets wrong
1. They open `TOKEN_PATTERN` in `search.py` and read, in one docstring, the Good / Bad examples and the
   three residual mismatches with the reason each is left alone — no need to re-run the Tester's
   scratch-index experiment.

### Story: The evals chapter re-pins after task 193
1. They re-run the enlarged protocol; `"gpt-4.1 pricing"` and `"sourdough 1,000 tokens"` both sit at
   N = 0 at every step ≥ 0.5, so neither phantom-term class can move the pin.

---

Blocked by: (none)

## Log

### [PA] 2026-10-09 02:05 — Grooming (acceptance-review follow-up on 194)

**Summary**
194's `TOKEN_PATTERN` keeps decimals whole but still splits `1,000` into `1` + `000`; the index keeps it
whole, so `1` is a phantom term that matches 95 rows. Live at the pin: `"sourdough 1,000 tokens"` → 9
text candidates, `found`, five off-topic passages. Same mechanism as 194's Blocker 1, one-line regex
fix plus a docstring that names every residual mismatch.

**Key decisions**
- A follow-up, not a rejection of 194: the leak is pre-existing (identical under `\w+`), outside 194's
  AC (decimals), disclosed in the PR body, and needs a short query carrying a grouped number.
- Comma between DIGITS only — `a,b` still splits, as UAX#29 does. Apostrophes stay out: the split feeds
  `STOP_WORDS` (`don` / `t`), and the measured effect is under-recall, not a leak.
- The constant stays the single shared pattern (production + fake), per 194.
- ADR-015 / glossary phrases are PA-applied at task start, in the Status-line style of 191 / 194.

**Dependencies**
- None (194 is done).

**User stories**
- 4 stories: the off-topic number query, the on-topic pricing query, the reviewer's docstring, the next
  re-pin.

Ready for implementation (after the PA applies the ADR-015 clause and the glossary phrase).
