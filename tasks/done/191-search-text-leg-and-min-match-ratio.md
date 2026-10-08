---
id: 191-search-text-leg-and-min-match-ratio
status: done
feature: atlas-search-text-leg
---

# The text leg runs `$search` on `text_search_index` — one `text` clause per distinct content term, `minimumShouldMatch = max(1, ceil(query.text_min_match_ratio × M))` (`min_text_score` deleted), an absent / not-ready index reads as `vector_only`, an all-stop-word query answers `[]` — and the classic `text_index` leaves the Beanie set with an idempotent drop in `ensure_indexes`

Tags: `retrieval`, `rag`, `graphrag`, `config`, `indexing`
Depends on: 190
Blocks: 192
Implements: ADR-015 §1–§5, §7 (amends ADR-008 §3, ADR-012 §1–§2, ADR-013 §7)

## Problem

`_text_search` (`apps/memory/src/tree/memory/rag/search.py`) is the ONLY `$text` user, shared by rag
`retrieve_parents` (`node_filter {type: chunk, subtype: child}`) and the graphrag seed search
(`graph/retrieval.py:311`, `node_filter {}`). Its `textScore` cannot express word overlap (1 term ×5 ≈
2 terms ×1; saturates at 1.1), so `min_text_score` shipped at 0.0 and an 8-word off-topic query still
seeds a particle-physics paper through the single word "vector". Task 190 built `text_search_index`;
this task moves the leg onto it with an N-of-M rule and retires the classic index.

## Scope

