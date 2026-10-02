---
id: 172-memory-reset-mode
status: done
feature: mode-reset-and-structure-viz
---

# `make memory-reset-mode` — the one command that switches a deployment between `rag` and `graphrag`

Tags: `memory`, `ops`, `cli`, `rag`, `graphrag`
Depends on: None (171 is reserved and not a dependency)
Blocks: 173 (its local verification switches modes with this command)
Implements: ADR-006 §5 + Consequences ("Operators switching modes drop the `memory` collection") and
ADR-012 §3 ("databases start from scratch … the same rule ADR-006 applies to a mode switch") — this task
is the MECHANISM those decisions assume; it changes neither decision. Branch
`feat/mode-reset-and-structure-viz` off `main` at `8bbc0fa`, worktree `building-agentic-systems-structure-viz`.

## Scope

Today a **Memory mode** switch is a raw `mongosh … db.getSiblingDB("tree").memory.drop()` one-liner that
lives in `apps/memory/README.md` ("Memory modes") and in `.agents/skills/run-pipelines-e2e/SKILL.md`
step 0. It forgets `memory_clusters` (a **Clustering run** over the dropped chunks keeps drawing an
**Embedding map** of rows that no longer exist), prints nothing about WHICH database it just emptied
(the direnv trap in `AGENTS.md`: prod vars are exported into every shell), and has no dry run.

**Human decisions (final):**

- The command is `make memory-reset-mode`. It is GLOBAL — the mode is one per deployment (ADR-006 §5),
  so it resets ALL users; it takes no `USER_ID` / `USER_IDENTIFIER`.
- It drops the `memory` AND `memory_clusters` collections. It keeps `documents` and `users` (and every
  other collection — see "What is kept and why").
- Every **Document** becomes pending again automatically: a Document is ingested iff a `memory` row
  lists its `_id` in `sources` (`tree/memory/graph/sharding.py::_resolve_pending_document_ids`), so the
  next `make memory-run-memory-pipeline` / `make memory-run-pipeline` re-ingests everything without
  any flag or marker. The command writes nothing to `documents`.
- Dry run unless `CONFIRM=yes`. It prints the active env target (`local` / `prod`), the redacted Mongo
  host, the database name, the CONFIGURED `memory.mode`, and the row count per collection — in the dry
  run AND right before a confirmed drop.
- The `graphrag` self node needs nothing from this command: `User.ensure_self_person()`
  (`tree/entities/users.py`) re-creates `{user_id}:person:self` at the start of every graphrag
  extraction run and on `make memory-signup`.

**PA decisions (recommended, apply unless the human objects):**

1. **Prod gets ONE extra guard: the confirmation token must name it.** `CONFIRM=yes` drops only when the
   target is local; when the env target is `prod` OR the Mongo scheme is `mongodb+srv` (an Atlas URI in
   `.env` is prod data whatever `.env.target` says) the command refuses `CONFIRM=yes` with exit 1 and the
   line `Target is prod — re-run with CONFIRM=prod to drop it.`; only `CONFIRM=prod` drops there. Why: the
   blast radius is the whole memory of every user (re-ingesting prod's ≥14k documents costs real Voyage +
   Gemini money and hours), and `AGENTS.md` records that direnv exports the prod vars into every shell —
   an operator who believes they are local is the expected failure, not a hypothetical one. One branch,
   no new config. `make memory-reset-embeddings` keeps its plain `CONFIRM=yes` (re-embedding is
   recoverable from the rows; a drop is not).
2. **Index state after the drop needs NO code.** `drop()` takes the classic indexes and the mongot
   `vector_index` with the collection. Beanie recreates the classic set FOR THE CONFIGURED MODE at the
   next `init_mongodb` of any entry point (ADR-012 §1 — `init_beanie` creates the collection with its
   indexes; the two mode sets are nested, rag ⊂ graphrag, so a stray boot in the wrong mode is harmless);
   `ensure_indexes` recreates `vector_index` on the next indexing run (and on MCP boot). Between the drop
   and that run `$vectorSearch` is simply absent, which is fine: there are no rows. CONSEQUENCE for the
   script: it must NOT call `init_mongodb` — booting Beanie right before a drop would create the old
   mode's indexes for nothing — it opens a raw `AsyncMongoClient` exactly like `scripts/check_db.py`.
   The documented order is: ① set the new mode (YAML or `TREE_MEMORY__MODE` exported in the SERVING
   shell), ② `make memory-reset-mode CONFIRM=yes`, ③ re-serve workflows, ④ `make memory-run-pipeline`.
3. **Prefect's task cache needs nothing either.** Audit of `tree/memory/pipeline.py` (2026-10-02): the
   `INPUTS`-cached tasks are ① `clean-and-chunk` (document + `ChunkingConfig` + `TASK_SOURCE`),
   ② `embed-children` (texts + `embedding_identity`), ④ `llm-extract-entities` (chunked doc +
   `llm_identity`, graphrag only) and ⑥ `embed-entities`; every write task (③ `load-rag-rows`,
   ⑤ resolve, ⑦ dedupe, ⑧ `apply-writes`) is `NO_CACHE`. None of the keys carries the mode and none
   should: ① and ② produce the SAME output in both modes by design (ADR-006 §3: chunk rows are
   byte-identical across modes), so after a switch the re-run replays chunking and child vectors from
   cache (no Voyage spend) and the `NO_CACHE` load task rewrites the rows; ④ only runs in graphrag and
   its replayed JSON is still the right JSON for the same parent text and the same LLM. The command
   therefore never touches Prefect state; say so in its docstring so nobody adds a cache purge later.
