"""The **Mode reset** is the documented way to switch **Memory mode**s.

Grep-level on purpose (the ``test_embedding_map_docs.py`` pattern): the e2e skill
and the memory README must send a reader to ``make memory-reset-mode``, and the
raw ``mongosh`` drop it replaces — which forgot ``memory_clusters`` and never
said which database it emptied — must be gone from everything a reader follows.
ADRs and ``tasks/`` keep it as history.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

# Spelled as a concatenation so this file is not itself a hit for the grep below.
_RAW_DROP = "memory." + "drop()"

_REPO_ROOT = Path(__file__).resolve().parents[4]
_MEMORY_README = _REPO_ROOT / "apps" / "memory" / "README.md"
_E2E_SKILL = _REPO_ROOT / ".agents" / "skills" / "run-pipelines-e2e" / "SKILL.md"


@pytest.mark.parametrize("path", [_MEMORY_README, _E2E_SKILL], ids=["readme", "e2e"])
def test_the_mode_switch_docs_name_the_command(path: Path) -> None:
    assert "make memory-reset-mode CONFIRM=yes" in path.read_text(encoding="utf-8")


def test_the_readme_names_the_prod_token() -> None:
    assert "CONFIRM=prod" in _MEMORY_README.read_text(encoding="utf-8")


def test_the_raw_mongosh_drop_is_gone_from_what_readers_follow() -> None:
    # ``--untracked``: plain ``git grep`` skips files not yet added, so a new
    # file carrying the one-liner would pass until it is committed.
    hits = subprocess.run(
        [
            "git",
            "grep",
            "--untracked",
            "-n",
            "-F",
            _RAW_DROP,
            "--",
            "apps/memory",
            ".agents",
            "README.md",
            "docs/glossary.md",
        ],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
    ).stdout

    assert hits == ""
