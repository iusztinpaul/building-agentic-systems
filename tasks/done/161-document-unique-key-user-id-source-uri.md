---
id: 161-document-unique-key-user-id-source-uri
status: done
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

- [x] `Document.Settings.indexes` carries exactly one unique index, keyed
      `[("user_id", 1), ("source_uri", 1)]`, named `user_source_uri_unique`; `source_type`
      appears in no index key (unit test).
- [x] `_resolve_source_uris` returns ONE id per URI, in the caller's order, and still raises
      `ValueError("No document for source_uri …")` on an unknown URI (unit tests; existing
      `test_offline.py` retry-selector tests unchanged and green).
- [x] `grep -rn "source_type, source_uri" apps/memory/src apps/memory/tests docs/notes` → 0 hits;
      `grep -rn "two .*source_type\|several rows" apps/memory/src/tree/offline.py` → 0 hits.
- [x] Live (local, `make env-status` → local, `make local-start` up, served from the branch):
      after `mongosh … --eval 'db.getSiblingDB("tree").dropDatabase()'` and `make memory-signup …`,
      `db.documents.getIndexes()` lists `user_source_uri_unique` with key `{ user_id: 1, source_uri: 1 }`,
      `unique: true`, and NO index key containing `source_type` — output pasted in `## Log`.
- [x] Live: `make memory-run-data-pipeline MODE=online SOURCE=<small url>` ingests one Document;
      re-running the SAME `SOURCE` answers `duplicate=True` (receipt line pasted in `## Log`).
- [x] Live (the behaviour this task exists to fix): a `mongosh` `insertOne` of a row with the
      ingested Document's `user_id` + `source_uri` and `source_type: "latent"` is rejected with
      `E11000 duplicate key error … index: user_source_uri_unique` — line pasted verbatim in `## Log`.
      (Under the old key this insert would have SUCCEEDED.)
- [x] `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green.

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

### [SWE] 2026-10-01 10:55 — Implementation

**Files modified**
- `apps/memory/src/tree/entities/documents.py` — `user_source_uri_unique` now keyed `[("user_id", 1), ("source_uri", 1)]` (same name); comment rewritten (source_type is a row attribute a LATENT upgrade rewrites, ADR-010).
- `apps/memory/src/tree/offline.py` — `_resolve_source_uris`: `ids_by_uri: dict[str, str]`, `append` not `extend`; "several rows / two source_types" sentence replaced by "each URI resolves to ONE id". `ValueError` and the `resolved %d source_uris to %d document_ids` log line unchanged. `_validate_single_tenant_scope` docstring names the pair.
- `apps/memory/src/tree/data/{file/file_pipeline.py,conversation/conversation_pipeline.py,conversation/conversation.py}` — docstring/comment: `(user_id, source_uri)`.
- `apps/memory/src/tree/data/{substack/substack.py,youtube/youtube.py}` — `DuplicateKeyError` comments name the pair and now say the guard also covers a LATENT placeholder racing the real row.
- `docs/notes/conversations-storage-tradeoffs.md:74` — one-word key edit.
- `apps/memory/tests/unit/entities/test_documents.py` — `TestDocumentCompoundUniqueIndex` rewritten: exactly one unique index, key = the pair, name `user_source_uri_unique`; NEW `test_source_type_is_not_part_of_any_index_key`; class docstring names the pair. `test_no_inline_unique_on_source_uri` untouched.
- `apps/memory/tests/unit/test_offline.py` — NEW `TestSourceUris::test_resolves_one_id_per_uri_in_caller_order` (two rows, reverse cursor order → one id each, caller order). Existing retry-selector tests NOT edited.
- `apps/memory/tests/unit/data/test_conversation.py:214` — comment only.

**Tests**
- Unit: 4080 passing, 0 failing (`make memory-tests`).
- Red before green: both `TestDocumentCompoundUniqueIndex` tests failed on the old triple (`AssertionError: assert 'source_type' not in {'source_type', 'source_uri', 'user_id'}`). The new `test_offline.py` test was green under BOTH the old and new resolver, because with one row per URI `extend`/`append` agree. It pins the one-id-per-URI, caller-order contract and is intentionally not a red test.
- Integration: N/A (no suite by design; live smoke below).

**Acceptance criteria**
- [x] One unique index on `(user_id, source_uri)` named `user_source_uri_unique`; `source_type` in no index key: `tests/unit/entities/test_documents.py::TestDocumentCompoundUniqueIndex::{test_settings_declares_the_natural_key_as_the_only_unique_index,test_source_type_is_not_part_of_any_index_key}`
- [x] One id per URI, caller order, `ValueError` on unknown: `tests/unit/test_offline.py::TestSourceUris::{test_resolves_one_id_per_uri_in_caller_order,test_unknown_uri_fails_loud}`; the existing retry-selector tests are unchanged and green.
- [x] Grep ACs: both greps return 0 hits (exit 1).
- [x] Live getIndexes: see Evidence.
- [x] Live ingest + `duplicate=True` receipt: see Evidence.
- [x] Live E11000 on a cross-type insert: see Evidence.
- [x] `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green.

