# Flatten the `data/core/` package → `data/ingest.py`

Status: in-progress
Tags: `data`, `refactor`
Depends on: #075
Blocks: —

## Scope

`apps/memory/src/tree/data/core/` holds only `ingest.py` (the `ingest_url` **URL
router**, the MCP single-URL dispatcher) plus a one-line `__init__.py`. A one-file
package adds nothing — flatten it to `apps/memory/src/tree/data/ingest.py` and delete
the `core/` directory. KEEP the filename `ingest.py` (the owner's words: "we just have
`ingest.py`"). This is a pure, mechanical move with ZERO behavior change — `ingest_url`
and every helper keep their bodies; only the module path changes
(`tree.data.core.ingest` → `tree.data.ingest`).

Do #075 first: it already removed `pipeline.py`'s `from tree.data.core.ingest import
ingest_url`, so that importer is NOT in the repoint list below.

### 1. Move the file and delete the empty package

- `git mv apps/memory/src/tree/data/core/ingest.py apps/memory/src/tree/data/ingest.py`
- Delete `apps/memory/src/tree/data/core/__init__.py` and the now-empty
  `apps/memory/src/tree/data/core/` directory.

### 2. Repoint production importers

- `apps/memory/src/tree/mcp/tools.py:14`
  `from tree.data.core.ingest import ingest_url as _ingest_url_dispatch`
  → `from tree.data.ingest import ingest_url as _ingest_url_dispatch`.

### 3. Repoint test importers

- `apps/memory/tests/unit/data/core/test_ingest.py` — this is the URL-router unit
  suite and it `mocker.patch`-es / imports `tree.data.core.ingest.*` in MANY places
  (the `from tree.data.core.ingest import (...)` block plus ~20 patch-target strings
  and a `caplog` logger name `"tree.data.core.ingest"`). Two equivalent options — pick
  one and be consistent:
  - **(preferred)** `git mv apps/memory/tests/unit/data/core/test_ingest.py
    apps/memory/tests/unit/data/test_ingest.py`, delete the now-empty
    `apps/memory/tests/unit/data/core/` dir (and its `__init__.py`), and rewrite every
    `tree.data.core.ingest` string → `tree.data.ingest` (mirror the source layout: the
    test moves up alongside the flattened module).
  - **(acceptable)** keep the file in place and only rewrite the `tree.data.core.ingest`
    strings → `tree.data.ingest`.
  Either way, NO `tree.data.core` string may remain in this file.
- `apps/memory/tests/integration/mcp/test_ingest_url_after_dispatcher_migration.py:24-25`
  `from tree.data.core import ingest as ingest_module`
  → `from tree.data.ingest import ...` (it uses `ingest_module` as a module handle —
  repoint to `from tree.data import ingest as ingest_module`), and
  `from tree.data.core.ingest import _get_configured_substack_domains, ingest_url`
  → `from tree.data.ingest import _get_configured_substack_domains, ingest_url`.
- `apps/memory/tests/integration/data/web/test_web_pipeline.py:22`
  `from tree.data.core.ingest import ingest_url`
  → `from tree.data.ingest import ingest_url`. (This file's `ingest_url` is still used
  by `test_dispatcher_falls_through_to_web` / `test_dispatcher_routes_substack_first`,
  so the import STAYS — only the path changes; #075 removed the worker's web→`ingest_url`
  coupling but these dispatcher tests exercise the MCP router directly.)

### 4. Fix in-file references to "core"

- In the moved `ingest.py`, fix any docstring/comment that says "core" referring to the
  old package location (the module docstring describes the URL dispatcher — confirm it
  reads correctly at its new path; there's no `tree.data.core` self-reference in the
  body, but scan for stray "core" wording).

### Files touched

- `apps/memory/src/tree/data/core/ingest.py` → `apps/memory/src/tree/data/ingest.py`
  (git mv).
- DELETE `apps/memory/src/tree/data/core/__init__.py` + the empty `core/` dir.
- `apps/memory/src/tree/mcp/tools.py` — repoint line 14.
- `apps/memory/tests/unit/data/core/test_ingest.py` (→ `…/unit/data/test_ingest.py`)
  — repoint all `tree.data.core.ingest` strings.
- `apps/memory/tests/integration/mcp/test_ingest_url_after_dispatcher_migration.py` —
  repoint lines 24-25.
- `apps/memory/tests/integration/data/web/test_web_pipeline.py` — repoint line 22.

## Acceptance Criteria

- [x] `apps/memory/src/tree/data/ingest.py` exists with the URL-router code; the file
      retains its history (moved via `git mv`).
- [x] `apps/memory/src/tree/data/core/` no longer exists (no `ingest.py`, no
      `__init__.py`, directory gone).
- [x] `grep -rn "tree\.data\.core" apps/memory` returns ZERO matches (src AND tests).
- [x] `tree.data.ingest.ingest_url` imports and behaves identically to the old
      `tree.data.core.ingest.ingest_url` — no body changes, no behavior change.
- [x] `apps/memory/src/tree/mcp/tools.py` imports `ingest_url` from
      `tree.data.ingest`.
- [x] The URL-router unit suite (`test_ingest.py`, wherever it now lives) patches
      `tree.data.ingest.*` targets and passes.
- [x] The two integration importers
      (`test_ingest_url_after_dispatcher_migration.py`,
      `test_web_pipeline.py`) import from `tree.data.ingest` and pass.
- [x] `make memory-format-fix && make memory-lint-fix && make memory-format-check &&
      make memory-lint-check` all clean.
- [x] `make pre-commit` passes.
- [x] `make memory-unit-tests` passes, 0 warnings.
- [x] `make memory-integration-tests` (fast tail) passes (touches the MCP imports +
      the dispatcher-migration regression test) — the 2 unrelated failures
      (`test_indexing_pipeline::test_embeds_nodes`, `test_meta_state::test_updated_at_is_recent`)
      are pre-existing on this branch: identical on a clean-HEAD baseline run.

## BDD scenarios

### Scenario: the URL router moves without behavior change
- **Given** the flattened `tree.data.ingest` module
- **When** a caller does `from tree.data.ingest import ingest_url` and routes a URL
- **Then** routing matches the pre-move behavior exactly (static registry → custom
  Substack domain → generic-web fallback), and `tree.data.core` is no longer
  importable.

### Scenario: no stray old-path reference survives
- **Given** the completed move
- **When** I run `grep -rn "tree\.data\.core" apps/memory`
- **Then** it returns nothing — src, unit tests, and integration tests all use
  `tree.data.ingest`.

### Scenario: the MCP single-URL tool still dispatches
- **Given** `mcp/tools.py` importing `ingest_url` from `tree.data.ingest`
- **When** the `ingest_url` MCP tool is invoked
- **Then** it dispatches through the URL router exactly as before the move.

## User Stories

### Story: A maintainer finds the URL router where the layout implies it should be
1. A maintainer looks for the single-URL dispatcher.
2. They find `apps/memory/src/tree/data/ingest.py` directly under `data/` (no
   one-file `core/` package to drill into).
3. `from tree.data.ingest import ingest_url` works everywhere; the old
   `tree.data.core.ingest` path is gone.

### Story: A test author patches the router at its real path
1. A test author writes a new test that needs to stub a router handler.
2. They patch `tree.data.ingest._ingest_web_url` (not the stale
   `tree.data.core.ingest...`) and it resolves first try.

## Test guidance

- This is a pure move: NO new behavior, NO new tests. The existing URL-router unit
  suite + the two integration tests provide full coverage; they must pass UNCHANGED
  except for the import/patch-path rewrite.
- The decisive check is `grep -rn "tree\.data\.core" apps/memory` returning empty —
  add it to your verification pass.
- Run `make memory-unit-tests` then `make memory-integration-tests` on the LOCAL env
  with the stack up. Never a bare `uv run pytest` (no `.env`). Per CLAUDE.md, call the
  `/testing-python` skill if you touch the test files beyond the mechanical path
  rewrite.

---

Blocked by: #075

## Log

### [PA] 2026-06-22 — Grooming

**Summary**
Flatten the one-file `tree.data.core` package to `tree.data.ingest` (`git mv`
`core/ingest.py` → `ingest.py`, delete `core/__init__.py` + the empty dir), and
repoint every importer: `mcp/tools.py`, the URL-router unit suite, and the two
integration tests. Pure mechanical move, zero behavior change.

**Key decisions**
- Keep the filename `ingest.py` per the owner; the new path is `tree.data.ingest`.
- The brief listed three importers; codebase research surfaced a FOURTH —
  `apps/memory/tests/unit/data/core/test_ingest.py`, the URL-router unit suite, which
  references `tree.data.core.ingest` in ~20 patch-target strings + its import block +
  a caplog logger name. I added it to the repoint list and recommended moving the test
  up alongside the flattened module (deleting the empty `tests/.../data/core/` dir) so
  the test layout mirrors the source layout. Without this the move would leave the
  largest cluster of stale references and the suite would fail.
- The `ingest_url` import in `test_web_pipeline.py` STAYS (still used by two
  dispatcher tests) — only its path is repointed; #075 removed the *worker's* web →
  `ingest_url` coupling, not the MCP-router tests.
- Acceptance hinges on `grep -rn "tree\.data\.core" apps/memory` returning empty.

**Dependencies**
- #075 — it already deleted `pipeline.py`'s `from tree.data.core.ingest import
  ingest_url`, so doing #075 first removes that importer from this task's repoint set.

**User stories**
- 2 stories: maintainer finds the router at the implied path; test author patches it
  at the real path.

**Open questions**
- None. (Test-move vs in-place rewrite for `test_ingest.py` is the SWE's call — both
  satisfy the "no `tree.data.core` survives" AC; preferred is moving it up.)

Ready for implementation.

### [SWE] 2026-06-23 — Implementation

**Summary**
Flattened the one-file `tree.data.core` package to `tree.data.ingest`. Pure
mechanical move, zero behavior change — `ingest_url` and every helper keep their
bodies; only the module path changed. The preferred option was taken for the test
suite: it was moved up alongside the flattened module.

**Moves (git-mv, history preserved as renames)**
- `apps/memory/src/tree/data/core/ingest.py` → `apps/memory/src/tree/data/ingest.py`
  (`git diff --staged -M` shows `core => }/ingest.py` with 0 body changes).
- `apps/memory/tests/unit/data/core/test_ingest.py` →
  `apps/memory/tests/unit/data/test_ingest.py` (rename + 56-line path rewrite).
- DELETED `apps/memory/src/tree/data/core/__init__.py`,
  `apps/memory/tests/unit/data/core/__init__.py`, and both now-empty `core/` dirs.

**Repoints (`tree.data.core.ingest` → `tree.data.ingest`)**
- `apps/memory/src/tree/mcp/tools.py:14` — `ingest_url as _ingest_url_dispatch`.
- `apps/memory/tests/unit/data/test_ingest.py` — import block + ~20 `mocker.patch`
  target strings + the `caplog` logger name + the module docstring (all via the
  single literal `tree.data.core.ingest` substring; nothing else referenced it).
- `apps/memory/tests/integration/mcp/test_ingest_url_after_dispatcher_migration.py:24-25`
  — `from tree.data import ingest as ingest_module` (module handle for
  `mocker.patch.object`) + `from tree.data.ingest import _get_configured_substack_domains, ingest_url`.
- `apps/memory/tests/integration/data/web/test_web_pipeline.py:22` —
  `from tree.data.ingest import ingest_url` (still used by the two dispatcher tests).

**In-file "core" references**
- The moved `ingest.py` module docstring describes the URL dispatcher generically;
  it has no `tree.data.core` self-reference and no stray "core" wording referring to
  the old package location. Nothing to fix; reads correctly at the new path.

**Verification**
- `grep -rn "tree\.data\.core" apps/memory` → ZERO matches (the decisive AC).
- Import smoke (env-loaded): `from tree.data.ingest import ingest_url`,
  `from tree.data import ingest as ingest_module`, and
  `from tree.mcp.tools import _ingest_url_dispatch` all resolve;
  `ingest_url is _ingest_url_dispatch` → True; `import tree.data.core` →
  `ModuleNotFoundError` (old path gone, as the BDD scenario requires).
- `make memory-format-fix && memory-lint-fix && memory-format-check &&
  memory-lint-check` → all clean ("All checks passed!", 280 files formatted).
- `make pre-commit` → all hooks Passed.
- `make memory-unit-tests` → **1598 passed, 0 warnings** (incl. the moved
  `tests/unit/data/test_ingest.py`, 25 passed in isolation patching
  `tree.data.ingest.*`).
- `make memory-integration-tests` (fast tail) → 176 passed, 1 skipped, 2 failed.
  The 2 failures (`test_indexing_pipeline::test_embeds_nodes`,
  `test_meta_state::test_updated_at_is_recent`) are in the memory-indexing /
  dream-consolidation area — disjoint from `tree.data.ingest`/MCP. **Proven
  pre-existing**: a clean-HEAD baseline run (my change stashed) failed the identical
  2 tests with identical counts (`2 failed, 176 passed, 1 skipped, 104 deselected`).
  The two task-relevant importer files
  (`test_ingest_url_after_dispatcher_migration.py` + `test_web_pipeline.py`'s
  dispatcher tests) → 8 passed.

**Notes**
- NOT COMMITTED — handing to Tester for review.
- The `opik` `UserWarning` (Pydantic V1 on Python 3.14) in import-smoke output is a
  pre-existing, change-unrelated warning; unit run reports 0 warnings.

### [Tester] 2026-06-23 — QA

**Test summary**
- Format-check / lint-check: PASS (`ruff format --check` → "280 files already
  formatted"; `ruff check` → "All checks passed!").
- Pre-commit: PASS (prettier, ruff check, ruff format, biome — all Passed).
- Unit tests (`make memory-unit-tests`): 1598 passed / 0 failed, **0 warnings**.
- Fast integration tail (`make memory-integration-tests`): 176 passed / 2 failed /
  1 skipped / 104 deselected. The 2 failures (`test_indexing_pipeline::test_embeds_nodes`,
  `test_meta_state::TestRecordDreamRun::test_updated_at_is_recent`) are **proven
  pre-existing** — see below. Both pass in isolation; both fail identically on a
  stashed clean-HEAD full fast-tail run (`2 failed, 176 passed, 1 skipped, 104
  deselected`). Disjoint from the move: neither file is touched by the diff and
  neither references `tree.data.ingest` / `ingest_url` / `tree.data.core`.

**Move integrity (independently verified)**
- `git diff --staged -M` → `rename apps/memory/src/tree/data/{core => }/ingest.py
  (100%)` — source body byte-identical (`diff` of HEAD:core/ingest.py vs
  :data/ingest.py → no output). URL-router LOGIC unchanged.
- Test file rename 85%; `diff` of old-test (with `tree.data.core.ingest →
  tree.data.ingest` sed) vs new-test → IDENTICAL. Only path strings rewritten, no
  test logic touched.
- Deleted `core/__init__.py` (src + test) were both 0 bytes; `core/` packages gone
  from git tree (0 `core/` entries staged under `data/`).

**E2E adversarial pass** (router exercised via the NEW `tree.data.ingest` path,
registry leaves mocked so no network)
- Happy path: `ingest_url("https://example.substack.com/p/post")` → routed to
  substack handler (PASS); `youtube.com` & `youtu.be` → youtube handler (PASS);
  unknown domain → web fallback (PASS). Match order preserved.
- Break 1 (boundary: empty string): `ingest_url("")` → `ValueError: Unsupported
  URL scheme ''` (PASS — clean error, no crash).
- Break 2 (malformed: wrong scheme): `ftp://example.com/x` → `ValueError:
  Unsupported URL scheme 'ftp'` (PASS).
- Break 3 (boundary: missing host): `https://` → `ValueError: URL is missing a
  host` (PASS).
- Break 4 (state edge: feed-shaped URL guard): `https://www.youtube.com/feeds/videos.xml`
  → `ValueError: RSS feed URLs are not supported by ingest_url` (PASS).
