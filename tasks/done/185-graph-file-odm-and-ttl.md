---
id: 185-graph-file-odm-and-ttl
status: done
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
   a new `async def reconcile_graph_file_ttl(db: AsyncDatabase, ttl_seconds: int) -> int` runs in
   `init_mongodb` BEFORE `init_beanie` and returns the TTL now in force: the configured one, or the LIVE
   one when `collMod` failed. `init_mongodb` then rebinds `GraphFile.Settings.indexes =
   graph_file_indexes(<that ttl>)` (the `MemoryEntry` rebinding pattern), so a failed `collMod` keeps the
   old TTL instead of crashing `init_beanie` on code 85 (amended after implementation; see the SWE log). Why before (verified in Beanie 2.0.1 `init_indexes`): Beanie passes
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

- [x] `GraphFile` (`tree.entities.graph_files`) has fields `user_id`, `name`, `html_gz: bytes`,
      `created_at` (UTC-aware default; a naive value raises) and declares exactly two indexes:
      `created_at_ttl` (TTL = `app_config.mcp.graph_file_ttl_seconds`) and the unique compound
      `name_user_id_unique` on `(name, user_id)`.
- [x] `default.yaml` `mcp.graph_file_ttl_seconds: 300` (commented) and `MCPConfig.graph_file_ttl_seconds`
      exist; `TREE_MCP__GRAPH_FILE_TTL_SECONDS=120` overrides it (test).
- [x] `GraphFile` is in `ALL_DOCUMENT_MODELS`; `init_mongodb` awaits `reconcile_graph_file_ttl` BEFORE
      `init_beanie` (test asserts the order).
- [x] `reconcile_graph_file_ttl` runs `collMod` with `{"index": {"name": "created_at_ttl",
      "expireAfterSeconds": <config>}}` only when the live value differs; no-ops when the index or collection
      is absent; swallows any exception into one WARNING (tests for all four).
- [x] Live: a second boot with a changed `TREE_MCP__GRAPH_FILE_TTL_SECONDS` survives and
      `db.graph_files.getIndexes()` shows the new value; evidence in the Log.
- [x] `make memory-format-check`, `make memory-lint-check`, `make pre-commit`, `make memory-tests` green.

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

### [SWE] 2026-10-05 20:13 — Implementation

**Files modified**
- `apps/memory/src/tree/entities/graph_files.py` — NEW: `GRAPH_FILES_COLLECTION`, `GRAPH_FILE_TTL_INDEX`, `graph_file_indexes(ttl_seconds)` (the two `IndexModel`s), `GraphFile` (`user_id`, `name`, `html_gz: bytes`, `created_at` with a UTC default and a naive-rejecting validator); `Settings.indexes = graph_file_indexes(app_config.mcp.graph_file_ttl_seconds)` at import.
- `apps/memory/src/tree/config/app_config.py` — `MCPConfig.graph_file_ttl_seconds: int = 300` (no bounds, on purpose: see Notes).
- `apps/memory/src/tree/config/default.yaml` — `mcp.graph_file_ttl_seconds: 300` + the comment from the spec.
- `apps/memory/src/tree/db.py` — `GraphFile` in `ALL_DOCUMENT_MODELS`; `reconcile_graph_file_ttl(db, ttl_seconds) -> int`; `init_mongodb` awaits it BEFORE `init_beanie` and rebinds `GraphFile.Settings.indexes = graph_file_indexes(<effective ttl>)` (the same pattern as the `MemoryEntry` mode rebinding one line above).
- `apps/memory/tests/unit/entities/test_graph_files.py` — NEW, 15 tests: shape, required fields, descriptions, UTC default, naive raises, exactly-two indexes + names/keys/options, TTL == config, builder, registration, live insert round-trip.
- `apps/memory/tests/unit/test_db.py` — +10 tests: reconcile with a fake db (differs → one `collMod` doc + INFO `300 → 120`; equal → no command; no TTL index → no command; missing collection → no command; `PyMongoError` in `index_information` → ONE WARNING, no raise; rejected `collMod` → ONE WARNING, returns the LIVE ttl); `GraphFile` registered; `init_mongodb` order (reconcile before `init_beanie`, configured TTL passed, `GraphFile` in models); failed reconcile → Beanie declares the live TTL; LIVE regression against local Mongo (a `created_at_ttl` at 300 + config 120 → `init_mongodb` survives, live is 120).
- `apps/memory/tests/unit/config/test_app_config.py` — +2 tests: typed default and `default.yaml` agree on 300; `TREE_MCP__GRAPH_FILE_TTL_SECONDS=120` overrides YAML.

