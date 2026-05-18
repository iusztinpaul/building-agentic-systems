# Conversation ingestion polish (Phase 2)

Status: pending
Tags: `phase-2`, `conversation`, `data-pipeline`, `cleanup`
Depends on: None
Blocks: #027, #028, #029, #030, #031, #032, #033

## Scope

Finish Phase 1's loose ends in the conversation ingestion path so the feature can move to the (large) Phase 3 ontology refactor on a clean foundation. Per `plan.md:79–95` ("Phase 2 — Conversation ingestion polish (small)"), this is intentionally small: `user_id` plumbing has already landed in Phase 1, so what remains is `source_uri` semantics, a small `Document` field for conversation session metadata, and a dead-code purge.

### Files touched

- `apps/memory/src/tree/data/conversation.py` — `load_conversation_document` signature gains `session_uri: str | None = None`, `session_started_at: datetime | None = None`. `source_uri` derivation rule: caller-supplied `session_uri` wins; else fall back to the existing `f"conversation://{_content_hash(text)}"`.
- `apps/memory/src/tree/data/conversation_pipeline.py` — pass the new optional kwargs through `ingest_conversation` and its inner task.
- `apps/memory/src/tree/entities/documents.py` — add `metadata: dict[str, Any] = Field(default_factory=dict)` on `Document` if not already present; document the `session_started_at` convention in the docstring (no new index on it).
- `apps/memory/src/tree/mcp/tools.py` — extend the `ingest_conversation` MCP tool signature with the same two optional kwargs and forward.
- `apps/memory/src/tree/data/conversation_pipeline.py` (sweep) — purge any leftover "structured messages" path code that was sketched but never landed (per `plan.md:89`). Confirm via grep: `Grep -rn "structured" apps/memory/src/tree/data/conversation*`.
- `apps/memory/tests/unit/data/test_conversation.py` (new or extended) — unit tests for the `source_uri` derivation rule, `metadata.session_started_at` round-trip, and the dedup behavior described below.
- Optional: a "long transcript" smoke test that runs the existing chunker on a ~50KB transcript and asserts the chunks come out in reasonable count + size (per `plan.md:87`).

### `source_uri` derivation contract

```python
async def load_conversation_document(
    conversation_text: str,
    user_id: PydanticObjectId,
    title: str | None = None,
    session_uri: str | None = None,        # NEW — caller-supplied session id
    session_started_at: datetime | None = None,  # NEW — UTC-aware
) -> Document | None:
    """...

    source_uri rule:
      - If session_uri is provided, source_uri = session_uri verbatim
        (caller is responsible for it being a stable, opaque, schemed
        string — e.g. "claude-session://abc123", "mcp-session://...",
        "openai-thread://thread_..."). No validation beyond non-empty.
      - Otherwise, source_uri = "conversation://{_content_hash(text)}".
        (Current behavior — preserved for backwards compatibility with
        Phase-1 ingest paths that did not propagate a session id.)
    """
```

`session_started_at`, if provided, MUST be timezone-aware (UTC). Stored in `Document.metadata["session_started_at"]` as an ISO 8601 string OR as a tz-aware `datetime` (whichever Beanie/Mongo preserves losslessly; the unit test pins the chosen shape). A naive `datetime` raises `ValueError` per `CLAUDE.md` ("All the dates are timezone aware (UTC by default). We don't accept any naive datetime objects.").

### Idempotency

The existing `(user_id, source_type, source_uri)` unique index on `Document` (Phase 1) continues to do all the work. Two callers passing the same `session_uri` get one Document; two callers passing distinct `session_uri`s get two Documents even if the text is byte-identical. Two callers passing no `session_uri` and identical text get one Document (content hash). All three behaviors are pinned by unit tests.

### Dead-code purge

Per `plan.md:89` — "Remove any dead code on the old 'structured messages' path that was sketched but never landed." Method:

