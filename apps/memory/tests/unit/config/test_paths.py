"""Unit tests for the ``.tree`` working-dir resolution in ``tree.config.paths``.

The location comes from the ``TREE_WORKING_DIR`` setting (``.env`` / ``.env.prod``):
unset → the in-repo project ``.tree``; set → that path verbatim. Prod/serverless
hosts whose install dir is read-only (Prefect Horizon) set
``TREE_WORKING_DIR=/tmp/.tree`` — which is what fixes the read-only crash there.
"""

from pathlib import Path

from tree.config.paths import _resolve_working_dir


class TestResolveWorkingDir:
    def test_unset_defaults_to_project_root_dot_tree(self, tmp_path):
        # Empty TREE_WORKING_DIR → write inside the project (local dev default).
        assert _resolve_working_dir(tmp_path, "") == tmp_path / ".tree"

    def test_explicit_value_is_used_verbatim(self, tmp_path):
        # Prod sets TREE_WORKING_DIR=/tmp/.tree → used as-is, ignoring the project.
        assert _resolve_working_dir(tmp_path, "/tmp/.tree") == Path("/tmp/.tree")
