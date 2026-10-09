---
id: 190-text-search-index-and-shared-mongot-helpers
status: done
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

- [x] The Log carries the spike: the raw `list_search_indexes` entry of a `search`-type index on local
      mongot, the K=2 / K=1 result sets (parent and user B never returned), the stop-word answer, the
      absent-index behaviour (`[]` or raise), the seconds-to-ready — or a `USER ACTION REQUIRED` stop.
- [x] `_wait_for_search_index_ready(collection, index_name)` and `_search_index_is_queryable(collection,
      index_name)` exist; `grep -rn "_wait_for_vector_index_ready\|_vector_index_is_queryable"
      apps/memory/src apps/memory/tests` is empty; every log line / raise of both names the index.
- [x] `_build_text_search_index_definition()` returns `dynamic: False` static mappings with exactly the
      four `string`/`lucene.english` text paths (nested via `document`) and the four filter paths
      (`user_id: objectId`, `kind/type/subtype: token`); `_TEXT_INDEX_FILTER_PATHS` and
      `_TEXT_INDEX_TEXT_PATHS` are module constants; `merged_into` is not declared.
- [x] `ensure_indexes` creates `text_search_index` when absent, leaves a matching one alone (server-echoed
      defaults are not drift), and drops + recreates on analyzer / path / `dynamic` drift with a WARN
      naming the differences; vector before text; the wait runs after every create.
- [x] A failing `create_search_index` raises a `RuntimeError` naming the index and the Atlas M0 3-index
      cap, with the driver error chained.
- [x] `ensure_indexes`'s docstring says it owns both mongot indexes; nothing in `indexing.py` still says
      `$text` is "not created here".
- [x] Live, local env: `make memory-run-indexing-pipeline` logs the text-index create + ready lines; a
      second run logs "already up-to-date" for both mongot indexes; `make memory-search QUERY="ReAct"`
      still answers `search_mode: hybrid` (the `$text` leg is untouched);
      `db.memory.aggregate([{$listSearchIndexes: {}}])` lists exactly `vector_index` and
      `text_search_index`.
- [x] `make memory-format-check`, `make memory-lint-check`, `make pre-commit`, `make memory-tests` green
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

### [SWE] 2026-10-08 19:46 — Spike (Scope step 0)

Verdict: **every gated feature works on `mongodb/mongodb-community-search:0.60.1`** — `lucene.english`,
`equals` on `objectId` + `token`, the nested-`compound` `mustNot`, `minimumShouldMatch`. No
`USER ACTION REQUIRED`. Throwaway pymongo script (scratchpad, not committed) against scratch DB
`tree_spike_190` (dropped after), definition exactly as step 2 (`properties` nested as `document`).

**Seconds to ready:** `create_search_index` → entry visible in `list_search_indexes` after **0.19 s**;
`$search` first answered rows after **0.82 s** (so the "treating it as ready" exit fires ~0.6 s before
mongot actually serves a freshly created index — harmless locally, same as `vector_index` today).

**RAW `list_search_indexes("text_search_index")` entry** (`$listSearchIndexes` returns the identical shape):
```json
{
  "id": "6ac7ef73a9e82a5f66624205", "name": "text_search_index", "type": "search",
  "latestDefinition": {
    "indexID": {"$oid": "6ac7ef73a9e82a5f66624205"}, "name": "text_search_index",
    "database": "tree_spike_190", "lastObservedCollectionName": "memory",
    "collectionUUID": {"$binary": {"base64": "lBv/RdWbQ3OIMtf/gPhVKA==", "subType": "04"}},
    "numPartitions": 1,
    "mappings": {
      "dynamic": false,
      "fields": {
        "aliases": {"type": "string", "analyzer": "lucene.english", "indexOptions": "offsets", "store": true, "norms": "include"},
        "user_id": {"type": "objectId"},
        "subtype": {"type": "token"},
        "kind": {"type": "token"},
        "name": {"type": "string", "analyzer": "lucene.english", "indexOptions": "offsets", "store": true, "norms": "include"},
        "type": {"type": "token"},
        "properties": {
          "type": "document", "dynamic": false,
          "fields": {
            "aliases": {"type": "string", "analyzer": "lucene.english", "indexOptions": "offsets", "store": true, "norms": "include"},
            "content": {"type": "string", "analyzer": "lucene.english", "indexOptions": "offsets", "store": true, "norms": "include"}
          }
        }
      }
    },
    "indexFeatureVersion": 4, "definitionVersion": 0, "definitionVersionCreatedAt": "2026-10-08T19:30:59Z"
  }
}
```
Shape facts the drift check is written against: `{id, name, type, latestDefinition}` — NO `status` /
`queryable` (as the vector entry); `properties` comes back nested as `document` (plus its own echoed
`"dynamic": false`); strings gain `indexOptions` / `store` / `norms`; NO `searchAnalyzer`, NO token
`normalizer` echoed. The flattener walks only `document.fields` and keeps `(type, analyzer)`, so all of
these are ignored.

