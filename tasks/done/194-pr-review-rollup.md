---
status: done
feature: atlas-search-text-leg
---

# [PR review rollup] Atlas Search text leg with a minimum-match ratio

Tags: `rollup`, `pr-review`
Refs: PR #47 (branch: `feat/atlas-search-text-leg`, head `440ff70`)

## Scope

PR Reviewer found 2 Blocker(s) and 7 Nit(s) in the diff. The SWE must fix Blocker 1 (and may fix Nits at
their discretion) in a single coordinated pass; Blocker 2 is PA's (a doc-discipline note that follows
from the Blocker 1 fix). Then hand back to the Tester. Pipeline re-runs from QA → PA acceptance → push →
re-review.

Everything else reviewed is sound: tenant isolation of the `$search` filter (pins first, `node_filter`
ANDed, keys restricted to the index's filter paths, `mustNot` parent exclusion), the shared mongot
helpers, the drift check, the legacy-index drop, config bounds, types/async/logging per AGENTS.md, the
glossary + ADR-015 + amended Status lines, and the test coverage (1148 tests in the touched modules pass
locally; `make memory-format-check` / `make memory-lint-check` green).

## Acceptance Criteria

- [x] Blocker 1: `query_terms` keeps decimal tokens whole, the way the index's `lucene.english`
      (standard / UAX#29) tokenizer does — `query_terms("gpt-4.1 pricing") == ["gpt", "4.1", "pricing"]`,
      `query_terms("voyage-3.5 embeddings") == ["voyage", "3.5", "embeddings"]`, every existing
      `TestQueryTerms` pin unchanged; the fake in `tests/unit/memory/conftest.py` tokenises with the SAME
      pattern (one shared constant); a behavioural test pins that a row containing the digits `4` and
      `1` but not `4.1` is NOT a text candidate for `"gpt-4.1 pricing"` at ratio `0.5`.
- [x] Blocker 2 [PA]: ADR-015 §3 and the glossary's **Minimum match ratio** row no longer describe the
      tokenizer as `\w+` — a Status-line note on ADR-015 (the mechanism task 191 already used) and the
      one-phrase glossary edit; optionally one version-bearing query added to task 193's re-pin protocol.
      *Applied by the PA 2026-10-09 (see Log): ADR-015 line 3 carries the task-194 note (§3's BODY still
      reads `\w+` by design — amendments live on the Status line, as 191's K-formula note does; check
      line 3, not §3); glossary row 39 reads `\w+(?:\.\w+)*` with the one-phrase why; task 193's
      protocol gained `"gpt-4.1 pricing"` (Scope 1 quoted note, Scope 2, Log-table AC). Evidence:
      `git diff --numstat docs/adrs docs/glossary.md` → `1 1` on 015 and glossary (glossary `2 2` once
      Nit 4 landed — see the 01:20 PA entry), the ADR hunk (`-U0`) `@@ -3 +3 @@`; `grep -n '\\w+' docs/glossary.md` → only the new pattern.*
- [x] Tester re-runs full QA suite and PASSES (including the new regression tests), and reproduces the
      live check below (`"gpt-4.1 pricing"` → 0 text candidates on the local corpus at `0.5`).
- [x] PA re-runs acceptance review and ACCEPTS.
- [x] PR Reviewer re-runs and reports `NO BLOCKERS`. *Round 2, head `717a443`, 2026-10-09 — see the PR Reviewer Log entry.*

## Blockers (detail)

### 1. [Standards / correctness] — `apps/memory/src/tree/memory/rag/search.py:356` (`query_terms`)
- **What's wrong:** `re.findall(r"\w+", …)` splits decimal / version tokens that the index keeps whole.
  mongot's `lucene.english` uses the standard (UAX#29) tokenizer, which does not break on `.` between
  digits (or between letters): `4.1`, `3.5`, `0.70`, `e.g` are single index tokens. Verified live on the
  local `text_search_index` (413 rows): `$search` for `"4.1"` → 0 rows, `"3.5"` → 0, `"e.g"` → 23 (one
  token), while bare `"4"` → 67 rows and `"1"` → 95 rows. So a query like `"gpt-4.1 pricing"` becomes
  `["gpt", "4", "1", "pricing"]` (M = 4, K = 2) and the phantom terms `4` and `1` match any row carrying
  those digits anywhere. Measured with the leg's exact compound shape (user/kind/child pins + `mustNot`)
  at the pinned `0.5`:

  | query | `\w+` terms | candidates | decimal-aware terms | candidates | rows naming the topic |
  |---|---|---|---|---|---|
  | `gpt-4.1 pricing` | gpt, 4, 1, pricing (K=2) | **20** | gpt, 4.1, pricing (K=2) | 0 | 0 |
  | `voyage-3.5 embeddings` | voyage, 3, 5, embeddings (K=2) | **22** | voyage, 3.5, embeddings (K=2) | 0 | 0 |
  | `claude 3.5 sonnet` | claude, 3, 5, sonnet (K=2) | **18** | claude, 3.5, sonnet (K=2) | 0 | 0 |
  | `python 3.12 asyncio` | python, 3, 12, asyncio (K=2) | **10** | python, 3.12, asyncio (K=2) | 1 | 0 |

  Twenty off-topic text candidates for a topic with zero rows is the exact failure the feature exists to
  remove ("off-topic → 0 text candidates", PR test plan), and model-version queries are the common case
  in this corpus's domain. The inverse also holds: a row that DOES say `gpt-4.1` only matches `gpt`
  (its token is `4.1`, never `4`), so on-topic rows lose a term they should get.
- **Why it's a Blocker:** a bug in the core K-of-M rule, measurable on the shipped corpus at the shipped
  ratio; distinct from task 193 (that leak is small-M on real generic words; this one is phantom terms
  that are not words at all).
- **Suggested fix:** tokenise the query the way the index does for the measured case —
  `re.findall(r"\w+(?:\.\w+)*", query.lower())` keeps `4.1` / `0.70` / `e.g` whole and still yields
  `vector-like` → `vector`, `like` and `bread.` → `bread`. Checked against every current
  `TestQueryTerms` pin (and the 10-query re-pin set): all unchanged; only decimal / dotted-abbreviation
  inputs differ. Hoist the
  pattern to a module constant (e.g. `_TOKEN_RE`) and have the fake's `_text_clause_matches`
  (`tests/unit/memory/conftest.py:114`) use the same constant so production and the double cannot
  diverge. Colon-joined identifiers (`genai:gemini`) are a possible further UAX#29 mismatch — not
  measured here; the SWE may leave them out.
- **Regression test:** `TestQueryTerms` cases `"gpt-4.1 pricing"` → `["gpt", "4.1", "pricing"]`,
  `"voyage-3.5"` → `["voyage", "3.5"]`, `"bread."` → `["bread"]`; and in `TestMinimumMatchRatio` a
  text-only row with content such as `"chapter 4 of 1 book"` asserted ABSENT from the hits for
  `"gpt-4.1 pricing"` at `0.5` (M = 3, K = 2), plus a row containing `gpt-4.1 pricing` asserted present.

### 2. [PA] [Documentation discipline] — `docs/adrs/015_atlas_search_text_leg_min_match_ratio.md` §3, `docs/glossary.md` (**Minimum match ratio** row)
- **What's wrong:** both state the query side is `re.findall(r"\w+", …)` / "lower-cased `\w+` tokens".
  Once Blocker 1 lands, the code contradicts an Accepted ADR and the glossary definition.
- **Why it's a Blocker:** ADR contradicted without supersession (conditional on Blocker 1; it is one
  sentence to fix).
- **Suggested fix:** append a Status-line note on ADR-015 ("§3's tokenizer keeps decimal tokens whole,
  `\w+(?:\.\w+)*`, matching the standard tokenizer — task 194") and change the glossary phrase. Optional:
  add one version-bearing query (e.g. `"gpt-4.1 pricing"`, off-topic on this corpus) to task 193's re-pin
  protocol so the leak class stays covered.

## Nits (non-blocking; will be appended to PR description if pipeline advances)

### 1. [Standards] — `apps/memory/src/tree/memory/rag/search.py:53-54`
- **Suggestion:** `search.py` imports the underscore-private `_TEXT_INDEX_FILTER_PATHS` /
  `_TEXT_INDEX_TEXT_PATHS` from `indexing.py`. They are now a two-module contract, like
  `TEXT_SEARCH_INDEX_NAME` / `VECTOR_INDEX_NAME`; drop the underscore.

### 2. [Clean code] — `apps/memory/src/tree/memory/rag/search.py:286` (`_search_index_is_queryable` docstring)
- **Suggestion:** "only on the path where the leg matched nothing — rare for a real query" is now false
  for the text leg: an empty text leg is the DESIGNED outcome of every off-topic query at `0.5`, so each
  one pays one `listSearchIndexes` round trip. The cost is fine (a metadata command on an LLM-paced
  path); the docstring should say so instead of calling it rare.

### 3. [Simplicity] — `apps/memory/src/tree/memory/rag/indexing.py:773` (`_create_search_index`)
- **Suggestion:** `shrink:` `except Exception` turns EVERY driver failure (auth, network, a malformed
  definition) into a message that opens with the Atlas M0 cap story. Decorate only when the driver error
  mentions `MAXIMUM_INDEXES_FOR_TENANT_EXCEEDED` / "maximum number of FTS indexes"; let the rest
  propagate unchanged so an unreachable mongot does not read as a cap problem.

### 4. [Documentation discipline] — `docs/glossary.md:41` (**Mode reset** row)
- **Suggestion:** "Classic indexes come back at the next boot (Beanie), `vector_index` at the next
  indexing run" — dropping `memory` drops both mongot indexes, so `text_search_index` comes back at the
  same indexing run. Name both.
- *Applied by the PA 2026-10-09 (see Log): glossary row 41 now reads "`vector_index` and
  `text_search_index` at the next indexing run (dropping `memory` drops both mongot indexes)". Evidence:
  `git diff -U0 docs/glossary.md` → hunks `@@ -39 +39 @@` (Blocker 2) and `@@ -41 +41 @@` (this nit);
  `grep -n 'text_search_index. at the next indexing run' docs/glossary.md` → row 41 only.*

### 5. [Clean code] — `apps/memory/tests/unit/memory/rag/test_load.py:263`
- **Suggestion:** comment still says "a URI-shaped `name` is also tokenised by `$text`"; it is the
  **Text search index** now (the `load.py` docstring in this diff already says so).

### 6. [Untested, edge] — `apps/memory/src/tree/memory/rag/indexing.py:645` (`_existing_text_index_mappings`)
- **Suggestion:** the `definition` fallback (an entry without `latestDefinition`) has no test; one more
  `pytest.param` in `test_matching_index_is_left_alone` with `{"name": …, "definition": {…}}` pins it.

### 7. [Simplicity] — `apps/memory/src/tree/memory/rag/search.py` (`_text_search`)
- **Suggestion:** `k` is computed before the `if not terms` early return and the early return then logs
  literal `0, 0, 0`; move the `k =` line below the guard (or log `k`) so the two cannot drift.

---

Refs: PR #47

## Log

### [PA] 2026-10-09 00:55 — Blocker 2 applied (ADR-015 Status-line note + glossary phrase + 193 query)

ADRs and the glossary are PA-owned, so the PA applied Blocker 2 ahead of Blocker 1; the SWE skips it.
(1) ADR-015 Status line: one more " — … per task 194." note appended after the 191 note, recording that
§3's query-side tokenizer is `re.findall(r"\w+(?:\.\w+)*", query.lower())` (decimal / dotted tokens
stay one term, as the index's standard tokenizer keeps them; one module constant shared with the
unit-test fake). Body text, Diagram and §3 untouched — 191's precedent. (2) Glossary **Minimum match
ratio** row: "lower-cased `\w+` tokens" → "lower-cased `\w+(?:\.\w+)*` tokens — decimals such as
`4.1` stay whole, as the index's standard tokenizer keeps them"; rows **Retrieval outcome** / **Text
search index** never named the pattern, so no edit. (3) Task 193 (optional item, taken): `"gpt-4.1
pricing"` (M = 3) added as ONE version-bearing off-topic query — Scope 1's quoted ADR note, Scope 2 and
the Log-table AC updated; 193 now names `tasks/194` in `Depends on:` / `Blocked by:` (the query reads
M = 3 only on 194's tokenizer); near-miss count unchanged; one `[PA]` log entry there. Verification:
`git diff --numstat docs/adrs docs/glossary.md` → `1 1` on 015 and glossary only; `git diff -U0` hunks
`@@ -3 +3 @@` (ADR) and `@@ -39 +39 @@` (glossary); `grep -nF '\w+(?:\.\w+)*'` hits ADR-015 line 3,
glossary row 39 and 193; `grep -n '\\w+' docs/glossary.md` → only the new pattern. Not committed —
ships with the 194 fix commit. The constant's name (`_TOKEN_RE` in the rollup) is the SWE's to pick; the
ADR note deliberately does not name it.

### [SWE] 2026-10-09 01:04 — Implementation (Blocker 1 + Nits 1–3, 5–7; Nit 4 handed to the PA)

**Files modified**
- `apps/memory/src/tree/memory/rag/search.py` — new public `TOKEN_PATTERN: re.Pattern[str] = re.compile(r"\w+(?:\.\w+)*")` (docstring: why, Good/Bad, colon-joined identifiers NOT modelled); `query_terms` tokenises with it; `query_terms` / `_text_search` / `STOP_WORDS`-comment wording now name `TOKEN_PATTERN` instead of `\w+` (Blocker 1). Imports `TEXT_INDEX_FILTER_PATHS` / `TEXT_INDEX_TEXT_PATHS` without the underscore (Nit 1). `_search_index_is_queryable` docstring: an empty text leg is the designed off-topic answer, so each off-topic query pays one `listSearchIndexes` round trip (Nit 2). `k =` moved below the `if not terms` guard (Nit 7).
- `apps/memory/src/tree/memory/rag/indexing.py` — `_TEXT_INDEX_FILTER_PATHS` / `_TEXT_INDEX_TEXT_PATHS` → `TEXT_INDEX_FILTER_PATHS` / `TEXT_INDEX_TEXT_PATHS` (Nit 1). `_create_search_index` decorates ONLY a driver error whose message carries `MAXIMUM_INDEXES_FOR_TENANT_EXCEEDED` or "maximum number of FTS indexes" (case-insensitive); everything else re-raises unchanged; docstring updated (Nit 3).
- `apps/memory/tests/unit/memory/conftest.py` — the fake's `_text_clause_matches` tokenises row text with the imported `TOKEN_PATTERN` (one shared constant; `import re` gone); module / function / class docstrings no longer say `\w+` — the class docstring now says the fake keeps `4.1` whole but still splits colon-joined identifiers (`google_genai:gemini`) and does not stem.
- `apps/memory/tests/unit/memory/rag/test_search.py` — 5 `TestQueryTerms` params (`decimal-stays-whole`, `version-stays-whole`, `version-query-three-terms` (the AC's literal `"voyage-3.5 embeddings"`), `dotted-abbreviation` `"e.g. 0.70"` → `["e.g", "0.70"]`, `trailing-dot-dropped`); `TestMinimumMatchRatio::test_decimal_query_term_never_matches_its_bare_digits`; class docstring.
- `apps/memory/tests/unit/memory/rag/test_indexing.py` — `TestCreateSearchIndex::test_other_driver_errors_propagate_unchanged` (Nit 3); `definition-fallback` param on `test_matching_index_is_left_alone` (Nit 6).
- `apps/memory/tests/unit/memory/rag/test_indexing_mongot_filter_paths.py` — renamed imports + module docstring (Nit 1).
- `apps/memory/tests/unit/memory/rag/test_load.py` — comment: "tokenised by `$text`" → "by the **Text search index**" (Nit 5).

**Tests**
- Red first (old `\w+` in prod AND fake): 5 failed for the right reason — `['gpt', '4', '1', 'pricing'] != ['gpt', '4.1', 'pricing']`, `['voyage', '3', '5']`, `['e', 'g', '0', '70']`, behavioural `['digits', 'on-topic'] != ['on-topic']`, and the non-cap `OperationFailure` re-raised as `RuntimeError`. (`bread.` and `definition-fallback` were already green — pins, not regressions.)
- Unit: `make memory-tests` → `5291 passed`. Integration: N/A (no suite, per AGENTS.md).
- `make memory-format-fix && make memory-lint-fix && make memory-format-check && make memory-lint-check` → "341 files already formatted", "All checks passed!"; `make pre-commit` → all hooks Passed.

**Acceptance criteria**
- [x] Blocker 1 — `query_terms("gpt-4.1 pricing") == ["gpt", "4.1", "pricing"]`, `query_terms("voyage-3.5 embeddings") == ["voyage", "3.5", "embeddings"]`, `"voyage-3.5"` → `["voyage", "3.5"]`, `"bread."` → `["bread"]` (`TestQueryTerms::test_terms[decimal-stays-whole|version-stays-whole|version-query-three-terms|trailing-dot-dropped|dotted-abbreviation]`); every pre-existing `TestQueryTerms` pin unchanged; the fake uses the SAME `TOKEN_PATTERN`; row `"chapter 4 of 1 book"` absent and row `"gpt-4.1 pricing per token"` present for `"gpt-4.1 pricing"` at `0.5`, log `text leg: 1 candidate(s), 3 query term(s), min_should_match=2` (`TestMinimumMatchRatio::test_decimal_query_term_never_matches_its_bare_digits`).

**Nits**
1. Done — underscore dropped in `indexing.py`, `search.py`, `test_indexing_mongot_filter_paths.py`. **For the PA:** ADR-015 line 73 (body) still names `_TEXT_INDEX_FILTER_PATHS`; SWE is read-only on ADRs — a Status-line note (or nothing, if the body is treated as historical) is the PA's call. `tasks/done/190` / `191` left as history. `_VECTOR_INDEX_FILTER_PATHS` kept private (single-module use; out of scope).
2. Done — docstring now reads correctly for both legs.
3. Done — narrowed, test added. Inlined the check rather than a new helper (smaller diff).
4. **NOT changed by the SWE — handed to the PA.** `docs/glossary.md` is PA-owned and the SWE is read-only on it (only mechanical fixes the PA explicitly asks for). Suggested phrase for the **Mode reset** row: "Classic indexes come back at the next boot (Beanie), `vector_index` and `text_search_index` at the next indexing run" — it can ship in the same uncommitted glossary edit as Blocker 2.
5. Done. 6. Done (`definition-fallback`). 7. Done — `k` is computed only after the guard; the M = 0 line stays pinned by `test_all_stop_word_query_sends_no_search_and_stays_hybrid`.

**Evidence — live, local mongot, user `6ac0f976f5896c3a79957359`, 372 child chunks** (the reviewer's "413 rows" is the same corpus: 372 children + 27 parents + 14 documents; the leg's filter / `mustNot` keep only children)
Same method as 192's Log (REAL `_text_search`, exact `$search` compound, no embedding, `limit=100000`), run on the pre-fix tree (`before.txt`) and after (`after.txt`); `diff before.txt after.txt` → the `children: 372` line and all 10 re-pin rows byte-identical; ONLY the four decimal rows differ:
```
before: 'gpt-4.1 pricing'       ['gpt','4','1','pricing'] 4        ... 0.5:K2->20
after:  'gpt-4.1 pricing'       ['gpt','4.1','pricing'] 3          ... 0.5:K2->0
before: 'voyage-3.5 embeddings' ['voyage','3','5','embeddings'] 4  ... 0.5:K2->22
after:  'voyage-3.5 embeddings' ['voyage','3.5','embeddings'] 3    ... 0.5:K2->0
before: 'claude 3.5 sonnet'     ['claude','3','5','sonnet'] 4      ... 0.5:K2->18
after:  'claude 3.5 sonnet'     ['claude','3.5','sonnet'] 3        ... 0.5:K2->0
before: 'python 3.12 asyncio'   ['python','3','12','asyncio'] 4    ... 0.5:K2->10
after:  'python 3.12 asyncio'   ['python','3.12','asyncio'] 3      ... 0.5:K2->1
```
(`python 3.12 asyncio` → 1 at 0.5 is the rollup's own measured decimal-aware number — a real two-word co-occurrence, not a phantom term.) Unchanged rows at 0.5: dark photon K3→0, sourdough K2→0, zxqv K2→0, quantization K4→0, context engineering K1→145, structured outputs pydantic K2→58, ReAct agent tool calling K2→183, pydantic 19, ReAct 55, Gemini 37.
CLI end to end:
```
$ make memory-search QUERY="gpt-4.1 pricing" TOP_K=5
vector leg: 20 candidate(s), 0 kept at min_vector_score=0.70 (top=0.672)
text leg: 0 candidate(s), 3 query term(s), min_should_match=2
No child hits for query (outcome=nothing_found, search_mode=hybrid): gpt-4.1 pricing
```

**Notes**
- Constant is public `TOKEN_PATTERN` (not `_TOKEN_RE`): conftest imports it across modules, and a private cross-module import is exactly Nit 1's complaint.
- Known remaining UAX#29 mismatches, deliberately out of scope (the rollup allows it): colon-joined identifiers (`genai:gemini` — named in the `TOKEN_PATTERN` and fake docstrings); a digit-dot-letter run such as `v1.a` stays one token here but splits in UAX#29 (noted here only; rare in queries).
- Not committed. The Docker prefect worker was not touched (no pipeline run needed); no processes left running.

### [PA] 2026-10-09 01:20 — Nit 4 applied + Nit 1 follow-on on ADR-015 (Status line only)

(1) Nit 4, glossary **Mode reset** row 41: "`vector_index` at the next indexing run" → "`vector_index`
and `text_search_index` at the next indexing run (dropping `memory` drops both mongot indexes)" — the
SWE's wording plus the one-phrase why. Rows 35 / 57 already name both indexes; no other edit. (2) Nit 1
follow-on: ADR-015 BODY line 73 (§6) still reads `_TEXT_INDEX_FILTER_PATHS` BY DESIGN — bodies are
historical, amendments live on the Status line (191 / 192 / Blocker 2 precedent). The existing task-194
Status-line note (line 3) was extended, not duplicated: it now names the shared tokenizer constant
(`TOKEN_PATTERN` in `search.py`, which the earlier note left to the SWE) and records that §6's filter-path
constant is the public `TEXT_INDEX_FILTER_PATHS` (a two-module contract with `search.py`, so no
underscore). Reviewers: check line 3, not line 73. No new ADR, no supersession — a constant's visibility
is not a design change. Verification: `git diff --numstat` → `1 1` on ADR-015 (single `@@ -3 +3 @@`
hunk), `2 2` on the glossary (`@@ -39 +39 @@`, `@@ -41 +41 @@`); `grep -n '_TEXT_INDEX_FILTER_PATHS'
docs/ apps/` → only ADR-015 line 73 (the code is renamed). Not committed — ships with the 194 fix commit.

### [Tester] 2026-10-09 — QA

**Test summary**
- Format / lint / pre-commit: PASS (`make memory-format-check` 341 files formatted; `make memory-lint-check` all checks passed; `make pre-commit` all hooks Passed). `make env-status` → local.
- Unit tests: `make memory-tests` -> 5291 passed / 0 failed (57.8 s). Integration: N/A (none, per AGENTS.md).
- Warnings: 0 new (run summary line reports only "5291 passed").

**E2E adversarial pass**
- Happy path (live, local mongot, user paul.iusztin@example.com, 372 children): `make memory-search QUERY=... TOP_K=5` -> `"gpt-4.1 pricing"`: `text leg: 0 candidate(s), 3 query term(s), min_should_match=2`, nothing_found; `"voyage-3.5 embeddings"` and `"claude 3.5 sonnet"`: same `text leg: 0 candidate(s), 3 query term(s), min_should_match=2` (the vector leg alone kept 4 for voyage-3.5, top=0.711 - vector side, not the text leg). PASS.
- Re-pin rows (own script, REAL `_text_search`, uncapped): all 10 rows reproduce 192's table exactly (dark photon K1->93 K2->6 K3->0; sourdough K1->1 K2->0; zxqv 0; quantization K1->79 K2->5 K3->1 K4->0; context engineering K1->145 K2->50; structured outputs K2->58 K3->13; ReAct agent tool calling K2->183 K3->83 K4->34; pydantic 19; ReAct 55; Gemini 37). Decimal rows at 0.5: gpt-4.1 pricing 0, voyage-3.5 embeddings 0, claude 3.5 sonnet 0, python 3.12 asyncio 1 (K=2, a real co-occurrence). PASS.
- Break path 1 (tokenizer vs Lucene, hostile/Unicode inputs): a scratch DB `qa194_scratch` (dropped afterwards) with a `lucene.english` index, a doc per tricky string, each term of our `TOKEN_PATTERN` run as a single `$search` clause and checked against its own doc. Terms that MATCH their own doc (our split == the index token): `3.5.1`, `1.2.3.4`, `v2.0-beta`->`v2.0`,`beta`, `U.S.A.`->`u.s.a`, `e.g.`->`e.g`, `a.b.c`, `0.70`, `10.5%`->`10.5`, `$4.99`->`4.99`, `www.example.com`, `file.py`, `3.12.1rc1`, `foo_bar.baz`, `naive.cafe`-style accented, Arabic-Indic `١٢٣.٤`, `3.5-turbo`, `5/6.2`->`5`,`6.2`, `gpt-4.1`; `trailing.`/`.leading`/`..` produce `trailing`/`leading`/no terms. PASS. Residual mismatches (phantom terms - the index holds no such token):
  - comma-grouped numbers: `1,000` -> ours `1`,`000`; index token `1,000` (a `$search` for `1` or `000` does not match a doc holding `1,000`). Live effect on the shipped corpus at 0.5: `"sourdough 1,000 tokens"` (M=4, K=2) -> **9** text candidates for a topic with 1 row; `"gpt pricing 1,000 tokens"` (M=5, K=3) -> 0. Pre-existing (same under old `\w+`), not a regression of 194.
  - apostrophe words: `o'reilly` -> ours `reilly`, index token `o'reilly` (`reilly` alone matches 0 docs); `ab'cd` -> `ab`,`cd` (0 matches). Phantom term inflates M (under-recall), cannot leak. `user's` -> `user` works (possessive filter).
  - colon-joined (`genai:gemini`) - already named as out of scope in the SWE's docstring; `v1.a`/`x.1`/`1.x` (digit-dot-letter): Lucene splits, so the single clause becomes an OR of the two pieces - looser, M not inflated (noted by the SWE).
  Verdict for this break path: the Blocker-1 class (dotted decimals/versions/abbreviations) is closed; the residual classes above are outside the AC and were not introduced by 194 -> PASS with note.
- Break path 2 (state/failure: `_create_search_index` non-cap errors): `TestCreateSearchIndex::test_other_driver_errors_propagate_unchanged` asserts `excinfo.value is driver_error` for `OperationFailure("connection refused: mongot:27028")`; the cap path still wraps (existing `test_*` asserting the RuntimeError with `__cause__`). Code read: the substring check is `"MAXIMUM_INDEXES_FOR_TENANT_EXCEEDED" not in message and "maximum number of fts indexes" not in message.lower()` -> bare `raise`. PASS.
- Break path 3 (boundary): empty / all-stop-word / `..` queries -> `query_terms` gives `[]` -> M=0 guard returns `[]` before `min_should_match` (`k` now below the guard); covered by `test_all_stop_word_query_sends_no_search_and_stays_hybrid`. PASS.

**Acceptance criteria**
- [x] PASS - Blocker 1: `TOKEN_PATTERN = re.compile(r"\w+(?:\.\w+)*")` public in `search.py:68`, used by `query_terms` (search.py:375) and the conftest fake (`conftest.py:115`, imported, `import re` gone); `TestQueryTerms[decimal-stays-whole|version-stays-whole|version-query-three-terms|dotted-abbreviation|trailing-dot-dropped]` and `TestMinimumMatchRatio::test_decimal_query_term_never_matches_its_bare_digits` pass (in the 5291); live 0 candidates at 0.5 (above).
- [x] PASS - Blocker 2 [PA]: `git diff -U0 docs/adrs` -> single hunk `@@ -3 +3 @@`, numstat `1 1`; line 3 names `re.findall(r"\w+(?:\.\w+)*", ...)`, `TOKEN_PATTERN` in `search.py`, public `TEXT_INDEX_FILTER_PATHS`, "per task 194"; glossary hunks `@@ -39 +39 @@` (pattern + why) and `@@ -41 +41 @@`, numstat `2 2`; tasks/193 has `Depends on:` and `Blocked by:` naming `tasks/194-pr-review-rollup.md`. (Presence/topic only.)
- [x] PASS - Tester AC (full suite + live reproduction).
- Nits: 1 PASS (no `_TEXT_INDEX_*` left in code; only ADR-015 body line 73 by PA design); 2 PASS (docstring says an empty text leg is the designed off-topic answer); 3 PASS (above); 4 PASS (glossary row 41 names both indexes); 5 PASS (`test_load.py:263`); 6 PASS (`definition-fallback` param); 7 PASS (`k` after the guard).
- Remaining unchecked: PA acceptance, PR Reviewer re-run (not mine).

**Other issues found (non-blocking; suggest a follow-up task or a one-line note in 193)**
- Comma-grouped numbers still produce phantom terms and a measured live leak: `"sourdough 1,000 tokens"` -> 9 candidates at 0.5 on the shipped corpus. A pattern that keeps a comma between DIGITS inside a token (e.g. `\w+(?:\.\w+|(?<=\d),(?=\d)\w+)*`) would match Lucene for `1,000` / `1,000.50`; letter,letter commas must stay split (Lucene splits them). Apostrophe-internal words (`o'reilly`) similar but under-recall only.
- ADR/docs: the TOKEN_PATTERN docstring lists colon-joined identifiers as unmodelled but not commas/apostrophes; if the SWE does not fix, add them to that list.
- Docker `tree-prefect-worker` untouched (Up); scratch DB `qa194_scratch` dropped; no processes left running.

**VERDICT: PASS**

### [PA] 2026-10-09 02:05 — Acceptance Review (round 2, head `8c077eb`)

**VERDICT: ACCEPT**

Reviewed evidence from the Tester log entry and re-walked the feature live from the user's POV
(`make env-status` → local; `make memory-search`, local mongot, user `paul.iusztin@example.com`,
372 child chunks). All acceptance criteria verified from the user's POV. Hand off to the PR Reviewer.

**What 194 changed, as the user sees it**
- Version-bearing questions no longer seed the text leg through phantom digit terms. Live, `TOP_K=5`:
  `"gpt-4.1 pricing"` → `vector leg: 20 candidate(s), 0 kept at min_vector_score=0.70 (top=0.672)`,
  `text leg: 0 candidate(s), 3 query term(s), min_should_match=2`, `outcome=nothing_found` → "No results."
  `"claude 3.5 sonnet"` → `0 kept (top=0.692)`, `text leg: 0 candidate(s), 3 query term(s),
  min_should_match=2`, `nothing_found`. `"voyage-3.5 embeddings"` → `text leg: 0 candidate(s), 3 query
  term(s), min_should_match=2`; the vector leg alone keeps 4 (`top=0.711`) — that is the vector leg's
  `min_vector_score` pin, not the text leg, and outside this feature. All three M = 3 (was M = 4).
- On-topic queries unchanged (192's table): `"ReAct agent tool calling"` → `text leg: 20 candidate(s), 4
  query term(s), min_should_match=2`, "Building Production ReAct Agents From Scratch Is Simple" leads;
  `"Gemini"` → `text leg: 20 candidate(s), 1 query term(s), min_should_match=1`, vector `0 kept
  (top=0.697)`, the text leg alone answers it. (20 is the child `$limit` at `TOP_K=5` — `top_k × 4`;
  192's 37 was at the default `TOP_K=10`. Task 193's Scope 6 / AC / Story 5 said "logs 37" without the
  `TOP_K` — reworded today so the next runner does not read the cap as a drop.)
- Operator story: `_create_search_index` (`indexing.py:776-781`) re-raises bare unless the driver
  message names `MAXIMUM_INDEXES_FOR_TENANT_EXCEEDED` or "maximum number of FTS indexes"; only the cap
  gets the "Atlas M0 allows 3 … drop the stray index … re-run `make memory-run-indexing-pipeline`"
  message. An unreachable mongot now reads as what it is.
- Docs: ADR-015 line 3 carries the task-194 clause (tokenizer `\w+(?:\.\w+)*`, `TOKEN_PATTERN` shared
  with the fake, public `TEXT_INDEX_FILTER_PATHS`); glossary row 39 names the pattern with the why, row
  41 names both mongot indexes on **Mode reset**; both in commit `8c077eb`. Task 193 `Depends on:` /
  `Blocked by:` name 194 (lines 10 / 162) — 194 is done, so 193 is unblocked. Glossary terms used
  consistently in the diff (**Text search index**, **Minimum match ratio**).

**Known follow-ups in the PR body — none changes the verdict**
- Comma-grouped numbers: reproduced live — `"sourdough 1,000 tokens"` → `text leg: 9 candidate(s), 4
  query term(s), min_should_match=2`, `found`, five AI-article parents for a baking question. Same
  mechanism as Blocker 1 (a phantom `1` from `1,000`), but pre-existing under `\w+`, outside 194's AC
  (decimals), and disclosed. Not a rejection of 194; filed as a groomed follow-up with the live numbers:
  `tasks/195-comma-grouped-numbers-in-token-pattern.md` (also lists apostrophes / colons in the
  `TOKEN_PATTERN` docstring — the Tester's open note).
- Apostrophe words: under-recall only (measured 0 matches), bundled into 195's docstring item.
- Near-miss short queries: task 193, pending, depends on 194 (now satisfied).
- `_vector_search` `node_filter` dict-merge (`search.py:233`): no caller passes `user_id` / `kind`
  today; pre-existing and outside this feature — stays recorded in the PR body, as 192's acceptance
  already decided.
- Stale Docker worker image, served-flow INFO visibility: operator / runbook class, not product.
- PR body test plan says `5283 passed`; the Tester's run on 194 is `5291` — refresh the number when the
  PR Reviewer re-runs.

Files touched by this review (not committed): `tasks/done/194-pr-review-rollup.md` (this entry + the PA
AC tick), `tasks/193-near-miss-queries-in-the-ratio-repin-protocol.md` (`TOP_K` wording on the Gemini
check, three places), `tasks/195-comma-grouped-numbers-in-token-pattern.md` (new).

### [PR Reviewer] 2026-10-09 01:35 — Review (round 2, head `717a443`)

**VERDICT: NO BLOCKERS**

Reviewed 33 files, 4439 insertions / 479 deletions (`git diff 7669c81...717a443`, every file read in full),
plus the two round-2 commits in isolation (`8c077eb` fix, `717a443` docs). Blockers: 0; Nits: 5.
Local: `make env-status` → local; `make memory-format-check` 341 files formatted; `make memory-lint-check`
all checks passed; `make memory-tests` → 5291 passed in 57 s. Working tree clean; no stray files in the diff.

**Round-1 items, each verified in the code (not the tick):**
- Blocker 1 — `search.py:68` `TOKEN_PATTERN = re.compile(r"\w+(?:\.\w+)*")`, used by `query_terms`
  (`search.py:375`) and imported by the fake (`tests/unit/memory/conftest.py:34`, `_text_clause_matches`
  at `:115`; `import re` gone). `TestQueryTerms` pins `decimal-stays-whole`, `version-stays-whole`,
  `version-query-three-terms`, `dotted-abbreviation`, `trailing-dot-dropped`; every pre-existing pin
  unchanged. `TestMinimumMatchRatio::test_decimal_query_term_never_matches_its_bare_digits` pins the
  behaviour (row `"chapter 4 of 1 book"` absent, `"gpt-4.1 pricing per token"` present, log line
  `text leg: 1 candidate(s), 3 query term(s), min_should_match=2`).
- Blocker 2 [PA] — ADR-015 line 3 carries the task-194 note (pattern, `TOKEN_PATTERN` shared with the
  fake, public `TEXT_INDEX_FILTER_PATHS`); glossary row 39 reads `\w+(?:\.\w+)*` with the why; task 193
  carries `"gpt-4.1 pricing"` and names 194 in `Depends on:` / `Blocked by:`.
- Nit 1 — `TEXT_INDEX_TEXT_PATHS` / `TEXT_INDEX_FILTER_PATHS` public in `indexing.py:582/600`; no
  `_TEXT_INDEX_*` left in `apps/` (grep). Nit 2 — `_search_index_is_queryable` docstring
  (`search.py:300-305`) now says the empty text leg is the designed off-topic answer. Nit 3 —
  `_create_search_index` (`indexing.py:776-781`) wraps only a message naming
  `MAXIMUM_INDEXES_FOR_TENANT_EXCEEDED` / "maximum number of fts indexes", bare `raise` otherwise;
  `TestCreateSearchIndex::test_other_driver_errors_propagate_unchanged` asserts identity. Nit 4 —
  glossary row 41 names both mongot indexes. Nit 5 — `test_load.py:263`. Nit 6 —
  `definition-fallback` param on `test_matching_index_is_left_alone`. Nit 7 — `k =` sits below the
  `if not terms` guard (`search.py:435-438`).

**Dimensions on the whole diff (round 2):**
- A Performance — nothing: one `$search` per query, the probe only on an empty leg, `query_terms` on a
  short string, the index reconcile once per indexing run.
- B Clean code — nothing: no unused imports (ruff), no dead helpers (`_equals` ×4, `_log_text_leg` ×2,
  `_existing_text_index_mappings` ×2, `_set_static_mapping`, `_extract_text_index_field_types`), no
  prints, no owner-less TODOs; the verbatim Lucene stop list in `test_search.py` is a deliberate pin.
- C Untested — nothing: `_check_text_node_filter` (2 params), `_search_index_is_queryable` (absent /
  building / readiness-less / raising), `_drop_legacy_text_index` (present / absent / code 27 / other
  failure), `_create_search_index` (cap / non-cap / routed from `ensure_indexes`), drift (analyzer /
  missing filter path / `dynamic` / type, echoed extras, `dynamic` absent, `definition` fallback),
  ensure order, readiness loop parametrised over both indexes, the exact `$search` pipeline, K edge
  cases incl. float overshoot, cap at 64, tenant pins ANDed.
- D Standards — tenant pins first and ANDed in the text leg; keys restricted to the index's filter
  paths; `PydanticObjectId` is a `bson.ObjectId` subclass so the `str | ObjectId` check holds; no new
  `.env.example` knob (per AGENTS.md); logger not print; timezone n/a. Only Nit 2 below.
- E Documentation discipline — **Text search index**, **Minimum match ratio**, **Search mode**,
  **Retrieval outcome**, **`memory` collection**, **Mode reset** rows present and used consistently
  in docstrings; ADR-015 Accepted with Status-line notes on 008 / 012 / 013. Nits 1 and 4 below.
- F Simplicity / anti-over-engineering — nothing flagged. Walked what 194 introduced (public
  `TOKEN_PATTERN` — one constant, two consumers; cap-only wrapping inlined, no new helper; the
  `TEXT_INDEX_*` rename) and the rest (`_equals`, `_log_text_leg`, `_set_static_mapping`,
  `_text_search_index_drift` returning `(have, want)`, `fallback_mode` on the shared probe, the
  conftest `$search` emulation, the `events` recorder in `_make_collection`): each has two or more
  callers or mirrors the existing vector helper one-for-one; none crosses "more than we needed".

**Known follow-ups in the PR body** (comma-grouped numbers → 195; near-miss short queries → 193;
apostrophe words; `_vector_search` dict-merge; stale Docker worker; served-flow INFO logs): none is
blocking; none re-filed.

**Nits** (also appended to the PR description; caveman-format comment posted on the PR):
1. [PA] [Documentation discipline] — `docs/adrs/015_atlas_search_text_leg_min_match_ratio.md:85` (§7)
   — body: "A `create_search_index` failure (e.g. `MAXIMUM_INDEXES_FOR_TENANT_EXCEEDED`) raises a
   `RuntimeError` naming the M0 3-index cap"; since round-1 Nit 3 only the cap error is wrapped and every
   other driver error propagates unchanged. One clause on the existing task-194 Status-line note.
2. [Standards] — `apps/memory/tests/unit/memory/rag/test_indexing.py:156` (`_list_search`),
   `apps/memory/tests/unit/config/test_app_config.py:710` and `:745` — three added defs with no return
   annotation (AGENTS.md: return types even for `None`). Ruff does not enforce it; cosmetic.
3. [Clean code] — `apps/memory/src/tree/memory/rag/indexing.py:371` (130-char docstring line in
   `ensure_indexes`) and `:579-580` (the `TEXT_INDEX_TEXT_PATHS` comment wraps as "queries / use);"
   after the rename shortened it) — rewrap both to 88 columns.
4. [PA] [Documentation discipline] — `docs/adrs/006_rag_graphrag_memory_modes.md:189` (Diagram:
   "$vectorSearch + $text · RRF") and `docs/adrs/007_embedding_clusters_and_explicit_offline_phases.md:84`
   — both bodies still name the retired `$text` operator; ADR-015 lists 006 as relied-on-unchanged and
   192 scoped the Status notes to 008 / 012 / 013. Optional one-clause Status pointer on 006, the ADR a
   reader of hybrid search opens.
5. [Standards] — `apps/memory/src/tree/memory/rag/search.py:233` (`_vector_search` merges
   `**node_filter` over its `user_id` / `kind` pins) — disclosed in the PR body and pre-existing;
   `TestTenantPins` calls `_text_search` directly to dodge it. Give it a task number so it survives the
   merge; today the PR body is its only record.

Not committed — the orchestrator owns the commit; this entry and the AC tick ship with whatever commit
closes the hand-off.
