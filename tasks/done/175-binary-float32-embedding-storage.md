---
id: 175-binary-float32-embedding-storage
status: done
feature: prod-backfill-hardening
---

# Store `memory.embedding` as BSON float32 `binData` (≈3× smaller)

Tags: `memory`, `indexing`, `mongodb`, `storage`
Depends on: —
Blocks: the prod rag rebuild

## Problem

Prod runs on Atlas M0 (512 MB cap). A 1024-dim vector stored as a BSON array of doubles costs
≈13 KB per row (8-byte double + per-element key). Loading 4,376 of 10,081 documents already took
`memory` to 134 MB data / 265 MB storage / 52 MB indexes, and the full backfill dies with
`AutoReconnect` at the same row count every time. Atlas Vector Search accepts float32 `binData`
vectors (`bson.binary.Binary.from_vector(v, BinaryVectorDtype.FLOAT32)`, pymongo 4.16 in `uv.lock`):
1024 × 4 B ≈ 4 KB per row.

Databases start from scratch (no migration of list-stored rows; prod gets `make memory-reset-mode`).

## Scope

1. **One write boundary + one read boundary**, in `apps/memory/src/tree/entities/memory.py` (or a
   small neighbouring module): `to_stored_vector(list[float]) -> Binary` (FLOAT32) and
   `from_stored_vector(Binary | list | None) -> list[float]`. Every persisted vector goes through the
   former; every Python read that USES the numbers goes through the latter. Prefect task outputs
   (`embed_children`, `embed_entities`, `EmbeddingMap.vectors`, 90-day caches) stay `list[float]` —
   convert only at the Mongo write.
2. **New pending marker: the field is absent / `null`, never `[]`.** Rows written without a vector
   (documents, parents, the self-person seed, unembedded children) omit `embedding` or set `None`.
   Queries: pending = `{"embedding": None}`; embedded = `{"embedding": {"$type": "binData"}}`.
   Replace every array-semantics query: `rag/indexing.py` `_backfill_filter` (`$in: [[], None]`) and
   `_reset_filter` (`$nin`), `reset_embeddings` (`$set: []` → `$unset`), `clustering/store.py`
   `_CHILD_FILTER` (`embedding.0`), `graph/consolidation/dream.py` (`$not: {$size: 0}`).
3. **Writes:** `rag/load.py` (document/parent/child rows, `_build_node_op` — wrap a `Binary` in
   `$literal` if needed inside pipeline updates), `rag/indexing.py` backfill `_embed_batch`,
   `graph/add_entity.py` `_upsert_node` (`$ifNull`), `graph/preference_supersession.py`,
   `entities/users.py` self-person seed.
4. **Reads:** `MemoryEntry.embedding` becomes `list[float] | None` with a before-validator that decodes
   `Binary` (so every Beanie `find` / `model_validate` in `graph/kgquery.py` keeps working);
   `clustering/store.py` load (today `list(document.get("embedding") or [])` would yield byte ints);
   `dream.py` (passes the stored vector back as `queryVector` — a `Binary` is a valid queryVector, or
   decode; fix the truthiness check). Paths that strip `embedding` before JSON
   (`MODEL_HIDDEN_KEYS`, `NO_EMBEDDING`, `nl_query` `$project`) must keep stripping it.
5. Vector index definition (`rag/indexing.py` `_build_vector_index_definition`) is unchanged
   (same path, `numDimensions`, cosine) — confirm mongot accepts binData locally.
6. Tests: update `tests/unit/memory/conftest.py` `FakeMemoryCollection` and every test that pins `[]`
   / list storage (≈13 files listed by grep for `"embedding": []`, `embedding.0`, `$size`, `$nin`).
7. Docs: ADR-006 / glossary / README sentences that say "parents are written with `embedding: []`"
   → absent/null; add a one-line note on the binData storage + why (M0 cap) where the embedding
   storage is described.

## Acceptance criteria

- [x] Unit: every persisted vector is a `Binary` with subtype 9 / dtype FLOAT32 and round-trips to the
      same floats (within float32 precision).
- [x] Unit: rows without a vector have no `embedding` (or `null`); backfill, reset, clustering load and
      dream select exactly the right rows under the new markers.
