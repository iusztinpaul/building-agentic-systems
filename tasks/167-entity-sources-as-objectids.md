---
id: 167-entity-sources-as-objectids
status: pending
feature: dynamic-graph-viz
---

# Store entity `sources` as ObjectIds, like every other `memory` row

Tags: `memory`, `bug`, `data-shape`
Depends on: 165 (removes its dual-form workaround)
Blocks: —

## Scope

Every `memory` row records its provenance in `sources: [<Document _id>]`. Documents, chunks
(`rag/load.py`) and edges (`pipeline.py:1452`, `PydanticObjectId(raw.document_id)`) store
**ObjectIds**; entity nodes store **hex strings**, because `add_entity(source_id: str)`
(`memory/graph/add_entity.py:82/346/423/469/498/556`) unions the raw string into `sources` via
`_sources_union_expr` (l.667) and the pipeline passes the string `raw.document_id`
(`pipeline.py:1626`). Local evidence (read-only count of `sources` element types): documents 4,
chunks 116, edges 232 → `objectId`; entities (object 27, fact 12, organization 7, person 4) →
`string`; no mixed rows.

Mongo never matches an ObjectId against its hex string, so any `{"sources": {"$in": [ObjectId]}}`
silently drops every entity. Task 165 found it and works around it in
`retrieval.py::fetch_full_graph` (both forms in the `$in` list, ranks keyed on `str(id)`,
l.371–481). `sharding.py:153` (pending-documents check) only works because chunk rows carry
ObjectIds. Fix the writer so the collection has ONE provenance type, then delete the workaround.

**No migration.** The data is disposable: the local database is dropped and rebuilt from scratch
with the fixed writer (human decision). No migration script, no startup conversion, no
dual-type read kept "for old rows".

### Writer
- `add_entity.py`: every `source_id` parameter becomes `source_id: PydanticObjectId` (docstrings:
  "the source Document's ObjectId"); `_sources_union_expr(source_id: PydanticObjectId)` unions
  the ObjectId. The extraction call site (`pipeline.py:1626`) passes
  `PydanticObjectId(source_document_id)` (convert once, where `source_document_ids` is already
  built at l.1452 if that is the cleaner seam).
- `review/core.py:500` (entity merge): today it passes `str(loser_sources[0])` or the synthetic
  `f"merge:{loser_id}"`. Pass `loser_sources[0]` unchanged (already an ObjectId after this
  task); when the loser has NO sources, do not invent one — skip the provenance union for that
  merge (make `source_id` optional on the merge path, or branch before calling) so `sources`
  only ever holds Document ObjectIds. The post-merge `$setUnion` of the remaining loser sources
  (l.505–520) stays.
- Grep `apps/memory/src` for any other writer of `sources` (`preference_supersession.py`,
  `entities/users.py`, `rag/load.py`, `pipeline.py`) and confirm each writes ObjectIds or `[]`.

### Read side — remove the 165 workaround
- `retrieval.py::fetch_full_graph`: `provenance_rank: dict[PydanticObjectId, int]` keyed on the
  ObjectId; the `$in` lists carry ObjectIds only; `_rank_by_provenance` compares ObjectIds;
  delete the hex-string comment (l.428) and the `str(...)` conversions. Still exactly 4 finds.
- `sharding.py:153`: unchanged logic, but it now holds by construction — add a one-line comment
  that every `sources` element is a Document ObjectId.

### Tests (`/squid-testing-python`)
- `add_entity` (insert, each merge strategy, alias/union paths): the stored `sources` elements
  are `ObjectId` instances, never `str` (assert `isinstance`); the `$setUnion` dedupes the same
  document across two extractions.
- Entity merge in `review/core.py`: winner `sources` = union of both ObjectId lists; a loser with
  no sources adds nothing (no `"merge:"` string anywhere).
- `fetch_full_graph`: rewrite the 165 dual-form test(s) into "entities carry ObjectIds and are
  returned by the plain ObjectId `$in`"; keep the cap/order/leak/no-provenance tests green
  unchanged; fake rows built by `conftest.py` helpers use ObjectIds for entities too.
- A guard test: `grep`-style assertion (or a fixture scan) that no test helper builds an entity
  row with string `sources`.

### Local reset + verification (orchestrator-approved, LOCAL ONLY)
1. `make env-status` → `local` (STOP if it prints prod).
2. Drop the local database:
   `mongosh "mongodb://$MONGO_INITDB_ROOT_USERNAME:$MONGO_INITDB_ROOT_PASSWORD@localhost:$MONGO_PORT/?directConnection=true" --quiet --eval 'db.getSiblingDB("tree").dropDatabase()'`.
   This also clears the `documents.user_source_uri_unique` index conflict with
   `feat/document-unique-key` for as long as only this branch boots against it — note in the Log
   which branch's index shape exists afterwards.
3. Re-create the user (`make memory-signup USER_IDENTIFIER=p.b.iusztin@gmail.com NAME="Paul Iusztin"`),
   serve workflows FROM THIS WORKTREE (`make memory-serve-workflows &`; the Docker
   `prefect-worker` runs main's image — stop it for the run), ingest a light corpus
   (`make memory-run-pipeline SOURCE_FILE=sources/light.yaml`, or a 2–3 article subset if
   time-boxed) through data → memory → indexing.
4. Read-only check: every `memory` row's `sources` elements are `objectId`
   (`$unwind` + `$type` group → no `string`); `fetch_full_graph` returns every entity of the
   included documents; `make memory-query-graph` (full graph) and one `QUERY=...` render boot in
   headless Chrome as in 162–166.
5. Stop the serve process; restart `tree-prefect-worker` only if it was running before.

## Out of scope
- Any migration of existing rows, or conversion of string `sources` on read.
- Prod data (not touched by this task).
- Changing the `sources` cap, adding a `(user_id, sources)` index.

## Acceptance Criteria

- [ ] `add_entity` and the entity-merge path store `sources` elements as ObjectIds only; no
      `str` and no `"merge:"` sentinel can be written (unit tests).
- [ ] `fetch_full_graph` queries `sources` with ObjectIds only; the hex-string workaround and its
      comment are gone (`grep -n "str(source)\|hex string" apps/memory/src/tree/memory/graph/retrieval.py` → 0);
      cap/order/leak/no-provenance tests still green; still 4 finds (unit tests).
- [ ] Local DB dropped (env-status local, logged) and rebuilt with the fixed writer; read-only
      `$type` count over `memory.sources` shows only `objectId` — output pasted in `## Log`.
- [ ] Full graph and a query render from the rebuilt DB boot headless (`data-layout="live"`), and the
      full graph includes every entity of its documents (count pasted in `## Log`).
- [ ] `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green.

## User Stories

### Story: Operator asks for one document's memory
1. A future feature reads "everything extracted from document X" with `{"sources": {"$in": [X]}}`.
2. It gets the document, its chunks, its edges AND its entities — no silent gaps, no dual-form query.

### Story: Operator rebuilds local memory
1. `make env-status` → local; drop `tree`; signup; ingest `sources/light.yaml`.
2. Every `memory` row's provenance is an ObjectId; the full graph shows every extracted entity.

---

Blocked by: (none — 165 is committed)

## Log

### [Orchestrator] 2026-10-02 — Drafted
Found during task 165 QA (entity `sources` hex strings vs ObjectIds everywhere else). Human
decision: no migration — drop the local `tree` database and rebuild from scratch with the fix.
