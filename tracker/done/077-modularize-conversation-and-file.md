# Module-ize `conversation` and `file` (mirror the `web/web.py` convention)

Status: in-progress
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

- [x] `conversation` is a package: `…/data/conversation/conversation.py`,
      `…/data/conversation/conversation_pipeline.py`, and
      `…/data/conversation/__init__.py` exist; the loose `data/conversation.py` and
      `data/conversation_pipeline.py` are gone (history preserved via `git mv`).
- [x] `file` is a package: `…/data/file/file.py`, `…/data/file/file_pipeline.py`,
      `…/data/file/__init__.py` exist; the loose `data/file.py` and
      `data/file_pipeline.py` are gone.
- [x] `conversation_pipeline.py` imports `load_conversation_document` from
      `tree.data.conversation.conversation`; `file_pipeline.py` imports
      `load_file_document` from `tree.data.file.file`.
- [x] `mcp/tools.py` imports `ingest_conversation` from
      `tree.data.conversation.conversation_pipeline` and `ingest_file` from
      `tree.data.file.file_pipeline`.
- [x] `orchestrator.py`'s two commented-out imports reference the new paths.
- [x] `web/web.py` line-5 docstring reads `tree.data.file.file.load_file_document`.
- [x] `test_conversation.py` patches `tree.data.conversation.conversation.*` and
      passes; `test_file.py` patches `tree.data.file.file.*` and passes.
- [x] `grep -rn "tree\.data\.conversation\b\|tree\.data\.conversation_pipeline\b"
      apps/memory` returns ONLY new-path matches
      (`tree.data.conversation.conversation` / `…conversation_pipeline`) — no bare
      `tree.data.conversation` / `tree.data.conversation_pipeline` import survives.
- [x] `grep -rn "tree\.data\.file_pipeline\b" apps/memory` returns nothing; every
      `tree.data.file` reference is either the new `tree.data.file.file` /
      `tree.data.file.file_pipeline` module path OR the untouched
      `file_data.file_uri` / `Part.from_uri(file_uri=...)` SDK usage in the YouTube
      transcript test (which is NOT an import).
- [x] `_DEPLOYMENT_SPECS` in `orchestrator.py` is UNCHANGED (no serve/registration
      delta — these pipelines remain un-deployed under the free-tier cap).
- [x] `make memory-format-fix && make memory-lint-fix && make memory-format-check &&
      make memory-lint-check` all clean.
- [x] `make pre-commit` passes.
- [x] `make memory-unit-tests` passes, 0 warnings.
- [x] `make memory-integration-tests` (fast tail) passes (the MCP imports moved).
      NB: 176 passed; 2 PRE-EXISTING flakes failed in the aggregate run
      (`test_indexing_pipeline::test_embeds_nodes` [requires_mongot] +
      `test_meta_state::TestRecordDreamRun::test_updated_at_is_recent`) — both in the
      `tree.memory.*` domain, neither touches `tree.data.conversation`/`tree.data.file`.
      Proven independent: each PASSES in isolation on clean HEAD *and* with this move
      applied (see Log). Move-relevant tests all green.
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

### [SWE] 2026-06-23 — Implementation: module-ize conversation + file

**Summary**
Pure code-org (no behavior change). Promoted `conversation` and `file` to packages
mirroring the `web/web.py` + `web/web_pipeline.py` convention, repointed every importer
to the new packaged paths, and rewrote the two unit suites' import + `mocker.patch`
target strings. Built on `refactor/fold-sharding-and-decouple-config-tests`, LOCAL env,
docker stack up. NOT committed — handing to Tester.

**Moves (`git mv`, history preserved — staged as renames `R`)**
- `data/conversation.py` → `data/conversation/conversation.py`
- `data/conversation_pipeline.py` → `data/conversation/conversation_pipeline.py`
- `data/file.py` → `data/file/file.py`
- `data/file_pipeline.py` → `data/file/file_pipeline.py`
- new empty `data/conversation/__init__.py` + `data/file/__init__.py` (like `web/__init__.py`)