- [x] Unit: `MemoryEntry.model_validate` accepts a `Binary` embedding and exposes `list[float]`.
- [x] `make memory-format-check`, `make memory-lint-check`, `make pre-commit`, `make memory-tests` green.
- [x] Local e2e (LOCAL env only — never prod), rag mode, from a reset `memory`: run the pipeline over a
      few articles; mongosh shows child `embedding` `$type: "binData"`, parents/documents without it,
      `Object.bsonsize` of a child row ≈ 4–5 KB (was ≈13 KB); `make memory-search QUERY=…` returns
      vector hits (`search_mode` not `text_only`); `make memory-run-clustering-pipeline` +
      `make memory-visualize-embeddings` work; `make memory-reset-embeddings CONFIRM=yes` then indexing
      re-embeds the same count.
      *(SWE: measured child row 5.0–6.0 KB, avg 5.7 KB — DEVIATION from "4–5 KB"; the vector field
      itself is 4098 B binData vs ≈13 KB as an array; the other ≈1.5 KB is denormalised content,
      the same overhead as before (14.7 − ~13.2 KB). For the Tester / PA to rule on.)*
- [x] Local e2e graphrag smoke (switch with `make memory-reset-mode`, restore rag after): entity rows
      get binData vectors and dedup `$vectorSearch` still resolves duplicates.

## Log

### [SWE] 2026-10-03 00:50 — Implementation

**Design**
- One write boundary + one read boundary in `tree.entities.memory`: `to_stored_vector(list[float]) -> Binary` (float32, subtype 9; raises on `[]`), `from_stored_vector(Binary | list | None) -> list[float]` (rejects non-subtype-9 / non-FLOAT32 Binary; `None` -> `[]`), and `STORED_VECTOR_QUERY = {"$type": "binData"}`. `MemoryEntry.embedding` is now `list[float] | None = None` with a before-validator that decodes `Binary`.
- Pending = field absent/null (`{"embedding": None}`); embedded = `STORED_VECTOR_QUERY`. Loader writes `"$$REMOVE"` for vector-less rows (pipeline `$set`; a `Binary` is a constant there, no `$literal` needed — verified on local Mongo 8.2.5); reset uses `$unset`.
- Prefect outputs / caches / `EmbeddingMap` stay `list[float]`; only the Mongo writes convert. `queryVector`s stay `list[float]`; the vector index definition is unchanged (local mongot 0.60.1 indexes + searches binData, probed before coding).

**Files modified**
- `apps/memory/src/tree/entities/memory.py` — boundary helpers, `STORED_VECTOR_QUERY`, `embedding: list[float] | None` + Binary-decoding validator.
- `apps/memory/src/tree/memory/rag/load.py` — child vectors as binData; documents/parents/unembedded children `$$REMOVE`.
- `apps/memory/src/tree/memory/rag/indexing.py` — backfill filter `embedding: None`, writes binData; reset filter `STORED_VECTOR_QUERY`, reset `$unset`.
- `apps/memory/src/tree/memory/clustering/store.py` — `_CHILD_FILTER` uses `STORED_VECTOR_QUERY`; load decodes via `from_stored_vector` (was `list(Binary)` -> byte ints).
- `apps/memory/src/tree/memory/graph/consolidation/dream.py` — driving query `STORED_VECTOR_QUERY`; stored vector decoded before `dedupe_entity`.
- `apps/memory/src/tree/memory/graph/add_entity.py` — `$ifNull` with binData, only for a non-empty vector.
- `apps/memory/src/tree/memory/graph/preference_supersession.py` — binData write.
- `apps/memory/src/tree/entities/users.py` — self-person seed has no `embedding`.
- `apps/memory/src/tree/memory/{pipeline.py,rag/search.py}` — docstrings only.
- `apps/memory/src/tree/memory/graph/nl_query.py` — the model-facing schema line said `embedding` is a "float array"; now says float32 binData, never filter with `$size` / `embedding.0`.
- Docs (authorised by scope item 7 of this task): `docs/glossary.md` (Child chunk, Embedding reset, Parent chunk), `docs/adrs/006_…` §4, `docs/adrs/009_…` diagram label, `apps/memory/README.md` (Memory indexing + reset wording), `.agents/skills/run-pipelines-e2e/SKILL.md` (verification line said `embedding: []`).
- Tests: `tests/unit/memory/conftest.py` (`$type: binData` in `matches`, no default `embedding`, binData fixtures), `entities/test_memory.py`, `entities/test_users.py`, `mcp/test_graph_tools.py`, `mcp/test_deep_search.py`, `memory/rag/test_load.py`, `memory/rag/test_indexing.py`, `memory/clustering/test_store.py`, `memory/graph/consolidation/test_dream.py`, `memory/graph/test_add_entity.py`, `memory/graph/test_preference_supersession.py`, `memory/test_pipeline.py`.

