"""The clustering stack is INSTALLED but never imported (ADR-007 §6, #115).

``umap-learn`` and ``scikit-learn`` are MAIN dependencies — the Prefect Managed
per-run ``pip install ./apps/memory`` must be able to run the clustering phase
without a second install step. They are also expensive: the first ``import
umap`` on a machine compiles numba kernels (~40 s; ~3 s warm). So every path
that does NOT cluster — the memory pipeline flow module, the MCP server in both
**Memory modes**, the mother ``offline_pipeline`` — must leave them out of
``sys.modules``.

#115 adds no imports at all; #116/#117 add them INSIDE the two function bodies
that need them. This module is the guard that keeps that true: it boots each
entry point in a FRESH interpreter (a same-process import would see whatever an
earlier test already loaded) and reports what landed in ``sys.modules``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import tomllib
from functools import lru_cache
from importlib.metadata import version
from pathlib import Path

import pytest

_PYPROJECT_PATH = Path(__file__).resolve().parents[2] / "pyproject.toml"
_PYPROJECT = tomllib.loads(_PYPROJECT_PATH.read_text(encoding="utf-8"))

# The clustering stack: the two declared dependencies plus the two transitive
# heavyweights ``umap`` pulls in (numba compiles its kernels, pynndescent builds
# its k-NN graph). All four are equally disqualifying in an import-free path.
_FORBIDDEN_MODULES = ["umap", "sklearn", "numba", "pynndescent"]

_DECLARED_DEPENDENCIES = {
    # "umap-learn>=0.5.12" -> "umap-learn"
    requirement.split(">")[0].split("=")[0].split("[")[0].strip(): requirement
    for requirement in _PYPROJECT["project"]["dependencies"]
}

_MARKER = "__PROBE__"

_PROBE = textwrap.dedent(
    f"""
    import importlib, json, sys

    importlib.import_module(sys.argv[1])
    print("{_MARKER}" + json.dumps(
        [m for m in {_FORBIDDEN_MODULES!r} if m in sys.modules]
    ))
    """
)


@lru_cache(maxsize=8)
def _forbidden_modules_after_importing(module: str, mode: str) -> tuple[str, ...]:
    """Import ``module`` in a fresh interpreter; report the stack it dragged in."""

    result = subprocess.run(
        [sys.executable, "-c", _PROBE, module],
        capture_output=True,
        text=True,
        env={**os.environ, "TREE_MEMORY__MODE": mode},
    )
    assert result.returncode == 0, (
        f"importing {module} in {mode} mode failed: {result.stderr}"
    )
    payload = next(
        line for line in result.stdout.splitlines() if line.startswith(_MARKER)
    )
    return tuple(json.loads(payload.removeprefix(_MARKER)))


class TestDeclaredDependencies:
    @pytest.mark.parametrize("distribution", ["umap-learn", "scikit-learn"])
    def test_is_a_main_dependency(self, distribution: str) -> None:
        """Main, not an optional extra: Prefect Managed installs the package
        with a plain ``pip install`` and must get the clustering stack with it."""

        assert distribution in _DECLARED_DEPENDENCIES

        extras = _PYPROJECT["project"].get("optional-dependencies", {})
        for extra_requirements in extras.values():
            assert not any(
                requirement.startswith(distribution)
                for requirement in extra_requirements
            )

    @pytest.mark.parametrize(
        "distribution,floor",
        [("umap-learn", (0, 5, 12)), ("scikit-learn", (1, 9))],
    )
    def test_declares_and_installs_at_least_the_verified_floor(
        self, distribution: str, floor: tuple[int, ...]
    ) -> None:
        """The floors ADR-007 §6 verified against Python 3.14 / numpy 2.4.2."""

        expected = ">=" + ".".join(str(part) for part in floor)
        assert _DECLARED_DEPENDENCIES[distribution] == f"{distribution}{expected}"

        installed = tuple(
            int(part) for part in version(distribution).split(".")[: len(floor)]
        )
        assert installed >= floor

    def test_slow_marker_is_registered(self, pytestconfig: pytest.Config) -> None:
        """#116's real-UMAP test is ``slow``-marked; an unregistered marker is a
        warning today and a typo that silently selects nothing tomorrow."""

        markers = list(pytestconfig.getini("markers"))

        assert any(marker.startswith("slow:") for marker in markers)


_SKIP_PATH_PROBE = textwrap.dedent(
    f"""
    import asyncio, json, sys
    from unittest.mock import AsyncMock, MagicMock

    from beanie import PydanticObjectId

    import tree.memory.pipeline as pipeline
    from tree.memory.clustering.store import ChildEmbeddingRow

    # A corpus of 10 embedded children, below the default min_cluster_size 15.
    rows = [
        ChildEmbeddingRow(chunk_id=f"c{{i}}", embedding=[0.1, 0.2, 0.3, 0.4])
        for i in range(10)
    ]
    pipeline.init_mongodb = AsyncMock(return_value=MagicMock())
    pipeline.load_child_embeddings_task = AsyncMock(return_value=rows)

    # ``.fn`` runs the flow BODY: the claim is about imports, and a real flow
    # run would need a Prefect API in this fresh interpreter.
    stats = asyncio.run(pipeline.memory_clustering.fn(user_id=PydanticObjectId()))
    print("{_MARKER}" + json.dumps({{
        "skipped_reason": stats.skipped_reason,
        "loaded": [m for m in {_FORBIDDEN_MODULES!r} if m in sys.modules],
    }}))
    """
)


@lru_cache(maxsize=1)
def _skip_path_probe() -> tuple[str, tuple[str, ...]]:
    """Run the flow body over 10 child chunks in a FRESH interpreter.

    Returns ``(skipped_reason, modules_of_the_stack_that_got_loaded)``.
    """

    result = subprocess.run(
        [sys.executable, "-c", _SKIP_PATH_PROBE],
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "TREE_MEMORY__CLUSTERING__HDBSCAN__MIN_CLUSTER_SIZE": "15",
        },
    )
    assert result.returncode == 0, f"the skip-path probe failed: {result.stderr}"
    payload = next(
        line for line in result.stdout.splitlines() if line.startswith(_MARKER)
    )
    parsed = json.loads(payload.removeprefix(_MARKER))
    return parsed["skipped_reason"], tuple(parsed["loaded"])


class TestTheSkipPathImportsNothing:
    """A corpus too small to cluster must cost MILLISECONDS, not a numba compile.

    ADR-007 §5 puts the guard in the flow (before ``reduce-and-cluster``) and
    ADR-007 §1 repeats it inside ``core.reduce_and_cluster``. This asserts the
    OUTER one really short-circuits: a fresh interpreter runs the whole flow
    body over 10 child chunks and must come back with the skip reason AND an
    unloaded clustering stack. In-process this would be vacuous — the
    ``slow``-marked test elsewhere in the suite may already have imported umap.
    """

    def test_the_run_is_skipped_with_both_numbers(self) -> None:
        assert _skip_path_probe()[0] == "10 child embeddings < min_cluster_size 15"

    def test_no_module_of_the_clustering_stack_was_loaded(self) -> None:
        assert _skip_path_probe()[1] == ()


class TestNothingImportsTheClusteringStack:
    """ADR-007 §6: lazy imports, asserted from the outside.

    Each case boots ONE entry point in a fresh interpreter. A regression here
    means someone moved an ``import umap`` / ``from sklearn...`` to module
    scope, and every nightly run, MCP boot and CLI start just got ~3 s slower
    (~40 s on a fresh container).
    """

    @pytest.mark.parametrize(
        "module,mode",
        [
            ("tree.memory.pipeline", "graphrag"),
            ("tree.offline", "graphrag"),
            ("tree.mcp.server", "rag"),
            ("tree.mcp.server", "graphrag"),
            # The clustering module itself: even the code that CALLS umap must
            # be importable for free, so #117's flow can import it at module
            # level and still leave the stack unloaded when the phase is off.
            ("tree.memory.clustering.core", "graphrag"),
        ],
        ids=[
            "memory-pipeline",
            "offline-pipeline",
            "mcp-server-rag",
            "mcp-server-graphrag",
            "clustering-core",
        ],
    )
    def test_entry_point_leaves_the_clustering_stack_unimported(
        self, module: str, mode: str
    ) -> None:
        assert _forbidden_modules_after_importing(module, mode) == ()
