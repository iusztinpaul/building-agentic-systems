---
id: 169-mode-aware-memory-indexes
status: pending
feature: memory-indexes
---

# Trim the `memory` collection indexes and make the set mode-aware

Tags: `memory`, `mongodb`, `refactor`, `indexes`
Depends on: None (code-wise). Ships on its OWN branch `refactor/memory-indexes` off `main`, routed
through `/squid-refactor` — NOT part of `dynamic-graph-viz`, never stacked on `feat/dynamic-graph-viz`
or `feat/document-unique-key`. Number 169 is reserved across branches (162–168 live on
`feat/dynamic-graph-viz`).
Blocks: —

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

- [ ] `memory_indexes("rag")` and `memory_indexes("graphrag")` return exactly the table's sets (names,
      keys, `active_user` partial filter), `MemoryEntry.kind` is a plain `str`, and the sets are disjoint
      from `retired_index_names(mode)` for both modes (unit tests).
- [ ] `init_mongodb` binds `MemoryEntry.Settings.indexes` from `app_config.memory.mode` before
      `init_beanie`; `tests/unit/test_db.py` proves it for both modes against a real throwaway database and
      restores the session binding (unit tests; `make memory-tests` green, no error 85/86 at session start).
- [ ] `ensure_indexes` awaits `create_index` exactly once (`text_index`), still calls the retirement drop,
      and the retirement drops exactly the 5 always-retired names (+ the 2 `*_node` names in `rag`) and never
      a kept name (unit tests). `grep -n "create_index(" apps/memory/src/tree/memory/rag/indexing.py` → 1
      line; `grep -rn "user_type_semantic_type\|user_canonical_name_index\|user_kind_embedding" apps/memory/src`
      → only inside the retired-names list.
- [ ] `select_active_user_ids`'s `properties.is_active_user` predicate and the `active_user` partial filter
      are pinned equal (unit test).
- [ ] Local graphrag indexing run: `getIndexes()` == the 7 classic graphrag names + `vector_index`; the 5
      drop log lines appear once and not on the second run (output in `## Log`).
- [ ] Local explains after the change: `select_active_user_ids` → `active_user` 1 key / 1 doc;
      `expand_graph` aggregate → `user_kind_source_node` / `user_kind_target_node`; backfill, reset and
      resolution reads → `user_kind_type_subtype` with `totalDocsExamined` unchanged vs the before capture
      (before/after numbers in `## Log`).
- [ ] Local rag indexing run on the leftover graphrag collection retires the `*_node` pair → `getIndexes()`
      == the 5 classic rag names (+ `vector_index`); `run-pipelines-e2e` green in `rag` and `graphrag` with
      zero drop lines on fresh collections (output in `## Log`) — only if OQ2 is approved.
- [ ] Docstrings/comments updated: `entities/memory.py` (the two stale comments), `ensure_indexes`,
      `_drop_legacy_compound_indexes` + its constant comment, the `memory_indexing` docstring.
- [ ] `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green.
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

Blocked by: OQ1 + OQ2 below (human decisions) before implementation starts.

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
