# Flatten the `data/core/` package → `data/ingest.py`

Status: pending
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

- [ ] `apps/memory/src/tree/data/ingest.py` exists with the URL-router code; the file
      retains its history (moved via `git mv`).
- [ ] `apps/memory/src/tree/data/core/` no longer exists (no `ingest.py`, no
      `__init__.py`, directory gone).
- [ ] `grep -rn "tree\.data\.core" apps/memory` returns ZERO matches (src AND tests).
- [ ] `tree.data.ingest.ingest_url` imports and behaves identically to the old
      `tree.data.core.ingest.ingest_url` — no body changes, no behavior change.
- [ ] `apps/memory/src/tree/mcp/tools.py` imports `ingest_url` from
      `tree.data.ingest`.
- [ ] The URL-router unit suite (`test_ingest.py`, wherever it now lives) patches
      `tree.data.ingest.*` targets and passes.
- [ ] The two integration importers
      (`test_ingest_url_after_dispatcher_migration.py`,
      `test_web_pipeline.py`) import from `tree.data.ingest` and pass.
- [ ] `make memory-format-fix && make memory-lint-fix && make memory-format-check &&
      make memory-lint-check` all clean.
- [ ] `make pre-commit` passes.
- [ ] `make memory-unit-tests` passes, 0 warnings.
- [ ] `make memory-integration-tests` (fast tail) passes (touches the MCP imports +
      the dispatcher-migration regression test).

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