4. **The logic lives in `tree/db.py`**, next to `init_mongodb`, which already owns "which collections
   exist and which mode's index set they carry". Not under `tree/memory/` — the top level there is pinned
   to four files by `tests/unit/memory/test_package_layout.py`, and the drop spans a `rag/` collection and
   a `clustering/` collection, so it is neither package's.
5. **Root `Makefile` needs no edit.** `make memory-reset-mode` already delegates through the
   `memory-%` pattern to `apps/memory/Makefile`'s `reset-mode` target (exactly how
   `make memory-reset-embeddings` works today). Only `apps/memory/Makefile` gains a target.

### What is kept and why (print nothing about these; document in the module docstring)

| Collection | Kept | Why |
|---|---|---|
| `documents` | yes | The Data Pipeline's output; the whole point is to re-ingest them. All become pending (see above). |
| `users` + `sessions` | yes | Tenant list and the current-user session; `person:self` is re-created per decision above. |
| `knowledge_graph_meta_state` | yes | The dream watermark is a START timestamp; rebuilt rows carry a newer `updated_at`, so the next dream run sees them. Graphrag-only anyway. |
| `extraction_rejections` / `extraction_dropped_fields` | yes | Audit history. Their `chunk_id` references go stale, which is harmless — nothing joins on them. |

### Implementation (by symbol)

1. **`tree/config/settings.py::MongoSettings.redacted_target() -> str`** — move `scripts/check_db.py::_redacted_target`
   here (scheme + host, + port for non-SRV; never credentials) and make `check_db.py` call it. One
   spelling of "which Mongo am I pointed at" for both commands.
2. **`tree/db.py`** — add:
   - `MODE_BOUND_COLLECTIONS: tuple[str, ...] = (MEMORY_COLLECTION, MEMORY_CLUSTERS_COLLECTION)` with a
     comment: the two collections whose contents depend on `memory.mode` (ADR-006 §1, ADR-007 §3).
   - `class ModeResetReport(BaseModel)`: `env_target: str`, `target: str` (the redacted host),
     `database: str`, `configured_mode: MemoryMode`, `dropped: dict[str, int]` (collection → rows
     counted BEFORE the drop, for the two mode-bound collections), `kept: dict[str, int]` (`documents`,
     `users`), `pending_documents: int` (documents with `content != None` — what the next memory run
     will print as "Processing N documents"), `dry_run: bool`.
   - `async def reset_memory_mode(client: AsyncMongoClient, database: str, *, env_target: str, dry_run: bool) -> ModeResetReport`:
     `count_documents({})` on the four collections (exact, not estimated — the numbers are the
     operator's evidence), count pending documents, then if not `dry_run`: `drop_collection` on each
     mode-bound collection (a missing collection is a no-op in pymongo — idempotent by construction).
     Log ONE line per collection (`Dropped memory (1 234 rows)` / `Would drop …` in a dry run) and one
     final line naming what to run next: `Next: set memory.mode, re-serve workflows (make
     memory-serve-workflows), then make memory-run-pipeline — N document(s) are pending.` No Beanie, no
     `MemoryEntry` — raw collections only.
3. **`scripts/reset_mode.py`** (glue; `init_logger()` at module level; Click): options `--env-target
   local|prod` (passed by the Makefile; default `unknown` for direct invocation — then the prod rule
   below applies on the scheme alone), `--confirm <token>` (default none). Required token:
   `"prod"` if `env_target == "prod"` or `settings.mongo.mongo_scheme == "mongodb+srv"`, else `"yes"`.
   Opens `AsyncMongoClient(settings.mongo.mongo_uri…)` (no `init_mongodb` — decision 2), calls
   `reset_memory_mode(dry_run = token != required)`, prints the report as a block:

   ```
   Mode reset — env target: local (mongodb://localhost:27017) · database: tree · memory.mode (configured): rag · ALL users
     memory:          1234 rows  → DROP
     memory_clusters:    7 rows  → DROP
     documents:          3 rows  → kept (3 become pending)
     users:              1 rows  → kept
   Dry run: nothing dropped. Re-run with CONFIRM=yes to drop both collections.
   ```

   Dry run (no token / wrong token) exits 1 after the block (the `make memory-reset-embeddings`
   convention). Wrong token on prod: the block, then `Target is prod — re-run with CONFIRM=prod to drop
   it.`, exit 1. Confirmed: the block, the two `Dropped …` lines, the `Next: …` line, exit 0. Module
   docstring states the four-step order, the `CONFIRM=prod` rule, that `person:self` comes back by
   itself, and that Prefect caches are intentionally untouched (decision 3).
4. **`apps/memory/Makefile`** — under `# --- Pipelines ---` next to `reset-embeddings`:
   ```make
   reset-mode: # MODE RESET for ALL users (memory.mode is one per deployment, ADR-006): drops `memory` + `memory_clusters` and keeps `documents` + `users`, so every document is pending again and the next make memory-run-pipeline rebuilds the memory in the CONFIGURED mode. Switch the mode FIRST (YAML or TREE_MEMORY__MODE in the serving shell), then run this, then re-serve + run the pipeline. Dry run unless CONFIRM=yes — and CONFIRM=prod on the prod target (prints the target + counts either way).
   	uv run python scripts/reset_mode.py --env-target "$(if $(filter prod,$(ENV_TARGET)),prod,local)" $(if $(CONFIRM),--confirm "$(CONFIRM)",)
   ```
5. **Docs** (same commit):
   - `.agents/skills/run-pipelines-e2e/SKILL.md` step 0: replace the `mongosh … memory.drop()` block
     with the four-step order using `make memory-reset-mode CONFIRM=yes`; keep the "users survive /
     `person:self` comes back" paragraph; "Verifying a change in BOTH modes = run steps 1–3 twice, with a
     `make memory-reset-mode CONFIRM=yes` in between". Add the dry-run line as the way to see what is
     about to go.
   - `apps/memory/README.md` "Memory modes": replace the `mongosh … memory.drop()` snippet with the
     command (dry run, then `CONFIRM=yes`), one sentence on `CONFIRM=prod`, and one on what is kept.
     Add `reset-mode` to the CLI list where `reset-embeddings` is documented.
   - **Glossary** (`docs/glossary.md`, new row — PA-authored, apply verbatim):
     `| **Mode reset** | \`make memory-reset-mode\` → \`tree.db.reset_memory_mode\`: drops the two mode-bound collections (\`memory\`, \`memory_clusters\`) for ALL users and keeps \`documents\` and \`users\`, so every **Document** is pending again and the next **Memory Pipeline** run rebuilds the memory in the configured **Memory mode**. Dry run unless \`CONFIRM=yes\` (\`CONFIRM=prod\` on the prod target); prints the env target, the redacted host and the row counts first. | The ONLY supported way to switch \`rag\` ↔ \`graphrag\` (ADR-006, ADR-012: no migration, no index retirement — a rebuild). Not per user, unlike the **Embedding reset**. Classic indexes come back at the next boot (Beanie), \`vector_index\` at the next indexing run; \`person:self\` at the next graphrag run. Touches no Prefect cache: chunking and child embeddings replay from cache on purpose. |`
   - No ADR edit: the command implements ADR-006/012's existing "drop and rebuild" rule.

### Tests (`/squid-testing-python`)

- `tests/unit/test_db.py` — `TestResetMemoryMode`, against the REAL local Mongo on a throwaway database
  (the file's existing pattern; drop it in `finally`): seed 3 `memory` rows (one `document`, one parent,
  one child with `sources: [doc_id]`), 1 `memory_clusters` row, 2 `documents` (one `content: None`),
  1 `users` row. Cases: dry run reports `dropped == {"memory": 3, "memory_clusters": 1}`,
  `kept == {"documents": 2, "users": 1}`, `pending_documents == 1`, and every collection still exists
  afterwards; confirmed run drops exactly the two mode-bound collections (`list_collection_names`) and
  leaves `documents` / `users` byte-identical; a second confirmed run reports zeros and raises nothing
  (idempotent); the report's `configured_mode` is `app_config.memory.mode` (parametrise `rag` /
  `graphrag` via `monkeypatch`); `MODE_BOUND_COLLECTIONS == ("memory", "memory_clusters")`.
