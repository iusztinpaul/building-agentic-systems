---
id: 167-entity-sources-as-objectids
status: done
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

- [x] `add_entity` and the entity-merge path store `sources` elements as ObjectIds only; no
      `str` and no `"merge:"` sentinel can be written (unit tests).
- [x] `fetch_full_graph` queries `sources` with ObjectIds only; the hex-string workaround and its
      comment are gone (`grep -n "str(source)\|hex string" apps/memory/src/tree/memory/graph/retrieval.py` → 0);
      cap/order/leak/no-provenance tests still green; still 4 finds (unit tests).
- [x] Local DB dropped (env-status local, logged) and rebuilt with the fixed writer; read-only
      `$type` count over `memory.sources` shows only `objectId` — output pasted in `## Log`.
- [x] Full graph and a query render from the rebuilt DB boot headless (`data-layout="live"`), and the
      full graph includes every entity of its documents (count pasted in `## Log`).
- [x] `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green.

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

### [SWE] 2026-10-02 08:30 — Implementation

**Files modified**
- `apps/memory/src/tree/memory/graph/add_entity.py` — `add_entity` / `_upsert_node` take `source_id: PydanticObjectId`; `_apply_merge` + the 3 strategy builders take `ObjectId | None` (`None` = no `sources` key in the `$set`, via `_sources_union_stage`); `_upsert_node` now calls `_sources_union_expr` instead of its inlined copy, so ONE guard covers insert + every merge: `_sources_union_expr` raises `TypeError` unless `isinstance(source_id, bson.ObjectId)`.
- `apps/memory/src/tree/memory/pipeline.py` — `_dispatch_entity_write` passes `PydanticObjectId(source_document_id)` (the string stays for `make_entity_key`).
- `apps/memory/src/tree/memory/graph/review/core.py` — entity merge passes `loser_sources[0]` unchanged, or `None` when the loser has no sources (the `f"merge:{loser_id}"` sentinel is gone); the post-merge `$setUnion` (len > 1) stays.
- `apps/memory/src/tree/memory/graph/retrieval.py` — `fetch_full_graph`: `provenance_rank: dict[PydanticObjectId, int]`, `$in` = `list(provenance_rank)` (ObjectIds, once each), `_rank_by_provenance` compares ObjectIds; hex-string comment + `str(...)` removed. Still 4 finds.
- `apps/memory/src/tree/memory/graph/sharding.py` — one-line comment at the pending-documents check.
- `apps/memory/tests/unit/memory/graph/test_add_entity.py` — 24 `source_id="src1"` → `_SOURCE_ID` ObjectId; new `TestAddEntitySourcesAreObjectIds` (insert short-circuit, none/flagged insert, 3 merge strategies: `$setUnion` operand is `[ObjectId]` over `$ifNull: ["$sources", []]`; hex string rejected with `TypeError` before any `update_one` on insert + 3 merges; `_apply_merge(source_id=None)` writes no `sources` for all 3 strategies).
- `apps/memory/tests/unit/memory/graph/review/test_core.py` — `TestConfirmMergeProvenance` (`_handle_confirm`, mocked collection, `_transfer_edges` patched): loser `[a, b]` → winner unions `[[a], [a, b]]`, all `ObjectId`; loser `[]` → one winner write, no `sources`, no `"merge:"` in any call.
- `apps/memory/tests/unit/memory/graph/test_retrieval.py` — hex-string regression rewritten as `test_entities_carry_objectids_and_match_the_plain_objectid_in`; `str(src)` entity fixtures → ObjectIds; the 4-finds test expects exactly the kept ObjectIds; cap/order/leak/no-provenance tests unchanged and green. New `TestFakeCollectionProvenance` guard test.
- `apps/memory/tests/unit/memory/conftest.py` — `FakeMemoryCollection.__init__` raises on any row whose `sources` holds a non-`ObjectId` (the guard: no fixture can build string `sources`).
- `apps/memory/tests/unit/memory/test_pipeline.py` — `"d1"` → 24-hex `_DOCUMENT_ID` in the dispatch test; new `TestDispatchEntityWriteProvenance` (add_entity receives `PydanticObjectId(_DOCUMENT_ID)`).

**Tests**
- Unit: 4430 passed, 0 failed — `make memory-tests`.
- Red confirmed: with the guard in, the 24 `"src1"` call sites raised `TypeError`; the 2 merge tests fail on HEAD's `core.py` (`TypeError ... 'merge:person:alyce'`); the dispatch test hit `InvalidId: 'd1'` before the id fix.
- Integration: N/A (no suite by design); e2e below.

**Acceptance criteria**
- [x] ObjectIds only, no `str` / `"merge:"` — `test_add_entity.py::TestAddEntitySourcesAreObjectIds::*`, `review/test_core.py::TestConfirmMergeProvenance::*`, `test_pipeline.py::TestDispatchEntityWriteProvenance`.
- [x] `fetch_full_graph` ObjectIds only — `grep -n "str(source)\|hex string" apps/memory/src/tree/memory/graph/retrieval.py` → 0 lines; `test_retrieval.py::TestFetchFullGraph::*` (4 finds asserted).
- [x] Local DB dropped + rebuilt; `$type` → only `objectId` (below).
- [x] Full graph + query render boot headless; full graph includes every entity (below).
- [x] format-check / lint-check / pre-commit / memory-tests green.

**Writer audit (`sources` writers in `apps/memory/src`)**: `rag/load.py:283` `[PydanticObjectId(source_document_id)]`; `pipeline.py:1452/1720` edges `source_document_ids` (ObjectIds); `review/core.py:778` edge transfer copies stored ObjectIds; `[]` only: `preference_supersession.py:602/782`, `entities/users.py:122`, `add_entity.py` same_as edge.

**Evidence**
```
$ make memory-format-fix && make memory-lint-fix && make memory-format-check && make memory-lint-check && make pre-commit
All checks passed! / 318 files already formatted / All checks passed!
prettier Passed | ruff check Passed | ruff format Passed | biome check (harness) Passed
$ make memory-tests
============================ 4430 passed in 53.41s =============================

