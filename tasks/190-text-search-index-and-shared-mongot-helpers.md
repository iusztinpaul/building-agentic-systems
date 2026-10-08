---
id: 190-text-search-index-and-shared-mongot-helpers
status: pending
feature: atlas-search-text-leg
---

# `ensure_indexes` owns BOTH mongot indexes: a static `text_search_index` (`lucene.english` text paths + `objectId`/`token` filter paths; create / definition drift → drop+recreate / wait ready / M0-cap error), through the two index helpers generalised to `(collection, index_name)` — after a live `$search` spike on the local community mongot

Tags: `retrieval`, `indexing`, `mongot`, `spike`
Depends on: —
Blocks: 191
Implements: ADR-015 §5–§7 (the index definition, the shared readiness helpers, the rollout order)

## Problem

The text leg of `hybrid_search` runs `$text` on Beanie's classic `text_index`, whose `textScore` cannot
express "how many of the query's words matched" (ADR-013 §7's eval: 1 term ×5 repetitions ≈ 2 distinct
terms; the gate shipped OFF). ADR-015 moves the leg to an Atlas Search index with `minimumShouldMatch`
— but the leg cannot switch until the index exists on every environment, and the readiness code that
would guard it is vector-only today: `indexing.py::_wait_for_vector_index_ready(collection)` and
`search.py::_vector_index_is_queryable(collection)` hard-code `VECTOR_INDEX_NAME`. This task builds the
index and the shared helpers while the `$text` leg keeps running — main stays shippable with two mongot
indexes and the old lexical path.

## Scope

**0. Spike FIRST (evidence in the Log; nothing committed).** Against `make local-start`'s
`mongodb/mongodb-community-search:0.60.1`, in a scratch database (e.g. `tree_spike_190`, dropped after),
with a throwaway script or `mongosh`:
- create the index of step 2 via `create_search_index(model={"name": "text_search_index", "type":
  "search", "definition": {...}})`; record the RAW `list_search_indexes("text_search_index")` entry
  (field names — expect `{id, name, type, latestDefinition}` as the vector entry has, no
  `status`/`queryable`) and how many seconds until it answered — the drift check of step 3 reads
  `latestDefinition.mappings`, so its real shape (does mongot echo `searchAnalyzer` / token
  `normalizer` defaults? nested `properties` as `document`?) must be in the Log;
- insert four rows: user A child (`type: chunk, subtype: child`, `properties.content: "ReAct agents call
  tools in a loop"`), user A parent (same content, `subtype: parent`), user A entity (`type: concept`,
  NO `subtype` field, `name: "ReAct"`), user B child (same content); `user_id` as ObjectId;
- run EXACTLY the pipeline ADR-015 §6 specifies — `compound.filter` = `equals` user_id (objectId) +
  `equals` kind `"node"`; `mustNot` = `[{compound: {must: [equals type "chunk", equals subtype
  "parent"]}}]`; `should` = two `text` clauses (`react`, `tools`), each with `path: ["name", "aliases",
  "properties.content", "properties.aliases"]`; `$addFields _search_score: {$meta: "searchScore"}`;
  `$limit` — with `minimumShouldMatch: 2` (expect: A child only) and `1` (expect: A child + A entity;
  never the parent, never user B);
- stop-word check: a single should clause `the` → 0 rows (does `lucene.english` drop Lucene's stop
  words at query time? the vendored `STOP_WORDS` of task 191 must be a superset of whatever is dropped);