- `tests/unit/scripts/test_reset_mode.py` — glue tests in the `test_reset_embeddings_script.py` style
  (`CliRunner`, `AsyncMongoClient` and `reset_memory_mode` patched on the module): no `--confirm` →
  `dry_run=True`, exit 1, the block printed with env target, host, database, mode and the four counts;
  `--env-target local --confirm yes` → `dry_run=False`, exit 0, the `Next:` line; `--env-target prod
  --confirm yes` → `dry_run=True`, exit 1, the `CONFIRM=prod` line; `--env-target prod --confirm prod` →
  drops; `--env-target local` with `mongo_scheme == "mongodb+srv"` (patch `settings.mongo`) behaves as
  prod; `--env-target unknown` (direct invocation) follows the scheme; the module never imports
  `init_mongodb` (`assert not hasattr(module, "init_mongodb")` — decision 2 pinned).
- `tests/unit/scripts/test_check_db.py` is not needed; the moved `redacted_target` gets two cases in
  `tests/unit/config/` (SRV → no port, plain → host:port, never the password).
- `tests/unit/test_mode_reset_docs.py` — grep-level, the `test_embedding_map_docs.py` pattern: the e2e
  skill and `apps/memory/README.md` contain `make memory-reset-mode CONFIRM=yes`, the README names
  `CONFIRM=prod`, and `git grep --untracked "memory.drop()" -- apps/memory .agents README.md
  docs/glossary.md` is empty (the raw one-liner is gone from everything a reader follows; ADRs and
  `tasks/` keep it as history).
- `make memory-tests` green from a clean session; `make memory-format-check && make memory-lint-check`.

### Local verification (LOCAL ONLY — `make env-status` → `local`; STOP if it prints prod)