**Tests**
- Unit: 4701 passing, 0 failing (`make memory-tests`, local env). New: `TestStoredVector` (12), `TestStoredVectorsInMongo` (live: only the embedded child is binData, re-upsert without a vector removes it), `TestStoredVectorMarkersLive` (live: backfill selects absent + null only; reset -> backfill round trip writes binData), clustering decode + absent-vector skip, dream driving query, self-seed without embedding, binData strip in `_serialize` and deep-search files.
- Integration: N/A (no suite).

**Acceptance criteria**
- [x] binData subtype 9 / FLOAT32 + round trip — `test_memory.py::TestStoredVector`, `test_load.py::TestStoredVectorsInMongo`, `test_pipeline.py::TestWorkerRowShape::test_only_children_carry_a_vector`
- [x] no-vector rows absent; backfill/reset/clustering/dream selection — `test_indexing.py::TestStoredVectorMarkersLive`, `TestResetEmbeddings`, `test_store.py::TestLoadChildEmbeddings`, `test_dream.py::test_driving_query_selects_stored_vectors_only`
- [x] `MemoryEntry.model_validate` decodes Binary — `TestStoredVector::test_model_validate_decodes_a_binary_embedding`
- [x] format/lint/pre-commit/tests green
- [x] Local e2e rag — see Evidence
- [x] Local e2e graphrag smoke — see Evidence

**Evidence** (LOCAL env only; `make env-status` -> `local (.env)` before every DB op)
```
$ make memory-format-check && make memory-lint-check && make memory-tests && make pre-commit   (after ALL edits, docs included)
329 files already formatted
All checks passed!
======================= 4701 passed in 60.36s (0:01:00) ========================
prettier Passed · ruff check Passed · ruff format Passed · biome check (harness) Passed

# BEFORE (task-174 corpus, list storage, captured before the reset)
child $bsonSize: 14406 / 14406 / 14702, embType array, dims 1024; avg over 17303 children 14739.7 B

$ make memory-reset-mode CONFIRM=yes        -> Dropped memory (37558 rows)
$ make memory-run-memory-pipeline SOURCE_URIS=<5 decodingai articles>
extraction: shards=1 succeeded=1 failed=0 · indexing: embedded=0 · Completed
mongosh group by type/subtype/$type(embedding):
  chunk/child binData 142 · chunk/parent missing 11 · document missing 5
child $bsonSize 5545 / 5940 / 5951 ($binarySize(embedding)=4098); avg over 142 children 5729.4 B (min 4976, max 6046)
parents/docs with an embedding field: 0

$ make memory-search QUERY="how do coding agents sandbox shell commands" TOP_K=3
vector leg: 12 candidate(s), 12 kept at min_vector_score=0.70 (top=0.837)
[0.032] From a Raw Shell to a Sandboxed Coding Agent ...
retrieve_parents (scratch script): search_mode=hybrid outcome=found parents=3
nonsense "zzzq wamble frobnitz": search_mode=hybrid outcome=nothing_found parents=0

$ make memory-run-clustering-pipeline   (TREE_MEMORY__CLUSTERING__HDBSCAN__MIN_CLUSTER_SIZE=5 in the serve shell)
clustering: clusters=8 clustered=128 noise=14 fallbacks=0
$ scripts/visualize_embeddings.py --hulls --no-open --output <scratchpad>
Embedding map: 142 chunks in 8 clusters (+14 noise)

$ make memory-reset-embeddings CONFIRM=yes -> rows=142 (children=142); after: binData 0, children with an embedding field 0, viz 0
$ make memory-run-indexing-pipeline        -> indexing: embedded=142; after: binData 142, array 0, pending 16
search after re-index: search_mode=hybrid outcome=found

# graphrag smoke (TREE_MEMORY__MODE=graphrag; reset-mode first)
doc A: dedupe n_none=10 (index not yet built), indexing embedded=1 (person:self seed backfilled)
docs B+C: dedupe_entities n_merged=0 n_flagged=1 n_none=33; apply_writes same_as_emitted=2
  same_as pending: object:pi -> object:pydantic ai (match_type embedding)
nodes by $type(embedding): binData 135, missing 10 (7 parents + 3 documents); entity rows avg 4537 B
dream _collect_dream_candidates (decision-only): partitions=5 nodes_driven=38 pairs_examined=2 flagged=1 skipped_existing_same_as=1
  pair: object:building a coding agent from scratch ~ object:coding agent, similarity 0.871, embedding
-> serve stopped, TREE_MEMORY__MODE unset, make memory-reset-mode CONFIRM=yes (rag) -> Dropped memory (350 rows)
```

