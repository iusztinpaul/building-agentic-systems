---
id: 169-mode-aware-memory-indexes
status: done
feature: memory-indexes
---

# Trim the `memory` collection indexes and make the set mode-aware

Tags: `memory`, `mongodb`, `refactor`, `indexes`
Depends on: None (code-wise). Ships on its OWN branch `refactor/memory-indexes` off `main`, routed
through `/squid-refactor` — NOT part of `dynamic-graph-viz`, never stacked on `feat/dynamic-graph-viz`
or `feat/document-unique-key`. Number 169 is reserved across branches (162–168 live on
`feat/dynamic-graph-viz`).
Blocks: —
Implements: ADR-012 (`docs/adrs/012_mode_aware_memory_index_set.md`)

## Scope

Source of truth: `docs/notes/plan_mongodb.md` (human-edited, status "ready to bake"; its decisions are
final — implement them, do not re-open). Line numbers below were taken from `feat/dynamic-graph-viz`
HEAD (`main` + tasks 162–167); on this branch (off `main`) re-locate every reference by symbol.

The **`memory` collection** carries 11 classic indexes today. Several serve no production query, one
(`user_kind_embedding`, multikey over the 1024-float vector) would cost ~16 KB per embedded row on
prod's Atlas M0 (512 MB cap, data + indexes), and `rag` **Memory mode** carries graph-only indexes it
never uses. Prod `tree.memory` is EMPTY (the **Memory Pipeline** has never run there) and carries only
the 6 Beanie-declared indexes, so nothing on prod needs migrating — the code change alone fixes prod on
its first boot + first memory run. No prod usage evidence exists or can exist (M0: no Performance
Advisor, `$indexStats` denied); decisions rest on the code audit + local `explain` in the plan.

### Target index set (plan § "Mode-aware set")

| Index | Keys / options | `rag` | `graphrag` |
|---|---|---|---|
| `_id_` | `_id` | ✓ | ✓ |
| `user_kind_type_subtype` | `(user_id, kind, type, subtype)` | ✓ | ✓ |
| `user_type_name` | `(user_id, type, name)` | ✓ | ✓ |
| `active_user` | `(properties.is_active_user)`, `partialFilterExpression: {"properties.is_active_user": true}` | ✓ | ✓ |
| `text_index` | `$text` on `name`, `aliases`, `properties.content`, `properties.aliases` (unchanged) | ✓ | ✓ |
| `vector_index` | mongot vector search index (unchanged) | ✓ | ✓ |
| `user_kind_source_node` | `(user_id, kind, source_node_id)` | — | ✓ |
| `user_kind_target_node` | `(user_id, kind, target_node_id)` | — | ✓ |

11 → 6 (`rag`) / 8 (`graphrag`), counting `vector_index`. Retired everywhere: `user_kind_type` (pure
prefix of `user_kind_type_subtype`), `kind_1` (replaced by `active_user`), `user_kind_embedding`
(multikey on the vector), `user_type_semantic_type` (no query uses `semantic_type` as a key —
`grep -rn '"semantic_type"' apps/memory/src/tree` shows only writers, the two index declarations and
display code), `user_canonical_name_index` (no query uses `canonical_name` as a key; resolution matches
names in Python over a `{user_id, kind, type, merged_into}` fetch, `pipeline.py:~1045-1058`). Retired in
`rag` only: the two `*_node` indexes (`$graphLookup` is graphrag-only).

### Ownership split after this task
- **Beanie (`MemoryEntry.Settings.indexes`) owns every classic index**, bound per mode at boot.
- **`ensure_indexes` owns only what Beanie cannot express** — the `$text` index and the mongot
  `vector_index` — plus the idempotent retirement of the names above.
- Creation happens on EVERY entry point (all go through `db.py::init_mongodb`; none calls `init_beanie`
  directly). Retirement is **lazy**: only `ensure_indexes` retires — the memory pipeline's indexing phase
  (`pipeline.py:~2270`, always) and MCP boot (`mcp/server.py:~205`, unless `MCP_SKIP_INDEX_BOOTSTRAP=true`).
  A stale index only costs writes until the next indexing run. `init_beanie` never drops
  (`allow_index_dropping` stays off).

### Implementation
1. `config/app_config.py` (`MemoryConfig.mode`, ~l.659) — extract the inline `Literal["rag", "graphrag"]`
   into a module-level `MemoryMode = Literal["rag", "graphrag"]` alias (cf. `ModelKind`) and use it in the
   field annotation. Behaviour unchanged.