The local database is `rag` with the 3-article subset
`/private/tmp/claude-501/-Users-pauliusztin-Documents-01-Projects-AI-Engineer-Handbook-building-agentic-systems-building-agentic-systems/944c4516-3945-43f4-8111-e0c95524323d/scratchpad/169/light3.yaml`;
counts script `…/scratchpad/169/counts.js` (run through the guarded `…/scratchpad/169/mongo.sh`, after
pointing its `cd` at THIS worktree). Serve workflows FROM THIS WORKTREE (`make memory-serve-workflows &`;
stop the Docker `prefect-worker` if it runs — it executes main's image). Paste every output in `## Log`.
End in `rag` (the YAML default).

1. **Dry run.** `make memory-reset-mode` → the block shows `env target: local (mongodb://localhost:…)`,
   `memory.mode (configured): rag`, `memory: N rows → DROP` with N equal to `counts.js`'s `rows:`,
   `memory_clusters` (0 or the last run's count), `documents: 3 rows → kept (3 become pending)`,
   `users: 1`; exit 1; `counts.js` unchanged.
2. **Prod guard without prod.** `uv --directory apps/memory run python scripts/reset_mode.py --env-target
   prod --confirm yes` → the block, the `CONFIRM=prod` line, exit 1, `counts.js` unchanged. (Never run
   `--confirm prod` here; never run `make env-prod`.)
3. **Confirmed.** `make memory-reset-mode CONFIRM=yes` → `Dropped memory (N rows)`, `Dropped
   memory_clusters (…)`, the `Next:` line, exit 0. `mongo.sh --eval 'db.getSiblingDB("tree").getCollectionNames()'`
   → neither `memory` nor `memory_clusters`; `documents` and `users` counts unchanged.
4. **Indexes come back by themselves.** `make memory-whoami` (any Beanie boot) → `counts.js` prints
   `getIndexes: ["_id_","user_kind_type_subtype","user_type_name","text_index"]` (the rag set, ADR-012)
   and `getSearchIndexes: []`, `rows: 0`.
5. **Everything is pending.** Re-serve, `make memory-run-pipeline SOURCE_FILE=<light3.yaml>` → the
   worker log says `Processing 3 documents in rag mode`; the `clean-and-chunk` / `embed-children` tasks
   report cache hits (`Finished in state Cached(…)`), `load-rag-rows` runs. `counts.js`: the same row
   groups as before step 3 (`chunk child` embedded dim 1024, `chunk parent`, `document`; `edges: 0`),
   `getSearchIndexes: ["vector_index"]`. `make memory-query-graph QUERY="how does memory for ai agents
   work"` prints ranked parents again (this target still exists until 173).
6. **The configured mode is reported, not assumed.** `TREE_MEMORY__MODE=graphrag make memory-reset-mode`
   (dry run) → `memory.mode (configured): graphrag`, exit 1, nothing dropped. Do NOT confirm in graphrag
   here — 173 does the full graphrag round trip.
7. **Clean up.** Stop the serve process; `make env-status` → local; `git status` shows only this task's
   files.

### Out of scope (intentional)

- Per-user reset (`USER_ID=`) — the mode is per deployment; a per-user drop would leave the other
  tenants' rows in the other mode's shape and break every ADR-012 invariant. Upgrade trigger: a second
  deployment shape where tenants legitimately differ in mode.
- Dropping or rebuilding indexes explicitly, a `--mode` flag that edits the YAML, or purging Prefect
  caches — see decisions 2 and 3.
- Touching `documents` (no `pending` flag, no `updated_at` bump) — pending-ness is derived from
  `sources` by design and stays that way.
- Re-creating `person:self` — `ensure_self_person` already does.

## Acceptance Criteria

- [x] `make memory-reset-mode` (no `CONFIRM`) prints the env target, the redacted Mongo host, the
      database, the configured `memory.mode`, the row counts of `memory`, `memory_clusters`,
      `documents`, `users` and the pending-document count, drops nothing, and exits 1.
- [x] `make memory-reset-mode CONFIRM=yes` on a local target drops exactly `memory` and
      `memory_clusters`, leaves `documents` and `users` untouched, prints `Dropped …` per collection and
      the `Next: …` line, and exits 0; running it again drops nothing and exits 0.
- [x] With env target `prod` or a `mongodb+srv` scheme, `CONFIRM=yes` is refused with exit 1 and a line
      naming `CONFIRM=prod`; `CONFIRM=prod` drops.
- [x] `scripts/reset_mode.py` opens a raw client and never calls `init_mongodb`; it contains no business
      logic beyond option parsing and printing (`tree.db.reset_memory_mode` does the work).
- [x] After a confirmed reset, the next Beanie boot recreates the configured mode's classic index set and
      the next indexing run recreates `vector_index` — verified locally (steps 4–5), no code added for it.
- [x] After a confirmed reset, `make memory-run-pipeline` re-ingests every document with content
      (`Processing N documents`) with no extra flag, replaying chunking + child embeddings from cache.
- [x] `tree.db.reset_memory_mode` returns a `ModeResetReport` (Pydantic) and is covered against the real
      test Mongo (dry run / confirmed / idempotent / mode reported).
- [x] The e2e skill step 0 and `apps/memory/README.md` document the command and the four-step order; the
      raw `memory.drop()` one-liner is gone from code, skills, READMEs and the glossary (docs test).
- [x] Glossary gains **Mode reset** verbatim; no ADR is edited.
- [x] `make memory-tests`, format and lint checks green; `make memory-query-graph QUERY=…` still works
      after the reset + rebuild (173 renames it).

## User Stories

### Story: An operator switches a local deployment from `rag` to `graphrag`
1. Operator exports `TREE_MEMORY__MODE=graphrag` in the shell that serves workflows.
2. Operator runs `make memory-reset-mode` and reads `env target: local (mongodb://localhost:27017)`,
   `memory.mode (configured): graphrag`, `memory: 118 rows → DROP`, `memory_clusters: 0 rows → DROP`,
   `documents: 3 rows → kept (3 become pending)`, `users: 1 rows → kept`, then
   `Dry run: nothing dropped. Re-run with CONFIRM=yes …`; the command exits 1.
3. Operator runs `make memory-reset-mode CONFIRM=yes` and reads the same block, `Dropped memory (118
   rows)`, `Dropped memory_clusters (0 rows)`, `Next: set memory.mode, re-serve workflows … — 3
   document(s) are pending.`; exit 0.
4. Operator re-serves and runs `make memory-run-pipeline SOURCE_FILE=…/light3.yaml`; the log reads
   `Processing 3 documents in graphrag mode`, `Self-person node upserted …`, and `counts.js` shows
   entity rows and `edges: > 0`.

### Story: An operator with prod vars in their shell is stopped
1. Operator's shell has `.env.target=prod` (or an Atlas `mongodb+srv` URI in `.env`).
2. Operator runs `make memory-reset-mode CONFIRM=yes` believing they are local.
3. The block prints `env target: prod (mongodb+srv://tree.xxxxx.mongodb.net)` and the counts, then
   `Target is prod — re-run with CONFIRM=prod to drop it.`; exit 1; nothing is dropped.
4. Operator runs `make env-local` and repeats on local, or deliberately types `CONFIRM=prod`.

### Story: A verifier follows the e2e skill in both modes
1. Verifier reads step 0 of `run-pipelines-e2e`: it says `make memory-reset-mode CONFIRM=yes` between
   the two mode runs, never a `mongosh` one-liner.
2. Verifier runs the rag round (serve → pipeline → query), then the reset, flips the mode, re-serves,
   runs the graphrag round.
3. The reset's dry run before each round gives the row counts they paste into the Log.

### Story: The embedding map does not outlive its chunks
1. Operator clustered the rag memory (`memory_clusters` has 4 rows), then runs the reset.
2. `make memory-visualize-embeddings` prints `No clustering run found for this user …` and exits 1 —
   not a map of 118 points that no longer exist.
3. After the rebuild and `make memory-run-clustering-pipeline`, the map is back.

### Story: Direct invocation without make
1. An operator runs `uv --directory apps/memory run python scripts/reset_mode.py` from a shell with no
   `.env.target` knowledge.
2. The block prints `env target: unknown (mongodb://localhost:27017)`; the required token is `yes` for a
   plain `mongodb://` URI and `prod` for `mongodb+srv://`.

---

Blocked by: (none)

## Log

### [PA] 2026-10-02 18:00 — Grooming

**Summary**
One make command, `make memory-reset-mode`, replaces the raw `mongosh` drop as the only supported way to
switch a deployment between `rag` and `graphrag`: it drops `memory` + `memory_clusters` for all users,
keeps `documents` + `users` (so every document is pending again by construction), is a dry run unless
`CONFIRM=yes` (`CONFIRM=prod` on prod), and prints the env target, host and row counts first.

**Key decisions**
- Logic in `tree/db.py::reset_memory_mode` next to `init_mongodb` (the module that already owns the
  collections and their mode-bound index sets); the script opens a raw client and never boots Beanie, so
  a drop cannot recreate the old mode's indexes as a side effect.
- Extra prod guard: the confirmation token must be `prod` when the env target is prod or the URI is SRV —
  justified by the direnv trap `AGENTS.md` records and by an unrecoverable blast radius.
- No index code: Beanie recreates the classic set at the next boot (ADR-012), `ensure_indexes` the
  vector index at the next indexing run; the task documents the order (mode first, reset, re-serve, run).
- No Prefect cache handling: the cached tasks' keys carry no mode and must not — chunking and child
  embeddings are mode-identical by ADR-006 §3 and replaying them is the desired saving; every write task
  is `NO_CACHE`.
- `memory_clusters` is dropped too (ADR-007 §3: clusters are derived from the chunks being dropped);
  `knowledge_graph_meta_state` and the audit collections are kept (reasons in the table).
- Root `Makefile` untouched: the `memory-%` delegation already routes `make memory-reset-mode`.

**Dependencies**
- None. Task 173 depends on this one (its verification switches modes with the command).

**User stories**
- 5 stories: rag→graphrag switch, the prod stop, the e2e verifier, the map not outliving its chunks,
  direct invocation.

**Open questions** (defaults recommended; do not block implementation)
- Keep the `CONFIRM=prod` guard? Recommended yes (decision 1). If the human prefers parity with
  `reset-embeddings`, drop decision 1 and its two tests — nothing else changes.
- Glossary wording is in-task (PA-authored, apply verbatim); the human asked PA not to write doc files in
  this grooming round.

Ready for implementation.

### [SWE] 2026-10-02 18:20 — Implementation

**Files modified**
- `apps/memory/src/tree/config/settings.py` — `MongoSettings.redacted_target()` (moved from `check_db.py`; plain method, not a computed field, so it never lands in dumps).
- `apps/memory/scripts/check_db.py` — calls `mongo.redacted_target()`; `_redacted_target` removed.
- `apps/memory/src/tree/db.py` — module docstring (what is kept and why), `MODE_BOUND_COLLECTIONS`, `ModeResetReport`, `reset_memory_mode` (raw collections, exact counts, `drop_collection` per mode-bound collection, one `Dropped <name> (N rows)` log line each).
- `apps/memory/scripts/reset_mode.py` — NEW Click glue: `--env-target local|prod|unknown` (default `unknown`), `--confirm`; required token `prod` if env target prod OR scheme `mongodb+srv`, else `yes`; raw `AsyncMongoClient`, never `init_mongodb`; docstring carries the four-step order, the `CONFIRM=prod` rule, `person:self`, and "no Prefect cache purge on purpose".
- `apps/memory/Makefile` — `reset-mode` target next to `reset-embeddings` (root Makefile untouched; `memory-%` delegates).
- `.agents/skills/run-pipelines-e2e/SKILL.md` step 0, `apps/memory/README.md` "Memory modes" + Layout scripts list — the raw mongosh drop replaced by the command, dry run first, four-step order, `CONFIRM=prod`, what is kept.
- `docs/glossary.md` — **Mode reset** row, verbatim, placed after **Modal catalog** (alphabetical).
- `apps/memory/tests/unit/test_db.py` — `TestResetMemoryMode` (real local Mongo, throwaway db `unit_tests_twin_mode_reset`, raw client).
- `apps/memory/tests/unit/scripts/test_reset_mode.py` — NEW glue tests (client + `reset_memory_mode` patched, `settings.mongo` patched with an explicit `MongoSettings`, assertions on `caplog`).
- `apps/memory/tests/unit/config/test_settings.py` — `TestRedactedTarget` (SRV → no port; plain → host:port; never credentials).
- `apps/memory/tests/unit/test_mode_reset_docs.py` — NEW grep-level docs test (needle built by concatenation, `git grep --untracked -F`).

**Tests**
- Unit: 4544 passed, 0 failed — `make memory-tests` (after `uv --directory apps/memory sync --all-extras --all-groups`; the fresh worktree venv lacked `modal` / `sentence_transformers`, which errored 8 modules at collection before the sync).
- Docs tests were red (4 failures: both mongosh hits) before the doc edits, green after.
- Format/lint/pre-commit: clean.
- Integration: N/A — the project has no integration suite.

**Acceptance criteria**
- [x] Dry run prints env target, redacted host, database, configured mode, four counts + pending, drops nothing, exits 1 — `test_reset_mode.py::test_without_a_token_it_prints_the_block_drops_nothing_and_exits_one`, `test_db.py::TestResetMemoryMode::test_a_dry_run_counts_everything_and_drops_nothing`, local step 1.
- [x] `CONFIRM=yes` local drops exactly the two, keeps documents/users, `Dropped …` + `Next: …`, exit 0; re-run exits 0 — `test_a_confirmed_run_drops_exactly_the_mode_bound_collections`, `test_a_second_confirmed_run_is_a_harmless_no_op`, `test_yes_on_a_local_target_drops_and_names_the_next_step`, local step 3 (+ second run).
- [x] Prod / SRV refuse `CONFIRM=yes` naming `CONFIRM=prod`; `CONFIRM=prod` drops — `test_yes_on_the_prod_target_is_refused`, `test_prod_on_the_prod_target_drops`, `test_an_srv_uri_is_prod_whatever_the_env_target_says[local|unknown]`, local step 2 (refusal only).
- [x] Raw client, never `init_mongodb`, logic in `tree.db` — `test_the_script_never_boots_beanie`.
- [x] Indexes come back with no code — local steps 4–5.
- [x] Rebuild re-ingests every document with content, chunking + child embeddings from cache — local step 5.
- [x] `ModeResetReport` covered against real Mongo (dry run / confirmed / idempotent / mode rag+graphrag) — `TestResetMemoryMode`.
- [x] Docs + no raw one-liner — `test_mode_reset_docs.py`.
- [x] Glossary row verbatim; no ADR edited.
- [x] Tests/format/lint green; `make memory-query-graph` works after reset + rebuild — local step 5.

**Evidence** (LOCAL only: `make env-status` → `Env target: local (.env)`, shell `MONGO_HOST=localhost`, no `MONGO_SCHEME` exported; no Docker `prefect-worker` running; serve from THIS worktree, `PREFECT_LOGGING_ROOT_LEVEL=INFO`, the only `tree.orchestrator` process. `CONFIRM=prod` never run.)

Baseline before the reset (`counts.js`):
```
getIndexes: ["_id_","user_kind_type_subtype","user_type_name","text_index"]
getSearchIndexes: ["vector_index"]
rows: 83 edges: 0 embedded: 75
   node chunk child n=75 embedded=75 dim=1024
   node chunk parent n=5 embedded=0 dim=0
   node document - n=3 embedded=0 dim=0
documents 69 users 1 memory_clusters 0      # 69 documents: 3 with content, 66 contentless
```

Step 1 — `make memory-reset-mode` (exit 1 from the script; make wraps it as Error 2); `counts.js` unchanged (rows: 83):
```
Mode reset — env target: local (mongodb://localhost:27017) · database: tree · memory.mode (configured): rag · ALL users
  memory:              83 rows  → DROP
  memory_clusters:      0 rows  → DROP
  documents:           69 rows  → kept (3 become pending)
  users:                1 rows  → kept
Dry run: nothing dropped. Re-run with CONFIRM=yes to drop both collections.
make[1]: *** [reset-mode] Error 1
```

Step 2 — `uv --directory apps/memory run python scripts/reset_mode.py --env-target prod --confirm yes` → exit=1, rows still 83:
```
Mode reset — env target: prod (mongodb://localhost:27017) · database: tree · memory.mode (configured): rag · ALL users
  ...same four count lines...
Dry run: nothing dropped.
Target is prod — re-run with CONFIRM=prod to drop it.
```

Step 3 — `make memory-reset-mode CONFIRM=yes` → exit=0:
```
  ...block...
Dropped memory (83 rows)
Dropped memory_clusters (0 rows)
Next: set memory.mode, re-serve workflows (make memory-serve-workflows), then make memory-run-pipeline — 3 document(s) are pending.
$ mongo.sh --eval 'getCollectionNames()'
["documents","extraction_dropped_fields","extraction_rejections","knowledge_graph_meta_state","sessions","users"]
documents 69 users 1
```
Second `make memory-reset-mode CONFIRM=yes` → `memory: 0 rows`, `Dropped memory (0 rows)`, `Dropped memory_clusters (0 rows)`, exit=0.

Step 4 — `make memory-whoami` then `counts.js`:
```
getIndexes: ["_id_","text_index","user_kind_type_subtype","user_type_name"]
getSearchIndexes: []
rows: 0 edges: 0 embedded: 0
```

Step 5 — `make memory-run-pipeline SOURCE_FILE=…/scratchpad/169/light3.yaml` → `Done. Flow completed successfully.` Worker log:
```
Processing 3 documents in rag mode (user_id=6abf89ef81b8cfb94d2032a2)
Task run 'clean-and-chunk-5e2' - Finished in state Cached(type=COMPLETED)
Task run 'clean-and-chunk-1f5' - Finished in state Cached(type=COMPLETED)
Task run 'clean-and-chunk-9a8' - Finished in state Cached(type=COMPLETED)
Task run 'embed-children-a09' - Finished in state Cached(type=COMPLETED)
Task run 'load-rag-rows-33f' - load_rag_rows: documents=3 rows_written=83
Task run 'ensure-kg-indexes-208' - Finished in state Completed()
```
`counts.js` — identical to the baseline:
```
getIndexes: ["_id_","text_index","user_kind_type_subtype","user_type_name"]
getSearchIndexes: ["vector_index"]
rows: 83 edges: 0 embedded: 75
   node chunk child n=75 embedded=75 dim=1024
   node chunk parent n=5 embedded=0 dim=0
   node document - n=3 embedded=0 dim=0
```
`make memory-query-graph QUERY="how does memory for ai agents work"` → exit 0, `40 child hit(s) -> 5 parent(s)`, top `[0.032] How Does Memory for AI Agents Work?`.

Step 6 — `TREE_MEMORY__MODE=graphrag make memory-reset-mode` → `memory.mode (configured): graphrag`, dry-run line, exit 1; rows still 83.

Step 7 — serve process killed (no `tree.orchestrator` left); `make env-status` → local; `git status` shows only this task's files (+ the untracked 173 task file, already present). DB ends in `rag`, rebuilt.

Story 4 (map does not outlive its chunks) — `make memory-visualize-embeddings` after the rebuild: `No clustering run found for this user — run make memory-run-clustering-pipeline to build the embedding map.`, exit 1.

`make memory-check-db` still prints `Checking MongoDB connectivity: mongodb://localhost:27017 (user=tree)` after the helper move.

**Notes**
- Deviation (design, not architecture): the script calls `reset_memory_mode` TWICE — `dry_run=True` to get the counts and print the block, then `dry_run=False` only when the token matches. That is the only way the block appears "right before a confirmed drop" while the spec'd glue tests (which patch `reset_memory_mode`) can still see every printed line. So the block, the dry-run tail, the `CONFIRM=prod` refusal and the `Next:` line are logged by the script; `tree.db` logs only the `Dropped …` lines. No separate `Would drop …` lines: the block's `→ DROP` already says it, and on a confirmed run they would print before `Dropped …` as noise.
- On a prod refusal the "Re-run with CONFIRM=yes" hint is NOT printed (it would be wrong advice there); the tail reads `Dry run: nothing dropped.` + the `CONFIRM=prod` line.
- `CONFIRM=prod` on a LOCAL target is a dry run (token must equal the required one, per spec).
- The local DB holds 69 `documents` (66 contentless from earlier runs), not 3 — so the block reads `documents: 69 rows → kept (3 become pending)`; `pending_documents` counts `content != None`, matching `Processing 3 documents`.
- Output goes through the logger (`init_logger`, no `print`), so the script tests assert on `caplog`.

### [Tester] 2026-10-02 18:45 — QA

**Test summary**
- Format / lint / pre-commit: PASS (`make memory-format-check`, `make memory-lint-check`, `make pre-commit` all exit 0)
- Unit tests: 4544 passed / 0 failed (`make memory-tests`, 57.5s). No integration suite exists (project convention).
- Warnings: 0

**E2E adversarial pass** (LOCAL only, `make env-status` -> `Env target: local (.env)`, host localhost; destructive checks on scratch DB `tester172`, dropped afterwards; scratch logs in `scratchpad/172t/`)
- Happy path, scratch DB (no args): block with `env target: unknown`, db `tester172`, `memory.mode (configured): rag`, `memory: 3`, `memory_clusters: 2`, `documents: 4 -> kept (2 become pending)`, `users: 1`, dry-run line, exit 1; collections unchanged (PASS). Pending = 2 because `{content:null}` and a doc with the field missing are not counted, matching `_resolve_pending_document_ids` (`content != None`).
- Break path 1 (odd confirm tokens, `--env-target local`): `YES`, `Yes`, ` yes`, `yes `, `true`, `y`, `1`, empty, `prod` -> all dry run, exit 1, nothing dropped (PASS; strict equality). Via make: `CONFIRM=`, `CONFIRM=YES`, `CONFIRM=prod`, `CONFIRM="yes please"` on the real local DB -> dry run, exit 1, memory still 83 (PASS).
- Break path 2 (direnv trap: prod vars with local env target): `MONGO_SCHEME=mongodb+srv MONGO_HOST=sim.invalid`, real script logic with only `AsyncMongoClient` redirected to the localhost scratch DB: `--env-target local --confirm yes` and `unknown --confirm yes` -> `env target: local (mongodb+srv://sim.invalid)`, `Target is prod - re-run with CONFIRM=prod to drop it.`, exit 1, nothing dropped; `local --confirm prod` -> drops (PASS). `make -f -` check of the Makefile expression: ENV_TARGET prod -> `--env-target prod`, empty -> `local`. Real prod via make not run (`.env.prod` missing, as intended).
- Break path 3 (unreachable SRV host, `MONGO_HOST=nonexistent-tester172.invalid`, sentinel password): exit 1 with a pymongo `ConfigurationError` traceback, sentinel password appears 0 times in output, nothing dropped (PASS with note: unhelpful traceback, same as other scripts).
- Break path 4 (collections missing / DB missing): second confirmed run on collections already gone -> zeros, `Dropped ... (0 rows)`, exit 0; nonexistent database -> zeros, exit 0, the database is NOT created (`getDBNames` excludes it) (PASS).
- Break path 5 (blast radius): scratch DB seeded with 10 collections incl. decoys `memory_backup`, `memory_clusters_old`, `sessions`, `knowledge_graph_meta_state`, `extraction_*`: after a confirmed run only `memory` + `memory_clusters` are gone, the 8 others have identical counts (PASS). On the real DB, `documents` count 69 and id-length checksum unchanged, `users`, `sessions`, `extraction_rejections=17` unchanged.
- Break path 6 (concurrency / held connection): two confirmed runs started simultaneously while a mongosh loop kept querying `memory`: both exit 0 (one reports 3/2 rows dropped, the other 0/0), no errors from the holder (PASS). A reset dry run and a `memory-query-graph` while `tree.orchestrator` was serving: fine.
- Break path 7 (config errors): `TREE_MEMORY__MODE=bogus` -> pydantic literal_error, exit 1, nothing dropped; `--env-target PROD` -> click usage error, exit 2 (PASS).
- Docs test mutation: an untracked file containing the raw one-liner fails `test_the_raw_mongosh_drop_is_gone_from_what_readers_follow` (PASS); variant `getCollection("memory").drop()` is not caught (note).

**Acceptance criteria**
- [x] PASS — dry run prints env target, redacted host, database, configured mode, four counts + pending, drops nothing, exits 1 — `make memory-reset-mode` real local: `env target: local (mongodb://localhost:27017) ... memory: 83 ... documents: 69 -> kept (3 become pending)`, make Error 1; `test_reset_mode.py::test_without_a_token_...`, `test_db.py::TestResetMemoryMode::test_a_dry_run_...`
- [x] PASS — `CONFIRM=yes` local drops exactly `memory` + `memory_clusters`, keeps documents/users, `Dropped ...` + `Next: ...`, exit 0; rerun exit 0 — real local run `Dropped memory (83 rows)`, `Dropped memory_clusters (0 rows)`, `Next: ... 3 document(s) are pending.`; collections list afterwards had neither; scratch rerun exit 0
- [x] PASS — prod / `mongodb+srv` refuses `CONFIRM=yes` naming `CONFIRM=prod`, `CONFIRM=prod` drops — break path 2 + `test_yes_on_the_prod_target_is_refused`, `test_prod_on_the_prod_target_drops`, `test_an_srv_uri_is_prod_whatever_the_env_target_says`; `--env-target prod --confirm yes` on local DB (SWE step 2, re-read) and the simulation above
- [x] PASS — raw client, never `init_mongodb`, logic in `tree.db` — `apps/memory/scripts/reset_mode.py` (only option parsing, token compare, logging), `test_the_script_never_boots_beanie`; after reset `memory` collection did not exist until `make memory-whoami`
- [x] PASS — classic indexes back at next Beanie boot, `vector_index` at next indexing run — after `make memory-whoami`: `getIndexes ["_id_","text_index","user_kind_type_subtype","user_type_name"]`, `getSearchIndexes []`; after pipeline: `["vector_index"]`
- [x] PASS — rebuild re-ingests every content document with no flag, replays chunking + child embeddings from cache — `make memory-run-pipeline SOURCE_FILE=light3.yaml` exit 0; serve log: `Processing 3 documents in rag mode`, 3x `clean-and-chunk ... Cached`, `embed-children ... Cached`, `load_rag_rows: documents=3 rows_written=83`; counts identical to baseline (83 rows, 75 embedded dim 1024, 5 parents, 3 documents)
- [x] PASS — `ModeResetReport` Pydantic, covered against real Mongo — `tree/db.py:78`, `TestResetMemoryMode` (4 cases + constant), all green
- [x] PASS — e2e skill step 0 + README document command and order; raw one-liner gone — `.agents/skills/run-pipelines-e2e/SKILL.md`, `apps/memory/README.md` diffs; `git grep --untracked "memory.drop()"` empty in apps/.agents/README/glossary; `test_mode_reset_docs.py` 4 passed
- [x] PASS — glossary **Mode reset** verbatim (programmatic substring match against the task text = True); no `docs/adr/` change in `git status`
- [x] PASS — tests/format/lint green; `make memory-query-graph QUERY="how does memory for ai agents work"` -> `40 child hit(s) -> 5 parent(s)`, top `[0.032] How Does Memory for AI Agents Work?`

**Evidence**
```
$ make memory-tests          -> 4544 passed in 57.51s (exit 0)
$ make memory-reset-mode CONFIRM=yes   (real local)
Mode reset — env target: local (mongodb://localhost:27017) · database: tree · memory.mode (configured): rag · ALL users
memory: 83 rows → DROP / memory_clusters: 0 rows → DROP / documents: 69 rows → kept (3 become pending) / users: 1 rows → kept
Dropped memory (83 rows) / Dropped memory_clusters (0 rows) / Next: ... 3 document(s) are pending.
before: ["documents=69","extraction_dropped_fields=0","extraction_rejections=17","knowledge_graph_meta_state=0","memory=83","memory_clusters=0","sessions=1","users=1"]
after : ["documents=69","extraction_dropped_fields=0","extraction_rejections=17","knowledge_graph_meta_state=0","sessions=1","users=1"]
```
End state: local DB `tree` rebuilt in `rag` (83 rows, `vector_index` present); serve process killed; `make env-status` local; scratch DB dropped; tasks/173 untouched.

**Other issues found (non-blocking)**
- `scripts/reset_mode.py` prints the prod/SRV refusal only after connecting and counting (needed for the block), so an unreachable Atlas host ends in a raw pymongo traceback rather than the refusal. Harmless (nothing dropped, no credential in the trace) but the guard could run before connecting.
- `Dropped X (N rows)` N comes from the second `reset_memory_mode` call, the block from the first; a concurrent writer can make them differ. Cosmetic.
- `test_the_raw_mongosh_drop_is_gone_...` returns `""` on a `git grep` failure (no returncode check) and only matches the literal `memory.drop()`; `getCollection("memory").drop()` variants slip through. Test-quality nit.
- No unit test pins `--confirm prod` on a local plain URI being a dry run, nor case/whitespace tokens (behaviour verified manually above).
- The `pending` count is global (`content != None`), the pipeline resolver is per user; correct for the single-user local DB, approximate for multi-tenant.

**VERDICT: PASS**
