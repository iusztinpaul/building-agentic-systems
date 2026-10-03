"""The **Memory structure** surfaces are documented where operators look (task 173).

The old query command and graph-view tool (spelled out in ``_RETIRED_NAMES``)
were replaced by ``make memory-visualize-structure`` + ``make memory-search`` and
the both-modes ``visualize_memory_structure`` tool. Grep-level on purpose, like
``test_embedding_map_docs.py``: they catch a stale command or tool name an agent
would copy, without pinning prose.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

# The retired names, spelled as concatenations so this file is not itself a hit
# for the stale-name guard below. The ADRs and ``tasks/`` keep them: they record
# the history.
_RETIRED_NAMES = (
    "memory-" + "query-graph",
    "query" + "_graph",
    "visualize_memory" + "_graph",
)

_REPO_ROOT = Path(__file__).resolve().parents[4]
_ROOT_README = _REPO_ROOT / "README.md"
_MEMORY_README = _REPO_ROOT / "apps" / "memory" / "README.md"
_MEMORY_SKILL = _REPO_ROOT / ".agents" / "skills" / "tree-memory" / "SKILL.md"
_E2E_SKILL = _REPO_ROOT / ".agents" / "skills" / "run-pipelines-e2e" / "SKILL.md"
# The tree-memory skill is a facade for the agent, which reaches the structure
# through the MCP tool — the CLI surfaces are operator docs, so it is not listed.
_DOCS = [_ROOT_README, _MEMORY_README, _E2E_SKILL]


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


@pytest.mark.parametrize("path", _DOCS, ids=lambda p: str(p.relative_to(_REPO_ROOT)))
@pytest.mark.parametrize(
    "needle",
    [
        "make memory-visualize-structure",
        "make memory-search",
        "visualize_memory_structure",
    ],
)
def test_every_operator_doc_names_the_structure_surfaces(
    path: Path, needle: str
) -> None:
    assert needle in _read(path)


def test_the_skill_lists_the_structure_tool_in_both_modes() -> None:
    row = next(
        line
        for line in _read(_MEMORY_SKILL).splitlines()
        if line.startswith("| `visualize_memory_structure` |")
    )

    # Two ✅ columns = rag AND graphrag.
    assert row.count("✅") == 2


def test_the_retired_names_are_gone_outside_the_history() -> None:
    # ``--untracked`` is load-bearing: plain ``git grep`` skips files not yet
    # added, so a brand-new file carrying an old name would pass until committed.
    needles = [arg for name in _RETIRED_NAMES for arg in ("-e", name)]
    hits = subprocess.run(
        [
            "git",
            "grep",
            "--untracked",
            "-n",
            *needles,
            "--",
            "apps/memory",
            ".agents",
            "docs/glossary.md",
            "README.md",
        ],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
    ).stdout

    assert hits == ""