1. `Grep -rn "structured\|Message\|MessageProperties\|messages_collection" apps/memory/src/tree/data/` and `apps/memory/src/tree/entities/`.
2. Any sketched-but-unreferenced code (no imports of it from live modules, no tests covering it, no MCP tool wiring) is deleted in this task.
3. If the grep returns nothing, this step is a no-op — note in the commit message.

### Long-transcript chunker smoke

Per `plan.md:87` — verify the existing `langchain-text-splitters` chunker behaves reasonably on long transcripts. Acceptance: one unit test (not integration — no Mongo / Prefect needed) that runs the configured `RecursiveCharacterTextSplitter` (or whatever the extraction pipeline uses) on a fixture conversation text of ~50KB and asserts (a) the chunk count is bounded (≤ ~200), (b) every chunk is non-empty, (c) every chunk is ≤ the configured `chunk_size + chunk_overlap`. This is a regression check, not a behavior change — if the chunker has a problem, file as a separate task; this task documents current behavior.

## Acceptance Criteria

- [x] `load_conversation_document(conversation_text, user_id, title=None, session_uri=None, session_started_at=None)` is the new signature; all five params present; only `conversation_text` and `user_id` are required.
- [x] Unit test: calling with `session_uri="claude-session://abc"` writes a `Document` with `source_uri == "claude-session://abc"` (no `conversation://` prefix, no content hash).
- [x] Unit test: calling with `session_uri=None` writes a `Document` with `source_uri == "conversation://{_content_hash(text)}"` — current Phase-1 behavior preserved.
- [x] Unit test: two calls with the same `session_uri` and same `user_id` return the same Document on the second call (idempotent — second call returns `None`, per current contract).
- [x] Unit test: two calls with **different** `session_uri`s and the same `user_id` and **identical text** produce **two** distinct Documents (different `source_uri` → not deduped).
- [x] Unit test: calling with a naive `session_started_at` raises `ValueError` (or whatever Pydantic raises) with a clear message; calling with a tz-aware `datetime` round-trips through `Document.metadata["session_started_at"]`.
- [x] `apps/memory/src/tree/data/conversation_pipeline.py::ingest_conversation` Prefect flow signature extended with `session_uri` and `session_started_at` and forwards them. The MCP tool at `apps/memory/src/tree/mcp/tools.py::ingest_conversation` exposes both as optional kwargs (default `None`) — verified by reading the tool registration / signature.
- [x] `Grep -rn "structured" apps/memory/src/tree/data/ apps/memory/src/tree/entities/` returns either zero hits (nothing to purge) or only hits that survived as live code with tests. Any dead-code lines that fail this check are removed in this task and the deletion shows up in the diff.
- [x] Long-transcript chunker smoke unit test passes: a ~50KB fixture transcript chunks to ≤ ~200 non-empty chunks, each ≤ `chunk_size + chunk_overlap` chars.
- [x] `make memory-format-fix && make memory-lint-fix && make memory-format-check && make memory-lint-check` clean.
- [x] `make pre-commit` green.
- [x] `make memory-unit-tests` green.
- [x] `make memory-integration-tests` green (fast loop only — no slow markers required for this task).

## User Stories

### Story: An MCP-driven agent ingests the current conversation by session id
1. The MCP client calls `mcp__tree-memory__ingest_conversation(conversation_text="...", title="Today's chat", session_uri="claude-session://abc123", session_started_at="2026-05-17T14:30:00Z")`.
2. The MCP tool resolves the active `user_id` from server startup config (Phase 1 behavior) and calls `ingest_conversation` with all five fields.
3. The pipeline writes `Document(source_type=CONVERSATION, source_uri="claude-session://abc123", user_id=<resolved>, title="Today's chat", content="...", metadata={"session_started_at": "2026-05-17T14:30:00Z"})`.
4. The caller invokes the same tool again with the same `session_uri` and updated text 30s later (e.g. the conversation continued); the second call returns `None` (idempotent) and the original Document is unchanged.

