---
id: 161-document-unique-key-user-id-source-uri
status: pending
feature: document-unique-key
---

# Make the `documents` unique index match the Document's natural key: `(user_id, source_uri)`

Tags: `data`, `memory`, `entities`, `docs`
Depends on: None
Blocks: —
Implements: ADR-010 (supersedes ADR-001 §2 Consequences / §4 on the Document unique key only)

## Scope

`apps/memory/src/tree/entities/documents.py` declares `user_source_uri_unique` on
`(user_id, source_type, source_uri)`. Nothing else in the codebase treats `source_type`
as part of the key: every loader (`data/file/file.py:105`, `data/web/web.py:107`,
`data/substack/substack.py:107/122/138`, `data/youtube/youtube.py:437`,
`data/huggingface/arxiv_dataset.py:182`, `data/conversation/conversation.py:116`) and
`online.py:282` dedupe with `find_one({"user_id", "source_uri"})`, and a `LATENT`
placeholder is upgraded IN PLACE by rewriting its `source_type`. So the real natural key
is `(user_id, source_uri)` (as `docs/glossary.md` **Document** already says), and with
`source_type` in the key the `DuplicateKeyError` race guard in the loaders does not fire
across source types — a `LATENT` and a `WEB` row for the same URI can coexist.
`source_type` entered the key in commit `a1085c0` (multi-tenancy) by carrying over an
older shape; it was never a decision. ADR-010 records the correction.

Bias-to-least: change the index declaration; delete code the triple made necessary;
fix prose. No migration — the local DB is dropped once by hand (step 1 of the smoke).

### Code
- `src/tree/entities/documents.py`: the index becomes
  `IndexModel([("user_id", 1), ("source_uri", 1)], unique=True, name="user_source_uri_unique")`
  — SAME name. Rewrite the comment above it (lines 51-53): the same `source_uri` may be
  ingested by different users; only `(user_id, source_uri)` is unique; `source_type` is a
  row attribute a `LATENT` upgrade rewrites, not part of the key.
- `src/tree/offline.py::_resolve_source_uris`: one Document per `(user_id, source_uri)`
  now, so resolve ONE id per URI — `ids_by_uri: dict[str, str]`, `append` not `extend`.
  Delete the sentence "a URI matching several rows (the same URI under two
  `source_type`s) yields all of them" (~line 162). Keep the order-follows-the-caller rule,
  the loud `ValueError` on an unknown URI, and the
  `offline-pipeline: resolved %d source_uris to %d document_ids` log line UNCHANGED
  (task 132 uses it as a regression guard).
- `src/tree/offline.py::_validate_single_tenant_scope` docstring (~lines 121-122):
  `(user_id, source_uri)` is unique per TENANT.

### Docstring / comment sweep (src + tests)
Every mention of the triple as the unique key becomes the pair:
- `src/tree/data/file/file_pipeline.py:31`, `src/tree/data/conversation/conversation_pipeline.py:32`
  ("deduped on `(user_id, source_uri)`").
- `src/tree/data/conversation/conversation.py:85` ("via the `(user_id, source_uri)` compound unique index").
- `src/tree/data/substack/substack.py:159` and `src/tree/data/youtube/youtube.py:465`
  (the `DuplicateKeyError` comments: "Concurrent insert of the same `(user_id, source_uri)`";
  add that the guard now also covers a `LATENT` placeholder racing a real row for the same URI).
- `tests/unit/data/test_conversation.py:214` (comment only).
- `docs/notes/conversations-storage-tradeoffs.md:74` — one-word edit, in scope (it describes
  the live index, not a historical decision).
- NOT touched: ADR-001's body (only its Status line, done in the grooming commit) and
  ADR-010's Context (quotes the old key on purpose). The grep AC below is scoped accordingly.

### Tests (`/squid-testing-python`)
- `tests/unit/entities/test_documents.py::TestDocumentCompoundUniqueIndex`: the target key
  becomes `[("user_id", 1), ("source_uri", 1)]`, `unique=True`, name `user_source_uri_unique`;
  ADD a negative assertion that `source_type` is NOT a key of any unique index; rewrite the
  class docstring (line 116) to name the pair. `test_no_inline_unique_on_source_uri` stays.
- `tests/unit/test_offline.py` retry-selector tests do not assert the multi-row path
  (`_patch_document_lookup` returns one row per URI) — they must stay green WITHOUT edits.
  Add one test that two rows for two URIs resolve to exactly one id each, in caller order.

## Out of scope
- Migration code (startup index drop, script, `allow_index_dropping`) — DBs are wiped, not migrated.
- A reusable reset / wipe make target — the one-shot `mongosh` command below is run by hand, nothing committed.
- Re-ingesting either environment.
- Any change to loader / `online.py` dedup logic — already on `(user_id, source_uri)`.
- Prod database changes.

## Acceptance Criteria

- [ ] `Document.Settings.indexes` carries exactly one unique index, keyed
      `[("user_id", 1), ("source_uri", 1)]`, named `user_source_uri_unique`; `source_type`
      appears in no index key (unit test).
- [ ] `_resolve_source_uris` returns ONE id per URI, in the caller's order, and still raises
      `ValueError("No document for source_uri …")` on an unknown URI (unit tests; existing
      `test_offline.py` retry-selector tests unchanged and green).