- absent index: the same pipeline with `index: "nope"` → does `$search` answer `[]` or raise? (ADR-015
  §5's empty-leg probe assumes `[]`, as `$vectorSearch` does locally — the Log must say which);
- `minimumShouldMatch` larger than the number of should clauses → error text (the code never sends it,
  the Log records the failure mode).
If `lucene.english`, `equals` on objectId/token, the nested-`compound` `mustNot`, or `minimumShouldMatch`
is unsupported on community mongot: STOP, write the finding in the Log, report `USER ACTION REQUIRED` —
do not build around it.

**1. Generalise the two readiness helpers (no copies).**
- `indexing.py`: `_wait_for_vector_index_ready(collection)` → `_wait_for_search_index_ready(collection,
  index_name: str)`; the constants become `_SEARCH_INDEX_READY_TIMEOUT_S = 300` / `_SEARCH_INDEX_POLL_S
  = 5` (same values, comment updated); every log line and the FAILED `RuntimeError` name `index_name`;
  the timeout WARN says the index's leg stays unavailable until it is queryable (no "text_only"
  hard-coding). `index_entry_is_queryable`'s "ONE reader" paragraph names the two new helpers.
- `search.py`: `_vector_index_is_queryable(collection)` → `_search_index_is_queryable(collection,
  index_name: str)` with the same three verdicts (probe raises → `True` fail-open; no entry → `False`;
  `index_entry_is_queryable(entry) is False` → `False`; else `True`). The WARN lines name the index AND
  the mode the query falls back to — how the mode reaches the line (a keyword, or the caller logging
  it) is the SWE's choice; `_vector_search` keeps reading `text_only` for `vector_index`.
- `tests/unit/memory/rag/test_indexing.py::TestVectorIndexReadiness` → the new name, parametrised over
  `VECTOR_INDEX_NAME` and `TEXT_SEARCH_INDEX_NAME`; `_make_collection`'s second `list_search_indexes`
  stub must answer PER NAME (today it returns `{"name": VECTOR_INDEX_NAME}` whatever the argument, which
  would make the text wait-loop poll for 300 s of mocked sleeps).

**2. The definition (`indexing.py`).**
- `TEXT_SEARCH_INDEX_NAME = "text_search_index"` next to `VECTOR_INDEX_NAME`.
- `_TEXT_INDEX_TEXT_PATHS: tuple[str, ...] = ("name", "aliases", "properties.content",
  "properties.aliases")` (the four paths `TEXT_INDEX_FIELDS` indexes today) and
  `_TEXT_INDEX_FILTER_PATHS: dict[str, str] = {"user_id": "objectId", "kind": "token", "type": "token",
  "subtype": "token"}` — `user_id` first; `merged_into` deliberately absent (ADR-015 §6). Both are what
  task 191's query validates against, so they are module constants, not literals.
- `_build_text_search_index_definition() -> dict`: `{"mappings": {"dynamic": False, "fields": {...}}}`
  where every text path is `{"type": "string", "analyzer": "lucene.english"}`, every filter path its
  declared type (`{"type": "objectId"}`, `{"type": "token"}` — no `normalizer`, the values are already
  lower-case literals), and the nested paths are declared the Atlas way — `"properties": {"type":
  "document", "fields": {"content": {...}, "aliases": {...}}}` — NOT as dotted keys (verified: static
  mappings nest through `document`; QUERY paths stay dotted). The exact dict is pinned by a test and
  recorded in ADR-015 §6.

**3. `_ensure_text_search_index(collection)`** mirroring `_ensure_vector_index`:
- `list_search_indexes()` → absent → create + wait ready;
- present → `_extract_text_index_field_types(existing) -> dict[str, tuple[str, str | None]]` flattens
  `latestDefinition` (else `definition`) `mappings` — `document` nesting into dotted paths — to
  `path → (type, analyzer)`; **drift** = `mappings.dynamic` is not `False`, OR any text path missing /
  not `string` / analyzer ≠ `lucene.english`, OR any filter path missing / wrong type. Extras the server
  echoes (defaults such as `searchAnalyzer`, `indexOptions`, `normalizer`, extra fields) are NOT drift —
  a subset comparison on the declared paths, exactly as the vector helper compares dimensions + the
  filter-path SET, never the whole dict (whole-dict equality would drop + recreate on every run);
