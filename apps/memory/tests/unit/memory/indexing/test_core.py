from unittest.mock import AsyncMock, MagicMock

from twin.memory.indexing.core import _node_to_text, ensure_indexes


class _AsyncCursorEmpty:
    """Mock async cursor that yields nothing."""

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise StopAsyncIteration

    async def to_list(self):
        return []


class _AsyncCursorWithItem:
    """Mock async cursor that returns one item from to_list."""

    def __init__(self, item: dict):
        self._item = item

    async def to_list(self):
        return [self._item]


class TestEnsureIndexes:
    async def test_creates_compound_indexes(self):
        collection = AsyncMock()

        # First call (no args): initial scan — empty iterator.
        # Subsequent calls (with name): wait loop — return a dummy result
        # so the loop exits immediately.
        call_count = 0

        async def _list_search(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _AsyncCursorEmpty()
            return _AsyncCursorWithItem({"name": "vector_index"})

        collection.list_search_indexes = _list_search
        collection.create_search_index = AsyncMock()

        db = MagicMock()
        db.__getitem__ = MagicMock(return_value=collection)
        client = MagicMock()
        client.__getitem__ = MagicMock(return_value=db)

        await ensure_indexes(client, "test_db")

        index_calls = collection.create_index.call_args_list
        created_names = {call.kwargs.get("name") for call in index_calls}

        assert "kind_source_node" in created_names
        assert "kind_target_node" in created_names
        assert "kind_embedding" in created_names

    async def test_vector_index_includes_filter_fields(self):
        """When vector index does not exist, the created definition must
        include 'kind' and 'type' as filter fields."""
        collection = AsyncMock()

        call_count = 0

        async def _list_search(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _AsyncCursorEmpty()
            return _AsyncCursorWithItem({"name": "vector_index"})

        collection.list_search_indexes = _list_search

        created_model = {}

        async def _capture_create(model):
            created_model.update(model)

        collection.create_search_index = _capture_create

        db = MagicMock()
        db.__getitem__ = MagicMock(return_value=collection)
        client = MagicMock()
        client.__getitem__ = MagicMock(return_value=db)

        await ensure_indexes(client, "test_db")

        fields = created_model.get("definition", {}).get("fields", [])
        filter_paths = {f["path"] for f in fields if f.get("type") == "filter"}
        assert "kind" in filter_paths
        assert "type" in filter_paths


class TestNodeToText:
    def test_basic_node(self):
        node = {
            "_id": "person:alice",
            "type": "person",
            "properties": {"aliases": ["ali"]},
        }
        text = _node_to_text(node)
        assert "person: person:alice" in text
        assert "aliases" in text

    def test_node_with_content(self):
        node = {
            "_id": "chunk:chunk-0",
            "type": "chunk",
            "properties": {"content": "Hello world", "source_type": "substack"},
        }
        text = _node_to_text(node)
        # Content should appear last.
        assert text.endswith("Hello world")
        assert "source_type" in text

    def test_empty_properties(self):
        node = {"_id": "task:test", "type": "task", "properties": {}}
        text = _node_to_text(node)
        assert "task: task:test" in text

    def test_missing_fields(self):
        text = _node_to_text({})
        assert ": " in text