**Repoints**
- `conversation/conversation_pipeline.py`: import + a docstring `:func:` path →
  `tree.data.conversation.conversation.load_conversation_document`.
- `file/file_pipeline.py`: import → `tree.data.file.file.load_file_document`.
- `mcp/tools.py`: `_ingest_conversation` ← `tree.data.conversation.conversation_pipeline`,
  `_ingest_file` ← `tree.data.file.file_pipeline` (ruff re-wrapped/sorted the 3-line
  import block; lint clean).
- `orchestrator.py`: the two COMMENTED-OUT imports → new paths (diff is ONLY those 2
  lines; `_DEPLOYMENT_SPECS` and all live topology untouched).
- `web/web.py:5`: docstring `tree.data.file.file.load_file_document`.
- `tests/unit/data/test_conversation.py`: import + all 21 `tree.data.conversation.*`
  patch targets → `tree.data.conversation.conversation.*`.
- `tests/unit/data/test_file.py`: top-level + 3 inline imports + 4 patch targets →
  `tree.data.file.file.*`.

**Untouched (per CAUTION)**
- `tests/unit/data/youtube/test_gemini_transcript_fetcher.py` `file_data.file_uri` /
  `Part.from_uri(file_uri=...)` — Gemini SDK usage, NOT a `tree.data.file` import.
  Verified `git diff --stat` empty for that file.

**Acceptance criteria** (15 total: 14 verified, 1 `[HUMAN]` deferred)
- [x] conversation/file are packages; loose root modules gone — `ls` evidence below.
- [x] internal pipeline imports, `mcp/tools.py`, orchestrator comments, web docstring,
      both test suites all on new paths — grep evidence below.
- [x] grep ACs: no bare `tree.data.conversation` / `tree.data.conversation_pipeline`
      survives; `tree.data.file_pipeline` returns nothing.
- [x] `_DEPLOYMENT_SPECS` UNCHANGED (orchestrator diff = 2 comment lines only).
- [x] format-fix / lint-fix / format-check / lint-check clean; `make pre-commit` passes.
- [x] `make memory-unit-tests`: 1598 passed, 0 warnings.
- [x] `make memory-integration-tests` (fast tail): 176 passed, 1 skipped — see Notes
      for the 2 PRE-EXISTING flakes (proven independent of this move).
- [ ] [HUMAN] Live smoke (serve workflows + `make memory-run-data-pipeline` web/custom
      URL ingest + MCP `ingest_url`/`ingest_file`/`ingest_conversation` sanity) —
      DEFERRED to owner, mirroring #074. Cannot run headless. Automated proxy done:
      every moved module + `mcp/tools.py`'s `_ingest_conversation`/`_ingest_file`
      import-resolve verified (evidence below).

**Evidence**
```
$ ls apps/memory/src/tree/data/{conversation,file}/*.py
conversation/__init__.py  conversation/conversation.py  conversation/conversation_pipeline.py
file/__init__.py          file/file.py                  file/file_pipeline.py
$ ls data/conversation.py data/file.py ...  → "No such file or directory" (loose files gone)

$ grep -rn "tree\.data\.conversation\b\|tree\.data\.conversation_pipeline\b" apps/memory
   → every hit is tree.data.conversation.conversation[_pipeline] (no bare path survives)
$ grep -rn "tree\.data\.file_pipeline\b" apps/memory   → (nothing; exit 1)

$ uv run python -c "import tree.mcp.tools as t; ..."
conversation.conversation: load_conversation_document
conversation_pipeline.ingest_conversation: ingest-conversation-etl
file.file: load_file_document
file_pipeline.ingest_file: ingest-file-etl
mcp.tools._ingest_conversation: ingest-conversation-etl
mcp.tools._ingest_file: ingest-file-etl
ALL IMPORTS RESOLVE OK

$ git diff apps/memory/src/tree/orchestrator.py
-# from tree.data.conversation_pipeline import ingest_conversation
-# from tree.data.file_pipeline import ingest_file
+# from tree.data.conversation.conversation_pipeline import ingest_conversation
+# from tree.data.file.file_pipeline import ingest_file
   (only these 2 comment lines; _DEPLOYMENT_SPECS unchanged)

$ make memory-unit-tests        → 1598 passed in 51.62s
$ pytest tests/unit/data/test_conversation.py tests/unit/data/test_file.py
                                → 35 passed
$ make memory-integration-tests → 2 failed, 176 passed, 1 skipped, 104 deselected
```