**1. Query terms (`search.py`).**
- `STOP_WORDS: frozenset[str]` — vendored, NO new dependency. MUST contain Lucene's 33 English stop words
  verbatim (`a an and are as at be but by for if in into is it no not of on or such that the their then
  there these they this to was will with` — what `lucene.english` drops at index time, so a query term
  that can never match must not count toward M) ∪ question / pronoun / auxiliary / function words
  (roughly NLTK's English list: `what how why when where who whom which does do did is are was were been
  being have has had i me my we our you your he she it its they them about should can could would
  will …`). Docstring with one good and one bad example per AGENTS.md (good: `"What is the ReAct
  agent?"` → `["react", "agent"]`; bad: dropping a domain word like `"vector"` because it is common).
- `query_terms(query: str) -> list[str]`: `re.findall(r"\w+", query.lower())` → drop `STOP_WORDS` →
  dedupe preserving first occurrence. `M = len(terms)`. Pre-stemming dedupe is the accepted trade-off
  (`agent` / `agents` = 2 terms). The term counts in the User Stories and the live AC assume this
  list (`how does the when to a what is it` are stop words) — pin the frozenset first, then the stories.
- `min_should_match(term_count: int, ratio: float) -> int`: `0` when `term_count == 0`, else
  `max(1, ceil(ratio * term_count))` — so `0.0` = "any one term", `1.0` = all, never above `M` (Atlas
  rejects `minimumShouldMatch` > the number of should clauses).

**2. Config.** `QueryConfig.text_min_match_ratio: float = Field(0.5, ge=0.0, le=1.0,
allow_inf_nan=False)`; `min_text_score` DELETED from `app_config.py` (field + docstring paragraph),
`default.yaml` (the whole block at lines 118–140 → a SHORT comment: what the ratio is, `0.0` = any one
term, the K formula with one example, "PROVISIONAL 0.5 — re-pinned by task 192's eval, see ADR-015",
`TREE_QUERY__TEXT_MIN_MATCH_RATIO=…`), `tests/unit/config/fixtures/frozen_config.yaml` (lines 96–98), the
`min_vector_score` comments in both YAMLs ("`$text` hits clear min_text_score below" → "the text leg is
gated by `text_min_match_ratio` below"), `apps/memory/README.md:76` (the `query` key list),
`tree/memory/rag/types.py:179–180` and `:209–211` (RetrievalOutcome docstring + `outcome` description:
"the text leg on `query.text_min_match_ratio` (K of M query terms)"). After this task `grep -rn
"min_text_score\|MIN_TEXT_SCORE" apps/ .agents docs/glossary.md` is empty (ADR body text and `tasks/done`
excluded).

**3. `_text_search`** (same signature; `None` = raised, `[]` = ran and matched nothing):
- validate `node_filter` BEFORE the `try`: every key must be in `_TEXT_INDEX_FILTER_PATHS` (import from
  `indexing`) and every value a scalar (`str` / `ObjectId`) — else `ValueError` naming the key / value
  (a programming error must raise, never read as a dead leg);
- `terms = query_terms(query)`; `k = min_should_match(len(terms), app_config.query.text_min_match_ratio)`;
  **M == 0 → log the line (below) and return `[]` WITHOUT issuing a pipeline** (mode stays `hybrid`);
- pipeline, `$search` FIRST (Atlas requires it), no `$sort` (`$search` returns by score):
  ```
  [{"$search": {"index": TEXT_SEARCH_INDEX_NAME, "compound": {
       "filter": [{"equals": {"path": "user_id", "value": user_id}},
                  {"equals": {"path": "kind", "value": "node"}},
                  *({"equals": {"path": key, "value": value}} for key, value in node_filter.items())],
       "mustNot": [{"compound": {"must": [{"equals": {"path": "type", "value": "chunk"}},
                                          {"equals": {"path": "subtype", "value": "parent"}}]}}],
       "should": [{"text": {"query": term, "path": list(_TEXT_INDEX_TEXT_PATHS)}} for term in terms],
       "minimumShouldMatch": k}}},
   {"$addFields": {"_search_score": {"$meta": "searchScore"}}},
   {"$limit": limit}]
  ```
  ONE `text` clause per term carrying ALL four paths (four clauses per term would break K); the
  `mustNot` values come from `_PARENT_ROW`, which stays the one place the parent-exclusion invariant is
  spelled (module docstring: "`$nor`" → "`mustNot`");
- `try: aggregate … except Exception: WARN "Text search leg unavailable; the query runs vector-only" →
  None` (unchanged shape);
- empty result → `[] if await _search_index_is_queryable(collection, TEXT_SEARCH_INDEX_NAME) else None`
  (absent / `queryable is False` → `None` → `vector_only`; a raising probe or a readiness-less local
  entry → `[]`); the probe runs ONLY on an empty leg;
- `_gate_text_candidates` DELETED; no score gate of any kind on `searchScore`;
- the per-query INFO line, on every leg that RAN (candidates, empty, or M == 0), never on a raised leg:
  `text leg: %d candidate(s), %d query term(s), min_should_match=%d`.
- Docstrings: module (`$text` paragraph → the `$search` rule, the "A Parent chunk is NEVER a seed" block
  names `mustNot`), `hybrid_search` ("the text `$match`" → "the `$search` filter"), `_text_search`
  (what M, K, `STOP_WORDS` are; why no gate: RRF reads rank; the ratio applies to graphrag's seed search
  too, as the old gate's docstring said).

**4. The classic `text_index` goes.** `entities/memory.py`: delete `TEXT_INDEX_NAME`,
`TEXT_INDEX_FIELDS`, the `IndexModel(TEXT_INDEX_FIELDS, …)` entry and the docstring bullet
("`ensure_indexes` owns only the mongot vector index" → "owns the two mongot indexes"); the set becomes
`user_kind_type_subtype`, `user_type_name` (+ the three graphrag ones). `indexing.py`:
`_drop_legacy_text_index(collection)` — `index_information()`; `"text_index"` present → `drop_index
("text_index")` + INFO `Dropped legacy $text index 'text_index' (replaced by 'text_search_index',
ADR-015)`; absent → nothing (DEBUG at most). Called as step 3 of `ensure_indexes`, AFTER
`_ensure_text_search_index` returned (ready, or fail-open timeout — the `$text` leg no longer exists, so
the classic index serves nothing either way). Beanie only creates (`allow_index_dropping` off), so this
is the one retirement step ADR-012 §3 did not have; ADR-012's "no retirement code" stands for every
OTHER index.

**5. Tests** (`squid-testing-python`):
- `tests/unit/memory/conftest.py::FakeMemoryCollection`: a `$search` branch in `_apply` — `filter` =
  `equals` on the row (`matches`-style), `mustNot` = the nested `compound.must` all-equal → excluded,
  `should` = per-clause: any of the four paths (arrays included) contains the term as a whole
  `\w+`-token, case-insensitive; keep the row iff matched clauses ≥ `minimumShouldMatch`; `$limit`;
  `text_pipeline` keys on `$search`; the DEFAULT catalogue becomes TWO queryable entries (`vector_index`
  + `text_search_index`) — otherwise every existing test whose text leg answers `[]` (all the
  `_OFF_TOPIC` ones) silently flips from `hybrid` to `vector_only`.
- `test_search.py`: `_TEXT_STAGE = "$search"`; the autouse `text_gate_off` fixture → pins
  `text_min_match_ratio` (0.0, so legacy single-term fixtures keep matching), renamed; `TestMinTextScore`
  → `TestMinimumMatchRatio`: the EXACT pipeline for `"What is the ReAct agent tool calling loop?"` at 0.5
  (`terms == ["react", "agent", "tool", "calling", "loop"]`, K = 3, filter / mustNot / should / index
  asserted as one dict); stop words + dedupe (`"ReAct react REACT"` → one clause); ratio 0.0 → K = 1;
  1.0 → K = M; all-stop-word `"what is it"` → `[]`, NO `$search` pipeline recorded, mode `hybrid`, log
  line `text leg: 0 candidate(s), 0 query term(s), min_should_match=0`; the log line on a leg with
  candidates; no line on a raised leg; `node_filter={"merged_into": None}` → `ValueError` (not
  swallowed, not `vector_only`); absent `text_search_index` → `vector_only`; `BUILDING` → `vector_only`;
  readiness-less entry → `hybrid`; probe only on an empty leg (`test_index_probe_only_runs_on_empty_leg`
  now sees probes for neither index when both legs answer); `TestChildOnlyFilter` /
  `TestGraphSeedFilter` assert the `equals` clauses (`subtype: child` present for rag, absent for
  graphrag) and the parent-row behavioural exclusion through `mustNot`; `_break_leg` keyed on `$search`.
- `tests/unit/memory/rag/test_search.py`-level pure tests for `query_terms` / `min_should_match`,
  parametrised (empty, all-stop, dedupe, hyphen split `"vector-like"` → `vector`, `like`, the 8-term
  quantization query; `(0, 0.5) → 0`, `(3, 0.0) → 1`, `(3, 0.5) → 2`, `(8, 0.5) → 4`, `(3, 1.0) → 3`,
  `(1, 0.1) → 1`).
- `test_retrieval.py:431` (`test_nothing_found_when_both_legs_gate_to_nothing`): patch the ratio to 1.0
  with a two-term query of which the row matches one → text leg `[]`, vector gated → `nothing_found`,
  mode `hybrid`.
- `test_app_config.py`: the three `test_min_text_score_*` → `text_min_match_ratio` default 0.5 / YAML
  override / env override / `-0.1` and `1.5` rejected / `inf`, `nan` rejected / frozen config.
- `tests/unit/entities/test_memory.py` (`_BASE_INDEX_NAMES`, the `text_index` skip, delete
  `test_text_index_covers_names_aliases_and_content`) and `tests/unit/test_db.py::_BASE` (LIVE Mongo: the
  classic set no longer contains `text_index`).
- `test_indexing.py`: the drop runs when `index_information()` lists `text_index` and not otherwise;
  it runs AFTER the text-index ensure (call order); a drop failure propagates (no silent swallow).

## Acceptance criteria

- [x] `query_terms` lower-cases, splits on `\w+`, drops `STOP_WORDS` (a superset of Lucene's 33, listed
      verbatim in the frozenset), dedupes in order; `min_should_match` follows `max(1, ceil(ratio × M))`
      with `0` for `M == 0` and `1` at ratio `0.0`.
- [x] `_text_search` issues exactly the `$search` pipeline above (index name, `compound.filter` with
      `user_id` + `kind` + one `equals` per `node_filter` key, the nested-`compound` `mustNot`, one
      four-path `text` clause per term, `minimumShouldMatch = K`, `searchScore`, `$limit`); no `$sort`,
      no `$text`, no `$match` anywhere in the leg.
- [x] An all-stop-word query answers `[]` without a pipeline and keeps `search_mode: hybrid`; a
      `node_filter` key outside `_TEXT_INDEX_FILTER_PATHS` raises `ValueError` before any aggregate.
- [x] An empty leg whose `text_search_index` is absent or `queryable: false` answers `None` →
      `vector_only`; a raising probe or a readiness-less entry keeps `hybrid`; the probe never runs when
      the leg has candidates.
- [x] `text leg: N candidate(s), M query term(s), min_should_match=K` is logged at INFO on every leg that
      ran (including `0 … 0 … 0`) and never on a raised leg; `_gate_text_candidates` and every
      `min_text_score` reference are gone from `apps/`, `.agents`, `docs/glossary.md`.
- [x] `query.text_min_match_ratio` exists in `default.yaml`, `frozen_config.yaml`, `QueryConfig`
      (`0.5`, `[0, 1]`, no inf/nan) and via `TREE_QUERY__TEXT_MIN_MATCH_RATIO`; README:76 and
      `types.py` describe it.
- [x] `memory_indexes(mode)` declares no `text_index` in either mode; `ensure_indexes` drops a present
      `text_index` after the text-index ensure and is a no-op when it is absent; `tests/unit/test_db.py`
      passes live.
- [x] `FakeMemoryCollection` emulates the `$search` compound (filter, mustNot, should-count ≥ K) and
      carries both indexes in its default catalogue.
- [x] Live, local env, ratio 0.5: `make memory-run-indexing-pipeline` logs the `text_index` drop once
      (`db.memory.getIndexes()` no longer lists it; a second run logs no drop); `make memory-search
      QUERY="ReAct agent tool calling"` logs `text leg: N candidate(s), 4 query term(s),
      min_should_match=2` with N ≥ 1 and `search_mode: hybrid`; `make memory-search QUERY="what is it"`
      logs `0 candidate(s), 0 query term(s), min_should_match=0` and still answers `hybrid` (M = 0 never
      probes); with `text_search_index` dropped by hand, `make memory-search QUERY="ReAct agent tool
      calling"` (M ≥ 1, empty leg → probe) answers `search_mode: vector_only` and a WARN naming the
      index.
- [x] `make memory-format-check`, `make memory-lint-check`, `make pre-commit`, `make memory-tests` green
      (env-status local).

## User Stories

### Story: A user asks about a topic the memory holds, in a full sentence
1. In Claude Code the user asks "How does the ReAct agent decide when to call a tool?"; the skill calls
   `search_memory(query=…, top_k=5)`.
2. The server logs `text leg: 7 candidate(s), 5 query term(s), min_should_match=3`
   (`react agent decide call tool`; `how does the when to a` dropped) and `search_mode: hybrid`.
3. The answer cites the ReAct passages; no row that merely shares the word "tool" with the question
   reaches the fused top-5.

### Story: A user asks about something the memory does not hold
1. The user asks "MongoDB Atlas Vector Search scalar binary quantization int8" on a memory with no
   MongoDB page but a particle-physics paper mentioning "vector".
2. The text leg logs `0 candidate(s), 8 query term(s), min_should_match=4`; the physics paper is not a
   text candidate (one shared term of eight); the fused list no longer carries it on lexical grounds.

### Story: A one-word query still works
1. `make memory-search QUERY="Gemini"` → `1 query term(s), min_should_match=1`; every chunk naming
   Gemini is a text candidate, as before.

### Story: An all-stop-word query
1. `make memory-search QUERY="what is it"` → `text leg: 0 candidate(s), 0 query term(s),
   min_should_match=0`, `search_mode: hybrid`; the vector leg alone decides; no `$search` was sent.

### Story: An operator loosens the rule for one shell
1. `TREE_QUERY__TEXT_MIN_MATCH_RATIO=0.0 make memory-search QUERY="structured outputs pydantic"` → the
   line says `min_should_match=1`; `=1.5` fails fast with a one-line pydantic error naming
   `text_min_match_ratio`.

### Story: The index is missing on prod between deploy and the first indexing run
1. Horizon redeploys from `main` with `MCP_SKIP_INDEX_BOOTSTRAP=true`; `text_search_index` does not exist
   yet.
2. `search_memory` answers normally with `search_mode: vector_only`; the server logs one WARN naming
   `text_search_index` as absent; the skill prefixes its one caveat line.
3. The operator runs `make memory-run-indexing-pipeline` against prod; the next query is `hybrid`.

### Story: A graphrag seed search
1. With `TREE_MEMORY__MODE=graphrag`, `query_memory("ReAct")` seeds from the same `$search` leg with
   `node_filter={}` — entity rows and child chunks both qualify, parents never do (the `mustNot`), and the
   same log line appears.

### Story: The developer passes an unsupported filter
1. A new caller sends `node_filter={"merged_into": None}`; `_text_search` raises
   `ValueError: node_filter key 'merged_into' is not a text_search_index filter path (user_id, kind,
   type, subtype)` — the test fails, nothing reads as a dead leg.

---

Blocked by: 190

## Log

### [PA] 2026-10-08 19:30 — Grooming

**Summary**
Switch the shared text leg from `$text` to `$search` on `text_search_index` with a K-of-M rule derived from
`query.text_min_match_ratio`, delete the `textScore` gate and its knob, read a missing index as
`vector_only`, and retire the classic `text_index` in the same change.

**Key decisions**
- One `text` clause per term with all four paths; `minimumShouldMatch` counts TERMS, which is the whole
  point — four clauses per term would count paths.
- `STOP_WORDS` must be a superset of Lucene's 33: a term `lucene.english` drops can never match, so
  counting it toward M would silently raise K.
- M == 0 answers `[]` without a pipeline (Atlas rejects an empty `should`); mode stays `hybrid` — the leg
  ran and found nothing to ask.
- No `searchScore` gate: RRF reads rank; the ratio is the only text-leg rule.
- `node_filter` validation raises OUTSIDE the leg's `try` — a programming error is never a dead leg.
- The classic `text_index` removal + drop lands HERE, not in 190: dropping it before the leg switches
  kills lexical search on existing DBs, and Beanie would recreate it while it is still declared.
- The test fake gains a real `$search` emulation and a two-index default catalogue — otherwise the
  existing `hybrid`-vs-`vector_only` tests change meaning silently.

**Dependencies**
- 190 — `TEXT_SEARCH_INDEX_NAME`, `_TEXT_INDEX_FILTER_PATHS`, `_TEXT_INDEX_TEXT_PATHS`,
  `_search_index_is_queryable`, and the index itself on every environment.

**User stories**
- 8 stories: full-sentence on-topic, off-topic with a shared word, one-word, all-stop-word, env override,
  missing index on prod, graphrag seeds, an unsupported filter key.

Ready for implementation.

### [SWE] 2026-10-08 23:30 — Implementation

**Files modified**
- `apps/memory/src/tree/memory/rag/search.py` — `STOP_WORDS` (Lucene's 33 verbatim ∪ NLTK-style question /
  pronoun / auxiliary words, `\w+`-split contractions; good/bad docstring), `query_terms`, `min_should_match`;
  `_text_search` → `$search` on `text_search_index` (filter / nested-compound `mustNot` from `_PARENT_ROW` /
  one four-path `text` clause per term / `minimumShouldMatch`, `searchScore`, `$limit`), `_check_text_node_filter`
  (raises before the `try`), `_log_text_leg`; M == 0 → line + `[]`, no pipeline, no probe; empty leg → probe
  with `fallback_mode="vector_only"`; `_gate_text_candidates` deleted; module / `hybrid_search` /
  `_gate_vector_candidates` docstrings.
- `apps/memory/src/tree/memory/rag/indexing.py` — `_LEGACY_TEXT_INDEX_NAME`, `_drop_legacy_text_index` (step 3 of
  `ensure_indexes`, after the text-index ensure); docstrings; **190 follow-up:** `_text_search_index_drift` reads a
  MISSING `mappings.dynamic` as `False` (Atlas's default) — only an explicit non-False value is drift.
- `apps/memory/src/tree/entities/memory.py` — `TEXT_INDEX_NAME` / `TEXT_INDEX_FIELDS` / the `IndexModel` removed,
  docstring.
- `apps/memory/src/tree/config/app_config.py`, `default.yaml`, `tests/unit/config/fixtures/frozen_config.yaml` —
  `text_min_match_ratio: 0.5` (`[0, 1]`, no inf/nan) replaces `min_text_score`; short YAML comment; the
  `min_vector_score` comments re-pointed.
- `apps/memory/src/tree/memory/rag/types.py`, `apps/memory/README.md` (:76 and :464), `.agents/skills/tree-memory/SKILL.md:54`.
- Tests: `tests/unit/memory/conftest.py` (`$search` emulation, two-index default catalogue, `$text` branch removed),
  `tests/unit/memory/rag/test_search.py` (`TestQueryTerms`, `TestMinShouldMatch`, `TestMinimumMatchRatio`; filter /
  mode tests on `$search`), `test_retrieval.py`, `test_indexing.py` (`TestDropLegacyTextIndex`, `dynamic-absent`),
  `tests/unit/config/test_app_config.py`, `tests/unit/entities/test_memory.py`, `tests/unit/test_db.py`.

**Tests**
- Unit: 5273 passing, 0 failing — `make memory-tests` (env-status local).
- Mutation checks on `search.py` (each reverted): dropping the `mustNot` → 15 fail; dropping the M == 0 line → 1;
  empty leg always `[]` (no probe verdict) → 4; skipping the `node_filter` check → 2; bare `ceil` without the
  rounding → 2.
- Integration: N/A (no suite; live runs below).

**Acceptance criteria**
- [x] `query_terms` / `min_should_match` — `test_search.py::TestQueryTerms::*` (incl. `test_stop_words_are_a_superset_of_lucene_english`), `::TestMinShouldMatch::*`
- [x] Exact pipeline — `TestMinimumMatchRatio::test_pipeline_is_one_search_compound_k_of_m_terms` (whole pipeline asserted: no `$sort` / `$match` / `$text`), `::test_stop_words_and_duplicates_never_become_clauses`
- [x] All-stop-word / ValueError — `::test_all_stop_word_query_sends_no_search_and_stays_hybrid`, `::test_unsupported_node_filter_raises_before_any_aggregate[key-outside-the-filter-paths|non-scalar-value]`
- [x] Absent / BUILDING → `vector_only`; raising probe / readiness-less → `hybrid`; no probe with candidates — `::test_empty_leg_on_an_unavailable_index_reads_vector_only[absent|building]`, `::test_empty_leg_with_a_raising_probe_stays_hybrid`, `::test_empty_leg_on_a_readiness_less_entry_stays_hybrid`, `::test_probe_never_runs_on_a_leg_with_candidates`, `TestSearchMode::test_index_probe_only_runs_on_empty_leg`
- [x] Log line / references gone — `::test_logs_the_line_on_a_leg_with_candidates`, `::test_no_line_on_a_raised_leg`, the M == 0 test; grep below
- [x] Config — `test_app_config.py::test_text_min_match_ratio_default_override_and_env`, `::test_text_min_match_ratio_rejects_values_outside_zero_one[-0.1|1.5|inf|nan]`, `::test_text_min_match_ratio_loaded_from_frozen_config`
- [x] Index set + drop — `test_memory.py::TestMemoryIndexes::*`, `test_db.py` (live Mongo), `test_indexing.py::TestDropLegacyTextIndex::*` (drop after `("wait", "text_search_index")`; absent → no-op; failure propagates)
- [x] Fake — `conftest.py::_search_compound_matches`; behavioural tests `test_half_ratio_keeps_rows_sharing_k_of_m_terms`, `test_graph_seed_search_shares_the_rule`, `TestGraphSeedFilter::test_a_matching_parent_row_is_not_returned`
- [x] Live local — evidence below
- [x] QA green — evidence below

**Evidence**
```
$ make memory-format-check && make memory-lint-check && make pre-commit
341 files already formatted / All checks passed! / prettier, ruff check, ruff format, biome Passed
$ make memory-tests
======================= 5273 passed in 66.05s (0:01:06) ========================
$ grep -rn "min_text_score\|MIN_TEXT_SCORE" apps/ .agents docs/glossary.md   (excl. .venv)
docs/glossary.md:39: ... Replaces `query.min_text_score` (ADR-015 §2–§4) ... why `min_text_score` never separated ...   <- PA-owned history, see Notes

# Live, local (make env-status: local), Docker worker stopped, served with
# PREFECT_LOGGING_ROOT_LEVEL=INFO PREFECT_LOGGING_EXTRA_LOGGERS=tree make memory-serve-workflows
# before: db.memory.getIndexes() = [_id_, text_index, user_kind_type_subtype, user_type_name, active_user, user_kind_source_node, user_kind_target_node]
$ make memory-run-indexing-pipeline        # run 1
INFO tree.memory.rag.indexing - Vector search index 'vector_index' already up-to-date (dimensions=1024, ...)
INFO tree.memory.rag.indexing - Search index 'text_search_index' already up-to-date (analyzer=lucene.english, ...)
INFO tree.memory.rag.indexing - Dropped legacy $text index 'text_index' (replaced by 'text_search_index', ADR-015)
# after: getIndexes() = [_id_, user_kind_type_subtype, user_type_name, active_user, user_kind_source_node, user_kind_target_node]
$ make memory-run-indexing-pipeline        # run 2: both "already up-to-date", NO drop line
$ make memory-search QUERY="ReAct agent tool calling"
vector leg: 40 candidate(s), 40 kept at min_vector_score=0.70 (top=0.805)
text leg: 40 candidate(s), 4 query term(s), min_should_match=2
Parent-document retrieval: 40 child hit(s) -> 13 parent(s), returning 10     # no "Search ran ..." caveat => hybrid
$ make memory-search QUERY="what is it"
text leg: 0 candidate(s), 0 query term(s), min_should_match=0
No child hits for query (outcome=nothing_found, search_mode=hybrid): what is it
$ mongosh ... db.memory.dropSearchIndex("text_search_index")   # $listSearchIndexes -> ["vector_index"]
$ make memory-search QUERY="ReAct agent tool calling"
text leg: 0 candidate(s), 4 query term(s), min_should_match=2
Search leg unavailable: search index 'text_search_index' absent; the query runs vector_only
Search ran vector_only — the other leg was unavailable; results may miss matches.
$ make memory-run-indexing-pipeline        # restores it: Creating search index 'text_search_index' ... treating it as ready
$ make memory-search QUERY="ReAct agent tool calling"   # text leg: 40 candidate(s), 4 query term(s), min_should_match=2, hybrid again
# Stories
$ make memory-search QUERY="MongoDB Atlas Vector Search scalar binary quantization int8"
text leg: 0 candidate(s), 8 query term(s), min_should_match=4   -> nothing_found, search_mode=hybrid
$ make memory-search QUERY="Gemini"
text leg: 37 candidate(s), 1 query term(s), min_should_match=1
$ TREE_QUERY__TEXT_MIN_MATCH_RATIO=0.0 make memory-search QUERY="structured outputs pydantic"
text leg: 40 candidate(s), 3 query term(s), min_should_match=1
$ TREE_QUERY__TEXT_MIN_MATCH_RATIO=1.5 make memory-search QUERY="structured outputs pydantic"
ValidationError: 1 validation error for AppConfig / query.text_min_match_ratio ... less_than_equal
# end state: $listSearchIndexes = [vector_index, text_search_index], no text_index; tree-prefect-worker started again (Up)
```

**Notes**
- **190 follow-up (from 190's Tester note 5):** a catalogue entry without `mappings.dynamic` is no longer drift
  (`test_indexing.py::TestEnsureTextSearchIndex::test_matching_index_is_left_alone[dynamic-absent]`, red before the fix);
  `dynamic-true` still drifts.
- **Log line on an absent / not-queryable index:** the leg RAN (the aggregate answered `[]`), so the line is written
  before the probe, then the probe WARNs and the leg answers `None` → `vector_only` (live output above). Pinned by
  `test_empty_leg_on_an_unavailable_index_reads_vector_only`. A RAISED leg writes no line.
- **Deliberate addition to the K formula:** `min(M, max(1, ceil(round(ratio × M, 9))))`. The rounding guards binary
  float overshoot (`0.28 × 25 = 7.000000000000001` → would ask for 8); no 0.1 step overshoots for M ≤ 100, so task
  192's sweep is unaffected. The `min(M, …)` keeps Atlas error code 8 unreachable from this function.
- **Overlap with 192:** the AC grep forced `README.md:464` and `.agents/skills/tree-memory/SKILL.md:54` here (192
  lists both); SKILL.md uses 192's wording verbatim. The glossary's **Minimum match ratio** row still says
  "Replaces `query.min_text_score`" — PA-owned history text; SWE is read-only there, so that grep hit stays.
  Left for 192 §4: `load.py:31` / `clusters.py:12` still say `$text`. 192's grep for `text_index\b|\$text` in
  `apps/memory/src` will also hit `indexing.py`'s legacy-drop code and docstrings, which must name the index they drop.
- `STOP_WORDS` beyond the task's list: `could would might must shall` (auxiliaries named in the task) — none of the
  story / live terms is in it (`test_domain_words_are_never_stop_words`).
- The fake does not stem (`agent` ≠ `agents`) — fixtures avoid relying on stemming.
- `TestMinVectorScore::test_fully_gated_leg_stays_hybrid_and_never_probes` now asserts no `vector_index` probe: its
  off-topic text leg is genuinely empty and probes `text_search_index`.
- A concurrent second `ensure_indexes` could hit `IndexNotFound` on the drop; per the task, drop failures propagate
  (not handled — one-time, one-window race).
- The stale `tree-prefect-worker` image still declares `text_index` in its Beanie set; if it ever runs a flow it
  would recreate it and the next new-code indexing run would drop it again. Restarting it did not recreate it.
- One `make memory-tests` run wedged at 89% CPU past 10 min (the intermittent wedge `test_indexing.py`'s sleep-stub
  docstring describes); two re-runs finished in ~66–70 s green. Not caused by this change as far as I can tell.
- Not verified live: graphrag `query_memory` seeds (local mode is `rag`; covered by `test_graph_seed_search_shares_the_rule`
  and `TestGraphSeedFilter`), prod / Atlas M0.

### [PA] 2026-10-08 23:30 — Grooming fix (glossary vs AC grep)

The SWE log flagged that the AC grep (`min_text_score\|MIN_TEXT_SCORE` over `apps/ .agents docs/glossary.md`)
hit only `docs/glossary.md`'s **Minimum match ratio** row — PA-owned text written at grooming that named the
retired key twice. Resolved on the glossary side so the AC holds as written: "Replaces `query.min_text_score`"
→ "Replaces the retired `textScore` bar (ADR-013 §7, ADR-015 §2–§4)"; "why `min_text_score` never
separated…" → "why the retired score bar never separated on-topic from off-topic (task 183's `textScore`
eval)". The M/K good/bad examples, the log-line format and the PROVISIONAL note are unchanged. The AC grep is
now empty (exit 1). No code, test or AC text touched.

### [Tester] 2026-10-08 23:55 — QA

**Test summary**
- Format / lint / pre-commit: PASS (341 files formatted; ruff clean; prettier/ruff/biome Passed)
- Unit tests: 5273 passed / 0 failed (`make memory-tests`, env-status local, 61 s; a second scoped run of tests/unit/{memory,config,entities}: 2566 passed, 18 s) — includes live-Mongo `test_db.py`
- Integration tests: N/A (no suite by design)
- Warnings: 0 test warnings (only the third-party opik/pydantic-v1 import notice outside pytest)
- `grep -rn "min_text_score\|MIN_TEXT_SCORE\|_gate_text_candidates" apps/ .agents docs/glossary.md` (excl. .venv) → empty; no `$text` / `$match` / `$nor` / `textScore` in `search.py`; no `TEXT_INDEX_NAME` / `TEXT_INDEX_FIELDS` anywhere. Remaining `$text` mentions: `load.py:31`, `clusters.py:12` (192's scope) and the legacy-drop code in `indexing.py`/`memory.py` (must name the index).

**E2E adversarial pass** (live local mongot, 413 rows / 372 children; Docker worker stopped, served from this tree; restored afterwards)
- Happy path: `make memory-run-indexing-pipeline` after re-creating `text_index` by hand → one `Dropped legacy $text index 'text_index'` line, getIndexes() has no `text_index`; run 2 → no drop line (PASS). `make memory-search QUERY="ReAct agent tool calling"` → `text leg: 40 candidate(s), 4 query term(s), min_should_match=2`, 40 child hits → 13 parents, no caveat (PASS). `"what is it"` → `0 candidate(s), 0 query term(s), min_should_match=0`, `search_mode=hybrid` (PASS). `"MongoDB Atlas Vector Search scalar binary quantization int8"` → `0 candidate(s), 8 query term(s), min_should_match=4`, nothing_found hybrid (PASS). `"Gemini"` → `37 candidate(s), 1 query term(s), min_should_match=1` (PASS). Ratio 0.0 → K=1 (40 cand.), 1.0 → K=3 (13 cand.), 1.5 / nan → one pydantic error naming `query.text_min_match_ratio` (PASS). `dropSearchIndex("text_search_index")` → `text leg: 0 candidate(s), 4 query term(s), min_should_match=2`, WARN `search index 'text_search_index' absent`, `Search ran vector_only` (PASS); re-index → hybrid again (PASS).
- Break path 1 (boundary/malformed queries, direct `_text_search` on live): punctuation `ReAct, agent!! (tool) calling?` → 4 terms, 10 cand.; hyphen `tool-calling agent` → 3 terms; underscore `tool_calling agent` → 2 terms; digits `int8 3.14 2024` → `int8 3 14 2024`; unicode/CJK/emoji → no crash (emoji dropped, `agent` M=1); `the`, `""`, whitespace, `what is the how does it` → M=0, `[]`, no pipeline; duplicate-heavy `agent agent AGENT Agent agent tool` → 2 terms; Lucene/Mongo-operator strings (`AND (tool OR "loop") -foo +bar *`, `{"$gt": ""} $where`) → treated as plain terms, no error (PASS). Note `_ _ _` → term `_` (M=1) which the analyzer can never match (trivial).
- Break path 2 (long queries): 50 / 100 / 200 / 250 distinct terms → leg ran (`[]`, hybrid). **256+ distinct terms → mongot `OperationFailure code 8: query has expanded into too many sub-queries internally: maxClauseCount is set to 1024` (4 paths × 256 clauses) → leg returns `None` → `vector_only` + WARN traceback.** Degrades gracefully, but it is a silent lexical-leg cliff for a pasted paragraph (FAIL, minor — see item 2).
- Break path 3 (node_filter): `{"type": None}` / `{"type": ["chunk","node"]}` / `{"type": True}` / `{"merged_into": None}` → `ValueError` naming key/value (PASS); `{}` (graphrag) → 10 cand.; ObjectId value accepted; explicit `{"type":"chunk","subtype":"parent"}` → 0 (mustNot wins) (PASS). **`{"user_id": <other ObjectId>, "kind": "edge"}` → recorded `compound.filter` is `[user_id=<OTHER>, kind=edge]`: the caller's pinned `user_id` and `kind` are REPLACED, not ANDed** (FAIL — item 1).
- Break path 4 (state/concurrency): 2 concurrent `_drop_legacy_text_index` both seeing the index present → one drops, the other raises `OperationFailure: index not found with name [text_index] (code 27)`. `ensure_indexes` runs at MCP boot and in the indexing pipeline, so a deploy-time overlap crashes one of them once (item 3, recommended).
- Fake vs live (413 live rows loaded into `FakeMemoryCollection`, same `_text_search`): fake ⊆ live except where Lucene tokenisation differs. `ReAct agent tool calling` live 183 / fake 102 (live-only 81 = stemming: calling→call, agents→agent); `structured outputs pydantic` 58 / 42; `agent` 236 / 190; `Gemini` live 37 / fake 39 — the 2 fake-only rows contain `google_genai:gemini-1.5-flash-lite`, which Lucene's UAX#29 tokenizer keeps as ONE token (`_` and `:` between letters do not split) while `\w+` splits it. The 8-term off-topic query → 0 / 0. The fake therefore cannot mask a K-of-M or mustNot bug (counts and exclusion agree on the shared tokens) but it does NOT model stemming or UAX#29 joins; any test relying on a stemmed match would be a false negative. No production bug; add a sentence to the fake's docstring (it says "NO stemming" but not the tokenizer difference).

**Acceptance criteria**
- [x] PASS — `query_terms` / `min_should_match` — `TestQueryTerms`, `TestMinShouldMatch` (incl. Lucene-33 superset test); live: 4/2, 8/4, 1/1, 0/0 reproduced.
- [ ] FAIL — exact `$search` pipeline: index name, nested-compound `mustNot`, one four-path `text` clause per term, `minimumShouldMatch`, `searchScore`, `$limit`, no `$sort`/`$text`/`$match` all verified live and in `TestMinimumMatchRatio`. BUT `compound.filter` is built from a dict merge (`search.py` `filters = {"user_id": user_id, "kind": "node", **node_filter}`), so it is not "`user_id` + `kind` + one `equals` per `node_filter` key": a `node_filter` containing `user_id` or `kind` (both are accepted by `_check_text_node_filter`) overwrites the tenant / kind pin (see break path 3).
      Expected: `[user_id equals, kind equals, *one equals per node_filter key]` (ANDed; a colliding key narrows to empty, never widens to another tenant).
      Actual: `[{user_id: OTHER}, {kind: edge}]`.
      Fix: `filter = [_equals("user_id", user_id), _equals("kind", "node"), *(_equals(k, v) for k, v in node_filter.items())]` + a regression test (`node_filter={"user_id": other}` → `filter` still contains the caller's `user_id` clause; fake returns no rows of `other`).
- [x] PASS — all-stop-word → `[]`, no pipeline, `hybrid` (live + `test_all_stop_word_query_sends_no_search_and_stays_hybrid`); unsupported key → `ValueError` before aggregate (live + 2 params).
- [x] PASS — absent / BUILDING → `vector_only`; raising probe / readiness-less → `hybrid`; no probe with candidates (live absent case reproduced; unit tests listed by SWE all in the 5273).
- [x] PASS — log line on every ran leg incl. `0 … 0 … 0`, none on a raised leg (`test_no_line_on_a_raised_leg`); references gone (grep empty).
- [x] PASS — `text_min_match_ratio` in default.yaml / frozen_config.yaml / `QueryConfig` (0.5, [0,1], no inf/nan) / env override; README:76 and `types.py` updated.
- [x] PASS — `memory_indexes` has no `text_index`; `ensure_indexes` drops it once after the text-index ensure, no-op after (live); `test_db.py` green live.
- [x] PASS — fake emulates `$search` compound (filter, mustNot, should ≥ K), two-index default catalogue.
- [x] PASS — live local ACs all reproduced (above), including the dropped-index case.
- [x] PASS — format / lint / pre-commit / `make memory-tests` green (env-status local).

**Deviations judged**
1. K = `min(M, max(1, ceil(round(ratio×M, 9))))`: ACCEPT. With the validated ratio ∈ [0,1] the `min(M, …)` clamp is unreachable (ratio×M ≤ M); the 9-digit rounding only changes results for float overshoot (0.28×25). Behaviourally identical to the spec formula on every ratio the sweep in 192 uses.
2. Log line before the probe WARN on an absent index: ACCEPT (the aggregate ran; AC says "every leg that ran"). Minor wart: an M == 0 query on an ABSENT index reports `hybrid` (never probes, by spec) — live reproduced; the vector leg alone decided, so harmless.
3. README:464 / SKILL.md read-chain edits: ACCEPT (needed for the AC grep; 192 can re-touch). Wording consistent.
4. Extra STOP_WORDS (could/would/might/must/shall): ACCEPT; none is a domain word.
5. `test_fully_gated_leg_stays_hybrid_and_never_probes` asserting no `vector_index` probe: ACCEPT.
6. `_text_search_index_drift` missing `dynamic` ≡ False: ACCEPT (live re-index reports up-to-date; `[dynamic-absent]` test).
- Test-suite hang: the documented wedge is pymongo's hot-spinning executor under the process-wide sleep stub; the fixture in `test_indexing.py` patches the module-local `asyncio` binding and the new tests use mocks only (no live client). Two full-suite runs here (and one scoped) finished in 61 s / 18 s; no evidence 191 worsened it.

**Other issues found**
- (item 2, recommended) ≥ 256 distinct terms → mongot `maxClauseCount` 1024 error → text leg dead → `vector_only`. Fix options: cap the clause list (e.g. first 200 terms by position; compute K over the capped M) with a `text leg:` line reporting the capped M, or state the limit in ADR-015 / `query_terms` docstring and add a test that asserts the chosen behaviour.
- (item 3, recommended) `_drop_legacy_text_index`: treat `OperationFailure` code 27 (IndexNotFound) as "already gone" (DEBUG) so the race between MCP boot `ensure_indexes` and the indexing pipeline cannot fail either; keep every other drop failure propagating (existing test stays valid); add a code-27 test.
- Fake fidelity (see above): docstring only.
- `default.yaml` / `frozen_config.yaml` min_vector_score comment reflow left a >80-char line ("(1 + cos) / 2, so this is an absolute similarity (the RRF score is not). PROVISIONAL and") — cosmetic.
- Not verified live: graphrag mode (covered by unit tests), prod / M0.

**VERDICT: FAIL** — 1 required fix (item 1: `node_filter` must AND with, never overwrite, the `user_id` / `kind` pins; AC 2 unchecked), 2 recommended (items 2, 3).


### [SWE] 2026-10-08 23:45 — Fixes (Tester QA 23:55)

**Files modified**
- `apps/memory/src/tree/memory/rag/search.py` — (1) `_text_search` builds `compound.filter` as a LIST:
  `[equals user_id, equals kind "node", *(equals k v for node_filter)]` — pins first, caller keys ANDed after,
  no dict merge (`_check_text_node_filter` still accepts `user_id` / `kind`; a colliding key now narrows to
  nothing); `hybrid_search` docstring no longer says the text filter is "merged". (2) `_MAX_QUERY_TERMS = 64`
  (one-line why: 1024 Lucene clauses / 4 paths = 256-term ceiling, 64 = 4x margin); `query_terms` returns
  the FIRST 64 DISTINCT terms, so K and the `text leg:` line use the capped M; good/bad example in its docstring.
- `apps/memory/src/tree/memory/rag/indexing.py` — (3) `_INDEX_NOT_FOUND = 27`; `_drop_legacy_text_index`
  catches `OperationFailure` code 27 on the drop → DEBUG `Legacy $text index 'text_index' already dropped by
  a concurrent ensure_indexes`; any other failure re-raises.
- `apps/memory/tests/unit/memory/conftest.py` — (4) `FakeMemoryCollection` docstring: `\w+` split, no UAX#29
  (`google_genai:gemini-1.5` stays one token live), no stemming → under-approximates live candidates.
- `apps/memory/src/tree/config/default.yaml`, `tests/unit/config/fixtures/frozen_config.yaml` — (5) the
  93-char `min_vector_score` comment line reflowed to ≤ 80 (pre-existing long lines elsewhere untouched).
- Tests: `tests/unit/memory/rag/test_search.py`, `tests/unit/memory/rag/test_indexing.py`.

**Tests** (10 new; each red with its fix reverted, green with it — 8 failures on the combined mutation)
- (1) `test_search.py::TestTenantPins::test_colliding_key_keeps_the_pins_and_returns_no_foreign_row[user-id|kind|user-id-and-kind]`
  — calls `_text_search` directly (the vector leg still merges); asserts the exact filter list
  (caller `user_id`, `kind: node`, then the colliding clauses) and that the other tenant's / the edge row is not returned.
- (2) `TestQueryTerms::test_keeps_the_first_capped_distinct_terms[under-cap|at-cap|one-over-cap|pasted-paragraph]`
  (63/64/65/300 distinct terms), `::test_cap_counts_distinct_terms_not_tokens` (duplicates + stop words do not use
  up the cap), `TestMinimumMatchRatio::test_long_query_sends_capped_clauses_and_k_over_the_cap` (300 terms → 64
  `should` clauses, `minimumShouldMatch == 32`, `hybrid`, line `text leg: 0 candidate(s), 64 query term(s), min_should_match=32`).
- (3) `test_indexing.py::TestDropLegacyTextIndex::test_index_not_found_on_the_drop_is_already_gone`;
  `::test_drop_failure_propagates` unchanged and green.
- Unit: 5283 passing, 0 failing (`make memory-tests`, env-status local). Integration: N/A.

**Acceptance criteria**
- [x] Exact `$search` pipeline (AC 2) — filter is now `user_id` + `kind` + one `equals` per `node_filter` key, ANDed:
  `TestTenantPins::*`, `TestMinimumMatchRatio::test_pipeline_is_one_search_compound_k_of_m_terms`,
  `TestChildOnlyFilter::test_text_filter_carries_the_same_filter_keys`, `TestGraphSeedFilter::test_text_filter_is_user_and_kind_and_excludes_parent_rows`.

**Evidence**
```
$ make env-status            -> Env target: local (.env)
$ make memory-format-fix && make memory-lint-fix && make memory-format-check && make memory-lint-check && make pre-commit
341 files left unchanged / All checks passed! / 341 files already formatted / All checks passed!
prettier Passed / ruff check Passed / ruff format Passed / biome check (harness) Passed
$ make memory-tests
============================ 5283 passed in 55.59s =============================

# Live, local mongot (db tree, 413 rows), direct _text_search / _drop_legacy_text_index (scratchpad scripts);
# tree-prefect-worker left Up, untouched.
# (1) node_filter={"user_id": OTHER, "kind": "edge"}, query "ReAct agent tool calling"
text leg: 0 candidate(s), 4 query term(s), min_should_match=2
filter=[{equals user_id=6ac0f976f5896c3a79957359}, {equals kind=node}, {equals user_id=0123456789abcdef01234567}, {equals kind=edge}]
hits=0        # baseline node_filter={} -> text leg: 40 candidate(s), 4 query term(s), min_should_match=2
# (2) 300 distinct terms ("agent tool calling react" + zz0..zz295)
text leg: 0 candidate(s), 64 query term(s), min_should_match=32
should clauses=64 minimumShouldMatch=32 hits=0      # leg ran; no OperationFailure code 8, no vector_only
# (3) raw drop of an absent index -> OperationFailure code=27 IndexNotFound (confirms the code)
#     re-created text_index, then two _drop_legacy_text_index racing (barrier: both checks see it present)
DEBUG Legacy $text index 'text_index' already dropped by a concurrent ensure_indexes
INFO  Dropped legacy $text index 'text_index' (replaced by 'text_search_index', ADR-015)
gather results=[None, None]
after: ['_id_', 'active_user', 'user_kind_source_node', 'user_kind_target_node', 'user_kind_type_subtype', 'user_type_name']
```

**Notes**
- **Follow-up candidate (not changed here, per Tester):** `_vector_search` still builds
  `"filter": {"user_id": user_id, "kind": "node", **node_filter}`, so a `node_filter` carrying `user_id` / `kind`
  REPLACES the vector leg's tenant / kind pin — the same bug class as item 1. No caller passes those keys today
  (rag: `{type, subtype}`; graphrag: `{}`), but it should get the same AND treatment (`$and` of the pins and the
  caller's keys) in its own task.
- The cap truncates silently apart from the capped M in the `text leg:` line; with 64 terms at ratio 0.5 a row
  must hold 32 of them, so a pasted paragraph's text leg is effectively an exact-passage matcher — intended.
- Item 3 live: an unforced `asyncio.gather` of two drops did not race (the second check ran after the first drop);
  the barrier wrapper forces both checks to see the index first, which reproduces the Tester's code-27 path.
- `docs/glossary.md` and `tasks/192-*` left as the PA amended them.


### [Tester] 2026-10-08 00:20 — Re-QA (after SWE fixes 23:45)

**Test summary**
- Format / lint / pre-commit: PASS (`make memory-format-fix && memory-lint-fix && memory-format-check && memory-lint-check && pre-commit`; 341 files unchanged, ruff clean, prettier/ruff/biome Passed; diff count unchanged at 19 files)
- Unit tests: 5283 passed / 0 failed (`make memory-tests`, env-status local, 54.8 s, includes live-Mongo `test_db.py`)
- Integration tests: N/A (no suite by design)
- Warnings: 0
- `grep -rIn "min_text_score|MIN_TEXT_SCORE|_gate_text_candidates" apps .agents docs/glossary.md` (excl. .venv) → empty; no `$text` / `$match` / `$nor` / `textScore` in `search.py` code.

**E2E adversarial pass** (live local mongot; Docker worker stopped, workflows served from this tree, worker restarted at the end → `tree-prefect-worker Up`)
- Happy path, all ACs re-run: re-created `text_index` by hand → `make memory-run-indexing-pipeline` removed it (getIndexes has no `text_index`), run 2 left the set unchanged. (The flow's INFO line does not reach the served stdout / Prefect logs API in this setup, so the drop log text was verified by calling `_drop_legacy_text_index` live: `INFO Dropped legacy $text index 'text_index' (replaced by 'text_search_index', ADR-015)` then `DEBUG No legacy $text index 'text_index' to drop`.) `make memory-search QUERY=` "ReAct agent tool calling" → `40 candidate(s), 4 query term(s), min_should_match=2`; "what is it" → `0, 0, 0` + `search_mode=hybrid`; the 8-term MongoDB query → `0, 8, 4` hybrid; "Gemini" → `37, 1, 1`; ratio 0.0 → `3 terms, min_should_match=1`; ratio 1.5 → pydantic error on `query.text_min_match_ratio`. `dropSearchIndex("text_search_index")` → `Search leg unavailable: search index 'text_search_index' absent; the query runs vector_only` + `Search ran vector_only`; re-index → `queryable`, hybrid, `40 candidate(s)` again. All PASS.
- Break path 1 (tenant pins, direct `_text_search`, live): `node_filter={"user_id": OTHER}` → 0 hits; `{"kind": "edge"}` → 0; `{"user_id": OTHER, "kind": "edge"}` (SWE) → 0; `{"kind": "node"}` repeated → filter `[user_id, kind=node, kind=node]`, 40 hits (idempotent, harmless); `{"kind": "node", "user_id": <caller>}` → 40; rag filter `{type: chunk, subtype: child}` → 40. The pins can no longer be replaced. PASS.
- Break path 2 (cap x ratio x input shape, live, recorded pipeline): 400 distinct terms → 64 `should` clauses and K = 1 / 32 / 64 at ratio 0.0 / 0.5 / 1.0 (never above the clause count; mongot accepted every pipeline, no code 8, hybrid); duplicate-heavy (3 terms × 400 repeats) → 3 clauses, K 1/2/3, 40 hits; 500 stop words + 2 terms → 2 clauses, K 1/1/2; stop words interleaved with 100 distinct terms (`the zqN what`) → 64 clauses (stop words do not consume the cap), K 1/32/64. Via `make memory-search` with 403 distinct terms → `0 candidate(s), 64 query term(s), min_should_match=32`, no traceback. `query_terms` unit-level: `"the "*50` → 0 terms. PASS.
- Break path 3 (`_drop_legacy_text_index` error codes, mocks): `OperationFailure(code=27)` → swallowed (DEBUG); codes 26, 13, 11601, `None` and a non-Mongo `RuntimeError` → propagate. Live sequential double call: one INFO drop then one DEBUG no-op. PASS.
- Fake vs live gap documented in the `FakeMemoryCollection` docstring (read in the diff). PASS.

**Acceptance criteria** (all re-verified after the fix; the boxes in the spec were already ticked and stay ticked)
- [x] PASS — `query_terms` / `min_should_match` (`TestQueryTerms`, `TestMinShouldMatch`, new cap tests; live 4/2, 8/4, 1/1, 0/0; cap = first 64 DISTINCT content terms, K over the capped M).
- [x] PASS — exact `$search` pipeline, now incl. `filter == [user_id, kind=node, *one equals per node_filter key]` (`search.py` `_text_search`, `TestTenantPins` x3, `TestMinimumMatchRatio`, live recorded pipelines); no `$sort` / `$text` / `$match`.
- [x] PASS — all-stop-word → `[]`, no pipeline, hybrid; invalid `node_filter` → `ValueError` before the aggregate.
- [x] PASS — absent / not-queryable → `vector_only`; probe only on an empty leg (live absent case reproduced).
- [x] PASS — log line on every ran leg, none on a raised leg; references gone (grep empty).
- [x] PASS — `text_min_match_ratio` in YAMLs / `QueryConfig` / env; README and `types.py`; new YAML comment lines ≤ 80 chars.
- [x] PASS — no `text_index` in `memory_indexes`; `ensure_indexes` drops it once after the text-index ensure, no-op afterwards; code 27 on the drop = already gone; other failures propagate; `test_db.py` green live.
- [x] PASS — `FakeMemoryCollection` emulates the `$search` compound, two-index catalogue.
- [x] PASS — live ACs reproduced (above).
- [x] PASS — format / lint / pre-commit / `make memory-tests` green (env-status local).

**Previous FAIL items**
1. Tenant / kind pin overwritten by `node_filter` → FIXED (list of ANDed `equals`, live 0 foreign rows).
2. 256+ terms → code 8 → `vector_only` → FIXED (cap 64; live 400 terms keep the leg at hybrid).
3. Concurrent legacy drop race → FIXED (code 27 tolerated, other codes propagate).
4. Fake docstring, 5. YAML wrap → done.

**Other issues found (non-blocking, nits)**
- `indexing.py` `ensure_indexes` docstring: the edited paragraph left one ~130-char line (`... (:func:`tree.entities.memory.memory_indexes`) on every ``init_mongodb``. ``user_id`` is optional ...`); same for one long line in the `_text_search` docstring (`deduped, capped at ..., K = :func:`min_should_match` at ...`). Ruff does not flag them; rewrap when next touched.
- Cap truncates silently beyond 64 terms (the capped M is visible in the `text leg:` line); the cap is a module constant, not YAML — acceptable, noted in the SWE entry.
- Known follow-up (already recorded by SWE, out of scope): `_vector_search` still dict-merges `node_filter` over the `user_id` / `kind` pins; no caller passes those keys today.

**VERDICT: PASS** — QA PASSED for #191. Ready to commit.
