"""Unit tests for the ``.tree`` working-dir resolution in ``tree.config.paths``.

The cloud MCP server crashed rendering graphs with
``[Errno 30] Read-only file system: '/usr/local/lib/python3.14/.tree'`` — the old
code rooted ``.tree`` at ``Path(__file__).parents[3]``, which is the repo locally
but a read-only ``site-packages`` parent once installed. ``_resolve_working_dir``
fixes that: repo → in-repo ``.tree``; installed → ``/tmp/.tree``; env override wins.
"""

from pathlib import Path

from tree.config.paths import _PROD_WORKING_DIR, _resolve_working_dir


class TestResolveWorkingDir:
    def test_source_checkout_uses_in_repo_dot_tree(self, tmp_path):
        # A ``src/tree`` under the root marks a source checkout (local dev).
        (tmp_path / "src" / "tree").mkdir(parents=True)

        assert _resolve_working_dir(tmp_path, None) == tmp_path / ".tree"

    def test_installed_package_falls_back_to_tmp(self, tmp_path):
        # No ``src/tree`` (installed in site-packages) → writable ``/tmp/.tree``.
        assert _resolve_working_dir(tmp_path, None) == _PROD_WORKING_DIR

    def test_explicit_override_wins_over_repo_layout(self, tmp_path):
        (tmp_path / "src" / "tree").mkdir(parents=True)

        assert _resolve_working_dir(tmp_path, "/custom/dir") == Path("/custom/dir")

    def test_empty_override_is_ignored(self, tmp_path):
        # Empty string is falsy → treated as unset, so the repo path is used.
        (tmp_path / "src" / "tree").mkdir(parents=True)

        assert _resolve_working_dir(tmp_path, "") == tmp_path / ".tree"