2. `entities/memory.py` — add `memory_indexes(mode: MemoryMode) -> list[IndexModel]`: the base set
   (`user_kind_type_subtype`, `user_type_name`, `active_user`) plus, for `graphrag`, the two `*_node`
   `IndexModel`s (same keys/names as today's `create_index` calls in `rag/indexing.py`).
   `Settings.indexes` keeps a static value (the `graphrag` set as the import-time default so the model is
   importable without a DB); `init_mongodb` overrides it per mode. `kind: Indexed(str)` (~l.243) →
   `kind: str` — REQUIRED, or Beanie recreates `kind_1` on every boot. Importing `MemoryMode` from
   `tree.config.app_config` is cycle-free; a `TYPE_CHECKING`-guarded import is the fallback if the
   import-time YAML load bites a test. Rewrite the stale comments (~l.237-242 no standalone `user_id`
   index; ~l.606-611 "the dynamic indexes ... created in tree.memory.rag.indexing") to describe the
   ownership split.
3. `db.py::init_mongodb` — `MemoryEntry.Settings.indexes = memory_indexes(app_config.memory.mode)`
   immediately before `init_beanie` (Beanie reads `Settings.indexes` at init). Read the mode ONCE here.
4. `rag/indexing.py::ensure_indexes` — delete the five compound `create_index` calls
   (`user_kind_source_node`, `user_kind_target_node`, `user_kind_embedding`, `user_canonical_name_index`,
   `user_type_semantic_type`) and the `_CANONICAL_NAME_INDEX` constant if nothing else uses it. Keep: the
   `_drop_legacy_compound_indexes` call, the `$text` `create_index`, `_ensure_vector_index`. Update the
   docstring (text + vector only; retirement).
5. `rag/indexing.py::_drop_legacy_compound_indexes` — make the retired list mode-aware:
   `retired_index_names(mode: MemoryMode) -> tuple[str, ...]` = the four pre-#019 names already in
   `_LEGACY_COMPOUND_INDEX_NAMES` (keep — zero cost, idempotent) + always `user_kind_type`,
   `user_kind_embedding`, `kind_1`, `user_type_semantic_type`, `user_canonical_name_index`; plus
   `user_kind_source_node`, `user_kind_target_node` when mode is `rag`. Read the mode from
   `app_config.memory.mode` at call time (same source `init_mongodb` uses). The name-by-name
   `index_information()` → `drop_index(name)` loop and its non-fatal error handling stay. Update its
   docstring ("pre-#019" → the retirement contract) and the `_LEGACY_COMPOUND_INDEX_NAMES` comment.
6. `memory/pipeline.py` `memory_indexing` docstring (~l.2302): "``ensure_indexes`` re-asserts the global
   compound indexes whose leading key is ``user_id``" → "ensures the text + vector search indexes and
   retires indexes this mode no longer declares; classic indexes are created by `init_mongodb`".
7. No operator step, no migration script, no new YAML knob, no `.env.example` change. The upgrade
   trigger for the dropped backfill index is RECORDED, not built: a partial index on a scalar
   `embedding_pending: true` marker — never a multikey index on `embedding`.

### Tests (`/squid-testing-python`)
Load-bearing context: the unit suite's session fixture (`tests/unit/conftest.py:~255-264`) runs the REAL
`init_mongodb` against local Mongo database `unit_tests_twin`, so every `make memory-tests` run boots
Beanie with `Settings.indexes` for real. Beanie (pinned in `uv.lock`) compares keys + options + name and
calls `create_indexes` on every boot — a same-keys/different-name drift fails every entry point with Mongo
error 85/86, and a name both declared and retired is dropped + recreated on every boot/indexing cycle.

- `tests/unit/entities/test_memory.py` — replace `TestMemorySettingsIndexes`, `TestSubtypeIndexDeclared`
  and `TestSemanticTypeIndex` with one class that pins, for `memory_indexes("rag")` and
  `memory_indexes("graphrag")`: the exact name set from the table; every key list except `active_user`
  leads with `("user_id", 1)`; `user_kind_source_node` / `user_kind_target_node` keys exactly as today;
  `active_user` key `[("properties.is_active_user", 1)]` with
  `partialFilterExpression == {"properties.is_active_user": True}`; `MemoryEntry.model_fields["kind"]`
  carries no Beanie `Indexed` metadata (mirror `test_documents.py`'s inline-unique check).
- New `tests/unit/test_db.py` — through the REAL `init_mongodb` path, for each mode: patch
  `app_config.memory.mode`, call `init_mongodb(uri, "<throwaway db per mode>")`, assert
  `{im.document["name"] for im in MemoryEntry.Settings.indexes}` == the mode's set AND
  `index_information()` on that database's `memory` collection == `{"_id_"} ∪ that set`. Teardown MUST drop
  the throwaway database and re-run `init_mongodb(uri, TEST_DATABASE)` — `init_beanie` rebinds the global
  `MemoryEntry` settings, so leaving the rebinding in place breaks the rest of the session.
- Disjointness: for each mode, `names(memory_indexes(mode)) ∩ retired_index_names(mode) == ∅`;
  `"kind_1"` and `"user_kind_embedding"` are in both modes' retired set; the `*_node` pair is retired in
  `rag` only; `_id_`, `text_index`, `vector_index` are retired in neither.
- `tests/unit/memory/rag/test_indexing.py` — `test_creates_compound_indexes` → `create_index` awaited
  exactly once, with `name="text_index"` (no compound `create_index`); delete
  `test_canonical_name_index_created`; new retirement tests using the existing `_make_collection`
  (override `index_information` to return all 11 legacy names): `rag` → `drop_index` awaited for the 5
  always-retired names + the 2 `*_node` names and never for `user_kind_type_subtype`, `user_type_name`,
  `active_user`, `text_index`; `graphrag` → the 5 only; `index_information` returning only the kept set →
  `drop_index` never awaited (idempotent). Keep every vector-index test green.
- `select_active_user_ids` ↔ `active_user` parity: one test pinning that the query's
  `properties.is_active_user` clause (`entities/users.py:~166-173`) equals the index's partial filter, so
  editing either side alone fails (a module constant is the least code).

### Local verification (LOCAL ONLY — `make env-status` → `local`; STOP if it prints prod)
Serve workflows FROM THIS BRANCH's worktree (`make memory-serve-workflows &`; stop the Docker
`prefect-worker` if it is running — it executes main's image). Paste every output in `## Log`.
NOTE: the shared local Mongo `documents` collection may carry a `user_source_uri_unique` index shape from
another branch (`feat/document-unique-key` vs `main`) — if a boot fails with an index name conflict on
`documents`, record it and coordinate with the orchestrator; do NOT change `documents` indexes here.

1. **Before.** `db.memory.getIndexes().map(i => i.name)` → expect today's classic names. Capture
   `explain("executionStats")` BEFORE the change for: (a) the `select_active_user_ids` filter
   (`{kind:"node", type:"person", name:"self", "properties.is_active_user": true}`, projection
   `{user_id:1}`); (b) the backfill filter (`rag/indexing.py::_backfill_filter`, with the local user_id);
   (c) the reset filter; (d) the resolution fetch (`pipeline.py`, `type:"person"`); (e) the real
   `expand_graph` aggregate (`retrieval.py`: `$match` + two `$graphLookup`, seed = one real node `_id`,
   `maxDepth` 0) via `db.memory.explain("executionStats").aggregate([...])`. Record winning index name,
   `totalKeysExamined`, `totalDocsExamined` for each.
2. **graphrag (YAML default).** `make memory-run-indexing-pipeline` → `ensure_indexes` runs. Expect
   `getIndexes()` names == `_id_, user_kind_type_subtype, user_type_name, active_user,
   user_kind_source_node, user_kind_target_node, text_index` (7) + `vector_index` in
   `getSearchIndexes()`; the serve-shell log shows `Dropped legacy compound index '<name>'` for exactly
   `user_kind_type, kind_1, user_kind_embedding, user_type_semantic_type, user_canonical_name_index`
   (order free). Re-run the same target: zero drop lines, same `getIndexes()`.
3. **After.** Re-run the five explains: (a) → `active_user`, `totalKeysExamined: 1`,
   `totalDocsExamined: 1`; (e) → the `$graphLookup` stages use `user_kind_source_node` /
   `user_kind_target_node`; (b)(c)(d) → `user_kind_type_subtype` (or its prefix) with `totalDocsExamined`
   == the before number. (b) after the drop examines every embeddable node of the user once per indexing
   run — expected, record it.
4. **rag retirement on a leftover graphrag collection.** In the serve shell `export TREE_MEMORY__MODE=rag`,
   re-serve, `TREE_MEMORY__MODE=rag make memory-run-indexing-pipeline` WITHOUT dropping `memory`
   (index-only check). Expect two more drop lines (`user_kind_source_node`, `user_kind_target_node`) and
   `getIndexes()` == `_id_, user_kind_type_subtype, user_type_name, active_user, text_index` (5) +
   `vector_index`.
5. **`run-pipelines-e2e` in `rag`, then `graphrag`** (`.agents/skills/run-pipelines-e2e/SKILL.md`): drop
   `memory` before each mode (ADR-006 / the skill; guarded one-liner — env-status local, host `localhost`)
   — **requires the human's OK, see Open questions OQ2**; signup if needed;
   `make memory-run-pipeline SOURCE_FILE=<light 3-article subset>`;
   `make memory-query-graph QUERY="how does memory for ai agents work"` (`rag` prints text, `graphrag`
   writes HTML). Straight after each run `getIndexes()` matches the mode's set with ZERO drop lines (fresh
   collection → nothing to retire). Finish in `graphrag`. Stop the serve process; restart
   `tree-prefect-worker` only if it was running before.
6. `make memory-format-fix && make memory-lint-fix && make memory-format-check && make memory-lint-check
   && make pre-commit && make memory-tests` green.

### Prod (after merge — nothing for the SWE to run)
- First boot of ANY entry point creates `active_user` (+ the `*_node` pair; prod mode is `graphrag`); the
  first memory run retires `kind_1`, `user_kind_type`, `user_type_semantic_type` from the empty
  collection. No operator step.
- `[HUMAN]` one-off, unrelated to this code: `db.knowledge_graph.drop()` on Atlas `tree` (legacy
  pre-rename collection, 1 row, 6 indexes). Human only — agents never write prod.

## Out of scope
- M0 capacity before the first prod memory run (≈ 200 MB of embedded rows even without
  `user_kind_embedding` vs the 512 MB cap — measure, then upgrade tier or cap ingestion). Separate task.
- Edges carrying every document of their extraction shard in `sources` (`pipeline.py:1452`); dangling
  `related_to` edges from the `paul iusztin` vs `paul-iusztin` id mismatch. Separate tasks (167 flagged both).
- Building the `embedding_pending` partial index (recorded as the upgrade trigger only).
- Any change to `text_index` fields, the vector index definition / filter paths, or the dimension gate.
- `users.identifier: Indexed(str, unique=True)` and every other collection's indexes (incl. `documents`).
- Migrating or touching prod data; any index work by hand.

## Acceptance Criteria

> `[HUMAN]` criteria are done by the human; the SWE/Tester do not block on them.

- [x] `memory_indexes("rag")` and `memory_indexes("graphrag")` return exactly the table's sets (names,
      keys, `active_user` partial filter), `MemoryEntry.kind` is a plain `str`, and the sets are disjoint
      from `retired_index_names(mode)` for both modes (unit tests).