# Local reset (run as ONE guarded command; twice — see Notes)
$ make env-status && make env-status | grep -q "Env target: local (.env)" && . ./.env && [ "$MONGO_HOST" = localhost ] \
    && mongosh "mongodb://…@localhost:27017/?directConnection=true" --eval 'db.getSiblingDB("tree").dropDatabase()'
Env target: local (.env)
URI host: localhost:27017
{ ok: 1, dropped: 'tree' }
$ make memory-signup USER_IDENTIFIER=p.b.iusztin@gmail.com NAME="Paul Iusztin"   -> id=6abf6a9a2591e6feb90ddf5e
$ make memory-serve-workflows &     # FROM THIS WORKTREE (.venv of building-agentic-systems-dynamic-graph-viz)
$ make memory-run-pipeline SOURCE_FILE=<scratchpad>/167/light3.yaml    # memory-for-ai-agents, context-engineering, ai-agents-planning
extraction: shards=1 succeeded=1 failed=0 | indexing: embedded=1 | Done. Flow completed successfully.

# Read-only: sources element $type, grouped by kind/type (unwind)
edge mentions objectId 15 | edge next objectId 216 | edge part_of objectId 240 | edge related_to objectId 15
node chunk objectId 80 | node document objectId 3 | node event objectId 2 | node fact objectId 2
node object objectId 10 | node organization objectId 6 | node person objectId 5 | node preference objectId 2
overall: {"_id":"objectId","n":596}
rows with string sources: 0 | rows whose sources hold a duplicate: 0 | document rows with >1 source: 0

# Full graph + query (plain make targets, no read-only wrapper — no index conflict on the fresh DB)
$ make memory-query-graph
Full graph: embedded 3 of 3 documents (most recent first) → 105 nodes, 162 edges   -> graph-20261002-082726.html
mongo entities: 22 | in full graph: 22 | missing: []
$ make memory-query-graph MAX_DOCS=1
Full graph: embedded 1 of 3 documents → 39 nodes, 55 edges   -> graph-20261002-082944.html
most recent doc (how-does-memory-for-ai-agents-work): its entities 12 | in graph 12 | missing: []
$ make memory-query-graph QUERY="how does memory for ai agents work"
Graph expansion: 4 seed(s) → 69 nodes, 68 edges (1 hops)   -> how-does-memory-for-ai-agents-work-20261002-082730.html

