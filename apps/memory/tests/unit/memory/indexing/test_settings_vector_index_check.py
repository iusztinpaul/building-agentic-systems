"""Unit tests for :func:`tree.memory.indexing.core.assert_settings_match_live_vector_index`.

The helper is the hard-error gate between ``settings.embedding_dim`` and the
live mongot ``vector_index`` definition. On mismatch it raises
``RuntimeError`` whose message names both numbers; on match it returns
``None``. See ``tracker/016-pin-embedding-model-and-dim-in-settings.groomed.md``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from tree.memory.indexing.core import (
    _VECTOR_INDEX_NAME,
    assert_settings_match_live_vector_index,
)


# ---------------------------------------------------------------------------
# Async cursor helpers (mirror the pattern used in test_core.py).
# ---------------------------------------------------------------------------


class _AsyncCursorFromList:
    """Mock async cursor that iterates a list (async)."""

    def __init__(self, items: list[dict[str, Any]]) -> None:
        self._items = list(items)

    def __aiter__(self):
        async def _agen():
            for item in self._items:
                yield item

        return _agen()

    async def to_list(self) -> list[dict[str, Any]]:
        return list(self._items)


def _make_collection_with_indexes(indexes: list[dict[str, Any]]) -> MagicMock:
    """Build a mock collection whose ``list_search_indexes`` returns ``indexes``."""

    collection = MagicMock()

    async def _list_search(*args: Any, **kwargs: Any) -> _AsyncCursorFromList:
        return _AsyncCursorFromList(indexes)

    collection.list_search_indexes = _list_search
    return collection


def _wire_client(collection: MagicMock) -> MagicMock:
    db = MagicMock()
    db.__getitem__ = MagicMock(return_value=collection)
    client = MagicMock()
    client.__getitem__ = MagicMock(return_value=db)
    return client


def _vector_index_doc(num_dimensions: int) -> dict[str, Any]:
    return {
        "name": _VECTOR_INDEX_NAME,
        "latestDefinition": {
            "fields": [
                {
                    "type": "vector",
                    "path": "embedding",
                    "numDimensions": num_dimensions,
                    "similarity": "cosine",
                }
            ]
        },
    }


# ---------------------------------------------------------------------------
# Match / mismatch / missing cases.
# ---------------------------------------------------------------------------


class TestAssertSettingsMatchLiveVectorIndex:
    async def test_match_returns_none(self, mocker) -> None:
        # Arrange
        mocker.patch(
            "tree.memory.indexing.core.settings.embedding_dim",
            new=1024,
        )
        collection = _make_collection_with_indexes([_vector_index_doc(1024)])
        client = _wire_client(collection)

        # Act
        result = await assert_settings_match_live_vector_index(client, "test_db")

        # Assert
        assert result is None

    async def test_mismatch_raises_runtime_error_with_both_numbers(
        self, mocker
    ) -> None:
        # Arrange
        mocker.patch(
            "tree.memory.indexing.core.settings.embedding_dim",
            new=1024,
        )
        collection = _make_collection_with_indexes([_vector_index_doc(384)])
        client = _wire_client(collection)

        # Act + Assert
        with pytest.raises(RuntimeError) as exc_info:
            await assert_settings_match_live_vector_index(client, "test_db")

        message = str(exc_info.value)
        assert "1024" in message
        assert "384" in message

    async def test_missing_vector_index_raises_runtime_error(self, mocker) -> None:
        # Arrange
        mocker.patch(
            "tree.memory.indexing.core.settings.embedding_dim",
            new=1024,
        )
        # No vector_index entry in the live result.
        collection = _make_collection_with_indexes(
            [{"name": "some_other_index", "latestDefinition": {"fields": []}}]
        )
        client = _wire_client(collection)

        # Act + Assert
        with pytest.raises(RuntimeError) as exc_info:
            await assert_settings_match_live_vector_index(client, "test_db")

        assert "vector_index not found" in str(exc_info.value)

    async def test_mismatch_message_names_settings_field(self, mocker) -> None:
        # Arrange
        mocker.patch(
            "tree.memory.indexing.core.settings.embedding_dim",
            new=768,
        )
        collection = _make_collection_with_indexes([_vector_index_doc(1024)])
        client = _wire_client(collection)

        # Act + Assert
        with pytest.raises(RuntimeError) as exc_info:
            await assert_settings_match_live_vector_index(client, "test_db")

        message = str(exc_info.value)
        assert "embedding_dim" in message
        assert "numDimensions" in message
