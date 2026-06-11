"""
Memory-app path constants.

``.tree/`` is the working directory for ephemeral/session data that should not be
committed to version control (deep-search files, rendered graph HTML). Its
location depends on how the app runs:

* **Local dev (source checkout):** ``<repo>/apps/memory/.tree`` — inside the app,
  gitignored, as before.
* **Prod (installed package, e.g. the serverless MCP server):** ``/tmp/.tree`` —
  the installed package lives under a read-only ``site-packages`` whose parent is
  not writable, so we default to ``/tmp`` (writable on serverless hosts).

Set ``TREE_WORKING_DIR`` to override the location explicitly in any environment.
"""

import os
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[3]

# Prod fallback when the package is installed rather than run from the source tree
# (a serverless host's ``site-packages`` parent is read-only; ``/tmp`` is writable).
_PROD_WORKING_DIR = Path("/tmp/.tree")


def _resolve_working_dir(project_root: Path, override: str | None) -> Path:
    """Resolve the ``.tree`` working dir: explicit override → repo → ``/tmp``.

    ``override`` (the ``TREE_WORKING_DIR`` env var) always wins. Otherwise, if
    ``project_root`` is a source checkout (``src/tree`` exists under it) we write
    inside the repo; an installed package — where ``project_root`` is a read-only
    ``site-packages`` parent — falls back to :data:`_PROD_WORKING_DIR`.
    """

    if override:
        return Path(override)
    if (project_root / "src" / "tree").is_dir():
        return project_root / ".tree"
    return _PROD_WORKING_DIR


TREE_WORKING_DIR: Path = _resolve_working_dir(
    _PROJECT_ROOT, os.environ.get("TREE_WORKING_DIR")
)
"""Root directory for short-term working data (`.tree/`)."""

MEMORY_DIR: Path = TREE_WORKING_DIR / "memory"
"""Directory for deep-search session files (`.tree/memory/`)."""

GRAPHS_DIR: Path = TREE_WORKING_DIR / "graphs"
"""Directory for rendered knowledge-graph HTML files (`.tree/graphs/`)."""
