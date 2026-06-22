# Module-ize `conversation` and `file` (mirror the `web/web.py` convention)

Status: pending
Tags: `data`, `refactor`
Depends on: #076
Blocks: —

## Scope

`conversation.py` + `conversation_pipeline.py` and `file.py` + `file_pipeline.py` sit
loose at the `data/` root, unlike `substack/`, `youtube/`, `web/`, and `huggingface/`
which are packages. Make `conversation` and `file` their own packages, following the
established `<module>/<module>.py` + `<module>/<module>_pipeline.py` convention (see
`web/web.py` + `web/web_pipeline.py`). Pure code-org: NO behavior change, NO logic
change — only file locations and import paths move.

**No deployment-topology impact.** `conversation_pipeline` / `file_pipeline` are NOT
served as Prefect deployments — they're commented out in
`apps/memory/src/tree/orchestrator.py` under the 5-deployment Prefect free-tier cap
(`_DEPLOYMENT_SPECS` does not include them). So the Tester should expect ZERO
serve/registration change and NO change to `_DEPLOYMENT_SPECS`; only import paths move.

### 1. Move the files into packages (`git mv`)

Conversation:
- `apps/memory/src/tree/data/conversation.py` →
  `apps/memory/src/tree/data/conversation/conversation.py`
- `apps/memory/src/tree/data/conversation_pipeline.py` →
  `apps/memory/src/tree/data/conversation/conversation_pipeline.py`
- add `apps/memory/src/tree/data/conversation/__init__.py` (empty, like
  `data/web/__init__.py`).

File:
- `apps/memory/src/tree/data/file.py` →
  `apps/memory/src/tree/data/file/file.py`
- `apps/memory/src/tree/data/file_pipeline.py` →
  `apps/memory/src/tree/data/file/file_pipeline.py`
- add `apps/memory/src/tree/data/file/__init__.py` (empty).

New import paths become `tree.data.conversation.conversation`,
`tree.data.conversation.conversation_pipeline`, `tree.data.file.file`,
`tree.data.file.file_pipeline` — mirroring `tree.data.web.web` /
`tree.data.web.web_pipeline`.

### 2. Repoint INTERNAL (intra-package) imports

- `conversation_pipeline.py`:
  `from tree.data.conversation import load_conversation_document`
  → `from tree.data.conversation.conversation import load_conversation_document`.
- `file_pipeline.py`:
  `from tree.data.file import load_file_document`
  → `from tree.data.file.file import load_file_document`.

### 3. Repoint external importers

- `apps/memory/src/tree/mcp/tools.py:13`
  `from tree.data.conversation_pipeline import ingest_conversation as
  _ingest_conversation`
  → `from tree.data.conversation.conversation_pipeline import ...`.
- `apps/memory/src/tree/mcp/tools.py:15`
  `from tree.data.file_pipeline import ingest_file as _ingest_file`
  → `from tree.data.file.file_pipeline import ...`.
- `apps/memory/src/tree/orchestrator.py:46-47` — the COMMENTED-OUT imports:
  `# from tree.data.conversation_pipeline import ingest_conversation`
  `# from tree.data.file_pipeline import ingest_file`
  → update both to the new paths
  (`# from tree.data.conversation.conversation_pipeline import ingest_conversation`,
  `# from tree.data.file.file_pipeline import ingest_file`) so a future re-enable
  doesn't resurrect a dead path.
- `apps/memory/src/tree/data/web/web.py:5` — the docstring reference "pattern from
  ``tree.data.file.load_file_document``." → "pattern from
  ``tree.data.file.file.load_file_document``." (comment-only path fix).

### 4. Repoint test patch paths + imports

- `apps/memory/tests/unit/data/test_conversation.py` — imports `from
  tree.data.conversation import ...` and `mocker.patch`-es
  `tree.data.conversation.Document.find_one` / `tree.data.conversation.Document.insert`
  in MANY places → ALL become `tree.data.conversation.conversation.*` (import +
  every patch-target string).
- `apps/memory/tests/unit/data/test_file.py` — `from tree.data.file import ...` and
  `tree.data.file.Document.find_one` / `tree.data.file.Document.insert` patch targets
  → ALL become `tree.data.file.file.*`.

### CAUTION — do NOT rewrite non-import references

These are NOT module-import references and must be left untouched:
- `tree.data.file.file` once rewritten is correct — but watch the YouTube test
  `apps/memory/tests/unit/data/youtube/test_gemini_transcript_fetcher.py`, which uses
  `file_data.file_uri` / `Part.from_uri(file_uri=...)`. Those are a Gemini SDK
  attribute/kwarg named `file_uri`, NOT a `tree.data.file` import — do NOT touch them.
