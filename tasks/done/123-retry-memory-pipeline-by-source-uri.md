---
id: 123-retry-memory-pipeline-by-source-uri
feature: mcp-tool-contracts
status: done
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

- [x] `offline_pipeline(user_id=U, source_uris=["file:///a.md"], run_data=False)` resolves to that document's id and calls the extraction Coordinator with it — `tests/unit/test_offline.py::TestSourceUris::test_resolves_uris_to_document_ids`.
- [x] `source_uris` + `document_ids` are unioned without duplicates — `::test_unions_with_document_ids`.
- [x] An unresolvable URI raises `ValueError` naming it before any phase runs — `::test_unknown_uri_fails_loud`.
- [x] `source_uris` without `user_id` raises `ValueError` — `::test_source_uris_require_user_id`.
- [x] `dispatch_offline_pipeline` forwards `source_uris` in the deployment parameters — `tests/unit/test_offline.py::test_dispatch_forwards_source_uris`.
- [x] `run_memory_pipeline.py --mode online --source-uris "a,b"` dispatches with `["a","b"]`; `--mode online` with neither selector exits with a `UsageError` naming both — `tests/unit/scripts/test_run_memory_pipeline.py::test_source_uris_option`, `::test_online_requires_a_selector`.
- [x] `grep -n "SOURCE_URIS" apps/memory/Makefile` shows the forward and the help text.
- [x] `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green.

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

### [SWE] 2026-09-12 14:25 — Implementation

**Files modified**
- `apps/memory/src/tree/offline.py` — `source_uris` selector on `offline_pipeline` + `dispatch_offline_pipeline`; `_validate_document_ids_scope` → `_validate_single_tenant_scope(document_ids, source_uris, user_id)`; new `_resolve_source_uris(user_id, source_uris)` (ONE `$in` read over `user_source_uri_unique`, connects only when asked), unioned with `document_ids` at flow entry.
- `apps/memory/scripts/run_memory_pipeline.py` — `--source-uris` option, `_parse_csv` helper (shared by both selectors), `--mode online` accepts either selector; glue only.
- `apps/memory/Makefile` — `SOURCE_URIS` forward + help text on `run-memory-pipeline`.
- `apps/memory/README.md`, `apps/memory/src/tree/online.py` — every documented `DOC_IDS=` retry now names the `SOURCE_URIS=` form in the same sentence.
- `apps/memory/tests/unit/test_offline.py` — `TestSourceUris` (5 tests) + 2 dispatcher tests; `source_uris` added to the exhaustive deployment-parameter assertion and the signature-order test.
- `apps/memory/tests/unit/scripts/test_run_memory_pipeline.py` — selector parsing/forwarding, both-names usage error.

**Tests**
- Unit: 2931 passing, 0 failing, 0 warnings — `make memory-tests` (env `local`).
- Integration: N/A — this repo has no integration suite (AGENTS.md); e2e ran the real pipeline instead.

**Acceptance criteria** (full node ids — copy-paste runnable)
- [x] URI resolves to the document id — `tests/unit/test_offline.py::TestSourceUris::test_resolves_uris_to_document_ids`
- [x] union without duplicates, order from the SELECTOR not the cursor — `tests/unit/test_offline.py::TestSourceUris::test_unions_with_document_ids`
- [x] unknown URI fails loud before any phase — `tests/unit/test_offline.py::TestSourceUris::test_unknown_uri_fails_loud`
- [x] `source_uris` without `user_id` — `tests/unit/test_offline.py::TestSourceUris::test_source_uris_require_user_id`
- [x] dispatcher forwards the selector — `tests/unit/test_offline.py::TestDispatchOfflineIngest::test_dispatch_forwards_source_uris` (+ `::test_source_uris_without_user_id_never_creates_a_flow_run`)
- [x] CLI parses/forwards + names both selectors — `tests/unit/scripts/test_run_memory_pipeline.py::TestRunMemoryPipelineForwarding::test_source_uris_option`, `::TestRunMemoryPipelineCliOptions::test_online_requires_a_selector`, `::TestRunMemoryPipelineDispatch::test_forwards_source_uris_to_the_dispatcher`
- [x] `grep -n "SOURCE_URIS" apps/memory/Makefile` — hits on line 173 (help) and 174 (forward)
- [x] format / lint / pre-commit / tests green
- Extra guard (not in the AC): `::TestSourceUris::test_a_run_without_source_uris_never_touches_mongo` — resolution connects only when asked, so the nightly cron's I/O is unchanged.

**Evidence**

```
$ make memory-tests
2931 passed in 53.55s

