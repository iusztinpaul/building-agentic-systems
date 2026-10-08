"""Both mongot indexes must filter on ``user_id`` (#020, ADR-015 §6).

Multi-tenant isolation depends on the index declarations carrying
``user_id`` as a filter path so ``$vectorSearch`` / ``$search`` prune other
tenants' rows server-side. The declarations live in
``tree.memory.rag.indexing._VECTOR_INDEX_FILTER_PATHS`` and
``TEXT_INDEX_FILTER_PATHS``; these tests lock in the contract so a future
refactor cannot silently drop it.
"""

from __future__ import annotations

from tree.memory.rag.indexing import (
    TEXT_INDEX_FILTER_PATHS,
    TEXT_INDEX_TEXT_PATHS,
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


class TestTextIndexPaths:
    def test_filter_paths_are_user_id_first_with_their_atlas_types(self) -> None:
        # Insertion order IS the declaration order: user_id first.
        assert list(TEXT_INDEX_FILTER_PATHS.items()) == [
            ("user_id", "objectId"),
            ("kind", "token"),
            ("type", "token"),
            ("subtype", "token"),
        ]

    def test_merged_into_is_not_a_filter_path(self) -> None:
        # ADR-015 §6: no text-leg reader filters tombstones.
        assert "merged_into" not in TEXT_INDEX_FILTER_PATHS

    def test_text_paths_are_the_four_lexical_fields(self) -> None:
        assert TEXT_INDEX_TEXT_PATHS == (
            "name",
            "aliases",
            "properties.content",
            "properties.aliases",
        )