# headless --dump-dom (162's command, 60 s alarm)
graph-20261002-082726        <body data-layout="live" data-docs="3/3" data-sim="running"> | 3 of 3 documents · 105 nodes · 162 edges | legend
how-does-memory-…-082730     <body data-layout="live" data-sim="running"> | 69 nodes · 68 edges | legend

# documents index after the rebuild (this branch / main shape)
{"name":"user_source_uri_unique","key":{"user_id":1,"source_type":1,"source_uri":1},"unique":true}
```

**Notes**
- Type deviation (deliberate): public `add_entity` / `_upsert_node` take `PydanticObjectId` as specced; the merge internals take `bson.ObjectId | None` and the guard checks `bson.ObjectId`, because the merge path passes `loser["sources"][0]` straight from Mongo (a plain `bson.ObjectId`; `PydanticObjectId` is its subclass).
- Local DB dropped TWICE (both guarded, env-status local, localhost). First rebuild (user 6abf688a…) checked clean: 600 elements, all `objectId`, 28/28 entities in the full graph (`graph-20261002-081916.html`, `how-does-memory-for-ai-agents-work-20261002-081920.html`). I then re-extracted one document (`make memory-run-memory-pipeline SOURCE_URIS=…/how-does-memory-for-ai-agents-work`) to prove cross-extraction dedup: before `{"rows":272,"srcElems":600,"dupRows":0,"strRows":0}` → after `{"rows":279,"srcElems":658,"dupRows":0,"strRows":0}`. That run exposed a PRE-EXISTING bug (below) that left the DB in a state where the full graph lost a document's subgraph (`graph-20261002-082258.html`: 72 nodes, 15 of 31 entities missing), so I dropped and rebuilt again to leave the specified clean state; all evidence above is from that second build.
- `documents` index now has this branch's `{user_id, source_type, source_uri}` shape; booting `feat/document-unique-key` (`(user_id, source_uri)`) against this DB will hit the `user_source_uri_unique` name conflict again.
- `tree-prefect-worker` was already `Exited (0) 21 hours ago` before the run, so it was neither stopped nor restarted. The serve process is stopped (no `tree.orchestrator` left).
- `make memory-query-graph` opens the browser (documented behaviour). Renders from this task (all under `apps/memory/.tree/graphs/`, no existing file touched): `graph-20261002-081916.html`, `how-does-memory-for-ai-agents-work-20261002-081920.html` (first clean build); `graph-20261002-082258.html` (polluted state, see bug 1); `graph-20261002-082726.html`, `how-does-memory-for-ai-agents-work-20261002-082730.html`, `graph-20261002-082944.html` (final build).

**Adjacent issues (not fixed, out of scope; for the orchestrator to file)**
1. Duplicate-URI Documents break `fetch_full_graph`. The data phase writes a `latent` stub Document for every linked URL, including the three ingested articles (e.g. `5962` latent and `59a5` substack for the same `source_uri`). The unique index includes `source_type`, so both rows exist. `SOURCE_URIS=` re-extraction "resolved 1 source_uris to 2 document_ids", and the memory `document` row's `sources` became `[5962, 59a5]`. `fetch_full_graph` seeds a document's provenance from `row["sources"][0]` (165 logic, kept), so after that the stub id was the seed and the real document's chunks and entities dropped out. `feat/document-unique-key`'s `(user_id, source_uri)` key addresses the root cause. Seeding from every element of the document row's `sources` would also be a one-line hardening.
2. Every edge carries every document of its extraction shard: `part_of` has 80 rows but 240 ObjectId elements (`pipeline.py:1452`, `source_document_ids` over all raws). So `{"sources": {"$in": [X]}}` over-includes other documents' edges.
3. A dangling `related_to` edge in the first build: it points to `…:person:paul iusztin` while the stored node is `…:person:paul-iusztin` (endpoint normalisation mismatch). `fetch_full_graph` drops it correctly.

### [Tester] 2026-10-02 11:40 — QA

**Test summary**
- Format / lint / pre-commit: PASS (318 files formatted; ruff, prettier, biome all Passed)
- Unit tests: 4430 passed / 0 failed — `make memory-tests` run twice (65.5 s, 65.4 s)
- Integration tests: N/A (no suite by design)
- Warnings: 0 pytest warnings (only the third-party opik/pydantic-v1 import UserWarning at process start)
- `make env-status` → `local (.env)`; Mongo touched read-only (find / aggregate / countDocuments only).

**E2E adversarial pass**
- Read-only `$type` over `memory.sources` (`$unwind` + group on `$type`) → `[{_id:'objectId', n:596}]`; rows with string `sources` 0; `$elemMatch $type string` 0; nodes missing the `sources` key 0. PASS
- Full-graph completeness (script calling the real `fetch_full_graph` against the rebuilt DB, 22 entities, 3 documents):
  max_docs=1 → 39 nodes/55 edges, entities with sources ⊆ kept docs 8, with any source in kept docs 12, missing 0, leaked (no kept source) 0;
  max_docs=2 → 84/125, subset 18, any 21, missing 0, leaked 0;
  max_docs=3 → 105/162, subset 22, any 22, missing 0, leaked 0. `doc_rank None` 0, no edge with a missing endpoint at any setting. PASS
- Static: `grep -n "str(source)\|hex string" retrieval.py` → 0; `grep -rn "merge:" apps/memory/src` → 0; `fetch_full_graph` has exactly 4 `.find(` (retrieval.py:420/438/451/471). PASS
- Callers audit (src, scripts/, deploy/, harness has none): the ONLY external caller of `add_entity` is `pipeline.py:1617` (`source_id=PydanticObjectId(source_document_id)`, `source_document_id` = `raw.document_id` str, always a 24-hex Document id; a non-hex id would raise `InvalidId` — same as the edge path at `pipeline.py:1452`). The ONLY external caller of `_apply_merge` is `review/core.py:492` (`loser_sources[0]` from Mongo, or `None`). `_upsert_node` / `_merge_*` / `_sources_union_*` are internal to `add_entity.py` and all fed from those two. No script, dream/consolidation, mcp or preference path calls `add_entity`. No caller passes a str. PASS
- Other `sources` writers: `rag/load.py:283` `[PydanticObjectId(...)]`; `pipeline.py:1720` edges `source_document_ids` (ObjectIds); `review/core.py:513/778` copy stored values (ObjectIds on a clean DB); `[]`: `preference_supersession.py:602/782`, `entities/users.py:122`. `preference_supersession.py:571` upserts a PREFERENCE node with NO `sources` key in `$setOnInsert` (pre-existing, writes no string; see note 4). No string writer remains. PASS
- Adversarial unit-level (mock collection, script in scratchpad, zero DB writes):
  - `add_entity` source_id ObjectId → OK; PydanticObjectId → OK; str hex / `"src1"` / None / bytes(12) / int / list → `TypeError: sources holds Document ObjectIds only; got <type>`, 0 `update_one` calls (nothing written). PASS
  - `_apply_merge(source_id=None)` all 3 strategies → OK, no `sources` key in `$set`. PASS
  - `_handle_confirm` loser `[]` (all 3 strategies) → OK, 3 writes, no `sources` written, no `merge:`; loser `None` sources → same. PASS
  - Legacy loser `["legacy-hex"]` or `["legacy-hex", oid]` → `TypeError` raised before any `update_one` (0 calls): the review flow fails loudly with no partial merge/tombstone. PASS (see note 1 for the mixed hole).
  - `fetch_full_graph` on: empty collection, document with `sources: []`, document missing `sources`, entity with `sources: []` + doc, entity with legacy string sources → no crash; doc embedded alone; string-sources entity silently excluded (expected under "no dual-type read"). PASS
- Headless smoke (`chrome --headless=new --enable-unsafe-swiftshader --use-angle=swiftshader --virtual-time-budget=15000 --dump-dom`, 30 s kill; 0 "Uncaught" in console):
  `graph-20261002-082726.html` → `<body data-layout="live" data-docs="3/3" data-sim="running">`, "3 of 3 documents · 105 nodes · 162 edges";
  `how-does-memory-for-ai-agents-work-20261002-082730.html` → `data-layout="live"`, "69 nodes · 68 edges";
  `graph-20261002-082944.html` → `data-layout="live" data-docs="1/1"`, "1 of 1 documents · 39 nodes · 55 edges". PASS
  (First attempt with `--disable-gpu` gave `blendFunc of null` from sigma = no WebGL context in my flags, not a page defect; fixed with swiftshader flags.)

**Acceptance criteria**
- [x] PASS — `add_entity` + entity-merge store ObjectIds only, no `str` / `"merge:"` — `_sources_union_expr` guard (add_entity.py:~670) covers insert + 3 merge strategies; `TestAddEntitySourcesAreObjectIds`, `TestConfirmMergeProvenance`, `TestDispatchEntityWriteProvenance` pass; my adversarial run above.
- [x] PASS — `fetch_full_graph` ObjectIds only; grep → 0; 4 finds; cap/order/leak/no-provenance tests green — retrieval.py:372-385, 428-434.
- [x] PASS — local DB rebuilt, `$type` → only `objectId` (596) — my own re-run of the aggregation.
- [x] PASS — full graph + query render boot headless `data-layout="live"`; full graph includes every entity (22/22 at 3 docs; also complete at 1 and 2) — above.
- [x] PASS — format-check / lint-check / pre-commit / memory-tests green.

**Out-of-scope issues the SWE flagged — read-only confirmation (not failing 167)**
1. CONFIRMED (cause), not currently manifest: `documents` holds 70 `latent` + 3 `substack` rows and each of the 3 article URIs exists twice (`latent` + `substack`; `source_type` is part of this branch's unique index). The memory `document` rows' `sources` are currently single-element, pointing at the substack ids (`…adda03/04/05`), so the full graph is correct now; the 2-source / `sources[0]` seed hazard appears only after a `SOURCE_URIS=` re-extraction resolves both ids. `feat/document-unique-key` addresses it.
2. CONFIRMED: every `part_of` (80), `next` (72), `mentions` (5), `related_to` (5) edge has `len(sources) = 3` = all documents of the shard; `has` edges have 0. `{"sources": {"$in": [X]}}` therefore over-includes other documents' edges.
3. CONFIRMED: 2 dangling edges, both `person:paul iusztin|related_to|…` (source `…:person:paul iusztin` absent; stored node slug is `paul-iusztin`; targets exist). `fetch_full_graph` drops them (0 edges with a missing endpoint in the 3 runs).

**Other issues found (non-blocking, PASS with note)**
1. Guard hole on the legacy path: `review/core.py` post-merge `$setUnion` of `list(loser_sources)` (len > 1) is not routed through `_sources_union_expr`, so a loser `[ObjectId, "legacy-str"]` writes the string to the winner (verified: sources write `[oid, 'legacy-hex']`). Unreachable on a rebuilt DB (all ObjectId, per spec legacy rows are dropped, no migration), but it contradicts "no str can be written" for legacy data and is inconsistent with a pure-legacy loser which raises. One-line option: validate `all(isinstance(s, ObjectId) for s in loser_sources)` at the top of `_handle_confirm` (raise before any write), or accept as is.
2. `_dispatch_entity_write` converts with `PydanticObjectId(source_document_id)` per call; a malformed `raw.document_id` raises `InvalidId` at entity-write time rather than at the shard boundary (same conversion already exists at pipeline.py:1452, so no new failure class).
3. `fetch_full_graph` hard-seeds a document from `row["sources"][0]` only (SWE's issue 1 hardening: seed from every element).
4. `preference_supersession.py:571` creates a node row without a `sources` field; `fetch_full_graph` never returns such a node via `{"sources": {"$in": …}}` nor `{"sources": []}` (missing ≠ `[]`). Pre-existing, not a string writer; DB currently has 0 such rows.

**VERDICT: PASS**
