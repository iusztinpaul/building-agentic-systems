"""The registered MCP tool set IS the **Memory mode** (ADR-006 decision 5, #110).

Every assertion here runs in a FRESH interpreter (the pattern of
``test_server_startup.py::test_entrypoint_loaded_by_path_registers_all_tools``):
``tree.mcp.server`` reads ``app_config.memory.mode`` ONCE at import and registers
tools as a side effect, so a same-process ``importlib.reload`` would assert
against whichever mode happened to import first. One subprocess per mode, cached,
answers every question about that mode's surface.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from functools import lru_cache
from typing import Any

import pytest

# Tools both modes serve — rag's ENTIRE surface (ADR-006 decision 5, ADR-007 §8).
_SHARED_TOOLS = [
    "ingest_conversation",
    "ingest_file",
    "ingest_url",
    "scrape_web",
    "search_memory",
    "search_web",
    "visualize_memory_embeddings",
]

# The seven tools that presuppose edges. Absent — not degraded — in rag mode.
_GRAPH_ONLY_TOOLS = [
    "deep_search_memory",
    "memory_dashboard",
    "query_memory",
    "review_confirm",
    "review_list_pending",
    "review_reject",
    "visualize_memory_graph",
]

# Modules that may only ever be imported by a graphrag server: the graph tool
# module plus the dashboard MCP App it pulls in as a side-effect import.
# ``tree.mcp.viz_app`` is deliberately NOT here — it is the MODE-NEUTRAL MCP App
# layer (the ``ui://`` / ``graphs://`` resources and the one dual-delivery
# helper), which rag mode is free to import (and now DOES, through
# ``visualize_memory_embeddings``).
_GRAPH_MODULES = [
    "tree.mcp.graph_tools",
    "tree.mcp.dashboard_app",
]

# The clustering recipe's heavy dependencies (ADR-007 §6): imported ONLY inside
# the function bodies that reduce and cluster, so no server — rag or graphrag —
# pays the ~40 s cold numba compile just to READ a stored **Embedding map**.
_LAZY_MODULES = ["umap", "sklearn"]

# The MODE-NEUTRAL MCP App layer every visualization tool delivers through.
_NEUTRAL_MODULE = "tree.mcp.viz_app"

_MARKER = "__PROBE__"

_PROBE = textwrap.dedent(
    f"""
    import asyncio, json, sys
    import tree.mcp.server as server


    async def probe():
        search_memory = await server.mcp.get_tool("search_memory")
        return {{
            "mode": server.MEMORY_MODE,
            "tools": sorted(t.name for t in await server.mcp.list_tools()),
            "search_memory_parameters": sorted(
                search_memory.parameters["properties"]
            ),
            "instructions": server.mcp.instructions,
            "graph_modules_imported": [
                m for m in {_GRAPH_MODULES!r} if m in sys.modules
            ],
            "lazy_modules_imported": [
                m for m in {_LAZY_MODULES!r} if m in sys.modules
            ],
            "neutral_module_imported": {_NEUTRAL_MODULE!r} in sys.modules,
            "embedding_map_parameters": sorted(
                (await server.mcp.get_tool("visualize_memory_embeddings"))
                .parameters["properties"]
            ),
        }}

    print("{_MARKER}" + json.dumps(asyncio.run(probe())))
    """
)


@lru_cache(maxsize=2)
def _probe(mode: str) -> dict[str, Any]:
    """Boot a server in ``mode`` in a fresh interpreter and report its surface."""

    result = subprocess.run(
        [sys.executable, "-c", _PROBE],
        capture_output=True,
        text=True,
        env={**os.environ, "TREE_MEMORY__MODE": mode},
    )
    assert result.returncode == 0, (
        f"importing tree.mcp.server in {mode} mode failed: {result.stderr}"
    )
    payload = next(
        line for line in result.stdout.splitlines() if line.startswith(_MARKER)
    )
    return json.loads(payload.removeprefix(_MARKER))


class TestRegisteredToolSet:
    def test_rag_mode_registers_only_the_seven_shared_tools(self) -> None:
        assert _probe("rag")["tools"] == _SHARED_TOOLS

    def test_graphrag_mode_adds_exactly_the_seven_graph_tools(self) -> None:
        tools = _probe("graphrag")["tools"]

        assert tools == sorted(_SHARED_TOOLS + _GRAPH_ONLY_TOOLS)
        assert len(tools) == 14

    def test_graph_tools_are_unknown_to_a_rag_server(self) -> None:
        # Story 2: the harness calling query_memory on a rag server gets the
        # standard unknown-tool error — there is no half-working path.
        registered = set(_probe("rag")["tools"])

        assert registered.isdisjoint(_GRAPH_ONLY_TOOLS)

    def test_rag_mode_never_imports_the_graph_modules(self) -> None:
        assert _probe("rag")["graph_modules_imported"] == []

    @pytest.mark.parametrize("mode", ["rag", "graphrag"])
    def test_no_mode_imports_the_clustering_stack_to_read_a_map(
        self, mode: str
    ) -> None:
        # ADR-007 §6: the surfaces READ stored coordinates. Importing umap here
        # would put the ~40 s cold numba compile on the MCP server's boot path.
        assert _probe(mode)["lazy_modules_imported"] == []

    @pytest.mark.parametrize("mode", ["rag", "graphrag"])
    def test_both_modes_import_the_neutral_mcp_app_layer(self, mode: str) -> None:
        # The Embedding map tool is registered in BOTH modes, so the ``ui://``
        # and ``graphs://`` resources it delivers through exist in both.
        assert _probe(mode)["neutral_module_imported"] is True

    def test_graphrag_mode_imports_the_graph_modules(self) -> None:
        assert _probe("graphrag")["graph_modules_imported"] == _GRAPH_MODULES


class TestEmbeddingMapToolSignature:
    """Two knobs, in both modes — the map takes no query and no mode branch."""

    @pytest.mark.parametrize("mode", ["rag", "graphrag"])
    def test_the_tool_advertises_exactly_hulls_and_as_html_file(
        self, mode: str
    ) -> None:
        assert _probe(mode)["embedding_map_parameters"] == ["as_html_file", "hulls"]


class TestSearchMemorySignaturePerMode:
    """One name, two signatures — never a parameter the mode cannot honour."""

    def test_rag_advertises_query_and_top_k_only(self) -> None:
        assert _probe("rag")["search_memory_parameters"] == ["query", "top_k"]

    def test_graphrag_keeps_its_five_graph_expansion_parameters(self) -> None:
        assert _probe("graphrag")["search_memory_parameters"] == [
            "max_hops",
            "max_results",
            "query",
            "top_k",
            "visualize",
        ]


class TestModeAwareInstructions:
    """The model must never be told about a tool the server did not register."""

    @pytest.mark.parametrize("tool", _GRAPH_ONLY_TOOLS)
    def test_rag_instructions_name_no_graph_tool(self, tool: str) -> None:
        assert tool not in _probe("rag")["instructions"]

    def test_rag_instructions_have_no_graph_vocabulary(self) -> None:
        assert "knowledge graph" not in _probe("rag")["instructions"].lower()

    @pytest.mark.parametrize("tool", _SHARED_TOOLS)
    def test_rag_instructions_name_every_tool_it_serves(self, tool: str) -> None:
        assert tool in _probe("rag")["instructions"]

    @pytest.mark.parametrize("mode", ["rag", "graphrag"])
    def test_both_modes_announce_the_embedding_map_tool(self, mode: str) -> None:
        # Clustering is mode-orthogonal: the map is one of the tools BOTH texts
        # must name, or a model in rag mode never learns it can draw one.
        assert "visualize_memory_embeddings" in _probe(mode)["instructions"]

    def test_graphrag_instructions_are_the_graph_text(self) -> None:
        instructions = _probe("graphrag")["instructions"]

        assert "knowledge graph" in instructions
        assert "'query_memory'" in instructions
        assert "'deep_search_memory'" in instructions

    def test_the_two_modes_advertise_different_instructions(self) -> None:
        assert _probe("rag")["instructions"] != _probe("graphrag")["instructions"]
