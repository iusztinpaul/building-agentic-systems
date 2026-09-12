"""Structural guards on the ``tree.memory`` package layout (ADR-006 §8, ADR-007 §7).

The layout is not decoration: ``rag/`` is the complete Chapter-4 system a
reader must be able to read end to end without meeting the graph, and
``graph/`` is exactly what Chapter 8 adds. Nothing at run time enforces that —
one convenience import from ``rag/chunking.py`` into ``graph/`` would quietly
undo it and no test would go red. So the invariants are asserted here, on the
source tree itself:

1. the top level of ``tree/memory/`` holds only the flow module, the two
   shared helpers and the layer packages;
2. no module under ``rag/`` imports ``tree.memory.graph`` or the flow module
   ``tree.memory.pipeline`` (``graph`` → ``rag`` is allowed, never the
   reverse);
3. ``clustering/`` and ``visualize/`` are NEUTRAL: they import neither
   ``graph/`` nor the flow module, and ``rag/`` does not import them either.
   ``visualize/`` additionally stays off ``clustering.store`` — it renders the
   **Embedding map** it is HANDED and never reads Mongo itself (ADR-007 §7, §8);
4. nothing under ``tree/memory/`` imports the clustering stack (``umap``,
   ``sklearn``, ``numba``, ``pynndescent``) at MODULE level — function-scope
   imports are the only allowed form (ADR-007 §6);
5. every module under ``rag/``, ``graph/``, ``clustering/`` and ``visualize/``
   has a mirroring test module.

The related stdlib-only purity of ``rag/cleaning.py`` (#106) is asserted where
the module is tested:
``tests/unit/memory/rag/test_cleaning.py::TestModulePurity``. The runtime half
of guard 4 — that a fresh interpreter importing an entry point really leaves
those modules out of ``sys.modules`` — lives in
``tests/unit/test_clustering_dependencies.py``.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import tree.memory
from tree.memory import clustering, graph, rag, visualize

_MEMORY_DIR = Path(tree.memory.__file__).parent
_RAG_DIR = Path(rag.__file__).parent
_GRAPH_DIR = Path(graph.__file__).parent
_CLUSTERING_DIR = Path(clustering.__file__).parent
_VISUALIZE_DIR = Path(visualize.__file__).parent
_TESTS_DIR = Path(__file__).parent

# The stack ADR-007 §6 keeps out of every import-time path: the two declared
# dependencies plus the two transitive heavyweights ``umap`` pulls in.
_HEAVY_ROOTS = ("umap", "sklearn", "numba", "pynndescent")

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


def _module_level_imports(path: Path) -> set[str]:
    """The dotted names imported when the file is IMPORTED.

    Function bodies are skipped: a function-scope import costs nothing until the
    function runs, which is exactly the form ADR-007 §6 requires for the
    clustering stack. Everything else — module scope, class bodies, ``try`` and
    ``if TYPE_CHECKING`` blocks — executes on import and counts.
    """

    imported: set[str] = set()

    def visit(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            if isinstance(child, ast.Import):
                imported.update(alias.name for alias in child.names)
            elif isinstance(child, ast.ImportFrom) and child.module is not None:
                imported.add(child.module)
            visit(child)

    visit(ast.parse(path.read_text(encoding="utf-8")))
    return imported


def _imports_of(imported: set[str], *roots: str) -> set[str]:
    """The subset of ``imported`` that is (or lives under) one of ``roots``."""

    return {
        name
        for name in imported
        if any(name == root or name.startswith(f"{root}.") for root in roots)
    }


def _mirror_candidates(package_dir: Path) -> list[Path]:
    """The modules that must have a mirroring ``test_<name>.py``."""

    return [
        path for path in _module_paths(package_dir) if path.name not in _NOT_MIRRORED
    ]


def _relative_id(path: Path) -> str:
    return str(path.relative_to(_MEMORY_DIR))


class TestTopLevelLayout:
    """ADR-006 §8 + ADR-007 §7: the final shape of ``tree/memory/``."""

    def test_top_level_holds_exactly_the_layers_and_three_modules(self) -> None:
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
            "clustering",
            "visualize",
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


class TestClusteringNeverDependsOnGraph:
    """``clustering/`` is NEUTRAL — it is the same code in both **Memory mode**s.

    A **Clustering run** reads child-chunk embeddings and writes
    ``memory_clusters`` rows; it has nothing to do with entities and edges. One
    import of ``graph/`` here would make the phase graphrag-flavoured and stop
    ``rag`` mode from drawing its own map (ADR-007 §7). The reverse guard —
    ``rag/`` importing ``clustering/`` — matters just as much: it would drag the
    UMAP stack into the Chapter-4 read path.
    """

    @pytest.mark.parametrize("path", _module_paths(_CLUSTERING_DIR), ids=_relative_id)
    def test_no_clustering_module_imports_the_graph_layer(self, path: Path) -> None:
        offenders = _imports_of(_imported_modules(path), "tree.memory.graph")

        assert not offenders, f"{_relative_id(path)} imports {sorted(offenders)}"

    @pytest.mark.parametrize("path", _module_paths(_CLUSTERING_DIR), ids=_relative_id)
    def test_no_clustering_module_imports_the_flow_module(self, path: Path) -> None:
        """The flow (#117) imports the library, never the other way round."""

        offenders = _imports_of(_imported_modules(path), "tree.memory.pipeline")

        assert not offenders, f"{_relative_id(path)} imports {sorted(offenders)}"

    @pytest.mark.parametrize("path", _module_paths(_RAG_DIR), ids=_relative_id)
    def test_no_rag_module_imports_the_clustering_layer(self, path: Path) -> None:
        offenders = _imports_of(_imported_modules(path), "tree.memory.clustering")

        assert not offenders, f"{_relative_id(path)} imports {sorted(offenders)}"


class TestVisualizeNeverDependsOnGraphOrStorage:
    """``visualize/`` is NEUTRAL — the renderer draws what it is HANDED.

    The same template draws a **Graph payload** and an **Embedding map**, so a
    single import of ``graph/`` here would make the drawing code graphrag-only
    and stop ``rag`` mode from rendering its own map (ADR-007 §7). The
    ``clustering.store`` guard is the same rule one layer down: surfaces READ
    the map and pass it in (ADR-007 §8) — a renderer that queried Mongo itself
    would drag pymongo (and a tenant scope) into the HTML writer, and could no
    longer be tested with a plain Pydantic object.
    """

    @pytest.mark.parametrize("path", _module_paths(_VISUALIZE_DIR), ids=_relative_id)
    def test_no_visualize_module_imports_the_graph_layer(self, path: Path) -> None:
        offenders = _imports_of(_imported_modules(path), "tree.memory.graph")

        assert not offenders, f"{_relative_id(path)} imports {sorted(offenders)}"

    @pytest.mark.parametrize("path", _module_paths(_VISUALIZE_DIR), ids=_relative_id)
    def test_no_visualize_module_imports_the_flow_module(self, path: Path) -> None:
        offenders = _imports_of(_imported_modules(path), "tree.memory.pipeline")

        assert not offenders, f"{_relative_id(path)} imports {sorted(offenders)}"

    @pytest.mark.parametrize("path", _module_paths(_VISUALIZE_DIR), ids=_relative_id)
    def test_no_visualize_module_imports_the_clustering_store(self, path: Path) -> None:
        offenders = _imports_of(_imported_modules(path), "tree.memory.clustering.store")

        assert not offenders, f"{_relative_id(path)} imports {sorted(offenders)}"

    @pytest.mark.parametrize("path", _module_paths(_RAG_DIR), ids=_relative_id)
    def test_no_rag_module_imports_the_visualize_layer(self, path: Path) -> None:
        offenders = _imports_of(_imported_modules(path), "tree.memory.visualize")

        assert not offenders, f"{_relative_id(path)} imports {sorted(offenders)}"

    @pytest.mark.parametrize("path", _module_paths(_VISUALIZE_DIR), ids=_relative_id)
    def test_no_visualize_module_imports_the_mcp_layer(self, path: Path) -> None:
        """The dependency direction is memory ← mcp, and only that way.

        ``tree.mcp.viz_app`` imports the renderer; the renderer importing back
        would make a cycle and drag FastMCP into the CLI's render path.
        """

        offenders = _imports_of(_imported_modules(path), "tree.mcp")

        assert not offenders, f"{_relative_id(path)} imports {sorted(offenders)}"

    @pytest.mark.parametrize("path", _module_paths(_VISUALIZE_DIR), ids=_relative_id)
    def test_no_visualize_module_imports_a_mongo_driver(self, path: Path) -> None:
        """Surfaces READ, renderers DRAW (ADR-007 §8).

        A renderer that could query Mongo would need a tenant scope and a
        client to be testable at all; as it stands every test here hands it a
        plain Pydantic object.
        """

        offenders = _imports_of(_imported_modules(path), "pymongo", "beanie", "motor")

        assert not offenders, f"{_relative_id(path)} imports {sorted(offenders)}"


class TestHeavyImportsAreLazy:
    """ADR-007 §6: ``umap`` / ``sklearn`` are function-scope imports, always.

    A single module-level ``import umap`` anywhere under ``tree/memory/`` makes
    every nightly run, MCP boot and CLI start pay ~3 s (~40 s on a fresh machine,
    where numba compiles umap's kernels) for a phase that is off by default. The
    cost is invisible in a green test run, so it is asserted from the source.
    """

    @pytest.mark.parametrize("path", _module_paths(_MEMORY_DIR), ids=_relative_id)
    def test_no_module_imports_the_clustering_stack_at_module_level(
        self, path: Path
    ) -> None:
        offenders = _imports_of(_module_level_imports(path), *_HEAVY_ROOTS)

        assert not offenders, (
            f"{_relative_id(path)} imports {sorted(offenders)} at module level; "
            "move it inside the function that uses it (ADR-007 §6)"
        )

    def test_the_guard_sees_a_module_level_import(self, tmp_path: Path) -> None:
        """The guard's own red case, so a refactor cannot leave it always green."""

        offender = tmp_path / "offender.py"
        offender.write_text("import umap\n", encoding="utf-8")

        assert _imports_of(_module_level_imports(offender), *_HEAVY_ROOTS) == {"umap"}

    def test_the_guard_ignores_a_function_scope_import(self, tmp_path: Path) -> None:
        allowed = tmp_path / "allowed.py"
        allowed.write_text(
            "def reduce():\n    import umap\n    return umap\n", encoding="utf-8"
        )

        assert _imports_of(_module_level_imports(allowed), *_HEAVY_ROOTS) == set()


class TestTestsMirrorTheModules:
    """Every moved module kept its test, under the mirroring path.

    A move that drops a test file is invisible in a green run — the suite just
    gets smaller. This makes the coverage gap fail instead.
    """

    @pytest.mark.parametrize(
        "path",
        _mirror_candidates(_RAG_DIR)
        + _mirror_candidates(_GRAPH_DIR)
        + _mirror_candidates(_CLUSTERING_DIR)
        + _mirror_candidates(_VISUALIZE_DIR),
        ids=_relative_id,
    )
    def test_module_has_a_mirroring_test_module(self, path: Path) -> None:
        relative = path.relative_to(_MEMORY_DIR)
        expected = _TESTS_DIR / relative.parent / f"test_{path.name}"

        assert expected.exists(), (
            f"{relative} has no test module; expected "
            f"{expected.relative_to(_TESTS_DIR.parents[2])}"
        )