**Notes — the 2 integration "failures" are PRE-EXISTING flakes, NOT regressions**
- Failures: `tests/integration/memory/test_indexing_pipeline.py::TestMemoryIndexingPipeline::test_embeds_nodes`
  (marked `@pytest.mark.requires_mongot` — the unreliable mongot/vector-index path
  CLAUDE.md flags) and `tests/integration/memory/test_meta_state.py::TestRecordDreamRun::test_updated_at_is_recent`.
- Both live entirely in `tree.memory.*` (KG node embedding / dream meta-state); NEITHER
  imports or references `tree.data.conversation` or `tree.data.file`.
- Proof of independence (run both tests in isolation, `-p no:randomly`):
  - On CLEAN baseline HEAD (my changes `git stash`-ed away): `2 passed`.
  - With this move APPLIED (stash popped): `2 passed`.
  So they pass with and without the move; they only fail intermittently in the full
  aggregate — consistent with CLAUDE.md's documented shared-DB cleanup-fixture collision
  on the integration tail. Zero linkage to this task's code-org change.
- The lone stderr `UserWarning: Core Pydantic V1 ... isn't compatible with Python 3.14`
  is a pre-existing third-party (opik) import-time warning, present on baseline too; the
  `make memory-unit-tests` target reports 0 test warnings.

**Handoff** — DO NOT COMMIT (Tester reviews first). Status set `in-progress`; file kept
in `tracker/` (not moved to `done/`).

### [Tester] 2026-06-23 — QA — PASS

**Test summary**
- Format / lint / pre-commit: PASS (`format-check`: 282 files formatted; `lint-check`: All checks passed; `pre-commit`: prettier/ruff-check/ruff-format/biome all Passed)
- Unit tests: 1598 passed / 0 failed, 0 warnings (`make memory-unit-tests`, 48.67s)
- Integration tests (fast tail): 176 passed / 1 skipped / 104 deselected / 2 failed — both failures PRE-EXISTING flakes, proven independent below
- Moved suites alone: 35 passed (`test_conversation.py` + `test_file.py`, 1.20s)

