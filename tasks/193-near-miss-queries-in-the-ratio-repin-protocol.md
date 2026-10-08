---
id: 193-near-miss-queries-in-the-ratio-repin-protocol
status: pending
feature: atlas-search-text-leg
---

# Extend ADR-015 §8's re-pin protocol with short near-miss off-topic queries (M = 2 / M = 3 on two generic words), re-run the table, re-pin `query.text_min_match_ratio` to the lowest step that separates the ENLARGED set, and record the known short-query leak where the next re-pinner reads (`default.yaml` comment, glossary **Minimum match ratio**)

Tags: `evals`, `config`, `retrieval`, `docs`, `adr`
Depends on: `tasks/194-pr-review-rollup.md` (its `query_terms` tokenizer, `\w+(?:\.\w+)*`, is what makes
`"gpt-4.1 pricing"` M = 3 / N = 0; on `\w+` it is M = 4 and seeds 20 candidates). 192 is done; this is the
PA's acceptance-review follow-up, not a rejection.
Blocks: —
Implements: ADR-015 §8 (amended by this task's Status-line note — the protocol gains near-miss queries)

## Problem

Task 192 pinned `query.text_min_match_ratio` at `0.5` exactly as ADR-015 §8 told it to: the lowest 0.1
step at which the four named off-topic queries keep 0 text candidates and the six named on-topic ones
keep ≥ 1. The Tester's QA on 192 (Log, 2026-10-09 01:00) then showed the protocol's blind spot: it holds
no SHORT off-topic query. K-of-M with `M = 2` gives `K = 1` at every ratio ≤ 0.5, and `M = 3` gives
`K = 2` between 0.34 and 0.67, so a two-or-three-word off-topic phrase sharing two generic words with the
corpus still seeds the text leg. Live at the pin (PA acceptance review, 2026-10-09 00:30, local corpus 14
documents / 372 child chunks):

- `"vector-like particles"` (M = 3: `vector`, `like`, `particles`; K = 2) → `text leg: 6 candidate(s)`,
  vector leg `0 kept (top=0.610)` → **3 parents, `outcome: found`** — "Stop Converting Documents to
  Text" and "How Does Memory for AI Agents Work?" answer a particle-physics question, on `vector` +
  `like` alone.
- `"bread recipe"` (M = 2, K = 1) → 1 row; `"vector-like"` (M = 2, K = 1) → 93 rows (Tester).

At `0.7` the 10 protocol queries still separate (on-topic fewest: "structured outputs pydantic" 13 at
K = 3, "context engineering" 50 at K = 2, "ReAct agent tool calling" 83 at K = 3; one-word queries
unchanged), "vector-like particles" and "bread recipe" fall to 0 (K = 3 / K = 2), and the full-sentence
story "How does the ReAct agent decide when to call a tool?" (M = 5) returns the identical fused top-2 at
K = 4 (PA, live). `"vector-like"` alone keeps 6 rows at every ratio ≥ 0.6 — both terms genuinely co-occur
in 6 chunks — so no ratio fixes a two-generic-word query; only the protocol can say what is acceptable.

The PA accepted the feature because the pin followed the approved rule and the leak is a strict
improvement over the retired `$text` leg (the same query returned 93 rows there). But the rule itself is
the gap: "re-pin with the same 10-query protocol" (the `default.yaml` comment, the glossary row) sends the
next re-pinner down the same blind spot. This task fixes the protocol, not the code.

## Scope

**1. Amend the protocol (PA-owned ADR edit — the SWE skips this item; the PA applies it when this task
starts, as on 192).** One Status-line note on ADR-015 (the project's amendment style, see ADR-008 / 012 /
013): "§8's eval set gains near-miss off-topic queries (M = 2 / M = 3 on generic words) and one
version-bearing off-topic query, per task 193; the pin rule is unchanged — the lowest 0.1 step separating
the enlarged set." Body text untouched.

**2. The enlarged query set.** Keep ADR-015 §8's ten. Add at least FIVE near-miss off-topic queries, each
sharing one or two GENERIC words with the corpus and none of its topic, covering M = 2 and M = 3:
`"vector-like particles"`, `"bread recipe"`, `"vector-like"`, plus ≥ 2 more of the SWE's choice (e.g.
`"memory foam mattress"` — `memory` + `foam` + `mattress`; `"tool shed plans"` — `tool` + `shed` +
`plans`). Also add ONE version-bearing off-topic query, `"gpt-4.1 pricing"` (M = 3 with task 194's
tokenizer: `gpt`, `4.1`, `pricing`; 0 corpus rows name it) — a different leak class from the near-miss
five (phantom digit terms, closed by 194's `\w+(?:\.\w+)*`, not generic words), kept in the protocol so
the next re-pinner sees it stay at N = 0 (run only after `tasks/194` has landed). Record each query's
terms and M in the Log before running
anything. Add at least TWO short
on-topic queries with M = 2–3 (e.g. `"agent memory"`, `"pydantic validation"`), chosen so that at least
ONE of them has an N that drops to 0 at some step ≤ 1.0 — a higher pin's recall cost must be a measured
number in the table, not an assumption.

**3. Re-run the table** with 192's method verbatim (its inline `ratio_table.py` against the real
`_text_search`, `limit=100000`, one run per DISTINCT K — never 10 ratios × N queries through Voyage). Table
`query → M; per 0.1 step: K → N` for every query, old and new.

