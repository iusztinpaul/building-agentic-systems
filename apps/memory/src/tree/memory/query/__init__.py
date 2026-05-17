"""Query module for the unified memory.

Public surface:

* :class:`KGQuery` — tenant-locked reader for ``knowledge_graph``. Every
  read in production code goes through this class; a CI grep enforces
  the rule.
* :func:`search_nodes`, :func:`expand_graph`, :func:`query_memory` — the
  hybrid search + graph-expansion pipeline used by the MCP query tools.
* :func:`execute_nl_query` — NL → MongoDB aggregation pipeline executor.
"""

from tree.memory.query.kgquery import KGQuery

__all__ = ["KGQuery"]