- drift → one WARN naming the differences (`have=…, want=…`), `drop_search_index`, `asyncio.sleep(2)`,
  create, wait ready; up-to-date → one INFO (`already up-to-date (analyzer=lucene.english, text paths=…,
  filter paths=…)`) and return.

**4. `_create_search_index(collection, model)`** — ONE wrapper used by both ensures: any exception from
`collection.create_search_index` re-raises as `RuntimeError(…) from exc` whose message names the index
and says: Atlas M0 allows 3 search indexes per cluster (`search` and `vectorSearch` counted together);
Tree needs `vector_index` + `text_search_index`; a `MAXIMUM_INDEXES_FOR_TENANT_EXCEEDED` cause means a
stray index in Atlas → Search & Vector Search must go before `make memory-run-indexing-pipeline` is
re-run. The original error stays visible through chaining.

**5. `ensure_indexes` order and docstring.** Body: `_ensure_vector_index` → `_ensure_text_search_index`
(task 191 appends the legacy drop as step 3). Docstring rewritten: this function owns BOTH mongot
indexes (`vector_index`, `text_search_index`); the classic set is Beanie's (`memory_indexes`, ADR-012);
reconcile rules per index; idempotent. Module docstring item 2 likewise. (`assert_settings_match_live_
vector_index` is untouched.)

**6. Tests** (`squid-testing-python`; `tests/unit/memory/rag/test_indexing.py`, plus
`test_indexing_mongot_filter_paths.py` for the two constants):
- the exact definition dict (`dynamic: False`, four `string`+`lucene.english` text paths with
  `properties` nested as `document`, four filter paths with their types, no `merged_into`); `user_id`
  is the first filter path;
- absent → `create_search_index` called with `{"name": "text_search_index", "type": "search",
  "definition": <that dict>}` then the wait helper with that name; present and matching → no create,
  no drop, INFO; present with a server-echoed extra (`searchAnalyzer`, a `normalizer`) → still no-op;
  drift ×3 (analyzer `lucene.standard`; `subtype` filter path missing; `dynamic: true`) → WARN + drop +
  create (order asserted on the mocks); the vector ensure runs BEFORE the text ensure (call order);
- wait helper: parametrised over both names — ready, polls until queryable, FAILED raises naming the
  index, timeout fail-open WARN naming the index, entry without readiness fields is ready;
- `_create_search_index` wraps a failing create into the M0 message with the cause chained;
- `tests/unit/memory/rag/test_search.py::TestSearchMode` keeps passing against the renamed probe (the
  vector leg still probes `vector_index`); one test that `_search_index_is_queryable(collection,
  "text_search_index")` answers `False` for an absent entry and `True` on a raising probe.

## Acceptance criteria

- [ ] The Log carries the spike: the raw `list_search_indexes` entry of a `search`-type index on local
      mongot, the K=2 / K=1 result sets (parent and user B never returned), the stop-word answer, the
      absent-index behaviour (`[]` or raise), the seconds-to-ready — or a `USER ACTION REQUIRED` stop.
- [ ] `_wait_for_search_index_ready(collection, index_name)` and `_search_index_is_queryable(collection,
      index_name)` exist; `grep -rn "_wait_for_vector_index_ready\|_vector_index_is_queryable"
      apps/memory/src apps/memory/tests` is empty; every log line / raise of both names the index.
- [ ] `_build_text_search_index_definition()` returns `dynamic: False` static mappings with exactly the
      four `string`/`lucene.english` text paths (nested via `document`) and the four filter paths
      (`user_id: objectId`, `kind/type/subtype: token`); `_TEXT_INDEX_FILTER_PATHS` and
      `_TEXT_INDEX_TEXT_PATHS` are module constants; `merged_into` is not declared.
- [ ] `ensure_indexes` creates `text_search_index` when absent, leaves a matching one alone (server-echoed
      defaults are not drift), and drops + recreates on analyzer / path / `dynamic` drift with a WARN
      naming the differences; vector before text; the wait runs after every create.