**4. Pin.** The LOWEST 0.1 step at which every off-topic N == 0 (old and near-miss) and every on-topic
N ≥ 1 (old and short). If none exists — expected for `"vector-like"` (M = 2, both terms co-occur) — the
Log names the queries that cannot be separated at any step and WHY (both generic terms really
co-occur), drops them from the separating set as "unseparable by ratio", and pins on the rest; the
`default.yaml` comment says which queries were set aside. Do not force a value; do not drop an on-topic
query to make a step work.

**5. Record the value and the leak** — every place the next re-pinner reads:
- `default.yaml` `text_min_match_ratio` comment: the new evidence rows in the existing style (date,
  corpus size, decisive rows, chosen step, override), PLUS one sentence stating the known leak class
  ("short off-topic queries of 2–3 generic words can still seed the leg: M = 2 → K = 1 at ≤ 0.5 …") and
  that the protocol now includes near-miss queries;
- `frozen_config.yaml`, `QueryConfig` default + docstring, `test_app_config.py`'s default assertion;
- `apps/memory/README.md` line 76 (the `query` key line) if the value or the protocol wording changes;
- `docs/glossary.md` **Minimum match ratio** and **Retrieval outcome** rows (PA-owned; the PA edits them
  at task start to say "pinned by the 15+-query protocol incl. near-miss queries, task 193" with the
  leak sentence — the SWE does not touch the glossary).

**6. Confirm through the user's path** (one Voyage embed each, ≥ 21 s apart): `make memory-search` at the
new pin for `"vector-like particles"` (expect `nothing_found`, or the Log says why it legitimately stays),
`"How does the ReAct agent decide when to call a tool?"` (expect the ReAct passages still lead),
`"Gemini"` (text leg alone answers it: `vector 0 kept`, so N must stay 37 in the uncapped table; the CLI
line shows `min(N, TOP_K × 4)` because the leg's `$limit` is `top_k * _CHILD_HITS_PER_PARENT` — 37 at the
default `TOP_K=10`, 20 at `TOP_K=5`), and the two short on-topic queries of step 2.

**Out of scope (intentional):** adding `like` (or any word) to `STOP_WORDS` — `like` is neither in
Lucene's 33 nor a question word, and ADR-015 §3 keeps the list minimal; see Open questions. Any change to
`min_vector_score`, to the K formula, to the 64-term cap, or to the vector leg.

## Acceptance criteria

- [ ] ADR-015 carries one Status-line note naming the enlarged protocol (task 193); `git diff --numstat
      docs/adrs` is `1 1` on 015 only.
- [ ] The Log holds the enlarged table: the ten ADR-015 §8 queries + ≥ 5 near-miss off-topic (M = 2 and
      M = 3 both present) + 1 version-bearing off-topic (`"gpt-4.1 pricing"`) + ≥ 2 short on-topic, each
      with its terms, M, and `K → N` per 0.1 step.
