---
id: 185-graph-file-odm-and-ttl
status: pending
feature: request-scoped-users
---

# `GraphFile` ODM in `graph_files` with a TTL on `created_at` (`mcp.graph_file_ttl_seconds`, 300), the compound `{name, user_id}` index, and a boot-time `collMod` reconcile that never crashes boot

Tags: `data`, `entities`, `config`, `mcp`
Depends on: —
Blocks: 187 (the resource and the tool write / read this model)
Implements: ADR-014 §4 (the **Graph file**)

## Problem

A rendered graph is written to the server's `/tmp/.tree/graphs/` during `tools/call` and read back during a
LATER `resources/read`. Horizon gives no instance affinity and no durable filesystem ("Do not rely on files
written during one request being available to another request"), so the read lands on an instance that
never saw the file → `FileNotFoundError` → "Error reading resource". The bytes (~2.4 MB gzip for the
worst-case map, task 179–181) fit a plain MongoDB document; MongoDB is already the system of record. This
task lands the store only; task 187 wires the tool and the resource to it.

## Scope

**Human decision (final):** a Beanie `GraphFile` ODM in `entities/`, indexes declared ON THE MODEL so
`init_beanie` creates them at every boot (Horizon included, where `MCP_SKIP_INDEX_BOOTSTRAP` skips
`ensure_indexes`); TTL from a new YAML key `mcp.graph_file_ttl_seconds: 300` (env hatch
`TREE_MCP__GRAPH_FILE_TTL_SECONDS`); at boot compare the live `expireAfterSeconds` to the config and
`collMod` when different — never crash boot.

1. **Entity** `apps/memory/src/tree/entities/graph_files.py` (the `clusters.py` shape):
   `GRAPH_FILES_COLLECTION = "graph_files"` (the one spelling), `GRAPH_FILE_TTL_INDEX = "created_at_ttl"`,
   and `class GraphFile(BeanieDocument)` with `user_id: PydanticObjectId` (tenant scope),
   `name: str` (the **Graph download** URI tail, `<token>.html.gz` — task 187 mints it with
   `secrets.token_urlsafe(16)`; unique by construction, 128 bits of randomness), `html_gz: bytes` (the gzip
   of the self-contained HTML; BSON binary), `created_at: datetime` (default `datetime.now(UTC)`; a naive
   value is rejected by a validator — AGENTS.md: no naive datetimes). `Settings.name =
   GRAPH_FILES_COLLECTION`; `Settings.indexes = [IndexModel([("created_at", 1)],
   expireAfterSeconds=app_config.mcp.graph_file_ttl_seconds, name=GRAPH_FILE_TTL_INDEX),
   IndexModel([("name", 1), ("user_id", 1)], unique=True, name="name_user_id_unique")]`. Exactly these two:
   the TTL and the one index the read `{name, user_id}` uses; no separate index on `name` (a second
   mechanism for a property the token already guarantees). The TTL value is read from config at import,
   like `users.py` reads `app_config`.
2. **Config:** `default.yaml` `mcp.graph_file_ttl_seconds: 300` with a comment ("how long a rendered graph
   download (`graphs://…html.gz`) stays readable; Mongo's TTL monitor runs about every 60 s, so a file can
   outlive this by a minute; override `TREE_MCP__GRAPH_FILE_TTL_SECONDS=…`; changing it is applied at the
   next boot by `collMod`, no index drop"); `MCPConfig.graph_file_ttl_seconds: int = 300` in
   `app_config.py` (the existing `_apply_env_overrides` hatch covers it — no new mechanism).
3. **Registration + reconcile** in `apps/memory/src/tree/db.py`: `GraphFile` joins `ALL_DOCUMENT_MODELS`;
   a new `async def reconcile_graph_file_ttl(db: AsyncDatabase, ttl_seconds: int) -> None` runs in
   `init_mongodb` BEFORE `init_beanie`. Why before (verified in Beanie 2.0.1 `init_indexes`): Beanie passes
   EVERY declared index to `create_indexes`, not the diff, so an existing `created_at_ttl` with a different
   `expireAfterSeconds` makes Mongo answer IndexOptionsConflict (code 85) and boot crashes. The reconcile
   reads `index_information()` of `graph_files`; when `created_at_ttl` exists with a different
   `expireAfterSeconds`, it runs `db.command({"collMod": GRAPH_FILES_COLLECTION, "index": {"name":
   GRAPH_FILE_TTL_INDEX, "expireAfterSeconds": ttl_seconds}})` (MongoDB `collMod` reference: `index:
   {name | keyPattern, expireAfterSeconds}`; answers `expireAfterSeconds_old/_new`) and logs ONE INFO line
   naming old and new. Missing collection or missing index → nothing to do (Beanie creates it next).
   ANY exception → ONE WARNING ("graph_files TTL reconcile skipped: <cause>") and boot continues — a wrong
   TTL is a nuisance, a dead MCP server is not. `init_mongodb` is the one place every entry point (MCP boot,
   pipelines, scripts) runs, so a TTL change in any deployment's env heals the collection rather than
   crashing it.
4. **Tests** (`/squid-testing-python`): `tests/unit/entities/test_graph_files.py` — the model declares
   exactly the two indexes with those names/options, the TTL equals `app_config.mcp.graph_file_ttl_seconds`,
   `created_at` defaults to a UTC-aware now, a naive `created_at` raises; `tests/unit/test_db.py` —
   `reconcile_graph_file_ttl` with a fake db: different live value → the `collMod` command document above is
   awaited once; equal value → no command; no index / no collection → no command; `index_information`
   raising `PyMongoError` → WARNING logged, no raise; `init_mongodb` awaits the reconcile BEFORE
   `init_beanie` (mock both; assert order) and registers `GraphFile`; `tests/unit/config/test_app_config.py`
   — `TREE_MCP__GRAPH_FILE_TTL_SECONDS=120` overrides the YAML value.
5. **Live verification (LOCAL env, `make env-status` → local):** `make memory-serve-mcp
   TRANSPORT=streamable-http`, then `mongosh` `db.graph_files.getIndexes()` shows `created_at_ttl`
   (`expireAfterSeconds: 300`) and `name_user_id_unique`; restart with
   `TREE_MCP__GRAPH_FILE_TTL_SECONDS=120` → boot survives (the old code path would have died on code 85),
   log line names 300 → 120, `getIndexes()` shows 120; restart without the override → 300 again. Evidence
   in the Log.

## Acceptance criteria

- [ ] `GraphFile` (`tree.entities.graph_files`) has fields `user_id`, `name`, `html_gz: bytes`,
      `created_at` (UTC-aware default; a naive value raises) and declares exactly two indexes:
      `created_at_ttl` (TTL = `app_config.mcp.graph_file_ttl_seconds`) and the unique compound
      `name_user_id_unique` on `(name, user_id)`.
- [ ] `default.yaml` `mcp.graph_file_ttl_seconds: 300` (commented) and `MCPConfig.graph_file_ttl_seconds`
      exist; `TREE_MCP__GRAPH_FILE_TTL_SECONDS=120` overrides it (test).
- [ ] `GraphFile` is in `ALL_DOCUMENT_MODELS`; `init_mongodb` awaits `reconcile_graph_file_ttl` BEFORE
      `init_beanie` (test asserts the order).
- [ ] `reconcile_graph_file_ttl` runs `collMod` with `{"index": {"name": "created_at_ttl",
      "expireAfterSeconds": <config>}}` only when the live value differs; no-ops when the index or collection
      is absent; swallows any exception into one WARNING (tests for all four).
- [ ] Live: a second boot with a changed `TREE_MCP__GRAPH_FILE_TTL_SECONDS` survives and
      `db.graph_files.getIndexes()` shows the new value; evidence in the Log.
- [ ] `make memory-format-check`, `make memory-lint-check`, `make pre-commit`, `make memory-tests` green.

## User Stories

### Story: The operator shortens the download lifetime without touching Mongo
1. The operator sets `TREE_MCP__GRAPH_FILE_TTL_SECONDS=120` in the server's environment and restarts it.
2. The boot log shows `graph_files created_at_ttl: expireAfterSeconds 300 → 120`; the server comes up.
3. `mongosh` `db.graph_files.getIndexes()` shows `expireAfterSeconds: 120`.

### Story: A fresh Horizon deploy creates the collection's indexes without `ensure_indexes`
1. Horizon boots the server with `MCP_SKIP_INDEX_BOOTSTRAP=true`.
2. `init_beanie` creates `created_at_ttl` and `name_user_id_unique` on `graph_files` (no `graph_files`
   row exists yet).
3. The first rendered graph (task 187) is inserted and expires about 5 minutes later with no further
   operator action.

### Story: A mis-typed TTL cannot take the server down
1. An operator's env sets the TTL to a value Mongo rejects for `collMod`.
2. Boot logs ONE WARNING naming the cause and continues; tools work; the old TTL stays in force.

---

Blocked by: (none)

## Log

### [PA] 2026-10-05 16:40 — Grooming

**Summary**
The store half of the stateless **Graph download**: a `GraphFile` row per rendered graph, a TTL index whose
value is a YAML knob, and a `collMod` reconcile at boot that keeps a changed knob from crashing
`init_beanie`.

**Key decisions**
- Indexes ON the model so Horizon (which skips `ensure_indexes`) still gets them at every boot.
- The reconcile runs in `init_mongodb` BEFORE `init_beanie`: Beanie re-submits every declared index, and
  Mongo rejects a same-name index with a different `expireAfterSeconds` (code 85).
- One compound unique index serves the read; no separate index on `name` — the token is the uniqueness.
- A plain document, not GridFS: the worst-case gzip (~2.4 MB) is far under the 16 MB BSON limit.

**Dependencies**
- None.

**User stories**
- 3 stories: TTL change via env, fresh Horizon boot, a bad TTL stays harmless.

Ready for implementation.