- [x] `init_mongodb` binds `MemoryEntry.Settings.indexes` from `app_config.memory.mode` before
      `init_beanie`; `tests/unit/test_db.py` proves it for both modes against a real throwaway database and
      restores the session binding (unit tests; `make memory-tests` green, no error 85/86 at session start).
- [x] `ensure_indexes` awaits `create_index` exactly once (`text_index`), still calls the retirement drop,
      and the retirement drops exactly the 5 always-retired names (+ the 2 `*_node` names in `rag`) and never
      a kept name (unit tests). `grep -n "create_index(" apps/memory/src/tree/memory/rag/indexing.py` → 1
      line; `grep -rn "user_type_semantic_type\|user_canonical_name_index\|user_kind_embedding" apps/memory/src`
      → only inside the retired-names list.
- [x] `select_active_user_ids`'s `properties.is_active_user` predicate and the `active_user` partial filter
      are pinned equal (unit test).
- [x] Local graphrag indexing run: `getIndexes()` == the 7 classic graphrag names + `vector_index`; the 5
      drop log lines appear once and not on the second run (output in `## Log`).
- [x] Local explains after the change: `select_active_user_ids` → `active_user` 1 key / 1 doc;
      `expand_graph` aggregate → `user_kind_source_node` / `user_kind_target_node`; backfill, reset and
      resolution reads → `user_kind_type_subtype` with `totalDocsExamined` unchanged vs the before capture
      (before/after numbers in `## Log`).
- [x] Local rag indexing run on the leftover graphrag collection retires the `*_node` pair → `getIndexes()`
      == the 5 classic rag names (+ `vector_index`); `run-pipelines-e2e` green in `rag` and `graphrag` with
      zero drop lines on fresh collections (output in `## Log`) — only if OQ2 is approved.
- [x] Docstrings/comments updated: `entities/memory.py` (the two stale comments), `ensure_indexes`,
      `_drop_legacy_compound_indexes` + its constant comment, the `memory_indexing` docstring.