- Break 5 (malformed: non-URL garbage): `"not a url at all"` → `ValueError:
  Unsupported URL scheme ''` (PASS). Non-ASCII Unicode host (`https://exämple.com/x`)
  → routes to web fallback, no crash (PASS).
- `from tree.data.ingest import ingest_url` + `from tree.mcp.tools import
  _ingest_url_dispatch` resolve; `ingest_url is _ingest_url_dispatch` → True (MCP
  dispatch points at the moved function).
- `import tree.data.core` correctly raises `ModuleNotFoundError` in a clean
  environment. (On the dev machine it briefly resolved as an implicit namespace
  package because a stale `core/__pycache__` lingered on disk — NOT in git's tree,
  absent from any clean clone/CI; I removed the stale bytecode dir and re-confirmed
  `ModuleNotFoundError`.)

**Acceptance criteria** — all 11 verified PASS
- [x] PASS — `data/ingest.py` exists, history preserved — `git diff --staged -M` →
      `rename .../{core => }/ingest.py (100%)`.
- [x] PASS — `data/core/` gone — 0 `core/` entries in staged tree; both `__init__.py`
      staged as deletions.
- [x] PASS — `grep -rn "tree\.data\.core" apps/memory` → ZERO (exit 1). Broader
      greps for `data/core`, `data.core` across .py/.toml/.yaml/.md/Makefile → zero.
