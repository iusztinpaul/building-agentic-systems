"""
Memory-app path constants.

``.tree/`` is the working directory for ephemeral/session data that
should not be committed to version control (added to ``.gitignore``).
It lives inside the memory app at ``apps/memory/.tree/``.
"""

from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[3]

TREE_WORKING_DIR: Path = _PROJECT_ROOT / ".tree"
"""Root directory for short-term working data (`.tree/`)."""

MEMORY_DIR: Path = TREE_WORKING_DIR / "memory"
"""Directory for deep-search session files (`.tree/memory/`)."""

GRAPHS_DIR: Path = TREE_WORKING_DIR / "graphs"
"""Directory for rendered knowledge-graph HTML files (`.tree/graphs/`)."""