- [x] `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green.
- [ ] [HUMAN] `db.knowledge_graph.drop()` executed on Atlas `tree` (one-off, outside the code change).

## User Stories

### Story: Operator boots any entry point in `graphrag`
1. `make memory-signup ...` (or any script / flow / the MCP server) on a database whose `memory`
   collection does not exist yet.
2. `db.memory.getIndexes()` lists `_id_`, `user_kind_type_subtype`, `user_type_name`, `active_user`,
   `user_kind_source_node`, `user_kind_target_node` — and no `kind_1`.

### Story: Operator runs the memory pipeline on a database that still carries the 11 old indexes
1. `make memory-run-indexing-pipeline` (default `graphrag`).
2. The serve log prints `Dropped legacy compound index '<name>'` for `user_kind_type`, `kind_1`,
   `user_kind_embedding`, `user_type_semantic_type`, `user_canonical_name_index`; `getIndexes()` now lists
   the 7 classic graphrag names.
3. A second run prints no drop line and changes nothing.

### Story: Operator switches the deployment to `rag`
1. Drops `memory` (ADR-006 rule), exports `TREE_MEMORY__MODE=rag`, runs `make memory-run-pipeline`.
2. `getIndexes()` lists `_id_`, `user_kind_type_subtype`, `user_type_name`, `active_user`, `text_index`; no
   `*_node` index was ever created. If the operator forgot to drop, the first indexing run retires the two
   `*_node` indexes instead and logs it.

### Story: The scheduled dream (and the nightly data pipeline) fans out per active user
1. `select_active_user_ids` runs at 03:00 UTC across all tenants (no `user_id` in the filter).
2. `explain` shows an `IXSCAN` on `active_user` with one key per active user, instead of scanning every
   node through `kind_1` for a single result.

### Story: Developer runs `make memory-tests`
1. The session fixture boots Beanie against `unit_tests_twin` with the default (`graphrag`) set.
2. The suite starts without a Mongo index conflict and `tests/unit/test_db.py` additionally proves the
   `rag` set on a throwaway database, then restores the session binding.

### Story: Prod's first memory run after merge
1. The human merges; the next Prefect boot creates `active_user` + the `*_node` pair on the empty
   `tree.memory`; the first `memory-indexing-etl` run retires `kind_1`, `user_kind_type`,
   `user_type_semantic_type`.
2. The 14,439 waiting documents are embedded without `user_kind_embedding` ever existing on prod (M0
   capacity is handled by its own follow-up task before that run is triggered).

---

## Log

### [PA] 2026-10-02 12:30 — Grooming

**Summary**
Bake `docs/notes/plan_mongodb.md` into one task: the `memory` collection's classic indexes become a
per-mode set owned by `MemoryEntry.Settings.indexes` (bound in `init_mongodb`), `ensure_indexes` keeps only
the `$text` + mongot vector indexes plus a mode-aware, idempotent retirement list; 11 → 6 (`rag`) / 8
(`graphrag`); `kind_1` is replaced by a partial `active_user` index; nothing is migrated by hand.

**Key decisions**
- Plan decisions are final; this grooming only maps them onto the current code.
- Code audit confirms every "serves nothing" claim: no `KGQuery(` instantiation in `src`/`scripts`
  (`find_nodes(name=)` is tests-only), no query uses `semantic_type` or `canonical_name` as a key, and every
  entry point calls `init_mongodb` (none calls `init_beanie` directly).
- Keep the four pre-#019 names in the retired list (idempotent, zero cost).
- `MemoryMode` is imported into `entities/memory.py` from `app_config` — cycle-free; `TYPE_CHECKING` guard
  is the fallback.
- `rag` retirement of the `*_node` pair is verified index-only on the leftover graphrag collection, THEN
  `memory` is dropped for the real `rag` e2e, per ADR-006 and the e2e skill.
- The through-`init_mongodb` tests use a throwaway database per mode and restore `unit_tests_twin`
  afterwards.

**Open questions (human — unanswered when the task was written; the orchestrator took no default)**
- **OQ1 — ADR-012?** Recommended YES: `docs/adrs/012_mode_aware_memory_index_set.md` recording (a) Beanie owns
  every classic `memory` index, bound per mode in `init_mongodb`; `ensure_indexes` owns `$text` + vector +
  retirement; (b) the per-mode set (legal because ADR-006 requires dropping `memory` on a mode switch);
  (c) lazy in-code retirement, no migration; (d) never a multikey index over `embedding` (upgrade trigger: a
  partial index on a scalar `embedding_pending` marker); (e) the partial `active_user` index for the
  cross-tenant fan-out. If approved, the PA writes it before implementation and this task gains
  "Implements: ADR-012".
- **OQ2 — local `memory` drops during verification (step 5)?** Recommended YES (167 precedent): drop the
  local `memory` collection twice, guarded (env-status local, host localhost), ending in `graphrag`. If
  declined, skip step 5 and its AC; steps 1–4 still run on the existing collection.

(Orchestrator note: the branch/worktree question was resolved by default — own worktree
`building-agentic-systems-memory-indexes` on `refactor/memory-indexes` off `main`, plan committed at
`docs/notes/plan_mongodb.md`, build not started.)

Ready for implementation once OQ1 and OQ2 are answered.

### [SWE] 2026-10-02 14:15 — Implementation

Human decisions: OQ1 = YES (ADR-012 written), OQ2 = YES (two guarded local `memory` drops, ending in
`graphrag`). "Blocked by" removed, "Implements: ADR-012" added.

**Files modified**
- `docs/adrs/012_mode_aware_memory_index_set.md` — NEW. Records (a) Beanie owns the classic set, bound per mode in `init_mongodb`, and `ensure_indexes` owns `$text` + vector + retirement; (b) the per-mode table (legal because of ADR-006's drop-on-switch rule); (c) lazy in-code retirement with no migration; (d) no multikey index on `embedding`, with the `embedding_pending` partial-index trigger; (e) the partial `active_user` index sharing `ACTIVE_USER_FILTER`.
- `apps/memory/src/tree/config/app_config.py` — `MemoryMode` alias used by `MemoryConfig.mode`.
- `apps/memory/src/tree/entities/memory.py` — `ACTIVE_USER_FILTER`, `memory_indexes(mode)`, `kind: str`, `Settings.indexes = memory_indexes("graphrag")` as the import-time default, and the two stale comments rewritten.
- `apps/memory/src/tree/entities/users.py` — `select_active_user_ids` spreads `**ACTIVE_USER_FILTER`.
- `apps/memory/src/tree/db.py` — `init_mongodb` binds `MemoryEntry.Settings.indexes = memory_indexes(app_config.memory.mode)` before `init_beanie`.
- `apps/memory/src/tree/memory/rag/indexing.py` — `ensure_indexes` now creates only `text_index` (+ vector). Adds `_RETIRED_INDEX_NAMES` (the 4 pre-#019 names + the 5 trimmed names; renamed from `_LEGACY_COMPOUND_INDEX_NAMES`, which the task said to keep: the contents are no longer only legacy, and nothing else referenced the old name), `_GRAPH_ONLY_INDEX_NAMES` and `retired_index_names(mode)`. `_drop_legacy_compound_indexes` iterates the retired names. `_CANONICAL_NAME_INDEX` is deleted, and docstrings and comments are updated.
- `apps/memory/src/tree/memory/pipeline.py` — `memory_indexing` docstring.
- `apps/memory/tests/unit/entities/test_memory.py` — `TestMemoryIndexes` (exact per-mode names, `user_id`-leading keys, `*_node` keys, `active_user` partial filter, no `embedding` key, `kind` annotation is plain `str`). It replaces the 3 old index classes.
- `apps/memory/tests/unit/memory/rag/test_indexing.py` — `test_creates_only_the_text_index` plus `TestIndexRetirement`: disjointness per mode, `_id_`/`text_index`/`vector_index` never retired, `*_node` retired in rag only, a legacy 11-index collection drops exactly 7 names in rag and 5 in graphrag, and a collection already on its set drops nothing.
- `apps/memory/tests/unit/entities/test_users.py` — `TestActiveUserIndexParity` (the query predicate equals the partial filter).
- `apps/memory/tests/unit/test_db.py` — NEW. Runs the real `init_mongodb` once per mode against a throwaway DB: declared set == live `index_information()` minus `_id_`. Teardown drops the DB and re-inits `unit_tests_twin`.

**Tests**
- Unit: 4531 passed, 0 failed (`make memory-tests`, 61.09 s). No error 85/86 at session start.
- Integration: N/A (no suite by design). E2E evidence is below.

**Acceptance criteria**
- [x] `memory_indexes` sets, `kind: str`, disjoint from `retired_index_names`. Verified by `test_memory.py::TestMemoryIndexes` and `test_indexing.py::TestIndexRetirement::test_retired_names_never_overlap_the_declared_set`.
- [x] `init_mongodb` binds per mode before `init_beanie`. Verified by `tests/unit/test_db.py::test_init_mongodb_creates_exactly_the_modes_classic_indexes[rag|graphrag]`.
- [x] `ensure_indexes` calls `create_index` once (`text_index`), and retirement is exact. Verified by `test_creates_only_the_text_index`, `test_drops_exactly_the_retired_names_of_a_legacy_collection[rag|graphrag]` and `test_a_collection_already_on_its_set_drops_nothing`. `grep -n "create_index(" …/rag/indexing.py` → `427:    await collection.create_index(` (1 line). `grep -rn "user_type_semantic_type\|user_canonical_name_index\|user_kind_embedding" apps/memory/src` → only `indexing.py:68-70` (inside `_RETIRED_INDEX_NAMES`).
- [x] Active-user parity. Verified by `test_users.py::TestActiveUserIndexParity`.
- [x] Local graphrag indexing run: 7 classic names + `vector_index`; 5 drop lines once, 0 on the re-run (see Evidence and Notes 1).
- [x] Local explains: `active_user` 1/1; `$graphLookup` on the `*_node` pair; reset 95 = 95 docs and resolution 3 = 3 docs on `user_kind_type_subtype`; backfill 8 → 95 docs (the rise the task's step 3 predicts, ADR-012 §4; the AC's general "unchanged" wording does not hold for backfill). Table below.
- [x] Local rag retirement on the leftover graphrag collection, and `run-pipelines-e2e` green in rag and graphrag with 0 drop lines on fresh collections.
- [x] Docstrings and comments updated.
- [x] format-check / lint-check / pre-commit / memory-tests green.
- [ ] [HUMAN] `db.knowledge_graph.drop()` on Atlas.

**Evidence**
```
$ grep -rn "_LEGACY_COMPOUND_INDEX_NAMES\|_CANONICAL_NAME_INDEX\|Compound indexes ensured" apps/memory/{src,tests,scripts} docs .agents   -> (none)
$ grep -rn "create_index\|init_beanie" apps/memory/{src,scripts,deploy}   -> db.py (init_beanie) + indexing.py:427 (the one text create_index) only
$ make memory-format-fix && make memory-lint-fix && make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests
2 files reformatted, 319 files left unchanged | All checks passed! | 321 files already formatted | All checks passed!
prettier Passed | ruff check Passed | ruff format Passed | biome check (harness) Passed
======================= 4531 passed in 61.09s (0:01:01) ========================

$ make env-status            -> Env target: local (.env); MONGO_HOST=localhost (every mongosh call below runs through a guard that refuses otherwise)
docker: tree-prefect-worker "Exited (0) 24 hours ago" before AND after; all runs served from THIS worktree
(apps/memory/.venv of building-agentic-systems-memory-indexes, `make memory-serve-workflows`)

# Step 1 — BEFORE (explain169.js, captured by the orchestrator; re-run by me before step 2: identical)
indexes: ["_id_","kind_1","user_kind_type","user_type_name","user_kind_type_subtype","user_type_semantic_type","text_index","user_kind_source_node","user_kind_target_node","user_kind_embedding","user_canonical_name_index"]
rows: 269 embedded: 95
active_user    IXSCAN:kind_1 keys=103 docs=103 n=1
backfill       IXSCAN:user_kind_embedding keys=9 docs=8 n=0
reset          IXSCAN:user_kind_type|IXSCAN:user_kind_type_subtype keys=96 docs=95 n=95
resolution     IXSCAN:user_kind_type keys=3 docs=3 n=3
graphLookup    seed=6abf89ef81b8cfb94d2032a2:person:self edges=5 index ops delta: user_kind_source_node+1, _id_+1, user_kind_target_node+1

# Step 2 — graphrag (YAML default), first real run on the 11-index collection
$ make memory-run-indexing-pipeline USER_ID=6abf89ef81b8cfb94d2032a2
indexing: user_id=6abf89ef81b8cfb94d2032a2 embedded=0 | Done. Flow completed successfully.
getIndexes(): ["_id_","user_type_name","user_kind_type_subtype","text_index","user_kind_source_node","user_kind_target_node","active_user"]
getSearchIndexes(): ["vector_index"]
(11 -> 7: exactly user_kind_type, kind_1, user_kind_embedding, user_type_semantic_type, user_canonical_name_index gone;
 active_user created by the flow's init_mongodb. Drop lines NOT visible in the serve log — see Notes 1.)

# Step 2 — two more runs on the state run 1 left (no fixture), after step 3
== control 1: serve with PREFECT_LOGGING_EXTRA_LOGGERS=tree -> exit 0, indexing: embedded=0; NO tree.* line in the serve log
   or in `prefect flow-run logs` (Prefect attaches handlers to `tree` but leaves its level at root WARNING): inconclusive
== control 2: serve with PREFECT_LOGGING_ROOT_LEVEL=INFO -> a genuine second run on run 1's result:
tree.memory.rag.indexing - Text index 'text_index' ensured on memory
tree.memory.rag.indexing - Vector search index 'vector_index' already up-to-date (dimensions=1024, filters=['kind', 'merged_into', 'subtype', 'type', 'user_id'])
drop lines: 0

# Step 3 — AFTER (same collection, same 269 rows / 95 embedded; run right after run 1, before the controls)
indexes: ["_id_","user_type_name","user_kind_type_subtype","text_index","user_kind_source_node","user_kind_target_node","active_user"]
rows: 269 embedded: 95
active_user    IXSCAN:active_user keys=1 docs=1 n=1
backfill       IXSCAN:user_kind_type_subtype|IXSCAN:user_kind_type_subtype keys=96 docs=95 n=0
reset          IXSCAN:user_kind_type_subtype|IXSCAN:user_kind_type_subtype keys=96 docs=95 n=95
resolution     IXSCAN:user_kind_type_subtype keys=3 docs=3 n=3
graphLookup    seed=6abf89ef81b8cfb94d2032a2:person:self edges=5 index ops delta: user_kind_source_node+1, user_kind_target_node+1, _id_+1

# Step 2 (log lines) — serve re-started with PREFECT_LOGGING_ROOT_LEVEL=INFO; legacy fixture re-created locally (Notes 1):
$ mongosh … legacy_fixture.js
  m.createIndex({kind:1})
  m.createIndex({user_id:1,kind:1,type:1},{name:"user_kind_type"})
  m.createIndex({user_id:1,type:1,semantic_type:1},{name:"user_type_semantic_type",partialFilterExpression:{semantic_type:{$type:"string"}}})
  m.createIndex({user_id:1,kind:1,embedding:1},{name:"user_kind_embedding"})
  m.createIndex({user_id:1,canonical_name:1},{name:"user_canonical_name_index",sparse:true})
-> 12 names (the 7 above + the 5)
== run A: make memory-run-indexing-pipeline   (serve-log slice for this run)
tree.memory.rag.indexing - Dropped legacy compound index 'user_kind_type' on memory
tree.memory.rag.indexing - Dropped legacy compound index 'kind_1' on memory
tree.memory.rag.indexing - Dropped legacy compound index 'user_kind_embedding' on memory
tree.memory.rag.indexing - Dropped legacy compound index 'user_type_semantic_type' on memory
tree.memory.rag.indexing - Dropped legacy compound index 'user_canonical_name_index' on memory
tree.memory.rag.indexing - Text index 'text_index' ensured on memory
tree.memory.rag.indexing - Vector search index 'vector_index' already up-to-date (dimensions=1024, filters=['kind', 'merged_into', 'subtype', 'type', 'user_id'])
drop lines: 5
getIndexes: ["_id_","user_type_name","user_kind_type_subtype","text_index","user_kind_source_node","user_kind_target_node","active_user"]
getSearchIndexes: ["vector_index"]
== run B: same target
tree.memory.rag.indexing - Text index 'text_index' ensured on memory
tree.memory.rag.indexing - Vector search index 'vector_index' already up-to-date (…)
drop lines: 0
getIndexes: (identical to run A)

# Step 4 — rag retirement on the leftover graphrag collection (serve re-started with TREE_MEMORY__MODE=rag, no drop)
after rag serve boot: ["_id_","user_type_name","user_kind_type_subtype","text_index","user_kind_source_node","user_kind_target_node","active_user"]
$ TREE_MEMORY__MODE=rag make memory-run-indexing-pipeline USER_ID=6abf89ef81b8cfb94d2032a2
tree.memory.rag.indexing - Dropped legacy compound index 'user_kind_source_node' on memory
tree.memory.rag.indexing - Dropped legacy compound index 'user_kind_target_node' on memory
tree.memory.rag.indexing - Text index 'text_index' ensured on memory
drop lines: 2
getIndexes: ["_id_","user_type_name","user_kind_type_subtype","text_index","active_user"]
getSearchIndexes: ["vector_index"]

# Step 5a — run-pipelines-e2e in rag
DROP #1 (guarded: env-status local, MONGO_HOST=localhost): rows before: 269 | db.getSiblingDB("tree").memory.drop() -> true
$ TREE_MEMORY__MODE=rag uv run python reseed_self.py   # re-fires the idempotent User.after_insert hook (person:self lives in `memory`)
mode: rag | getIndexes: ["_id_","user_kind_type_subtype","user_type_name","active_user"]   <- fresh boot, rag set, no kind_1
$ TREE_MEMORY__MODE=rag make memory-run-pipeline USER_ID=6abf89ef81b8cfb94d2032a2 SOURCE_FILE=<scratchpad>/169/light3.yaml
   # how-does-memory-for-ai-agents-work, context-engineering-2025s-1-skill, ai-agents-planning
extraction: shards=1 succeeded=1 failed=0 | indexing: embedded=1 | Done. Flow completed successfully.
serve slice: Text index 'text_index' ensured | Vector search index … (local mongot); treating it as ready | drop lines: 0
getIndexes: ["_id_","user_kind_type_subtype","user_type_name","active_user","text_index"] | getSearchIndexes: ["vector_index"]
rows: 84 edges: 0 embedded: 76
   node chunk child n=75 embedded=75 dim=1024 | node chunk parent n=5 embedded=0 | node document n=3 embedded=0 | node person individual n=1 embedded=1
$ TREE_MEMORY__MODE=rag make memory-query-graph QUERY="how does memory for ai agents work"
vector leg: 40 candidate(s), 29 kept at min_vector_score=0.70 (top=0.836)
Parent-document retrieval: 40 child hit(s) -> 5 parent(s), returning 5
[0.032] How Does Memory for AI Agents Work? (matched children: 20) | [0.032] … (4) | [0.027] Context Engineering: 2025's #1 Skill in AI (9)
[0.016] You're Not Building Agents … (2) | [0.014] You're Not Building Agents … (5)          (text only, no HTML — expected in rag)
getIndexes after query: ["_id_","user_kind_type_subtype","user_type_name","active_user","text_index"]

# Step 5b — run-pipelines-e2e in graphrag (final state)
DROP #2 (guarded): rows before: 84 | memory.drop() -> true ; serve re-started WITHOUT TREE_MEMORY__MODE
$ uv run python reseed_self.py -> mode: graphrag
getIndexes after graphrag boot: ["_id_","user_kind_type_subtype","user_type_name","active_user","user_kind_source_node","user_kind_target_node"]
$ make memory-run-pipeline USER_ID=6abf89ef81b8cfb94d2032a2 SOURCE_FILE=<scratchpad>/169/light3.yaml
extraction: shards=1 succeeded=1 failed=0 | indexing: embedded=1 | Done. Flow completed successfully.
serve slice: Text index 'text_index' ensured | Vector search index … treating it as ready | drop lines: 0
getIndexes: ["_id_","user_kind_type_subtype","user_type_name","active_user","user_kind_source_node","user_kind_target_node","text_index"] | getSearchIndexes: ["vector_index"]
rows: 268 edges: 162 embedded: 98
   edges: has 2, mentions 4, next 72, part_of 80, related_to 4
   chunk child 75 (75 x 1024) | chunk parent 5 | document 3 | event 3 | fact 4 | object 7 | organization 5 | person 2 | preference 2 (all entities embedded)
$ make memory-query-graph QUERY="how does memory for ai agents work"
Graph expansion: 4 seed(s) → 69 nodes, 68 edges (1 hops) -> wrote how-does-memory-for-ai-agents-work-20261002-111131.html (3 of 3 documents shown)
getIndexes after query: unchanged (7)
explain169.js on the final collection: active_user IXSCAN:active_user keys=1 docs=1 | backfill/reset IXSCAN:user_kind_type_subtype docs=98 | resolution user_kind_type_subtype docs=2 | graphLookup user_kind_source_node+1, user_kind_target_node+1
```

**Before / after (step 1 vs step 3, same 269-row collection)**

| Read | Before: index · keys / docs | After: index · keys / docs |
|---|---|---|
| `select_active_user_ids` | `kind_1` · 103 / 103 | `active_user` · 1 / 1 |
| backfill (`_backfill_filter`) | `user_kind_embedding` · 9 / 8 | `user_kind_type_subtype` (×2 `$or` branches) · 96 / 95 — rise expected (ADR-012 §4) |
| reset | `user_kind_type` + `user_kind_type_subtype` · 96 / 95 | `user_kind_type_subtype` ×2 · 96 / 95 (unchanged) |
| resolution fetch (`person`) | `user_kind_type` · 3 / 3 | `user_kind_type_subtype` · 3 / 3 (unchanged) |
| `expand_graph` `$graphLookup` ×2 | `user_kind_source_node` +1, `user_kind_target_node` +1 | same |

**Notes**
1. **Drop log lines.** In this setup the flow-run subprocess leaves the root logger at Prefect's default WARNING level, so `tree.*` INFO lines reach neither the serve terminal nor the Prefect API. This applies to `Text index … ensured` too. `PREFECT_LOGGING_EXTRA_LOGGERS=tree` attaches handlers but does not set a level, so it did not help either. The first real graphrag run therefore retired the 5 names silently. A second run with `PREFECT_LOGGING_EXTRA_LOGGERS=tree` also logged nothing. A third, with `PREFECT_LOGGING_ROOT_LEVEL=INFO`, ran on run 1's result and logged 0 drops plus the control line, which is the fixture-free "second run" evidence. Its evidence is the before/after `getIndexes()` diff (11 → 7, exactly the 5 names gone, `active_user` added). To capture the AC's lines, I re-served with `PREFECT_LOGGING_ROOT_LEVEL=INFO` (env only, no code change) and re-created the 5 retired indexes locally with their pre-ADR-012 specs, copied verbatim from the deleted code above. I then ran indexing twice (A: 5 drops; B: 0 drops). The positive control `Text index … ensured` appears in every slice, so every "0 drop lines" result is meaningful. All later serves used the same env var. The `run-pipelines-e2e` skill states that module-logger lines print in the serve terminal, which does not hold here. That is an adjacent docs/observability issue and is not fixed here.
2. **Drops.** Exactly two drops, each `db.getSiblingDB("tree").memory.drop()` (memory only, not `dropDatabase`) and each guarded. `documents` and `users` were untouched, and the `documents` index stayed `user_source_uri_unique {user_id, source_uri}` (main's shape) with no conflict on any boot. The 3 substack Documents became pending again because "ingested" = referenced in a `memory` row's `sources`. A memory-only drop deletes `person:self` (it lives in `memory`), and `make memory-signup` for an existing identifier does not recreate it. So after each drop I re-fired the idempotent `User.after_insert` hook from a scratch script that goes through `init_mongodb`, run with the matching `TREE_MEMORY__MODE`. That boot also shows the User Story "Operator boots any entry point" on a fresh collection: the mode's set is created and `kind_1` is absent, in both modes.
3. In `rag`, `embedded=1` is `person:self`. `_backfill_filter` is mode-blind and `person` is an embeddable type, so the self node gets a vector in `rag` too. This is pre-existing behaviour, out of scope, and only flagged here.
4. The graphrag e2e matches task 167's numbers for the same 3 articles: 69 nodes / 68 edges on the same query, and 75 children / 5 parents / 3 documents.
5. **ADR-012 authorship.** SWE-authored on the orchestrator's instruction, relaying the human's OQ1 = YES, so PA review applies. The glossary's **`memory` collection** entry ("one vector index + one text index") is still accurate. ADR-006 and the glossary are untouched.
6. **`tree-prefect-worker`.** It was already `Exited (0) 24 hours ago`, so it was neither stopped nor restarted. The serve process is stopped (no `tree.orchestrator` left), and the one HTML this task wrote (`apps/memory/.tree/graphs/how-does-memory-for-ai-agents-work-20261002-111131.html`) is removed.

### [Tester] 2026-10-02 — QA

**Test summary**
- Format / lint / pre-commit: PASS (`321 files already formatted`, `All checks passed!`, prettier/ruff/biome Passed)
- Unit tests: 4531 passed / 0 failed (`make memory-tests`, 57.69 s). No integration suite by design.
- Warnings: 0 pytest warnings (only the pre-existing Python 3.14 pydantic-v1 import UserWarning when invoking pytest directly).
- `make env-status` -> local, MONGO_HOST=localhost. Nothing prod touched. Real `tree.memory` left untouched: 268 rows, `["_id_","user_kind_type_subtype","user_type_name","active_user","user_kind_source_node","user_kind_target_node","text_index"]` + `vector_index`.

**E2E adversarial pass** (scratch DB `qa169_scratch`, script `scratchpad/169/qa_scratch.py`, dropped afterwards; `_ensure_vector_index` stubbed, mongot is not what changed)
- Happy path: real `init_mongodb` rag on a fresh DB -> `_id_, active_user, user_kind_type_subtype, user_type_name`. PASS.
- Break 1 (mode switch without drop, boot-time index conflict): boot rag -> boot graphrag -> boot rag on the same DB: no error 85/86; graphrag adds the `*_node` pair; rag boot over graphrag creates/drops nothing (lazy retirement as documented). PASS.
- Break 2 (retire + re-create cycle): `ensure_indexes` in rag drops exactly `user_kind_source_node`, `user_kind_target_node`; second call drops nothing; boot graphrag recreates the pair. Matches ADR-012 ("boot creates, indexing retires"). PASS.
- Break 3 (legacy DB with kind_1, user_kind_type, user_type_semantic_type, user_kind_embedding, user_canonical_name_index, canonical_name_index): boot graphrag over it -> no conflict (kind_1 not recreated by Beanie, none dropped); `ensure_indexes(graphrag)` -> 6 drop lines for exactly the retired names, result == the 7 classic graphrag names. PASS.
- Break 4 (`index_information` raises): `_drop_legacy_compound_indexes` returns without raising and without dropping. PASS (note below).
- Break 5 (one `drop_index` raises): it logs a warning and carries on; the other 10 retired names are still attempted. PASS.
- Break 6 (name both declared and retired, mutation): add `user_type_name` to `_RETIRED_INDEX_NAMES` -> `test_retired_names_never_overlap_the_declared_set[rag]` FAILS. PASS (guard works).
- Break 7 (mutation: remove the bind in `init_mongodb`): `test_db.py[rag]` FAILS. PASS. Mutation: `kind: Indexed(str)` -> `test_db.py[rag]` FAILS (kind_1 appears). PASS. Mutation: query predicate changed to `{"$ne": False}` -> `TestActiveUserIndexParity` FAILS. PASS. All sources restored byte-identical (cmp against backups).
- Break 8 (test_db leak): `test_db.py` alone (2 passed); `test_db.py` + `tests/unit/entities` (394 passed); `entities` + `test_db.py` + `memory/rag` (772 passed); `test_db.py` first followed by every unit sub-package (4329 passed). The graphrag session binding is restored. `pytest-randomly` is not installed, so no random order run.

**Acceptance criteria**
- [x] PASS — `memory_indexes` sets / `kind: str` / disjointness: `test_memory.py::TestMemoryIndexes` (7 tests), `test_indexing.py::TestIndexRetirement::test_retired_names_never_overlap_the_declared_set[rag|graphrag]`; mutation 6 proves it bites.
- [x] PASS — `init_mongodb` binds from `app_config.memory.mode` before `init_beanie`: `db.py:32`; `tests/unit/test_db.py` (2 tests) against a throwaway DB, restores the session DB; session starts with no 85/86; mutation 7 proves it bites.
- [x] PASS — `ensure_indexes`: `grep -n "create_index(" indexing.py` -> `427` only; `test_creates_only_the_text_index`; `test_drops_exactly_the_retired_names_of_a_legacy_collection[rag|graphrag]` (exact sets 7 / 5, `len(_LEGACY_SET) == 11`); `test_a_collection_already_on_its_set_drops_nothing[rag|graphrag]`; `grep -rn "user_type_semantic_type|user_canonical_name_index|user_kind_embedding" src` -> `indexing.py:68-70` only.
- [x] PASS — parity test `TestActiveUserIndexParity`; shared `ACTIVE_USER_FILTER` (`entities/memory.py`, `users.py:173`); mutation proves it bites.
- [x] PASS — graphrag indexing run: independently reproduced the retirement on a legacy scratch DB (Break 3: 6 drop lines incl. the 5; second run 0 drops, Break 2/step 5). SWE's serve-shell lines (5 drops, then 0) are in the SWE log; the `PREFECT_LOGGING_ROOT_LEVEL=INFO` caveat is a pre-existing observability gap, not a defect of this change.
- [x] PASS — explains: I re-ran `explain169.js` on the live collection: `active_user` IXSCAN keys=1 docs=1; backfill/reset `user_kind_type_subtype` docs=98 (= embedded count, row set has since changed 269->268 after the SWE's rebuild); resolution `user_kind_type_subtype` 2/2; graphLookup uses `user_kind_source_node` +1 / `user_kind_target_node` +1. Before/after numbers in the SWE log; backfill 8 -> 95 is the expected, ADR-recorded rise, so the AC's "unchanged" wording is loose for backfill only.
- [x] PASS — rag retirement on a leftover graphrag collection: reproduced in Break 2 (drops exactly the `*_node` pair -> 5 classic names); rag/graphrag e2e results from the SWE log accepted (final live state is a consistent graphrag build, 268 rows, 7 indexes + `vector_index`).
- [x] PASS — docstrings/comments: `entities/memory.py` (ownership comments rewritten), `ensure_indexes`, `_drop_legacy_compound_indexes`, `_RETIRED_INDEX_NAMES` comment, `memory_indexing` docstring (`pipeline.py:2302`). No stale references to retired names outside the retired list / ADR / tests / notes (grep).
- [x] PASS — format-check / lint-check / pre-commit / memory-tests green (above).
- [ ] [HUMAN] `db.knowledge_graph.drop()` on Atlas — awaiting human verification.

**ADR-012 accuracy vs code**: table == `memory_indexes`; retired lists == `retired_index_names`; `init_mongodb` binding, ensure_indexes call sites (indexing phase always, MCP unless `MCP_SKIP_INDEX_BOOTSTRAP`) and `allow_index_dropping` unset all match; the 11-index history and the 5 / 7 per-write counts (including `_id_` + `text_index`) add up. Format matches ADR-010. There is no ADR index file to update.

**Other issues found (non-blocking)**
- `_drop_legacy_compound_indexes` logs a `debug` line when `index_information` raises, so a failed retirement is invisible at default log level (pre-existing behaviour, but retirement is now the only place stale indexes get cleaned). Consider `warning`.
- The log message still says "Dropped legacy compound index" although the list now includes non-legacy trims and `kind_1` (single-key). Cosmetic; the task's own AC quotes this text so leave it.
- `test_db.py` teardown keeps the `restored` client open (`del restored`): a leaked connection per session, harmless.
- `_backfill_filter` is mode-blind, so `person:self` gets embedded in rag (SWE note 3, pre-existing, out of scope).

**VERDICT: PASS**
