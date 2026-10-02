# Plan: trim the `memory` collection indexes and make them mode-aware

Status: **ready to bake into `tasks/<NNN>-*.md`.** Decisions rest on a code audit + local `explain`;
prod cannot supply usage evidence (see "Prod state").

## Why

`memory` carries 11 classic indexes. Several serve no production query, one (`user_kind_embedding`)
would add ~16 KB per embedded row on prod's M0 (≈ 230 MB at ≥14k rows; 512 MB cap, data + indexes), and `rag` mode carries graph-only
indexes it never uses. Every index costs write amplification on every upsert.

## Prod state (Atlas, via MongoDB MCP, 2026-10-02)

- Cluster `tree`: **M0 (FREE)**, MongoDB 8.0.32, GCP Western Europe.
- `tree.memory` is **empty** (the memory pipeline has never run on prod) and carries only the 6
  Beanie-declared indexes. `tree.documents` holds 14,439 raw documents waiting for that first run.
- Performance Advisor returns nothing (not supported on M0) and `$indexStats` is denied to the MCP
  temporary user. → No prod usage evidence exists or can exist before the first run. Do not wait for it.
- Legacy `tree.knowledge_graph`: 1 row (a July `person:self`) + 6 indexes. Pre-rename leftover.

## Local evidence (269 rows, 97 embedded; `$indexStats` + `explain("executionStats")`)