- A blind find-replace of `tree.data.file` is UNSAFE: it would corrupt
  `tree.data.file.file` (the new module) and is irrelevant to `file_uri`. Only rewrite
  the specific patch-target / import strings listed in step 4.

### Files touched

- `apps/memory/src/tree/data/conversation.py` →
  `…/data/conversation/conversation.py` (git mv) + new `conversation/__init__.py`.
- `apps/memory/src/tree/data/conversation_pipeline.py` →
  `…/data/conversation/conversation_pipeline.py` (git mv) + its internal import.
- `apps/memory/src/tree/data/file.py` → `…/data/file/file.py` (git mv) + new
  `file/__init__.py`.
- `apps/memory/src/tree/data/file_pipeline.py` → `…/data/file/file_pipeline.py`
  (git mv) + its internal import.
- `apps/memory/src/tree/mcp/tools.py` — repoint lines 13 + 15.
- `apps/memory/src/tree/orchestrator.py` — repoint the two commented-out imports
  (lines 46-47).
- `apps/memory/src/tree/data/web/web.py` — fix the line-5 docstring path.
- `apps/memory/tests/unit/data/test_conversation.py` — import + all patch targets.
- `apps/memory/tests/unit/data/test_file.py` — import + all patch targets.

## Acceptance Criteria

- [ ] `conversation` is a package: `…/data/conversation/conversation.py`,
      `…/data/conversation/conversation_pipeline.py`, and
      `…/data/conversation/__init__.py` exist; the loose `data/conversation.py` and
      `data/conversation_pipeline.py` are gone (history preserved via `git mv`).
- [ ] `file` is a package: `…/data/file/file.py`, `…/data/file/file_pipeline.py`,
      `…/data/file/__init__.py` exist; the loose `data/file.py` and
      `data/file_pipeline.py` are gone.
- [ ] `conversation_pipeline.py` imports `load_conversation_document` from
      `tree.data.conversation.conversation`; `file_pipeline.py` imports
      `load_file_document` from `tree.data.file.file`.
- [ ] `mcp/tools.py` imports `ingest_conversation` from
      `tree.data.conversation.conversation_pipeline` and `ingest_file` from
      `tree.data.file.file_pipeline`.
- [ ] `orchestrator.py`'s two commented-out imports reference the new paths.
- [ ] `web/web.py` line-5 docstring reads `tree.data.file.file.load_file_document`.
- [ ] `test_conversation.py` patches `tree.data.conversation.conversation.*` and
      passes; `test_file.py` patches `tree.data.file.file.*` and passes.
- [ ] `grep -rn "tree\.data\.conversation\b\|tree\.data\.conversation_pipeline\b"
      apps/memory` returns ONLY new-path matches
      (`tree.data.conversation.conversation` / `…conversation_pipeline`) — no bare
      `tree.data.conversation` / `tree.data.conversation_pipeline` import survives.
- [ ] `grep -rn "tree\.data\.file_pipeline\b" apps/memory` returns nothing; every
      `tree.data.file` reference is either the new `tree.data.file.file` /
      `tree.data.file.file_pipeline` module path OR the untouched
      `file_data.file_uri` / `Part.from_uri(file_uri=...)` SDK usage in the YouTube
      transcript test (which is NOT an import).
- [ ] `_DEPLOYMENT_SPECS` in `orchestrator.py` is UNCHANGED (no serve/registration
      delta — these pipelines remain un-deployed under the free-tier cap).
- [ ] `make memory-format-fix && make memory-lint-fix && make memory-format-check &&
      make memory-lint-check` all clean.
- [ ] `make pre-commit` passes.
- [ ] `make memory-unit-tests` passes, 0 warnings.
- [ ] `make memory-integration-tests` (fast tail) passes (the MCP imports moved).
- [ ] [HUMAN] Live smoke (mirrors how #074 deferred its `[HUMAN]` ACs — the automated
      gate above is the real bar): with the stack up and `make memory-serve-workflows`
      re-served, run `make memory-run-data-pipeline USER_ID=<oid>` and confirm web /
      custom URLs ingest via the web batch; and sanity-check the MCP `ingest_url` /
      `ingest_file` / `ingest_conversation` tools still import and run after the move.

## BDD scenarios

### Scenario: conversation/file are packages mirroring the web convention
- **Given** the completed move
- **When** I list `apps/memory/src/tree/data/conversation/` and
  `apps/memory/src/tree/data/file/`
- **Then** each contains `<module>.py` + `<module>_pipeline.py` + `__init__.py`, the
  same shape as `data/web/` — and the loose root files are gone.

