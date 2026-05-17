"""Mongot Atlas Vector Search must filter on ``user_id`` (#020).

Multi-tenant isolation depends on the index declaration carrying
``user_id`` as a filter path so ``$vectorSearch`` prunes other tenants'
rows server-side. The actual declaration lives in
``tree.memory.indexing.core._VECTOR_INDEX_FILTER_PATHS``; this test
locks in the contract so a future refactor cannot silently drop it.
"""

from __future__ import annotations

from tree.memory.indexing.core import (
    _VECTOR_INDEX_FILTER_PATHS,
    _build_vector_index_definition,
)


class TestVectorIndexFilterPaths:
    def test_user_id_is_first(self) -> None:
        # ``user_id`` must be the first filter path so every tenant-scoped
        # $vectorSearch hits the user_id-prefixed slice of the index.
        assert _VECTOR_INDEX_FILTER_PATHS[0] == "user_id"

    def test_definition_declares_user_id_as_filter(self) -> None:
        definition = _build_vector_index_definition(dimensions=8)
        filter_paths = {
            field["path"]
            for field in definition["fields"]
            if field.get("type") == "filter"
        }
        assert "user_id" in filter_paths