| Index | Keys / options | Local ops | Serves (production code) | Decision |
|---|---|---|---|---|
| `_id_` | `_id` | 4271 | everything | keep |
| `user_kind_type_subtype` | `user_id, kind, type, subtype` | 2 | every kind/type read (plans identical to `user_kind_type`) | keep |
| `user_kind_type` | `user_id, kind, type` | 371 | same reads — pure prefix of the above | **drop** |
| `user_kind_source_node` / `user_kind_target_node` | `user_id, kind, *_node_id` | 771 / 867 | `$graphLookup` (`memory/graph/retrieval.py:97-117`): 1 key vs 164 without | keep (`graphrag`) |
| `text_index` | `$text` on `name`, `properties.content`, `properties.aliases` | 239 | lexical search | keep |
| `vector_index` (mongot) | `embedding` | n/a | vector search | keep |
| `kind_1` | `kind` | 8 | **`select_active_user_ids`** (`entities/users.py:166`): cross-tenant, no `user_id`; fan-out for scheduled dream (`memory/graph/consolidation/dream.py:1021`) + data pipeline (`offline_pipeline.py:464`). 105 keys / 105 docs for 1 result | **replace** (1) |
| `user_type_name` | `user_id, type, name` | 2 | NL `query_memory` — its prompt teaches `name` as a filter field (`memory/graph/nl_query.py:119`), and only `user_id` is injected, so `{type, name}` lands here exactly | keep (32 KB) |
| `user_kind_embedding` | `user_id, kind, embedding` — **multikey** over 1024 floats | 0 | embedding backfill (`memory/rag/indexing.py:143`) once per indexing run | **drop** (2) |
| `user_type_semantic_type` | partial `semantic_type: string` | 0 | nothing (cited `find_edges(semantic_type=…)` doesn't exist; not in NL prompt) | **drop** |
| `user_canonical_name_index` | `user_id, canonical_name`, `sparse` (no-op on a compound with `user_id`) | 2 | nothing — resolution matches names in Python over a `{user_id, kind, type}` fetch (`pipeline.py:1045`); not in NL prompt | **drop** |

`KGQuery.find_nodes(name=)` / `find_edges` filter on `name` but have no production callers (tests only),
which explains the 2 local ops on `user_type_name` / `user_canonical_name_index`.

1. **`kind_1` → partial `active_user`.** Dropping it turns every scheduled fan-out into a
   cross-tenant COLLSCAN. Replace with `{"properties.is_active_user": 1}`,
   `partialFilterExpression: {"properties.is_active_user": true}`: one entry per user; the query's
   `$eq: true` satisfies the partial predicate and FETCH filters the rest (`kind/type/name` add no
   selectivity — every entry is `node/person/self`). `kind: Indexed(str)` → `kind: str`
   (`entities/memory.py:273`) — required, or Beanie recreates `kind_1` on every boot.
2. **`user_kind_embedding` must go before the first prod memory run.** Local measurements: ~16 KB of
   index per embedded row (1.57 MB / 97) on top of ~14 KB of BSON per row (`$bsonSize`). Prod
   `documents` average ~200 chars of `content`, so ≥ ~1 child chunk each → ≥ ~14k embedded rows before
   entity nodes: ≈ 200 MB of data alone, ≈ 430 MB with this index. M0 caps logical data + indexes at
   512 MB, so dropping the index is necessary but not sufficient — **flag M0 capacity separately**
   before the first prod run. Cost of the drop: today the backfill wins on this index (9 keys / 8 docs);
   after it, the backfill fetches every embeddable node of the user via `user_kind_type_subtype` once
   per indexing run (fine at personal scale). Upgrade trigger (record, don't build): a partial index on
   a scalar `embedding_pending: true` marker — never a multikey on the vector.

## Mode-aware set

Mode is one global setting per deployment, and switching modes already requires dropping `memory`
(ADR-006), so a per-mode set never meets the other mode's rows.

| Index | `rag` | `graphrag` |
|---|---|---|
| `_id_`, `user_kind_type_subtype`, `text_index`, `vector_index`, `active_user`, `user_type_name` | ✓ | ✓ |
| `user_kind_source_node`, `user_kind_target_node` | — | ✓ |

11 → 6 (`rag`) / 8 (`graphrag`).

## Implementation

- `config/app_config.py:653`: extract the inline `Literal["rag", "graphrag"]` into a `MemoryMode`
  alias (cf. `ModelKind` at :657).
- `entities/memory.py`: `memory_indexes(mode: MemoryMode) -> list[IndexModel]` — base set + graph
  set for `graphrag`.
- `db.py::init_mongodb`: set `MemoryEntry.Settings.indexes = memory_indexes(app_config.memory.mode)`
  right before `init_beanie` (Beanie reads it at init). Every entry point goes through `init_mongodb`.
- `rag/indexing.py::ensure_indexes`: keep only what Beanie can't express — `$text` + mongot vector.
  Remove every compound `create_index` (ends the double declaration of `user_type_semantic_type`).
- **Retire indexes in code, not by hand.** `_drop_legacy_compound_indexes` (`rag/indexing.py:458`)
  already drops known names idempotently on every `ensure_indexes` call (`pipeline.py:2270`,
  `mcp/server.py:205`). Make its list mode-aware: always `user_kind_type`, `user_kind_embedding`,
  `kind_1`, `user_type_semantic_type`, `user_canonical_name_index`; plus the two `*_node` indexes in
  `rag`. Keep the call when slimming `ensure_indexes`. No operator step, no migration script.
  Update the `pipeline.py:2302` docstring ("re-asserts the global compound indexes") to match.
- Retirement is **lazy**: `init_mongodb` (every entry point) creates, but only `ensure_indexes`
  retires — the memory pipeline (always) and MCP start (unless `MCP_SKIP_INDEX_BOOTSTRAP=true`).
  Dream, the data pipeline, `cli.py` and `scripts/*` never retire. Fine: a stale index only costs
  writes until the next memory run.
- `init_beanie` never drops indexes (`allow_index_dropping` stays off).

## Tests (`/squid-testing-python`)

- `memory_indexes("rag")` / `("graphrag")` == the table above, through the real `init_mongodb` path
  (patch `app_config.memory.mode`), not only the pure function.
- **No name is both declared and retired** for either mode — otherwise every boot drops and
  recreates it. Load-bearing: Beanie 2.0.1 compares keys + options + name and calls `create_indexes`
  on every boot, so any same-keys/different-name drift fails every entry point (error 85/86).
- `ensure_indexes` issues no compound `create_index` and still calls the retirement drop.
- `active_user` declared partial on `properties.is_active_user: true`.
- Replace/update the tests that pin today's set: `test_user_kind_type_index_declared`
  (`tests/unit/entities/test_memory.py:132`), `test_user_type_semantic_type_index_declared` (`:990`),
  the `user_kind_type_subtype` declaration test, `test_creates_compound_indexes`
  (`tests/unit/memory/rag/test_indexing.py:160-191`), `test_canonical_name_index_created` (`:242-265`).

## Verification (local only)

- Run the **memory pipeline** in each mode against the existing local DB (booting alone only
  creates; retirement needs `ensure_indexes`); `getIndexes()` then matches the table — no rebuild.
- `explain("executionStats")`: `select_active_user_ids` → `active_user`, 1 key / 1 doc (today
  105 / 105); real `expand_graph` aggregate → `user_kind_*_node`; backfill, reset, dream, kind+type
  reads → `user_kind_type_subtype` with the same docs examined as today.
- `make memory-tests` green; `run-pipelines-e2e` in `rag` and `graphrag`.

## Operator step (prod, one-off, unrelated to the code)

`db.knowledge_graph.drop()` — legacy collection, 1 row, 6 indexes.

## Scope

- Separate branch off `main`; route `/squid-refactor`.
- Follow-ups (separate tasks): **M0 capacity before the first prod memory run** (≈ 200 MB of
  embedded rows even without `user_kind_embedding`; 512 MB cap — measure, then upgrade tier or cap
  ingestion); edges carry every document of their extraction shard in `sources`
  (`pipeline.py:1452`); dangling `related_to` edges from a `paul iusztin` vs `paul-iusztin` id mismatch.
