"""Query module for the unified memory.

Public surface:

* :class:`KGQuery` — tenant-locked reader for ``memory``. Every
  read in production code goes through this class; a CI grep enforces
  the rule.
* :func:`execute_nl_query` — NL → MongoDB aggregation pipeline executor.

The hybrid search + graph-expansion pipeline moved out under ADR-006: seeds live
in :mod:`tree.memory.rag.search`, parent-document retrieval in
:mod:`tree.memory.rag.retrieval`, expansion and composition in
:mod:`tree.memory.graph.retrieval`.
"""

from tree.memory.query.kgquery import KGQuery

__all__ = ["KGQuery"]
