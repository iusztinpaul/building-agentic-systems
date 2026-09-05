"""Graph layer of the memory app: everything Chapter 8 adds on top of ``rag``.

ADR-006 §8. Today it holds :mod:`tree.memory.graph.retrieval` (expansion +
mode-aware query composition); extraction, resolution, review and the graph
query/visualisation surfaces move here in #111. It MAY import
:mod:`tree.memory.rag` (graph retrieval reuses the rag hybrid search and the
parent grouping) — never the other way round. Intentionally empty of
re-exports so a submodule import pulls in nothing else.
"""
