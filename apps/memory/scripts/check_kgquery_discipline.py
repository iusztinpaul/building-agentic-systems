"""Fail when raw KG reads/writes appear outside the allow-list.

Run as ``uv run python scripts/check_kgquery_discipline.py`` (also wired
into the pre-commit hook via ``make memory-check-kgquery-discipline``).

The rule: every read of the ``knowledge_graph`` collection in
production code goes through :class:`tree.memory.query.kgquery.KGQuery`,
which binds ``user_id`` in its constructor and injects it into every
filter. The lint enforces this by flagging:

1. **Beanie ODM bypass**: ``KnowledgeGraphEntry.find(...)`` /
   ``KnowledgeGraphEntry.find_one(...)`` — the original Phase-1 trip
   wire (#019).
2. **Raw pymongo bypass**: ``<collection>.aggregate(...)``,
   ``<collection>.find(...)``, ``<collection>.find_one(...)``,
   ``<collection>.update_many(...)``, ``<collection>.delete_many(...)``,
   on any local variable named ``collection`` (or ``col``/``kg``/``coll``).
   Added in #023 after the PR Reviewer found ``review/core.py`` was
   bypassing the Beanie surface entirely via raw pymongo aggregations —
   the original lint never saw the gap.

The allow-list is conservative: only files that have been audited and
are known to either (a) be the ``KGQuery`` helper itself, (b) live in a
tenant-locked code path that always carries ``user_id``, or (c) be a
one-shot script/test fixture, are exempted. Adding a new entry requires
a SWE+PR-Reviewer sign-off that the file is tenant-safe.

The script prints every violation it found (file + line) and exits
non-zero. Empty output + exit 0 = clean.

Logging discipline: the script uses ``init_logger()`` per the project
convention (CLAUDE.md: "Logging: Native Python logger (never prints!)").
INFO logs go to stdout via the root logger's default handler; errors go
to stderr indirectly through pre-commit's "FAILED" surface. The exit
code is the source of truth for CI.
"""

from __future__ import annotations

import logging
import pathlib
import re
import sys

from tree.logging import init_logger

init_logger()
logger = logging.getLogger(__name__)


# Repo layout: this script lives in apps/memory/scripts.
_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent  # apps/memory/
_SOURCE_DIRS = (
    _REPO_ROOT / "src",
    _REPO_ROOT / "scripts",
    _REPO_ROOT / "deploy",
)

# Forbidden patterns.
#
# 1. ``KnowledgeGraphEntry.find(`` and ``KnowledgeGraphEntry.find_one(`` —
#    the Beanie ODM bypass. This is the original #019 trip-wire.
_BEANIE_BYPASS_RE = re.compile(r"\bKnowledgeGraphEntry\.find(?:_one)?\(")

# 2. Raw pymongo calls on a local handle named ``collection`` /
#    ``col`` / ``kg`` / ``coll``. Catches the #023 leak shape where
#    ``review/core.py`` reached for ``database["knowledge_graph"]`` and
#    issued aggregations without a ``user_id`` filter. Update_one /
#    delete_one are intentionally NOT flagged because both work on
#    tenant-scoped ``_id`` values (``"{user_id}:type:name"`` for nodes,
#    ``"source|type|target"`` for edges whose endpoints carry the user
#    prefix) — a stray ``update_one({"_id": x}, ...)`` cannot cross
#    tenants even without an explicit ``user_id`` predicate.
_RAW_PYMONGO_RE = re.compile(
    r"\b(?:collection|col|kg|coll)\.(?:aggregate|find|find_one|update_many|delete_many)\("
)

# Files allowed to issue the call directly. Paths are relative to
# ``apps/memory/``.
_ALLOWLIST: frozenset[str] = frozenset(
    {
        # --- KGQuery and Beanie bypass exemptions (the original #019 set) ---
        "src/tree/memory/query/kgquery.py",
        "src/tree/entities/users.py",
        "scripts/migrate_multi_tenancy.py",
        # This very script discusses the patterns in its docstring /
        # regex literal — exempt it from itself.
        "scripts/check_kgquery_discipline.py",
        # --- Raw-pymongo audited tenant-locked production paths (#023) ---
        # Each of these threads ``user_id`` into every ``$match`` /
        # filter and has integration coverage in the two-user isolation
        # test (``tests/integration/test_two_user_isolation.py``). Adding
        # a new entry requires a fresh tenant-isolation test that
        # exercises the new path.
        "src/tree/memory/query/core.py",
        "src/tree/memory/query/nl_query.py",
        "src/tree/memory/extraction/dedup.py",
        "src/tree/memory/extraction/pipeline.py",
        "src/tree/memory/indexing/core.py",
        "src/tree/memory/review/core.py",
        # --- Operator / exploration scripts (audited, tenant-aware) ---
        "scripts/query_graph.py",  # CLI: every find() carries user_id
        "scripts/demo_graphrag.py",  # throwaway exploration; not prod.
        "scripts/test_mongodb_setup.py",  # smoke test on test_twin_assets, not KG.
    }
)


def _scan() -> list[tuple[pathlib.Path, int, str]]:
    """Return ``(path, line_number, line)`` for every violation."""

    violations: list[tuple[pathlib.Path, int, str]] = []
    for root in _SOURCE_DIRS:
        if not root.exists():
            continue
        for path in root.rglob("*.py"):
            rel = path.relative_to(_REPO_ROOT).as_posix()
            if rel in _ALLOWLIST:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            for n, line in enumerate(text.splitlines(), start=1):
                if _BEANIE_BYPASS_RE.search(line) or _RAW_PYMONGO_RE.search(line):
                    violations.append((path, n, line.rstrip()))
    return violations


def main() -> int:
    violations = _scan()
    if not violations:
        logger.info("KGQuery discipline OK: no raw knowledge_graph access found.")
        return 0
    logger.error(
        "KGQuery discipline FAILED — raw knowledge_graph access outside the allow-list:"
    )
    for path, n, line in violations:
        logger.error("  %s:%d: %s", path.relative_to(_REPO_ROOT), n, line)
    logger.error(
        "\nFix: route the read through tree.memory.query.kgquery.KGQuery, OR "
        "thread ``user_id`` into every $match / filter at the call site and "
        "add the file to _ALLOWLIST in this script (requires a tenant-"
        "isolation integration test for the new path)."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
