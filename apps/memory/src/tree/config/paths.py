"""
Memory-app path constants.

``.tree/`` is the working directory for ephemeral/session data that should not be
committed to version control (deep-search files, rendered graph HTML). Its
location is the ``TREE_WORKING_DIR`` setting (``.env`` / ``.env.prod``), which
defaults to the project's ``apps/memory/.tree`` when unset. A serverless host
whose install dir is read-only (Prefect Horizon) sets ``TREE_WORKING_DIR=/tmp/.tree``.
"""

from pathlib import Path

from tree.config.settings import settings

_PROJECT_ROOT = Path(__file__).resolve().parents[3]


def _resolve_working_dir(project_root: Path, override: str) -> Path:
    """Resolve the ``.tree`` working dir from the ``TREE_WORKING_DIR`` setting.

    ``override`` (``settings.tree_working_dir``) wins when set; otherwise default
    to the in-repo ``<project>/.tree``.
    """

    return Path(override) if override else project_root / ".tree"


TREE_WORKING_DIR: Path = _resolve_working_dir(_PROJECT_ROOT, settings.tree_working_dir)
"""Root directory for short-term working data (`.tree/`)."""

MEMORY_DIR: Path = TREE_WORKING_DIR / "memory"
"""Directory for deep-search session files (`.tree/memory/`)."""

GRAPHS_DIR: Path = TREE_WORKING_DIR / "graphs"
"""Directory for rendered knowledge-graph HTML files (`.tree/graphs/`)."""
