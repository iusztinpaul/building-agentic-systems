"""RAG layer of the memory app: everything Chapter 4 needs (ADR-006 §8).

Holds the Clean step, chunking, embedding, load, search, retrieval and
indexing. Never imports ``tree.memory.graph``. Intentionally empty of
re-exports so importing a submodule (e.g. the pure
:mod:`tree.memory.rag.cleaning`) pulls in nothing else.
"""