- [ ] The pinned value is the lowest 0.1 step separating the enlarged set; any query set aside as
      "unseparable by ratio" is named in the Log AND in the `default.yaml` comment with the reason.
- [ ] `default.yaml`, `frozen_config.yaml`, `QueryConfig` and `test_app_config.py` agree on the value; the
      `default.yaml` comment and the glossary **Minimum match ratio** row both carry the leak sentence and
      say the protocol includes near-miss queries.
- [ ] Live, local: `make memory-search QUERY="vector-like particles"` at the pin answers `nothing_found`
      (or the Log says why not); `"How does the ReAct agent decide when to call a tool?"` still leads with
      ReAct / tool-calling passages; `"Gemini"` logs `37 candidate(s), 1 query term(s), min_should_match=1`
      at the default `TOP_K` (10 → child `$limit` 40; at `TOP_K=5` the same line reads `20 candidate(s)` —
      the cap, not a drop — and the uncapped table must still say 37).
- [ ] `grep -rnI '10-query' apps/memory/src docs/glossary.md apps/memory/README.md` is empty AFTER the
      task (the old wording no longer tells the re-pinner to repeat the blind spot). Non-vacuity: BEFORE
      the task the same grep hits exactly 3 lines — `default.yaml:134` (the comment's "Re-pin with the
      same 10-query / protocol" wraps across two lines, which is why the pattern is the bare `10-query`,
      not the phrase) and `docs/glossary.md` rows 39 (**Minimum match ratio**) and 50 (**Retrieval
      outcome**); verified by the PA at grooming.
- [ ] `make memory-format-check`, `make memory-lint-check`, `make pre-commit`, `make memory-tests` green
      (env-status local).

## User Stories

### Story: A user asks Claude Code about a topic the memory does not hold, in three words
1. The user asks "vector-like particles"; the skill calls `search_memory(query=…, top_k=5)`.
2. The server logs `text leg: 0 candidate(s), 3 query term(s), min_should_match=K` with the pinned K;
   the vector leg keeps nothing (top ≈ 0.61).
3. `search_memory` answers `outcome: nothing_found`; the skill answers "Not in memory" after its one
   retry, instead of reading three passages about document conversion and agent memory.

### Story: A user asks a two-word on-topic question
1. The user asks "agent memory"; the text leg logs `M = 2` and the pinned K; N ≥ 1.
2. The fused top-5 leads with "How Does Memory for AI Agents Work?" passages.

### Story: The evals chapter re-pins next quarter
1. They open `default.yaml`, read the comment — the enlarged protocol, the decisive near-miss rows, the
   leak sentence and the queries set aside — and re-run the SAME enlarged set with
   `TREE_QUERY__TEXT_MIN_MATCH_RATIO=r make memory-search …`, moving the value with the same evidence shape.

### Story: A reviewer traces why the ratio moved (or did not)
1. From ADR-015's Status line they land on task 193's Log table and see which near-miss query set the
   step, and which (e.g. `"vector-like"`) no ratio can separate and why.

### Story: A one-word query still works
1. `make memory-search QUERY="Gemini"` (default `TOP_K`) → `1 query term(s), min_should_match=1`, 37
   candidates (20 at `TOP_K=5`: the child `$limit`, not a loss), the text leg alone answers it (vector top
   0.697 < 0.70) — unchanged by any pin.

## Open questions

- `like` is the term that inflates M on every hyphen split (`vector-like` → `vector`, `like`) and it is
  in neither Lucene's 33 nor the NLTK-style set. Adding it to `STOP_WORDS` would turn `"vector-like
  particles"` into M = 2 (K = 1 at 0.5 → 93 rows — WORSE) but `"vector-like"` into M = 1 (any `vector`
  row — also worse). So it is not a fix; flagged only so nobody "fixes" it that way. Decision: leave
  `STOP_WORDS` alone unless the enlarged table shows a word that is a question word in disguise.