### Story: A legacy caller without a session id still works
1. A script calls `await ingest_conversation(conversation_text="hello there", user_id=<oid>)` — no `session_uri`, no `session_started_at`.
2. The pipeline writes a Document with `source_uri == "conversation://{hash(\"hello there\")[:16]}"` (current Phase-1 behavior).
3. `Document.metadata == {}` — no `session_started_at` key.
4. Re-running with the same text and same `user_id` returns `None` — content-hash idempotency preserved.

### Story: Two users ingest a transcript with the same session id from different MCP servers
1. User A's MCP server (configured with `--user-id <A>`) ingests `session_uri="meeting://2026-05-17-standup"`.
2. User B's MCP server (configured with `--user-id <B>`) ingests `session_uri="meeting://2026-05-17-standup"` independently.
3. The compound unique index `(user_id, source_type, source_uri)` lets both Documents land — one row per tenant.
4. Each user's KG extraction runs only over their own row. The Phase-1 two-user isolation guarantee is preserved.

### Story: A long transcript chunks cleanly
1. A 50KB conversation transcript is passed to the chunker via the existing extraction-pipeline shape.
2. The chunker emits ≤ ~200 non-empty chunks, each within `chunk_size + chunk_overlap` characters.
3. No `RecursionError`, no `MemoryError`, no infinite loop. This is a documented current behavior — if it fails today, a new bug task is filed before this task can ship.

## Out of scope for this task

- Any `NodeType` / `EdgeType` / registry change — that's #027 onward.
- Any structured `{role, content}` message parsing — `plan.md:93–95` defers indefinitely.
- Any new MCP tool or CLI for session-scoped queries.
- Any retroactive backfill of `session_uri` onto existing Documents (the dev set was wiped in Phase 1; new Documents from here on use the new field if callers supply it).

## Test plan

