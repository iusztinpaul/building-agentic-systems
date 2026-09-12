---
id: 123-retry-memory-pipeline-by-source-uri
feature: mcp-tool-contracts
status: pending
---

# `SOURCE_URIS=` retry path for `make memory-run-memory-pipeline MODE=online`

Tags: `scripts`, `offline`, `make`
Depends on: None
Blocks: —
Implements: ADR-008 — Decision 1 (the receipt's `source_uri` is what an operator retries with)

## Scope

A failed online extraction is retried with `make memory-run-memory-pipeline MODE=online DOC_IDS=<id>`,
but after #122 the receipt a model holds carries a `source_uri`, not a document id. Add a URI
selector beside `DOC_IDS`; keep `DOC_IDS`.

- `tree/offline.py`: `offline_pipeline(..., source_uris: list[str] | None = None)` and
  `dispatch_offline_pipeline(..., source_uris=...)` forwarded unchanged. `_validate_document_ids_scope`
  becomes `_validate_single_tenant_scope(document_ids, source_uris, user_id)` — either selector
  without `user_id` raises the existing `ValueError` (message names the selector). At flow entry,
  after `init_mongodb`, `_resolve_source_uris(user_id, source_uris) -> list[str]` runs ONE
  `Document.find({"user_id": user_id, "source_uri": {"$in": uris}})`; every URI must resolve —
  an unknown URI raises `ValueError("No document for source_uri <uri> (user <id>)")` (a typo must
  fail loud, not silently extract nothing). The resolved ids are unioned with `document_ids`
  (deduplicated, order preserved) and passed to the extraction Coordinator as today.
- `apps/memory/scripts/run_memory_pipeline.py`: `--source-uris` (comma-separated). `--mode online`
  requires `--doc-ids` OR `--source-uris`; the `UsageError` text names both. Glue only.
- `apps/memory/Makefile` `run-memory-pipeline`: `$(if $(SOURCE_URIS),--source-uris "$(SOURCE_URIS)",)`;
  help comment mentions `SOURCE_URIS="<uri>[,<uri2>]"`.
- Docstrings: `online.py::online_pipeline` "retryable via … `DOC_IDS=<id>` or `SOURCE_URIS=<uri>`";
  every place `DOC_IDS=` is documented (`grep -rn "DOC_IDS" apps/memory/README.md docs .agents`) gains
  the URI form in the same sentence.

## Out of scope
- Resolving URIs across users (`user_id=None`) — single-tenant by design, like `document_ids`.
- Re-running the DATA step for a URI (that is `run-pipeline MODE=online SOURCE=`).

## Acceptance Criteria

- [ ] `offline_pipeline(user_id=U, source_uris=["file:///a.md"], run_data=False)` resolves to that document's id and calls the extraction Coordinator with it — `tests/unit/test_offline.py::TestSourceUris::test_resolves_uris_to_document_ids`.
- [ ] `source_uris` + `document_ids` are unioned without duplicates — `::test_unions_with_document_ids`.
- [ ] An unresolvable URI raises `ValueError` naming it before any phase runs — `::test_unknown_uri_fails_loud`.
- [ ] `source_uris` without `user_id` raises `ValueError` — `::test_source_uris_require_user_id`.
- [ ] `dispatch_offline_pipeline` forwards `source_uris` in the deployment parameters — `tests/unit/test_offline.py::test_dispatch_forwards_source_uris`.
- [ ] `run_memory_pipeline.py --mode online --source-uris "a,b"` dispatches with `["a","b"]`; `--mode online` with neither selector exits with a `UsageError` naming both — `tests/unit/scripts/test_run_memory_pipeline.py::test_source_uris_option`, `::test_online_requires_a_selector`.
- [ ] `grep -n "SOURCE_URIS" apps/memory/Makefile` shows the forward and the help text.
- [ ] `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green.

## User Stories

### Story: Operator retries a failed online extraction from the receipt
1. A user's `ingest_url` receipt shows `source_uri: "https://www.youtube.com/watch?v=abc"`; the Prefect run failed in extraction.
2. Operator runs `make memory-run-memory-pipeline MODE=online SOURCE_URIS="https://www.youtube.com/watch?v=abc"`.
3. The log streams `extraction: user_id=… shards=1 …` for exactly that document, then `indexing: …`.

### Story: Operator mistypes the URI
1. Operator runs `make memory-run-memory-pipeline MODE=online SOURCE_URIS="https://youtube.com/watch?v=abd"`.
2. The flow fails at entry with `No document for source_uri https://youtube.com/watch?v=abd (user …)`; no extraction runs.

### Story: Operator mixes both selectors
1. `make memory-run-memory-pipeline MODE=online DOC_IDS="66f…" SOURCE_URIS="file:///tmp/a.md"`.
2. Both documents are extracted in one Coordinator run.

---

Blocked by: (none)

## Log

### [PA] 2026-09-12 10:05 — Grooming

**Summary**
The retry command accepts the natural key the receipt hands out; ids are resolved once at flow entry and fed to the unchanged extraction path.

**Key decisions**
- Resolve at flow entry (single `$in` query), not in the script — the flow is the contract; the script stays glue.
- Unknown URI fails loud; silent "0 documents" is what makes retries look successful.

**Dependencies**
- None (uses the existing `(user_id, source_uri)` index).

**User stories**
- 3 stories covering: retry from receipt, typo, mixed selectors.

Ready for implementation.