- If the enlarged set pins at ≥ 0.7, ADR-015's Consequences already record the recall cost ("a question
  whose only on-topic word is rare but whose other words are generic can lose its lexical hit at a high
  ratio — the vector leg still carries it"). The two short on-topic queries of Scope 2 are what make that
  cost a number; if they lose their only lexical hit, the human decides — do not force the pin.

---

Blocked by: `tasks/194-pr-review-rollup.md`

## Log

### [PA] 2026-10-09 00:40 — Grooming (acceptance-review follow-up on 192)

**Summary**
ADR-015 §8's re-pin protocol has no short off-topic query, so the pin it produced (0.5) lets 2–3-word
off-topic phrases on two generic words seed the text leg (`"vector-like particles"` → 3 parents,
`found`). Enlarge the protocol with near-miss off-topic and short on-topic queries, re-run 192's table,
re-pin by the unchanged rule, and record the leak class where the next re-pinner reads.

**Key decisions**
- A follow-up, not a rejection of 192: 192 met its AC exactly as the human approved them; the gap is the
  protocol (PA-owned), and the fix is one YAML value plus the evidence that justifies it.
- The pin rule stays "lowest separating step" — only the query set changes. A query no ratio can separate
  (`"vector-like"`: both terms co-occur in 6 chunks) is set aside and named, not used to force a value.
- Short ON-topic queries join the set so a higher pin's recall cost is measured (ADR-015 Consequences
  names that cost; today nothing in the protocol would detect it).
- `STOP_WORDS` untouched (see Open questions — adding `like` makes both near-miss cases worse).
- Evidence from the acceptance review (0.7 keeps the ReAct full-sentence story's top-2 at K = 4; the 10
  protocol queries still separate at 0.7 per the Tester) is recorded in Problem so the SWE starts from it.

**Dependencies**
- None. 190–192 are done and ship in PR #47; the ADR Status-line note and the glossary rows are
  PA-applied at task start.

**User stories**
- 5 stories: the three-word off-topic question, the two-word on-topic question, the next re-pin, the
  reviewer's trail, the one-word query.

**Open questions**
- Whether `like` deserves a stop-word entry (recommendation: no — see Open questions above).
- What to do if the enlarged set pins ≥ 0.7 and a short on-topic query loses its only lexical hit
  (recommendation: surface to the human; ADR-015 already names the vector leg as the carrier).

Ready for implementation (after the PA applies the ADR-015 Status-line note and the glossary rows).

### [PA] 2026-10-09 00:55 — Grooming fix (one version-bearing query joins the protocol, from rollup 194)

PR review of #47 (rollup `tasks/194`, Blocker 1) found that `query_terms`'s `\w+` split `4.1` into the
phantom terms `4` / `1`, which the index's standard tokenizer never emits — `"gpt-4.1 pricing"` seeded 20
off-topic text candidates at `0.5` on a corpus with 0 rows naming it. 194 fixes the tokenizer
(`\w+(?:\.\w+)*`); this task's protocol gains ONE version-bearing off-topic query, `"gpt-4.1 pricing"`
(M = 3), so the closed leak class stays measured by the re-pin table rather than only by a unit test.
Scope 1's quoted ADR-015 note and the Log-table AC name it; the near-miss count (≥ 5) is unchanged —
this query is a separate class, not a sixth near-miss. Nothing else in the task changes.

Addendum (same entry, 00:58): the query only reads M = 3 / N = 0 once 194's tokenizer is in; on today's
`\w+` it is M = 4 and seeds 20 candidates, which would break the "every off-topic N == 0" pin rule — so
`Depends on:` / `Blocked by:` now name `tasks/194`; run this task after 194 lands.

### [PA] 2026-10-09 02:05 — Grooming fix (the Gemini check names its `TOP_K`)

At 194's acceptance review `make memory-search QUERY="Gemini" TOP_K=5` logged `20 candidate(s)`, not
37: the text leg's `$limit` is `top_k * _CHILD_HITS_PER_PARENT` (4), so the CLI line shows
`min(N, TOP_K × 4)` — 37 at the default `TOP_K=10` (192's run), 20 at `TOP_K=5`. Scope 6, the live AC
and Story 5 now say so, so the cap is not read as a drop. The uncapped table's 37 is unchanged. 194 is
done; this task is unblocked.