**E2E adversarial pass** (AC #15 live pipeline is `[HUMAN]`-deferred; exercised the moved code paths + importers directly as the automatable proxy)
- Happy path: new packaged imports all resolve — `tree.data.conversation.conversation.load_conversation_document`, `…conversation_pipeline.ingest_conversation` (flow `ingest-conversation-etl`), `tree.data.file.file.load_file_document`, `…file_pipeline.ingest_file` (flow `ingest-file-etl`); `conversation`/`file` are packages; `tree.mcp.tools` loads and wires both ingest fns; `tree.orchestrator` loads with 5 deployment specs. (PASS)
- Break path 1 (negative import — old loose modules must be gone): `from tree.data.file import load_file_document` → ImportError; `from tree.data.conversation import load_conversation_document` → ImportError; `import tree.data.conversation_pipeline` / `tree.data.file_pipeline` → ModuleNotFoundError. All correctly fail. (PASS)
- Break path 2 (file boundary/malformed inputs): `read_file('/nonexistent.txt')` → FileNotFoundError (graceful); `read_file(<.xyz>)` unsupported ext → ValueError (graceful); `read_file(<empty .txt>)` → reads OK len 0. No crash, no leaked traceback. (PASS)
- Break path 3 (content-hash boundary/unicode): `_content_hash('')` deterministic; `_content_hash('café—🌳')` deterministic 16-char digest (truncated-by-design, verbatim-unchanged from HEAD). (PASS)

**Acceptance criteria** (14 automatable verified; 1 `[HUMAN]` deferred)
- [x] PASS — conversation/file are packages; loose root modules gone — `git diff -M HEAD` shows 4 renames; `ls` confirms `<module>.py`+`<module>_pipeline.py`+empty `__init__.py`; old `data/{conversation,file}{,_pipeline}.py` deleted.
- [x] PASS — internal pipeline imports repointed — old→new file diff: `conversation_pipeline.py` import (L13) + docstring `:func:` (L70) only; `file_pipeline.py` import (L12) only.
- [x] PASS — `mcp/tools.py` imports `ingest_conversation`←`tree.data.conversation.conversation_pipeline`, `ingest_file`←`tree.data.file.file_pipeline` (diff L13/L16).
- [x] PASS — `orchestrator.py` two commented imports on new paths (diff L46-47); rest of file unchanged.
- [x] PASS — `web/web.py:5` docstring reads `tree.data.file.file.load_file_document`.
- [x] PASS — `test_conversation.py` patches `tree.data.conversation.conversation.*`, `test_file.py` patches `tree.data.file.file.*`; 35 passed.
- [x] PASS — grep AC1: `tree.data.conversation\b|tree.data.conversation_pipeline\b` → ONLY new-path hits, no bare module import survives.
- [x] PASS — grep AC2: `tree.data.file_pipeline\b` → nothing (exit 1); every `tree.data.file` ref is `tree.data.file.file[_pipeline]` OR the untouched YouTube `file_uri`/`Part.from_uri(file_uri=…)` SDK usage (file untouched per `git diff --stat`).
- [x] PASS — `_DEPLOYMENT_SPECS` byte-identical to HEAD (`diff` of the block → exit 0); orchestrator change is exactly the 2 comment lines.
- [x] PASS — format-fix/lint-fix/format-check/lint-check clean.
- [x] PASS — `make pre-commit` passes.
- [x] PASS — `make memory-unit-tests`: 1598 passed, 0 warnings.
- [x] PASS — `make memory-integration-tests` (fast tail): MCP imports resolve; 176 passed (see flake note).
- [ ] [HUMAN] — Live smoke DEFERRED to owner (cannot run headless), mirroring #074. Automated proxy DONE: every moved module + `mcp/tools.py` `_ingest_conversation`/`_ingest_file` import-resolve verified in-env; left correctly unchecked.

**Evidence — the 2 integration failures are PRE-EXISTING flakes, NOT regressions**
- `test_indexing_pipeline::TestMemoryIndexingPipeline::test_embeds_nodes` (`@requires_mongot`) + `test_meta_state::TestRecordDreamRun::test_updated_at_is_recent`.
- Neither test references `tree.data.conversation`/`tree.data.file` (grep → no references; both live in `tree.memory.*`).
- Re-run in ISOLATION on this working tree (`-p no:randomly`): `2 passed in 5.67s`. So they pass standalone and only fail in the full aggregate — the documented shared-DB cleanup-fixture collision on the integration tail. Zero linkage to this code-org move.

**Other issues found**
- None blocking. Minor doc note: the spec's parenthetical "`__init__.py` (empty, like `data/web/__init__.py`)" is slightly inaccurate — `web/__init__.py` is 399 bytes (not empty). The SWE made the two new `__init__.py` empty, which matches the package-marker convention and is the correct choice; not a defect.

**VERDICT: PASS**
QA PASSED. Pure code-org move verified: nothing broke, zero stale old-path references anywhere in `apps/memory`, history preserved (4 renames), `conversation.py`/`file.py` moved byte-identical, pipelines changed import lines only, `_DEPLOYMENT_SPECS` untouched, packages + MCP module import cleanly. Hand off to PA for acceptance review (the `[HUMAN]` live smoke remains for the owner).