$ make memory-format-check && make memory-lint-check && make pre-commit
288 files already formatted
All checks passed!
ruff check ... Passed / ruff format ... Passed / prettier ... Passed / biome ... Passed
```

E2E (real pipelines, local env, `TREE_MEMORY__MODE=rag`, served from this worktree). Timestamps
below are the CLI's UTC stream (`11:2x`); the serving process logs the same runs in local time
(`14:2x`).

The Docker `tree-prefect-worker` races the worktree's `serve` process for these deployments and
grabbed the first attempt, failing with `SignatureMismatchError: Function expects ['user_id',
'source_files', 'sources', 'num_shards', 'run_data', 'run_extraction', 'document_ids']`. **That
image is stale independently of this task** — it predates `run_indexing` / `run_clustering`
(ADR-007, already on `main`), which the same error names as unexpected. So it is NOT evidence that
`source_uris` breaks deployed runs; it is the documented worktree gotcha in
`.agents/skills/run-pipelines-e2e/SKILL.md`. The container was stopped for the three runs below and
restarted afterwards.

```
# Story 2 — operator mistypes the URI
$ make memory-run-memory-pipeline MODE=online SOURCE_URIS="https://www.decodingai.com/p/typo-does-not-exist" USER_IDENTIFIER="paul@example.com"
ERROR | Encountered exception during execution: ValueError('No document for source_uri
        https://www.decodingai.com/p/typo-does-not-exist (user 6a8ea9579a7aeb13175955c8)')
Finished in state Failed  (exit 2; no extraction ran)

# Story 1 — retry from the receipt's source_uri
$ make memory-run-memory-pipeline MODE=online SOURCE_URIS="https://www.decodingai.com/p/ai-agents-planning" USER_IDENTIFIER="paul@example.com"
INFO | offline-pipeline: resolved 1 source_uris to 1 document_ids
INFO | extraction: user_id=6a8ea9579a7aeb13175955c8 shards=1 succeeded=1 failed=0
INFO | indexing: user_id=6a8ea9579a7aeb13175955c8 embedded=0
INFO | Finished in state Completed()

# Story 3 — both selectors in one run
$ make memory-run-memory-pipeline MODE=online DOC_IDS="6a8ea976dfbcf322b44281c4" SOURCE_URIS="https://www.decodingai.com/p/stop-building-ai-agents-use-these" USER_IDENTIFIER="paul@example.com"
INFO | offline-pipeline: resolved 1 source_uris to 1 document_ids
INFO | memory_extract_etl_worker complete (rag): documents=2 nodes_written=59   # both docs, ONE Coordinator run
INFO | Finished in state Completed()

$ uv run python scripts/run_memory_pipeline.py --mode online          # neither selector
Error: --mode online requires --doc-ids '<id>[,<id2>]' (the id printed by run_data_pipeline.py
       --mode online) or --source-uris '<uri>[,<uri2>]' (the source_uri an ingest receipt carries).
exit=2
```

**Notes**
- Resolution runs BEFORE the data phase, so `SOURCE_URIS=` is a retry selector over ALREADY-INGESTED documents, never an ingest selector (matches the task's out-of-scope note; stated in `_resolve_source_uris`' docstring). Ingesting is still `run-pipeline MODE=online SOURCE=`.
- `init_mongodb` is called ONLY when `source_uris` is non-empty (same "connect only when needed" shape as `resolve_target_user_ids`), so every existing offline run keeps its exact I/O — pinned by `test_a_run_without_source_uris_never_touches_mongo`.
- A URI matching several rows (same URI under two `source_type`s) resolves to ALL of them; resolved order follows the URIs the operator typed, since Mongo guarantees no cursor order.
- Unresolvable URIs are NOT pre-checked in the dispatcher (it does no I/O by design); they fail the flow run, which is where the operator streams logs.
- `document_ids is single-tenant — pass user_id too.` is byte-identical after the rename; the new sibling message names `source_uris`.
- `--mode offline --source-uris "<uri>" --num-shards 4` is legal and coherent (the Coordinator shards the resolved set); the offline-only `--num-shards` guard is unchanged. Intentional, and deliberately untested — it is not an AC, and the forwarding path it uses is already pinned by `::test_forwards_source_uris_to_the_dispatcher`.
- Local env touched during e2e: the pre-existing `make memory-serve-workflows` process was restarted (stale code) and left running; `tree-prefect-worker` was stopped for ~2 min and restarted. No prod access, no repo artefacts left behind.

### [Tester] 2026-09-12 — QA

**Test summary**
- Format / lint / pre-commit: PASS (`make memory-format-check && make memory-lint-check` — "288 files already formatted" / "All checks passed!"; `make pre-commit` — prettier/ruff check/ruff format/biome all Passed)
- Unit tests: 2931 passed / 0 failed (`make memory-tests`, env `local`)
- Integration tests: N/A (no integration suite in this repo, per AGENTS.md)
- Warnings: 0 (no pytest warnings summary line; the one `UserWarning` seen when invoking `uv run pytest` directly is `opik`'s pre-existing Pydantic-v1/Python-3.14 warning, unrelated to this diff, and does not appear under `make memory-tests`)
- `code-review` plugin: not invoked — it operates via `gh pr diff`/`gh pr view` on an open PR; this task has no PR yet (uncommitted local diff on a feature branch, per squid's file-mode flow). Substituted with an equivalent manual pass: CLAUDE.md/AGENTS.md compliance (types on every new signature, native logger, no `print`, Pydantic types unaffected), diff-only bug scan, and doc-comment consistency (`_resolve_source_uris`/`_validate_single_tenant_scope` docstrings match behavior) — no issues found.

**E2E adversarial pass**
- Happy path: `offline_pipeline(user_id=U, source_uris=["file:///a.md"], run_data=False)` → resolves the URI, calls the extraction Coordinator with `document_ids=[<id>]` (PASS — `tests/unit/test_offline.py::TestSourceUris::test_resolves_uris_to_document_ids`); SWE's own e2e log confirms the same shape against a live Prefect/Mongo run for all 3 user stories (retry, typo, mixed selectors).
- Break path 1 (malformed CLI input — `--source-uris "a, ,b"`): scratch test invoking `scripts/run_memory_pipeline.py`'s `main` with `["--mode", "online", "--source-uris", "a, ,b"]` → `_run` received `source_uris=["a", "b"]` (blank element silently dropped, no crash, matches `--doc-ids`' existing CSV semantics) (PASS)
- Break path 2 (cross-tenant / hostile-ish input — URI that only exists for another user): mocked `Document.find` to return `[]` for a tenant-scoped `{"user_id": U, "source_uri": {"$in": [...]}}` query (i.e. the query itself is correctly scoped, so another tenant's row never matches) → `ValueError("No document for source_uri <uri> (user <id>)")`, `extract` never awaited (PASS — fails loud, no cross-tenant leak, no silent no-op)
- Break path 3 (state edge — duplicate URI + overlap with `document_ids`): `document_ids=[_DOC_ID]`, `source_uris=[_SOURCE_URI, _SOURCE_URI]` where `_SOURCE_URI` resolves to `_DOC_ID` → extraction called with `document_ids=[_DOC_ID]` exactly once, no duplicate (PASS — `dict.fromkeys` union dedupes as designed)
- Break path 4 (boundary — `source_uris=[]` vs `None`): `offline_pipeline(user_id=None, source_uris=[], document_ids=[])` → no `ValueError`, `init_mongodb`/`Document.find` never called (falsy-list check treats `[]` identically to `None`); same for `dispatch_offline_pipeline(user_id=None, source_uris=[], document_ids=[])` — no validation error, dispatch proceeds (PASS)
- Break path 5 (consistency — `--mode offline` + `--source-uris`): CLI accepts `--mode offline --source-uris "file:///a.md" --num-shards 4` (no `UsageError`, `--num-shards` still forwarded since that guard is online-only) and the flow itself has no `mode` concept — `offline_pipeline(user_id=U, source_uris=[uri], num_shards=4, run_data=False)` resolves and extracts identically regardless of the CLI mode used to reach it (PASS — script and flow behavior consistent; matches SWE's documented "legal and coherent" note)

All 5 break-path checks (plus the happy path) were run as ad-hoc pytest cases reusing `test_offline.py`'s mocking helpers, executed via `make memory-tests ARGS=...` inside a scratch file, then deleted — no artefacts left in the tree (`git status` clean of anything besides the SWE's own diff, confirmed after removal).

**Acceptance criteria**
- [x] PASS — URI resolves to the document id, Coordinator called with it — `tests/unit/test_offline.py::TestSourceUris::test_resolves_uris_to_document_ids` passes; `Document.find` called once with `{"user_id": U, "source_uri": {"$in": [uri]}}` (`src/tree/offline.py:172-176`)
- [x] PASS — `source_uris` + `document_ids` unioned without duplicates, order preserved — `::test_unions_with_document_ids` passes; `dict.fromkeys([*(document_ids or []), *resolved])` (`src/tree/offline.py:310-312`)
- [x] PASS — unresolvable URI raises `ValueError` naming it, before any phase — `::test_unknown_uri_fails_loud` passes; confirmed `data`/`extract` never awaited
- [x] PASS — `source_uris` without `user_id` raises `ValueError` — `::test_source_uris_require_user_id` passes; message `"source_uris is single-tenant — pass user_id too."` (`src/tree/offline.py:139-140`)
- [x] PASS — `dispatch_offline_pipeline` forwards `source_uris` in deployment parameters — `tests/unit/test_offline.py::TestDispatchOfflineIngest::test_dispatch_forwards_source_uris` passes; `parameters["source_uris"] == [uri]`
- [x] PASS — `--mode online --source-uris "a,b"` dispatches `["a","b"]`; neither selector → `UsageError` naming both — `tests/unit/scripts/test_run_memory_pipeline.py::TestRunMemoryPipelineForwarding::test_source_uris_option`, `::TestRunMemoryPipelineCliOptions::test_online_requires_a_selector` both pass (output asserts on both `--doc-ids` and `--source-uris`)
- [x] PASS — `grep -n "SOURCE_URIS" apps/memory/Makefile` → hits on lines 173 (help text: `SOURCE_URIS="<uri>[,<uri2>]"`) and 174 (`$(if $(SOURCE_URIS),--source-uris "$(SOURCE_URIS)",)`)
- [x] PASS — `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` all green (see Evidence)
- [x] PASS (doc consistency, not a separate checkbox but named in Scope) — every `DOC_IDS=` mention in `apps/memory/README.md`, `apps/memory/Makefile`, `apps/memory/src/tree/online.py` now names the `SOURCE_URIS=` form alongside it (`grep -rn "DOC_IDS" apps/memory/README.md apps/memory/Makefile apps/memory/src/tree/online.py apps/memory/scripts/run_memory_pipeline.py docs .agents`); `docs/glossary.md`'s "Ingest receipt" row already named both forms (from the prior planning commit `3c31437`, not modified by this diff — no discrepancy)

**Evidence**
```
$ make memory-format-check && make memory-lint-check
288 files already formatted
All checks passed!

$ make pre-commit
prettier.................................................................Passed
ruff check...............................................................Passed
ruff format..............................................................Passed
biome check (harness)....................................................Passed

$ make memory-tests
============================ 2931 passed in 55.64s =============================

$ grep -n "SOURCE_URIS" apps/memory/Makefile
173:run-memory-pipeline: # ... optionally narrowed with DOC_IDS="id1,id2" or SOURCE_URIS="<uri>[,<uri2>]" ...
174:	uv run python scripts/run_memory_pipeline.py $(PIPELINE_MODE) $(USER_FLAGS) $(if $(DOC_IDS),--doc-ids "$(DOC_IDS)",) $(if $(SOURCE_URIS),--source-uris "$(SOURCE_URIS)",) $(if $(NUM_SHARDS),--num-shards "$(NUM_SHARDS)",)
```

**Other issues found**
- None. `_resolve_source_uris`/`_validate_single_tenant_scope` are fully typed, use the native logger (no `print`), and the extraction-only scope of `extraction_document_ids` (never leaking into indexing/clustering) is correct per spec.
- Minor, non-blocking: `--mode offline --source-uris "<uri>"` is deliberately untested per the SWE's own note; I added an ad-hoc test confirming the behavior (script forwards, flow resolves identically to `--mode online`) during this QA pass, but did not add it to the permanent suite since it isn't an AC and the SWE explicitly scoped it out. Worth a follow-up test if this combination becomes a documented/supported operator flow.

**VERDICT: PASS**