- **Unit:** `tests/unit/data/test_conversation.py` — six tests as listed in ACs above (session_uri-wins, content-hash-fallback, idempotent-with-session-uri, distinct-session-uri-distinct-doc, naive-datetime-rejected, long-transcript-chunker-smoke).
- **Integration (fast loop):** the existing two-user isolation integration test from Phase 1 (#021) MUST stay green — this is the standing regression for the Phase-2 polish task.
- **No slow markers.** No mongot dependency.

---

Refs: `plan.md:79–95`, Phase-1 #021 (two-user isolation test).

## Log

### [SWE] 2026-05-18 10:15 — Implementation

**Files modified**
- `apps/memory/src/tree/data/conversation.py` — `load_conversation_document` signature gains `session_uri: str | None = None` and `session_started_at: datetime | None = None`; added `_normalize_session_started_at` (rejects naive `datetime`, canonicalizes to UTC); `source_uri` derivation: caller-supplied `session_uri` wins verbatim, else `conversation://{_content_hash(text)}` fallback; `metadata` propagated to the new `Document`.
- `apps/memory/src/tree/data/conversation_pipeline.py` — `ingest_conversation` flow + `load_conversation_document_task` task forward the two new optional kwargs.
- `apps/memory/src/tree/entities/documents.py` — added `metadata: dict[str, Any] = Field(default_factory=dict)`; docstring documents the Phase-2 `session_started_at` convention; no new index.
- `apps/memory/src/tree/mcp/tools.py` — MCP `ingest_conversation` tool exposes `session_uri` and `session_started_at` (ISO-8601 string) as optional kwargs; parses the timestamp (`fromisoformat`, with `"Z"` → `"+00:00"` accommodation) and forwards; surfaces `ValueError` from the core function as a structured `invalid_input` error.
- `apps/memory/tests/unit/data/test_conversation.py` — added three new test classes: `TestSourceUriDerivation` (5 tests), `TestSessionStartedAt` (4 tests), `TestLongTranscriptChunker` (1 test); preserved the existing 11 tests. Total in this file: 21 (was 11).

**Tests**
- Unit (this file): 21 passing, 0 failing — `uv run pytest tests/unit/data/test_conversation.py -q` → `21 passed in 1.02s`.
- Unit (full suite): 855 passing, 0 failing — `make memory-unit-tests` → `855 passed in 96.84s`.
- Integration (fast loop): 130 passing, 1 skipped, 46 deselected (slow) — `make memory-integration-tests` → `130 passed, 1 skipped, 46 deselected in 183.87s`. The Phase-1 two-user isolation regression (`tests/integration/entities/test_document_compound_unique.py`) is green (3 passed in that file).

**Acceptance criteria**
- [x] `load_conversation_document(conversation_text, user_id, title=None, session_uri=None, session_started_at=None)` is the new signature — only `conversation_text` and `user_id` are positional/required. Verified by `tests/unit/data/test_conversation.py::TestSourceUriDerivation::test_session_uri_used_verbatim_when_provided`.
- [x] Unit test: `session_uri="claude-session://abc"` → `source_uri == "claude-session://abc"`, no `conversation://` prefix — `tests/unit/data/test_conversation.py::TestSourceUriDerivation::test_session_uri_used_verbatim_when_provided`.
- [x] Unit test: `session_uri=None` → `source_uri == "conversation://{_content_hash(text)}"` — `tests/unit/data/test_conversation.py::TestSourceUriDerivation::test_session_uri_none_falls_back_to_content_hash`.
- [x] Unit test: same `(user_id, session_uri)` → second call returns `None` — `tests/unit/data/test_conversation.py::TestSourceUriDerivation::test_same_session_uri_returns_none_on_second_call`.
- [x] Unit test: distinct `session_uri`s + identical text → two distinct Documents — `tests/unit/data/test_conversation.py::TestSourceUriDerivation::test_distinct_session_uris_produce_distinct_documents`.
- [x] Unit test: naive `session_started_at` → `ValueError` (clear "timezone-aware" message); tz-aware `datetime` round-trips through `Document.metadata["session_started_at"]` — `tests/unit/data/test_conversation.py::TestSessionStartedAt::test_naive_datetime_rejected` + `::test_tz_aware_utc_roundtrips_to_metadata` + `::test_non_utc_tz_aware_normalized_to_utc`.
- [x] `ingest_conversation` Prefect flow + MCP tool both extended with `session_uri` / `session_started_at` and forward — verified by reading the new signatures (`conversation_pipeline.py:38`, `mcp/tools.py:514`).
- [x] `Grep -rn "structured" apps/memory/src/tree/data/ apps/memory/src/tree/entities/` returns zero hits (no-op purge — confirmed below in Evidence). Same for `Message|MessageProperties|messages_collection`.
- [x] Long-transcript chunker smoke unit test passes — `tests/unit/data/test_conversation.py::TestLongTranscriptChunker::test_50kb_transcript_chunks_cleanly`. NOTE: the actual chunker in `tree.memory.extraction.core` is **token-based** (tiktoken `cl100k_base`), not `RecursiveCharacterTextSplitter` as the spec's prose suggested. The test bounds chunk size in tokens (the splitter's natural unit) — see test docstring.
- [x] `make memory-format-fix && make memory-lint-fix && make memory-format-check && make memory-lint-check` clean — see Evidence.
- [x] `make pre-commit` green — see Evidence.
- [x] `make memory-unit-tests` green — see Evidence.
- [x] `make memory-integration-tests` green — see Evidence.

**Evidence**

Lint / format / pre-commit (all clean):
```
$ make memory-format-fix
2 files reformatted, 216 files left unchanged
$ make memory-lint-fix
All checks passed!
$ make memory-format-check
218 files already formatted
$ make memory-lint-check
All checks passed!
$ make pre-commit
Validate pyproject.toml..............................(no files to check)Skipped
prettier.................................................................Passed
ruff check...............................................................Passed
ruff format..............................................................Passed
biome check (harness)....................................................Passed
KGQuery discipline (memory)..............................................Passed
```

Dead-code purge (no-op confirmed):
```
$ grep -rn "structured" apps/memory/src/tree/data/ apps/memory/src/tree/entities/
(no output — exit 1)
$ grep -rn "Message\|MessageProperties\|messages_collection" apps/memory/src/tree/data/ apps/memory/src/tree/entities/
(no output — exit 1)
```

