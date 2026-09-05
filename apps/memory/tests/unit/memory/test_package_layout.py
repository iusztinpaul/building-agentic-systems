"""Structural guards on the ``tree.memory`` package layout (ADR-006 §8).

The layout is not decoration: ``rag/`` is the complete Chapter-4 system a
reader must be able to read end to end without meeting the graph, and
``graph/`` is exactly what Chapter 8 adds. Nothing at run time enforces that —
one convenience import from ``rag/chunking.py`` into ``graph/`` would quietly
undo it and no test would go red. So the invariants are asserted here, on the
source tree itself:

1. the top level of ``tree/memory/`` holds only the flow module, the two
   shared helpers and the two layer packages;
2. no module under ``rag/`` imports ``tree.memory.graph`` or the flow module
   ``tree.memory.pipeline`` (``graph`` → ``rag`` is allowed, never the
   reverse);
3. every module under ``rag/`` and ``graph/`` has a mirroring test module.

The related stdlib-only purity of ``rag/cleaning.py`` (#106) is asserted where
the module is tested:
``tests/unit/memory/rag/test_cleaning.py::TestModulePurity``.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import tree.memory
from tree.memory import graph, rag

_MEMORY_DIR = Path(tree.memory.__file__).parent
_RAG_DIR = Path(rag.__file__).parent
_GRAPH_DIR = Path(graph.__file__).parent
_TESTS_DIR = Path(__file__).parent

# Packages have no module of their own to test, and ``types.py`` files are
# plain Pydantic transit models covered wherever they are produced.
_NOT_MIRRORED = {"__init__.py", "types.py"}


def _module_paths(package_dir: Path) -> list[Path]:
    """Every ``.py`` file of a package, recursively, sorted for stable ids."""

    return sorted(
        path for path in package_dir.rglob("*.py") if "__pycache__" not in path.parts
    )


def _imported_modules(path: Path) -> set[str]:
    """Every dotted module name imported by the file at ``path``."""

    module_ast = ast.parse(path.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(module_ast):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.add(node.module)
    return imported


def _mirror_candidates(package_dir: Path) -> list[Path]:
    """The modules that must have a mirroring ``test_<name>.py``."""

    return [
        path for path in _module_paths(package_dir) if path.name not in _NOT_MIRRORED
    ]


def _relative_id(path: Path) -> str:
    return str(path.relative_to(_MEMORY_DIR))


class TestTopLevelLayout:
    """ADR-006 §8: the final shape of ``tree/memory/``."""

    def test_top_level_holds_exactly_the_two_layers_and_three_modules(self) -> None:
        entries = {
            entry.name for entry in _MEMORY_DIR.iterdir() if entry.name != "__pycache__"
        }

        assert entries == {
            "__init__.py",
            "pipeline.py",
            "embedding_text.py",
            "types.py",
            "rag",
            "graph",
        }

    @pytest.mark.parametrize("retired", ["extraction", "indexing", "query"])
    def test_the_retired_packages_are_gone(self, retired: str) -> None:
        """Their contents moved into ``rag/`` (#108) and ``graph/`` (#111)."""

        assert not (_MEMORY_DIR / retired).exists()


class TestRagNeverDependsOnGraph:
    """``rag`` is Chapter 4 standalone: it may not reach up into the graph.

    Import direction is the one-way valve of the split. ``graph/retrieval.py``
    importing ``rag.search`` is by design; the reverse would mean a reader of
    ``rag/`` has to understand entities and edges to follow the code, and would
    make ``rag`` mode carry graph code it never runs.
    """

    @pytest.mark.parametrize("path", _module_paths(_RAG_DIR), ids=_relative_id)
    def test_no_rag_module_imports_the_graph_layer(self, path: Path) -> None:
        offenders = {
            name
            for name in _imported_modules(path)
            if name == "tree.memory.graph" or name.startswith("tree.memory.graph.")
        }

        assert not offenders, f"{_relative_id(path)} imports {sorted(offenders)}"

    @pytest.mark.parametrize("path", _module_paths(_RAG_DIR), ids=_relative_id)
    def test_no_rag_module_imports_the_flow_module(self, path: Path) -> None:
        """The stages are libraries; ``pipeline.py`` is their only caller.

        A stage importing the flow module would invert the dependency (and
        re-introduce the sharding↔pipeline cycle #108 had to break with a
        function-scope import).
        """

        offenders = {
            name
            for name in _imported_modules(path)
            if name == "tree.memory.pipeline"
            or name.startswith("tree.memory.pipeline.")
        }

        assert not offenders, f"{_relative_id(path)} imports {sorted(offenders)}"


class TestTestsMirrorTheModules:
    """Every moved module kept its test, under the mirroring path.

    A move that drops a test file is invisible in a green run — the suite just
    gets smaller. This makes the coverage gap fail instead.
    """

    @pytest.mark.parametrize(
        "path",
        _mirror_candidates(_RAG_DIR) + _mirror_candidates(_GRAPH_DIR),
        ids=_relative_id,
    )
    def test_module_has_a_mirroring_test_module(self, path: Path) -> None:
        relative = path.relative_to(_MEMORY_DIR)
        expected = _TESTS_DIR / relative.parent / f"test_{path.name}"

        assert expected.exists(), (
            f"{relative} has no test module; expected "
            f"{expected.relative_to(_TESTS_DIR.parents[2])}"
        )
