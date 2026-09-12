"""Rendering layer of the memory app: the **Graph renderer** (ADR-005, ADR-007 §7).

A NEUTRAL package, like ``clustering/``: it may import ``rag/`` types and
``entities``, but never ``graph/``, the flow module ``pipeline.py`` or
``clustering.store`` (its Mongo reads). It draws whatever payload it is HANDED
— ``graph/`` and the MCP layer may import it, it imports neither.

``graph.py`` is the renderer itself: the **Graph payload** builder, the one
graphology + Sigma.js + ForceAtlas2 HTML template shared by every surface, and
the self-contained-file writer. ``embeddings.py`` builds the **Embedding map**
payload the same template renders with fixed coordinates.

Intentionally empty of re-exports so importing ``visualize.embeddings`` pulls in
nothing it does not use.
"""