Unit tests (full suite — 855 passed):
```
$ make memory-unit-tests
... (trimmed for brevity — relevant section)
tests/unit/data/test_conversation.py ..................... [ 12%]
...
tests/unit/test_migrate_multi_tenancy.py ........              [100%]
======================== 855 passed in 96.84s (0:01:36) ========================
```

Conversation-only test run:
```
$ uv run pytest tests/unit/data/test_conversation.py -q
.....................                                                    [100%]
21 passed in 1.02s
```

Integration tests (fast loop, 130 passed):
```
$ make memory-integration-tests
... (relevant section)
tests/integration/entities/test_document_compound_unique.py ...    [ 27%]
tests/integration/mcp/test_tools.py ............                   [ 57%]
...
========== 130 passed, 1 skipped, 46 deselected in 183.87s (0:03:03) ===========
```

End-to-end runtime exercise (`/tmp/exercise_ingest_conversation.py`, driven against the live MongoDB at `localhost:27017` — see "Notes" below for why the Prefect flow was not invoked directly):
```
$ uv --directory apps/memory run python /tmp/exercise_ingest_conversation.py

--- S1: session_uri verbatim + metadata round-trip ---
  source_uri        = 'claude-session://phase2-e2e-s1'
  metadata          = {'session_started_at': datetime.datetime(2026, 5, 17, 14, 30, tzinfo=datetime.timezone.utc)}
  source_type       = <SourceType.CONVERSATION: 'conversation'>
  reloaded.metadata = {'session_started_at': datetime.datetime(2026, 5, 17, 14, 30, tzinfo=FixedOffset(datetime.timedelta(0), 'UTC'))}
  PASS

--- S2: no session_uri → content-hash fallback ---
  source_uri = 'conversation://9e5624aa19af6edb'
  PASS

--- S3: replay same session_uri → 2nd call returns None ---
  first  = id=ObjectId('6a0abaa8daeaa83d868139f9') ... source_uri='claude-session://phase2-e2e-s3' ... content='First message.' ... metadata={}
  second = None
  reloaded.content = 'First message.'
  PASS

--- S4: distinct session_uris + identical text → two docs ---
  doc_a.source_uri = 'meeting://phase2-e2e-s4-a'
  doc_b.source_uri = 'meeting://phase2-e2e-s4-b'
  doc_a.id != doc_b.id: True
  PASS

--- S5: naive session_started_at → ValueError ---
  raised ValueError as expected: session_started_at must be timezone-aware (UTC); got a naive datetime.
  PASS

ALL SCENARIOS PASSED
```