- [x] PASS — `ingest_url` body byte-identical (proven) + adversarial e2e shows
      identical routing/validation behavior.
- [x] PASS — `mcp/tools.py:14` → `from tree.data.ingest import ingest_url as
      _ingest_url_dispatch`.
- [x] PASS — `tests/unit/data/test_ingest.py` (moved) patches `tree.data.ingest.*`
      (28 refs, 0 `tree.data.core`); isolated run → 25 passed.
- [x] PASS — `test_ingest_url_after_dispatcher_migration.py` (2 passed: substack +
      web-fallback routing) + `test_web_pipeline.py::TestDispatcherFallback` (imports
      from `tree.data.ingest`, collects clean; 2 dispatcher tests skip only on absent
      Bright Data creds — environmental, pre-existing).
- [x] PASS — format-fix/lint-fix/format-check/lint-check all clean.
- [x] PASS — `make pre-commit` all hooks Passed.
- [x] PASS — `make memory-unit-tests` 1598 passed, 0 warnings.
- [x] PASS — fast integration tail green modulo the 2 proven-pre-existing flakes.

**Evidence**
```
$ make memory-unit-tests
============================ 1598 passed in 49.69s =============================

$ uv run pytest tests/unit/data/test_ingest.py -q
25 passed in 0.52s

$ make memory-integration-tests   # with change
===== 2 failed, 176 passed, 1 skipped, 104 deselected in 170.87s =====
$ make memory-integration-tests   # clean HEAD (change stashed)
===== 2 failed, 176 passed, 1 skipped, 104 deselected in 173.13s =====
  (identical: test_embeds_nodes, test_updated_at_is_recent)

$ grep -rn "tree\.data\.core" apps/memory   # exit 1 → ZERO matches
```

**Other issues found**
- Cosmetic only: a stale `apps/memory/src/tree/data/core/__pycache__/` lingered on
  the dev filesystem (gitignored, not in the commit). I cleared it during QA. Not a
  defect in the change; a clean clone never has it. Worth a `find -name __pycache__
  -delete` habit but no action required for this PR.

**VERDICT: PASS**