**Tests**
- Unit: 4921 passing, 0 failing, 0 warnings (`make memory-tests`, env target local).
- Regression proof: with the reconcile call replaced by the raw config value, the live test fails with `OperationFailure ... 'code': 85, 'codeName': 'IndexOptionsConflict'`; restored afterwards.
- Integration: N/A (the project has no integration suite).

**Acceptance criteria**
- [x] `GraphFile` fields + exactly two indexes: `tests/unit/entities/test_graph_files.py::TestGraphFileShape`, `::TestCreatedAt`, `::TestGraphFileIndexes`
- [x] YAML key + `MCPConfig` + env override: `tests/unit/config/test_app_config.py::TestGraphFileTtlConfig`
- [x] Registered, reconcile before `init_beanie`: `tests/unit/test_db.py::TestInitMongodbGraphFiles`
- [x] `collMod` only on a difference, no-op on missing index/collection, exceptions become one WARNING: `tests/unit/test_db.py::TestReconcileGraphFileTtl`
- [x] Live second boot with a changed TTL: evidence below
- [x] format-check, lint-check, pre-commit, memory-tests green

**Evidence**
```
$ make env-status
Env target: local (.env)

# Boot 1: fresh local `tree` db (no graph_files collection before)
$ FASTMCP_PORT=8765 make memory-serve-mcp TRANSPORT=streamable-http
INFO:     Uvicorn running on http://127.0.0.1:8765
$ mongosh ... --eval 'printjson(db.graph_files.getIndexes())'
  { key: { _id: 1 }, name: '_id_' },
  { key: { created_at: 1 }, name: 'created_at_ttl', expireAfterSeconds: 300 },
  { key: { name: 1, user_id: 1 }, name: 'name_user_id_unique', unique: true }

# Boot 2: TREE_MCP__GRAPH_FILE_TTL_SECONDS=120
graph_files created_at_ttl: expireAfterSeconds 300 → 120
INFO:     Uvicorn running on http://127.0.0.1:8765
$ mongosh ... getIndexes()  ->  created_at_ttl expireAfterSeconds: 120, name_user_id_unique unique: true

# Boot 3 (Story 3): TREE_MCP__GRAPH_FILE_TTL_SECONDS=-5
graph_files TTL reconcile skipped: TTL index 'expireAfterSeconds' option cannot be less than 0, full error: {... 'code': 72, 'codeName': 'InvalidOptions' ...}
INFO:     Uvicorn running on http://127.0.0.1:8765
$ mongosh ... getIndexes()  ->  created_at_ttl expireAfterSeconds: 120   (old TTL kept)

# Boot 4: no override
graph_files created_at_ttl: expireAfterSeconds 120 → 300
INFO:     Uvicorn running on http://127.0.0.1:8765
$ mongosh ... getIndexes().map(i => [i.name, i.expireAfterSeconds, i.unique])
  ['_id_', undefined, undefined], ['created_at_ttl', 300, undefined], ['name_user_id_unique', undefined, true]

$ make memory-format-check && make memory-lint-check && make pre-commit
335 files already formatted / All checks passed! / ruff check Passed, ruff format Passed, prettier Passed, biome Passed
$ make memory-tests
============================ 4921 passed in 58.93s =============================
```

**Notes**
- **Deviation from the spec: the signature is `-> int`, not `-> None`.** I confirmed in Beanie 2.0.1's `init_indexes` that every declared index goes to `create_indexes`. So when `collMod` fails (bad value, missing privilege), declaring the configured TTL would still crash `init_beanie` on code 85. Story 3 ("boot continues; the old TTL stays in force") would then not hold. The reconcile therefore returns the TTL now in force: the config value normally, or the LIVE value after a failed `collMod`. `init_mongodb` declares that value. On the happy path the rebinding produces the same indexes as the import-time declaration. Boot 3 above shows this working live.
- **No `ge`/`le` on `MCPConfig.graph_file_ttl_seconds`.** If Pydantic rejected the value, boot would crash at config load, which Story 3 forbids. Leftover gap: a bad value on a FRESH collection (no live TTL to fall back to) still fails at `init_beanie`'s `createIndexes`, because nothing older exists to keep. I checked this on local Mongo: `createIndex({created_at:1},{expireAfterSeconds:-5})` returns `CannotCreateIndex ... cannot be less than 0`.
- The `-5` boot logged the WARNING twice. `serve_mcp.py` calls `init_mongodb` once in `_resolve_and_pin`, and the FastMCP lifespan calls it again. Each call logs one warning. This double init already existed and is out of scope here.
- Port 8000 is held by an unrelated container (`kitaru-local-server-1`), so the live boots used `FASTMCP_PORT=8765`.
- `tree.entities.__init__` does not re-export `GraphFile`. Sessions and extraction_audit aren't re-exported either, and every caller imports from the module.

