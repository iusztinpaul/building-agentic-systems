"""
Project-level path constants.

``.twin/`` is the working directory for ephemeral/session data that
should not be committed to version control (added to ``.gitignore``).
"""

from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[3]

TWIN_WORKING_DIR: Path = _PROJECT_ROOT / ".twin"
"""Root directory for short-term working data (`.twin/`)."""

MEMORY_DIR: Path = TWIN_WORKING_DIR / "memory"
"""Directory for deep-search session files (`.twin/memory/`)."""