**Notes**
- Child rows are ≈5.0–6.0 KB, not 4–5 KB: the vector itself is 4098 B (was ≈12–13 KB as doubles), and the rest is the ≈1–2 KB of denormalised content/title/heading_path. Overall 14.7 KB -> 5.7 KB per child (≈2.6×).
- Beanie `insert()`/`save()` of a `MemoryEntry` would still persist `embedding` as a float ARRAY (Beanie's encoder bypasses Pydantic serializers). No production path Beanie-writes `MemoryEntry` (all writes are raw pymongo), so I did not add an encoder; test fixtures that seed via the ODM set the binData vector raw afterwards. If a Beanie write path is ever added, it must go through `to_stored_vector`.
- Existing list-stored databases are NOT migrated (per scope): a list-stored row is neither pending nor embedded under the new markers. Prod needs `make memory-reset-mode` before the rebuild.
- Local DB left in rag mode with an empty `memory` (the task-174 corpus was dropped as allowed); `documents`/`users` kept.
- `make memory-visualize-embeddings` always opens a browser, so I ran the same script directly: `scripts/visualize_embeddings.py --hulls --no-open --output <scratchpad>`.
- ADR-006 Status line not touched (PA-owned): PA please add the task-175 amendment entry. The ADR-009 diagram label and the e2e SKILL.md line are extensions of scope item 7 (they repeated `embedding=[]`) — PA may veto.
- Not committed — awaiting Tester.

### [Tester] 2026-10-03 01:15 — QA

**Test summary**
- Format / lint / pre-commit: PASS (329 files formatted, ruff clean, prettier/ruff/biome pre-commit Passed)
- Unit tests: 4701 passed / 0 failed (`make memory-tests`, local env). Integration: N/A (no suite)
- Warnings: 0 from the suite's own code; only the pre-existing opik/Pydantic-V1-on-3.14 import `UserWarning`

**E2E adversarial pass** (LOCAL only; `make env-status` -> `local (.env)` before every DB op)
- Happy path (rag): `make memory-run-memory-pipeline SOURCE_URIS=<5 decodingai articles>` -> children binData 150, parents/documents no `embedding`; `search_memory` -> `outcome=found, search_mode=hybrid`, nonsense -> `nothing_found`; clustering clusters=5 clustered=150; embedding map 150 chunks; reset-embeddings dry run rows=152 -> CONFIRM=yes -> field REMOVED (not `[]`) -> 2nd dry run rows=0 -> indexing `embedded=152` (same count) (PASS)
- Break 1 (legacy/stray rows): legacy list child, `[]`, `null`, absent inserted next to real rows -> clustering load ignores list+`[]`+null+absent (150 rows, width 1024); backfill selects exactly null+absent (2); reset selects 0 (PASS)
- Break 2 (malformed binData, subtype 0): rows with `Binary(b"\x00"*16, 0)` / `Binary(b"abc", 0)` -> `load_child_embeddings` returns widths `[1024, 16, 3]` as FLOAT lists, NO error (FAIL, see below)
- Break 3 (boundary inputs to the write/read boundary): `to_stored_vector([])`/`None` -> ValueError (PASS); ints OK; `1e39` -> OverflowError (acceptable); `str` elem -> struct.error (OK); numpy array -> "truth value of an array is ambiguous" ValueError (confusing; non-blocking); NaN/inf stored silently (pre-existing semantic; clustering has its own finite check); int8 vector and subtype!=9 `Binary` -> clear ValueError (PASS)
- Break 4 (leaks via MCP, rag + graphrag): `search_memory`, `visualize_memory_structure` (with and without query), `visualize_memory_embeddings`, graphrag `deep_search_memory`, `query_memory` (NL aggregation + "all their fields" row dump), `memory_dashboard`, generated `.tree/graphs/*.html` and `.tree/memory/<id>/` files -> 0 occurrences of `$binary` / `binData` / `"embedding"` (PASS)
- Break 5 (precision): float64->float32 cosine drift over 200 random 1024-d unit vectors = 2.2e-9 max; no 0.70 threshold flips. Same-float32 vectors stored as list vs binData in a scratch collection+index: identical ranked ids and scores for 12 queries (this isolates storage format; the drift figure is the precision evidence). Real queries: top vector score 0.837 / 0.843 unchanged; MCP server booted with binData rows (index assertion OK) (PASS)
- Break 6 (other write paths): MCP `ingest_file` (online pipeline) -> child binData, parent/document none, unicode/emoji text found by `search_memory` hybrid; `ingest_conversation` -> same shape (PASS). Real `scripts/hook_session_end.py` NOT run (it targets the configured MCP URL; port 8000 is another project's server here) - it is a thin shim over the same `ingest_conversation` tool I called.
- Break 7 (graphrag smoke, AC6): `make memory-reset-mode CONFIRM=yes` under `TREE_MEMORY__MODE=graphrag`, 3 docs then 2 docs -> all 60 entity nodes binData (avg row 4570 B, vector 4098 B); second batch `dedupe_entities n_merged=1 n_none=19` (dedup `$vectorSearch` resolves against binData); `make memory-run-dream-consolidation` (dry run) `nodes_driven=60 pairs_examined=7 auto_merged=3 flagged=4`. Reset back to rag afterwards (PASS)
- Grep of whole `src/ scripts/ deploy/ docs/ README AGENTS .agents`: no remaining `"embedding": []`, `embedding.0`, `$nin: [[]`, `$size` on embedding, or `len(...embedding)` array semantics; no Beanie `insert/save/replace` of `MemoryEntry` (the `.insert()/.replace()` hits are source `Document` classes in `data/`). Graph viz payloads whitelist fields (no raw `properties`/`embedding` dump); `_serialize`/`write_deep_search_results` strip `embedding` (PASS)

**Acceptance criteria**
- [x] PASS — Unit: persisted vectors are `Binary` subtype 9 FLOAT32, round-trip — `TestStoredVector`, `TestStoredVectorsInMongo`; live: mongosh `$type(embedding)` = binData for 17,305 children, `$binarySize`=4098
- [x] PASS — Unit: rows without a vector have no field; backfill/reset/clustering/dream markers — Break 1 above + `TestStoredVectorMarkersLive`, `test_driving_query_selects_stored_vectors_only`
- [x] PASS — `MemoryEntry.model_validate` decodes `Binary` — `TestStoredVector::test_model_validate_decodes_a_binary_embedding`
- [x] PASS — format-check / lint-check / pre-commit / memory-tests green (above)
- [x] PASS (with note on size) — Local e2e rag: see happy path. Child row size: 5,612 B average over the FULL local corpus (17,305 children), vector 4,098 B, content ~965 B, ~550 B other metadata. The AC's "≈4-5 KB" excludes the unchanged denormalised content/title/heading_path; the intent (~3x vector shrink) holds: vector 13.2 -> 4.1 KB (3.2x), whole child row 14.7 -> 5.6 KB (2.6x), whole `memory` collection data ~2.6x smaller. Ruled satisfied; no further shrink possible without un-denormalising (out of scope).
- [x] PASS — Local e2e graphrag smoke (Break 7)
- [ ] FAIL (contract of the new read boundary) — `from_stored_vector` docstring promises "a Binary that is not a float32 vector raises instead of decoding into garbage", but PyMongo decodes BSON binData subtype 0 to plain `bytes`, which skips the `isinstance(value, Binary)` guard and goes through the list branch: `from_stored_vector(b"\x01\x02") -> [1.0, 2.0]`. `STORED_VECTOR_QUERY` (`$type: binData`) matches every subtype, so a stray subtype-0 value is selected by clustering/dream and decoded to byte-valued floats (clustering then dies later in `_stack_embeddings` with a misleading "mixed widths" error, or - if a bytes value happened to be 1024 long - clusters garbage silently).
      Expected: ValueError at the boundary. Actual: silent byte->float decode.
      Fix (`apps/memory/src/tree/entities/memory.py`, `from_stored_vector`): `if isinstance(value, (bytes, bytearray, memoryview)): raise ValueError("embedding is raw binData (subtype 0), expected a float32 vector (subtype 9)")` before the list branch + one regression test in `TestStoredVector`. Not reachable from any production write path (low severity), but it breaks the boundary's stated contract.

**Prod fit estimate (measured on the full local corpus, 10,085 pending docs ~ prod's 10,081; `fsync` first, rag mode, 37,564 rows)**
```
memory rows: children 17,305 (avg 5,612 B, 97.1 MB) | parents 10,174 (avg 2,018 B, 20.5 MB) | documents 10,085 (avg 463 B, 4.7 MB)
memory:  dataSize 116.6 MiB | storageSize 98.0 MiB | indexes 74.3 MiB (text_index 71.3 MiB; _id 1.3; two compound ~0.9 each)
db tree: dataSize 135.5 MiB | storageSize 121.1 MiB | indexSize 76.2 MiB | totalSize 197.4 MiB   (documents 18.8/23.0 MiB incl.)
```
- Cap check: 135.5 MiB data + 76.2 MiB indexes = ~212 MiB logical (197 MiB on disk) vs 512 MB M0 = ~2.4x headroom (41%).
- Before (list storage, extrapolating prod's measured 4,376 docs -> 134 MB data / 265 MB storage / 52 MB indexes to 10,081 docs): ~310 MB data / ~610 MB storage / ~120 MB indexes, i.e. over the cap, consistent with the observed AutoReconnect at the cap.
- The mongot vector index lives outside mongod collection stats locally, so its footprint on Atlas M0 is not measured here. Children are 79% of data and parents (untouched by this task) are only ~17%.
- Pipeline over the whole corpus took 4m49s, `extraction ... failed=0`; `load_child_embeddings` decodes 17,305 x 1024 vectors in 1.3 s.

**Other issues found (non-blocking)**
- `to_stored_vector(np.ndarray)` raises an ambiguous truth-value ValueError (`if not vector`); contract is `list[float]` and no call site passes arrays.
- NaN/inf are stored silently (same as before).
- Known limitation confirmed: Beanie `insert()/save()` of a `MemoryEntry` would write a float array; no production path does it.
- ADR-006 amendment line for task 175 is PA-owned (per SWE note); not checked.

**Cleanup**: local DB left in rag mode, `memory` empty (555 graphrag rows dropped), `documents`/`users` kept, my 2 MCP-ingested documents deleted, no serve/MCP/orchestrator process running, scratch artefacts removed from `.tree/`.

**VERDICT: FAIL** (one small, well-scoped contract defect; everything else PASS)

### [SWE] 2026-10-02 22:17 — Fixes (Tester FAIL: raw binData decode)

**Files modified**
- `apps/memory/src/tree/entities/memory.py` — `from_stored_vector` now raises `ValueError("embedding is raw binData (subtype 0), expected a float32 vector (subtype 9)")` on plain `bytes` / `bytearray` / `memoryview` (checked before the list pass-through; `Binary` still takes its own branch). `MemoryEntry._decode_stored_embedding` routes every bytes-like value through it, so Beanie reads fail with the same message. `to_stored_vector` empty check is now `len(vector) == 0` (np.ndarray-safe).
- `apps/memory/src/tree/memory/clustering/store.py` — `load_child_embeddings` docstring documents the chosen behaviour: a non-float32 binData row FAILS the run with that ValueError (not skipped).
- `apps/memory/tests/unit/entities/test_memory.py` — TestStoredVector: `test_from_stored_vector_rejects_raw_bindata[bytes|bytearray|memoryview]`, `test_to_stored_vector_accepts_a_numpy_array`, `test_to_stored_vector_rejects_an_empty_numpy_array`, `test_model_validate_rejects_a_raw_bindata_embedding`.
- `apps/memory/tests/unit/memory/clustering/test_store.py` — `TestLoadChildEmbeddings::test_a_raw_subtype_0_vector_fails_loudly` (real Mongo, subtype-0 row → ValueError).

**Tests**
- Red first: 6 failed for the right reason (DID NOT RAISE / ambiguous truth value), then green.
- Unit: 4708 passed, 0 failing (`make memory-tests`, env local).
- Format/lint/pre-commit: clean.

**Evidence**
```
$ make memory-tests
============================ 4708 passed in 56.51s =============================
$ uv run python -c "...from_stored_vector(b'\x01\x02')..."
bytes -> embedding is raw binData (subtype 0), expected a float32 vector (subtype 9)
bytearray -> embedding is raw binData (subtype 0), expected a float32 vector (subtype 9)
memoryview -> embedding is raw binData (subtype 0), expected a float32 vector (subtype 9)
[0.25, -0.5]   # to_stored_vector(np.ndarray) round-trip
```

**Notes**
- `STORED_VECTOR_QUERY` NOT narrowed: `$type` cannot filter on binData subtype; doing so needs `$expr`/`$bitsAllSet`-style predicates that don't fit the plain-match filters (and Atlas vector-search `filter` doesn't support them). The decode guard is the enforcement point.

### [Tester] 2026-10-02 22:25 — QA re-review (raw binData fix)

**Test summary**
- Format / lint / pre-commit: PASS (329 files formatted, ruff clean, pre-commit all Passed)
- Unit tests (`make memory-tests`, env local): 4708 passed / 0 failed, 0 failures
- Integration tests: none by design (AGENTS.md)

**E2E adversarial pass (local Mongo, isolated fake ObjectId, rows removed after)**
- Break path 1 (raw subtype-0 binData child via `load_child_embeddings`): `ValueError: embedding is raw binData (subtype 0), expected a float32 vector (subtype 9)` — PASS (previously decoded to byte-int floats).
- Break path 2 (non-FLOAT32 subtype-9, INT8 vector): `ValueError: embedding is a INT8 vector, expected FLOAT32` — PASS.
- Break path 3 (`MemoryEntry.model_validate` on a raw row as read from PyMongo, type `bytes`): `ValidationError ... Value error, embedding is raw binData (subtype 0) ...` — PASS.
- Baseline float32 row in the same run decodes to `[0.5, -0.25, 0.125, 1.0]` — PASS.
- Happy path regression (rag mode, fresh user `qa175-recheck`, 2 articles via `make memory-run-pipeline MODE=online`): 38 child chunks stored as binData subtype 9 / 4098 bytes, parents + documents with no embedding; `make memory-run-clustering-pipeline` → `clusters=2 clustered=38 noise=0 fallbacks=0`; `make memory-search QUERY="how does episodic memory work"` → `vector leg: 38 candidate(s), 19 kept at min_vector_score=0.70 (top=0.809)`, 1 parent returned (hybrid, no text_only degradation) — PASS.

**Acceptance criteria**: unchanged from the previous review (all PASS); the one failed item (raw subtype-0 decode) now PASS. Full-corpus fit run not repeated per orchestrator.

**Other issues found (non-blocking)**
- `STORED_VECTOR_QUERY` still matches every binData subtype; the decode guard is the only enforcement (documented, accepted).
- A corrupt row fails the whole clustering run rather than skipping (documented in `load_child_embeddings` Raises:).

**Cleanup**: fake user `qa175-recheck` and its rows (42 memory, 2 clusters, 2 documents) deleted, `qa175-*` probe rows deleted; local `memory` empty (0), rag mode, `documents`/`users` of the original user kept (12124 docs), current user restored to `paul-iusztin-e2e`, serve process stopped.

**VERDICT: PASS**