### Scenario: every importer uses the new path, no bare module survives
- **Given** the repointed importers
- **When** I grep for the bare `tree.data.conversation` / `tree.data.conversation_pipeline`
  / `tree.data.file_pipeline` import paths
- **Then** none survive as imports; `mcp/tools.py`, the pipelines' internal imports,
  and the unit tests all use the package paths.

### Scenario: the move changes no deployment topology
- **Given** the move is complete
- **When** the orchestrator registers deployments
- **Then** `_DEPLOYMENT_SPECS` is identical to before — conversation/file remain
  un-deployed (free-tier cap), only their import paths changed.

### Scenario: the YouTube `file_uri` SDK usage is untouched
- **Given** the move's path rewrites
- **When** I inspect `test_gemini_transcript_fetcher.py`
- **Then** `file_data.file_uri` / `Part.from_uri(file_uri=...)` are unchanged (they
  are a Gemini SDK attribute/kwarg, not a `tree.data.file` import).

## User Stories

### Story: A maintainer sees one packaging convention across all sources
1. A maintainer browses `apps/memory/src/tree/data/`.
2. Every source — substack, youtube, web, huggingface, conversation, file — is a
   package with `<module>.py` + `<module>_pipeline.py`.
3. No loose `conversation.py` / `file.py` / `*_pipeline.py` files clutter the `data/`
   root.

### Story: A future engineer re-enables the conversation deployment cleanly
1. The Prefect plan is upgraded past the free-tier cap.
2. An engineer uncomments the `orchestrator.py` imports.
3. They resolve to the live `tree.data.conversation.conversation_pipeline` /
   `tree.data.file.file_pipeline` modules first try — the commented imports were
   already updated, so no dead path resurfaces.

## Test guidance

- Pure move: NO new behavior, NO new tests. `test_conversation.py` and `test_file.py`
  must pass after the import + patch-path rewrite ONLY.
- The two `grep` ACs are the decisive checks for stale references. Pay special
  attention to the `file` rename — `tree.data.file.file` (correct new module) and
  `file_data.file_uri` (untouched SDK usage) must be distinguished by hand; do NOT run
  a blind `tree.data.file` → `tree.data.file.file` find-replace.
- Run `make memory-unit-tests` then `make memory-integration-tests` on the LOCAL env
  with the stack up. Never a bare `uv run pytest`. Per CLAUDE.md, call the
  `/testing-python` skill if you touch the test files beyond the mechanical rewrite.
- The `[HUMAN]` live smoke is deferred like #074's `[HUMAN]` ACs — record evidence in
  the log when the owner runs it; the automated gate is what gates the PR.

---

Blocked by: #076

## Log

### [PA] 2026-06-22 — Grooming

**Summary**
Promote `conversation` and `file` to packages (`<module>/<module>.py` +
`<module>/<module>_pipeline.py` + `__init__.py`, mirroring `web/`), repointing the
internal pipeline imports, `mcp/tools.py`, the two commented-out `orchestrator.py`
imports, a `web/web.py` docstring path, and the two unit-test suites' patch paths.
Pure code-org with ZERO deployment-topology impact.

**Key decisions**
- Convention pinned to the existing `web/web.py` + `web/web_pipeline.py` shape, so the
  modules become `tree.data.conversation.conversation` etc.
- Explicitly flagged the `file` rename trap: `tree.data.file.file` (new module) vs the
  YouTube test's `file_data.file_uri` / `Part.from_uri(file_uri=...)` Gemini-SDK usage
  — a blind find-replace would corrupt both. Two precise grep ACs guard against stale
  references AND against over-rewriting.
- Stated NO `_DEPLOYMENT_SPECS` change (these pipelines are commented out under the
  free-tier 5-deployment cap), so the Tester doesn't expect a serve/registration
  delta. Still updated the commented-out imports so a future re-enable is clean.
- Added a deferred `[HUMAN]` live smoke (MCP `ingest_url`/`ingest_file`/
  `ingest_conversation` tools + web-batch ingest) mirroring how #074 deferred its
  `[HUMAN]` ACs; the automated gate (`make memory-integration-tests` fast tail) is the
  real bar.

**Dependencies**
- #076 — final task in the feature; ordered after the `core/` flatten so the data
  module is fully tidied in one sweep. Independent of #076's content (no shared files
  beyond `mcp/tools.py`, which both edit on distinct lines).

**User stories**
- 2 stories: maintainer sees one packaging convention across all sources; a future
  engineer re-enables the conversation deployment with no dead import path.

**Open questions**
- None.

Ready for implementation.