### [Tester] 2026-10-05 21:00 — QA

**Test summary**
- Format / lint / pre-commit: PASS (`make memory-format-check`, `memory-lint-check`, `pre-commit` all green; env target local)
- Unit tests: 4921 passed / 0 failed (`make memory-tests`); no integration suite in this project
- Warnings: 0

**E2E adversarial pass** (independent script against local Mongo, scratch db `qa185_scratch`, dropped afterwards; calls the real `init_mongodb`)
- Happy path: fresh db -> `init_mongodb` -> indexes `created_at_ttl` (300), `name_user_id_unique` (unique) (PASS). Insert/read round-trip returns tz-aware UTC `created_at`, 2000-byte `html_gz`; read plan is IXSCAN on `name_user_id_unique` (PASS).
- Boundary: duplicate `(name, user_id)` -> `DuplicateKeyError`; same name for another user accepted (PASS). Naive `created_at` -> `ValidationError` (PASS).
- TTL change: `TREE_MCP__GRAPH_FILE_TTL_SECONDS=120` -> INFO `300 → 120`, live 120 (PASS). `=0` -> INFO `120 → 0`, live 0 (PASS). Back to default -> `0 → 300` (PASS).
- Hostile/failure: `=-5` -> exactly ONE WARNING (code 72 InvalidOptions), boot survives, live TTL stays 120 (PASS, Story 3). `=abc` -> pydantic ValidationError at config load, boot refuses (expected: not an int, it is a typo not a Mongo-rejected value; noted below).
- State/concurrency: 3 simultaneous boots with TTL 77 over a 300 collection -> all 3 survive, live 77, one INFO line from the winner (PASS).
- Real expiry: TTL=1, inserted a row, polled -> row gone at the ~60 s TTL-monitor tick (PASS).

**Acceptance criteria**
- [x] PASS — fields + exactly two indexes: `tests/unit/entities/test_graph_files.py`; live `index_information()` above.
- [x] PASS — YAML key + `MCPConfig` + env override: `tests/unit/config/test_app_config.py::TestGraphFileTtlConfig`; live 300 -> 120 via env.
- [x] PASS — `GraphFile` in `ALL_DOCUMENT_MODELS`, reconcile awaited before `init_beanie`: `test_db.py::TestInitMongodbGraphFiles`; `db.py:66-72` read.
- [x] PASS — `collMod` only on a difference, no-ops on missing index/collection, exceptions -> one WARNING: `test_db.py::TestReconcileGraphFileTtl`; `db.py` `reconcile_graph_file_ttl`.
- [x] PASS — live second boot with changed TTL survives and `getIndexes()` shows the new value (independent run, above).
- [x] PASS — format/lint/pre-commit/memory-tests green.

**Judgement on the `-> int` deviation:** sound. Beanie re-submits every declared index, so after a failed `collMod` declaring the configured TTL would crash `init_beanie` on code 85 and Story 3 ("boot continues, old TTL stays") would be false. Returning the live TTL and rebinding `Settings.indexes` matches the existing `MemoryEntry` rebinding pattern; on the happy path the indexes are identical. My -5 run confirms it live. Spec text said `-> None`; the AC wording ("swallows any exception into one WARNING") still holds.

**Other issues found (non-blocking, follow-up only)**
- A bad TTL on a FRESH collection (no live TTL) still fails at `createIndexes` (SWE already disclosed).
- If `created_at_ttl` exists WITHOUT `expireAfterSeconds` (live None) and `collMod` fails, the reconcile returns the configured TTL and `init_beanie` would hit code 85. Contrived (only a hand-made index); no action.
- `TREE_MCP__GRAPH_FILE_TTL_SECONDS=abc` kills boot at config load (pydantic). Consistent with every other config key; mentioned only because Story 3 is about Mongo-rejected values.
- Double `init_mongodb` per boot logs the warning twice (pre-existing, goes away in 186).

**VERDICT: PASS**
