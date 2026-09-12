---
id: 122-ingest-receipt-and-source-uri-helper
feature: mcp-tool-contracts
status: done
---

# Ingest receipt: shared `source_uri` derivation + pre-flight duplicate lookup on dispatch

Tags: `mcp`, `data`, `online`
Depends on: None
Blocks: #126, #128, #129
Implements: ADR-008 — Decision 1 (natural-key receipt)

## Scope

Today `ingest_url` / `ingest_file` / `ingest_conversation` (`apps/memory/src/tree/mcp/tools.py`,
shared tail `_ingest` → `tree/online.py::dispatch_online_pipeline`) answer
`{"status", "flow_run_id"}`; whether the source was new or a duplicate lives only in the
flow-run result. `Document._id` stays a random ObjectId (ADR-008 §1); a **Document**'s identity
is `(user_id, source_uri)`, so the receipt carries the natural key.

**1. One `source_uri` helper per leaf, called by BOTH the leaf and the dispatcher** (a leaf and
the pre-flight lookup that disagree would report `duplicate: false` and then dedupe on the worker):
- `tree/data/conversation/conversation.py`: `def conversation_source_uri(text: str, session_uri: str | None) -> str`
  — `session_uri` verbatim when given (blank → `ValueError`, the message the leaf raises today),
  else `f"conversation://{_content_hash(text)}"`. The leaf's inline derivation (lines ~95–100)
  is replaced by a call.
- `tree/data/file/file.py`: `def file_source_uri(file_path: str) -> str` → `f"file://{Path(file_path)}"`;
  the leaf calls it.
- `tree/data/online_pipeline.py`: `def source_uri_for(source: OnlineSource) -> str` — the ONE
  entry the dispatcher uses: `UrlSource` → if the host matches a YouTube pattern in
  `_URL_HANDLERS` and `tree.data.youtube.youtube.extract_video_id(url)` is not `None`,
  `canonical_video_url(video_id)`; otherwise the URL verbatim (substack + web leaves store it
  verbatim). `FileSource` → `file_source_uri(path)`. `ConversationSource` →
  `conversation_source_uri(text, session_uri)`. Pure, no I/O.

**2. `IngestReceipt` (Pydantic, `tree/online.py`)** — `source_uri: str`, `duplicate: bool`,
`document_id: str | None = None`, `flow_run_id: str | None = None`, `status: str`. Field
descriptions state the invariant: `document_id` is set iff `duplicate` (the id is only known
on the duplicate path); `flow_run_id` is set iff a run was dispatched.

**3. `dispatch_online_pipeline` returns `IngestReceipt`.** After `validate_online_source`:
`source_uri = source_uri_for(source)`; `existing = await Document.find_one({"user_id": user_id,
"source_uri": source_uri})`. Hit with `existing.source_type != SourceType.LATENT` → log INFO
"duplicate at submit time: <uri>" and return `IngestReceipt(source_uri, duplicate=True,
document_id=str(existing.id), flow_run_id=None, status="duplicate")` — NOTHING is dispatched.
Miss or LATENT (the leaf upgrades LATENT in place — not a duplicate) → `run_deployment` as today,
`IngestReceipt(source_uri, duplicate=False, flow_run_id=str(flow_run.id), status=flow_run_status(flow_run))`.
Docstring: one sentence that `duplicate` is "known at submit time" — two concurrent submits of
the same URI can both read `False`; the worker still dedupes (`DuplicateKeyError` path).
Both callers already initialise Beanie (`mcp/server.py:162`, `cli.py:113`).

**4. Callers:** `mcp/tools.py::_ingest` → `json.dumps({**receipt.model_dump(), **dup_extra})`
(per-tool extras `url` / `file_path` stay). `tree/cli.py::wait_for_dispatch` (used by
`scripts/run_pipeline.py MODE=online`) accepts the receipt: `flow_run_id is None` → print
"Already ingested: <source_uri> (document <id>)" and return without waiting. Tool docstrings
for the three ingest tools describe the receipt shape and the duplicate case.