**Result sets** (rows: A child, A parent, A entity `type: concept` with NO `subtype` / `name: "ReAct"`,
B child; ADR-015 §6 pipeline, `should` = `react`, `tools`):
```
--- K=2 should=[react, tools]: 1 row(s)
    a-child score=0.1214
--- K=1 should=[react, tools]: 2 row(s)
    a-entity score=0.5960
    a-child score=0.1214
```
Parent and user B never returned; the `mustNot` compound keeps a `subtype`-less entity.

**Stop words:** `should=[the] K=1` → 0 rows; `should=[a, in] K=1` → 0 rows on a row whose content
contains "a" and "in". Follow-up on a row containing "the … it is not the end … and": `the`, `is`,
`not`, `it`, `and` → 0 rows each; `[react, the] K=2` → 0 and `[react, in] K=2` → 0 (a stop-word clause
can never match, so it MUST NOT count toward M — task 191's `STOP_WORDS` must be a superset);
`[react, agent] K=2` → 1 (stemming: `agent` matches `agents`).

**Absent index:** the same pipeline with `index: "nope"` → **`[]`, no raise** (as `$vectorSearch`), so
ADR-015 §5's empty-leg probe assumption holds.

**`minimumShouldMatch` > should clauses** (`K=3`, 2 clauses) → `OperationFailure` code 8 `UnknownError`:
`"compound" minimumShouldMatch cannot be greater than number of should clauses`.

### [SWE] 2026-10-08 19:46 — Implementation

**Files modified**
- `apps/memory/src/tree/memory/rag/indexing.py` — `TEXT_SEARCH_INDEX_NAME`, `_TEXT_INDEX_TEXT_PATHS`,
  `_TEXT_INDEX_FILTER_PATHS`, `_build_text_search_index_definition` (+ `_set_static_mapping` nesting
  helper), `_extract_text_index_field_types`, `_text_search_index_drift` (subset compare → `(have, want)`),
  `_ensure_text_search_index`, `_create_search_index` (ONE wrapper, M0-cap `RuntimeError` from exc, used
  by both ensures); `_wait_for_vector_index_ready` → `_wait_for_search_index_ready(collection, index_name)`
  with `_SEARCH_INDEX_READY_TIMEOUT_S` / `_SEARCH_INDEX_POLL_S`; `ensure_indexes` = vector → text, docstring
  + module docstring item 2 rewritten; `index_entry_is_queryable`'s "ONE reader" paragraph names the new helpers.
- `apps/memory/src/tree/memory/rag/search.py` — `_vector_index_is_queryable` →
  `_search_index_is_queryable(collection, index_name, *, fallback_mode: SearchMode)`; WARN lines name the
  index and the fallback mode; `_vector_search` passes `VECTOR_INDEX_NAME, fallback_mode="text_only"`.
- `apps/memory/src/tree/entities/memory.py`, `apps/memory/src/tree/memory/pipeline.py` — two docstring
  sentences that said `ensure_indexes` owns only the vector index (now false).
- `apps/memory/tests/unit/memory/rag/test_indexing.py` — `_make_collection` answers per name + records
  `events` (create / drop / wait order); `_LIVE_TEXT_INDEX` = the spike's raw echo; `TestSearchIndexReadiness`
  parametrised over both names; `TestTextSearchIndexDefinition`, `TestEnsureTextSearchIndex`,
  `TestCreateSearchIndex`; existing vector tests adapted to two creates.
- `apps/memory/tests/unit/memory/rag/test_indexing_mongot_filter_paths.py` — `TestTextIndexPaths`.
- `apps/memory/tests/unit/memory/rag/test_search.py` — `TestSearchIndexIsQueryable` (absent text index →
  `False` while `vector_index` is present; raising probe → `True`).

**Tests**
- Unit: 5110 passing, 0 failing — `make memory-tests` (env-status local).
- Mutation check: making the flattener keep an echoed key (`store`) or lose the `document` prefix fails
  8 tests each (reverted).
- Integration: N/A (no suite in this repo; live runs below).

**Acceptance criteria**
- [x] Spike in the Log — entry above.
- [x] Renamed helpers — `grep -rn "_wait_for_vector_index_ready\|_vector_index_is_queryable" apps/memory/src
  apps/memory/tests` → exit 1 (empty); index-named lines pinned by
  `test_indexing.py::TestSearchIndexReadiness::*[vector_index|text_search_index]` and
  `test_search.py::TestSearchIndexIsQueryable`.
- [x] Definition — `test_indexing.py::TestTextSearchIndexDefinition::test_definition_is_static_english_text_paths_plus_filter_paths`,
  `test_indexing_mongot_filter_paths.py::TestTextIndexPaths::*`.
- [x] Ensure create / no-op / drift — `TestEnsureTextSearchIndex::test_absent_index_is_created_then_waited_on`,
  `::test_matching_index_is_left_alone[exactly-as-sent|local-mongot-echo|echoed-defaults-and-extra-path]`,
  `::test_drift_warns_then_drops_and_recreates[analyzer-lucene-standard|subtype-filter-missing|dynamic-true]`
  (drop → sleep 2 → create → wait), `::test_ensure_indexes_runs_vector_before_text`.
- [x] M0 error — `TestCreateSearchIndex::test_failing_create_names_the_index_and_the_m0_cap[vector_index|text_search_index]`
  (`__cause__ is driver_error`), `::test_ensure_indexes_routes_the_vector_create_through_it`.
- [x] Docstrings — `ensure_indexes` / module docstring say both mongot indexes; no "not created here" left.
- [x] Live, local — evidence below.
- [x] QA green — `make memory-format-check`, `make memory-lint-check`, `make pre-commit`, `make memory-tests`.

**Evidence**
```
$ make memory-tests
======================= 5110 passed in 69.35s (0:01:09) ========================
$ make memory-format-check && make memory-lint-check && make pre-commit
341 files already formatted / All checks passed! / ruff check Passed, ruff format Passed, prettier Passed, biome Passed

# Run 1 — DB had only vector_index (text_search_index dropped first)
$ make memory-run-indexing-pipeline      # exit 0; serve-process log:
INFO tree.memory.rag.indexing - Vector search index 'vector_index' already up-to-date (dimensions=1024, filters=['kind', 'merged_into', 'subtype', 'type', 'user_id'])
INFO tree.memory.rag.indexing - Creating search index 'text_search_index' (type=search)...
INFO tree.memory.rag.indexing - Waiting for search index 'text_search_index' to be ready (up to 300 s)...
INFO tree.memory.rag.indexing - Search index 'text_search_index' reports neither 'status' nor 'queryable' (local mongot); treating it as ready
# Run 2
INFO tree.memory.rag.indexing - Vector search index 'vector_index' already up-to-date (dimensions=1024, ...)
INFO tree.memory.rag.indexing - Search index 'text_search_index' already up-to-date (analyzer=lucene.english, text paths=['name', 'aliases', 'properties.content', 'properties.aliases'], filter paths={'user_id': 'objectId', 'kind': 'token', 'type': 'token', 'subtype': 'token'})
# Run 3 — after planting a drifted index via mongosh (properties.content on lucene.standard, no subtype)
WARNING tree.memory.rag.indexing - Search index 'text_search_index' definition drift (have={'properties.content': ('string', 'lucene.standard'), 'subtype': None}, want={'properties.content': ('string', 'lucene.english'), 'subtype': 'token'}) — dropping and recreating
INFO tree.memory.rag.indexing - Creating search index 'text_search_index' (type=search)...
INFO ... Waiting for search index 'text_search_index' ... treating it as ready
# Run 4 — both "already up-to-date" again, no create / drop

$ mongosh … --eval 'db.memory.aggregate([{$listSearchIndexes: {}}]).toArray().map(i => i.name)'
[ 'vector_index', 'text_search_index' ]

$ make memory-search QUERY="ReAct"            # exit 0, 10 parents, no "Search ran …" caveat => hybrid
vector leg: 40 candidate(s), 5 kept at min_vector_score=0.70 (top=0.719)
text leg: 40 candidate(s), 40 kept at min_text_score=0.00 (top=1.062)
$ make memory-search QUERY="context engineering"   # exit 0, 10 parents, no caveat => hybrid
```

**Notes**
- **Pipeline INFO logs are invisible by default.** In the served flow process Prefect leaves the root
  logger at WARNING, so `tree.*` INFO lines (the vector ones too, pre-existing) never reach the console
  or the Prefect API. The evidence above was captured by serving with
  `PREFECT_LOGGING_ROOT_LEVEL=INFO PREFECT_LOGGING_EXTRA_LOGGERS=tree make memory-serve-workflows`;
  the drift WARNING is visible without it. Not changed here (out of scope) — a candidate follow-up.
- **Stale Docker worker.** `tree-prefect-worker` runs an image of older main code and raced my serve
  process for the first dispatch (`SignatureMismatchError … run_indexing`). I stopped it for the live
  runs and `docker start`-ed it again afterwards.
- The WARN probe lines now read `Search leg unavailable: search index '<name>' absent; the query runs
  <mode>` (was `Vector search leg unavailable: …`). `_vector_search`'s own aggregate-raise WARN still
  says "Vector search leg unavailable" (pinned by `test_leg_failure_logs_traceback`).
- `_create_search_index` wraps ANY driver error in the M0-cap message (as specified); the message is
  conditional ("A MAXIMUM_INDEXES_FOR_TENANT_EXCEEDED cause means …") and includes the driver text.
- ADR-015 §6 records the definition in prose; I did not edit the ADR (read-only for SWE). The exact dict
  is pinned by `TestTextSearchIndexDefinition`.
- Prod (Atlas M0) not exercised — local only, per the task.

### [Tester] 2026-10-08 22:55 — QA

**Test summary**
- Format / lint / pre-commit: PASS (`341 files already formatted`, `All checks passed!`, ruff/prettier/biome Passed; env-status = local)
- Unit tests: 5110 passed / 0 failed (`make memory-tests`, 74 s)
- Integration tests: N/A (suite deliberately deleted; live runs below)
- Warnings: 0
- `grep -rn "_wait_for_vector_index_ready\|_vector_index_is_queryable" apps/memory/src apps/memory/tests` → exit 1 (empty)

**E2E adversarial pass** (live local mongot via `make memory-serve-workflows` from this worktree with `PREFECT_LOGGING_ROOT_LEVEL=INFO PREFECT_LOGGING_EXTRA_LOGGERS=tree`, Docker `tree-prefect-worker` stopped for the run and `docker start`-ed again afterwards: `Up`, as found)
- Happy path: dropped `text_search_index` via mongosh → `make memory-run-indexing-pipeline` → `Creating search index 'text_search_index' (type=search)...` / `Waiting for ... (up to 300 s)` / `reports neither 'status' nor 'queryable' (local mongot); treating it as ready`. Run 2 → `Vector search index ... already up-to-date`, `Search index 'text_search_index' already up-to-date (analyzer=lucene.english, text paths=[...], filter paths={...})`, no create/drop. `$listSearchIndexes` = `['vector_index','text_search_index']`. `make memory-search QUERY="ReAct"` and `"context engineering"` → exit 0, both `vector leg: 40 candidate(s)…` and `text leg: 40 candidate(s)…` logged, no "Search ran" caveat (hybrid). Raw `$search` against the new index on the real DB answers rows. PASS
- Break 1 (drift, live): hand-planted `name` on `lucene.standard` + no `subtype` path → run logs `definition drift (have={'name': ('string','lucene.standard'),'subtype': None}, want={'name': ('string','lucene.english'),'subtype': 'token'}) — dropping and recreating`, create, ready; next run no-op. PASS
- Break 2 (echo tolerance, live scratch DB `tree_qa_190`, dropped after): hand-created index with extra `searchAnalyzer`, extra `created_at` date field, token `normalizer: lowercase` → no-op; fields declared in REVERSED order → no-op. PASS
- Break 3 (state edge, live): two concurrent `_ensure_text_search_index` on an empty collection → both returned `None`, exactly one index results; third call no-op. PASS
- Break 4 (drift function, offline, 17 shapes): as-sent / `definition` key instead of `latestDefinition` / reordered / `{}` latest + real `definition` → `({}, {})` (correct; note `latestDefinition: {}` is falsy so falls to `definition`); no `properties` doc, `properties` doc with no `fields`, `fields: None`, empty entry, `dynamic` absent or a dict, analyzer absent, `user_id` as string, `name` as `document` → drift reported, no crash. PASS
- Break 5 (wait loop): FAILED (also with `queryable: true`), BUILDING→READY, absent→READY, 60-poll fail-open timeout WARN naming the index — each pinned by `TestSearchIndexReadiness` for BOTH index names; read the loop (FAILED checked before `queryable`, poll `list_search_indexes(index_name)`). PASS
- Break 6 (M0 wrapper): `ConnectionFailure("mongot down")` and `OperationFailure("Index already exists")` → both re-raised as the M0 `RuntimeError` with `__cause__` set; `asyncio.CancelledError` passes through unwrapped. PASS (see note 2)

**Acceptance criteria**
- [x] PASS — Spike in Log (raw entry, K=2/K=1 sets, stop words, absent index `[]`, seconds-to-ready, K>#should error) — Log entry "[SWE] Spike"; its entry shape matches what I observed on the live index (`latestDefinition.mappings.dynamic == False`, nested `document`)
- [x] PASS — `_wait_for_search_index_ready(collection, index_name)` / `_search_index_is_queryable(collection, index_name, *, fallback_mode)` exist, old names gone (grep empty), every log/raise names the index (`indexing.py:776-851`, `search.py:219-280`; tests `TestSearchIndexReadiness[*]`, `TestSearchIndexIsQueryable`). On the extra required kw-only `fallback_mode`: the task states "a keyword, or the caller logging it — the SWE's choice", so it satisfies the AC; it is a hard required arg task 191's text leg must pass (`"vector_only"`)
- [x] PASS — Definition: `TestTextSearchIndexDefinition::test_definition_is_static_english_text_paths_plus_filter_paths`, `test_indexing_mongot_filter_paths.py::TestTextIndexPaths` (user_id first, no `merged_into`, 4 text paths); constants at `indexing.py:~535-560`
- [x] PASS — Ensure create / no-op / drift: `TestEnsureTextSearchIndex` (absent, 3× matching incl. local echo and echoed defaults, 3× drift with drop → sleep 2 → create → wait order, vector-before-text) all pass; live run 1/2/3/4 above
- [x] PASS — M0: `TestCreateSearchIndex[vector_index|text_search_index]` (names index, "Atlas M0 allows 3 search indexes per cluster", `__cause__ is driver_error`); `ensure_indexes` routes the vector create through it
- [x] PASS — Docstrings: `ensure_indexes` + module docstring say both mongot indexes; `grep "not created here"` has no hit in src
- [x] PASS — Live local: see happy path; hybrid confirmed by both legs logging
- [x] PASS — format/lint/pre-commit/tests green

**Other issues found (none blocking; for the orchestrator to triage)**
1. Drift check false-negative on literal dotted keys: a live definition declaring `"properties.content": {...}` as a flat dotted KEY (which indexes a field literally named so, not `properties.content`) flattens identically to the nested form and reads as up-to-date. Only reachable via a hand-made index; `_extract_text_index_field_types` could skip keys containing `.`.
2. `_extract_text_index_field_types` crashes (`AttributeError: 'list' object has no attribute 'get'`) on a field mapping given as an array of mappings (valid Atlas syntax). Only reachable via a hand-made index; crash is loud, not silent.
3. `_create_search_index` wraps ANY exception (connection failure, "already exists") in the M0-cap message — per spec, but it is misleading for non-cap failures. The driver text is appended and the cause chained, so diagnosable.
4. `searchAnalyzer` differing from `lucene.english` is treated as non-drift (per task: echoed defaults are not drift). If someone sets a different query-time analyzer, the stop-word behaviour task 191 relies on changes silently.
5. `mappings.dynamic` ABSENT reads as drift. Local mongot echoes `dynamic: false` (verified live); Atlas M0 echo is unverified here — if Atlas omits `dynamic` when false, prod would drop + recreate on every run. Worth a look on the first prod run (the second prod run must say "already up-to-date").
6. Pre-existing, noted by SWE: served-flow `tree.*` INFO lines are invisible by default (root logger WARNING); the Docker `tree-prefect-worker` runs an older image and fails `run_indexing` with SignatureMismatchError. Not caused by this change.
7. Nit: `entities/memory.py` docstring line now exceeds the surrounding wrap width (ruff does not flag it).

**VERDICT: PASS**
