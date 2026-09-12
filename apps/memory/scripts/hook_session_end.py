"""Claude Code ``SessionEnd`` hook — persist the finished session over MCP.

Glue only: the logic lives in :mod:`tree.mcp.hooks` (a pure MCP client — ADR-008
§5). Claude Code pipes the ``SessionEnd`` JSON on stdin; this script reads it
ONCE, locates the repo-root ``.mcp.json`` and hands both to
:func:`tree.mcp.hooks.run`, which ALWAYS returns 0 so leaving a session can
never fail on memory.

Wired in the repo-root ``.claude/settings.json`` (``timeout: 60``, since
``SessionEnd`` hooks otherwise share a 1.5 s budget). Point it at the cloud
server by passing ``tree-memory`` instead — see ``apps/memory/README.md``.

Usage:
    echo '{"session_id":"abc","transcript_path":"/path/to/transcript.jsonl"}' \\
      | uv run python scripts/hook_session_end.py tree-memory-local
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import sys
from pathlib import Path

import click

from tree.logging import init_logger
from tree.mcp.hooks import run

init_logger()
logger = logging.getLogger(__name__)

# ``<repo>/apps/memory/scripts/hook_session_end.py`` → ``<repo>``.
_FALLBACK_REPO_ROOT = Path(__file__).resolve().parents[3]


def _repo_root(raw_stdin: str) -> Path:
    """The directory holding ``.mcp.json``: the hook's ``cwd``, else this repo.

    Claude Code reports the session's working directory, which may be a
    subdirectory (or another checkout); it only answers here when it actually
    carries a ``.mcp.json``.
    """

    try:
        cwd = json.loads(raw_stdin or "{}").get("cwd")
    # PEP 758 (3.14) parenthesis-free multi-type except, as ruff formats it.
    except json.JSONDecodeError, AttributeError:
        cwd = None
    if isinstance(cwd, str) and cwd and (Path(cwd) / ".mcp.json").is_file():
        return Path(cwd)
    return _FALLBACK_REPO_ROOT


@click.command()
@click.argument("server_name", default="tree-memory-local")
def main(server_name: str) -> None:
    """Ingest the just-ended session through the ``.mcp.json`` SERVER_NAME entry."""

    raw_stdin = sys.stdin.read()
    mcp_json = _repo_root(raw_stdin) / ".mcp.json"

    sys.exit(asyncio.run(run(io.StringIO(raw_stdin), mcp_json, server_name)))


if __name__ == "__main__":
    main()