## Out of scope
- `SOURCE_URIS` retry path (#123). Error envelope (#126). SKILL.md text (#128).
- Changing `Document._id`, the `(user_id, source_type, source_uri)` unique index, or LATENT semantics.

## Acceptance Criteria

- [x] `source_uri_for(UrlSource(uri="https://www.youtube.com/watch?v=abc123XYZ_-"))` equals `canonical_video_url("abc123XYZ_-")`; a substack/web URL round-trips verbatim; `FileSource(path="/tmp/a.md", …)` → `file:///tmp/a.md`; conversation with `session_uri` → verbatim, without → `conversation://<16 hex>` — `tests/unit/data/test_online_pipeline.py::TestSourceUriFor`.
- [x] `conversation_source_uri("text", "  ")` raises `ValueError`; the leaf `ingest_conversation` produces the SAME `source_uri` as the helper for both branches — `tests/unit/data/test_conversation.py::TestConversationSourceUri`.
- [x] `ingest_file` leaf and `file_source_uri` agree — `tests/unit/data/test_file.py::TestFileSourceUri::test_file_source_uri_matches_leaf`.
- [x] `dispatch_online_pipeline` on an existing non-LATENT Document returns `IngestReceipt(duplicate=True, document_id=<id>, flow_run_id=None, status="duplicate")` and `run_deployment` is NOT called — `tests/unit/test_online.py::TestDispatchOnlineIngest::test_duplicate_short_circuits_without_dispatch`.
- [x] On a LATENT hit and on a miss it dispatches and returns `duplicate=False, document_id=None, flow_run_id=<id>` — `::test_latent_row_is_not_a_duplicate`, `::test_submits_the_deployment_fire_and_forget` (updated).
- [x] `ingest_url` / `ingest_file` / `ingest_conversation` answers are JSON with exactly the keys `source_uri, duplicate, document_id, flow_run_id, status` (+ `url` / `file_path`) — `tests/unit/mcp/test_tools.py::TestIngestReceipt` (one test per tool, duplicate and dispatched cases).
- [x] `wait_for_dispatch` returns without polling when `flow_run_id is None` — `tests/unit/test_cli.py::TestWaitForDispatch::test_wait_for_dispatch_skips_duplicate_receipt`.
- [x] `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green (env `local`).

## User Stories

### Story: Model ingests a URL that is already in memory
1. Assistant calls `ingest_url(url="https://example.substack.com/p/post")` for a URL ingested yesterday.
2. Answer: `{"source_uri": "https://example.substack.com/p/post", "duplicate": true, "document_id": "66f…", "flow_run_id": null, "status": "duplicate", "url": "…"}`.
3. No Prefect flow run exists for it (`prefect flow-run ls` shows none new); the assistant tells the user the page is already in memory.

### Story: Model ingests a new YouTube video via a short link
1. Assistant calls `ingest_url(url="https://youtu.be/abc123XYZ_-")`.
2. Answer has `source_uri == "https://www.youtube.com/watch?v=abc123XYZ_-"`, `duplicate: false`, `flow_run_id` set, `status: "scheduled"`.
3. A second call with the long-form URL answers `duplicate: true` with the same `source_uri`.

### Story: Operator retries an online ingest from the CLI on a duplicate
1. Operator runs `make memory-run-pipeline MODE=online SOURCE="/tmp/notes.md"` twice.
2. Second run prints `Already ingested: file:///tmp/notes.md (document 66f…)` and exits 0 without waiting on a flow run.

### Story: Two concurrent submits of the same conversation
1. Two harness sessions call `ingest_conversation` with the same `session_uri` within the same second.
2. Both receipts read `duplicate: false`; the worker dedupes the second on `DuplicateKeyError`; exactly one Document exists.

---

Blocked by: (none)

## Log

### [PA] 2026-09-12 10:00 — Grooming

**Summary**
Ingest tools answer with the Document's natural key and a submit-time duplicate verdict; the URI derivation is lifted into one helper per leaf so the dispatcher and the worker cannot disagree.

**Key decisions**
- Natural key over surrogate id: `Document._id` stays random; identity is `(user_id, source_uri)` (ADR-008 §1).
- `source_uri_for` mirrors the leaves exactly — YouTube canonicalises, files are `file://`, not "verbatim" as the plan assumed.
- LATENT hit is not a duplicate (the leaf upgrades it), keeping `document_id != null ⇔ duplicate`.
- The race is accepted and documented, not locked.

**Dependencies**
- None.

**User stories**
- 4 stories covering: duplicate URL, canonicalised YouTube URL, CLI duplicate, concurrent race.

Ready for implementation.

### [SWE] 2026-09-12 14:05 — Implementation

**Files modified**
- `apps/memory/src/tree/data/conversation/conversation.py` — new `conversation_source_uri(text, session_uri)`; the leaf's inline derivation (and its blank-`session_uri` `ValueError`) now lives in the helper the leaf calls.
- `apps/memory/src/tree/data/file/file.py` — new `file_source_uri(file_path)`; `load_file_document` calls it.
- `apps/memory/src/tree/data/online_pipeline.py` — new `source_uri_for(source)` (pure) + `_YOUTUBE_URL_PATTERNS`, read off `_URL_HANDLERS` so it canonicalises exactly the URLs `_ingest_url` routes to the YouTube leaf.
- `apps/memory/src/tree/online.py` — new `IngestReceipt` (field descriptions carry the `document_id ⇔ duplicate` / `flow_run_id ⇔ dispatched` invariants); `dispatch_online_pipeline` now derives the natural key, does ONE pre-flight `Document.find_one({user_id, source_uri})`, short-circuits a non-LATENT hit and returns the receipt.
- `apps/memory/src/tree/mcp/tools.py` — `_ingest` serializes `receipt.model_dump()` + `dup_extra`; the three ingest tool docstrings now describe the receipt and the duplicate case (this is what the model reads at call time).
- `apps/memory/src/tree/cli.py` — `wait_for_dispatch` accepts `dict | IngestReceipt` (`TYPE_CHECKING` import, `isinstance(dict)` discriminator — no runtime import of `tree.online`) and returns without polling on `flow_run_id is None`.
- `apps/memory/tests/unit/data/test_online_pipeline.py` — `TestSourceUriFor` (5 tests: long/short/mobile YouTube, substack/web/`@channel` verbatim, file, conversation both branches).
- `apps/memory/tests/unit/data/test_conversation.py` — `TestConversationSourceUri` (verbatim / hash / blank raises / leaf-agrees for both branches).
- `apps/memory/tests/unit/data/test_file.py` — `TestFileSourceUri` (scheme + leaf-agrees).
- `apps/memory/tests/unit/test_online.py` — `TestDispatchOnlineIngest`: `mock_find_one` fixture (miss by default) + duplicate short-circuit, LATENT-is-not-a-duplicate, duplicate log line; the fire-and-forget and status tests now assert the receipt.
- `apps/memory/tests/unit/mcp/test_tools.py` — `TestIngestReceipt`: one test per tool × {dispatched, duplicate}, asserting the EXACT key set.
- `apps/memory/tests/unit/test_cli.py` — duplicate-receipt skip + dispatched-receipt wait (the plain-dict test stays, five other scripts still pass dicts).
- `apps/memory/tests/unit/mcp/test_tools_user_id_pinning.py` — `_SUBMITTED` is now an `IngestReceipt` (a dict has no `model_dump`).

**Tests**
- Unit: 2922 passing, 0 failing (`make memory-tests`, env `local`).
- Integration: N/A — this repo has no integration suite (AGENTS.md); e2e verification by running the real pipelines, below.

**Acceptance criteria**
- [x] `source_uri_for` per variant — `tests/unit/data/test_online_pipeline.py::TestSourceUriFor` (adds `youtu.be` + a YouTube URL with no video id, the only guard against `canonical_video_url(None)`).
- [x] Blank `session_uri` raises; leaf agrees with the helper — `tests/unit/data/test_conversation.py::TestConversationSourceUri`.
- [x] File leaf agrees with `file_source_uri` — `tests/unit/data/test_file.py::TestFileSourceUri::test_file_source_uri_matches_leaf`.
- [x] Duplicate short-circuits without dispatch — `tests/unit/test_online.py::TestDispatchOnlineIngest::test_duplicate_short_circuits_without_dispatch`.
- [x] LATENT hit and miss both dispatch — `::test_latent_row_is_not_a_duplicate`, `::test_submits_the_deployment_fire_and_forget`.
- [x] Tool answers carry exactly the receipt keys (+ `url` / `file_path`) — `tests/unit/mcp/test_tools.py::TestIngestReceipt` (3 tools × dispatched/duplicate).
- [x] `wait_for_dispatch` skips a duplicate — `tests/unit/test_cli.py::TestWaitForDispatch::test_wait_for_dispatch_skips_duplicate_receipt`.
- [x] QA loop green (env `local`).

**Evidence**

```
$ make memory-tests
======================= 2922 passed in 76.41s (0:01:16) ========================

$ make memory-format-check && make memory-lint-check && make pre-commit
288 files already formatted
All checks passed!
ruff check...............................................................Passed
ruff format..............................................................Passed
biome check (harness)....................................................Passed
```

Story 3 (CLI duplicate), with `make memory-serve-workflows` running from THIS worktree in `rag` mode:

```
$ TREE_MEMORY__MODE=rag make memory-run-pipeline MODE=online SOURCE="<scratch>/notes.md"   # 1st
Submitted flow run ba50a786-e960-4218-bfb6-f16015fb8661 (scheduled); waiting for it...
Done. Flow completed successfully.

$ TREE_MEMORY__MODE=rag make memory-run-pipeline MODE=online SOURCE="<scratch>/notes.md"   # 2nd
Duplicate at submit time: file:///private/tmp/.../scratchpad/notes.md
Already ingested: file:///private/tmp/.../scratchpad/notes.md (document 6aa52e8a720054c4c86acc65)
   -> exit 0, no flow run created, no polling
```

Stories 1/4 over a REAL stdio MCP session (`scripts/serve_mcp.py --transport stdio` + `fastmcp.Client`), two rounds:

```
--- ingest_file (already ingested)
{"source_uri": "file:///.../notes.md", "duplicate": true, "document_id": "6aa52e8a720054c4c86acc65",
 "flow_run_id": null, "status": "duplicate", "file_path": "/.../notes.md"}

--- ingest_conversation(session_uri="claude-session://receipt-e2e") x2, back-to-back (round 1)
{"source_uri": "claude-session://receipt-e2e", "duplicate": false, "document_id": null, "flow_run_id": "693db2c5-...", "status": "scheduled"}
{"source_uri": "claude-session://receipt-e2e", "duplicate": false, "document_id": null, "flow_run_id": "4c9ad7b0-...", "status": "scheduled"}
   -> the documented submit-time race: both read false; the worker deduped ->
      db.documents.countDocuments({source_uri: "claude-session://receipt-e2e"}) == 1

--- the same two calls after the worker landed the Document (round 2)
{"source_uri": "claude-session://receipt-e2e", "duplicate": true, "document_id": "6aa52ef7e46b097d48e9a297", "flow_run_id": null, "status": "duplicate"}
```

Story 2's canonicalisation, pure-function check (a real YouTube ingest is NOT RUN — needs a transcript fetch + quota):

```
$ uv run python -c "... source_uri_for ..."
https://youtu.be/abc123XYZ_-                -> https://www.youtube.com/watch?v=abc123XYZ_-
https://www.youtube.com/watch?v=abc123XYZ_-  -> https://www.youtube.com/watch?v=abc123XYZ_-
https://example.substack.com/p/post          -> https://example.substack.com/p/post
https://www.youtube.com/@channel             -> https://www.youtube.com/@channel
```

**Notes**
- `wait_for_dispatch` normalizes to a dict rather than importing `IngestReceipt` at runtime: five other scripts pass plain `{"status", "flow_run_id"}` dicts, and `from tree.online import ...` inside `tree.cli` would drag `tree.memory.pipeline` into every CLI entry point.
- `find_one` filters on `(user_id, source_uri)` only — no `source_type` — mirroring `load_file_document` / `load_conversation_document` exactly, which is the point of the shared helper. The unique index is on the triple, so the pair can in principle match more than one row; the leaves already behave this way.
- Bug found during the run: `tests/unit/mcp/test_tools_user_id_pinning.py` patched the dispatcher with a dict, which now hits `receipt.model_dump()` (red -> green after switching the fixture to `IngestReceipt`). No production bug surfaced.
- Cleanup: the two e2e Documents + their 3 `memory` rows were deleted from the local DB; `make memory-serve-workflows` and the stdio MCP server are stopped. MCP over HTTP was NOT used — port 8000 is held by an unrelated local container and `serve-mcp` has no port knob, so stdio covered it.
- FYI for the PA (pre-existing, unrelated): `.env`'s `TREE_USER_IDENTIFIER=paul.iusztin@example.com` does not match the only seeded local user (`paul@example.com`), so `make memory-serve-mcp` cannot boot locally without an override.

**Addendum — callers and consumers audited after the run**

- The THIRD caller of `dispatch_online_pipeline`, `scripts/run_data_pipeline.py::_run_online:92`, calls `connect_and_resolve_user` before dispatching, so Beanie is initialised for the new pre-flight `find_one` (its unit test mocks the dispatcher and would NOT have caught a missing init).
- Value-level consumers of the answer: none in code. `flow_run_id` / `status` appear only in `.agents/skills/tree-memory/SKILL.md` (#128's scope — still promises `{"status": <Prefect run state>, "flow_run_id"}` and must be rewritten there) and in `docs/adrs/002_…md:309`, a historical consequence line that ADR-008 §1 amends — I am read-only on ADRs, so the PA decides whether to add a superseded-by note. No harness TypeScript consumer parses an ingest answer.
- New VALUES the key-compatible answer can carry, for whoever reads them next: `status: "duplicate"` is not a Prefect state name, and `flow_run_id` can be `null`.

### [Tester] 2026-09-12 14:04 — QA

**Test summary**
- Format / lint / pre-commit: PASS (`make memory-format-check && make memory-lint-check`, `make pre-commit` — ruff format/check, prettier, biome all green)
- Unit tests: 2922 passed / 0 failed (`make memory-tests`, env `local`, Docker infra already up: `kitaru-local-db-1`, `tree-mongodb`, `tree-prefect-server`, `tree-prefect-worker`, `tree-mongot`)
- Integration tests: N/A — no integration suite in this repo (AGENTS.md); e2e verification done against the real local MongoDB below
- Warnings: 0 (pytest run showed only an unrelated `opik`/Pydantic-v1/Python-3.14 `UserWarning` printed at import time, pre-existing, not from this diff)

**E2E adversarial pass**
- Happy path: `uv run python -c "source_uri_for(UrlSource(uri='https://youtu.be/dQw4w9WgXcQ'))"` → `https://www.youtube.com/watch?v=dQw4w9WgXcQ` == `canonical_video_url("dQw4w9WgXcQ")` (PASS)
- Break path 1 (leaf-vs-helper divergence, boundary/malformed inputs): YouTube short link `https://youtu.be/dQw4w9WgXcQ` → canonicalised match; `www.`-prefixed substack URL `https://www.example.substack.com/p/post` → verbatim `https://www.example.substack.com/p/post`; file path with spaces + unicode `/tmp/my notes ünïcödé 笔记.md` → `source_uri_for(FileSource(...))` and `file_source_uri(...)` both return `file:///tmp/my notes ünïcödé 笔记.md` (identical); conversation with blank `session_uri="   "` via `source_uri_for` → raised `ValueError: session_uri must not be empty...` (PASS, all four match expected)
- Break path 2 (state edge: LATENT row present must still dispatch): inserted a real `SourceType.LATENT` Document into local MongoDB for `file:///tmp/qa-adversarial-latent-test.md`, called `dispatch_online_pipeline` with `run_deployment` mocked — result: `duplicate=False, document_id=None, flow_run_id='fake-run-id', status='scheduled'`, `run_deployment` called once. Expected: dispatch fires, not a duplicate (PASS)
- Break path 3 (cross-tenant boundary: same `source_uri`, different `user_id` must NOT collide): inserted a real Document for `USER_A` at `file:///tmp/qa-adversarial-cross-user-test.md`; `dispatch_online_pipeline` for `USER_B` with the same path → `duplicate=False, flow_run_id` set (dispatched); the same call for `USER_A` → `duplicate=True, document_id=<USER_A's doc id>`. Expected: the pre-flight lookup is scoped by `(user_id, source_uri)`, not `source_uri` alone (PASS)
- Break path 4 (hostile/malformed input via the MCP tool boundary): `ingest_conversation(conversation_text="hello there", session_uri="   ")` (in-process, mocked ctx) → `{"error": "invalid_input", "detail": "session_uri must not be empty when supplied; pass None to fall back to the content-hash source_uri."}`, no traceback leaked, no crash. Expected: `ValueError` from `source_uri_for` is caught by the tool's `except ValueError` and returned as a clean error envelope (PASS)
- Break path 5 (backward compatibility: plain dict): `tests/unit/test_cli.py::TestWaitForDispatch::test_waits_on_the_submitted_flow_run` passes a `{"status": "scheduled", "flow_run_id": "abc"}` dict through `wait_for_dispatch` and asserts it still polls — ran in isolation, 1 passed (PASS)

All adversarial DB-backed scripts cleaned up after themselves; verified `mongosh` shows 0 leftover `qa-adversarial*` documents.

**Acceptance criteria**
- [x] PASS — `source_uri_for` per-variant derivation (YouTube canonicalise / substack+web verbatim / file / conversation both branches) — `tests/unit/data/test_online_pipeline.py::TestSourceUriFor` (8 tests, all pass); manually re-derived all four variants above, all matched
- [x] PASS — `conversation_source_uri("text", "  ")` raises `ValueError`; leaf agrees with the helper for both branches — `tests/unit/data/test_conversation.py::TestConversationSourceUri` (5 tests, all pass, incl. `test_leaf_derives_the_same_source_uri` parametrized hash/session)
- [x] PASS — `ingest_file` leaf agrees with `file_source_uri` — `tests/unit/data/test_file.py::TestFileSourceUri::test_file_source_uri_matches_leaf` passes
- [x] PASS — duplicate (non-LATENT) short-circuits without dispatch — `tests/unit/test_online.py::TestDispatchOnlineIngest::test_duplicate_short_circuits_without_dispatch` passes; `mock_run.assert_not_awaited()` verified
- [x] PASS — LATENT hit and miss both dispatch with `duplicate=False, document_id=None` — `::test_latent_row_is_not_a_duplicate`, `::test_submits_the_deployment_fire_and_forget` both pass; reconfirmed against a real Mongo LATENT row (break path 2 above)
- [x] PASS — the three ingest tools answer exactly `{source_uri, duplicate, document_id, flow_run_id, status}` (+ `url`/`file_path`) — `tests/unit/mcp/test_tools.py::TestIngestReceipt` (6 tests: 3 tools × dispatched/duplicate) all pass, each asserting `set(payload) == _RECEIPT_KEYS | {...}`
- [x] PASS — `wait_for_dispatch` skips polling when `flow_run_id is None` — `tests/unit/test_cli.py::TestWaitForDispatch::test_wait_for_dispatch_skips_duplicate_receipt` passes; plain-dict backward path also re-verified (break path 5)
- [x] PASS — `make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests` green (env `local`) — reran independently, 2922 passed, 0 failed, lint/format/pre-commit all green

**Evidence**
```
$ make env-status
Env target: local (.env)

$ make memory-format-check && make memory-lint-check
288 files already formatted
All checks passed!

$ make pre-commit
ruff check...............................................................Passed
ruff format..............................................................Passed
biome check (harness)....................................................Passed

$ make memory-tests
============================ 2922 passed in 52.64s =============================

$ uv run pytest -q tests/unit/data/test_online_pipeline.py::TestSourceUriFor tests/unit/data/test_conversation.py::TestConversationSourceUri tests/unit/data/test_file.py::TestFileSourceUri::test_file_source_uri_matches_leaf tests/unit/test_online.py::TestDispatchOnlineIngest::test_duplicate_short_circuits_without_dispatch tests/unit/test_online.py::TestDispatchOnlineIngest::test_latent_row_is_not_a_duplicate tests/unit/test_online.py::TestDispatchOnlineIngest::test_submits_the_deployment_fire_and_forget tests/unit/mcp/test_tools.py::TestIngestReceipt tests/unit/test_cli.py::TestWaitForDispatch::test_wait_for_dispatch_skips_duplicate_receipt tests/unit/test_cli.py::TestWaitForDispatch::test_waits_on_the_submitted_flow_run
27 passed in 1.62s
```

**Other issues found**
- None blocking. Note (not a defect): `source_uri_for`'s YouTube domain match (`pattern in domain`) inherits the pre-existing `_ingest_url` substring-matching quirk (e.g. a contrived domain containing "youtube.com" as a substring would route to YouTube) — this is intentional mirroring of existing routing behavior per the task's own design goal (helper must never diverge from the leaf), not a regression introduced here.
- The `code-review` plugin is enabled in `.claude/settings.json`, but this session's toolset has no mechanism to invoke a Claude Code slash-command plugin directly; substituted with a full manual line-by-line diff review of all 6 source files and 7 test files instead.

**VERDICT: PASS**