**Evidence** (LOCAL only. `make env-status` printed `Env target: local (.env)` before every mongosh/pipeline command; exported `MONGO_HOST=localhost`.)
```
$ grep -rn "source_type, source_uri" apps/memory/src apps/memory/tests docs/notes      -> (no output, exit 1)
$ grep -rn "two .*source_type\|several rows" apps/memory/src/tree/offline.py           -> (no output, exit 1)

$ make memory-format-check && make memory-lint-check && make pre-commit
317 files already formatted / All checks passed! / prettier, ruff check, ruff format, biome check: Passed
$ make memory-tests
======================= 4080 passed in 68.31s (0:01:08) ========================

$ make env-status
Env target: local (.env)
$ docker stop tree-prefect-worker     # it runs main's image (old key); restarted at cleanup
$ mongosh "mongodb://$MONGO_INITDB_ROOT_USERNAME:$MONGO_INITDB_ROOT_PASSWORD@localhost:$MONGO_PORT/?directConnection=true" --quiet --eval 'db.getSiblingDB("tree").dropDatabase()'
{ ok: 1, dropped: 'tree' }

$ make memory-signup USER_IDENTIFIER=p.b.iusztin@gmail.com NAME="Paul Iusztin"
Created user identifier=p.b.iusztin@gmail.com id=6abe3b76136fe3826b13c405
Set current user -> id=6abe3b76136fe3826b13c405

$ mongosh ... --eval 'db.getSiblingDB("tree").documents.getIndexes()'      # BEFORE any ingest
[
  { v: 2, key: { _id: 1 }, name: '_id_' },
  {
    v: 2,
    key: { user_id: 1, source_uri: 1 },
    name: 'user_source_uri_unique',
    unique: true
  }
]

$ make memory-serve-workflows &       # from feat/document-unique-key
$ make memory-run-data-pipeline MODE=online SOURCE="https://example.com"
Submitted flow run d298d83b-de0c-4e0c-bdc3-5c8ceedaf5b8 (scheduled); waiting for it...
INFO | Runner 'runner-65516646-…' submitting flow run 'd298d83b-…'     # the branch serve process, confirmed in its own log
INFO | Beginning flow run 'quixotic-weasel' for flow 'online-pipeline'
Done. Flow completed successfully.
$ mongosh ... documents.countDocuments() / findOne()
{ count: 1 }
{ _id: ObjectId('6abe3b9b2fbb362fd79ef7db'), source_type: 'web', source_uri: 'https://example.com', user_id: ObjectId('6abe3b76136fe3826b13c405'), title: 'Example.Com' }

$ make memory-run-data-pipeline MODE=online SOURCE="https://example.com"    # same SOURCE again
Duplicate at submit time: https://example.com
Already ingested: https://example.com (document 6abe3b9b2fbb362fd79ef7db)
# The same receipt dumped by calling tree.online.dispatch_online_pipeline directly (duplicate path, dispatches nothing):
{"source_uri":"https://example.com","duplicate":true,"document_id":"6abe3b9b2fbb362fd79ef7db","flow_run_id":null,"status":"duplicate"}

$ mongosh ... --eval 'const d=db.getSiblingDB("tree").documents; const r=d.findOne(); d.insertOne({user_id: r.user_id, source_uri: r.source_uri, source_type: "latent", authors: [], references: [], metadata: {}})'
E11000 duplicate key error collection: tree.documents index: user_source_uri_unique dup key: { user_id: ObjectId('6abe3b76136fe3826b13c405'), source_uri: "https://example.com" }
{ user_id_type: 'ObjectId', count_after: 1 }

$ make memory-run-memory-pipeline MODE=online SOURCE_URIS="https://example.com"     # Story 4
INFO | Beginning flow run 'classic-slug' for flow 'offline-pipeline'
INFO | offline-pipeline: resolved 1 source_uris to 1 document_ids
INFO | extraction: user_id=6abe3b76136fe3826b13c405 shards=1 succeeded=1 failed=0
INFO | indexing: user_id=6abe3b76136fe3826b13c405 embedded=1
Done. Flow completed successfully.
```

**Notes**
- The Docker `tree-prefect-worker` runs main's image and declares the OLD triple under the same index name, so I stopped it for the smoke. That way every run executed on the branch serve process. It was restarted afterwards (`docker start tree-prefect-worker`). The serve process is stopped.
- The online data-pipeline CLI does not print the literal `duplicate=True`. Its `Already ingested … (document …)` line is the `flow_run_id is None` branch of the receipt. I pasted the receipt JSON from a one-off call to `dispatch_online_pipeline`, which is read-only on the duplicate path. Nothing was committed for it.
- Story 4 ran `make memory-run-memory-pipeline`, which includes its own indexing phase over the one doc. `make memory-run-indexing-pipeline` and clustering were not run, per the smoke boundaries.
- The local `tree` DB now holds 1 user plus the 1 example.com Document and its extracted memory. The pre-smoke local corpus is gone; it was dropped by design, with no re-ingest (out of scope).
- Prod was not touched.

