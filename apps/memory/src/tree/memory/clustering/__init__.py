"""Clustering layer of the memory app: the **Embedding map**'s data (ADR-007 §7).

A NEUTRAL package, like ``visualize/``: it may import ``rag/`` types and
``entities``, but never ``graph/`` or the flow module ``pipeline.py``. Clustering
is mode-orthogonal — a **Clustering run** reads the same **Child chunk** rows and
writes the same ``memory_clusters`` rows in ``rag`` and in ``graphrag``.

``types.py`` holds the transit shapes and the LLM contract; ``core.py`` is the
pure numpy recipe (UMAP -> HDBSCAN -> a separate 2-D UMAP) plus member sampling.

Intentionally empty of re-exports: importing ``clustering.types`` must not drag
in ``core`` (and through it numpy), the way importing ``rag.cleaning`` drags in
nothing.
"""