- [ ] A failing `create_search_index` raises a `RuntimeError` naming the index and the Atlas M0 3-index
      cap, with the driver error chained.
- [ ] `ensure_indexes`'s docstring says it owns both mongot indexes; nothing in `indexing.py` still says
      `$text` is "not created here".
- [ ] Live, local env: `make memory-run-indexing-pipeline` logs the text-index create + ready lines; a
      second run logs "already up-to-date" for both mongot indexes; `make memory-search QUERY="ReAct"`
      still answers `search_mode: hybrid` (the `$text` leg is untouched);
      `db.memory.aggregate([{$listSearchIndexes: {}}])` lists exactly `vector_index` and
      `text_search_index`.
- [ ] `make memory-format-check`, `make memory-lint-check`, `make pre-commit`, `make memory-tests` green
      (env-status local).

## User Stories

### Story: An operator runs the indexing pipeline on a database that has only `vector_index`
1. The operator runs `make memory-run-indexing-pipeline` (local env).
2. The logs show `Vector search index 'vector_index' already up-to-date …`, then `Creating search index
   'text_search_index' …`, `Waiting for search index 'text_search_index' to be ready (up to 300 s)…`,
   then the ready line (on local mongot: "reports neither 'status' nor 'queryable'; treating it as
   ready").
3. `$listSearchIndexes` shows both indexes; `make memory-search QUERY="context engineering"` answers
   `search_mode: hybrid` exactly as before.

### Story: The same operator runs it again
1. The second run logs one "already up-to-date" line per mongot index and issues no create / drop.

### Story: Someone changes the analyzer in `_build_text_search_index_definition`
1. The next indexing run logs `Search index 'text_search_index' definition drift (have=…, want=…) —
   dropping and recreating`, drops, creates, waits.
2. The run after that is a no-op again.

### Story: The prod cluster already carries a stray third search index
1. `make memory-run-indexing-pipeline` against prod fails on the text-index create.
2. The error reads `Could not create search index 'text_search_index': Atlas M0 allows 3 search indexes
   per cluster (search + vectorSearch together) … drop the stray index in Atlas → Search & Vector Search
   and re-run …` with the driver's `MAXIMUM_INDEXES_FOR_TENANT_EXCEEDED` chained below it.

### Story: The SWE reads the spike before writing the drift check
1. The Log's raw catalogue entry shows whether `properties` comes back nested as `document` and which
   defaults mongot echoes; the comparison is written against that shape, so the first real run is a
   no-op on the second pass (Story 2) instead of an endless drop + recreate.

---

Blocked by: (none)

## Log

### [PA] 2026-10-08 19:30 — Grooming

**Summary**
Build the Atlas Search `text_search_index` and the two name-parametrised readiness helpers while the
`$text` leg keeps running, so task 191 can switch the leg onto an index that already exists everywhere.

**Key decisions**
- Spike before code (decision 8): `$search` on community mongot is unverified for `equals` on objectId /
  token, nested-`compound` `mustNot`, `minimumShouldMatch` and `lucene.english`; the spike's raw
  catalogue entry is what the drift check is written against.
- Drift = subset comparison on declared paths (type + analyzer), never whole-dict equality — the same rule
  the vector helper applies (dimensions + filter-path set), so server-echoed defaults cannot cause a
  drop + recreate loop.
- Nested paths are declared through `document` fields (Atlas static-mapping syntax), queried dotted.
- `text_index` stays in the Beanie set and `$text` stays the live leg until 191 — removing it here would
  kill lexical search on existing DBs before the replacement leg exists.
- One `_create_search_index` wrapper for both ensures carries the M0-cap message.

**Dependencies**
- None (first task of the feature). Blocks 191.

**User stories**
- 5 stories: first run creates, second run no-ops, drift heals, the M0 cap fails loudly, the spike guides
  the drift check.

Ready for implementation.