### [Tester] 2026-10-01 — QA

**Test summary**
- Format / lint / pre-commit: PASS (`make memory-format-check`: 317 files formatted; `make memory-lint-check`: all checks passed; `make pre-commit`: prettier/ruff/biome Passed)
- Unit tests: 4080 passed / 0 failed (`make memory-tests`). Integration: N/A (no suite by design; live smoke below).
- Warnings: none surfaced by the suite summary

**E2E adversarial pass** (LOCAL only: `make env-status` -> `Env target: local (.env)`; MONGO_HOST=localhost; settings URI host = localhost; prod never touched)
- Independent `getIndexes()` on `tree.documents`: `{ key: { user_id: 1, source_uri: 1 }, name: 'user_source_uri_unique', unique: true }` + `_id_` only; no `source_type` in any key (PASS)
- Happy path: existing row `https://example.com` / `web` present (1 document, 1 user) (PASS)
- Break 1 (cross-type, same user, same URI), `insertOne` with source_type latent / youtube / file / conversation / substack / arxiv: ALL six -> `E11000 duplicate key error collection: tree.documents index: user_source_uri_unique dup key: { user_id: ObjectId('6abe3b76…'), source_uri: "https://example.com" }` (PASS; old triple would have accepted all six)
- Break 2 (tenant isolation): different (random) user_id, same URI, `source_type: latent` -> inserted OK (PASS); same other user + same URI again as `web` -> E11000 (correct, per-tenant uniqueness holds)
- Break 3 (boundary URIs, same user): trailing-slash variant and non-ASCII URI (`https://exämple.com/日本語`) insert OK (distinct keys, no normalization: expected); empty-string URI inserts once, second (other type) -> E11000 (PASS)
- Break 4 (resolver, mocked Document): `_resolve_source_uris(["u2","u1","u2"])` -> `['b2','a1','b2']` (caller order, repeated URI repeats as before); unknown URI -> `ValueError: No document for source_uri nope (user U)`; `[]` -> `[]` (PASS)
- Cleanup: all 4 hand-inserted rows deleted (`deleteMany({qa:"tester161"})` -> 4); `countDocuments()` back to 1.

**Acceptance criteria**
- [x] PASS — one unique index on `(user_id, source_uri)` named `user_source_uri_unique`, no `source_type` in any key: `tests/unit/entities/test_documents.py::TestDocumentCompoundUniqueIndex` (2 tests green) + live getIndexes above
- [x] PASS — `_resolve_source_uris` one id per URI, caller order, ValueError on unknown: `tests/unit/test_offline.py::TestSourceUris` green (incl. new `test_resolves_one_id_per_uri_in_caller_order`; pre-existing retry-selector tests not edited per `git diff`), plus break 4
- [x] PASS — greps: `grep -rn "source_type, source_uri" apps/memory/src apps/memory/tests docs/notes` -> 0 hits; `grep -rn "two .*source_type\|several rows" apps/memory/src/tree/offline.py` -> 0 hits (both exit 1); log line `resolved %d source_uris to %d document_ids` unchanged in diff
- [x] PASS — live getIndexes (independently re-run)
- [x] PASS — live ingest + duplicate receipt: not re-executed by me (would need the serve process); evidence = SWE log; DB state consistent with it (1 web doc, receipt path relies on unchanged `find_one` dedup)
- [x] PASS — live E11000 on cross-type insert (independently reproduced with 6 types)
- [x] PASS — format-check / lint-check / pre-commit / memory-tests green (re-run by me)

**Other issues found**
- STYLE (fix before commit, one line): `apps/memory/src/tree/offline.py:122` is 137 chars; the re-wrap in the `_validate_single_tenant_scope` docstring left `unique per TENANT, so the same URI legitimately exists for several users). Checked at BOTH edges — the flow and the fire-and-forget` on one line while the rest of the docstring wraps at <=81. ruff does not flag it (E501 not enforced). Fix: re-wrap after "several users)." so each line is <=81 chars.
- Note: the docker `tree-prefect-worker` is up again (main image, still declares the old triple under the same index name). Beanie's index sync against the new-key DB from that worker could conflict; not in scope, but do not start it before the branch merges unless the DB is wiped.
- Note: the SWE's new offline test is green under old and new code (documented by SWE); acceptable as a contract pin.

**VERDICT: PASS** (with one cosmetic docstring re-wrap to apply at offline.py:121-122; no behavioural issue)
