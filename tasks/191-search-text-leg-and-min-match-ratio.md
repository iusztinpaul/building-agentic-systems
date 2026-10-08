---
id: 191-search-text-leg-and-min-match-ratio
status: pending
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

- [ ] `query_terms` lower-cases, splits on `\w+`, drops `STOP_WORDS` (a superset of Lucene's 33, listed
      verbatim in the frozenset), dedupes in order; `min_should_match` follows `max(1, ceil(ratio × M))`
      with `0` for `M == 0` and `1` at ratio `0.0`.
- [ ] `_text_search` issues exactly the `$search` pipeline above (index name, `compound.filter` with
      `user_id` + `kind` + one `equals` per `node_filter` key, the nested-`compound` `mustNot`, one
      four-path `text` clause per term, `minimumShouldMatch = K`, `searchScore`, `$limit`); no `$sort`,
      no `$text`, no `$match` anywhere in the leg.
- [ ] An all-stop-word query answers `[]` without a pipeline and keeps `search_mode: hybrid`; a
      `node_filter` key outside `_TEXT_INDEX_FILTER_PATHS` raises `ValueError` before any aggregate.
- [ ] An empty leg whose `text_search_index` is absent or `queryable: false` answers `None` →
      `vector_only`; a raising probe or a readiness-less entry keeps `hybrid`; the probe never runs when
      the leg has candidates.
- [ ] `text leg: N candidate(s), M query term(s), min_should_match=K` is logged at INFO on every leg that
      ran (including `0 … 0 … 0`) and never on a raised leg; `_gate_text_candidates` and every
      `min_text_score` reference are gone from `apps/`, `.agents`, `docs/glossary.md`.
- [ ] `query.text_min_match_ratio` exists in `default.yaml`, `frozen_config.yaml`, `QueryConfig`
      (`0.5`, `[0, 1]`, no inf/nan) and via `TREE_QUERY__TEXT_MIN_MATCH_RATIO`; README:76 and
      `types.py` describe it.
- [ ] `memory_indexes(mode)` declares no `text_index` in either mode; `ensure_indexes` drops a present
      `text_index` after the text-index ensure and is a no-op when it is absent; `tests/unit/test_db.py`
      passes live.
- [ ] `FakeMemoryCollection` emulates the `$search` compound (filter, mustNot, should-count ≥ K) and
      carries both indexes in its default catalogue.
- [ ] Live, local env, ratio 0.5: `make memory-run-indexing-pipeline` logs the `text_index` drop once
      (`db.memory.getIndexes()` no longer lists it; a second run logs no drop); `make memory-search
      QUERY="ReAct agent tool calling"` logs `text leg: N candidate(s), 4 query term(s),
      min_should_match=2` with N ≥ 1 and `search_mode: hybrid`; `make memory-search QUERY="what is it"`
      logs `0 candidate(s), 0 query term(s), min_should_match=0` and still answers `hybrid` (M = 0 never
      probes); with `text_search_index` dropped by hand, `make memory-search QUERY="ReAct agent tool
      calling"` (M ≥ 1, empty leg → probe) answers `search_mode: vector_only` and a WARN naming the
      index.
- [ ] `make memory-format-check`, `make memory-lint-check`, `make pre-commit`, `make memory-tests` green
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
