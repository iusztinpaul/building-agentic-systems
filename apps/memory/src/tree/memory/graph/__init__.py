"""Graph layer of the memory app: everything Chapter 8 adds on top of ``rag``.

ADR-006 §8. The write path — LLM extraction (:mod:`~tree.memory.graph.extraction`),
``add_entity``, ``dedup``, ``validation``, ``judge``, ``first_person_resolver``,
``preference_supersession``, ``sharding``, ``resolution/``, ``review/``,
``consolidation/`` — plus the graph read surfaces (``retrieval``, ``kgquery``,
``nl_query``, ``visualize``).

It MAY import :mod:`tree.memory.rag` (graph retrieval reuses the rag hybrid
search and the parent grouping) — never the other way round; the direction is
asserted by ``tests/unit/memory/test_package_layout.py``. Intentionally empty
of re-exports so a submodule import pulls in nothing else.
"""
