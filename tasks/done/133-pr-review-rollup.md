---
status: done
feature: mcp-tool-contracts
---

# [PR review rollup] MCP tool contracts — ingest receipts, error envelope, retrieval outcome, SessionEnd hook (ADR-008)

Tags: `rollup`, `pr-review`
Refs: PR #43 (branch: `feat/mcp-tool-contracts`, HEAD `6d6820b`)

## Scope

PR Reviewer found 1 Blocker and 7 Nits in the diff (`git diff fcf697e...HEAD`, 63 files, +8292/-489). The SWE must fix the Blocker (and may fix Nits at their discretion) in a single coordinated pass, then hand back to the Tester. Pipeline re-runs from QA → PA acceptance → push → re-review.

The Blocker is small and mechanical (a ~6-line delegation the feature's own task-127 log already scheduled and never picked up). Everything else reviewed is sound — see the `[PR Reviewer]` entry on `tasks/done/130-pa-rejection-mcp-tool-contracts.md` for the evidence summary (3106/3106 tests, format + lint clean, envelope sweep, receipt invariant, gate ordering, hook purity, ADR-008 / glossary consistency).

## Acceptance Criteria

- [x] Blocker 1: `apps/memory/src/tree/memory/rag/search.py::_vector_index_is_queryable` decides the per-entry verdict by calling `tree.memory.rag.indexing.index_entry_is_queryable` — `grep -n 'entry.get("queryable")\|entry.get("status")' apps/memory/src/tree/memory/rag/search.py` returns nothing; the "Mirrors … case for case" sentences in BOTH docstrings (`search.py` and `indexing.py::index_entry_is_queryable`) are gone; `tests/unit/memory/rag/test_search.py::TestSearchMode` (`test_missing_vector_index_reports_text_only`, `test_building_vector_index_reports_text_only`, `test_empty_but_queryable_index_stays_hybrid`, `test_index_entry_without_status_or_queryable_stays_hybrid`) still green unchanged — they are behavioural and must not need edits.
- [x] Tester re-runs full QA suite (`make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests`, env `local`) and PASSES.
- [x] PA re-runs acceptance review and ACCEPTS.
- [ ] PR Reviewer re-runs and reports `NO BLOCKERS`.

## Blockers (detail)

### 1. [Clean code] — `apps/memory/src/tree/memory/rag/search.py:258-260`
- **What's wrong:** `_vector_index_is_queryable` re-implements the three-valued readiness rule that `indexing.py:478 index_entry_is_queryable` already exposes as a public helper — the expression `queryable is False or (queryable is None and status not in (None, "READY"))` IS `index_entry_is_queryable(entry) is False`, case for case. Both docstrings say so out loud ("Mirrors `tree.memory.rag.search._vector_index_is_queryable` case for case so the two readers … cannot drift apart") — the only thing stopping drift today is a sentence. `search.py:42` already imports from `indexing.py` (`_VECTOR_INDEX_NAME`), so there is no import-cycle reason for the copy. The feature's own log scheduled this: `tasks/done/127-vector-index-readiness-poll.md:200` ("Follow-up for `rag/search.py` (NOT done here — file owned by #124): `_vector_index_is_queryable` should delegate its per-entry decision to `index_entry_is_queryable`") and the Tester noted `index_entry_is_queryable` "has zero callers outside `indexing.py`" (`:294`). Task 124 has been done for hours in the same PR; the follow-up was never filed as a task and never picked up.
- **Why it's a Blocker:** duplicated block where a single helper is the obvious fix — and the helper already exists (Severity Rule, dimension B).
- **Suggested fix:** in `search.py`, replace lines 258-260 with `queryable = index_entry_is_queryable(entry)` / `if queryable is False:` (keep the WARNING and `return False`; keep the existing "no entry → unavailable" and "probe raised → available" branches, which are `search.py`'s own). Import `index_entry_is_queryable` next to `_VECTOR_INDEX_NAME`. Drop the "Mirrors …" sentence from both docstrings and, in `index_entry_is_queryable`, name `search.py` as the second caller instead. The log line may keep printing `entry.get("status")` / `entry.get("queryable")` for the operator — that is display, not a decision.
- **Regression test (if applicable):** none new — the four `TestSearchMode` index-probe tests already pin every row of the truth table from the caller's side; the point of the fix is that `TestIndexEntryIsQueryable::test_truth_table` now covers both readers.

## Nits (non-blocking; will be appended to PR description if pipeline advances)

### 1. [Simplicity] — `apps/memory/src/tree/mcp/tools.py:464-478`
- **Suggestion:** `delete:` the four `except` arms in `ingest_url` for `BrightDataConfigurationError`, `BrightDataRequestError`, `httpx.HTTPStatusError` and `(httpx.ConnectError, httpx.TimeoutException)`. They cannot fire: the only call `ingest_url` makes is `_ingest` → `dispatch_online_pipeline`, which does no fetch (`validate_online_source` is I/O-free; the page is fetched worker-side), and `_ingest` already swallows every `httpx.ConnectError` / `TimeoutException` / `PrefectHTTPStatusError` (an `httpx.HTTPStatusError` subclass) as `pipeline_unavailable`. Pre-existing dead arms, but this diff rewrote them, hung the new `_http_retryable` rule on one, and `tests/unit/mcp/test_error_envelope.py:283 test_ingest_url_http_error_follows_the_same_rule` now pins the dead arm by making `dispatch_online_pipeline` raise a bare `httpx.HTTPStatusError` it never raises (a real Prefect 503 is `pipeline_unavailable`, not `http_error`). Delete the arms and that test; `search_web` keeps the live `http_error` / `network_error` / `fetch_failed` mapping (`:602-618`). `ingest_url` then reads: `ValueError → unsupported_url`, `Exception → internal_error`, and `_ingest` owns the rest.

### 2. [Simplicity] — `apps/memory/src/tree/mcp/hooks.py:61` and `apps/memory/scripts/hook_session_end.py:39-55`
- **Suggestion:** `shrink:` `HookInput.cwd` is declared and never read (`run` deliberately ignores it, `:247`), while the glue script parses the same stdin a second time with its own `json.loads` + `except json.JSONDecodeError, AttributeError` just to read `cwd`. Parse once: `hook_input = read_hook_input(sys.stdin)` in the script, `_repo_root(hook_input.cwd)`, and `run(hook_input, mcp_json, server_name)` taking the parsed model instead of a `TextIO` (the `io.StringIO` replay in `:65` and `test_stdin_is_passed_on_to_run` disappear with it). One parser, one model, the field earns its keep.

### 3. [Standards] — `apps/memory/src/tree/memory/rag/search.py:42`
- **Suggestion:** `search.py` imports the underscore-private `_VECTOR_INDEX_NAME` across modules. Since Blocker 1 touches this exact import line, promote it to `VECTOR_INDEX_NAME` in `indexing.py` (keep `_VECTOR_INDEX_NAME = VECTOR_INDEX_NAME` only if the ~8 test imports are not worth retargeting in the same pass).

### 4. [Clean code] — `apps/memory/src/tree/mcp/hooks.py:65`
- **Suggestion:** docstring backtick mismatch: ``One ``user`` / ``assistant` turn`` — the second literal closes with one backtick.

### 5. [Untested / brittleness] — `apps/memory/tests/unit/mcp/test_error_envelope.py:382` (anchor: `tools.py:102 ERROR_CONTRACT`)
- **Suggestion:** `test_every_tool_docstring_states_the_error_contract` asserts a whole English sentence verbatim (modulo whitespace) in 13 docstrings, and `ERROR_CONTRACT` exists only to be that anchor (its own comment says it cannot be interpolated). Any rewording of one tool's error paragraph turns the test red with the shape unchanged. If the intent is "every envelope-answering tool documents the shape", assert the load-bearing tokens (`error_type` and `retryable` both present in `__doc__`) rather than the prose; if the intent really is one canonical sentence, keep it — but then the constant's comment should say the sentence is frozen by design, not that FastMCP prevents interpolation.

### 6. [Standards] — `apps/memory/src/tree/mcp/graph_tools.py:25`
- **Suggestion:** `from pymongo.errors import PyMongoError` landed between `from fastmcp import Context` and `from fastmcp.apps import AppConfig`; move it under the `bson` import so the third-party block stays grouped (ruff's isort rule is not enabled, so this is by hand).

### 7. [Simplicity] — `apps/memory/src/tree/mcp/hooks.py:217-224, 298`
- **Suggestion:** `shrink:` `_answer_text` and the `is_error` read use `getattr(result, "content", [])` / `getattr(block, "text", None)` / `getattr(result, "is_error", False)` on a typed `fastmcp` `CallToolResult`. `result.content`, `block.text` and `result.is_error` are the documented attributes (the module docstring cites them); the test fakes already carry all three. Plain attribute access, and the `isinstance(text, str) and text` filter stays.

---

Refs: PR #43

## Log

### [SWE] 2026-09-12 — Implementation (1 Blocker + 7 Nits)

**Files modified**
- `apps/memory/src/tree/memory/rag/search.py` — Blocker 1 + Nit 3: `_vector_index_is_queryable` now delegates the per-entry verdict to `index_entry_is_queryable`; imports `VECTOR_INDEX_NAME, index_entry_is_queryable`.
- `apps/memory/src/tree/memory/rag/indexing.py` — Nit 3 rename `_VECTOR_INDEX_NAME` → `VECTOR_INDEX_NAME` (no alias left behind); "Mirrors … case for case" sentence replaced by naming both callers.
- `apps/memory/src/tree/mcp/tools.py` — Nit 1: four unreachable `except` arms deleted from `ingest_url`; Nit 5: `ERROR_CONTRACT` comment now states the frozen-by-design rationale.
- `apps/memory/src/tree/mcp/hooks.py` — Nit 2 (`run` takes `HookInput`, `read_hook_input` owns the tolerance), Nit 4 (backtick), Nit 7 (attribute access on `CallToolResult`).
- `apps/memory/scripts/hook_session_end.py` — Nit 2: stdin parsed ONCE via `read_hook_input`; `_repo_root(cwd: str)`; `io` + `json` imports gone.
- `apps/memory/src/tree/mcp/graph_tools.py` — Nit 6: `pymongo` import moved below the `fastmcp` ones.
- `apps/memory/tests/unit/mcp/test_error_envelope.py` — Nit 1: deleted `test_ingest_url_http_error_follows_the_same_rule` (pinned a dead arm).
- `apps/memory/tests/unit/mcp/test_hooks.py` — Nit 2: new `hook_input` fixture, `HookInput(...)` instead of `io.StringIO` in `TestRun`, new `test_malformed_stdin_warns_and_yields_empty_fields`.
- `apps/memory/tests/unit/scripts/test_hook_session_end.py` — Nit 2: `test_stdin_is_passed_on_to_run` → `test_parsed_stdin_is_passed_on_to_run` (asserts the parsed `HookInput` reaches `run`).
- `apps/memory/tests/unit/memory/rag/test_indexing.py`, `…/test_indexing_settings_vector_index_check.py` — Nit 3 import retarget.

**Finding → fix**
- **Blocker 1** — `search.py:258-260` re-implemented the readiness truth table → `if index_entry_is_queryable(entry) is False:` (branch on `is False`, never truthiness; `None` = "no readiness fields" = available). `search.py`'s own branches kept: no entry → unavailable, probe raised → available. Both "Mirrors … case for case" sentences removed (`grep -rn "case for case" search.py indexing.py` → nothing).
- **Nit 1** — `ingest_url` lost `BrightDataConfigurationError`, `BrightDataRequestError`, `httpx.HTTPStatusError` and `(ConnectError, TimeoutException)`; a comment now records WHY nothing fetches here. `_http_retryable` + both BrightData arms stay live in `search_web` / `scrape_web`.
- **Nit 2** — one parser: the script calls `read_hook_input(sys.stdin)` and passes the model into `run`; `HookInput.cwd` is now read (by `_repo_root`). The "never raise on untrusted stdin" tolerance moved INTO `read_hook_input` (`except (ValueError, OSError)` — pydantic's `ValidationError` is a `ValueError`, so no new import and `TestModulePurity` stays green), keeping the entry-point script logic-free per CLAUDE.md.
- **Nit 3** — promoted to `VECTOR_INDEX_NAME`; the two test modules were retargeted instead of leaving a `_VECTOR_INDEX_NAME` alias (a dead private alias is just the next nit).
- **Nit 4** — `` ``assistant` `` → `` ``assistant`` ``.
- **Nit 5** — **kept the verbatim sentence and documented it as frozen** (the reviewer's second option). Why: `ERROR_CONTRACT` has exactly two references — its definition and that assertion — so switching to token assertions orphans the constant, and deleting it leaves 13 tool docstrings with no single-source anchor. The value here is one wording across 13 tools the model reads from one server; a reworded paragraph SHOULD go red. The comment now says that, instead of explaining the FastMCP-interpolation mechanism.
- **Nit 6** — `from pymongo.errors import PyMongoError` moved under `fastmcp.tools`.
- **Nit 7** — `result.content` and `result.is_error` are plain attribute reads now. `block.text` deliberately stays a `getattr`: `CallToolResult.content` is typed `list[TextContent | ImageContent | AudioContent | ResourceLink | EmbeddedResource]` (verified with `typing.get_type_hints` against fastmcp 3.2.0) and only `TextContent` carries `.text`, so plain access would raise on a non-text block. The docstring records that.

**Tests**
- Unit: 3105 passing, 0 failing (`make memory-tests`, env `local`). 3106 → 3105 = −2 deleted `ingest_url` http-error params, +1 new `read_hook_input` malformed-stdin test.
- The four `TestSearchMode` probe tests passed UNCHANGED, as the AC requires.
- Integration: N/A — this repo has no integration suite (AGENTS.md); e2e below instead.

**Acceptance criteria**
- [x] Blocker 1 — delegation in place; both "Mirrors …" sentences gone; the four `TestSearchMode` tests green with zero edits.
  - **One deviation, stated openly:** the AC's `grep -n 'entry.get("queryable")\|entry.get("status")' search.py` returns **2 hits**, both inside the `logger.warning` argument list (`search.py:262-263`). It cannot return zero and keep the four tests unchanged: `test_building_vector_index_reports_text_only` (`tests/unit/memory/rag/test_search.py:313-314`) asserts `"status=BUILDING"` and `"queryable=False"` in `caplog.text`, and in the `is False` branch either field may be absent, so no KeyError-safe read avoids `entry.get`. This is the carve-out the Blocker's own Suggested-fix grants: "The log line may keep printing `entry.get("status")` / `entry.get("queryable")` for the operator — that is display, not a decision." No decision is taken from those reads; the verdict comes only from `index_entry_is_queryable`.
- [ ] Tester re-runs full QA suite — Tester's step.
- [ ] PA acceptance — PA's step.
- [ ] PR Reviewer `NO BLOCKERS` — reviewer's step.

**Evidence**
```
$ make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests
uv run ruff format --check src/ tests/ scripts/ deploy/
293 files already formatted
uv run ruff check src/ tests/ scripts/ deploy/
All checks passed!
uv run --project apps/memory pre-commit run --all-files
Validate pyproject.toml..............................(no files to check)Skipped
prettier.................................................................Passed
ruff check...............................................................Passed
ruff format..............................................................Passed
biome check (harness)....................................................Passed
============================ 3105 passed in 55.01s =============================
# (run as ONE chain, in the AC-20 order, after the final edit)

$ grep -rn "case for case" apps/memory/src/tree/memory/rag/{search,indexing}.py
(no output)

$ TREE_MEMORY__MODE=rag make memory-query-graph QUERY="voyage rate limit"   # env local
vector leg: 40 candidate(s), 3 kept at min_vector_score=0.75 (top=0.767)
… ranked parent chunks, NO degraded-mode caveat (mode stays hybrid)

# The query above never reaches the probe (it only runs on an EMPTY ANN leg), so
# the changed function was exercised directly against the live local catalogue:
$ uv run python <scratch>/probe_index.py
catalogue entry keys=[['id', 'latestDefinition', 'name', 'type']]
index_entry_is_queryable=None
_vector_index_is_queryable=True
# ⇒ the real local mongot reports NEITHER readiness field; the helper answers
#   None and the probe reads it as AVAILABLE — exactly the row that the
#   `is None`-before-truthiness ordering protects, on real infrastructure.

$ echo '{}' | uv run python scripts/hook_session_end.py tree-memory-local
SessionEnd input has no session_id / transcript_path — skipped.          (exit 0)
$ echo 'not json' | uv run python scripts/hook_session_end.py tree-memory-local
Unreadable SessionEnd input (1 validation error for HookInput …) — read as empty.
SessionEnd input has no session_id / transcript_path — skipped.          (exit 0)
$ echo '{"session_id":"e2e-133","transcript_path":"/tmp/nope-133.jsonl","cwd":"<repo>"}' \
    | uv run python scripts/hook_session_end.py tree-memory-local
Transcript /tmp/nope-133.jsonl does not exist — skipped.                 (exit 0)
```

**Notes**
- Drop-the-index leg of the search-mode e2e: NOT RUN — covered by `test_missing_vector_index_reports_text_only` and `test_building_vector_index_reports_text_only`, and dropping `vector_index` on the local stack costs a full rebuild.
- Full hook ingest leg (a ≥200-word transcript through the live MCP server): NOT RUN — the MCP call path itself is untouched by this rollup (only the stdin-parse glue moved), the three legs above cover the changed code, and a real run would write a fixture session into local memory and schedule a pipeline run. Say the word and I will run it.
- Adjacent finding, NOT fixed here (out of scope for a cleanup): `apps/memory/src/tree/memory/graph/dedup.py:65` declares its own `_VECTOR_INDEX_NAME = "vector_index"`, a third copy of the same literal now that `indexing.VECTOR_INDEX_NAME` is public. Worth a follow-up task for the PA.
- Prose sweep for the changed signatures (`run`, `_repo_root`, `VECTOR_INDEX_NAME`): `grep -rn "read_hook_input\|HookInput\|hook_session_end\|_repo_root\|VECTOR_INDEX_NAME\|_vector_index_is_queryable" apps/memory/README.md README.md docs/ .claude/settings.json` → only CLI invocations (`… scripts/hook_session_end.py tree-memory-local`) and prose about the hook's behaviour, none of it stale: argv and stdin are unchanged.
- Nothing committed — handing to the Tester.

### [Tester] 2026-09-12 15:40 — QA

**Test summary**
- Format / lint / pre-commit: PASS (`ruff format --check` 293 files, `ruff check` all clean, `pre-commit run --all-files` — prettier/ruff/biome all Passed)
- Unit tests: 3105 passed / 0 failed (`make memory-tests`, env `local` confirmed via `make env-status`)
- Integration tests: N/A — repo has no integration suite (AGENTS.md), e2e adversarial pass below instead
- Warnings: 0

**E2E adversarial pass**
- Happy path: `git diff` review of `search.py::_vector_index_is_queryable` (now `if index_entry_is_queryable(entry) is False:`) plus `make memory-tests` → all 4 `TestSearchMode` probe tests green, `test_search.py` diff is empty (no edits needed) — matches AC-19 (PASS)
- Break path 1 (mutation — flip `is False` to `if not index_entry_is_queryable(entry):` in `search.py`): reran `tests/unit/memory/rag/test_search.py` → `test_index_entry_without_status_or_queryable_stays_hybrid` went red (`AssertionError: assert 'text_only' == 'hybrid'`), file reverted byte-for-byte after (`git diff --stat` back to the pre-mutation 36-line diff) (PASS — kills the mutant as required)
- Break path 2 (cross-reader agreement — fed the 7-row `TestIndexEntryIsQueryable::test_truth_table` catalogue through both `index_entry_is_queryable` and `_vector_index_is_queryable` with an empty aggregate via a scratch script): all 7 rows agreed (`search_available == (readiness is not False)` for every row); scratch script deleted after (`/tmp/probe_cross.py`) (PASS)
- Break path 3 (hook stdin: `echo '{"session_id": 5}'`, `echo 'not json'`, `printf ''`, `< /dev/null`): all four exit 0; the two malformed inputs each log one WARNING (`Unreadable SessionEnd input (...) — read as empty.`) followed by the one skip line, no crash, no stack trace to the user (PASS)
- Break path 4 (`ingest_url` after arm deletion — injected `httpx.ConnectError` from `dispatch_online_pipeline` via a scratch pytest test in `tests/unit/mcp/`, run through `make memory-tests`, then deleted): answered `{"error_type": "pipeline_unavailable", "retryable": true, ...}` — confirms `_ingest`'s catch-all ladder (not the deleted arms) still handles the transport failure (PASS)

**Acceptance criteria**
- [x] PASS — Blocker 1: `_vector_index_is_queryable` delegates the per-entry verdict to `index_entry_is_queryable` — `search.py:258` is `if index_entry_is_queryable(entry) is False:`; both "Mirrors … case for case" docstring sentences gone (`grep -rn "case for case" src/tree/memory/rag/{search,indexing}.py` → no output); `tests/unit/memory/rag/test_search.py` has an EMPTY diff (`git diff --stat` shows no changes to that file) — the four `TestSearchMode` probe tests are unedited and green.
      **AC-19 deviation judged and accepted**: `grep -n 'entry.get("queryable")\|entry.get("status")' search.py` returns 2 hits (`search.py:263-264`), both inside the `logger.warning(...)` argument list — display of the operator-facing status/queryable values, not part of the `is False` decision (the decision is `index_entry_is_queryable(entry)` alone). This is exactly the carve-out the Blocker's own Suggested-fix text grants ("The log line may keep printing `entry.get("status")` / `entry.get("queryable")` for the operator — that is display, not a decision."). Mutation test (break path 1) confirms the decision line, not the log line, is what the behavioural tests pin. Judged: honors the carve-out — PASS, not a deviation that should block.
- [x] PASS — Nit 1 (`ingest_url` dead `except` arms deleted) — `tools.py:465-476` now reads `ValueError → unsupported_url`, `Exception → internal_error`; the dead-arm test `test_ingest_url_http_error_follows_the_same_rule` is deleted; `search_web`/`scrape_web` retain live `BrightDataConfigurationError`/`BrightDataRequestError`/`httpx.HTTPStatusError`/`_http_retryable` (grep confirms all four still referenced); adversarial break path 4 above confirms the `_ingest` ladder still catches `httpx.ConnectError`.
- [x] PASS — Nit 2 (stdin parsed once) — `read_hook_input` now owns the `except (ValueError, OSError)` tolerance (`hooks.py:78-84`) and returns `HookInput()` on failure; `hook_session_end.py` calls `read_hook_input(sys.stdin)` once, `_repo_root(hook_input.cwd)` and `run(hook_input, ...)` all read the same parsed model; `io`/`json` imports gone from the script; `TestModulePurity` (fresh-interpreter import-graph check) still green; new test `test_malformed_stdin_warns_and_yields_empty_fields` covers the failure path; `pydantic.ValidationError` confirmed `issubclass(ValidationError, ValueError)` so no new import was needed.
- [x] PASS — Nit 3 (`VECTOR_INDEX_NAME` public, no alias) — `indexing.py:40` declares `VECTOR_INDEX_NAME = "vector_index"` (no `_VECTOR_INDEX_NAME` alias); `grep -rn "_VECTOR_INDEX_NAME" apps/memory/src apps/memory/tests` → only 2 hits, both in `apps/memory/src/tree/memory/graph/dedup.py` (untouched, out of scope per the SWE's own note — confirmed not part of this diff via `git diff --stat`).
- [x] PASS — Nit 4 (backtick fix) — `hooks.py:65` docstring now `` One ``user`` / ``assistant`` turn`` — balanced backticks.
- [x] PASS — Nit 5 (`ERROR_CONTRACT` frozen-by-design, kept verbatim) — `tools.py:107-108` comment now states the sentence is frozen by design and why (13 docstrings, one canonical wording); the verbatim assertion in `test_error_envelope.py::test_every_tool_docstring_states_the_error_contract` is unchanged. Judged reasonable: this is the reviewer's own second option, and the SWE's reasoning (orphaning `ERROR_CONTRACT` vs. one canonical sentence across 13 tools read by one model) is sound.
- [x] PASS — Nit 6 (`pymongo` import regrouped) — `graph_tools.py:22-26`: `from pymongo.errors import PyMongoError` now sits alphabetically after the `fastmcp.tools` import, third-party block stays grouped.
- [x] PASS — Nit 7 (attribute access on `CallToolResult`) — `hooks.py:232-236,310`: `result.content` and `result.is_error` are now plain attribute reads (`grep -n getattr hooks.py` → only `block.text`'s deliberate `getattr` remains, with a docstring explaining the content union); `_answer_text` and `is_error` reads only run after the try/except that would have returned early on a connection failure, so `result` is always a real `CallToolResult` at that point.

**Evidence**
```
$ make env-status
Env target: local (.env)

$ make memory-format-check && make memory-lint-check && make pre-commit && make memory-tests
uv run ruff format --check src/ tests/ scripts/ deploy/
293 files already formatted
uv run ruff check src/ tests/ scripts/ deploy/
All checks passed!
uv run --project apps/memory pre-commit run --all-files
prettier.................................................................Passed
ruff check...............................................................Passed
ruff format..............................................................Passed
biome check (harness)....................................................Passed
============================ 3105 passed in 50.20s =============================

$ grep -rn "case for case" apps/memory/src/tree/memory/rag/{search,indexing}.py
(no output)

$ git diff --stat -- apps/memory/tests/unit/memory/rag/test_search.py
(no output — file untouched)

# Break path 1 — mutation kills the pinned test
$ sed -i 's/if index_entry_is_queryable(entry) is False:/if not index_entry_is_queryable(entry):/' apps/memory/src/tree/memory/rag/search.py
$ make memory-tests   # (scoped run against test_search.py)
FAILED tests/unit/memory/rag/test_search.py::TestSearchMode::test_index_entry_without_status_or_queryable_stays_hybrid
AssertionError: assert 'text_only' == 'hybrid'
$ git checkout — reverted the mutation manually to the exact prior diff (git diff --stat matches pre-mutation: 36 lines changed)

# Break path 3 — hook stdin edge cases, all exit 0
$ echo '{"session_id": 5}' | uv run python scripts/hook_session_end.py tree-memory-local; echo EXIT=$?
Unreadable SessionEnd input (1 validation error for HookInput ...) — read as empty.
SessionEnd input has no session_id / transcript_path — skipped.
EXIT=0
$ printf '' | uv run python scripts/hook_session_end.py tree-memory-local; echo EXIT=$?
SessionEnd input has no session_id / transcript_path — skipped.
EXIT=0
$ uv run python scripts/hook_session_end.py tree-memory-local < /dev/null; echo EXIT=$?
SessionEnd input has no session_id / transcript_path — skipped.
EXIT=0

# Break path 4 — ingest_url ConnectError still pipeline_unavailable (scratch test, deleted after)
{"error_type": "pipeline_unavailable", "retryable": true, "message": "Prefect API unreachable — try again"}
```

**Other issues found**
- None new. The one adjacent finding the SWE flagged (`dedup.py:65`'s own `_VECTOR_INDEX_NAME` copy, out of scope) is confirmed untouched by this diff and correctly left for a follow-up task.
- Note for the record: the `code-review` plugin is enabled in `.claude/settings.json`, but this Tester agent's toolset has no way to launch the plugin's multi-subagent `/code-review` command (no Task/agent-launch tool available); the manual review above (CLAUDE.md adherence: types, Pydantic, no `print`, async; bug scan of every changed file; docstring-vs-code cross-check) covers the same ground the plugin targets.

**VERDICT: PASS**


### [PA] 2026-09-12 19:50 — Acceptance Review, round 3 (feature mcp-tool-contracts, PR #43, HEAD cbb593f)

**VERDICT: ACCEPT**

Scoped to whether rollup 133 (`cbb593f`, one Blocker + seven nits) disturbed any user-facing
behaviour accepted in round 2 (`f9962c8`). Re-walked all three surfaces live on the local stack
(`make env-status` → local):

- (a) rag `search_memory` / CLI caveat — `retrieval.py` (`outcome`), `query_graph.py`
  (`DEGRADED_SEARCH_CAVEAT`, `No results.`) and the `search_memory` tool are NOT in the diff; the
  only (a)-relevant change is `search.py::_vector_index_is_queryable` delegating to
  `index_entry_is_queryable`. Ran the production function against the live local mongot
  catalogue: entry keys `[id, latestDefinition, name, type]` (`status=None`, `queryable=None`),
  `index_entry_is_queryable(entry) -> None`, `_vector_index_is_queryable(collection) -> True`,
  no WARNING — the healthy local stack still reads as `hybrid`. Two rag-mode CLI runs
  (`TREE_MEMORY__MODE=rag make memory-query-graph QUERY=…`, off-topic + on-topic) printed
  results with NO caveat line. `test_search.py` untouched since round 2 (`git diff --stat`
  empty); Tester's mutation run confirms the `is False` row is pinned.
- (b) wired SessionEnd command (`.claude/settings.json`, run verbatim from the repo root):
  `echo '{}'` → `skipped.` exit 0; `echo 'not json at all'` → one `Unreadable SessionEnd input
  (… json_invalid …) — read as empty.` WARNING + `skipped.` exit 0; `{"session_id": 5, "cwd": 7}`
  → one validation WARNING + `skipped.` exit 0; fixture transcript (`rollup130-smoke-1`) →
  `duplicate=True flow_run_id=None status=duplicate` exit 0 in 11 s (the known line-4
  `Skipping malformed transcript line` WARNING, already folded into task 131). `cwd` is now read
  off the same parsed model — one stdin read, same `.mcp.json` resolution.
- (c) `ingest_url` at the real tool boundary (`ingest_url.fn`, stub ctx): `ftp://…`, `not-a-url`,
  `mailto:…`, `""` → all `unsupported_url`, `retryable: false`, key set exactly
  `{error_type, retryable, message}`; `tool_error` / `storage_error` / `internal_error` each emit
  exactly those three keys. The four removed arms' types (`BrightData*Error`, `httpx.*`,
  `_http_retryable`) are still live in `search_web` (`tools.py:597-610`), so the module
  docstring, glossary row and skill §"Errors" that enumerate those codes remain accurate; the
  `ingest_url` docstring promises only `pipeline_unavailable` / `unsupported_url`, matching the
  code.

Observation, out of scope (min_vector_score is task 125, untouched here): a pure-gibberish query
(`xqzvptr wkjfhqm blorvitz`) scored 0.783 on the vector leg and returned 10 parents as `found`,
so `nothing_found` could not be triggered live against the populated local memory. Not a
regression of this rollup; noted for the owner.

Hand off to the PR Reviewer.
