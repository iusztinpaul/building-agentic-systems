from unittest.mock import AsyncMock, MagicMock

import pytest

from tree.memory.indexing.core import (
    _build_vector_index_definition,
    _CANONICAL_NAME_INDEX,
    _TEXT_INDEX_FIELDS,
    _TEXT_INDEX_NAME,
    _VECTOR_INDEX_FILTER_PATHS,
    _VECTOR_INDEX_NAME,
    _node_to_text,
    embed_nodes,
    ensure_indexes,
)
from tree.models.base import BaseEmbeddingModel
from tree.models.fake_model import FakeEmbeddingModel


# ---------------------------------------------------------------------------
# Cursor helpers
# ---------------------------------------------------------------------------


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


class _AsyncCursorFromList:
    """Mock async cursor that iterates a list (async)."""

    def __init__(self, items: list[dict]):
        self._items = list(items)

    def __aiter__(self):
        async def _agen():
            for item in self._items:
                yield item

        return _agen()

    async def to_list(self):
        return list(self._items)


def _make_collection(
    *,
    initial_indexes: list[dict] | None = None,
) -> MagicMock:
    """Build a mock collection with the wait-loop hooks satisfied.

    The first call to ``list_search_indexes()`` (without a name) returns
    the supplied ``initial_indexes`` so the reconcile logic sees the
    desired starting state; subsequent calls (with the index name) return
    a non-empty result so the wait-loop in ``_ensure_vector_index`` exits
    immediately.
    """

    initial = initial_indexes or []
    collection = AsyncMock()
    call_count = 0

    async def _list_search(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _AsyncCursorFromList(initial)
        return _AsyncCursorWithItem({"name": _VECTOR_INDEX_NAME})

    collection.list_search_indexes = _list_search
    collection.create_search_index = AsyncMock()
    collection.drop_search_index = AsyncMock()
    return collection


def _wire_client(collection: MagicMock) -> MagicMock:
    db = MagicMock()
    db.__getitem__ = MagicMock(return_value=collection)
    client = MagicMock()
    client.__getitem__ = MagicMock(return_value=db)
    return client


# ---------------------------------------------------------------------------
# ensure_indexes — index definition shape
# ---------------------------------------------------------------------------


class TestEnsureIndexes:
    async def test_creates_compound_indexes(self) -> None:
        collection = _make_collection()
        client = _wire_client(collection)

        await ensure_indexes(
            client,
            "test_db",
            embedding_model=FakeEmbeddingModel(dimensions=8),
        )

        created_names = {
            call.kwargs.get("name") for call in collection.create_index.call_args_list
        }

        assert "kind_source_node" in created_names
        assert "kind_target_node" in created_names
        assert "kind_embedding" in created_names

    async def test_vector_index_includes_filter_fields(self) -> None:
        """The created vector index must declare ``kind``, ``type`` AND
        ``merged_into`` as filter paths so $vectorSearch can prune
        tombstones server-side."""

        collection = _make_collection()
        client = _wire_client(collection)

        await ensure_indexes(
            client,
            "test_db",
            embedding_model=FakeEmbeddingModel(dimensions=8),
        )

        collection.create_search_index.assert_awaited_once()
        model = collection.create_search_index.await_args.kwargs["model"]
        fields = model["definition"]["fields"]
        filter_paths = {f["path"] for f in fields if f.get("type") == "filter"}

        assert "kind" in filter_paths
        assert "type" in filter_paths
        assert "merged_into" in filter_paths

    async def test_vector_index_uses_live_model_dimensions(self) -> None:
        """``numDimensions`` is sourced from the live embedding model, not
        the YAML default."""

        collection = _make_collection()
        client = _wire_client(collection)

        await ensure_indexes(
            client,
            "test_db",
            embedding_model=FakeEmbeddingModel(dimensions=42),
        )

        model = collection.create_search_index.await_args.kwargs["model"]
        vector_field = next(
            f for f in model["definition"]["fields"] if f.get("type") == "vector"
        )
        assert vector_field["numDimensions"] == 42

    async def test_canonical_name_index_created(self) -> None:
        """A non-unique, sparse index on ``canonical_name`` must be created."""

        collection = _make_collection()
        client = _wire_client(collection)

        await ensure_indexes(
            client,
            "test_db",
            embedding_model=FakeEmbeddingModel(dimensions=8),
        )

        canonical_call = next(
            call
            for call in collection.create_index.call_args_list
            if call.kwargs.get("name") == _CANONICAL_NAME_INDEX
        )
        keys = canonical_call.args[0]
        assert keys == [("canonical_name", 1)]
        assert canonical_call.kwargs.get("sparse") is True
        assert canonical_call.kwargs.get("unique") is False

    async def test_text_index_covers_top_level_aliases(self) -> None:
        """The text index definition must cover both ``aliases``
        (top-level) and ``properties.aliases`` (legacy/back-compat)."""

        collection = _make_collection()
        client = _wire_client(collection)

        await ensure_indexes(
            client,
            "test_db",
            embedding_model=FakeEmbeddingModel(dimensions=8),
        )

        text_call = next(
            call
            for call in collection.create_index.call_args_list
            if call.kwargs.get("name") == _TEXT_INDEX_NAME
        )
        fields = text_call.args[0]
        paths = {path for path, _ in fields}
        assert "aliases" in paths
        assert "properties.aliases" in paths

    async def test_dimension_mismatch_drops_and_recreates_with_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Existing vector index with a different ``numDimensions`` must
        be dropped + recreated, with a WARNING that names both numbers."""

        existing = {
            "name": _VECTOR_INDEX_NAME,
            "latestDefinition": {
                "fields": [
                    {
                        "type": "vector",
                        "path": "embedding",
                        "numDimensions": 1536,
                        "similarity": "cosine",
                    },
                    {"type": "filter", "path": "kind"},
                    {"type": "filter", "path": "type"},
                    {"type": "filter", "path": "merged_into"},
                ]
            },
        }
        collection = _make_collection(initial_indexes=[existing])
        client = _wire_client(collection)

        with caplog.at_level("WARNING", logger="tree.memory.indexing.core"):
            await ensure_indexes(
                client,
                "test_db",
                embedding_model=FakeEmbeddingModel(dimensions=768),
            )

        collection.drop_search_index.assert_awaited_once_with(_VECTOR_INDEX_NAME)
        collection.create_search_index.assert_awaited_once()

        warning_text = " ".join(
            record.getMessage()
            for record in caplog.records
            if record.levelname == "WARNING"
        )
        assert "1536" in warning_text
        assert "768" in warning_text

    async def test_dimension_match_with_full_filters_is_noop(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """When the live vector index already has the target dimension AND
        every required filter path, the reconcile logic must NOT drop or
        recreate it (search-index ops only — classic indexes are still
        re-asserted because ``create_index`` is itself idempotent)."""

        existing = {
            "name": _VECTOR_INDEX_NAME,
            "latestDefinition": {
                "fields": [
                    {
                        "type": "vector",
                        "path": "embedding",
                        "numDimensions": 8,
                        "similarity": "cosine",
                    },
                    {"type": "filter", "path": "kind"},
                    {"type": "filter", "path": "type"},
                    {"type": "filter", "path": "merged_into"},
                ]
            },
        }
        collection = _make_collection(initial_indexes=[existing])
        client = _wire_client(collection)

        with caplog.at_level("WARNING", logger="tree.memory.indexing.core"):
            await ensure_indexes(
                client,
                "test_db",
                embedding_model=FakeEmbeddingModel(dimensions=8),
            )

        collection.drop_search_index.assert_not_awaited()
        collection.create_search_index.assert_not_awaited()

        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert warnings == []

    async def test_missing_filter_paths_triggers_recreate_without_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """An existing index missing ``merged_into`` must be recreated,
        but with no WARNING (only dimension mismatch warns)."""

        existing = {
            "name": _VECTOR_INDEX_NAME,
            "latestDefinition": {
                "fields": [
                    {
                        "type": "vector",
                        "path": "embedding",
                        "numDimensions": 8,
                        "similarity": "cosine",
                    },
                    {"type": "filter", "path": "kind"},
                    {"type": "filter", "path": "type"},
                ]
            },
        }
        collection = _make_collection(initial_indexes=[existing])
        client = _wire_client(collection)

        with caplog.at_level("WARNING", logger="tree.memory.indexing.core"):
            await ensure_indexes(
                client,
                "test_db",
                embedding_model=FakeEmbeddingModel(dimensions=8),
            )

        collection.drop_search_index.assert_awaited_once_with(_VECTOR_INDEX_NAME)
        collection.create_search_index.assert_awaited_once()
        warnings = [r for r in caplog.records if r.levelname == "WARNING"]
        assert warnings == []


# ---------------------------------------------------------------------------
# ensure_indexes — module-level constants
# ---------------------------------------------------------------------------


class TestVectorIndexDefinition:
    def test_definition_includes_required_filters(self) -> None:
        defn = _build_vector_index_definition(dimensions=16)
        filter_paths = {f["path"] for f in defn["fields"] if f.get("type") == "filter"}
        assert set(_VECTOR_INDEX_FILTER_PATHS) == {"kind", "type", "merged_into"}
        assert set(_VECTOR_INDEX_FILTER_PATHS).issubset(filter_paths)

    def test_text_index_fields_constant(self) -> None:
        paths = {path for path, _ in _TEXT_INDEX_FIELDS}
        assert "name" in paths
        assert "aliases" in paths
        assert "properties.aliases" in paths


# ---------------------------------------------------------------------------
# embed_nodes — backfill semantics
# ---------------------------------------------------------------------------


class _SpyEmbeddingModel(BaseEmbeddingModel):
    """Embedding model that records the texts it was asked to embed."""

    def __init__(self, dimensions: int = 4) -> None:
        self._dimensions = dimensions
        self.calls: list[list[str]] = []

    @property
    def dimensions(self) -> int:
        return self._dimensions

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [[0.1] * self._dimensions for _ in texts]


class TestEmbedNodesIsBackfillOnly:
    async def test_skips_nodes_with_non_empty_embedding(self, mocker) -> None:
        """Nodes with a non-empty ``embedding`` must NOT be re-embedded —
        the query filter is ``embedding in [[], None]`` so anything else is
        skipped."""

        # Arrange — only the "empty" node should make it through the query.
        empty_node = {
            "_id": "person:alice",
            "type": "person",
            "kind": "node",
            "properties": {},
            "embedding": [],
        }
        # The filled node is excluded by the query filter — we should never
        # see it in the fetched docs list.
        fetched_docs = [empty_node]

        collection = AsyncMock()
        collection.find = MagicMock(
            return_value=AsyncMock(to_list=AsyncMock(return_value=fetched_docs))
        )

        client = _wire_client(collection)
        spy_model = _SpyEmbeddingModel(dimensions=4)

        # Act
        embedded = await embed_nodes(client, "test_db", spy_model)

        # Assert
        assert embedded == 1
        # The query the function issues must exclude non-empty embeddings.
        find_filter = collection.find.call_args.args[0]
        assert find_filter == {"kind": "node", "embedding": {"$in": [[], None]}}
        # Only one batch with the single empty-embedding node was embedded.
        assert len(spy_model.calls) == 1
        assert len(spy_model.calls[0]) == 1

    async def test_no_docs_no_embed(self) -> None:
        """When every node already has a non-empty embedding, the embed
        call is a complete no-op (no batch call, zero count)."""

        collection = AsyncMock()
        collection.find = MagicMock(
            return_value=AsyncMock(to_list=AsyncMock(return_value=[]))
        )
        client = _wire_client(collection)
        spy_model = _SpyEmbeddingModel(dimensions=4)

        embedded = await embed_nodes(client, "test_db", spy_model)

        assert embedded == 0
        assert spy_model.calls == []


# ---------------------------------------------------------------------------
# _node_to_text (unchanged — retained from previous suite)
# ---------------------------------------------------------------------------


class TestNodeToText:
    def test_basic_node(self) -> None:
        node = {
            "_id": "person:alice",
            "type": "person",
            "properties": {"aliases": ["ali"]},
        }
        text = _node_to_text(node)
        assert "person: person:alice" in text
        assert "aliases" in text

    def test_node_with_content(self) -> None:
        node = {
            "_id": "chunk:chunk-0",
            "type": "chunk",
            "properties": {"content": "Hello world", "source_type": "substack"},
        }
        text = _node_to_text(node)
        # Content should appear last.
        assert text.endswith("Hello world")
        assert "source_type" in text

    def test_empty_properties(self) -> None:
        node = {"_id": "task:test", "type": "task", "properties": {}}
        text = _node_to_text(node)
        assert "task: task:test" in text

    def test_missing_fields(self) -> None:
        text = _node_to_text({})
        assert ": " in text