**Notes**
- The `metadata` round-trip via Beanie/PyMongo deserializes the BSON datetime with `tzinfo=FixedOffset(0, 'UTC')` instead of the literal `UTC` singleton — the values compare equal (`stored == started_at`) and the test pins the round-trip on equality, not on tzinfo identity. This is standard PyMongo `tz_aware=True` behavior (see `tree.db.init_mongodb`).
- E2E exercise hits `load_conversation_document` directly (the function under test, against live MongoDB) rather than triggering the Prefect deployment via `prefect deployment run`. Reason: the `tree-prefect-worker` container that's currently running was started by a sibling worktree (containers `tree-mongodb` / `tree-prefect-server` / `tree-prefect-worker` were already up from another branch), so its serve process is pinned to that worktree's code — submitting a flow run from here would execute the *old* code from the other worktree, defeating the purpose of the exercise. The Prefect flow + task in `conversation_pipeline.py` is a 3-line `await load_conversation_document(...)` wrapper; its forwarding behavior is pinned by the unit tests' signature checks. **NOT RUN — `prefect deployment run ingest-conversation-etl/...` was not invoked from this worktree** because the wrong worker would have picked it up; the equivalent of the flow body was exercised via the direct-call script above.
- The chunker is token-based (tiktoken `cl100k_base`, default `chunk_size=512` tokens, `chunk_overlap=64`); the spec's "char" wording in the long-transcript AC was relaxed to "tokens" in the test (documented in the test's docstring). If the PM disagrees, the test can be re-bounded on `len(chunk)` instead — currently every chunk also satisfies `len(chunk) <= chunk_size * 6` chars-per-token-rough-estimate well under any sane bound.
- One-shot helper script `/tmp/exercise_ingest_conversation.py` is intentionally outside the repo (it's a runtime probe, not a regression test).
- DO NOT COMMIT — awaiting Tester review per role spec.

### [Tester] 2026-05-18 10:30 — QA

**Test summary**
- Format / lint / pre-commit: PASS
- Unit tests: 855 passed / 0 failed / 0 warnings (40.72s)
- Integration tests (full acceptance gate — `make memory-integration-tests-all`, slow + mongot included): 176 passed / 1 skipped / 0 failed / 0 warnings (434.43s)
- The single skipped test is `tests/integration/data/web/test_web_search_ingest.py` — a pre-existing skip unrelated to this task.

**E2E adversarial pass** — driver: `/tmp/tester_adversarial_026.py` (called the `@flow`-decorated `ingest_conversation` directly so the Prefect flow + task wrappers DID run, with real task-run logs; only the deployment-serialization layer was bypassed — see "Cross-check on Prefect deployment" below). All 23 sub-assertions across 10 scenarios PASS:

- Happy path: `ingest_conversation(text, user_id, session_uri="tester-026://happy", session_started_at=<UTC datetime>)` → Document persisted with `source_uri="tester-026://happy"`, `metadata={"session_started_at": <UTC>}`, round-trip from MongoDB equal to input. PASS
- Break path 1 (boundary: naive datetime): `session_started_at=datetime(2026,5,18,10,0,0)` (no tzinfo) → `ValueError("session_started_at must be timezone-aware (UTC); got a naive datetime.")` raised at the core function. PASS
- Break path 2 (idempotency: replay same `(user_id, source_uri)` with *different text*): first call → Document; second call → `None`; one row persisted; **original content preserved (not overwritten)** — confirming the existing-row-wins contract. PASS
- Break path 3 (boundary: empty inputs): empty / whitespace-only `conversation_text` → ValueError; empty / whitespace-only `session_uri` → ValueError (4 sub-cases). PASS
- Break path 4 (boundary: ~200KB transcript): no crash, no memory blow-up, full text persisted to Mongo as a single Document. PASS
- Break path 5 (state edge: non-UTC tz-aware): `datetime(..., tzinfo=UTC+02:00)` → stored as the equivalent UTC instant; verified by reload from DB. PASS
- Break path 6 (idempotency negative: identical text, distinct `session_uri`s): two Documents persisted with distinct ids — confirming `(user_id, source_type, source_uri)` uniqueness, not text-hash. PASS
- Break path 7 (legacy callers: no `session_uri`): content-hash URI used; second call returns None; `metadata == {}`. PASS
- Break path 8 (MCP tool — malformed inputs):
  - `session_started_at="this-is-not-a-date"` → `{"error": "invalid_input", "detail": "session_started_at must be an ISO-8601 datetime string..."}`. PASS
  - Empty `conversation_text` → `{"error": "empty_input", ...}`. PASS
  - `session_started_at="2026-05-18T10:00:00"` (parses as naive — no offset, no Z) → core ValueError caught and surfaced as `{"error": "invalid_input", "detail": "session_started_at must be timezone-aware..."}` — **important: the SWE's try/except around `_ingest_conversation` correctly catches this path**, so a malformed-but-parseable timestamp does NOT crash the MCP server. PASS
- Break path 9 (hostile inputs: Mongo operator chars + unicode in `session_uri`): `'evil://$ne":null,"x":"ünîcødé🐍'` stored verbatim, no injection (PyMongo treats it as a string value, not an operator expression). PASS

**Acceptance criteria**
- [x] PASS — `load_conversation_document(conversation_text, user_id, title=None, session_uri=None, session_started_at=None)` is the new signature; all five params present; only `conversation_text` and `user_id` are required. Evidence: `apps/memory/src/tree/data/conversation.py:44-50`; verified by `tests/unit/data/test_conversation.py::TestSourceUriDerivation::test_session_uri_used_verbatim_when_provided`.
- [x] PASS — `session_uri="claude-session://abc"` → `source_uri == "claude-session://abc"` (no `conversation://` prefix). Evidence: `tests/unit/data/test_conversation.py::TestSourceUriDerivation::test_session_uri_used_verbatim_when_provided` + adversarial happy path.
- [x] PASS — `session_uri=None` → `source_uri == "conversation://{_content_hash(text)}"`. Evidence: `tests/unit/data/test_conversation.py::TestSourceUriDerivation::test_session_uri_none_falls_back_to_content_hash` + adversarial break path 7.
- [x] PASS — Two calls with same `session_uri` and same `user_id` → second call returns `None`. Evidence: unit test `test_same_session_uri_returns_none_on_second_call` + adversarial break path 2 (live MongoDB).
- [x] PASS — Two calls with different `session_uri`s and identical text → two distinct Documents. Evidence: unit test `test_distinct_session_uris_produce_distinct_documents` + adversarial break path 6 (live MongoDB; two persisted rows, distinct ids).
- [x] PASS — Naive `session_started_at` raises `ValueError` with clear message; tz-aware datetime round-trips through `Document.metadata["session_started_at"]`. Evidence: unit tests `test_naive_datetime_rejected`, `test_tz_aware_utc_roundtrips_to_metadata`, `test_non_utc_tz_aware_normalized_to_utc` + adversarial break paths 1, 5, 8 (live MongoDB round-trip).
- [x] PASS — `ingest_conversation` Prefect flow signature extended with both kwargs and forwards them; MCP tool exposes both as optional kwargs (default `None`). Evidence: `apps/memory/src/tree/data/conversation_pipeline.py:38-44` (flow) and `apps/memory/src/tree/data/conversation_pipeline.py:21-32` (task); `apps/memory/src/tree/mcp/tools.py:509-571`. Live MCP signature inspection: `(conversation_text: str, ctx: Context = ..., title: str | None = None, session_uri: str | None = None, session_started_at: str | None = None) -> str`.
- [x] PASS — Dead-code purge grep is a no-op. Evidence: `grep -rn "structured" apps/memory/src/tree/data/ apps/memory/src/tree/entities/` → zero hits; `grep -rn "Message\|MessageProperties\|messages_collection" apps/memory/src/tree/data/ apps/memory/src/tree/entities/` → zero hits.
- [x] PASS — Long-transcript chunker smoke test passes: a 50KB fixture chunks to ≤ 200 non-empty chunks, each ≤ `chunk_size` tokens. Evidence: `tests/unit/data/test_conversation.py::TestLongTranscriptChunker::test_50kb_transcript_chunks_cleanly`. Note: the spec said "chars" but the project's `chunk_document` is token-based (`tiktoken cl100k_base`); SWE bounded by tokens with a docstring justification. Acceptable — the intent (no infinite loop / no RecursionError / bounded count) is met. Adversarial break path 4 separately exercised a 200KB transcript through the full ingest path (no chunking — chunking lives in the extraction pipeline, not the data-ingest path being polished here).
- [x] PASS — `make memory-format-check && make memory-lint-check` clean (218 files formatted, all lint checks passed).
- [x] PASS — `make pre-commit` green (5 hooks passed).
- [x] PASS — `make memory-unit-tests` green (855 passed, 0 warnings).
- [x] PASS — `make memory-integration-tests` green. **Plus** the acceptance-gate target `make memory-integration-tests-all` (slow + mongot) is green: 176 passed, 1 skipped (pre-existing), 0 failed. The spec only required the fast loop; the Tester ran the full gate as required by `docs/PROCESS.md`.

**Cross-check on Prefect deployment** — the SWE noted that `prefect deployment run ingest-conversation-etl/...` was NOT exercised because a sibling-worktree `tree-prefect-worker` container is currently pinned to that worktree's code (would have run the wrong code). Decision: **NOT A FAIL**. Reasons:
  1. The adversarial e2e driver imports and `await`s the `@flow`-decorated `ingest_conversation` directly. Prefect emits real flow-run + task-run logs throughout (visible in script output: "Flow run 'ivory-jackdaw' - Beginning flow run...", "Task run 'load-conversation-document-ded' - Finished in state Completed()"). The flow + task wrappers therefore DID execute end-to-end, including parameter forwarding, retry semantics, and the `ValueError` propagation through the Prefect task engine.
  2. The deployment-serialization layer (deployment registration, parameter serialization to JSON, worker pickup) is Prefect-framework code, not project code; the `to_deployment(...)` call in `orchestrator.py` is unchanged for `ingest-conversation`.
  3. The two new kwargs (`session_uri: str | None`, `session_started_at: datetime | None`) are JSON-serializable scalars; no exotic types that could trip Prefect's pickler.
  4. Stopping the sibling worker would disrupt the parallel-worktree development the user has set up. The risk-adjusted benefit of forcing a deployment-level run does not justify that disruption when the flow body and Prefect wrappers are already proven green by the direct-flow-call probe + the existing fast-loop integration `tests/integration/mcp/test_ingest_tools.py` (which exercises the same flow via the MCP server).

**Evidence**

```
$ make memory-format-check
218 files already formatted

$ make memory-lint-check
All checks passed!

$ make pre-commit
prettier.................................................................Passed
ruff check...............................................................Passed
ruff format..............................................................Passed
biome check (harness)....................................................Passed
KGQuery discipline (memory)..............................................Passed

$ make memory-unit-tests
============================= 855 passed in 40.72s =============================

$ make memory-integration-tests-all
================== 176 passed, 1 skipped in 434.43s (0:07:14) ==================

$ uv --directory apps/memory run python /tmp/tester_adversarial_026.py
... (Prefect flow + task logs trimmed)
=== SUMMARY ===
  PASSES: 23
  FAILS:  0
ALL E2E ADVERSARIAL SCENARIOS PASSED

$ grep -rn "structured" apps/memory/src/tree/data/ apps/memory/src/tree/entities/
(no output — exit 1)

$ grep -rn "Message\|MessageProperties\|messages_collection" apps/memory/src/tree/data/ apps/memory/src/tree/entities/
(no output — exit 1)
```

**Other issues found** — none. The implementation is clean:
- Validation lives at the core function boundary (`_normalize_session_started_at`); the MCP layer catches `ValueError` and surfaces a structured `invalid_input` envelope. No stack traces leak to MCP clients.
- The new `Document.metadata` field is a backwards-compatible Pydantic `dict[str, Any]` with a `default_factory=dict`, so existing Documents without the field deserialize cleanly (verified implicitly — the full integration suite reads/writes pre-existing fixtures across 176 tests, all green).
- Idempotency is consistent under both `session_uri` and content-hash paths and preserves the original Document's content on replay (verified live in break path 2).
- The "Notes" section's tzinfo observation (BSON deserializes UTC as `FixedOffset(0, 'UTC')` rather than `datetime.timezone.utc`) is accurate and harmless — `==` comparison works because `tzinfo.utcoffset()` is what counts; no test relies on tzinfo identity.

**Minor nit (not a FAIL — flagged for the SWE / PR Reviewer's awareness, not blocking):**
- `apps/memory/src/tree/mcp/tools.py:543` — `from datetime import datetime as _dt` is imported inside the function instead of at module top. Project convention (e.g. `conversation_pipeline.py:8`) is module-level imports. Cosmetic; ruff didn't flag it; leaving for PR-Reviewer discretion.

**VERDICT: PASS**

Hand-off note: the implementation meets every acceptance criterion with both unit-test evidence and live-MongoDB adversarial evidence. The Prefect-deployment-level run was substituted with a direct flow invocation that still exercises the Prefect engine end-to-end; the deviation is documented and justified above.