- [ ] `grep -rn "source_type, source_uri" apps/memory/src apps/memory/tests docs/notes` → 0 hits;
      `grep -rn "two .*source_type\|several rows" apps/memory/src/tree/offline.py` → 0 hits.
- [ ] Live (local, `make env-status` → local, `make local-start` up, served from the branch):
      after `mongosh … --eval 'db.getSiblingDB("tree").dropDatabase()'` and `make memory-signup …`,
      `db.documents.getIndexes()` lists `user_source_uri_unique` with key `{ user_id: 1, source_uri: 1 }`,
      `unique: true`, and NO index key containing `source_type` — output pasted in `## Log`.
- [ ] Live: `make memory-run-data-pipeline MODE=online SOURCE=<small url>` ingests one Document;
      re-running the SAME `SOURCE` answers `duplicate=True` (receipt line pasted in `## Log`).
- [ ] Live (the behaviour this task exists to fix): a `mongosh` `insertOne` of a row with the
      ingested Document's `user_id` + `source_uri` and `source_type: "latent"` is rejected with
      `E11000 duplicate key error … index: user_source_uri_unique` — line pasted verbatim in `## Log`.
      (Under the old key this insert would have SUCCEEDED.)
- [ ] `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green.

## User Stories

### Story: Operator wipes the local DB and boots on the new key
1. `make env-status` prints local; `make local-start` brings up MongoDB + mongot.
2. Operator runs
   `mongosh "mongodb://$MONGO_INITDB_ROOT_USERNAME:$MONGO_INITDB_ROOT_PASSWORD@localhost:$MONGO_PORT/?directConnection=true" --quiet --eval 'db.getSiblingDB("tree").dropDatabase()'`
   (`$MONGO_INITDB_DATABASE` is `tree` locally). Nothing is committed for this step.
3. From the feature branch: `make memory-serve-workflows &` (the Docker `prefect-worker` runs
   main's image — the e2e skill's warning applies), then
   `make memory-signup USER_IDENTIFIER=<handle> NAME="Paul Iusztin"`. `signup.py`'s
   `init_mongodb` → `init_beanie` creates the index.
4. `db.documents.getIndexes()` shows `user_source_uri_unique` on `{ user_id: 1, source_uri: 1 }`, `unique: true`.

### Story: Operator ingests one source and re-submits it
1. `make memory-run-data-pipeline MODE=online SOURCE="https://<small page>"` — data step only (lightest path; `documents` only).
2. The streamed log shows the Document ingested; `db.documents.countDocuments()` is 1.
3. The same command again answers an **Ingest receipt** with `duplicate=True`, `status: duplicate`, and dispatches nothing.

### Story: The DB rejects a LATENT placeholder racing a real row (the fixed bug)
1. Operator reads the ingested row's `user_id` and `source_uri` from `db.documents.findOne()`.
2. `db.documents.insertOne({user_id: <that ObjectId>, source_uri: "<that uri>", source_type: "latent", authors: [], references: [], metadata: {}})`.
3. mongosh prints `E11000 duplicate key error collection: tree.documents index: user_source_uri_unique …`.
   Before this task the insert succeeded, leaving two rows for one URI.

### Story: Operator retries an extraction by receipt URI
1. `make memory-run-memory-pipeline MODE=online SOURCE_URIS=<the uri above>`.
2. The run log shows `offline-pipeline: resolved 1 source_uris to 1 document_ids` (unchanged line, now the only possible count per URI).

### Smoke-test boundaries
A freshly dropped DB has no `memory` collection and no search indexes; the smoke must NOT
grow into `make memory-run-indexing-pipeline`, clustering, or a re-ingest of the old corpus.
Vector-index readiness noise in the serve terminal is expected, not a failure. Stop the serve
process when done (e2e skill step 6).

---

Blocked by: (none)

## Log

### [PA] 2026-10-01 — Grooming

**Summary**
Align the `documents` unique index with the Document's natural key `(user_id, source_uri)` — the key every loader, `online.py` and the LATENT upgrade already use — so the DB race guard fires across source types; delete the multi-row handling the triple made necessary; fix the prose. No migration: local DB dropped once by hand.

**Key decisions**
- Index key `(user_id, source_uri)`, SAME name `user_source_uri_unique`; `source_type` becomes a plain row attribute (it already was in practice — the LATENT upgrade rewrites it).
- No migration code of any kind; no reusable wipe target; one-shot `mongosh dropDatabase` locally, nothing committed. Prod database changes are out of scope.
- ADR-010 supersedes ADR-001 on this one point (006 precedent: pointer on ADR-001's Status line, body untouched). Glossary unchanged — **Document** already says `(user_id, source_uri)`.
- The discriminating live check is the `E11000` on a cross-`source_type` insert, not `getIndexes()` or the duplicate receipt (the latter was already true under the old key via `find_one`).
- Grep AC scoped to `apps/memory/src`, `apps/memory/tests`, `docs/notes` — ADR-001's body and ADR-010's Context quote the old key on purpose.

**Dependencies**
- None.

**User stories**
- 4 stories covering: local wipe + boot, ingest + duplicate receipt, cross-type E11000 rejection, retry-by-URI count.

Ready for implementation.
